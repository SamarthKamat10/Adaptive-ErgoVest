"""Password policy/hashing, login throttling, login sessions and CSRF tokens."""
import hashlib, os, re, secrets, stat, time
from collections import defaultdict, deque

from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

import config

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
COMMON = {"password", "password1", "password123", "1234567890", "qwertyuiop", "iloveyou123", "welcome123",
          "admin12345", "letmein123", "ergovest123", "adaptive123", "changeme123", "0123456789", "abcdefghij1"}
_DUMMY = generate_password_hash("dummy-password-for-timing")


def secret_key():
    """Random key generated on first run, stored outside the source in instance/secret.key."""
    if not os.path.exists(config.SECRET_FILE):
        os.makedirs(os.path.dirname(config.SECRET_FILE), exist_ok=True)
        with open(config.SECRET_FILE, "w") as f: f.write(secrets.token_hex(32))
        try: os.chmod(config.SECRET_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError: pass
    return open(config.SECRET_FILE).read().strip()


_ser = None
def _signer():
    global _ser
    if _ser is None: _ser = URLSafeTimedSerializer(secret_key(), salt="ergovest-csrf")
    return _ser


def make_csrf(sid_hash):
    return _signer().dumps(sid_hash or "anon")


def check_csrf(token, sid_hash):
    try: return _signer().loads(token or "", max_age=86400) == (sid_hash or "anon")
    except BadSignature: return False


def clean_text(s, maxlen):
    return re.sub(r"[\x00-\x1f\x7f]", "", (s or "")).strip()[:maxlen]


def validate_password(pw, username="", email=""):
    errs = []
    if len(pw or "") < 10: errs.append("Use at least 10 characters.")
    if len(pw or "") > 128: errs.append("Use at most 128 characters.")
    if pw and not re.search(r"[A-Za-z]", pw): errs.append("Include at least one letter.")
    if pw and not re.search(r"\d", pw): errs.append("Include at least one number.")
    low = (pw or "").lower()
    if low in COMMON: errs.append("That password is too common.")
    if username and len(username) >= 3 and username.lower() in low: errs.append("Do not include your username.")
    if email and email.split("@")[0].lower() in low and len(email.split("@")[0]) >= 4: errs.append("Do not include your email name.")
    return errs


def validate_identity(username, email, full_name):
    errs = {}
    if not (1 <= len(full_name) <= 80): errs["full_name"] = "Enter your name (up to 80 characters)."
    if not USERNAME_RE.match(username or ""): errs["username"] = "3-32 characters: letters, numbers, dot, dash or underscore."
    if not EMAIL_RE.match(email or ""): errs["email"] = "Enter a valid email address."
    return errs


def hash_password(pw): return generate_password_hash(pw)          # scrypt, salted
def verify_password(pw, h): return check_password_hash(h, pw)
def verify_dummy(pw): check_password_hash(_DUMMY, pw)               # equalise timing for unknown users


def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()
def new_token(): return secrets.token_urlsafe(32)


class Throttle:
    """In-memory failure limiter: `limit` hits per `window` seconds per key."""
    def __init__(self, limit, window):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def _prune(self, key):
        q, now = self.hits[key], time.time()
        while q and now - q[0] > self.window: q.popleft()
        return q

    def blocked(self, key):
        q = self._prune(key)
        return int(self.window - (time.time() - q[0])) + 1 if len(q) >= self.limit else 0

    def hit(self, key): self._prune(key).append(time.time())
    def clear(self, key): self.hits.pop(key, None)
