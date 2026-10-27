"""owner_commands.py: on-demand owner commands (optional, off by default).

The owner asks, the agent answers:
  /roast                    honest roast of the last 24h (Overseer prompt and judges)
  /recap                    plain recap of the last 24h (same judges, kinder framing)
  /autopsy [market|last]    the latest (or named) closed perp trade, from perp_episodes.jsonl
  /manage <ticker>          let the bot manage an owner position it found (a perps
                            position it did not open); /leave <ticker> keeps it the owner's
  plain text                "roast me", "recap please", "autopsy sol" ... (case-insensitive)

Sources (OWNER_COMMANDS in .env):
  off       default. This process exits right away and the launcher does not start it.
  telegram  a SEPARATE command bot (OWNER_COMMANDS_TG_TOKEN). It reads only the owner's chat
            (OWNER_COMMANDS_TG_CHAT_ID, else TELEGRAM_CHAT_ID) and replies in that chat.
            It refuses to run on TELEGRAM_TOKEN or WARDEN_TELEGRAM_TOKEN, refuses when a
            webhook is set, and stops polling on a 409 conflict, so it never fights another
            getUpdates reader.
  file      drop a .txt or .json ({"text": "/roast"}) into <workspace>/owner_commands/.
            Works on any OS, no Telegram needed. Replies go to the configured alert channels.
Every reply is also saved under inbox_results/owner_replies/. Processed command files move to
inbox_results/owner_commands/done (or failed).

This process only reads trade logs and writes replies. It never trades, never changes sizes,
filters or schedules, and never writes the nightly Overseer packet.

  python owner_commands.py                 run (follows OWNER_COMMANDS)
  python owner_commands.py --check         show status (never prints a secret)
  python owner_commands.py --once "/roast" answer one command locally, then exit
"""
from __future__ import annotations

import asyncio
import glob
import importlib.util
import json
import os

def _envv(key, default=None):
    """os.getenv, but a blank value (KEY= in .env) counts as unset and returns the default."""
    v = os.environ.get(key)
    return default if v is None or not str(v).strip() else v

import re
import shutil
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MODES = ("off", "telegram", "file")
HELP_TEXT = ("Commands: /roast, /recap, /autopsy [market|last], /manage <market>, /leave <market>. "
             "Plain text works too, e.g. \"roast me\" or \"autopsy $gork\".")


# ── env ──────────────────────────────────────────────────────────────────────
def _load_env() -> None:
    for d in (HERE, os.path.dirname(HERE)):
        p = os.path.join(d, ".env")
        if os.path.isfile(p):
            try:
                from dotenv import load_dotenv
                load_dotenv(p, override=False)
            except Exception:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    for ln in f:
                        if "=" in ln and not ln.lstrip().startswith("#"):
                            k, v = ln.split("=", 1)
                            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def mode(env=None) -> str:
    env = os.environ if env is None else env
    m = str(env.get("OWNER_COMMANDS") or "off").strip().lower()
    return m if m in MODES else "off"


