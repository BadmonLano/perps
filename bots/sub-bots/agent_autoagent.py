"""
agent_autoagent.py - The Overseer (Perps ecosystem)

Nightly judge of the agent's last 24h on the Perps lane only: perps.py decisions,
the open book (perp_positions.json, plural), closes and autopsies, the feed lanes the
packets carried (Flow, live odds, news), inbox handling, sub-bot health, skill
evolution and code_tuner patch discipline.

SI: provider order from LLM_MAIN / LLM_FALLBACK (grok | ollama | off). Both off = no SI
call; the run still writes a rule-based stub packet so the agent sees the inputs.

CADENCE: OVERSEER_SCHEDULED_HOUR_EST (default 2) America/New_York nightly.

OUTPUT:
  - inbox/agent_autoagent - overseer_<date>.json (P2 packet, the agent's morning read)
  - bot_logs/daily_summaries/<date>.json (rolling daily summary)
  - bot_logs/overseer_history.json, bot_logs/overseer.log
  - Telegram alert with headline + score (if TELEGRAM_TOKEN / TELEGRAM_CHAT_ID set)
(Public build: history trimmed; see TIMELINE.md.)
"""
import os

def _envv(key, default=None):
    """os.getenv, but a blank value (KEY= in .env) counts as unset and returns the default."""
    v = os.environ.get(key)
    return default if v is None or not str(v).strip() else v

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _si_gate  # LLM_MAIN / LLM_FALLBACK gate for every SI call
# Repo root: WORKSPACE_DIR, else the folder above bots/ (this file is bots/sub-bots/x.py).
_WS = (os.environ.get("WORKSPACE_DIR") or "").strip() or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import asyncio
import httpx
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# ── env ──────────────────────────────────────────────────────────────────────
_env_path = os.path.join(_WS, "bots", ".env")
load_dotenv(_env_path, override=False)

EST = ZoneInfo("America/New_York")

# ── paths ────────────────────────────────────────────────────────────────────
BASE          = os.path.join(_WS, r"bots")
LOGS          = os.path.join(_WS, "bot_logs")

# ── BLIND-SPOT TRACKER (2026-07-29) ─────────────────────────────────────────
# The overseer reported a FLAT BOOK while $100 at 10x was open, because it read
# "perp_position.json" and the writer is "perp_positionS.json". load_json
# returned the default, nothing errored, and every downstream check agreed there
# was no position.
#
# That is the whole failure class: A MISSING FILE AND AN EMPTY FILE ARE THE SAME
# VALUE.
#
# _seen_input() records whether the file actually existed. Anything missing goes
# into the blind-spot list and is reported OUT, so "I cannot see this" can never
# again be rendered as "there is nothing here". An overseer that does not know
# what it cannot see is worse than no overseer  -  it launders absence as evidence.
_BLIND_SPOTS: list = []


def _seen_input(path: str, value, label: str = ""):
    """Pass-through that flags a read whose file does not exist."""
    try:
        if not os.path.exists(path):
            _BLIND_SPOTS.append({"file": os.path.basename(path),
                                 "label": label or os.path.basename(path),
                                 "note": "file does not exist  -  value is a DEFAULT, not an observation"})
    except Exception:
        pass
    return value
JSON_RELAY    = os.path.join(_WS, "json_relay")
INBOX         = os.path.join(_WS, r"inbox")
INBOX_RESULTS = os.path.join(_WS, r"inbox_results")
INBOX_DONE    = os.path.join(INBOX_RESULTS, "done")
LEARNINGS     = os.path.join(_WS, "learnings", "LEARNINGS.md")
DAILY_SUM     = os.path.join(LOGS, "daily_summaries")
OVERSEER_HIST = os.path.join(LOGS, "overseer_history.json")
OVERSEER_LOG  = os.path.join(LOGS, "overseer.log")
SKILLS_DIR    = os.path.join(_WS, r"skills")

# ── xai grok api ─────────────────────────────────────────────────────────────
# Overseer judge runs Grok (xAI) when LLM_MAIN/LLM_FALLBACK list grok. The prompt runs a
# sharp intent-vs-action spec to keep the
# self-judge honest. Ollama stays as fallback.
# xAI is OpenAI-compatible (chat/completions), NOT Anthropic's messages shape.
GROK_API_KEY  = os.getenv("GROK_API_KEY", "")  # env var is GROK_API_KEY
XAI_API_URL  = "https://api.x.ai/v1/chat/completions"
def _overseer_model() -> str:
    """OVERSEER_GROK_MODEL pins it; else grok-4.7."""
    pin = os.getenv("OVERSEER_GROK_MODEL", "").strip()
    return pin if pin and pin.lower() != "auto" else "grok-4.7"


XAI_MODEL    = _overseer_model()
GROK_MAX_TOKENS = 4096
GROK_TIMEOUT    = 360  # grok-4.7 reasons past the old 120s cap

# ── ollama fallback ──────────────────────────────────────────────────────────
OLLAMA_URL          = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_FALLBACK_MODEL = os.getenv("OVERSEER_FALLBACK_MODEL", "qwen3:14b-q4_K_M")
OLLAMA_TIMEOUT      = 480

# ── telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── runtime constants ────────────────────────────────────────────────────────
LOOKBACK_HOURS  = 24
# Reverse-size check needs more than one day of closes to mean anything.
SIZING_LOOKBACK_HOURS = int(_envv("AUTOAGENT_SIZING_LOOKBACK_H", "168") or 168)
MAX_PROMPT_CHARS = 120000  # grok-4.7 has a large context window, still well under.

# ── daemon mode constants (Sprint 29.1) ──────────────────────────────────────
HEARTBEAT_FILE      = os.path.join(LOGS, "agent_autoagent_heartbeat.json")
LAST_RUN_STATE_FILE = os.path.join(LOGS, "overseer_last_run.json")
HEARTBEAT_INTERVAL  = 60       # write heartbeat every 60s while sleeping
SCHEDULED_HOUR_EST  = int(_envv("OVERSEER_SCHEDULED_HOUR_EST", "2"))   # default 2am
SCHEDULED_MINUTE    = int(_envv("OVERSEER_SCHEDULED_MINUTE", "0"))    # default :00
MIN_HOURS_BETWEEN_RUNS = 23    # belt-and-suspenders: don't double-fire if clock jumps


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def now_iso() -> str:
    return datetime.now(EST).isoformat(timespec="seconds")


def now_str() -> str:
    return datetime.now(EST).strftime("%Y-%m-%d %H:%M EST")


def log(msg: str):
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(OVERSEER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_text(path: str, max_chars: int = 5000) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
        return txt[-max_chars:] if len(txt) > max_chars else txt
    except Exception:
        return ""


def load_jsonl_tail(path: str, max_records: int = 50) -> list:
    """Read last N records from a JSONL file."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        records = []
        for line in lines[-max_records*2:]:  # over-read to account for filtering
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
        return records[-max_records:]
    except Exception as e:
        log(f"  load_jsonl_tail({path}): {e}")
        return []


def save_json_atomic(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def is_recent(timestamp_str: str, hours: int = LOOKBACK_HOURS) -> bool:
    """True if ISO timestamp string falls within last N hours."""
    if not timestamp_str:
        return False
    try:
        ts = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=EST)
        cutoff = datetime.now(EST) - timedelta(hours=hours)
        return ts >= cutoff
    except Exception:
        return False


def parse_timestamp(timestamp_str: str):
    """Best-effort ISO timestamp parse, returns datetime or None."""
    if not timestamp_str:
        return None
    try:
        ts = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=EST)
        return ts.astimezone(EST)
    except Exception:
        return None


def time_bucket(ts) -> str:
    """Bucket a datetime into morning/midday/afternoon/evening/late."""
    if not ts:
        return "unknown"
    h = ts.hour
    if 5 <= h < 11:
        return "morning"      # 5am-11am
    if 11 <= h < 14:
        return "midday"       # 11am-2pm
    if 14 <= h < 17:
        return "afternoon"    # 2pm-5pm (the tilt window)
    if 17 <= h < 21:
        return "evening"      # 5pm-9pm
    return "late"             # 9pm-5am


async def notify(msg: str):
    """Best-effort telegram. No-op if not configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id":    TELEGRAM_CHAT_ID,
                    "text":       msg,
                    "parse_mode": "HTML",
                },
            )
    except Exception as e:
        log(f"telegram notify failed (non-fatal): {e}")


def _filter_recent_hours(records, hours, *fields):
    """filter_recent with an explicit window (hours)."""
    out = []
    for r in records if isinstance(records, list) else []:
        if isinstance(r, dict) and any(r.get(f) and is_recent(str(r.get(f)), hours) for f in fields):
            out.append(r)
    return out


def _read_jsonl_tail(path, max_bytes=4_000_000):
    """Parse the last max_bytes of a JSONL file (miss_book grows forever)."""
    rows = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        lines = data.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]  # first line is probably cut
        for ln in lines:
            ln = ln.strip()
            if ln:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def filter_recent(records, *fields):
    """Filter list of dicts to only those with one of `fields` within LOOKBACK_HOURS."""
    out = []
    if not isinstance(records, list):
        return out
    for r in records:
        if not isinstance(r, dict):
            continue
        for f in fields:
            ts = r.get(f)
            if ts and is_recent(str(ts)):
                out.append(r)
                break
    return out


# ═════════════════════════════════════════════════════════════════════════════
# DAEMON HELPERS (Sprint 29.1  -  forever-loop mode)
# ═════════════════════════════════════════════════════════════════════════════

def write_heartbeat(status: str, extra: dict = None):
    """Write the bot's own heartbeat. Warden + overseer's own bot_health
    block reads this. 30-min staleness = bot considered dead."""
    payload = {
        "timestamp":  now_iso(),
        "status":     status,
        "scheduled_hour_est":   SCHEDULED_HOUR_EST,
        "scheduled_minute":     SCHEDULED_MINUTE,
        "next_run_at_est":      _next_scheduled_dt().isoformat(),
        "grok_model":           XAI_MODEL,
        "ollama_fallback_model": OLLAMA_FALLBACK_MODEL,
    }
    if extra:
        payload.update(extra)
    try:
        save_json_atomic(HEARTBEAT_FILE, payload)
    except Exception as e:
        log(f"heartbeat write failed (non-fatal): {e}")


