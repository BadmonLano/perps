"""macro_fetcher.py - released US macro prints (CPI) for the perps packet. Signal only.

What it gives the agent: the actual numbers behind a macro headline, so a loud headline
can be checked against the print (headline vs core, energy-driven vs broad).

Sources, in order:
  1. Flow's FRED fields, when present (core_cpi, cpi_chg_3m, cpi_accelerating,
     fed_funds_rate, unemployment, oil, oil_chg). Read from the Flow cache
     (feed_hook.read_feed("macro")) or bot_logs/macro.json. No network.
  2. BLS public API (v1, no key; BLS_API_KEY in bots/.env switches to v2) for the CPI
     components Flow does not carry. One POST per fetch, all series at once:
       CUUR0000SA0 / CUUR0000SA0L1E / CUUR0000SETB01  (not seasonally adjusted) -> y/y
       CUSR0000SA0 / CUSR0000SA0L1E / CUSR0000SETB01  (seasonally adjusted)     -> m/m
     Headline = all items, core = less food and energy, gasoline = all types.

  3. FRED mirrors of the same series (CPIAUCNS/CPILFENS/CUUR0000SETB01, CPIAUCSL/CPILFESL/
     CUSR0000SETB01) when BLS refuses (e.g. the keyless per-IP daily limit) and FRED_API_KEY is set.

Budget: BLS v1 allows 25 queries/day. This fetches at most once per day, plus a re-check
every MACRO_RELEASE_RECHECK_MIN (default 20) on weekday release mornings (08:31-11:00 ET)
while the previous month's print is not in yet, hard-capped at MACRO_BLS_DAILY_MAX
(default 12) calls per ET day.

Cache: bot_logs/macro_prints.json. Any failure serves the last cache (flagged stale) and
never raises into the caller. Optional one-line `read` is written by the caller's SI brain
(see read_prompt / parse_read / set_read), cached per print.

CLI: python macro_fetcher.py            (fetch if due, print the numbers)
     python macro_fetcher.py --force    (fetch now, still inside the daily cap)
"""
import json, os, re, sys, tempfile, urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-4))

HERE = os.path.dirname(os.path.abspath(__file__))
BOTS = os.path.dirname(HERE) if os.path.basename(HERE) == "feeds" else HERE
if BOTS not in sys.path:
    sys.path.insert(0, BOTS)

try:  # repo layout: bots/feeds/_common.py
    from feeds._common import env, bot_logs  # noqa: E402
except Exception:  # standalone layout: this file sits next to the bot, logs in ./bot_logs
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

    def bot_logs(*p):
        d = os.path.join(BOTS, "bot_logs")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, *p)

CACHE_NAME = "macro_prints.json"
FLOW_KEYS = ("core_cpi", "cpi_chg_3m", "cpi_accelerating", "fed_funds_rate", "unemployment", "oil", "oil_chg")
SERIES = {
    "CUUR0000SA0": ("headline", "nsa"), "CUUR0000SA0L1E": ("core", "nsa"), "CUUR0000SETB01": ("gasoline", "nsa"),
    "CUSR0000SA0": ("headline", "sa"), "CUSR0000SA0L1E": ("core", "sa"), "CUSR0000SETB01": ("gasoline", "sa"),
}
DOCTRINE = ("Released macro numbers (BLS CPI + Flow's FRED fields). Use them to check a macro headline "
            "against the print: headline vs core, energy-driven vs broad. Context for your call, never a "
            "trade trigger or an auto-gate. y/y from not-seasonally-adjusted series, m/m from seasonally "
            "adjusted, as BLS reports them.")


def _now():
    return datetime.now(ET)


def _iso(t=None):
    return (t or _now()).isoformat()


def _parse(ts):
    try:
        t = datetime.fromisoformat(str(ts))
        return t if t.tzinfo else t.replace(tzinfo=ET)
    except Exception:
        return None


def _age_min(ts):
    t = _parse(ts)
    return None if t is None else round((_now() - t).total_seconds() / 60.0, 1)


def _f(v):
    try:
        return None if v in (None, "", "-") else float(v)
    except (TypeError, ValueError):
        return None


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    os.replace(tmp, path)


def cache_path():
    return bot_logs(CACHE_NAME)


