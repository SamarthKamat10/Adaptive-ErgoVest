"""Run:  python -m pytest tests -q     (no hardware needed)"""
import json, os, pty, threading, time, tty, http.client

import numpy as np
import pytest

import auth, common, config, engine as eng_mod, trainer, webapp
from db import DB
from simulator import Sim, synth

ROW19 = "1000,0.1,9.7,1.2,0.01,0.02,0.03,45.0,27.0,33.0,310,320,300,1800,1820,1790,7.0,0.6,7.1"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    for d in ("data/labeled", "data/calibration", "models", "logs"): (tmp_path / d).mkdir(parents=True)
    monkeypatch.setattr(eng_mod, "HERE", str(tmp_path)); monkeypatch.setattr(trainer, "HERE", str(tmp_path))
    monkeypatch.setattr(trainer, "LABELED_DIR", str(tmp_path / "data/labeled"))
    monkeypatch.setattr(trainer, "MODEL_PATH", str(tmp_path / "models/movement_model.joblib"))
    monkeypatch.setattr(trainer, "META_PATH", str(tmp_path / "models/movement_model_meta.json"))
    monkeypatch.setattr(config, "SECRET_FILE", str(tmp_path / "secret.key"))
    auth._ser = None
    return tmp_path


@pytest.fixture()
def stack(sandbox):
    db = DB(str(sandbox / "t.db")); e = eng_mod.Engine(store=db, allow_any_port=True)
    app = webapp.create_app(e, db); app.testing = True
    webapp.login_throttle.hits.clear(); webapp.ip_throttle.hits.clear(); webapp.reg_throttle.hits.clear()
    return e, db, app


class Client:
    """Tiny browser: cookie jar + CSRF handling around Flask's test client."""
    def __init__(self, app): self.c = app.test_client(); self.csrf = None
    def page(self, path):
        r = self.c.get(path, follow_redirects=False)
        import re
        m = re.search(rb'name="csrf" content="([^"]+)"', r.data)
        if m: self.csrf = m.group(1).decode()
        return r
    def post(self, path, body=None, csrf=True):
        h = {"X-CSRF-Token": self.csrf} if csrf and self.csrf else {}
        return self.c.post(path, json=body or {}, headers=h, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})


def make_admin(app):
    c = Client(app); c.page("/setup")
    r = c.post("/api/auth/setup", dict(full_name="Ada Admin", username="ada", email="ada@x.io", password="Str0ngPassw0rd", confirm="Str0ngPassw0rd"))
    assert r.status_code == 200, r.data
    c.page("/app"); return c


# ---------- parsing / risk (original logic untouched) ----------
def test_parse_19_and_16_and_malformed():
    assert len(common.parse_line(ROW19)) == 19
    legacy = common.parse_line(",".join(ROW19.split(",")[:16]))
    assert len(legacy) == 19 and np.isnan(legacy[16])
    for bad in ["", "hello", "1,2,3", ROW19 + ",9", ROW19.replace("0.1", "abc", 1), "nan," + ROW19.split(",", 1)[1]]:
        assert common.parse_line(bad) is None
    nan_imu = ROW19.replace("0.1,9.7,1.2", "nan,nan,nan")
    assert common.parse_line(nan_imu) is None            # IMU failure is rejected
    env_nan = ROW19.replace("45.0,27.0,33.0", "nan,nan,nan")
    assert common.parse_line(env_nan) is not None         # DHT/DS failure is tolerated


def test_risk_breakdown_sums_to_score_and_levels():
    chunks = [synth("flat", 15, 1), synth("bend", 20, 2, peak=35), synth("twist", 20, 3), synth("bend", 15, 4, peak=40)]
    base = common.compute_baseline(chunks, "T")
    sim = Sim(); buf = []; seen = set()
    for i in range(50 * 60):
        buf.append(sim.next()); buf = buf[-common.WIN:]
        if len(buf) == common.WIN and i % common.STEP == 0:
            s, lv, parts, _ = common.risk_score(np.array(buf), base, 0)
            assert abs(sum(common.risk_breakdown(parts).values()) - s) < 1e-6
            seen.add(lv)
    assert seen == {"Low", "Moderate", "Elevated", "High"}


# ---------- authentication ----------
def test_password_policy_and_hashing(stack):
    assert auth.validate_password("short1") and auth.validate_password("alllettersonly") and auth.validate_password("1234567890")
    assert not auth.validate_password("Str0ngPassw0rd", "ada")
    h = auth.hash_password("Str0ngPassw0rd")
    assert "Str0ngPassw0rd" not in h and auth.verify_password("Str0ngPassw0rd", h) and not auth.verify_password("wrong", h)