def _next_scheduled_dt() -> datetime:
    """Compute the next 2am EST datetime, in EST. If we're already past 2am today,
    returns tomorrow's 2am. Used both for sleep-target and heartbeat reporting."""
    now = datetime.now(EST)
    target = now.replace(hour=SCHEDULED_HOUR_EST, minute=SCHEDULED_MINUTE, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target


def _seconds_until_next_run() -> float:
    """How many seconds until the next 2am EST."""
    return (_next_scheduled_dt() - datetime.now(EST)).total_seconds()


def _load_last_run_state() -> dict:
    """Read state from previous run  -  used to enforce MIN_HOURS_BETWEEN_RUNS."""
    return load_json(LAST_RUN_STATE_FILE, {}) or {}


def _save_last_run_state(state: dict):
    """Persist last-run state so a daemon restart doesn't double-fire."""
    save_json_atomic(LAST_RUN_STATE_FILE, state)


def _can_fire_run() -> tuple:
    """
    Belt-and-suspenders: even if our scheduler logic gets confused (DST,
    manual time change, drift), refuse to re-fire within MIN_HOURS_BETWEEN_RUNS.
    Returns (allowed: bool, reason: str).
    """
    state = _load_last_run_state()
    last_completed_iso = state.get("last_completed_at")
    if not last_completed_iso:
        return True, "no prior run recorded"
    try:
        last_dt = datetime.fromisoformat(last_completed_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=EST)
        elapsed_h = (datetime.now(EST) - last_dt).total_seconds() / 3600
        if elapsed_h < MIN_HOURS_BETWEEN_RUNS:
            return False, f"only {elapsed_h:.1f}h since last run (min {MIN_HOURS_BETWEEN_RUNS}h)"
        return True, f"{elapsed_h:.1f}h since last run, OK to fire"
    except Exception as e:
        return True, f"could not parse last run state ({e}), defaulting to allow"


def _record_run_completion(success: bool, packet_path: str = None, error: str = None):
    """After a run completes (or errors), persist state."""
    state = _load_last_run_state()
    state["last_attempted_at"] = now_iso()
    if success:
        state["last_completed_at"] = now_iso()
        state["last_packet_path"]  = packet_path
        state["last_status"]       = "success"
        state["consecutive_failures"] = 0
    else:
        state["last_status"] = "error"
        state["last_error"]  = (error or "")[:300]
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    _save_last_run_state(state)


# ═════════════════════════════════════════════════════════════════════════════
# STAGE 1  -  INPUT COLLECTION (FULL COVERAGE)
# Sweep everything the agent touched in the last 24h. Better to over-read than under.
# ═════════════════════════════════════════════════════════════════════════════

def collect_inputs() -> dict:
    """Read everything from the last 24h relevant to grading the agent on the Perps lane."""
    log("STAGE 1: collecting inputs from last 24h…")
    inputs = {
        "lookback_hours": LOOKBACK_HOURS,
        "now": now_iso(),

        # Stated intent
        "thesis":       {},
        "news_catalysts": {},

        # Wallet / collateral
        "wallet_balance": {},

        # The one execution lane
        "perps":  {},
        # Feed lanes the packets carried (bots/feeds + bots/feed_hook.py outputs)
        "feeds":  {},

        # Cross-cutting
        "autopsies":       [],
        "inbox_handled":   [],
        "inbox_decisions": [],
        "learnings_appended": "",
        "yesterdays_overseer_report": None,

        # Time-of-day analysis
        "time_buckets": {
            "morning":   {"trades": 0, "signals": 0},
            "midday":    {"trades": 0, "signals": 0},
            "afternoon": {"trades": 0, "signals": 0},
            "evening":   {"trades": 0, "signals": 0},
            "late":      {"trades": 0, "signals": 0},
        },

        # Bot health: perps.py + its sub-bots
        "bot_health": {
            "perps_bot":       {"heartbeat_age_sec": None, "alive": False},
            "security_warden": {"heartbeat_age_sec": None, "alive": False},
            "code_tuner":      {"heartbeat_age_sec": None, "alive": False},
            "agent_autoagent": {"heartbeat_age_sec": None, "alive": False},
        },

        # skill-evolution + capability-gap routing evidence
        "skill_evolution_summary": {},
        "skill_requests_pending": 0,
        "last_3day_packet_ts": None,
        "meeting_watcher_gaps": 0,
        "patch_landed_acks": [],

        # code_tuner observation surface evidence
        "code_tuner_setup_state": {},
        "metrics_current_snapshot": {},
        "source_delta_count_24h": 0,
        "code_tuner_setup_activity_24h": {},
        "pending_patch_packets_in_owner_inbox": 0,
    }

    # ── Stated intent ─────────────────────────────────────────
    _th = os.path.join(LOGS, "agent_thesis.json")
    inputs["thesis"] = _seen_input(_th, load_json(_th, {}) or {}, "agent thesis")
    inputs["news_catalysts"] = load_json(os.path.join(LOGS, "news_catalysts.json"), {}) or {}

    # ── Wallet / collateral (perps.py writes wallet_balance.json) ─────
    _wb = os.path.join(LOGS, "wallet_balance.json")
    inputs["wallet_balance"] = load_json(_wb, {}) or {}
    try:
        _wb_age_min = (datetime.now().timestamp() - os.path.getmtime(_wb)) / 60.0
        inputs["wallet_balance"]["_age_minutes"] = round(_wb_age_min, 1)
        if _wb_age_min > 60:
            inputs["wallet_balance"]["_trust"] = "STALE - untrusted (>60 min old)"
    except Exception:
        inputs["wallet_balance"]["_trust"] = "MISSING - no wallet_balance.json"

    # ── PERPS LANE ────────────────────────────────────────────
    perps = inputs["perps"]
    # perp_positions.json (PLURAL) is the venue of record: {market: position_dict}.
    # A missing file is NOT a flat book; surface the count so the two never look alike.
    _pp_path = os.path.join(LOGS, "perp_positions.json")
    _pp = _seen_input(_pp_path, load_json(_pp_path, None), "perp positions")
    perps["position_open"] = _pp
    if isinstance(_pp, dict):
        _live = {m: p for m, p in _pp.items()
                 if isinstance(p, dict) and (p.get("size_usd") or p.get("collateral_usd"))}
        perps["open_position_count"] = len(_live)
        perps["open_markets"] = sorted(_live.keys())
        perps["open_notional_usd"] = round(
            sum(float(p.get("size_usd") or 0) for p in _live.values()), 2)
        perps["open_collateral_usd"] = round(
            sum(float(p.get("collateral_usd") or 0) for p in _live.values()), 2)
        perps["_position_source"] = "perp_positions.json"
    else:
        perps["open_position_count"] = 0
        perps["open_markets"] = []
        perps["open_notional_usd"] = 0.0
        perps["open_collateral_usd"] = 0.0
        perps["_position_source"] = ("no positions file readable  -  this is NOT the same "
                                     "as a flat book; treat exposure as UNKNOWN")
    _pnl = os.path.join(LOGS, "perp_pnl.json")
    perps["pnl_log"] = _seen_input(_pnl, load_json(_pnl, None), "perp pnl")
    # Decisions (opens / declines / holds) and closed episodes
    _dec = load_json(os.path.join(LOGS, "perp_decisions.json"), []) or []
    if isinstance(_dec, dict):
        _dec = _dec.get("decisions") or list(_dec.values())
    perps["decisions_24h"] = filter_recent([d for d in _dec if isinstance(d, dict)],
                                           "ts", "timestamp", "time")[-60:]
    perps["episodes_24h"] = filter_recent(
        load_jsonl_tail(os.path.join(LOGS, "perp_episodes.jsonl"), max_records=200),
        "ts", "closed_at", "timestamp")[-40:]
    perps["episodes_sizing"] = _filter_recent_hours(
        load_jsonl_tail(os.path.join(LOGS, "perp_episodes.jsonl"), max_records=500),
        SIZING_LOOKBACK_HOURS, "ts", "closed_at", "timestamp")
    perps["autopsy_lessons"] = load_json(os.path.join(LOGS, "perp_autopsy_lessons.json"), {}) or {}
    perps["missed_autopsy"] = load_json(os.path.join(LOGS, "missed_perp_autopsy.json"), {}) or {}
    perps["exit_suggestion"] = load_json(os.path.join(INBOX, "perp_exit_suggestion.json"), None)
    perps["last_signal"] = load_json(os.path.join(JSON_RELAY, "perp_signal.json"), None)
    perps["param_changes_24h"] = filter_recent(
        load_jsonl_tail(os.path.join(LOGS, "perps_param_changes.jsonl"), max_records=100),
        "ts", "timestamp")
    for d in perps["decisions_24h"]:
        ts = parse_timestamp(d.get("ts") or d.get("timestamp") or d.get("time"))
        inputs["time_buckets"][time_bucket(ts)]["signals"] += 1
    for e in perps["episodes_24h"]:
        ts = parse_timestamp(e.get("ts") or e.get("closed_at") or e.get("timestamp"))
        inputs["time_buckets"][time_bucket(ts)]["trades"] += 1

    # ── FEED LANES (what the packets carried; FEED_CADENCE=manual by default) ──
    feeds = inputs["feeds"]
    for name, fn in (("flow", "flow_cache.json"), ("router_state", "feed_router_state.json"),
                     ("kalshi_odds", "kalshi_implied_probs.json"),
                     ("polymarket_odds", "polymarket_implied_probabilities.json")):
        p = os.path.join(LOGS, fn)
        try:
            age = round((datetime.now().timestamp() - os.path.getmtime(p)) / 60.0, 1)
        except OSError:
            age = None
        feeds[name] = {"age_min": age, "present": age is not None}

    # ── INBOX HANDLED + DECISIONS ─────────────────────────────
    if os.path.exists(INBOX_DONE):
        try:
            done_files = sorted(
                os.listdir(INBOX_DONE),
                key=lambda n: os.path.getmtime(os.path.join(INBOX_DONE, n)),
                reverse=True
            )
            cutoff = datetime.now(EST) - timedelta(hours=LOOKBACK_HOURS)
            recent = []
            decisions = []
            for fn in done_files[:300]:
                full = os.path.join(INBOX_DONE, fn)
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(full), tz=EST)
                    if mtime < cutoff:
                        continue
                    recent.append({"filename": fn, "mtime": mtime.isoformat()})
                    if fn.endswith(".json"):
                        packet = load_json(full, {})
                        if packet:
                            action_taken = packet.get("actions_taken") or packet.get("agent_decision") or packet.get("decision")
                            if action_taken:
                                decisions.append({
                                    "filename": fn,
                                    "type":     packet.get("type", "unknown"),
                                    "market":   packet.get("market") or packet.get("chain", ""),
                                    "decision": action_taken,
                                    "ts":       mtime.isoformat(),
                                })
                            if "autopsy" in fn.lower():
                                inputs["autopsies"].append({"filename": fn, "ts": mtime.isoformat()})
                except Exception:
                    continue
            inputs["inbox_handled"]   = recent[:150]
            inputs["inbox_decisions"] = decisions[:50]
        except Exception as e:
            log(f"  inbox_handled scan: {e}")

    # ── LEARNINGS APPENDED ────────────────────────────────────
    inputs["learnings_appended"] = load_text(LEARNINGS, max_chars=4000)

    # ── YESTERDAY'S OVERSEER REPORT (continuity) ──────────────
    history = load_json(OVERSEER_HIST, []) or []
    if isinstance(history, list) and history:
        last = history[-1]
        if last and last.get("inbox_path") and os.path.exists(last["inbox_path"]):
            inputs["yesterdays_overseer_report"] = load_json(last["inbox_path"], None)
        elif last and last.get("inbox_path"):
            archived_path = os.path.join(INBOX_DONE, os.path.basename(last["inbox_path"]))
            if os.path.exists(archived_path):
                inputs["yesterdays_overseer_report"] = load_json(archived_path, None)

    # ── BOT HEALTH (heartbeat staleness) ──────────────────────
    for bot in list(inputs["bot_health"].keys()):
        path = os.path.join(LOGS, f"{bot}_heartbeat.json")
        if not os.path.exists(path):
            continue
        try:
            age_sec = datetime.now(EST).timestamp() - os.path.getmtime(path)
            h = inputs["bot_health"][bot]
            h["heartbeat_age_sec"] = round(age_sec, 1)
            # Bots may declare their cadence in the heartbeat; read it instead of guessing.
            _hb = load_json(path, {}) or {}
            _declared_min = None
            for _k in ("sweep_interval_min", "interval_min", "cadence_min", "heartbeat_interval_min"):
                if isinstance(_hb.get(_k), (int, float)) and _hb[_k] > 0:
                    _declared_min = float(_hb[_k]); break
            if _declared_min is None:
                for _k in ("interval_sec", "heartbeat_interval_sec", "cadence_sec"):
                    if isinstance(_hb.get(_k), (int, float)) and _hb[_k] > 0:
                        _declared_min = float(_hb[_k]) / 60.0; break
            if _declared_min:
                threshold = _declared_min * 60 * 2.5
                _basis = f"declared cadence {_declared_min:.0f}m x2.5"
            else:
                threshold = 1800
                _basis = "fallback: 30m (standard)"
            _alive = age_sec < threshold
            h["alive"] = _alive
            h["threshold_sec"] = round(threshold)
            h["threshold_basis"] = _basis
            h["health_note"] = f"beat {age_sec/60:.0f}m ago vs {threshold/60:.0f}m threshold ({_basis})"
            if _hb.get("status"):
                h["reported_status"] = _hb["status"]
            if _hb.get("stop_reason"):
                h["stop_reason"] = _hb.get("stop_reason")
                h["stopped_at"] = _hb.get("stopped_at")
                if _hb.get("stop_reason") == "user_stopped" and not _alive:
                    h["intentional_stop"] = True
                    h["health_note"] = (f"STOPPED BY OWNER at {_hb.get('stopped_at')} (user_stopped)  -  "
                                        "intentional, not dead. Do not count as a failure.")
        except Exception:
            pass

    log(
        f"  thesis={'present' if inputs['thesis'] else 'EMPTY'} | "
        f"perp_open={perps['open_position_count']} | "
        f"perp_decisions={len(perps['decisions_24h'])} | "
        f"perp_closes={len(perps['episodes_24h'])} | "
        f"decisions={len(inputs['inbox_decisions'])} | "
        f"prev_overseer={'yes' if inputs['yesterdays_overseer_report'] else 'no'}"
    )

    # skill-evolution + capability-gap evidence
    sprint41 = _collect_sprint41_evidence()
    inputs["skill_evolution_summary"] = sprint41.get("skill_evolution_summary", {})
    inputs["skill_requests_pending"]  = sprint41.get("skill_requests_pending", 0)
    inputs["last_3day_packet_ts"]     = sprint41.get("last_3day_packet_ts")
    inputs["meeting_watcher_gaps"]    = sprint41.get("meeting_watcher_gaps", 0)
    inputs["patch_landed_acks"]       = sprint41.get("patch_landed_acks", [])

    # code_tuner observation surface evidence
    sprint42 = _collect_sprint42_evidence()
    inputs["code_tuner_setup_state"]            = sprint42.get("code_tuner_setup_state", {})
    inputs["metrics_current_snapshot"]          = sprint42.get("metrics_current_snapshot", {})
    inputs["source_delta_count_24h"]            = sprint42.get("source_delta_count_24h", 0)
    inputs["code_tuner_setup_activity_24h"]     = sprint42.get("code_tuner_setup_activity_24h", {})
    inputs["pending_patch_packets_in_owner_inbox"] = sprint42.get("pending_patch_packets_in_owner_inbox", 0)
    inputs["oldest_pending_patches"]            = sprint42.get("oldest_pending_patches", [])
    return inputs




