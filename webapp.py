"""Flask web app: authentication, protected API, live SSE stream. Hardware logic stays in engine.py."""
import csv, io, json, os, re, threading, time
from functools import wraps

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, url_for

import auth, config, netinfo
from engine import LABELS, SAFE_NAME, STALE_SECONDS

COOKIE = "ergovest_sid"
login_throttle = auth.Throttle(5, 600)        # 5 failures / 10 min per ip+account
ip_throttle = auth.Throttle(30, 600)
reg_throttle = auth.Throttle(10, 3600)


def create_app(engine, db):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.config["JSON_SORT_KEYS"] = False
    app.config["TEMPLATES_AUTO_RELOAD"] = False
    clients = {"n": 0}
    ctx = {"serve_port": config.HTTP_PORT}
    app.extensions["ergovest"] = ctx

    # ---------------- helpers ----------------
    def ip(): return request.remote_addr or "?"

    def current():
        """Return (user_row, token_hash) for a valid login cookie, else (None, None)."""
        tok = request.cookies.get(COOKIE)
        if not tok: return None, None
        th = auth.token_hash(tok)
        s = db.get_auth(th)
        if not s: return None, None
        idle = (config.REMEMBER_DAYS * 86400) if s["remember"] else (config.SESSION_IDLE_HOURS * 3600)
        if time.time() - s["last_seen"] > idle:
            db.del_auth(th); return None, None
        u = db.user(s["user_id"])
        if not u: return None, None
        if time.time() - s["last_seen"] > 60: db.touch_auth(th)
        return u, th

    @app.before_request
    def load_user():
        g.user, g.sid = current()
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.path.startswith("/api/"):
            if not auth.check_csrf(request.headers.get("X-CSRF-Token"), g.sid):
                return jsonify(ok=False, error="csrf", msg="Your page expired. Reloading..."), 403

    def api_login_required(f):
        @wraps(f)
        def w(*a, **k):
            if not g.user: return jsonify(ok=False, error="auth", msg="Please sign in"), 401
            return f(*a, **k)
        return w

    def admin_required(f):
        @wraps(f)
        def w(*a, **k):
            if g.user["role"] != "admin": return jsonify(ok=False, msg="Only the administrator can do this"), 403
            return f(*a, **k)
        return api_login_required(w)

    @app.after_request
    def headers(r):
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Referrer-Policy"] = "same-origin"
        r.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.path.startswith("/api/") or request.path in ("/login", "/register", "/app", "/setup"):
            r.headers["Cache-Control"] = "no-store"
        return r

    def page(tpl, **kw):
        return render_template(tpl, csrf=auth.make_csrf(g.sid), allow_reg=config.ALLOW_REGISTRATION, **kw)

    def is_local(): return request.remote_addr in ("127.0.0.1", "::1")

    def set_cookie(resp, token, remember):
        resp.set_cookie(COOKIE, token, httponly=True, samesite="Lax", secure=config.COOKIE_SECURE,
                        max_age=int(config.REMEMBER_DAYS * 86400) if remember else None, path="/")

    def login_user(uid, remember):
        token = auth.new_token()
        db.add_auth(auth.token_hash(token), uid, remember, ip(), request.headers.get("User-Agent"))
        db.update_user(uid, last_login=time.time())
        db.purge_auth(config.SESSION_IDLE_HOURS * 3600, config.REMEMBER_DAYS * 86400)
        return token

    def pub(u):
        return {"id": u["id"], "username": u["username"], "email": u["email"], "full_name": u["full_name"],
                "role": u["role"], "created": u["created"], "last_login": u["last_login"]}

    # ---------------- pages ----------------
    @app.route("/")
    def index():
        if db.count_users() == 0: return redirect(url_for("setup"))
        return redirect(url_for("app_page") if g.user else url_for("login"))

    @app.route("/setup")
    def setup():
        if db.count_users() > 0: return redirect(url_for("login"))
        return page("setup.html", local=is_local())

    @app.route("/login")
    def login():
        if db.count_users() == 0: return redirect(url_for("setup"))
        if g.user: return redirect(url_for("app_page"))
        return page("login.html")

    @app.route("/register")
    def register():
        if db.count_users() == 0: return redirect(url_for("setup"))
        if g.user: return redirect(url_for("app_page"))
        if not config.ALLOW_REGISTRATION: return redirect(url_for("login"))
        return page("register.html")

    @app.route("/forgot")
    def forgot():
        return page("forgot.html")

    @app.route("/app")
    def app_page():
        if not g.user: return redirect(url_for("login"))
        return page("app.html", me=pub(g.user), labels=LABELS)

    # ---------------- auth API ----------------
    @app.post("/api/auth/setup")
    def api_setup():
        if db.count_users() > 0: return jsonify(ok=False, msg="Setup is already complete"), 403
        if not is_local(): return jsonify(ok=False, msg="Create the administrator on the laptop itself (http://localhost)"), 403
        return create_account(role="admin", first=True)

    @app.post("/api/auth/register")
    def api_register():
        if db.count_users() == 0: return jsonify(ok=False, msg="Run first-time setup first"), 403
        if not config.ALLOW_REGISTRATION: return jsonify(ok=False, msg="Registration is turned off"), 403
        w = reg_throttle.blocked(ip())
        if w: return jsonify(ok=False, msg=f"Too many sign-ups from this device. Try again in {w // 60 + 1} min"), 429
        reg_throttle.hit(ip())
        return create_account(role="user")

    def create_account(role, first=False):
        d = request.get_json(silent=True) or {}
        full = auth.clean_text(d.get("full_name"), 80); username = auth.clean_text(d.get("username"), 32)
        email = auth.clean_text(d.get("email"), 254); pw = d.get("password") or ""
        errs = auth.validate_identity(username, email, full)
        pe = auth.validate_password(pw, username, email)
        if pe: errs["password"] = " ".join(pe)
        if not isinstance(pw, str) or pw != d.get("confirm"): errs["confirm"] = "Passwords do not match."
        if errs: return jsonify(ok=False, errors=errs, msg="Please fix the highlighted fields"), 400
        dup = db.exists(username, email)
        if dup: return jsonify(ok=False, errors={dup: f"That {dup} is already registered."}, msg="Account already exists"), 409
        uid = db.create_user(username, email, full, auth.hash_password(pw), role, only_if_empty=first)
        if not uid: return jsonify(ok=False, msg="Could not create the account (already exists)"), 409
        resp = jsonify(ok=True, next=url_for("app_page"))
        set_cookie(resp, login_user(uid, False), False)
        return resp

    @app.post("/api/auth/login")
    def api_login():
        d = request.get_json(silent=True) or {}
        ident = auth.clean_text(d.get("identifier"), 254); pw = d.get("password") or ""
        key = f"{ip()}|{ident.lower()}"
        w = max(login_throttle.blocked(key), ip_throttle.blocked(ip()))
        if w: return jsonify(ok=False, msg=f"Too many attempts. Try again in {w // 60 + 1} min"), 429
        u = db.user_by_ident(ident) if ident and isinstance(pw, str) else None
        if u and auth.verify_password(pw, u["pw_hash"]):
            login_throttle.clear(key)
            resp = jsonify(ok=True, next=url_for("app_page"))
            set_cookie(resp, login_user(u["id"], bool(d.get("remember"))), bool(d.get("remember")))
            return resp
        if not u: auth.verify_dummy(pw if isinstance(pw, str) else "")
        login_throttle.hit(key); ip_throttle.hit(ip())
        return jsonify(ok=False, msg="Incorrect username/email or password"), 401

    @app.post("/api/auth/logout")
    def api_logout():
        if g.sid: db.del_auth(g.sid)
        resp = jsonify(ok=True, next=url_for("login"))
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/csrf")
    def api_csrf():
        return jsonify(csrf=auth.make_csrf(g.sid))

    @app.get("/api/me")
    @api_login_required
    def api_me():
        return jsonify(ok=True, user=pub(g.user))

    @app.post("/api/me/profile")
    @api_login_required
    def api_profile():
        d = request.get_json(silent=True) or {}
        full = auth.clean_text(d.get("full_name"), 80); email = auth.clean_text(d.get("email"), 254)
        errs = {k: v for k, v in auth.validate_identity(g.user["username"], email, full).items() if k != "username"}
        if not errs and db.exists(email=email, exclude_id=g.user["id"]): errs["email"] = "That email is already registered."
        if errs: return jsonify(ok=False, errors=errs, msg="Please fix the highlighted fields"), 400
        db.update_user(g.user["id"], full_name=full, email=email)
        return jsonify(ok=True, user=pub(db.user(g.user["id"])), msg="Profile saved")

    @app.post("/api/me/password")
    @api_login_required
    def api_password():
        d = request.get_json(silent=True) or {}
        if not auth.verify_password(d.get("current") or "", g.user["pw_hash"]):
            return jsonify(ok=False, errors={"current": "Current password is incorrect."}, msg="Current password is incorrect"), 400
        new = d.get("password") or ""
        errs = auth.validate_password(new, g.user["username"], g.user["email"])
        if new != d.get("confirm"): errs.append("Passwords do not match.")
        if errs: return jsonify(ok=False, errors={"password": " ".join(errs)}, msg="New password rejected"), 400
        db.update_user(g.user["id"], pw_hash=auth.hash_password(new))
        db.del_user_auth(g.user["id"], except_hash=g.sid)          # sign out other devices
        return jsonify(ok=True, msg="Password changed. Other devices were signed out")

    # ---------------- live data ----------------
    def attach(d, uid):
        d["session"] = engine.session_info(uid); d["clients"] = clients["n"]
        return d

    @app.get("/api/state")
    @api_login_required
    def api_state():
        return jsonify(attach(engine.snapshot(), g.user["id"]))

    @app.get("/api/stream")
    @api_login_required
    def api_stream():
        uid, sid = g.user["id"], g.sid
        def gen():
            clients["n"] += 1
            try:
                yield "retry: 2000\n\n"
                snap = attach(engine.snapshot(), uid)
                cursor, ev, epoch = snap["seq"], snap["event_seq"], snap["epoch"]
                yield f"data: {json.dumps(snap)}\n\n"
                last_check = last_beat = time.time()
                while not engine.stop:
                    time.sleep(0.1)
                    now = time.time()
                    if now - last_check > 20:
                        last_check = now
                        s = db.get_auth(sid)
                        if not s: yield "event: auth\ndata: {}\n\n"; return
                    f = engine.frame(cursor, ev)
                    if f["epoch"] != epoch:
                        f = engine.snapshot(); epoch = f["epoch"]
                    cursor, ev = f["seq"], f["event_seq"]
                    attach(f, uid)
                    yield f"data: {json.dumps(f)}\n\n"
            finally:
                clients["n"] -= 1
        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    # ---------------- hardware / controls ----------------
    def res(r): return jsonify(ok=r[0], msg=r[1]), (200 if r[0] else 400)

    @app.get("/api/ports")
    @api_login_required
    def api_ports(): return jsonify(ports=engine.list_ports())

    @app.post("/api/connect")
    @admin_required
    def api_connect(): return res(engine.connect((request.get_json(silent=True) or {}).get("port", "")))

    @app.post("/api/disconnect")
    @admin_required
    def api_disconnect(): return res(engine.disconnect())

    @app.post("/api/mode")
    @admin_required
    def api_mode():
        m = (request.get_json(silent=True) or {}).get("mode")
        if m == "demo": return res(engine.use_demo())
        if m == "live": return res(engine.use_live())
        return jsonify(ok=False, msg="mode must be demo or live"), 400

    @app.post("/api/cue")
    @api_login_required
    def api_cue():
        cmd = str((request.get_json(silent=True) or {}).get("cmd", ""))[:1]
        ok, msg = engine.send_cue(cmd)
        if ok: engine.event(f"Manual motor test: {cmd} (by {g.user['username']})", "info")
        return jsonify(ok=ok, msg=msg), (200 if ok else 400)

    @app.post("/api/reset")
    @admin_required
    def api_reset():
        engine.reset_session(); return jsonify(ok=True, msg="Counters reset")

    @app.post("/api/calibrate")
    @admin_required
    def api_calibrate():
        w = auth.clean_text((request.get_json(silent=True) or {}).get("worker", "W01"), 20) or "W01"
        if not SAFE_NAME.match(w): return jsonify(ok=False, msg="Worker ID: letters, numbers, dash, underscore (max 20)"), 400
        return res(engine.start_task(engine.task_calibrate, w))

    @app.post("/api/record")
    @admin_required
    def api_record():
        d = request.get_json(silent=True) or {}
        if d.get("label") not in LABELS: return jsonify(ok=False, msg="Unknown movement label"), 400
        try: secs = max(5, min(120, int(d.get("seconds", 30))))
        except (TypeError, ValueError): return jsonify(ok=False, msg="Seconds must be a number"), 400
        return res(engine.start_task(engine.task_record, d["label"], secs))

    @app.post("/api/train")
    @admin_required
    def api_train(): return res(engine.start_task(engine.task_train))

    @app.post("/api/cancel")
    @admin_required
    def api_cancel(): return res(engine.cancel_task())

    # ---------------- monitoring sessions ----------------
    @app.post("/api/session/start")
    @api_login_required
    def api_sess_start():
        ok, msg, _ = engine.start_session(g.user["id"]); return jsonify(ok=ok, msg=msg), (200 if ok else 400)

    @app.post("/api/session/stop")
    @api_login_required
    def api_sess_stop(): return res(engine.stop_session(g.user["id"]))

    @app.get("/api/sessions")
    @api_login_required
    def api_sessions(): return jsonify(sessions=db.sessions(g.user["id"]), active=engine.session_info(g.user["id"]))

    @app.get("/api/sessions/<int:sid>")
    @api_login_required
    def api_session(sid):
        s = db.session(g.user["id"], sid)
        return (jsonify(ok=True, session=s) if s else (jsonify(ok=False, msg="Not found"), 404))

    @app.delete("/api/sessions/<int:sid>")
    @api_login_required
    def api_sess_del(sid):
        return jsonify(ok=bool(db.delete_session(g.user["id"], sid)))

    @app.get("/api/sessions/<int:sid>/export.csv")
    @api_login_required
    def api_sess_csv(sid):
        s = db.session(g.user["id"], sid)
        if not s: abort(404)
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(["seconds_from_start", "avg_risk", "max_risk", "avg_forward_flexion_deg"]); w.writerows(s["series"])
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=ergovest_session_{sid}_{s['mode']}.csv"})

    def csv_file(name):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", name)
        data = open(p, "rb").read() if os.path.exists(p) else b"no data yet\n"
        return Response(data, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.get("/api/export/verification")
    @admin_required
    def exp_ver(): return csv_file("verification_log.csv")

    @app.get("/api/export/session")
    @admin_required
    def exp_ses(): return csv_file("session_log.csv")

    # ---------------- info ----------------
    @app.get("/api/lan")
    @api_login_required
    def api_lan():
        urls = [f"http://{i}:{ctx['serve_port']}" for i in netinfo.lan_ips()]
        return jsonify(urls=urls, qr=netinfo.qr_svg(urls[0]) if urls else None, port=ctx["serve_port"],
                       bound=config.HOST, clients=clients["n"])

    @app.errorhandler(404)
    def nf(e):
        if request.path.startswith("/api/"): return jsonify(ok=False, msg="Not found"), 404
        return redirect(url_for("index"))

    @app.errorhandler(413)
    def big(e): return jsonify(ok=False, msg="Request too large"), 413

    @app.errorhandler(500)
    def err(e):
        app.logger.exception("server error")
        return jsonify(ok=False, msg="Something went wrong on the server"), 500

    return app