def test_protected_routes_and_flow(stack):
    e, db, app = stack
    anon = Client(app)
    assert anon.c.get("/", follow_redirects=False).headers["Location"].endswith("/setup")
    admin = make_admin(app)
    anon = Client(app)
    for p in ["/api/state", "/api/stream", "/api/ports", "/api/sessions", "/api/me", "/api/lan", "/api/export/session"]:
        assert anon.c.get(p).status_code == 401, p
    assert anon.c.get("/app", follow_redirects=False).status_code == 302
    assert anon.c.get("/setup", follow_redirects=False).status_code == 302          # setup closed after first admin
    assert admin.c.get("/api/state").status_code == 200
    # registration: duplicate + validation + csrf
    u = Client(app); u.page("/register")
    body = dict(full_name="Bo", username="bob", email="bo@x.io", password="An0therStrongPw", confirm="An0therStrongPw")
    assert u.post("/api/auth/register", body, csrf=False).status_code == 403          # no CSRF token
    assert u.post("/api/auth/register", body).status_code == 200
    d = Client(app); d.page("/register")
    assert d.post("/api/auth/register", dict(body, username="BOB", email="other@x.io")).status_code == 409   # case-insensitive dup
    assert d.post("/api/auth/register", dict(body, username="x y", email="nope")).status_code == 400
    assert d.post("/api/auth/register", dict(body, username="cy", email="cy@x.io", password="weak", confirm="weak")).status_code == 400
    # plain-text password never stored
    with db.conn() as c: assert all("An0therStrongPw" not in r["pw_hash"] for r in c.execute("SELECT pw_hash FROM users"))
    u.page("/app")                                                                       # browser lands on /app after sign-up
    # member cannot use admin endpoints but can run motor test route
    assert u.post("/api/connect", {"port": "x"}).status_code == 403 and u.post("/api/calibrate", {}).status_code == 403
    assert u.post("/api/cue", {"cmd": "L"}).status_code == 400                            # honest: no ESP32 attached
    # logout kills the session server-side
    cookie = u.c.get_cookie("ergovest_sid").value
    assert u.post("/api/auth/logout").status_code == 200
    stale = Client(app); stale.c.set_cookie("ergovest_sid", cookie)
    assert stale.c.get("/api/me").status_code == 401


def test_login_errors_and_throttle(stack):
    e, db, app = stack; make_admin(app)
    c = Client(app); c.page("/login")
    r = c.post("/api/auth/login", dict(identifier="ada", password="nope"))
    r2 = c.post("/api/auth/login", dict(identifier="ghost", password="nope"))
    assert r.status_code == r2.status_code == 401 and r.get_json()["msg"] == r2.get_json()["msg"]
    codes = [c.post("/api/auth/login", dict(identifier="ada", password="nope")).status_code for _ in range(5)]
    assert 429 in codes
    assert c.post("/api/auth/login", dict(identifier="ada", password="Str0ngPassw0rd")).status_code == 429   # locked even for right pw


def test_security_headers_and_cookie_flags(stack):
    e, db, app = stack; admin = make_admin(app)
    r = admin.c.get("/app")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"] and r.headers["X-Frame-Options"] == "DENY"
    c = Client(app); c.page("/login")
    r = c.post("/api/auth/login", dict(identifier="ada", password="Str0ngPassw0rd", remember=True))
    sc = r.headers["Set-Cookie"]; assert "HttpOnly" in sc and "SameSite=Lax" in sc and "Max-Age" in sc


def test_session_history_isolated_between_users(stack):
    e, db, app = stack; a = make_admin(app); e.demo_base = e.demo_model = None; e._prepare_demo()
    b = Client(app); b.page("/register")
    assert b.post("/api/auth/register", dict(full_name="Bo", username="bob", email="bo@x.io", password="An0therStrongPw", confirm="An0therStrongPw")).status_code == 200
    b.page("/app")
    assert a.post("/api/session/start").status_code == 200
    for _ in range(300): e.feed(e.sim.next())
    assert a.post("/api/session/stop").status_code == 200
    ses = a.c.get("/api/sessions").get_json()["sessions"]; assert len(ses) == 1 and ses[0]["mode"] == "demo" and ses[0]["windows"] > 0
    assert b.c.get("/api/sessions").get_json()["sessions"] == []
    assert b.c.get(f"/api/sessions/{ses[0]['id']}").status_code == 404
    assert b.c.delete(f"/api/sessions/{ses[0]['id']}", headers={"X-CSRF-Token": b.csrf}).get_json()["ok"] is False
    assert a.c.get(f"/api/sessions/{ses[0]['id']}/export.csv").status_code == 200