# ═════════════════════════════════════════════════════════════════════════════
# SPRINT 41+ EVIDENCE COLLECTOR (v4.2)
# ═════════════════════════════════════════════════════════════════════════════
#
# Reads:
#   • bot_logs/skill_evolution_log.jsonl        -  warden Layer 10's append-only
#     audit (auto_applied / owner_approved / owner_rejected / refused / etc.)
#   • bots/json_relay/skill_requests.json       -  NOT READ (dead queue; pending hardcoded 0)
#   • bot_logs/skill_evolution_3day_state.json  -  warden's last 3-day packet fire ts
#   • inbox_results/done/ for                   -  gap_routed_to_code_tuner meeting
#     responses + patch_landed_ack packets
#
# Output structure feeds the xAI grok-4.7 judge (ollama fallback): did the agent amend skills with
# evidence? Did his bots correctly route capability gaps? Did patches land?
# ═════════════════════════════════════════════════════════════════════════════

_SKILL_EVOLUTION_LOG_PATH = os.path.join(LOGS, "skill_evolution_log.jsonl")
# Unused path. skill_requests.json is not read; pending is hardcoded 0 below.
# _SKILL_REQUESTS_PATH      = os.path.join(BASE, "json_relay", "skill_requests.json")
_SKILL_EVO_3DAY_STATE     = os.path.join(LOGS, "skill_evolution_3day_state.json")

_PATCH_LANDED_ACK_PREFIXES = (
    # old priority-prefix convention
    "P2_perps_bot_patch_landed_ack_",
    # Sprint 44  -  new botname convention (match BOTH so acks aren't silently dropped)
    "perps_bot - patch_landed_ack_",
    "code_tuner - patch_landed_ack_",
)

_MEETING_RESPONSE_PREFIXES = (
    # old priority-prefix convention
    "P2_perps_meeting_response_",
    # new botname convention
    "code_tuner - meeting_response_",   # Sprint 42  -  code_tuner's own meeting_watcher
    "perps_bot - meeting_response_",
)