# ── Flow (FRED) fields: no network ────────────────────────────────────────────
def _flow_macro():
    """(fields, age_min, source) from the Flow cache or bot_logs/macro.json."""
    try:
        import feed_hook  # repo layout
        m = feed_hook.read_feed("macro") or {}
        if m:
            age = (feed_hook.cached() or {}).get("age_min")
            return {k: m.get(k) for k in FLOW_KEYS if m.get(k) is not None}, age, "flow_cache"
    except Exception:
        pass
    p = bot_logs("macro.json")
    m = _read(p, {}) or {}
    if m:
        try:
            age = round((_now().timestamp() - os.path.getmtime(p)) / 60.0, 1)
        except Exception:
            age = None
        return {k: m.get(k) for k in FLOW_KEYS if m.get(k) is not None}, age, "flow_macro_json"
    return {}, None, None


# ── BLS ───────────────────────────────────────────────────────────────────────
def _bls_post(year):
    key = env("BLS_API_KEY", "")
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/" if key else "https://api.bls.gov/publicAPI/v1/timeseries/data/"
    body = {"seriesid": list(SERIES), "startyear": str(year - 1), "endyear": str(year)}
    if key:
        body["registrationkey"] = key
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "perps-macro/1.0"})
    with urllib.request.urlopen(req, timeout=float(env("MACRO_BLS_TIMEOUT_S", "20"))) as r:
        d = json.loads(r.read())
    if d.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS {d.get('status')}: {'; '.join(d.get('message') or [])[:160]}")
    return d


def _monthly(rows):
    """{(year, month): value} for M01..M12 rows."""
    out = {}
    for r in rows or []:
        p = str(r.get("period") or "")
        v = _f(r.get("value"))
        if p.startswith("M") and p != "M13" and v is not None:
            try:
                out[(int(r["year"]), int(p[1:]))] = v
            except Exception:
                continue
    return out


def _pct(a, b):
    return None if a is None or not b else round((a / b - 1.0) * 100.0, 2)


def _bls_data(d):
    data = {}
    for s in (d.get("Results") or {}).get("series") or []:
        sid = s.get("seriesID")
        if sid in SERIES:
            data[SERIES[sid]] = _monthly(s.get("data"))
    return data


# FRED mirrors of the same BLS series (used only when BLS refuses and FRED_API_KEY is set).
FRED_SERIES = {
    "CPIAUCNS": ("headline", "nsa"), "CPILFENS": ("core", "nsa"), "CUUR0000SETB01": ("gasoline", "nsa"),
    "CPIAUCSL": ("headline", "sa"), "CPILFESL": ("core", "sa"), "CUSR0000SETB01": ("gasoline", "sa"),
}


