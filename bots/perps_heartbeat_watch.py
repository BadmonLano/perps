"""perps_heartbeat_watch.py: high-priority ping when the perps bot is dead
while a position is open.

The perps bot cannot report its own death, so this runs as a separate process.
Every CHECK_S seconds it reads two files the bot already writes:
  bot_logs/perps_bot_heartbeat.json   (refreshed every 60s while alive)
  bot_logs/perp_positions.json        ({market: position} for open positions)
If any position is open and the heartbeat is older than STALE_MIN minutes, it
sends a Telegram alert (repeats every REPEAT_MIN while still dead) and writes
an alert file to bot_logs/. Read-only otherwise: it never opens, closes, or
restarts anything.

Env: PERPS_WATCH_TELEGRAM_TOKEN (falls back to TELEGRAM_TOKEN), TELEGRAM_CHAT_ID.
Run:  python perps_heartbeat_watch.py          (loop)
      python perps_heartbeat_watch.py --once   (single check, prints result)
"""
import json, os, sys, time

def _envv(key, default=None):
    """os.getenv, but a blank value (KEY= in .env) counts as unset and returns the default."""
    v = os.environ.get(key)
    return default if v is None or not str(v).strip() else v

from datetime import datetime, timezone
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("WORKSPACE_DIR") or os.path.dirname(HERE)
LOGS = os.path.join(ROOT, "bot_logs")
HB = os.path.join(LOGS, "perps_bot_heartbeat.json")
POS = os.path.join(LOGS, "perp_positions.json")
ALERT_FILE = os.path.join(LOGS, "perps_heartbeat_dead_alert.json")
STALE_MIN = float(_envv("PERPS_WATCH_STALE_MIN", "5"))
REPEAT_MIN = float(_envv("PERPS_WATCH_REPEAT_MIN", "30"))
CHECK_S = float(_envv("PERPS_WATCH_CHECK_S", "60"))