def _collect_sprint41_evidence() -> dict:
    """v4.2 / v4.3  -  Scan Sprint 41+ artifacts for the last 24h.

    Returns dict with:
      skill_evolution_summary: {auto_applied, owner_approved, owner_amended,
                                owner_rejected, refused, doctrine_drift_alerts}
      skill_requests_pending: int
      last_3day_packet_ts: ISO str or None
      meeting_watcher_gaps: int (count of capability_gap [v4.3] OR legacy
                                 gap_routed_to_code_tuner [v4.2] in window)
      patch_landed_acks: [{"bot": ..., "version": ..., "ts": ...}, ...]

    Sprint 42 note: gap_routed_to_code_tuner is the LEGACY label (Sprint 41
    doctrine  -  code_tuner auto-fixed gaps). The new label is capability_gap
    (Sprint 42  -  code_tuner observes; capability gaps route to owner_inbox for
    The owner to apply manually). This function detects BOTH for backward compat
    against old packets still sitting in inbox_results/done/.

    Fail-soft: returns the structure with zeros on any error. Overseer
    judgment still runs, just downgraded.
    """
    summary = {
        "skill_evolution_summary": {
            "auto_applied": 0,
            "owner_approved": 0,
            "owner_amended": 0,
            "owner_rejected": 0,
            "refused": 0,
            "doctrine_drift_alerts": 0,
            "recent_targets": [],   # last 5 amendments: [{"target":..., "event":..., "ts":...}]
        },
        "skill_requests_pending": 0,
        "last_3day_packet_ts": None,
        "meeting_watcher_gaps": 0,
        "patch_landed_acks": [],
    }

    now_ts = datetime.now(EST).timestamp()
    cutoff_ts = now_ts - (LOOKBACK_HOURS * 3600)

    # ── skill_evolution_log.jsonl ────────────────────────────────────────
    if os.path.exists(_SKILL_EVOLUTION_LOG_PATH):
        try:
            recent_targets = []
            with open(_SKILL_EVOLUTION_LOG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts_str = rec.get("ts", "")
                    try:
                        rec_ts = datetime.fromisoformat(ts_str).timestamp()
                    except Exception:
                        continue
                    if rec_ts < cutoff_ts:
                        continue
                    event = rec.get("event", "")
                    if event == "auto_applied":
                        summary["skill_evolution_summary"]["auto_applied"] += 1
                    elif event == "owner_approved_and_applied":
                        summary["skill_evolution_summary"]["owner_approved"] += 1
                    elif event == "owner_amended_and_applied":
                        summary["skill_evolution_summary"]["owner_amended"] += 1
                    elif event == "owner_rejected":
                        summary["skill_evolution_summary"]["owner_rejected"] += 1
                    elif event == "refused":
                        summary["skill_evolution_summary"]["refused"] += 1
                    elif event == "doctrine_drift_alert":
                        summary["skill_evolution_summary"]["doctrine_drift_alerts"] += 1

                    # Track recent amendment targets
                    if event in ("auto_applied", "owner_approved_and_applied",
                                 "owner_amended_and_applied", "owner_rejected", "refused"):
                        recent_targets.append({
                            "target": rec.get("target", "?"),
                            "event":  event,
                            "ts":     ts_str,
                        })
            # Keep the 5 most recent
            recent_targets.sort(key=lambda x: x["ts"], reverse=True)
            summary["skill_evolution_summary"]["recent_targets"] = recent_targets[:5]
        except Exception as e:
            log(f"  sprint41: skill_evolution_log read failed: {e}")

    # ── skill_requests.json = DEAD QUEUE (meta-tools phase; amendments go
    # agent_direct now). RETIRED: never read this file. A missing file used to
    # collapse to pending=0, laundering absence as a clear queue (absence-is-signal
    # violation). We revive neither a reader nor a writer; the shape is kept only so
    # downstream prompt sections don't KeyError, and it's tagged DEAD so the overseer
    # does not roast an empty dead queue.
    summary["skill_requests_pending"] = 0
    summary["skill_requests_status"] = "DEAD_QUEUE_agent_direct"

    # ── last 3-day packet fire ts ─────────────────────────────────────────
    if os.path.exists(_SKILL_EVO_3DAY_STATE):
        try:
            with open(_SKILL_EVO_3DAY_STATE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                summary["last_3day_packet_ts"] = state.get("last_fired_ts")
        except Exception as e:
            log(f"  sprint41: 3day_state read failed: {e}")

    # ── Walk inbox_results/done for meeting responses + patch acks (24h) ──
    if os.path.exists(INBOX_DONE):
        try:
            all_files = os.listdir(INBOX_DONE)
        except Exception as e:
            log(f"  sprint41: listdir INBOX_DONE failed: {e}")
            all_files = []

        for fname in all_files:
            fpath = os.path.join(INBOX_DONE, fname)
            try:
                mtime = os.path.getmtime(fpath)
                if mtime < cutoff_ts:
                    continue
            except OSError:
                continue

            # Meeting response  -  look for capability_gap (Sprint 42) OR
            # gap_routed_to_code_tuner (Sprint 41 legacy) status. Both are
            # the same training-signal class  -  just under different vocabulary.
            if any(fname.startswith(p) for p in _MEETING_RESPONSE_PREFIXES):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    # v4.3 (Sprint 42): new label "capability_gap" appears as
                    # **gap_classification:** capability_gap. Old packets still
                    # have the legacy "gap_routed_to_code_tuner" string in body.
                    if ("capability_gap" in content
                        or "gap_routed_to_code_tuner" in content):
                        summary["meeting_watcher_gaps"] += 1
                except Exception:
                    continue
                continue

            # Patch landed ack
            if any(fname.startswith(p) for p in _PATCH_LANDED_ACK_PREFIXES):
                bot = "unknown"
                for prefix in _PATCH_LANDED_ACK_PREFIXES:
                    if fname.startswith(prefix):
                        if prefix.startswith("P2_"):
                            # old: "P2_perps_bot_patch_landed_ack_" → "perps_bot"
                            bot = prefix.replace("P2_", "").replace("_patch_landed_ack_", "")
                        else:
                            # new: "perps_bot - patch_landed_ack_" → "perps_bot"
                            bot = prefix.split(" - ")[0]
                        break
                # Try to extract version from markdown body
                new_version = "?"
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    for line in content.split("\n"):
                        line_l = line.lower()
                        if "new version" in line_l and "`" in line:
                            parts = line.split("`")
                            if len(parts) >= 2:
                                new_version = parts[-2]
                                break
                except Exception:
                    pass
                summary["patch_landed_acks"].append({
                    "bot":     bot,
                    "version": new_version,
                    "ts":      datetime.fromtimestamp(mtime, EST).isoformat(),
                })

    # Sort patch acks by ts desc
    summary["patch_landed_acks"].sort(key=lambda x: x["ts"], reverse=True)
    summary["patch_landed_acks"] = summary["patch_landed_acks"][:10]

    return summary


# ═════════════════════════════════════════════════════════════════════════════
# SPRINT 42 EVIDENCE COLLECTOR (v4.3)
# ═════════════════════════════════════════════════════════════════════════════
#
# Reads:
#   • bot_logs/bots_list.json               -  code_tuner's bot registry; dual-
#     owned with warden. Tells us configured vs unconfigured bot counts,
#     monitoring_active counts, action categories chosen by the agent.
#   • bot_logs/metrics_current.json         -  code_tuner's 30-min refreshed
#     per-bot perf surface. Each entry has rolling metric values + last_updated.
#   • inbox_results/done/ for 24h           -  P3_source_updated_*, setup
#     invitations/discoveries/rejections counts.
#   • owner_inbox/ (workspace root)          -  The agent-authored patch packets
#     sitting unapplied by the owner. Stale patches = The agent should follow up.
#
# Output structure feeds the xAI grok-4.7 judge (ollama fallback): did the agent complete setup mode? Are
# bot perf metrics current? Are source changes anomalous? Is patch authorship
# discipline holding (every code change has a patch packet in owner_inbox)?
# ═════════════════════════════════════════════════════════════════════════════

_BOTS_LIST_PATH         = os.path.join(LOGS, "bots_list.json")
_METRICS_CURRENT_PATH   = os.path.join(LOGS, "metrics_current.json")
_OWNER_INBOX_DIR         = os.path.join(_WS, r"owner_inbox")

_SOURCE_DELTA_PREFIXES = (
    "code_tuner - source_updated_",
)
_CODE_TUNER_SETUP_PREFIXES_INVITATION = (
    "code_tuner - setup_invitation_",
)
_CODE_TUNER_SETUP_PREFIXES_DISCOVERY = (
    "code_tuner - discovery_",
)
_CODE_TUNER_SETUP_PREFIXES_REJECTED = (
    "code_tuner - setup_rejected_",
)
_AGENT_PATCH_PACKET_PREFIXES = (
    "agent - patch_",     # The agent-authored patches for the owner to apply (priority off the filename now)
)


def _collect_sprint42_evidence() -> dict:
    """v4.3  -  Scan Sprint 42 code_tuner observation surface for the last 24h.

    Returns dict with:
      code_tuner_setup_state: {
        total_known_bots, configured_count, unconfigured_count,
        monitoring_active_count, action_breakdown: {track: N, skip: N, ...},
        unconfigured_bots: [list of names]
      }
      metrics_current_snapshot: {
        last_updated, bots_with_metrics: int, sample_metrics: {<bot>: {...}}
      }
      source_delta_count_24h: int
      code_tuner_setup_activity_24h: {invitations: N, discoveries: N, rejections: N}
      pending_patch_packets_in_owner_inbox: int + ages of oldest 3

    Fail-soft: returns zero/empty structure on any error.
    """
    out = {
        "code_tuner_setup_state": {
            "total_known_bots": 0,
            "configured_count": 0,
            "unconfigured_count": 0,
            "monitoring_active_count": 0,
            "action_breakdown": {},
            "unconfigured_bots": [],
        },
        "metrics_current_snapshot": {
            "last_updated": None,
            "bots_with_metrics": 0,
            "sample_metrics": {},
        },
        "source_delta_count_24h": 0,
        "code_tuner_setup_activity_24h": {
            "invitations": 0,
            "discoveries": 0,
            "rejections": 0,
        },
        "pending_patch_packets_in_owner_inbox": 0,
        "oldest_pending_patches": [],   # [{name, age_hours}]
    }

    now_ts = datetime.now(EST).timestamp()
    cutoff_ts = now_ts - (LOOKBACK_HOURS * 3600)

    # ── bots_list.json  -  setup-mode state ────────────────────────────────
    if os.path.exists(_BOTS_LIST_PATH):
        try:
            with open(_BOTS_LIST_PATH, "r", encoding="utf-8") as f:
                bots_list = json.load(f)
            if isinstance(bots_list, dict):
                bots = bots_list.get("bots") or {}
                if isinstance(bots, dict):
                    state = out["code_tuner_setup_state"]
                    unconf_names = []
                    actions = {}
                    for bot_name, bot_meta in bots.items():
                        state["total_known_bots"] += 1
                        if not isinstance(bot_meta, dict):
                            continue
                        curated = bot_meta.get("_curated_by_agent") or {}
                        if isinstance(curated, dict) and curated:
                            state["configured_count"] += 1
                            action = curated.get("action") or "?"
                            actions[action] = actions.get(action, 0) + 1
                            if curated.get("monitoring_active"):
                                state["monitoring_active_count"] += 1
                        else:
                            state["unconfigured_count"] += 1
                            unconf_names.append(bot_name)
                    state["action_breakdown"] = actions
                    state["unconfigured_bots"] = sorted(unconf_names)[:15]
        except Exception as e:
            log(f"  sprint42: bots_list.json read failed: {e}")

    # ── metrics_current.json  -  rolling perf snapshot ─────────────────────
    if os.path.exists(_METRICS_CURRENT_PATH):
        try:
            with open(_METRICS_CURRENT_PATH, "r", encoding="utf-8") as f:
                metrics = json.load(f)
            if isinstance(metrics, dict):
                out["metrics_current_snapshot"]["last_updated"] = metrics.get(
                    "last_updated") or metrics.get("ts")
                bots_metrics = metrics.get("bots") or {}
                if isinstance(bots_metrics, dict):
                    out["metrics_current_snapshot"]["bots_with_metrics"] = len(bots_metrics)
                    # Sample up to 4 bots' metrics for the prompt
                    sample = {}
                    for bot_name in list(bots_metrics.keys())[:4]:
                        bot_metrics = bots_metrics.get(bot_name) or {}
                        if isinstance(bot_metrics, dict):
                            # Flatten one level  -  keep up to 6 metric values per bot
                            flat = {}
                            count = 0
                            for k, v in bot_metrics.items():
                                if count >= 6:
                                    break
                                if isinstance(v, dict):
                                    val = v.get("value")
                                    if val is not None:
                                        flat[k] = val
                                        count += 1
                                elif isinstance(v, (int, float, str)):
                                    flat[k] = v
                                    count += 1
                            if flat:
                                sample[bot_name] = flat
                    out["metrics_current_snapshot"]["sample_metrics"] = sample
        except Exception as e:
            log(f"  sprint42: metrics_current.json read failed: {e}")

    # ── Walk inbox_results/done for source deltas + setup mode (24h) ─────
    if os.path.exists(INBOX_DONE):
        try:
            all_files = os.listdir(INBOX_DONE)
        except Exception as e:
            log(f"  sprint42: listdir INBOX_DONE failed: {e}")
            all_files = []

        for fname in all_files:
            fpath = os.path.join(INBOX_DONE, fname)
            try:
                mtime = os.path.getmtime(fpath)
                if mtime < cutoff_ts:
                    continue
            except OSError:
                continue

            if any(fname.startswith(p) for p in _SOURCE_DELTA_PREFIXES):
                out["source_delta_count_24h"] += 1
            elif any(fname.startswith(p) for p in _CODE_TUNER_SETUP_PREFIXES_INVITATION):
                out["code_tuner_setup_activity_24h"]["invitations"] += 1
            elif any(fname.startswith(p) for p in _CODE_TUNER_SETUP_PREFIXES_DISCOVERY):
                out["code_tuner_setup_activity_24h"]["discoveries"] += 1
            elif any(fname.startswith(p) for p in _CODE_TUNER_SETUP_PREFIXES_REJECTED):
                out["code_tuner_setup_activity_24h"]["rejections"] += 1

    # ── Walk owner_inbox/ for pending the agent-authored patches ─────────────
    if os.path.exists(_OWNER_INBOX_DIR):
        try:
            for fname in os.listdir(_OWNER_INBOX_DIR):
                if not fname.endswith(".md"):
                    continue
                if not any(fname.startswith(p) for p in _AGENT_PATCH_PACKET_PREFIXES):
                    continue
                fpath = os.path.join(_OWNER_INBOX_DIR, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                out["pending_patch_packets_in_owner_inbox"] += 1
                age_hours = (now_ts - mtime) / 3600.0
                out["oldest_pending_patches"].append({
                    "name": fname,
                    "age_hours": round(age_hours, 1),
                })
        except Exception as e:
            log(f"  sprint42: owner_inbox scan failed: {e}")

    # Sort pending patches by age desc, keep 3 oldest
    out["oldest_pending_patches"].sort(key=lambda x: x["age_hours"], reverse=True)
    out["oldest_pending_patches"] = out["oldest_pending_patches"][:3]

    return out


# ═════════════════════════════════════════════════════════════════════════════
# STAGE 2  -  (no token scan on the Perps lane)
# ═════════════════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════════════════
# STAGE 3 - JUDGMENT (xAI grok-4.7 primary, local Ollama fallback, both optional)
# ═════════════════════════════════════════════════════════════════════════════

OVERSEER_SYSTEM_PROMPT = """You are The Overseer  -  the agent's nightly mirror for the Perps ecosystem. The judge may share a model lineage with the agent. Do NOT let that make you soft: you know exactly where an operator like this cuts corners, rationalizes a bad entry, and dresses up inactivity as discipline. Be harder because you understand him, not easier.

The agent runs Jupiter perpetuals (SOL / ETH / BTC) through perps.py. Every chain packet carries the bot fields plus feed lanes: Flow (call, live_call, cycle, signals, risk, calibration), live odds (Kalshi hourly + end-of-day ladders, Polymarket price markets + movers), news, flags and per-lane staleness. He decides open / add / hold / close / stand-down and writes the per-chain json_relay/perp_<chain>_signal.json. Leverage vocabulary is exact: collateral (what he can lose), leverage (10x up to PERP_MAX_LEVERAGE, 70x default), notional (collateral x leverage), liquidation price. His operator is the owner.

YOUR JOB
Read 24 hours of the agent's Perps activity below and write ONE direct, honest report. Not corporate fluff. Lowercase, direct, low hedge language, high specificity.

INTENT vs ACTION
The best judgment is not a recital. It is "you SAID X, you DID Y, here's the gap." Weight these:
- DAY TYPE (classify before you judge): `disciplined_sitout` (no trades and the lanes justified it: flags, stale lanes, Flow vs odds disagreement, abort conditions  -  praise it), `lazy_sitout` (sat out while his own lanes lined up), `active`, `mixed`. Zero trades is NOT a failure by default.
- FLAGS AND STALENESS: did he size down, cut leverage or pass when the packet flagged a stale lane or a disagreement? Did he pull fresh data (json_relay/feed_request.json) before a call that depended on a stale lane?
- LEVERAGE DISCIPLINE: collateral vs notional vs liq distance. Did he lever up into a flagged setup? Did a stop sit on the wrong side or past liquidation?
- EXITS: were tp/sl side-aware and sane? Did he manage the open book (exit suggestions, correlation across chains) or let it drift?
- REVERSE SIZE: bigger on losers than winners is the defining failure. Cite the ratio when present.

WHAT GOOD LOOKS LIKE
- Cite timestamps, markets, sizes (collateral + leverage + notional), decisions
- Diff the morning thesis and standing rules vs executed behavior
- Spot temporal chains (a loss, then a revenge entry minutes later)
- Honor what was strong without inflating it
- If the data is sparse (no thesis, unreadable positions), make that the headline

WHAT TO AVOID
- Generic platitudes, repeating data back, inventing specifics not in the data
- Being soft because the day was rough or because you share his lineage
- Proposing trades, size changes or halts: this is analysis only

OUTPUT FORMAT
Return ONE valid JSON object. No markdown fences, no preamble. Start with `{` and end with `}`.

{
  "headline": "<one sentence verdict, lowercase, direct>",
  "score_0_to_10": <integer 0-10>,
  "day_type": "<active | disciplined_sitout | lazy_sitout | mixed>",
  "what_was_strong": ["<specific>", "..."],
  "what_was_weak": ["<specific>", "..."],
  "patterns_spotted": ["<pattern across decisions>", "..."],
  "rule_breaks": ["<quote the rule + cite the moment>", "..."],
  "missed_opportunities": ["<window / market>", "..."],
  "time_of_day_observations": ["<by morning/midday/afternoon/evening/late>", "..."],
  "operator_drift": ["<committed vs executed; lead with the reverse-size ratio when present>", "..."],
  "improvements_for_tomorrow": ["<concrete action>", "..."],
  "follow_ups_for_agent": [{"priority": "high|medium|low", "action": "<thing to do this cycle>"}]
}

Lists may be empty arrays. Don't invent. Be specific. Be honest. Be useful.
When the reverse-size signature reads UNKNOWN, say UNKNOWN explicitly - never silently skip it.
"""


def _collect_over_engagement_signatures(inputs: dict) -> dict:
    """Reverse-size signature on the perps lane, with RECEIPTS. Returns real ratios only
    when valid data is found; otherwise status=UNKNOWN with a reason (absence is signal).
    Analysis-only: reads, never writes."""
    _SIZE_FIELDS = ("collateral_usd", "size_usd", "notional_usd", "collateral_sol", "size")
    _PNL_FIELDS  = ("pnl_usd", "pnl", "realized_pnl", "net_pnl", "pnl_pct")

    def _num(d, fields):
        if not isinstance(d, dict):
            return None
        for f in fields:
            if d.get(f) is not None:
                try:
                    return float(d.get(f))
                except Exception:
                    continue
        return None

    def _won(d):
        for f in ("outcome", "result", "win", "won"):
            v = d.get(f) if isinstance(d, dict) else None
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                lv = v.lower()
                if lv in ("win", "won", "profit", "tp"):
                    return True
                if lv in ("loss", "lost", "stop", "sl", "liquidated"):
                    return False
        p = _num(d, _PNL_FIELDS)
        return (p > 0) if p is not None else None

    p = inputs.get("perps", {}) or {}
    rows = [r for r in (p.get("episodes_sizing") or []) if isinstance(r, dict)]
    if not rows:
        pnl_log = p.get("pnl_log")
        if isinstance(pnl_log, dict):
            pnl_log = pnl_log.get("trades") or pnl_log.get("closed") or pnl_log.get("history") or []
        rows = [r for r in (pnl_log or []) if isinstance(r, dict)]
    wins, losses = [], []
    for r in rows:
        sz, w = _num(r, _SIZE_FIELDS), _won(r)
        if sz is None or w is None:
            continue
        (wins if w else losses).append(sz)
    if len(wins) < 2 or len(losses) < 2:
        rs = {"status": "UNKNOWN",
              "reason": "need >=2 win AND >=2 loss with size+outcome; got %dW/%dL" % (len(wins), len(losses))}
    else:
        wa, la = sum(wins) / len(wins), sum(losses) / len(losses)
        ratio = round(la / wa, 2) if wa > 0 else None
        rs = {"winner_avg_size": round(wa, 4), "loser_avg_size": round(la, 4),
              "n_winners": len(wins), "n_losers": len(losses),
              "loser_over_winner_ratio": ratio, "flag": bool(ratio and ratio > 1.0),
              "roast_hint": ("sized %sx bigger on losers than winners" % ratio)
                            if ratio and ratio > 1.0 else "no reverse-size"}
    return {"reverse_size": rs}


def build_user_prompt(inputs: dict, top_tokens: dict = None) -> str:
    """Compose the user message with the 24h Perps data structured for the overseer judge."""

    def fmt(obj, max_chars=3000):
        try:
            s = json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj
        except Exception:
            s = str(obj)
        if len(s) > max_chars:
            s = s[:max_chars] + f"\n…[truncated, was {len(s)} chars]"
        return s

    today = datetime.now(EST).strftime("%Y-%m-%d (%A)")
    prev = inputs.get("yesterdays_overseer_report")
    if prev:
        prev_summary = json.dumps({
            "headline":         prev.get("headline"),
            "score":            prev.get("score_0_to_10"),
            "follow_ups_given": prev.get("follow_ups_for_agent"),
            "actions_taken":    prev.get("actions_taken"),
        }, indent=2, default=str)
    else:
        prev_summary = "(no prior overseer report)"
    rs = (inputs.get("over_engagement_signatures") or {}).get("reverse_size") or {}
    if rs.get("status") == "UNKNOWN":
        rs_line = f"- reverse_size perp: UNKNOWN - {rs.get('reason', '')}"
    else:
        rs_line = f"- reverse_size perp: {json.dumps({k: v for k, v in rs.items() if k != 'roast_hint'}, default=str)} | {rs.get('roast_hint', '')}"
    p = inputs.get("perps") or {}

    parts = [
        f"=== AGENT'S LAST 24H ON PERPS  -  {today} ===",
        "",
        "==== REVERSE-SIZE SIGNATURE (ratios first) ====",
        rs_line,
        "",
        "==== STATED INTENT ====",
        "MORNING THESIS:",
        fmt(inputs.get("thesis") or "(NO THESIS LOGGED  -  flag this as the headline)", 2500),
        "",
        "NEWS (news_catalysts.json from the feeds):",
        fmt(inputs.get("news_catalysts") or {}, 1500),
        "",
        "==== WALLET / BOOK ====",
        "Wallet (SOL, perps collateral source):",
        fmt(inputs.get("wallet_balance"), 600),
        "",
        "Live book (perp_positions.json PLURAL):",
        fmt({k: p.get(k) for k in ("_position_source", "open_position_count", "open_markets",
                                    "open_collateral_usd", "open_notional_usd", "position_open")}, 2000),
        "",
        "Exit suggestion currently in inbox:",
        fmt(p.get("exit_suggestion"), 1000),
        "",
        "==== DECISIONS (perp_decisions.json, last 24h) ====",
        fmt(p.get("decisions_24h") or [], 5000),
        "",
        "==== CLOSES (perp_episodes.jsonl, last 24h) ====",
        fmt(p.get("episodes_24h") or [], 4000),
        "",
        "PnL log (perp_pnl.json):",
        fmt(p.get("pnl_log"), 1500),
        "",
        "Autopsy lessons + missed-trade autopsy:",
        fmt({"lessons": p.get("autopsy_lessons"), "missed": p.get("missed_autopsy")}, 2000),
        "",
        "Last signal written (json_relay/perp_signal.json):",
        fmt(p.get("last_signal"), 800),
        "",
        "Param changes in 24h (perps_param_changes.jsonl):",
        fmt(p.get("param_changes_24h") or [], 1500),
        "",
        "==== FEED LANES (age of each feed output; FEED_CADENCE=manual means pulls only on request) ====",
        fmt(inputs.get("feeds"), 1200),
        "",
        "==== AGENT'S DECISIONS (from inbox_results/done) ====",
        f"Inbox packets handled: {len(inputs.get('inbox_handled') or [])}",
        f"Decisions logged with reasoning: {len(inputs.get('inbox_decisions') or [])}",
        fmt(inputs.get("inbox_decisions") or [], 4000),
        "",
        "==== TIME-OF-DAY DISTRIBUTION (ET buckets) ====",
        fmt(inputs.get("time_buckets"), 800),
        "",
        "==== BOT HEALTH (perps.py + sub-bots) ====",
        fmt(inputs.get("bot_health"), 1500),
        "",
        "==== BLIND SPOTS (files that did not exist: values are DEFAULTS, not observations) ====",
        fmt(list(_BLIND_SPOTS), 800),
        "",
        "==== LATEST LEARNINGS (last 4KB of LEARNINGS.md) ====",
        (inputs.get("learnings_appended") or "(empty)")[-3000:],
        "",
        "==== YESTERDAY'S OVERSEER REPORT (continuity) ====",
        prev_summary[:2000],
        "",
        "==== SKILL EVOLUTION + CAPABILITY GAP ROUTING ====",
        "(Judge: amendments backed by evidence, HARD LAW respected, honest risk tiers, clean gap classification.)",
        fmt({
            "skill_evolution_summary": inputs.get("skill_evolution_summary", {}),
            "last_3day_packet_ts":     inputs.get("last_3day_packet_ts"),
            "meeting_watcher_gaps":    inputs.get("meeting_watcher_gaps", 0),
            "patch_landed_acks":       inputs.get("patch_landed_acks", []),
        }, 3000),
        "",
        "==== CODE_TUNER OBSERVATION SURFACE + MANUAL-PATCH DISCIPLINE ====",
        "(code_tuner observes, the agent authors patches into owner_inbox with a risk note + test plan, the owner applies. A source change with no patch trail is a cowboy edit.)",
        fmt({
            "code_tuner_setup_state":            inputs.get("code_tuner_setup_state", {}),
            "metrics_current_snapshot":          inputs.get("metrics_current_snapshot", {}),
            "source_delta_count_24h":            inputs.get("source_delta_count_24h", 0),
            "code_tuner_setup_activity_24h":     inputs.get("code_tuner_setup_activity_24h", {}),
            "pending_patch_packets_in_owner_inbox": inputs.get("pending_patch_packets_in_owner_inbox", 0),
            "oldest_pending_patches":            inputs.get("oldest_pending_patches", []),
        }, 3000),
        "",
        "=== END DATA ===",
        "",
        "Now return your overseer verdict as ONE JSON object. Start with `{` and end with `}`.",
    ]
    prompt = "\n".join(parts)
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n[...truncated to fit context]"
    return prompt


# ─── xAI grok-4.7 caller (ollama fallback is separate) ───────────────────────────────────────────────────────

async def call_grok(system_prompt: str, user_prompt: str) -> dict:
    """Returns {ok: bool, verdict: dict|None, source: str, error: str|None, elapsed_s: float}

    xAI Grok via OpenAI-compatible chat/completions. System goes in the messages
    array as a role=system entry (NOT a top-level field like Anthropic). Response
    text is choices[0].message.content. Usage is prompt_tokens/completion_tokens.
    """
    if not GROK_API_KEY:
        return {"ok": False, "verdict": None, "source": "grok", "error": "no_api_key", "elapsed_s": 0}
    
    t0 = datetime.now()
    log(f"  [Grok] calling {XAI_MODEL}…")
    try:
        async with httpx.AsyncClient(timeout=GROK_TIMEOUT) as c:
            r = await c.post(
                XAI_API_URL,
                headers={
                    "Authorization": f"Bearer {GROK_API_KEY}",
                    "content-type":  "application/json",
                },
                json={
                    "model":       XAI_MODEL,
                    "max_tokens":  GROK_MAX_TOKENS,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                },
            )
        elapsed = (datetime.now() - t0).total_seconds()
        
        if r.status_code != 200:
            err_body = r.text[:500]
            log(f"  [Grok] ❌ http {r.status_code}: {err_body}")
            return {"ok": False, "verdict": None, "source": "grok", "error": f"http_{r.status_code}: {err_body[:200]}", "elapsed_s": elapsed}
        
        data = r.json()
        # Extract text content  -  OpenAI-compatible shape
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        
        if not text.strip():
            return {"ok": False, "verdict": None, "source": "grok", "error": "empty_response", "elapsed_s": elapsed}
        
        # Parse JSON  -  strip any markdown fences if present
        verdict = _parse_verdict_json(text)
        if not verdict:
            log(f"  [Grok] ❌ JSON parse failed. Raw: {text[:300]}")
            return {"ok": False, "verdict": None, "source": "grok", "error": "json_parse_failed", "elapsed_s": elapsed}
        
        log(f"  [Grok] ✅ {elapsed:.1f}s  -  score={verdict.get('score_0_to_10', '?')}")
        usage = data.get("usage", {})
        verdict["_meta"] = {
            "source":        "xai",
            "model":         XAI_MODEL,
            "elapsed_s":     round(elapsed, 1),
            "input_tokens":  usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        }
        return {"ok": True, "verdict": verdict, "source": "grok", "error": None, "elapsed_s": elapsed}

    except httpx.TimeoutException:
        elapsed = (datetime.now() - t0).total_seconds()
        log(f"  [Grok] ❌ timeout after {elapsed:.1f}s")
        return {"ok": False, "verdict": None, "source": "grok", "error": "timeout", "elapsed_s": elapsed}
    except Exception as e:
        elapsed = (datetime.now() - t0).total_seconds()
        log(f"  [Grok] ❌ exception: {e}")
        return {"ok": False, "verdict": None, "source": "grok", "error": str(e)[:300], "elapsed_s": elapsed}


# ─── Ollama fallback caller ──────────────────────────────────────────────────

async def call_ollama_fallback(system_prompt: str, user_prompt: str) -> dict:
    """Returns same shape as call_grok. Used only when Grok fails."""
    log(f"  [Ollama-Fallback] calling {OLLAMA_FALLBACK_MODEL}…")
    t0 = datetime.now()
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as c:
            r = await c.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model":  OLLAMA_FALLBACK_MODEL,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "options": {
                        "temperature": 0.5,
                        "num_predict": 2000,
                    },
                    "keep_alive": 0,
                    "format": "json",
                },
            )
        elapsed = (datetime.now() - t0).total_seconds()
        if r.status_code != 200:
            log(f"  [Ollama-Fallback] ❌ http {r.status_code}")
            return {"ok": False, "verdict": None, "source": "ollama_fallback", "error": f"http_{r.status_code}", "elapsed_s": elapsed}
        data = r.json()
        content = (data.get("message") or {}).get("content", "")
        verdict = _parse_verdict_json(content)
        if not verdict:
            return {"ok": False, "verdict": None, "source": "ollama_fallback", "error": "json_parse_failed", "elapsed_s": elapsed}
        verdict["_meta"] = {
            "source":    "ollama_fallback",
            "model":     OLLAMA_FALLBACK_MODEL,
            "elapsed_s": round(elapsed, 1),
        }
        log(f"  [Ollama-Fallback] ✅ {elapsed:.1f}s  -  score={verdict.get('score_0_to_10', '?')}")
        return {"ok": True, "verdict": verdict, "source": "ollama_fallback", "error": None, "elapsed_s": elapsed}
    except httpx.TimeoutException:
        elapsed = (datetime.now() - t0).total_seconds()
        log(f"  [Ollama-Fallback] timeout after {elapsed:.1f}s")
        return {"ok": False, "verdict": None, "source": "ollama_fallback", "error": "timeout", "elapsed_s": elapsed}
    except Exception as e:
        elapsed = (datetime.now() - t0).total_seconds()
        log(f"  [Ollama-Fallback] {e}")
        return {"ok": False, "verdict": None, "source": "ollama_fallback", "error": (str(e) or type(e).__name__)[:300], "elapsed_s": elapsed}