# ---------- engine: DEMO, missing models/data ----------
def test_demo_and_missing_models(sandbox):
    e = eng_mod.Engine(); assert e.model is None and e.base is None                      # nothing on disk: no crash
    assert e.model_info()["has_baseline"] is False                                       # demo brain not built yet => 'preparing'
    e._prepare_demo()
    for _ in range(200): e.feed(e.sim.next())
    assert e.status["level"] in common_levels() and e.model_info()["synthetic"] is True
    assert e.send_cue("L")[0] is False                                                   # no fake success
    e.use_live(); assert e.conn_state() == "disconnected" and e.status == {}            # demo values do not leak into LIVE
    with pytest.raises(RuntimeError, match="No recordings"): trainer.train(log=lambda *_: None)
    e.task = {"name": "idle", "text": "", "remaining": 0}; e._wrap(e.task_train)         # LIVE, no data -> readable error event
    assert any("No recordings" in ev["msg"] for ev in e.events)
    e.use_demo(); e._wrap(e.task_train); assert any("DEMO data is never used" in ev["msg"] for ev in e.events)


def common_levels(): return [n for _, _, n in common.LEVELS]


def test_trainer_on_recorded_files(sandbox):
    for k, (kind, pk) in enumerate([("flat", 0), ("flat", 0), ("flat", 0), ("bend", 60), ("bend", 60), ("bend", 60)]):
        common.save_csv(str(sandbox / "data/labeled" / f"{'standing' if kind == 'flat' else 'bend_deep'}__{k}.csv"), synth(kind, 30, k, peak=pk))
    lines = []; m = trainer.train(log=lines.append)
    assert m.ergovest_meta["cv_accuracy"] is not None and (sandbox / "models/movement_model.joblib").exists()
    assert any("Cross-validated accuracy" in l for l in lines)


def test_worker_name_and_label_validation(stack):
    e, db, app = stack; a = make_admin(app)
    assert a.post("/api/calibrate", {"worker": "../../evil"}).status_code == 400
    assert a.post("/api/record", {"label": "../x", "seconds": 10}).status_code == 400


# ---------- serial: ESP32 emulated on a pseudo-terminal (NOT real hardware) ----------
class FakeEsp:
    def __init__(self, sim=False):
        self.sim = Sim() if sim else None
        self.master, slave = pty.openpty(); tty.setraw(slave); self.name = os.ttyname(slave); self.slave = slave
        self.got = b""; self.run = True; self.rate = 50; self.mode = "good"; self.n = 0
        threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        time.sleep(0.8)                       # the real ESP32 resets AFTER the port is opened, then prints its boot text
        os.write(self.master, b"MPU6050 ready (direct I2C register mode).\r\nERROR: DS18B20 not found.\r\ntimestamp_ms,ax,ay\r\n")
        while self.run:
            if self.mode == "good":
                self.n += 1
                line = ROW19.replace("1000", str(self.n * 20), 1) if not self.sim else ",".join(f"{v:.4f}" for v in self.sim.next())
                os.write(self.master, (line + "\r\n").encode())
                if self.n % 10 == 0: os.write(self.master, b"1,2,3\r\n garbage ,,,\r\n" + ROW19.replace("0.1,9.7,1.2", "nan,nan,nan").encode() + b"\r\n")
            time.sleep(1 / self.rate)
    def read_cmds(self):
        import select
        while select.select([self.master], [], [], 0)[0]: self.got += os.read(self.master, 64)
        return self.got


def wait(cond, t=6):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False


def test_serial_live_path_with_emulated_esp32(sandbox, monkeypatch):
    monkeypatch.setattr(time, "sleep", time.sleep)          # real sleep; connect() waits 2 s for the ESP32 reset
    esp = FakeEsp(); e = eng_mod.Engine(allow_any_port=True); e.start_background()
    ok, msg = e.connect(esp.name); assert ok, msg
    assert wait(lambda: e.conn_state() == "ok"), "no live data"
    assert wait(lambda: e.n > 40) and e.mode == "live" and e.port == esp.name
    assert e.diag["bad_lines"] > 0 and e.diag["imu_invalid"] > 0                          # malformed + IMU-invalid lines counted, not fed
    assert all(len(r) == 19 for _, r in e.raw)
    assert any("DS18B20 not found" in ev["msg"] for ev in e.events)                       # firmware text surfaced
    for cmd in "LRBFS":
        assert e.send_cue(cmd)[0] is True
    assert wait(lambda: len(esp.read_cmds()) >= 10); assert esp.read_cmds() == b"L\nR\nB\nF\nS\n"   # exact protocol bytes
    assert e.send_cue("X")[0] is False
    # stale: ESP32 stops talking but port stays open
    esp.mode = "silent"; assert wait(lambda: e.conn_state() == "stale", 5)
    assert any("stopped arriving" in ev["msg"] for ev in e.events)
    esp.mode = "good"; assert wait(lambda: e.conn_state() == "ok", 5)
    # unplug: close the far end of the pty
    n_before = e.n; esp.run = False; time.sleep(.2); os.close(esp.master)
    assert wait(lambda: e.ser is None, 6), "disconnect not detected"
    assert e.conn_state() == "disconnected" and e.send_cue("L")[0] is False
    e.shutdown()


