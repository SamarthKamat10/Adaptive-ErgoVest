"""SQLite storage: accounts, login sessions, monitoring-session history."""
import json, os, sqlite3, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL, username_lc TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL, email_lc TEXT NOT NULL UNIQUE,
  full_name TEXT NOT NULL, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user', created REAL NOT NULL, last_login REAL);
CREATE TABLE IF NOT EXISTS auth_sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created REAL NOT NULL, last_seen REAL NOT NULL, remember INTEGER NOT NULL, ip TEXT, ua TEXT);
CREATE TABLE IF NOT EXISTS monitor_sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mode TEXT NOT NULL, started REAL NOT NULL, ended REAL, status TEXT NOT NULL DEFAULT 'active',
  windows INTEGER DEFAULT 0, avg_risk REAL, max_risk REAL, cues INTEGER DEFAULT 0,
  levels TEXT DEFAULT '{}', series TEXT DEFAULT '[]', note TEXT);
CREATE INDEX IF NOT EXISTS ix_ms_user ON monitor_sessions(user_id, started DESC);
"""


class DB:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            # a crash leaves 'active' rows behind: mark them honestly
            c.execute("UPDATE monitor_sessions SET status='interrupted', ended=COALESCE(ended, started) WHERE status='active'")

    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ----- users -----
    def count_users(self):
        with self.conn() as c: return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, username, email, full_name, pw_hash, role="user", only_if_empty=False):
        """Returns user id, or None when the username/email already exists (or db not empty)."""
        with self.conn() as c:
            try:
                c.execute("BEGIN IMMEDIATE")
                if only_if_empty and c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                    return None
                cur = c.execute("INSERT INTO users(username,username_lc,email,email_lc,full_name,pw_hash,role,created) VALUES(?,?,?,?,?,?,?,?)",
                                (username, username.lower(), email, email.lower(), full_name, pw_hash, role, time.time()))
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def user_by_ident(self, ident):
        ident = ident.strip().lower()
        with self.conn() as c:
            return c.execute("SELECT * FROM users WHERE username_lc=? OR email_lc=?", (ident, ident)).fetchone()

    def user(self, uid):
        with self.conn() as c: return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def exists(self, username=None, email=None, exclude_id=None):
        with self.conn() as c:
            if username and c.execute("SELECT 1 FROM users WHERE username_lc=? AND id IS NOT ?", (username.lower(), exclude_id)).fetchone(): return "username"
            if email and c.execute("SELECT 1 FROM users WHERE email_lc=? AND id IS NOT ?", (email.lower(), exclude_id)).fetchone(): return "email"
        return None

    def update_user(self, uid, **f):
        allowed = {"full_name", "email", "pw_hash", "last_login"}
        sets, vals = [], []
        for k, v in f.items():
            if k not in allowed: continue
            sets.append(f"{k}=?"); vals.append(v)
            if k == "email": sets.append("email_lc=?"); vals.append(v.lower())
        if sets:
            with self.conn() as c: c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", (*vals, uid))

    # ----- login sessions -----
    def add_auth(self, token_hash, uid, remember, ip, ua):
        now = time.time()
        with self.conn() as c:
            c.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?)", (token_hash, uid, now, now, int(remember), ip, (ua or "")[:200]))

    def get_auth(self, token_hash):
        with self.conn() as c: return c.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_hash,)).fetchone()

    def touch_auth(self, token_hash):
        with self.conn() as c: c.execute("UPDATE auth_sessions SET last_seen=? WHERE token_hash=?", (time.time(), token_hash))

    def del_auth(self, token_hash):
        with self.conn() as c: c.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))

    def del_user_auth(self, uid, except_hash=None):
        with self.conn() as c: c.execute("DELETE FROM auth_sessions WHERE user_id=? AND token_hash IS NOT ?", (uid, except_hash))

    def purge_auth(self, idle_s, remember_s):
        now = time.time()
        with self.conn() as c:
            c.execute("DELETE FROM auth_sessions WHERE (remember=0 AND last_seen<?) OR (remember=1 AND last_seen<?)", (now - idle_s, now - remember_s))

    # ----- monitoring sessions (also the engine's `store`) -----
    def create(self, user_id, mode, started):
        with self.conn() as c:
            return c.execute("INSERT INTO monitor_sessions(user_id,mode,started) VALUES(?,?,?)", (user_id, mode, started)).lastrowid

    def update(self, sid, now, s):
        with self.conn() as c:
            c.execute("UPDATE monitor_sessions SET ended=?,windows=?,avg_risk=?,max_risk=?,cues=?,levels=?,series=? WHERE id=?",
                      (now, s["windows"], s["avg_risk"], s["max_risk"], s["cues"], json.dumps(s["levels"]), json.dumps(s["series"]), sid))

    def finish(self, sid, now, status, s, note):
        self.update(sid, now, s)
        with self.conn() as c: c.execute("UPDATE monitor_sessions SET status=?, note=? WHERE id=?", (status, note, sid))

    def sessions(self, user_id):
        with self.conn() as c:
            rows = c.execute("SELECT id,mode,started,ended,status,windows,avg_risk,max_risk,cues,levels,note FROM monitor_sessions WHERE user_id=? ORDER BY started DESC LIMIT 200", (user_id,)).fetchall()
        return [dict(r, levels=json.loads(r["levels"] or "{}")) for r in rows]

    def session(self, user_id, sid):
        with self.conn() as c:
            r = c.execute("SELECT * FROM monitor_sessions WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
        return dict(r, levels=json.loads(r["levels"] or "{}"), series=json.loads(r["series"] or "[]")) if r else None

    def delete_session(self, user_id, sid):
        with self.conn() as c:
            return c.execute("DELETE FROM monitor_sessions WHERE id=? AND user_id=? AND status!='active'", (sid, user_id)).rowcount