def tg_config(env=None) -> dict:
    """{ok, reason, token, chat_id}. ok=False explains why Telegram polling must not start."""
    env = os.environ if env is None else env
    tok = str(env.get("OWNER_COMMANDS_TG_TOKEN") or "").strip()
    chat = str(env.get("OWNER_COMMANDS_TG_CHAT_ID") or env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not tok:
        return {"ok": False, "reason": "OWNER_COMMANDS_TG_TOKEN is not set", "token": "", "chat_id": chat}
    others = {str(env.get(k) or "").strip() for k in ("TELEGRAM_TOKEN", "WARDEN_TELEGRAM_TOKEN", "PERPS_WATCH_TELEGRAM_TOKEN")} - {""}
    if tok in others:
        return {"ok": False, "reason": "OWNER_COMMANDS_TG_TOKEN must be a separate bot, not the alert bot "
                                       "(another reader may already poll it)", "token": "", "chat_id": chat}
    if not chat:
        return {"ok": False, "reason": "no owner chat id (OWNER_COMMANDS_TG_CHAT_ID or TELEGRAM_CHAT_ID)",
                "token": "", "chat_id": ""}
    return {"ok": True, "reason": "", "token": tok, "chat_id": chat}


# ── parsing ──────────────────────────────────────────────────────────────────
_TICK = re.compile(r"\$?([A-Za-z0-9]{2,44})")


def parse_command(text: str):
    """-> (kind, target) with kind in roast / recap / autopsy / help, or None for empty text."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    if low.startswith("/"):
        parts = low.split()
        cmd = parts[0][1:].split("@", 1)[0]          # "/Roast@MyBot" -> "roast"
        arg = t.split()[1].lstrip("$") if len(parts) > 1 else None
        if cmd in ("roast", "recap"):
            return (cmd, None)
        if cmd in ("autopsy", "postmortem"):
            return ("autopsy", (arg or "last"))
        if cmd in ("manage", "leave"):
            return (cmd, arg)
        return ("help", None)
    if "autopsy" in low or "post-mortem" in low or "postmortem" in low or "what happened" in low:
        m = re.search(r"\$([A-Za-z0-9]{2,44})", t)
        if not m:
            after = re.split(r"(?i)autopsy|post-?mortem|what happened( (to|with|on))?", t)[-1]
            m2 = _TICK.search(after or "")
            word = m2.group(1) if m2 else None
            target = word if word and word.lower() not in ("on", "of", "for", "the", "last", "latest", "please") else "last"
        else:
            target = m.group(1)
        return ("autopsy", target)
    if "roast" in low:
        return ("roast", None)
    if any(w in low for w in ("recap", "summary", "how was the day", "how'd we do", "how did we do")):
        return ("recap", None)
    return ("help", None)


# ── overseer link ────────────────────────────────────────────────────────────
_OVERSEER = None


def overseer():
    """Import the Overseer module that sits next to this file (*_autoagent.py)."""
    global _OVERSEER
    if _OVERSEER is None:
        cands = sorted(p for p in glob.glob(os.path.join(HERE, "*_autoagent.py")))
        if not cands:
            raise RuntimeError("no *_autoagent.py next to owner_commands.py")
        spec = importlib.util.spec_from_file_location("owner_overseer", cands[0])
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, HERE)
        spec.loader.exec_module(mod)
        for need in ("collect_inputs", "run_judgment", "notify", "load_json", "save_json_atomic", "LOGS", "INBOX_RESULTS"):
            if not hasattr(mod, need):
                raise RuntimeError(f"{os.path.basename(cands[0])} has no {need}")
        _OVERSEER = mod
    return _OVERSEER


def _paths():
    o = overseer()
    ws = getattr(o, "_WS", os.path.dirname(HERE))
    return {"ws": ws, "logs": o.LOGS, "results": o.INBOX_RESULTS,
            "cmd_dir": os.environ.get("OWNER_COMMANDS_DIR") or os.path.join(ws, "owner_commands")}


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [owner_commands] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(_paths()["logs"], "owner_commands.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _state_path() -> str:
    return os.path.join(_paths()["logs"], "owner_commands_state.json")


def _state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(**kw) -> None:
    st = {**_state(), **kw, "updated_at": datetime.now().isoformat(timespec="seconds")}
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, p)


# ── roast_now: on-demand roast / recap / autopsy ─────────────────────────────
# Reuses the nightly Overseer prompt, inputs and judges from the *_autoagent.py next to
# this file WITHOUT editing it. Read-only toward the nightly pipeline: no daily summary,
# no inbox packet, no history, no LEARNINGS entry, no schedule change, no trading.
def _cooldown_s() -> int:
    try:
        return int(_envv("OWNER_ROAST_COOLDOWN_S") or 900)
    except ValueError:
        return 900


def _roast_state_path() -> str:
    return os.path.join(overseer().LOGS, "owner_roast_last.json")


def _replies_dir() -> str:
    return os.path.join(overseer().INBOX_RESULTS, "owner_replies")


def _h(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cooldown_left(now_ts: float = None) -> int:
    st = overseer().load_json(_roast_state_path(), {}) or {}
    last = float(st.get("last_ts") or 0)
    return max(0, int(last + _cooldown_s() - (time.time() if now_ts is None else now_ts)))


def pick_autopsy(target: str = None) -> dict:
    """Latest (or named) closed perp trade from perp_episodes.jsonl (perps.py writes it).
    Target matches the market (SOL-PERP), its base (sol) or the episode fp_key."""
    o = overseer()
    rows = o.load_jsonl_tail(os.path.join(o.LOGS, "perp_episodes.jsonl"), max_records=500)
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return {}
    t = (target or "last").strip().lstrip("$").lower()
    if t in ("", "last", "latest"):
        return rows[-1]
    for r in reversed(rows):
        mk = str(r.get("market") or "").lower()
        if t in (mk, mk.split("-")[0], str(r.get("fp_key") or "").lower()):
            return r
    return {}


def format_autopsy(r: dict, target: str = None) -> str:
    if not r:
        return f"🔎 <b>Autopsy</b>\nNo closed trade found for {_h(target or 'last')}."
    lines = [f"🔎 <b>Autopsy: {_h(r.get('market') or '?')} {_h(r.get('direction') or '')}</b>"]
    for label, key in (("PnL %", "pnl_pct"), ("PnL USD", "pnl_usd"), ("Peak %", "peak_pct"),
                       ("Leverage", "leverage"), ("Notional USD", "size_usd"), ("Entry", "entry_price"),
                       ("Exit", "exit_price"), ("Reason", "reason"), ("Held min", "hold_minutes"),
                       ("Liquidated", "liquidated")):
        v = r.get(key)
        if v not in (None, ""):
            lines.append(f"{label}: {_h(round(v, 2) if isinstance(v, float) else v)}")
    return "\n".join(lines)


def format_verdict(kind: str, verdict: dict, judge_meta: dict) -> str:
    score = verdict.get("score_0_to_10")
    head = "🔥 <b>Roast</b>" if kind == "roast" else "📋 <b>Recap</b>"
    out = [f"{head} | Score: {score if score is not None else 'n/a'}/10",
           f"<i>{_h((verdict.get('headline') or '')[:220])}</i>"]
    fields = (("what_was_weak", "Weak"), ("rule_breaks", "Rule breaks"), ("operator_drift", "Drift")) \
        if kind == "roast" else (("what_was_strong", "Strong"), ("what_was_weak", "Weak"),
                                 ("missed_opportunities", "Missed"))
    for key, label in fields + (("improvements_for_tomorrow", "Next"),):
        items = [i for i in (verdict.get(key) or []) if isinstance(i, str) and i.strip()][:3]
        if items:
            out.append(f"\n<b>{label}</b>\n" + "\n".join(f"• {_h(i[:180])}" for i in items))
    out.append(f"\n<i>judge: {_h(judge_meta.get('judge'))}</i>")
    return "\n".join(out)


def _save_reply(kind: str, target, text: str, extra: dict = None) -> str:
    d = _replies_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{kind}.json")
    overseer().save_json_atomic(path, {"kind": kind, "target": target, "at": datetime.now().isoformat(timespec="seconds"),
                                       "text": text, **(extra or {})})
    return path


async def roast_now(kind: str = "roast", target: str = None, *, send: bool = True,
                    source: str = "owner_commands") -> dict:
    """On-demand roast / recap / autopsy -> {ok, kind, text, reply_path, ...}.
    roast + recap run the Overseer judges on the last 24h (cooldown OWNER_ROAST_COOLDOWN_S).
    autopsy reads the latest (or named) closed trade from autopsy_reports.json, no SI call.
    send=True also pushes the text through the Overseer's notify() (the configured alert channels)."""
    o = overseer()
    kind = (kind or "roast").strip().lower()
    if kind not in ("roast", "recap", "autopsy"):
        return {"ok": False, "kind": kind, "reason": "unknown_kind", "text": ""}
    if kind == "autopsy":
        res = {"ok": True, "kind": kind, "text": format_autopsy(pick_autopsy(target), target)}
    else:
        left = cooldown_left()
        if left > 0:
            res = {"ok": False, "kind": kind, "reason": "cooldown", "retry_in_s": left,
                   "text": f"⏳ {kind.title()} is cooling down. Try again in {max(1, -(-left // 60))} min."}
        else:
            o.save_json_atomic(_roast_state_path(), {"last_ts": time.time(), "kind": kind,
                                                     "at": datetime.now().isoformat(timespec="seconds")})
            _log(f"{kind} requested (source={source})")
            inputs = o.collect_inputs()
            judgment = await o.run_judgment(inputs, {})
            res = {"ok": True, "kind": kind, "judge": judgment["meta"].get("judge"),
                   "text": format_verdict(kind, judgment["verdict"], judgment["meta"])}
    res["reply_path"] = _save_reply(kind, target, res["text"], {k: v for k, v in res.items() if k != "text"})
    if send and res["text"]:
        await o.notify(res["text"])
    return res


async def answer(text: str, *, source: str, send_alerts: bool) -> dict:
    """Parse and answer one owner message. Returns the roast_now result (or a help reply)."""
    parsed = parse_command(text)
    if parsed is None:
        return {"ok": False, "kind": None, "text": ""}
    kind, target = parsed
    if kind == "help":
        return {"ok": True, "kind": "help", "text": HELP_TEXT}
    if kind in ("manage", "leave"):
        return owner_position_decision(kind, target, source=source)
    return await roast_now(kind, target, send=send_alerts, source=source)


# ── owner positions: /manage and /leave ──────────────────────────────────────
# The bots tag wallet positions they did not open as owner positions and ask.
# This writes the owner's answer to owner_position_decisions.jsonl; each bot
# applies it on its next pass (about a minute). It never trades by itself.
def _owner_pos_pending(logs: str) -> list:
    out = []
    for name, bot in (("owner_positions_perps.json", "perps"),):
        try:
            with open(os.path.join(logs, name), encoding="utf-8-sig") as f:
                recs = (json.load(f) or {}).get("records") or {}
        except Exception:
            continue
        for key, r in recs.items():
            if isinstance(r, dict) and r.get("state") == "pending":
                out.append({"bot": bot, "key": key, "id": r.get("id"),
                            "ticker": r.get("ticker") or str(r.get("market") or key).split("-")[0]})
    return out


def owner_position_decision(kind: str, target, *, source: str = "owner_commands", logs: str = None) -> dict:
    logs = logs or _paths()["logs"]
    pend = _owner_pos_pending(logs)
    t = str(target or "").strip().lstrip("$").lower()
    if not t:
        lst = ", ".join(f"{p['ticker']} ({p['bot']})" for p in pend) or "none"
        return {"ok": False, "kind": kind, "text": f"Usage: /{kind} <ticker>. Pending owner positions: {lst}."}
    hits = [p for p in pend if t in (str(p["ticker"]).lower(), str(p["id"] or "").lower(),
                                    str(p["key"]).lower(), str(p["key"]).split("-")[0].lower())]
    have_state = any(os.path.isfile(os.path.join(logs, n))
                     for n in ("owner_positions_perps.json",))
    if have_state and not hits:
        lst = ", ".join(f"{p['ticker']} ({p['bot']})" for p in pend) or "none"
        return {"ok": False, "kind": kind,
                "text": f"No pending owner position matches {_h(target)}. Pending: {_h(lst)}."}
    path = os.environ.get("OWNER_POS_DECISIONS_FILE") or os.path.join(logs, "owner_position_decisions.jsonl")
    row = {"ts": time.time(), "at": datetime.now().isoformat(timespec="seconds"),
           "target": t, "decision": kind, "source": source}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    _log(f"owner_position decision {kind} {t} (source={source})")
    what = ", ".join(f"{p['ticker']} ({p['bot']})" for p in hits) or _h(target)
    verb = "I'll manage" if kind == "manage" else "Leaving to you:"
    return {"ok": True, "kind": kind, "target": t,
            "text": f"👤 {verb} {_h(what)}. The bot applies it within about a minute."}


# ── telegram source ──────────────────────────────────────────────────────────
class TgConflict(Exception):
    pass


async def _tg_call(token: str, method: str, params: dict, timeout: float = 35.0) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/{method}", json=params)
    if r.status_code == 409:
        raise TgConflict(method)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": f"http {r.status_code}"}


async def telegram_loop(cfg: dict, *, call=None, max_polls: int = None) -> str:
    """Poll the separate command bot. Returns why it stopped (never raises on Telegram errors)."""
    call = call or _tg_call
    tok, chat = cfg["token"], str(cfg["chat_id"])
    try:
        wh = await call(tok, "getWebhookInfo", {}, 15.0)
        if (wh.get("result") or {}).get("url"):
            _log("telegram: a webhook is set on the command bot; not polling (would conflict)")
            _save_state(telegram="stopped: webhook set")
            return "webhook"
    except TgConflict:
        _save_state(telegram="stopped: 409 conflict")
        return "conflict"
    except Exception as e:
        _log(f"telegram: webhook check failed ({type(e).__name__}); continuing")
    offset = _state().get("tg_offset")
    if offset is None:   # first start: skip the backlog so old messages are not replayed
        try:
            r = await call(tok, "getUpdates", {"offset": -1, "timeout": 0}, 15.0)
            res = r.get("result") or []
            offset = (res[-1]["update_id"] + 1) if res else 0
            _save_state(tg_offset=offset)
        except TgConflict:
            _log("telegram: 409 conflict, another reader polls this bot; stopping the telegram source")
            _save_state(telegram="stopped: 409 conflict")
            return "conflict"
    _save_state(telegram="polling")
    _log("telegram: polling the command bot (owner chat only)")
    polls, backoff = 0, 5
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            r = await call(tok, "getUpdates", {"offset": offset, "timeout": 25,
                                               "allowed_updates": ["message"]}, 35.0)
            backoff = 5
        except TgConflict:
            _log("telegram: 409 conflict, another reader polls this bot; stopping the telegram source")
            _save_state(telegram="stopped: 409 conflict")
            return "conflict"
        except Exception as e:
            _log(f"telegram: poll error {type(e).__name__}; retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        for u in r.get("result") or []:
            offset = max(offset or 0, int(u.get("update_id", 0)) + 1)
            _save_state(tg_offset=offset)
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != chat:
                continue                              # only the owner's chat
            text = msg.get("text") or ""
            try:
                res = await answer(text, source="telegram", send_alerts=False)
            except Exception as e:
                _log(f"telegram: answer failed {type(e).__name__}")
                res = {"text": "Something went wrong answering that. Check owner_commands.log."}
            if res.get("text"):
                try:
                    await call(tok, "sendMessage", {"chat_id": chat, "text": res["text"][:4000],
                                                    "parse_mode": "HTML",
                                                    "reply_to_message_id": msg.get("message_id")}, 20.0)
                except Exception as e:
                    _log(f"telegram: reply failed {type(e).__name__}")
    return "max_polls"


# ── file source ──────────────────────────────────────────────────────────────
def _read_cmd_file(p: str) -> str:
    with open(p, encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    if p.lower().endswith(".json"):
        try:
            j = json.loads(raw)
            return str(j.get("text") or j.get("command") or "") if isinstance(j, dict) else ""
        except Exception:
            return ""
    return raw.strip().splitlines()[0] if raw.strip() else ""


async def file_once() -> int:
    """Answer every command file waiting in owner_commands/. Returns how many were handled."""
    P = _paths()
    os.makedirs(P["cmd_dir"], exist_ok=True)
    n = 0
    for p in sorted(glob.glob(os.path.join(P["cmd_dir"], "*"))):
        if not os.path.isfile(p) or not p.lower().endswith((".txt", ".json")):
            continue
        ok = True
        try:
            res = await answer(_read_cmd_file(p), source="file", send_alerts=True)
            ok = bool(res.get("text"))
            if res.get("kind") == "help":
                await overseer().notify(res["text"])
        except Exception as e:
            _log(f"file: {os.path.basename(p)} failed {type(e).__name__}")
            ok = False
        dest = os.path.join(P["results"], "owner_commands", "done" if ok else "failed")
        os.makedirs(dest, exist_ok=True)
        shutil.move(p, os.path.join(dest, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.path.basename(p)}"))
        n += 1
    return n


async def file_loop(interval: float = 5.0) -> None:
    _save_state(file="watching")
    _log(f"file: watching {_paths()['cmd_dir']}")
    while True:
        try:
            await file_once()
        except Exception as e:
            _log(f"file: loop error {type(e).__name__}")
        await asyncio.sleep(interval)


# ── entry ────────────────────────────────────────────────────────────────────
def status(env=None) -> dict:
    env = os.environ if env is None else env
    m = mode(env)
    out = {"mode": m}
    if m == "telegram":
        c = tg_config(env)
        out["telegram"] = "ready" if c["ok"] else f"not ready: {c['reason']}"
    return out


async def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _load_env()
    if "--check" in argv:
        print(json.dumps(status(), indent=2))
        return 0
    if "--once" in argv:
        i = argv.index("--once")
        text = argv[i + 1] if i + 1 < len(argv) else "/roast"
        res = await answer(text, source="cli", send_alerts="--no-send" not in argv)
        print(re.sub(r"<[^>]+>", "", res.get("text") or "(no reply)"))
        return 0 if res.get("ok") or res.get("reason") == "cooldown" else 1
    async def _hb():
        while True:
            try:
                with open(os.path.join(_paths()["logs"], "owner_commands_heartbeat.json"), "w", encoding="utf-8") as f:
                    json.dump({"timestamp": datetime.now().astimezone().isoformat(), "status": "alive", "mode": mode()}, f)
            except Exception:
                pass
            await asyncio.sleep(60)
    asyncio.get_running_loop().create_task(_hb())
    m = mode()
    if m == "off":
        print("owner_commands: OWNER_COMMANDS=off, nothing to do")
        return 0
    if m == "telegram":
        cfg = tg_config()
        if not cfg["ok"]:
            _log(f"telegram source not started: {cfg['reason']}")
            _save_state(telegram=f"not started: {cfg['reason']}")
            return 0
        why = await telegram_loop(cfg)
        _log(f"telegram source stopped ({why}); idling so the launcher does not restart-loop")
        while True:
            await asyncio.sleep(3600)
    await file_loop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
