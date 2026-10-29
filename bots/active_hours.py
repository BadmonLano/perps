"""active_hours.py - the bot is a LOOP meant to run continuously (v1.0.6).

It runs 24/7 on its cadence. ACTIVE_HOURS only decides when it may take NEW
exposure; it never stops the process, never cancels or interrupts an order
already being sent, and never pauses exits, stops, SL/TP or monitoring.
Outside the window the loop idles (new entries are declined with reason
outside_active_hours) and resumes on its own when the window opens again.

bots/.env (all tunable, defaults keep the old always-on behaviour):
  ACTIVE_HOURS   00:00-23:59  window, HH:MM-HH:MM. Wraps midnight (22:00-02:00 ok).
                              Several windows: 09:00-12:00,18:00-23:00.
                              Blank / always / 24/7 / 00:00-23:59 = always on.
  ACTIVE_TZ      America/New_York  IANA timezone for the window.
  ACTIVE_DAYS    blank = every day; e.g. mon-fri or mon,wed,sat.
  LANE_CADENCE_<LANE>_MIN     how often your agent runs each lane (automations/).

CLI (no secrets printed):  python bots/active_hours.py   -> status + cadences
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

ALWAYS = ("", "always", "24/7", "24x7", "all", "00:00-23:59", "00:00-24:00", "0:00-23:59")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_TZ = "America/New_York"
# lane -> default minutes (0 = once a day at LANE_<LANE>_AT)
LANE_AT_DEFAULTS = {"morning_brief": "08:00", "nightly_eval": "23:30"}
LANE_DEFAULTS = {"perps_lane": 10, "misc_lane": 15, "warden_lane": 20, "morning_brief": 0, "nightly_eval": 0}


def _env(env, k, d=""):
    return str((env if env is not None else os.environ).get(k, d) or d).strip()


def _mins(s: str) -> int:
    h, m = [int(x) for x in s.strip().split(":")]
    if not (0 <= h <= 24 and 0 <= m <= 59):
        raise ValueError(s)
    return min(h * 60 + m, 1440)


def _days(spec: str) -> set:
    out = set()
    for part in spec.lower().replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            i, j = DAYS.index(a[:3]), DAYS.index(b[:3])
            k = i
            while True:
                out.add(k)
                if k == j:
                    break
                k = (k + 1) % 7
        else:
            out.add(DAYS.index(part[:3]))
    return out


def is_always(env=None) -> bool:
    return _env(env, "ACTIVE_HOURS").lower() in ALWAYS and not _env(env, "ACTIVE_DAYS")


def in_active_window(now: datetime | None = None, env=None) -> tuple:
    """(ok, why). Never raises: an unreadable setting is ignored (stays on) and says so."""
    spec = _env(env, "ACTIVE_HOURS")
    tzn = _env(env, "ACTIVE_TZ") or DEFAULT_TZ
    days = _env(env, "ACTIVE_DAYS")
    if spec.lower() in ALWAYS and not days:
        return True, "always on"
    try:
        tz = ZoneInfo(tzn) if ZoneInfo else None
        n = now or datetime.now(tz)
        if tz and n.tzinfo is not None:
            n = n.astimezone(tz)
        if days and n.weekday() not in _days(days):
            return False, f"ACTIVE_DAYS {days} {tzn}"
        if spec.lower() in ALWAYS:
            return True, f"ACTIVE_DAYS {days} {tzn}"
        cur = n.hour * 60 + n.minute
        for w in spec.split(","):
            a, b = [s.strip() for s in w.split("-", 1)]
            start, end = _mins(a), _mins(b)
            if end == 23 * 60 + 59:
                end = 1440   # 23:59 means through the end of the day
            if (start <= cur < end) if start <= end else (cur >= start or cur < end):
                return True, f"ACTIVE_HOURS {spec} {tzn}"
        return False, f"ACTIVE_HOURS {spec} {tzn}"
    except Exception:
        return True, f"ACTIVE_HOURS '{spec}' / ACTIVE_DAYS '{days}' unreadable, ignored (always on)"


def lane_cadences(env=None) -> dict:
    out = {}
    for lane, d in LANE_DEFAULTS.items():
        try:
            out[lane] = float(_env(env, f"LANE_CADENCE_{lane.upper()}_MIN", str(d)))
        except ValueError:
            out[lane] = float(d)
    return out


def status(env=None) -> str:
    ok, why = in_active_window(env=env)
    lines = [f"loop: runs continuously; new entries {'ALLOWED' if ok else 'PAUSED (idle until the window opens)'} - {why}",
             "exits, stops, SL/TP and monitoring: always on"]
    for lane, m in lane_cadences(env).items():
        lines.append(f"  {lane}: " + (f"every {m:g} min" if m > 0 else
                                       f"once a day at {_env(env, 'LANE_' + lane.upper() + '_AT', LANE_AT_DEFAULTS.get(lane, '08:00'))}"))
    return "\n".join(lines)


def _load_dotenv() -> dict:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    env = dict(os.environ)
    try:
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip().startswith(("ACTIVE_", "LANE_")):   # only these keys are read; no secrets
                    env.setdefault(k.strip(), v.split(" #")[0].strip())
    except OSError:
        pass
    return env


if __name__ == "__main__":
    print(status(_load_dotenv()))
    sys.exit(0)
