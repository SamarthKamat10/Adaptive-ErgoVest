"""Settings from environment / .env (never hard-coded secrets)."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    if not os.path.exists(path): return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(HERE, ".env"))


def _bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


HOST = os.environ.get("ERGOVEST_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("ERGOVEST_HTTP_PORT", "8000"))
ALLOW_REGISTRATION = _bool("ERGOVEST_ALLOW_REGISTRATION", True)
COOKIE_SECURE = _bool("ERGOVEST_COOKIE_SECURE", False)      # set true only when served over HTTPS
SESSION_IDLE_HOURS = float(os.environ.get("ERGOVEST_SESSION_IDLE_HOURS", "12"))
REMEMBER_DAYS = float(os.environ.get("ERGOVEST_REMEMBER_DAYS", "30"))
DB_PATH = os.environ.get("ERGOVEST_DB", os.path.join(HERE, "instance", "ergovest.db"))
SECRET_FILE = os.path.join(HERE, "instance", "secret.key")
ALLOW_ANY_PORT = _bool("ERGOVEST_ALLOW_ANY_PORT", False)   # tests only (loop://, pty paths)
