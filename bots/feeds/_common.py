"""Shared helpers for bots/feeds: repo paths, HTTP with 429-only backoff, atomic JSON."""
import json, os, tempfile, time, urllib.error, urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
BOTS = os.path.dirname(HERE)


def _env_file(key):
    try:
        with open(os.path.join(BOTS, ".env"), encoding="utf-8") as f:
            for ln in f:
                if ln.strip().startswith(key + "="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def env(key, default=""):
    v = os.environ.get(key)
    if v is None or v == "":
        v = _env_file(key)
    return v if v not in (None, "") else default


def root():
    return env("WORKSPACE_DIR") or os.path.dirname(BOTS)


def bot_logs(*p):
    d = os.path.join(root(), "bot_logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, *p)


def now_iso():
    return datetime.now(ET).isoformat()


def parse_ts(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=ET)
    except Exception:
        return None


def age_min(ts):
    t = parse_ts(ts)
    return None if t is None else round((datetime.now(timezone.utc) - t).total_seconds() / 60.0, 1)


class HttpError(Exception):
    pass


def http_get(url, *, params=None, timeout=15.0, headers=None, as_json=True, max_429=3):
    """GET with backoff on HTTP 429 only (Retry-After honored, else 2s, 4s, 8s). Other errors raise."""
    if params:
        from urllib.parse import urlencode
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    h = {"User-Agent": "perps-feeds/1.0", "Accept": "application/json" if as_json else "*/*"}
    h.update(headers or {})
    for attempt in range(max_429 + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                body = r.read()
            return json.loads(body) if as_json else body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra)
                except (TypeError, ValueError):
                    wait = 2.0 * (2 ** attempt)
                time.sleep(min(wait, 30.0))
                continue
            raise HttpError(f"HTTP {e.code} {url.split('?')[0]}")
        except Exception as e:
            raise HttpError(f"{type(e).__name__}: {str(e)[:120]}")
    raise HttpError("429 backoff exhausted")


def write_json(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default