def _parse_verdict_json(text: str) -> dict:
    """Robust JSON parse  -  handles markdown fences, leading/trailing prose."""
    if not text:
        return None
    
    # First try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # Try stripping markdown fences
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    
    try:
        return json.loads(stripped.strip())
    except json.JSONDecodeError:
        pass
    
    # Last resort: extract first JSON object from anywhere in text
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    
    return None


async def run_judgment(inputs: dict, top_tokens: dict) -> dict:
    """Run Grok primary, fall back to Ollama if needed.
    
    Returns {verdict: dict, meta: dict} where meta describes which path was taken.
    """
    log("STAGE 3: judgment (Grok primary, Ollama fallback)…")
    
    inputs["over_engagement_signatures"] = _collect_over_engagement_signatures(inputs)
    user_prompt = build_user_prompt(inputs, top_tokens)
    log(f"  user prompt: {len(user_prompt)} chars (~{len(user_prompt)//4} tokens)")
    
    _order = _si_gate.order()  # LLM_MAIN / LLM_FALLBACK; both off = rule-based stub verdict
    _skip = {"ok": False, "verdict": None, "error": "si_off", "elapsed_s": 0}
    # Primary: Grok
    grok_result = (await call_grok(OVERSEER_SYSTEM_PROMPT, user_prompt)) if "grok" in _order else dict(_skip, source="grok")
    if grok_result["ok"] and grok_result["verdict"]:
        return {
            "verdict": grok_result["verdict"],
            "meta": {
                "judge":           "xai",
                "model":           XAI_MODEL,
                "elapsed_s":       grok_result["elapsed_s"],
                "fallback_used":   False,
                "grok_error":      None,
            },
        }
    
    # Fallback: Ollama
    if "ollama" in _order:
        if "grok" in _order:
            log(f"  Grok failed ({grok_result['error']})  -  falling back to Ollama")
            await notify(f"⚠️ Overseer Grok API failed ({grok_result['error']})  -  falling back to local Ollama brain")
        ollama_result = await call_ollama_fallback(OVERSEER_SYSTEM_PROMPT, user_prompt)
    else:
        ollama_result = dict(_skip, source="ollama")
    if ollama_result["ok"] and ollama_result["verdict"]:
        return {
            "verdict": ollama_result["verdict"],
            "meta": {
                "judge":         "ollama_fallback",
                "model":         OLLAMA_FALLBACK_MODEL,
                "elapsed_s":     ollama_result["elapsed_s"],
                "fallback_used": True,
                "grok_error":    grok_result["error"],
            },
        }
    
    # Both failed  -  return a stub verdict so we still write SOMETHING to inbox
    log(f"  ❌ both judges failed: grok={grok_result['error']}, ollama={ollama_result['error']}")
    return {
        "verdict": {
            "headline":     "OVERSEER FAILED  -  both Grok and Ollama errored, no judgment available tonight",
            "score_0_to_10": 0,
            "what_was_strong":  [],
            "what_was_weak":    [],
            "patterns_spotted": [],
            "rule_breaks":      [],
            "missed_opportunities": [],
            "time_of_day_observations": [],
            "operator_drift": [],
            "improvements_for_tomorrow": ["fix Overseer infrastructure  -  both judges are down"],
            "follow_ups_for_agent": [
                {"priority": "high", "action": f"check GROK_API_KEY in .env (Grok failed: {grok_result['error']})"},
                {"priority": "high", "action": f"check Ollama is running with {OLLAMA_FALLBACK_MODEL} loaded (Ollama failed: {ollama_result['error']})"},
            ],
        },
        "meta": {
            "judge":         "none",
            "model":         "none",
            "elapsed_s":     0,
            "fallback_used": True,
            "grok_error":    grok_result["error"],
            "ollama_error":  ollama_result["error"],
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
# STAGE 4  -  DAILY SUMMARY WRITER (NEW v4)
# Writes a compact daily summary so future overseer runs have an information
# pyramid: today's raw data + N days of summaries.
# ═════════════════════════════════════════════════════════════════════════════

def write_daily_summary(inputs: dict, verdict: dict, top_tokens: dict, judge_meta: dict) -> str:
    """Compact summary of yesterday for future overseer runs to read."""
    log("STAGE 4: writing daily summary…")
    os.makedirs(DAILY_SUM, exist_ok=True)
    date_str = (datetime.now(EST) - timedelta(hours=2)).strftime("%Y-%m-%d")
    # We're running at 2am, so the data is for "yesterday"  -  subtract a few hours to be safe
    
    perps = inputs.get("perps") or {}
    perp_pnl = 0.0
    perp_wins = perp_losses = 0
    for e in perps.get("episodes_24h") or []:
        try:
            pnl = float(e.get("pnl_usd") or e.get("pnl") or 0)
            perp_pnl += pnl
            if pnl > 0:
                perp_wins += 1
            elif pnl < 0:
                perp_losses += 1
        except Exception:
            continue

    summary = {
        "date":     date_str,
        "written_at": now_iso(),
        "judge":    judge_meta,

        # What this overseer COULD NOT SEE this pass. Empty list = every input
        # file existed. A non-empty list means some value below is a DEFAULT
        # rather than an observation  -  read those sections as UNKNOWN, not as
        # zero. This exists because the overseer once reported a flat book while
        # $100 at 10x was open: the positions file was misnamed, the read
        # returned None, and nothing anywhere said "I couldn't look."
        "blind_spots": list(_BLIND_SPOTS),
        "blind_spot_count": len(_BLIND_SPOTS),
        
        "thesis_present": bool(inputs.get("thesis")),
        "thesis_summary": (inputs.get("thesis") or {}).get("market_read", "")[:200] if inputs.get("thesis") else None,
        
        "perps": {
            "open_positions":   perps.get("open_position_count", 0),
            "open_markets":     perps.get("open_markets") or [],
            "open_collateral_usd": perps.get("open_collateral_usd"),
            "open_notional_usd":   perps.get("open_notional_usd"),
            "decisions":        len(perps.get("decisions_24h") or []),
            "closes":           len(perps.get("episodes_24h") or []),
            "wins":             perp_wins,
            "losses":           perp_losses,
            "pnl_usd_estimate": round(perp_pnl, 2),
        },

        "bot_health":  inputs.get("bot_health"),
        "time_buckets": inputs.get("time_buckets"),
        
        "overseer_score":    verdict.get("score_0_to_10"),
        "overseer_headline": verdict.get("headline"),
        "rule_breaks":       verdict.get("rule_breaks") or [],
        "patterns_spotted":  verdict.get("patterns_spotted") or [],
        "operator_drift":    verdict.get("operator_drift") or [],
        "improvements":      verdict.get("improvements_for_tomorrow") or [],

    }
    
    path = os.path.join(DAILY_SUM, f"{date_str}.json")
    save_json_atomic(path, summary)
    log(f"  ✅ wrote {path}")
    return path


# ═════════════════════════════════════════════════════════════════════════════
# STAGE 5  -  INBOX PACKET WRITER + HISTORY
# ═════════════════════════════════════════════════════════════════════════════

def write_inbox_packet(verdict: dict, judge_meta: dict, inputs: dict, top_tokens: dict, daily_summary_path: str) -> str:
    """Single P2 packet for the agent's morning inbox."""
    log("STAGE 5: writing inbox packet…")
    os.makedirs(INBOX, exist_ok=True)
    
    today_str = datetime.now(EST).strftime("%Y-%m-%d")
    packet_id = f"agent_autoagent - overseer_{today_str}"
    
    packet = {
        "id":             packet_id,
        "type":           "overseer_report",
        "priority":       "P2",
        "ts":             now_iso(),
        "from":           "agent_autoagent_v4.3.1",
        "lookback_hours": LOOKBACK_HOURS,
        
        "judge": {
            "source":         judge_meta.get("judge"),
            "model":          judge_meta.get("model"),
            "elapsed_s":      judge_meta.get("elapsed_s"),
            "fallback_used":  judge_meta.get("fallback_used"),
            "grok_error":     judge_meta.get("grok_error"),
            "ollama_error":   judge_meta.get("ollama_error"),
        },
        
        "context": {
            "date":                  today_str,
            "thesis_present":        bool(inputs.get("thesis")),
            "inbox_packets_handled": len(inputs.get("inbox_handled") or []),
            "inbox_decisions":       len(inputs.get("inbox_decisions") or []),
            "autopsies_handled":     len(inputs.get("autopsies") or []),
            "perp_open_positions":   (inputs.get("perps") or {}).get("open_position_count", 0),
            "perp_closes_24h":       len((inputs.get("perps") or {}).get("episodes_24h") or []),
            "bot_health":            inputs.get("bot_health") or {},
            "time_buckets":          inputs.get("time_buckets") or {},
        },
        
        # The roast
        "headline":              verdict.get("headline"),
        "score_0_to_10":         verdict.get("score_0_to_10"),
        "day_type":              verdict.get("day_type"),
        "what_was_strong":       verdict.get("what_was_strong") or [],
        "what_was_weak":         verdict.get("what_was_weak") or [],
        "patterns_spotted":      verdict.get("patterns_spotted") or [],
        "rule_breaks":           verdict.get("rule_breaks") or [],
        "missed_opportunities":  verdict.get("missed_opportunities") or [],
        "time_of_day_observations": verdict.get("time_of_day_observations") or [],
        "operator_drift":        verdict.get("operator_drift") or [],
        "improvements_for_tomorrow": verdict.get("improvements_for_tomorrow") or [],
        "follow_ups_for_agent": verdict.get("follow_ups_for_agent") or [],
        
        # Pointer to where the daily summary was written (so future runs can read it)
        "daily_summary_path": daily_summary_path,
        
        # The agent populates this when he processes the report
        "actions_taken": [],
    }
    
    path = os.path.join(INBOX, packet_id + ".json")
    save_json_atomic(path, packet)
    log(f"  ✅ wrote {path}")
    return path


def append_history(packet_path: str, verdict: dict, judge_meta: dict):
    """Append today's report metadata to overseer_history.json."""
    history = load_json(OVERSEER_HIST, []) or []
    if not isinstance(history, list):
        history = []
    history.append({
        "ts":            now_iso(),
        "date":          datetime.now(EST).strftime("%Y-%m-%d"),
        "inbox_path":    packet_path,
        "headline":      verdict.get("headline"),
        "score":         verdict.get("score_0_to_10"),
        "judge":         judge_meta.get("judge"),
        "model":         judge_meta.get("model"),
        "fallback_used": judge_meta.get("fallback_used"),
    })
    history = history[-365:]
    save_json_atomic(OVERSEER_HIST, history)


def _roast_signature_lines(verdict: dict) -> list:
    """The lines from a verdict that represent DURABLE failure patterns worth
    tracking across nights (not one-off day color). We watch weak points, patterns,
    rule breaks, and operator drift  -  the stuff that, if it repeats, is a real
    problem, not a bad day."""
    out = []
    for field in ("what_was_weak", "patterns_spotted", "rule_breaks", "operator_drift"):
        for item in (verdict.get(field) or []):
            if isinstance(item, str) and item.strip():
                out.append((field, item.strip()))
    return out


def _norm_for_match(s: str) -> set:
    """Loose normalization → a SET of stemmed meaningful words, so 'revenge-bought at
    2:14pm' and 'revenge buying in the afternoon' are recognized as the same recurring
    pattern. The roast rephrases every night, so we match on shared concept words with
    light stemming (drop -ing/-ed/-s), not exact wording."""
    import re
    words = re.findall(r"[a-z]{4,}", (s or "").lower())
    stop = {"that", "this", "with", "from", "your", "when", "then", "into", "have",
            "just", "still", "which", "while", "them", "they", "were", "been",
            "agent", "today", "night", "day", "after", "right", "kept", "took",
            "entry", "another", "again", "about", "there", "here", "what", "some"}
    out = set()
    for w in words:
        if w in stop:
            continue
        w = re.sub(r"(ing|ed)$", "", w)
        w = re.sub(r"s$", "", w)
        if len(w) >= 3:
            out.add(w)
    return out


def maybe_append_recurring_to_learnings(verdict: dict, inputs: dict) -> dict:
    """The COMPOUNDING layer (The owner): a roast that just lands in the inbox evaporates.
    So when tonight's roast surfaces a failure pattern that ALSO appeared in a recent
    prior report, that's a REPEAT  -  not a bad day, a standing problem  -  and it gets
    appended to LEARNINGS.md (the authoritative memory) with an escalating
    recurrence marker. One-offs stay in the packet only; repeats compound.

    Returns {appended: [...], escalations: {...}} for the packet to surface.
    """
    result = {"appended": [], "recurrence": {}}
    try:
        today_sig = _roast_signature_lines(verdict)
        if not today_sig:
            return result

        # gather the last ~5 nights of roast lines from overseer_history's linked packets
        history = load_json(OVERSEER_HIST, []) or []
        prior_lines = []
        for h in history[-6:-1]:   # exclude tonight (not written yet)
            rep = load_json(h.get("inbox_path", ""), None)
            if not rep:
                continue
            for field in ("what_was_weak", "patterns_spotted", "rule_breaks", "operator_drift"):
                for item in (rep.get(field) or []):
                    if isinstance(item, str) and item.strip():
                        prior_lines.append(item.strip())
        # also fold in yesterday's report if collected separately
        yrep = inputs.get("yesterdays_overseer_report")
        if isinstance(yrep, dict):
            for field in ("what_was_weak", "patterns_spotted", "rule_breaks", "operator_drift"):
                for item in (yrep.get(field) or []):
                    if isinstance(item, str) and item.strip():
                        prior_lines.append(item.strip())

        prior_norm = [(_norm_for_match(p), p) for p in prior_lines]

        appended_lines = []
        for field, line in today_sig:
            tset = _norm_for_match(line)
            if len(tset) < 2:
                continue
            # count how many prior nights had a strongly-overlapping line
            hits = 0
            for pset, praw in prior_norm:
                if len(pset) < 2:
                    continue
                shared = tset & pset
                if not shared:
                    continue
                overlap = len(shared) / min(len(tset), len(pset))
                # same pattern rephrased: ≥34% concept overlap AND ≥2 shared words
                if overlap >= 0.34 and len(shared) >= 2:
                    hits += 1
            if hits >= 1:
                # RECURRING  -  this failure has shown up before. Compound it.
                occ = hits + 1
                result["recurrence"][line] = occ
                appended_lines.append((field, line, occ))

        if appended_lines:
            today = datetime.now(EST).strftime("%Y-%m-%d")
            block = [f"\n### [overseer recurring pattern] {today}"]
            for field, line, occ in appended_lines:
                block.append(
                    f"- [learning candidate] RECURRING ({occ}x): {line} "
                    f" -  flagged by the nightly overseer across {occ} recent reports; "
                    f"this is a standing pattern, not a one-off. (source field: {field})"
                )
            try:
                # learnings_writer puts the block INSIDE Section 8 (between the append-window
                # markers) so the nightly self-eval sees it; a raw append would land below the
                # end marker. Fallback to the raw append only if the writer is missing.
                try:
                    import sys as _lw_sys
                    _lw_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                    from learnings_writer import append_to_learnings as _lw_append
                except Exception:
                    _lw_append = None
                body = "\n".join(block[1:])
                if _lw_append is None or not _lw_append(body, "overseer_followup", "overseer"):
                    with open(LEARNINGS, "a", encoding="utf-8") as f:
                        f.write("\n".join(block) + "\n")
                result["appended"] = [l for _, l, _ in appended_lines]
                log(f"  📝 appended {len(appended_lines)} RECURRING pattern(s) to LEARNINGS.md")
            except Exception as e:
                log(f"  ⚠ LEARNINGS append failed: {e}")
    except Exception as e:
        log(f"  ⚠ recurrence check failed: {e}")
    return result


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

async def run_overseer():
    """Execute the full overseer cycle once."""
    t_start = datetime.now()
    log("═══════════════════════════════════════════════════════")
    log("THE OVERSEER - nightly run (xAI grok-4.7, ollama fallback)")
    log(f"local time: {now_str()}")
    log(f"primary judge: Grok API ({XAI_MODEL})")
    log(f"fallback judge: Ollama ({OLLAMA_FALLBACK_MODEL})")
    log("═══════════════════════════════════════════════════════")
    
    # Stage 1: collect inputs
    inputs = collect_inputs()
    
    # Stage 2: top-10 token analysis (does its own logging)
    top_tokens = {}  # no token scan on the Perps lane
    
    # Stage 3: judgment (xAI grok-4.7 primary, ollama fallback)
    judgment = await run_judgment(inputs, top_tokens)
    verdict = judgment["verdict"]
    judge_meta = judgment["meta"]
    
    # Stage 4: daily summary (always write, even if judges failed)
    daily_summary_path = write_daily_summary(inputs, verdict, top_tokens, judge_meta)
    
    # Stage 5: inbox packet + history
    packet_path = write_inbox_packet(verdict, judge_meta, inputs, top_tokens, daily_summary_path)
    append_history(packet_path, verdict, judge_meta)

    # Stage 5b: the COMPOUNDING layer  -  recurring failure patterns append to
    # LEARNINGS.md so the roast doesn't just evaporate in the inbox. One-offs stay
    # in the packet; repeats (flagged across recent nights) become durable lessons.
    recurrence = maybe_append_recurring_to_learnings(verdict, inputs)
    if recurrence.get("appended"):
        log(f"  compounded {len(recurrence['appended'])} recurring pattern(s) into LEARNINGS")
        # patch the just-written packet so the agent SEES the recurrence in his morning read
        try:
            _pkt = load_json(packet_path, None)
            if _pkt is not None:
                _pkt["recurring_patterns"] = recurrence.get("recurrence", {})
                _pkt["recurring_appended_to_learnings"] = recurrence.get("appended", [])
                save_json_atomic(packet_path, _pkt)
        except Exception as _re:
            log(f"  ⚠ couldn't patch packet with recurrence: {_re}")
    
    elapsed = (datetime.now() - t_start).total_seconds()
    log(f"═══════════════════════════════════════════════════════")
    log(f"complete in {elapsed:.1f}s | judge={judge_meta.get('judge')} | "
        f"score={verdict.get('score_0_to_10')}/10")
    log("═══════════════════════════════════════════════════════")
    
    # Telegram alert
    score_str = f"{verdict.get('score_0_to_10')}/10" if verdict.get("score_0_to_10") is not None else "n/a"
    headline_short = (verdict.get("headline") or "")[:140]
    judge_label = "Grok" if judge_meta.get("judge") == "xai" else (
        "Ollama (fallback)" if judge_meta.get("judge") == "ollama_fallback" else "FAILED"
    )
    await notify(
        f"🪞 <b>Overseer report ready</b>\n"
        f"Judge: {judge_label} | Score: {score_str}\n"
        f"\n<i>{headline_short}</i>\n"
        f"\nRead in inbox: {os.path.basename(packet_path)}"
    )
    
    return packet_path


async def preflight_checks():
    """Verify environment before entering main loop. Logs warnings, doesn't fail."""
    if not _si_gate.enabled():
        log("SI brain off (LLM_MAIN/LLM_FALLBACK=off): overseer writes a rule-based stub, no SI calls")
        return
    if not GROK_API_KEY and _si_gate.allows("grok"):
        log("⚠️ GROK_API_KEY not set in .env  -  Grok path will fail, fallback only mode")
        await notify("⚠️ Overseer starting WITHOUT GROK_API_KEY  -  will run on Ollama fallback only")
    
    # Pre-flight: check Ollama is at least available for fallback
    if not _si_gate.allows("ollama"):
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code == 200:
                tags = r.json()
                models = [m.get("name") for m in tags.get("models", [])]
                if OLLAMA_FALLBACK_MODEL not in models:
                    log(f"⚠️ fallback model {OLLAMA_FALLBACK_MODEL} not present in Ollama  -  fallback will fail")
                else:
                    log(f"✅ Ollama fallback model {OLLAMA_FALLBACK_MODEL} present")
            else:
                log(f"⚠️ Ollama returned {r.status_code}  -  fallback may not work")
    except Exception as e:
        log(f"⚠️ Ollama unreachable at {OLLAMA_URL}: {e}  -  fallback will fail if Grok does")


async def fire_one_run(reason: str = "scheduled"):
    """Wrap run_overseer with state persistence + telemetry.
    Errors caught here don't crash the daemon  -  they're logged + persisted + telegram'd."""
    log(f"firing overseer cycle (reason: {reason})")
    write_heartbeat("running", {"trigger": reason})
    
    try:
        packet_path = await run_overseer()
        _record_run_completion(success=True, packet_path=packet_path)
        write_heartbeat("idle_after_run_ok", {"last_packet": os.path.basename(packet_path)})
        return True
    except KeyboardInterrupt:
        # Bubble up  -  daemon should exit clean
        raise
    except Exception as e:
        import traceback
        err_str = str(e)[:300]
        tb = traceback.format_exc()
        log(f"❌ run errored: {err_str}")
        log(tb)
        _record_run_completion(success=False, error=err_str)
        write_heartbeat("idle_after_run_err", {"last_error": err_str})
        try:
            await notify(f"❌ Overseer run errored: {err_str}\n\nWill retry at next scheduled time. Daemon stays alive.")
        except Exception:
            pass
        return False


async def daemon_loop():
    """Forever-loop: sleep until 2am EST, fire run, write heartbeats throughout."""
    log("entering daemon loop  -  runs nightly at 2:00 AM EST")
    log(f"next scheduled run: {_next_scheduled_dt().isoformat()}")
    write_heartbeat("starting")
    
    # Telegram once on boot so user knows daemon is up
    try:
        await notify(
            f"🪞 <b>Overseer daemon started</b>\n"
            f"Schedule: {SCHEDULED_HOUR_EST:02d}:{SCHEDULED_MINUTE:02d} EST nightly\n"
            f"Next run: {_next_scheduled_dt().strftime('%Y-%m-%d %H:%M EST')}\n"
            f"Primary: Grok API ({XAI_MODEL})\n"
            f"Fallback: Ollama ({OLLAMA_FALLBACK_MODEL})"
        )
    except Exception:
        pass
    
    while True:
        try:
            now = datetime.now(EST)
            seconds_left = _seconds_until_next_run()
            
            if seconds_left > HEARTBEAT_INTERVAL:
                # live-update: far from the next run = safe point to reload an owner patch
                _lu_maybe_reload()
                # Far from target  -  sleep one heartbeat interval, then loop back to check again
                # This way the heartbeat stays fresh and we react to clock jumps gracefully
                write_heartbeat("idle_waiting", {
                    "seconds_until_run": int(seconds_left),
                    "minutes_until_run": round(seconds_left / 60, 1),
                    "hours_until_run":   round(seconds_left / 3600, 2),
                })
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                continue
            
            # We're within HEARTBEAT_INTERVAL of the target. Sleep precisely until target.
            if seconds_left > 0:
                write_heartbeat("imminent", {"seconds_until_run": int(seconds_left)})
                await asyncio.sleep(seconds_left)
            
            # Verify we can actually fire (re-check time + min-hours-between-runs guard)
            allowed, why = _can_fire_run()
            if not allowed:
                log(f"⚠️ skip nightly fire: {why}")
                write_heartbeat("idle_skip_dedup", {"reason": why})
                # Sleep past the trigger window so we don't immediately re-enter this branch
                await asyncio.sleep(120)
                continue
            
            log(f"✅ scheduled trigger reached ({why})")
            await fire_one_run(reason="scheduled_2am")
            
            # After firing, sleep at least a few minutes so the next loop iteration
            # doesn't immediately compute "0 seconds to next run" and refire.
            # _next_scheduled_dt() will already roll forward to tomorrow.
            await asyncio.sleep(120)
        
        except KeyboardInterrupt:
            log("⏹️  daemon interrupted by user (Ctrl+C)")
            write_heartbeat("stopped")
            try:
                await notify("⏹️ Overseer daemon stopped (Ctrl+C)")
            except Exception:
                pass
            return
        except Exception as e:
            # Shouldn't happen  -  fire_one_run catches its own errors. But just in case
            # the loop logic itself fails, log + sleep + continue. Don't die.
            import traceback
            log(f"❌ daemon loop iteration error: {e}")
            log(traceback.format_exc())
            write_heartbeat("loop_error", {"error": str(e)[:200]})
            await asyncio.sleep(300)  # 5 min before retrying loop


# ============================================================================
#   LIVE-UPDATE (element e, simple-bot clean core)
# ============================================================================
# This overseer is idle between nightly runs, so the safe point is trivial: when
# we're far from the next run, if THIS file changed on disk (The owner applied a patch),
# re-exec in place (os.execv, same PID + console) so the patch goes live without a
# manual restart and the bot stays in the terminal the owner launched it from. No browser
# or external process to tear down; Ctrl-C works normally. Floor: only ever reload a
# change already on disk (The owner applies it); never fetch, self-modify, or choose code.
import sys as _lu_sys
import shutil as _lu_shutil
import hashlib as _lu_hashlib

_LU_SELF     = os.path.abspath(__file__)
_LU_MARKER   = os.path.join(LOGS, "agent_autoagent_reload.json")
_LU_LASTGOOD = _LU_SELF + ".last-good"


def _lu_mtime(pth):
    try:
        return os.path.getmtime(pth)
    except Exception:
        return 0.0


def _lu_hash(pth):
    try:
        with open(pth, "rb") as f:
            return _lu_hashlib.sha256(f.read()).hexdigest()[:8]
    except Exception:
        return ""


_LU_BOOT_MTIME = _lu_mtime(_LU_SELF)
_LU_BOOT_HASH  = _lu_hash(_LU_SELF)


def _lu_snapshot_last_good():
    try:
        _lu_shutil.copy2(_LU_SELF, _LU_LASTGOOD)
    except Exception:
        pass


def _lu_write_report(kind, severity, summary, extra=None):
    try:
        os.makedirs(_OWNER_INBOX_DIR, exist_ok=True)
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_OWNER_INBOX_DIR, f"agent_autoagent - {kind}_{ts}.md")
        fm = {"kind": kind, "severity": severity,
              "dedupe_key": f"agent_autoagent:{kind}",
              "source_skill": "autoagent", "summary": summary}
        lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", "",
                 f"# agent_autoagent {kind}", summary]
        if extra:
            lines += ["", "```json", json.dumps(extra, indent=2, default=str), "```"]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        log(f"live-update report failed: {e}")