def _fred_data():
    key = env("FRED_API_KEY", "")
    if not key:
        raise RuntimeError("no FRED_API_KEY for the FRED fallback")
    from urllib.parse import urlencode
    data = {}
    for sid, slot in FRED_SERIES.items():
        q = urlencode({"series_id": sid, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 15})
        req = urllib.request.Request("https://api.stlouisfed.org/fred/series/observations?" + q,
                                     headers={"User-Agent": "perps-macro/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=float(env("MACRO_BLS_TIMEOUT_S", "20"))) as r:
                obs = json.loads(r.read()).get("observations") or []
        except Exception as e:  # never echo the request URL (it carries the key)
            raise RuntimeError(f"FRED {sid}: {type(e).__name__}") from None
        rows = {}
        for o in obs:
            v = _f(o.get("value"))
            try:
                y, m = int(o["date"][:4]), int(o["date"][5:7])
            except Exception:
                continue
            if v is not None:
                rows[(y, m)] = v
        data[slot] = rows
    return data


def _compute(data):
    nsa_head = data.get(("headline", "nsa")) or {}
    if not nsa_head:
        raise RuntimeError("BLS returned no headline CPI rows")
    y, m = max(nsa_head)
    prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
    cpi = {}
    for comp in ("headline", "core", "gasoline"):
        nsa = data.get((comp, "nsa")) or {}
        sa = data.get((comp, "sa")) or {}
        cpi[f"{comp}_yoy_pct"] = _pct(nsa.get((y, m)), nsa.get((y - 1, m)))
        cpi[f"{comp}_mom_pct"] = _pct(sa.get((y, m)), sa.get(prev_m))
        cpi[f"{comp}_index_sa"] = sa.get((y, m))
    label = datetime(y, m, 1).strftime("%B %Y")
    return f"{y}-{m:02d}", label, cpi


# ── schedule ──────────────────────────────────────────────────────────────────
def _expected_period(now):
    """The print that should exist after this month's release: last month."""
    y, m = now.year, now.month - 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y}-{m:02d}"


def _due(cache, now, force=False):
    """(due, reason). Respects the daily cap always."""
    today = now.date().isoformat()
    calls = cache.get("_bls_calls") or {}
    used = calls.get("count", 0) if calls.get("date") == today else 0
    cap = int(env("MACRO_BLS_DAILY_MAX", "12"))
    if used >= cap:
        return False, f"daily cap {cap} reached"
    if force:
        return True, "forced"
    last_ok = _parse(cache.get("bls_fetched_at"))
    last_try = _parse(cache.get("_last_attempt_at"))
    since_try = (now - last_try).total_seconds() / 60.0 if last_try else 1e9
    if not cache.get("period"):
        return (since_try >= 30, "no print cached" if since_try >= 30 else "retry backoff")
    if last_ok is None or last_ok.astimezone(ET).date() != now.date():
        if since_try >= 30:
            return True, "daily refresh"
    release_window = (now.weekday() < 5 and (now.hour, now.minute) >= (8, 31) and now.hour < 11)
    if release_window and cache.get("period", "") < _expected_period(now):
        if since_try >= float(env("MACRO_RELEASE_RECHECK_MIN", "20")):
            return True, "release-morning recheck"
    return False, "cached"


# ── public API ────────────────────────────────────────────────────────────────
def _public(c):
    out = {k: v for k, v in (c or {}).items() if not k.startswith("_")}
    out["age_min"] = _age_min(out.get("bls_fetched_at"))
    first = _parse(out.get("print_first_seen_at"))
    out["new_print_today"] = bool(first and first.astimezone(ET).date() == _now().date())
    out["stale"] = (out.get("age_min") is None) or (out["age_min"] > 60 * 24 * 3)
    out["_doctrine"] = DOCTRINE
    return out


def cached_prints():
    """Cache only, no network. Never raises."""
    try:
        c = _read(cache_path(), {}) or {}
        if not c:
            return {"available": False, "reason": "no macro_prints cache yet", "_doctrine": DOCTRINE}
        return _public(c)
    except Exception as e:
        return {"available": False, "reason": f"cache read error: {e}"[:160], "_doctrine": DOCTRINE}


def get_macro_prints(force=False):
    """Packet block. Flow fields every call, BLS when due. Never raises."""
    try:
        path = cache_path()
        c = _read(path, {}) or {}
        now = _now()
        errors = []
        flow, flow_age, flow_src = _flow_macro()
        due, why = _due(c, now, force)
        if due:
            today = now.date().isoformat()
            calls = c.get("_bls_calls") or {}
            calls = calls if calls.get("date") == today else {"date": today, "count": 0}
            calls["count"] = int(calls.get("count", 0)) + 1
            c["_bls_calls"] = calls
            c["_last_attempt_at"] = _iso(now)
            try:
                try:
                    period, label, cpi = _compute(_bls_data(_bls_post(now.year)))
                    via = "bls"
                    c.pop("fred_note", None)
                except Exception as be:
                    try:
                        period, label, cpi = _compute(_fred_data())
                        via = "fred"
                        c["fred_note"] = f"BLS refused ({type(be).__name__}: {str(be)[:120]}); served from FRED"
                    except Exception as fe:
                        raise RuntimeError(f"BLS: {str(be)[:140]} | FRED: {str(fe)[:80]}") from None
                if period != c.get("period"):
                    # first sight of this print; on a fresh cache the release day is unknown
                    c["print_first_seen_at"] = _iso(now) if c.get("period") else None
                    if c.get("read_period") != period:
                        c["read"], c["read_by"], c["read_period"] = None, None, None
                        c.pop("_read_attempt_at", None)
                c.update({"period": period, "period_label": label, "cpi": cpi, "bls_fetched_at": _iso(now),
                          "cpi_via": via, "bls_series": list(SERIES) if via == "bls" else list(FRED_SERIES)})
                c.pop("bls_error", None)
            except Exception as e:
                c["bls_error"] = f"{type(e).__name__}: {e}"[:200]
        if c.get("bls_error"):
            errors.append(c["bls_error"])
        c["flow"] = flow or None
        c["flow_source"] = flow_src
        c["flow_age_min"] = flow_age
        has_bls = bool(c.get("cpi"))
        via = c.get("cpi_via") or "bls"
        c["source"] = (f"flow+{via}" if flow and has_bls else via if has_bls else "flow" if flow else "none")
        c["available"] = bool(flow or has_bls)
        c["last_check_reason"] = why
        c["errors"] = errors or None
        c["_updated"] = _iso(now)
        try:
            _write(path, c)
        except Exception as e:
            errors.append(f"cache write: {e}"[:120])
        return _public(c)
    except Exception as e:
        out = cached_prints()
        out.setdefault("errors", [])
        out["errors"] = (out.get("errors") or []) + [f"macro_fetcher: {type(e).__name__}: {e}"[:160]]
        return out


# ── optional SI-brain one-liner (caller supplies the model call) ──────────────
def needs_read(mp, retry_min=60):
    """True when there is a print without a cached read and no recent failed attempt."""
    try:
        if not (mp and mp.get("cpi") and mp.get("period")):
            return False
        if mp.get("read") and mp.get("read_period") == mp.get("period"):
            return False
        c = _read(cache_path(), {}) or {}
        t = _parse(c.get("_read_attempt_at"))
        return not (t and (_now() - t).total_seconds() < retry_min * 60)
    except Exception:
        return False


def read_prompt(mp):
    c = mp.get("cpi") or {}
    nums = {k: c.get(k) for k in ("headline_yoy_pct", "core_yoy_pct", "headline_mom_pct", "core_mom_pct",
                                  "gasoline_mom_pct", "gasoline_yoy_pct")}
    flow = {k: (mp.get("flow") or {}).get(k) for k in ("cpi_chg_3m", "cpi_accelerating", "fed_funds_rate",
                                                        "unemployment", "oil_chg")}
    return ("You read a US CPI release from these numbers ONLY. Do not use outside knowledge or guess "
            "consensus. In one line (max 16 words), say whether the print is broad or driven by one "
            "component, comparing headline to core and gasoline. No trade advice.\n"
            f"PERIOD: {mp.get('period_label')}\nCPI: {json.dumps(nums)}\nCONTEXT: {json.dumps(flow)}\n"
            'Reply ONLY as JSON: {"read": "..."}')


def parse_read(raw):
    if not raw:
        return None
    t = str(raw).strip()
    try:
        t = json.loads(t[t.index("{"): t.rindex("}") + 1]).get("read") or ""
    except Exception:
        t = re.sub(r"\s+", " ", t)
    t = str(t).strip().strip('"')
    return t[:200] or None


def mark_read_attempt():
    try:
        c = _read(cache_path(), {}) or {}
        c["_read_attempt_at"] = _iso()
        _write(cache_path(), c)
    except Exception:
        pass


def set_read(text, by="si_brain"):
    """Cache the one-liner against the current print. Returns the public block."""
    try:
        c = _read(cache_path(), {}) or {}
        if text and c.get("period"):
            c.update({"read": str(text)[:200], "read_by": by, "read_period": c.get("period"),
                      "read_at": _iso()})
            c["_read_attempt_at"] = _iso()
            _write(cache_path(), c)
        return _public(c)
    except Exception:
        return cached_prints()


if __name__ == "__main__":
    mp = get_macro_prints(force="--force" in sys.argv)
    c = mp.get("cpi") or {}
    print(f"macro_prints: source={mp.get('source')} period={mp.get('period_label')} "
          f"age_min={mp.get('age_min')} new_print_today={mp.get('new_print_today')} "
          f"check={mp.get('last_check_reason')} errors={mp.get('errors')}")
    if c:
        def _s(v, sign=False):
            return "n/a" if v is None else (f"{v:+}%" if sign else f"{v}%")
        print(f"  headline {_s(c.get('headline_yoy_pct'))} y/y ({_s(c.get('headline_mom_pct'), True)} m/m) | "
              f"core {_s(c.get('core_yoy_pct'))} y/y ({_s(c.get('core_mom_pct'), True)} m/m) | "
              f"gasoline {_s(c.get('gasoline_mom_pct'), True)} m/m ({_s(c.get('gasoline_yoy_pct'))} y/y)")
    print(f"  flow: {mp.get('flow')} (src {mp.get('flow_source')}, age_min {mp.get('flow_age_min')})")
    print(f"  read: {mp.get('read')}")
    print(f"  cache: {cache_path()}")