def _load(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def heartbeat_age_min():
    hb = _load(HB) or {}
    ts = hb.get("timestamp")
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        try:
            return (time.time() - os.path.getmtime(HB)) / 60.0
        except Exception:
            return None


def open_positions():
    store = _load(POS) or {}
    if not isinstance(store, dict):
        return {}
    return {m: p for m, p in store.items() if isinstance(p, dict) and p.get("direction")}


def send(text):
    tok = os.getenv("PERPS_WATCH_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        print("[watch] no telegram creds in env; alert file only")
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                       "parse_mode": "HTML", "disable_notification": "false"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=15).read()
        return True
    except Exception as e:
        print(f"[watch] telegram send failed: {type(e).__name__}")
        return False


def check(last_sent):
    pos = open_positions()
    age = heartbeat_age_min()
    dead = pos and (age is None or age > STALE_MIN)
    if not dead:
        return {"dead": False, "age_min": age, "open": list(pos)}, last_sent
    lines = [f"{m} {p.get('direction','?').upper()} ${p.get('size_usd','?')} @ {p.get('leverage','?')}x "
             f"entry {p.get('entry_price','?')} liq {p.get('liq_price','?')}" for m, p in pos.items()]
    age_s = "unknown" if age is None else f"{age:.0f} min"
    msg = ("\U0001F6A8 <b>PERPS BOT DOWN WITH A POSITION OPEN</b>\n"
           f"Heartbeat age: {age_s}. Nothing is watching TP, trail, floor or liquidation.\n"
           + "\n".join(lines) + "\nCheck the bot or the position on Jupiter.")
    res = {"dead": True, "age_min": age, "open": list(pos), "at": datetime.now().isoformat()}
    if time.time() - last_sent >= REPEAT_MIN * 60:
        res["sent"] = send(msg)
        res["inbox"] = _inbox_note(os.path.join(ROOT, "inbox"), "engine_heartbeat_stale",
                                   "Perps engine heartbeat stale with positions open",
                                   msg.replace("<b>", "").replace("</b>", ""))
        last_sent = time.time()
        try:
            with open(ALERT_FILE, "w", encoding="utf-8") as f:
                json.dump({**res, "message": msg}, f, indent=2)
        except Exception:
            pass
    return res, last_sent


def _inbox_note(inbox, slug, title, body):
    """Agent inbox note so the agent alerts too (naming: botname - subject_ts)."""
    try:
        os.makedirs(inbox, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = os.path.join(inbox, f"perps_heartbeat_watch - {slug}_{ts}.md")
        with open(fp, "w", encoding="utf-8") as f:
            f.write("---\nkind: %s\nseverity: high\ndedupe_key: perps_watch:%s\nsource_skill: perps_bot\n"
                    "summary: %s\n---\n\n# %s\n\n%s\n\nAlert the owner. Do not restart anything yourself.\n"
                    % (slug, slug, title, title, body))
        return fp
    except Exception as e:
        print(f"[watch] inbox note err {e!r}")
        return None


def relaunch_check(argv):
    """2026-10-16: spawned DETACHED by perps.py right before a live-update relaunch.
    The sub-bots (warden included) are stopped during the relaunch, so this is the
    only thing watching. After --wait seconds the new process must have booted
    (reload marker removed) and written a fresh heartbeat; otherwise Telegram +
    inbox note. Read-only: never restarts anything."""
    def arg(k, d=None):
        return argv[argv.index(k) + 1] if k in argv and argv.index(k) + 1 < len(argv) else d
    marker = arg("--marker", os.path.join(LOGS, "perps_bot_reload.json"))
    hb = arg("--hb", HB)
    inbox = arg("--inbox", os.path.join(ROOT, "inbox"))
    wait = float(arg("--wait", "180"))
    new_hash, old_hash = arg("--hash", "?"), arg("--from-hash", "?")
    t0 = time.time()
    time.sleep(wait)
    booted = not os.path.exists(marker)
    try:
        hb_fresh = os.path.getmtime(hb) > t0
    except Exception:
        hb_fresh = False
    res = {"at": datetime.now().isoformat(), "new_hash": new_hash, "from_hash": old_hash,
           "booted": booted, "heartbeat_fresh": hb_fresh, "wait_s": wait}
    if booted and hb_fresh:
        res["ok"] = True
    else:
        why = ("the new process never finished booting (reload marker still there)" if not booted
               else "the new process booted but its heartbeat is stale")
        pos = open_positions()
        lines = [f"{m} {p.get('direction','?').upper()} entry {p.get('entry_price','?')} liq {p.get('liq_price','?')}"
                 for m, p in pos.items()]
        msg = ("\U0001F6A8 <b>PERPS RELAUNCH FAILED</b>\n"
               f"Live-update {old_hash} -> {new_hash}: {why} after {wait:.0f}s.\n"
               + ("Open positions NOT monitored:\n" + "\n".join(lines) if lines else "No open positions.")
               + "\nStart the bot (Perps window) or check the console.")
        res.update(ok=False, reason=why, sent=(send(msg) if "--no-send" not in argv else "skipped (--no-send)"),
                   inbox=_inbox_note(inbox, "relaunch_failed", "Perps relaunch failed", msg.replace("<b>", "").replace("</b>", "")))
    try:
        with open(os.path.join(LOGS, "perps_relaunch_check.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
    except Exception:
        pass
    print(json.dumps(res))
    return res


if __name__ == "__main__":
    if "--relaunch-check" in sys.argv:
        relaunch_check(sys.argv)
        sys.exit(0)
    if "--once" in sys.argv:
        r, _ = check(0 if "--send" in sys.argv else time.time())
        print(json.dumps(r, default=str))
        sys.exit(0)
    last = 0.0
    while True:
        try:
            r, last = check(last)
            if r.get("dead"):
                print(f"[watch] DEAD heartbeat with open {r['open']}")
        except Exception as e:
            print(f"[watch] err {e!r}")
        time.sleep(CHECK_S)