def test_single_serial_reader_and_unlisted_ports_rejected(sandbox):
    e = eng_mod.Engine(allow_any_port=False)
    ok, msg = e.connect("loop://"); assert not ok and "not an available serial port" in msg
    ok, msg = e.connect("COM99"); assert not ok


# ---------- shared live stream: two authenticated clients, one engine ----------
def test_two_clients_receive_same_stream(sandbox):
    from werkzeug.serving import make_server
    db = DB(str(sandbox / "t.db")); e = eng_mod.Engine(store=db); e._prepare_demo(); e.start_background()
    app = webapp.create_app(e, db); srv = make_server("127.0.0.1", 0, app, threaded=True); port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    admin = make_admin(app) if False else None
    uid = db.create_user("ada", "a@x.io", "Ada", auth.hash_password("Str0ngPassw0rd"), "admin")
    uid2 = db.create_user("bob", "b@x.io", "Bo", auth.hash_password("Str0ngPassw0rd"), "user")
    def cookie(u): t = auth.new_token(); db.add_auth(auth.token_hash(t), u, False, "x", "t"); return t
    def reader(tok, out):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/api/stream", headers={"Cookie": f"ergovest_sid={tok}"})
        r = c.getresponse(); assert r.status == 200
        buf = b""; t0 = time.time()
        while time.time() - t0 < 2.5:
            ch = r.readline()
            if ch.startswith(b"data: "): out.append(json.loads(ch[6:]))
        c.close()
    o1, o2 = [], []
    ts = [threading.Thread(target=reader, args=(cookie(uid), o1)), threading.Thread(target=reader, args=(cookie(uid2), o2))]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(o1) > 10 and len(o2) > 10 and o1[0]["type"] == "full" and o2[0]["type"] == "full"
    common_seq = set(f["seq"] for f in o1) & set(f["seq"] for f in o2)
    assert len(common_seq) > 5                                                    # both saw the same samples
    assert max(f["clients"] for f in o1) == 2
    # incremental frames never repeat a row
    seen = []
    for f in o1[1:]: seen += [r[0] for r in f["rows"]]
    assert len(seen) == len(set(seen))
    assert e.ser is None                                                          # no serial reader per client
    # unauthenticated stream refused
    c = http.client.HTTPConnection("127.0.0.1", port); c.request("GET", "/api/stream"); assert c.getresponse().status == 401
    srv.shutdown(); e.shutdown()


def test_reader_survives_processing_error_and_degenerate_calibration(sandbox):
    e = eng_mod.Engine(); e._prepare_demo()
    bad = dict(e.demo_base); bad["feat_mean"] = ["x"]; e.demo_base = bad         # processing will raise
    threading.Thread(target=e.run, daemon=True).start()
    assert wait(lambda: e.n > 120, 8) and any("Processing error" in ev["msg"] for ev in e.events)   # thread still alive & reporting
    # motionless calibration data is refused instead of saving a /0 baseline
    flat = [synth("flat", 25, 1)[:, :].copy() for _ in range(4)]
    for f in flat: f[:, 1:4] = f[0, 1:4]
    b = common.compute_baseline(flat, "T"); assert b["jerk_ref"] < 0.05
    w = np.array(flat[0][:100]); b["jerk_ref"] = 0.0
    common.risk_score(w, dict(b, feat_std=[1.0] * len(b["feat_std"])), 0)            # guard: no ZeroDivisionError


def test_short_session_still_has_a_chart_point(sandbox):
    r = eng_mod.Recorder(1, 1, "demo", time.time())
    for i in range(5): r.add(r.started + i * .5, 40.0 + i, "Moderate", 20.0)
    s = r.summary(); assert s["windows"] == 5 and len(s["series"]) == 1 and s["series"][0][2] == 44.0