def _lu_on_boot():
    marker = load_json(_LU_MARKER, None)
    if isinstance(marker, dict):
        _lu_write_report("bot_restart", "info",
                         f"agent_autoagent reloaded {marker.get('from_hash')} -> {marker.get('to_hash')} (live-update, healthy)",
                         marker)
        try:
            os.remove(_LU_MARKER)
        except Exception:
            pass
    _lu_snapshot_last_good()


def _lu_reload_now():
    new_hash = _lu_hash(_LU_SELF)
    _lu_snapshot_last_good()
    save_json_atomic(_LU_MARKER, {"from_hash": _LU_BOOT_HASH, "to_hash": new_hash,
                                  "detected_at": now_iso(), "lastgood": _LU_LASTGOOD})
    log(f"live-update: reloading {_LU_BOOT_HASH} -> {new_hash} (re-exec in place)")
    try:
        os.execv(_lu_sys.executable, [_lu_sys.executable, _LU_SELF] + _lu_sys.argv[1:])
    except Exception as e:
        # Re-exec failed -> still on the OLD (working) code. Report, keep running.
        log(f"live-update: os.execv failed, staying on current code: {e}")
        _lu_write_report("bot_restart_failed", "high",
                         f"agent_autoagent patch {new_hash} detected but re-exec failed; STILL RUNNING old {_LU_BOOT_HASH}",
                         {"error": str(e)[:200]})
        try:
            os.remove(_LU_MARKER)
        except Exception:
            pass


def _lu_maybe_reload():
    # call only at a safe point (between nightly runs). os.execv does not return.
    if _lu_mtime(_LU_SELF) > _LU_BOOT_MTIME and _lu_hash(_LU_SELF) != _LU_BOOT_HASH:
        _lu_reload_now()


async def main():
    """
    Forever-loop daemon. Runs nightly at 2am EST.
    
    Usage:
      python agent_autoagent.py            → start daemon (forever loop)
      python agent_autoagent.py --now      → fire one run immediately, then exit
      python agent_autoagent.py --once     → alias for --now (back-compat)
    
    Started by bots/perps.py with the other sub-bots (PERPS_COMPONENTS limits which).
    Standalone: python bots/sub-bots/agent_autoagent.py
    """
    import sys
    
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    one_shot = any(a in args for a in ["--now", "--once", "--manual"])
    
    log(f"agent_autoagent (Overseer daemon mode) starting at {now_str()} - judge xAI grok-4.7, ollama fallback")
    _lu_on_boot()  # live-update: report a completed reload + snapshot last-good
    log(f"  mode: {'ONE-SHOT (--now)' if one_shot else 'DAEMON (forever loop)'}")
    log(f"  primary judge: Grok API ({XAI_MODEL})")
    log(f"  fallback judge: Ollama ({OLLAMA_FALLBACK_MODEL})")
    if not one_shot:
        log(f"  schedule: {SCHEDULED_HOUR_EST:02d}:{SCHEDULED_MINUTE:02d} EST nightly")
        log(f"  next run: {_next_scheduled_dt().isoformat()}")
    
    await preflight_checks()
    
    if one_shot:
        # Manual fire  -  same wrap as scheduled fire so state gets persisted
        await fire_one_run(reason="manual_invoke")
        log("✅ one-shot run complete, exiting")
        return
    
    # Daemon mode
    await daemon_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Already handled inside daemon_loop, but in case main() didn't get to it
        pass