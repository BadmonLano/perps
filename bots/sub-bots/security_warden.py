"""security_warden.py - Perps ecosystem guard.
Watches only the Perps ecosystem: perps.py, bots/sub-bots, bots/feeds, their files and wallet.

Layers: ingress scan of untrusted feed text, egress check on perp signals, wallet / canary /
API health, feed freshness (bots/feeds outputs), heartbeat + work-evidence + exec-failure
monitors for perps_bot and the sub-bots, medic (mechanical restore + report only, never a
restart), doctor (evidence packets to the agent), file integrity, daily digest, outbound
Telegram alerts. Optional SI calls follow LLM_MAIN / LLM_FALLBACK (both off = none).
(Public build: history trimmed; see TIMELINE.md.)
"""

import os

def _envv(key, default=None):
    """os.getenv, but a blank value (KEY= in .env) counts as unset and returns the default."""
    v = os.environ.get(key)
    return default if v is None or not str(v).strip() else v


# ── Repo root (public build) ──────────────────────────────────────────────
# WORKSPACE_DIR in the environment (or bots/.env) wins; otherwise the repo root is
# the folder that contains bots/ (works from bots/ and bots/sub-bots/).
def _resolve_workspace_root():
    _env = (os.environ.get("WORKSPACE_DIR") or "").strip()
    if _env:
        return os.path.abspath(os.path.expanduser(_env))
    _p = os.path.dirname(os.path.abspath(__file__))
    _root = None
    for _ in range(6):
        if os.path.basename(_p).lower() == "bots":
            _root = os.path.dirname(_p); break
        _p = os.path.dirname(_p)
    _root = _root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for _envf in (os.path.join(_root, "bots", ".env"), os.path.join(_root, ".env")):
        try:
            with open(_envf, encoding="utf-8-sig") as _fh:
                for _ln in _fh:
                    if _ln.strip().startswith("WORKSPACE_DIR="):
                        _v = _ln.split("=", 1)[1].strip().strip('"').strip("'")
                        if _v:
                            return os.path.abspath(os.path.expanduser(_v))
        except OSError:
            pass
    return _root
WS_ROOT = _resolve_workspace_root()
WS_BOTS = os.path.join(WS_ROOT, "bots")
WS_BOT_LOGS = os.path.join(WS_ROOT, "bot_logs")
WS_JSON_RELAY = os.path.join(WS_ROOT, "json_relay")
# ──────────────────────────────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _si_gate  # LLM_MAIN / LLM_FALLBACK gate for every SI call
import json
import re
import time
import asyncio
import hashlib
import subprocess
import shutil
import difflib
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# The agent's wake-up brief , runs as a task in main()'s gather (see below).
# Imported after load_dotenv() so its wallet env vars are populated.
# (public build) the wake-up brief writer is not shipped; the warden writes its own heartbeat JSON.

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

EST              = ZoneInfo("America/New_York")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Optional separate alert bot token (avoids getUpdates conflicts with another poller on
# TELEGRAM_TOKEN). Warden is outbound-only. Falls back to TELEGRAM_TOKEN.
WARDEN_TG_TOKEN = os.getenv("WARDEN_TELEGRAM_TOKEN", "") or TELEGRAM_TOKEN
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY", "") or os.getenv("RPC_URL", "").split("api-key=")[-1]
WALLET_ADDRESS   = os.getenv("WALLET_ADDRESS", "")

# Optional SI: order from LLM_MAIN / LLM_FALLBACK (see _si_gate). NO OpenRouter.
XAI_API_KEY      = os.getenv("XAI_API_KEY", "")
_GROK_PIN        = (os.getenv("GROK_TEXT_MODEL") or "").strip()
XAI_MODEL        = ((os.getenv("XAI_MODEL") or "").strip()
                    or (_GROK_PIN if _GROK_PIN and _GROK_PIN.lower() != "auto" else "")
                    or "grok-4.7")  # cloud primary; Ollama stays local fallback only
OLLAMA_URL       = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL     = os.getenv("OLLAMA_MODEL", "qwen3:14b-q4_K_M")

# Workspace paths (The agent's actual paths)
WORKSPACE        = WS_ROOT
BOT_LOGS         = WS_BOT_LOGS

# ── SELF-HEARTBEAT (2026-07-26, the agent's overseer ticket) ────────────────────
# Scored alive=false with "no hb". Root cause: this bot reads every OTHER bot's heartbeat, but it never
# wrote its OWN security_warden_heartbeat.json — the exact filename the overseer
# mtime-checks in bot_logs (30-min threshold). Instrumentation gap, not a dead
# process. Half the "dead stack" in the 02:05 revive ticket was this.
_SELF_HB_FILE = os.path.join(BOT_LOGS, "security_warden_heartbeat.json")

def _write_self_heartbeat(status: str = "alive", **extra) -> None:
    try:
        _hb_now = datetime.now(EST).isoformat()
        _hb_payload = {"components": {"security_warden": {"status": status, "last_update": _hb_now,
                                                   "pid": os.getpid(), **extra}},
                        "last_overall_update": _hb_now}
        _hb_tmp = _SELF_HB_FILE + ".tmp"
        with open(_hb_tmp, "w", encoding="utf-8") as _hbf:
            json.dump(_hb_payload, _hbf, indent=2)
        os.replace(_hb_tmp, _SELF_HB_FILE)
    except Exception:
        pass

JSON_RELAY       = WS_JSON_RELAY
INBOX            = os.path.join(WS_ROOT, "inbox")
# Canonical archive is inbox_results/done/ (never inbox/done).
INBOX_DONE       = os.path.join(WS_ROOT, "inbox_results", "done")
OWNER_INBOX       = os.path.join(WS_ROOT, "owner_inbox")
BOTS_DIR         = WS_BOTS

# Inbox cron lock reaper (Sprint 65): the inbox cron writes bot_logs/inbox_cron.lock
# at the start of a pass and deletes it when done. If a pass dies mid-run (crash,
# timeout, context cut) BEFORE the delete step, the lock goes stale and blocks future
# passes until manually removed. The warden reaps a lock older than this threshold so
# The owner doesn't have to. Margin above the cron's own 12-min guard so we never kill a
# lock from a pass that's legitimately still running.
INBOX_CRON_LOCK_FILE     = os.path.join(BOT_LOGS, "inbox_cron.lock")
INBOX_CRON_LOCK_MAX_AGE_S = int(_envv("INBOX_CRON_LOCK_MAX_AGE_S", str(15 * 60)))  # 15 min (cron guard is 12)
INBOX_CRON_LOCK_CHECK_S   = 60  # check once a minute
SUBAGENTS_DIR    = os.path.join(WS_BOTS, "subagents")

# Log files
WARDEN_LOG       = os.path.join(BOT_LOGS, "security_warden.json")
COUNTER_INTEL    = os.path.join(BOT_LOGS, "counter_intel.json")
BLACKLIST_FILE   = os.path.join(BOT_LOGS, "injection_blacklist.json")
CONFIG_FILE      = os.path.join(BOT_LOGS, "warden_config.json")
KILL_SWITCH_FILE = os.path.join(BOT_LOGS, "KILL_SWITCH.flag")
LOCKDOWN_FILE    = os.path.join(BOT_LOGS, "lockdown_state.flag")
QUARANTINE_DIR   = os.path.join(BOT_LOGS, "quarantine")
CANARY_DIR       = os.path.join(BOT_LOGS, "canary")
CANARY_LOG       = os.path.join(BOT_LOGS, "canary_trips.json")

# Medic-specific
MEDIC_LOG        = os.path.join(BOT_LOGS, "medic_log.json")
MEDIC_STAGING    = os.path.join(BOT_LOGS, "medic_staging")
MEDIC_BACKUPS    = os.path.join(BOT_LOGS, "medic_backups")
MEDIC_AUDIT      = os.path.join(BOT_LOGS, "medic_audit")

# Integrity-specific
INTEGRITY_LOG    = os.path.join(BOT_LOGS, "integrity_log.json")
HASH_BASELINE    = os.path.join(BOT_LOGS, "known_good_hashes.json")

# Comms-specific
TELEGRAM_CMD_LOG = os.path.join(BOT_LOGS, "warden_tg_commands.json")
LAST_TG_UPDATE   = os.path.join(BOT_LOGS, "warden_tg_last_update.txt")

# ── Sprint 41 — Skill Evolution Layer 10 paths ──────────────────────────────
# Skill ecosystem (read targets)
SKILLS_DIR              = os.path.join(WORKSPACE, "skills")
SKILLS_DOCTRINE_DIR     = os.path.join(SKILLS_DIR, "_doctrine")
SKILLS_ARCHIVED_DIR     = os.path.join(SKILLS_DIR, "_archived")
# Auto-generated catalog (Layer 10 maintains)
SKILL_CATALOG_FILE      = os.path.join(BOT_LOGS, "skill_catalog.json")
# Append-only audit chain for skill evolution
SKILL_EVOLUTION_LOG     = os.path.join(BOT_LOGS, "skill_evolution_log.jsonl")
# Cross-skill doctrine map (which skills inherit which doctrines)
CROSS_SKILL_DOCTRINE_MAP = os.path.join(BOT_LOGS, "cross_skill_doctrine_map.json")
# Skill catalog hash baseline (separate from CRITICAL_FILES baseline)
SKILL_HASH_BASELINE     = os.path.join(BOT_LOGS, "skill_known_good_hashes.json")
# Pending skill requests folder (mirrors code_requests.json pattern)
SKILL_REQUESTS_FILE     = os.path.join(WORKSPACE, "bots", "json_relay", "skill_requests.json")  # RETIRED dead constant - no longer read or written (agent_direct)
# The owner review folder for skill amendments (parallel to bot_builder drafts)
OWNER_INBOX_SKILLS_DIR   = os.path.join(OWNER_INBOX, "pending_skill_requests")

# Ensure dirs exist
for d in (BOT_LOGS, INBOX, INBOX_DONE, OWNER_INBOX, QUARANTINE_DIR, CANARY_DIR,
          MEDIC_STAGING, MEDIC_BACKUPS, MEDIC_AUDIT,
          SKILLS_DOCTRINE_DIR, SKILLS_ARCHIVED_DIR, OWNER_INBOX_SKILLS_DIR):
    os.makedirs(d, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
# CRITICAL FILES (integrity check targets)
# ═══════════════════════════════════════════════════════════════════════════

# ── v3.4: OPERATOR-AWARE INTEGRITY (Sprint 38) ─────────────────────────────
# The old model treated ANY hash change as a possible breach: The owner edits soul.md
# (which he does constantly — that's literally his job), 6h later a P1 packet
# tells the agent "critical files modified... if unauthorized: WARDEN LOCKDOWN
# IMMEDIATELY," The agent asks the owner, the owner says "it was me" — every single time.
# And because the baseline refresh was a manual Telegram command nobody ran,
# the SAME edit re-alarmed every 6h forever. The threat model was inverted:
# routine operator activity was treated as the attack.
#
# v3.4 splits the watchlist into two classes:
#   OPERATOR_DOC_FILES — docs the owner edits routinely. A change here is a CHANGE
#     LEDGER entry + auto-rebaseline + a line in the daily digest. No packet,
#     no Telegram, no interrogation. The agent reads the digest as change
#     awareness ("The owner touched soul.md and rules.md this week"), not intrusion.
#   SENTINEL_FILES — the secrets file. A change here is rare and always worth
#     one confirmation ping ("was this you?") — but the copy asks, it does not
#     accuse, and it never says lockdown.
# The REAL threat classes that still escalate hard:
#   • a baselined file DELETED            → HIGH packet
#   • .env changed                        → HIGH packet + Telegram (confirm)
#   • mass change (≥6 files) INCLUDING .env → CRITICAL (lockdown language earned)
OPERATOR_DOC_FILES = [
    # Public drop docs (the owner edits these; a change is awareness, not intrusion)
    os.path.join(WORKSPACE, n) for n in
    ("README.md", "SETUP.md", "SECURITY.md", "CONTRIBUTING.md", "TIMELINE.md", "requirements.txt")
] + [
    os.path.join(BOTS_DIR, n) for n in ("perps.py", "perps_ui.py", "perps_setup.py", "feed_hook.py")
]
SENTINEL_FILES = [
    # .env tracked by hash only (never read contents). Lives in bots/.
    os.path.join(BOTS_DIR, ".env"),
]
CRITICAL_FILES = OPERATOR_DOC_FILES + SENTINEL_FILES   # baseline covers both

# Bot whitelist — these are agent's bots that medic is authorized to heal
# Anything not in this list, medic will refuse to touch (Tier 3 escalation only).
AGENT_BOTS_WHITELIST = [
    # ── Core execution layer ───────────────────────────────────
    "perps_bot.py",   # heartbeat name; source is bots/perps.py (see _bot_source_path)
    # ── Intelligence + brain ───────────────────────────────────
    # ── Infrastructure ─────────────────────────────────────────
    "agent_autoagent.py",
    # ── Code modification toolchain ────────────────────────────
    "code_tuner.py",  # medic + doctor route requests THROUGH it; not modified BY it
    "owner_commands.py",
    "learnings_writer.py",
    # security_warden.py SELF excluded — never let medic heal warden itself
]

# Files medic NEVER touches under any circumstance
MEDIC_BLACKLIST_FILES = [
    ".env",
    "wallet_keys.json",
    "private_key.json",
    "perp_positions.json",  # plural venue of record (singular is dead)
    "pnl.json",
    "LEARNINGS.md",  # forensic record, never auto-mutated
    "security_warden.py",  # warden never modifies itself
]

# ═══════════════════════════════════════════════════════════════════════════
# INJECTION PATTERNS (applied to UNTRUSTED text only)
# ═══════════════════════════════════════════════════════════════════════════

BASE_INJECTION_PATTERNS = [
    # Instruction override attempts
    "ignore previous instructions", "ignore all instructions", "ignore prior",
    "disregard your programming", "disregard previous", "forget everything",
    "forget your instructions", "forget your training", "forget your role",
    "new instructions:", "updated instructions:", "revised instructions:",
    # Role manipulation
    "you are now", "act as", "pretend to be", "roleplay as",
    "from now on you are", "you will now act",
    # Capability extraction
    "show me your prompt", "reveal your system", "print your instructions",
    "what are your rules", "list your guidelines",
    # Jailbreak patterns
    "do anything now", "in dan mode", "developer mode enabled",
    "no restrictions", "without limitations", "bypass your safety",
    # Direct attacks
    "drop all tables", "rm -rf", "del /f /s",
    # Wallet/credential probes via injection
    "send funds to", "transfer balance to", "export private key",
    "what's in your .env", "show your wallet seed",
]

CREDENTIAL_THEFT_PATTERNS = [
    "private_key", "wallet seed", "api_key for", "bearer token for",
    "what is your helius", "what is your telegram token",
    "leak your credentials", "expose your secrets",
    "dump environment variables", "echo $env",
]

# ═══════════════════════════════════════════════════════════════════════════
# DEFAULT CONFIG
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "ingress_scan_interval_seconds": 60,
    "egress_scan_interval_seconds":  90,
    "wallet_unknown_tx_threshold":   2,
    "wallet_alert_cooldown_minutes": 30,
    "api_health_check_interval_seconds": 1800,
    "feed_check_interval_seconds": 600,
    "canary_enabled": True,
    # v3.3: was 60min, now 12h. The "dead_man" semantic is now "system-wide
    # activity has stopped" not "any one bot is quiet." For per-bot liveness
    # use bot_heartbeat_monitor (see below).
    "dead_mans_switch_max_silence_minutes": 720,  # 12h
    "medic_enabled": True,
    "medic_check_interval_seconds": 300,
    "medic_max_heal_attempts_per_bot_per_hour": 3,
    "medic_backoff_window_hours": 6,  # v3.3: 3 attempts in 6h → escalate to doctor
    "integrity_check_interval_seconds": 21600,  # 6h
    "telegram_poll_interval_seconds": 30,
    "daily_digest_hour_est": 8,
    # ── v3.3 NEW: bot heartbeat liveness thresholds ──────────────
    # Per-bot stale threshold in MINUTES. Bot's heartbeat must be fresher
    # than threshold OR it's considered offline. Critical bots get tight,
    # low-cadence bots get loose.
    "bot_heartbeat_monitor_enabled": True,
    "bot_heartbeat_check_interval_seconds": 600,  # 10min
    "bot_heartbeat_alert_cooldown_hours": 24,     # don't ping same bot twice in 24h
    # ── PARKED BOTS (2026-08-06, trainer_runner LENS) ────────────────────────
    # A bot listed here is DORMANT BY DESIGN. It is not launched by Start_Bots,
    # nothing consumes its output, and its heartbeat going stale is the expected
    # state - not an incident. The heartbeat monitor skips it entirely instead of
    # paging on a schedule.
    #
    #
    # This is NOT a way to silence a bot that is supposed to be running. Removal
    # from this list is the un-park gate: a bot comes off the list at the same
    # moment it goes back into Start_Bots, and warden resumes paging that turn.
    "parked_bots": [],

    "bot_heartbeat_thresholds_minutes": {
        # CRITICAL — execution layer + brain
        "perps_bot":        360,      # 6h
        # INFRASTRUCTURE
        "agent_autoagent": 1500,     # 25h (24h cron + slack)
        "code_tuner":       1500,     # 25h (daily cron + slack)
        "learnings_writer":   30,     # heartbeat every LEARNINGS_WRITER_INTERVAL_S (60s)
        "security_warden":  60,       # self heartbeat every few min
        "owner_commands":   60,       # owner_commands_heartbeat.json every 60s
                                      # threshold is inert while parked and is kept only
                                      # so un-parking is a one-line list removal.
                                      # The old comment here claimed the bot "writes
                                      # heartbeat each fire-check cycle" and fires when
                                      # the validated corpus crosses a threshold. Neither
                                      # was true: it beats every 60s regardless, and the
                                      # validated corpus has been 0 since 2026-05-23, so
                                      # it has never fired once. (Sprint 44 scaffold)
    },
    # ── v3.3 NEW: doctor layer ──────────────────────────────────
    "doctor_enabled": True,
    "doctor_check_interval_seconds": 300,
    "doctor_use_claude": False,  # RETIRED , warden no longer calls any LLM to diagnose; doctor routes evidence to the agent
    # ── v3.3 NEW: silent failure detection (venue-offline-style problems) ──
    # If an execution bot's heartbeat is FRESH but it hasn't produced an
    # output (trade, decision, post, etc.) for > N hours during signals
    # being good, that's a silent failure. Doctor investigates.
    "silent_failure_check_interval_seconds": 1800,  # 30min
    "silent_failure_min_idle_hours": 12,  # only flag after 12h of total silence on output
    # ── Sprint 41 NEW: skill evolution Layer 10 ─────────────────────
    "skill_evolution_enabled": True,
    "skill_evolution_check_interval_seconds": 300,  # 5min
    "bounds": {
        "perps":  {"max_size_usd": 200},
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# CORE UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

def _bot_source_path(bot_name: str) -> str:
    """Where a Perps-ecosystem bot's source lives: perps_bot -> bots/perps.py,
    sub-bots -> bots/sub-bots/<name>.py, anything else -> bots/<name>."""
    n = bot_name if bot_name.endswith(".py") else bot_name + ".py"
    if n == "perps_bot.py":
        return os.path.join(BOTS_DIR, "perps.py")
    sub = os.path.join(BOTS_DIR, "sub-bots", n)
    return sub if os.path.exists(sub) else os.path.join(BOTS_DIR, n)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data):
    """Atomic write via tmp + replace."""
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[warden] save_json err for {path}: {e}")
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass




def load_config() -> dict:
    cfg = load_json(CONFIG_FILE, {})
    if not cfg:
        save_json(CONFIG_FILE, DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    # Fill in any missing keys from defaults
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    return cfg


async def notify(msg: str):
    """Send a Telegram alert (outbound only). Uses WARDEN_TELEGRAM_TOKEN if set, else
    TELEGRAM_TOKEN. Silent fallback to stdout if Telegram is unavailable."""
    if not WARDEN_TG_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[warden] notify: {msg[:150]}")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{WARDEN_TG_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
            )
    except Exception as e:
        print(f"[warden] notify err: {e}")


def log_finding(category: str, severity: str, detail: str, remediation: str = ""):
    findings = load_json(WARDEN_LOG, [])
    findings.append({
        "timestamp":   datetime.now(EST).isoformat(),
        "category":    category,
        "severity":    severity,
        "detail":      detail,
        "remediation": remediation,
        "resolved":    False,
    })
    findings = findings[-500:]
    save_json(WARDEN_LOG, findings)


def log_attack(source: str, payload: str, attack_type: str, metadata: dict = None):
    intel = load_json(COUNTER_INTEL, [])
    intel.append({
        "timestamp":   datetime.now(EST).isoformat(),
        "source":      source,
        "attack_type": attack_type,
        "payload":     payload[:500],
        "metadata":    metadata or {},
        "blocked":     True,
    })
    intel = intel[-2000:]
    save_json(COUNTER_INTEL, intel)
    print(f"[warden] attack logged: {attack_type} from {source}")


def is_kill_switch_active() -> bool:
    return os.path.exists(KILL_SWITCH_FILE)


def is_lockdown_active() -> bool:
    return os.path.exists(LOCKDOWN_FILE)


def engage_lockdown(reason: str):
    """Engage lockdown — bots that check this flag will halt writes."""
    try:
        with open(LOCKDOWN_FILE, "w", encoding="utf-8") as f:
            f.write(f"ENGAGED: {datetime.now(EST).isoformat()}\nREASON: {reason}\n")
        log_finding("lockdown", "CRITICAL", f"lockdown engaged: {reason}",
                    f"Telegram WARDEN RESUME or delete {LOCKDOWN_FILE} to lift")
    except Exception as e:
        print(f"[warden] lockdown engage err: {e}")


def lift_lockdown():
    if os.path.exists(LOCKDOWN_FILE):
        try:
            os.remove(LOCKDOWN_FILE)
            log_finding("lockdown", "INFO", "lockdown lifted", "")
        except Exception as e:
            print(f"[warden] lockdown lift err: {e}")


def engage_kill_switch(reason: str):
    """Manual emergency only. Warden does NOT auto-engage this."""
    try:
        with open(KILL_SWITCH_FILE, "w", encoding="utf-8") as f:
            f.write(f"ENGAGED: {datetime.now(EST).isoformat()}\nREASON: {reason}\n")
        log_finding("kill_switch", "CRITICAL", f"kill switch engaged: {reason}",
                    f"Review and delete {KILL_SWITCH_FILE} to resume")
    except Exception as e:
        print(f"[warden] kill_switch write err: {e}")


_SECRET_LINE_RE = re.compile(
    r"(?i)("
    r"(?:api[_-]?key|apikey|secret(?:[_-]?key)?|password|passwd|pwd|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|"
    r"seed[_-]?phrase|mnemonic|wallet[_-]?seed|bearer[_-]?token)"
    r"\s*[:=]\s*\S+"
    r"|authorization\s*:\s*bearer\s+\S+"
    r"|\bbearer\s+[A-Za-z0-9\-._~+/]{16,}"
    r"|-----BEGIN\s+[A-Z0-9 ]*PRIVATE\s+KEY-----"
    r"|xai-[A-Za-z0-9]{12,}"
    r"|sk-[A-Za-z0-9_\-]{12,}"
    r"|ghp_[A-Za-z0-9]{12,}"
    r"|AKIA[0-9A-Z]{16}"
    r")"
)
_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|password|passwd|private[_-]?key|"
    r"seed[_-]?phrase|mnemonic|bearer|access[_-]?token|refresh[_-]?token|secret[_-]?key)"
)


def _strip_secret_lines(text: str) -> str:
    """Drop lines that look like secrets. Never prints the dropped text."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    kept = []
    for line in text.splitlines(keepends=True):
        if _SECRET_LINE_RE.search(line):
            continue
        kept.append(line)
    return "".join(kept)


def _strip_secrets_obj(obj):
    """Redact secret-looking keys and lines inside a quarantine payload."""
    if isinstance(obj, str):
        return _strip_secret_lines(obj)
    if isinstance(obj, list):
        return [_strip_secrets_obj(x) for x in obj]
    if isinstance(obj, dict):
        cleaned = {}
        for key, val in obj.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                continue
            cleaned[key] = _strip_secrets_obj(val)
        return cleaned
    return obj

def write_inbox_packet(priority: str, source: str, slug: str, content: str,
                       target_inbox: str = "agent") -> str:
    """
    Write a packet to an inbox.

    priority: "P1" (critical) | "P2" (high) | "P3" (normal) | "P4" (low/self-note)
    source: short identifier ("warden_critical", "warden_heal", "warden_daily")
    slug: short descriptor ("balance_anomaly", "perps_bot_healed")
    content: full markdown body of the packet
    target_inbox: "agent" (default — the agent's inbox at INBOX) or "owner" (The owner's
                  inbox at OWNER_INBOX, used for items requiring human review like
                  bot-builder drafts).

    Returns the absolute path of the written packet.
    """
    ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
    # Naming doctrine: EVERYTHING landing in an inbox is named `botname - subject_ts`.
    # No priority prefix on the filename anymore — priority lives in the header below.
    # `source` is ignored for the filename (all warden layers collapse to one botname);
    # it's still recorded in the header for context.
    filename = f"security_warden - {slug}_{ts}.md"

    if target_inbox == "owner":
        path = os.path.join(OWNER_INBOX, filename)
        recipient = "The owner (review + manual action required)"
    else:
        path = os.path.join(INBOX, filename)
        recipient = "The agent (autonomous processing)"

    header = (
        f"# {slug.replace('_', ' ').title()}\n\n"
        f"**Source:** {source}\n"
        f"**Priority:** {priority}\n"
        f"**Recipient:** {recipient}\n"
        f"**Written:** {datetime.now(EST).isoformat()}\n"
        f"**Lockdown active:** {is_lockdown_active()}\n"
        f"**Kill switch active:** {is_kill_switch_active()}\n\n"
        f"---\n\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + content + "\n")
        print(f"[warden] inbox packet written ({target_inbox}): {filename}")
    except Exception as e:
        print(f"[warden] inbox write err: {e}")
    return path


def file_sha256(path: str) -> str:
    """Compute SHA-256 hex digest of a file. Returns empty string on error."""
    if not os.path.exists(path):
        return ""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        print(f"[warden] hash err for {path}: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# INFERENCE LAYER (grok-4.7 primary, Ollama fallback)
# ═══════════════════════════════════════════════════════════════════════════

async def ask_llm(prompt: str, max_tokens: int = 2000, system: str = "") -> str:
    """
    Provider order from LLM_MAIN / LLM_FALLBACK (grok | ollama | off). Returns "" when
    both are off or nothing answers; callers then use their rule-based path.
    Used by medic for fix suggestions and bot-builder for code synthesis.
    """
    for prov in _si_gate.order():  # LLM_MAIN / LLM_FALLBACK; both off = no SI call
        try:
            if prov == "grok" and XAI_API_KEY:
                return await _ask_grok(prompt, max_tokens, system)
            if prov == "ollama":
                return await _ask_ollama(prompt, max_tokens, system)
        except Exception as e:
            print(f"[warden] {prov} err: {e}")
    return ""


async def _ask_grok(prompt: str, max_tokens: int, system: str) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {XAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": XAI_MODEL, "messages": messages, "max_tokens": max_tokens}
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def _ask_ollama(prompt: str, max_tokens: int, system: str) -> str:
    full = (f"{system}\n\n{prompt}" if system else prompt)
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": full, "stream": False,
                  "options": {"num_predict": max_tokens}}
        )
        r.raise_for_status()
        return r.json().get("response", "")


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 1: INGRESS GUARDIAN
# Scans UNTRUSTED text the feeds pull in (news headlines, market titles).
# ═══════════════════════════════════════════════════════════════════════════

UNTRUSTED_SOURCES = {
    "news_catalysts":  os.path.join(BOT_LOGS, "news_catalysts.json"),
    "polymarket_markets": os.path.join(BOT_LOGS, "polymarket_markets.json"),
}


def get_injection_patterns():
    blacklist = load_json(BLACKLIST_FILE, [])
    patterns = [p.lower() for p in BASE_INJECTION_PATTERNS + CREDENTIAL_THEFT_PATTERNS]
    patterns.extend([b.lower() for b in blacklist if len(b) > 5])
    return list(set(patterns))


def scan_text_for_injection(text: str) -> list:
    if not text or not isinstance(text, str):
        return []
    lower = text.lower()
    matches = []
    for pattern in get_injection_patterns():
        if pattern in lower:
            matches.append(pattern)
    return matches[:5]


def block_untrusted_item(source: str, item: dict, matched_patterns: list, item_identifier: str = ""):
    log_attack(
        source=source,
        payload=json.dumps(item, default=str)[:500],
        attack_type="prompt_injection_blocked",
        metadata={"patterns": matched_patterns, "identifier": item_identifier}
    )
    blacklist = load_json(BLACKLIST_FILE, [])
    learned = 0
    for pattern in matched_patterns:
        if 5 < len(pattern) < 100 and pattern not in blacklist:
            blacklist.append(pattern)
            learned += 1
    save_json(BLACKLIST_FILE, blacklist[-500:])

    quarantine_path = os.path.join(
        QUARANTINE_DIR,
        f"{datetime.now(EST).strftime('%Y%m%d_%H%M%S')}_{source}_{item_identifier[:20]}.json"
    )
    try:
        save_json(quarantine_path, {
            "source":     source,
            "blocked_at": datetime.now(EST).isoformat(),
            "patterns":   matched_patterns,
            "item":       _strip_secrets_obj(item),
        })
    except Exception as e:
        print(f"[warden] quarantine write err: {e}")


async def ingress_guardian():
    print("[warden] ingress guardian active")
    cfg = load_config()
    interval = cfg.get("ingress_scan_interval_seconds", 60)
    seen_identifiers = set()

    while True:
        try:
            for source_name, source_path in UNTRUSTED_SOURCES.items():
                if not os.path.exists(source_path):
                    continue
                data = load_json(source_path, [])
                if not isinstance(data, list):
                    if isinstance(data, dict):
                        data = [data]
                    else:
                        continue

                for item in data[-100:]:  # only scan last 100 per cycle
                    if not isinstance(item, dict):
                        continue
                    identifier = (item.get("id") or item.get("username")
                                  or item.get("url") or item.get("ticker") or "")
                    if not identifier:
                        identifier = json.dumps(item, default=str)[:50]
                    if identifier in seen_identifiers:
                        continue

                    text_blob = ""
                    for k in ("text", "content", "description", "tweet", "name", "title", "body"):
                        v = item.get(k, "")
                        if isinstance(v, str):
                            text_blob += " " + v

                    matches = scan_text_for_injection(text_blob)
                    if matches:
                        block_untrusted_item(source_name, item, matches, identifier)
                        await notify(
                            f"🛡️ <b>Warden: Injection blocked</b>\n"
                            f"Source: {source_name}\n"
                            f"From: {identifier[:60]}\n"
                            f"Patterns: {', '.join(matches[:3])}"
                        )
                    seen_identifiers.add(identifier)

            # Cap memory of seen IDs
            if len(seen_identifiers) > 5000:
                seen_identifiers = set(list(seen_identifiers)[-2000:])

            await asyncio.sleep(interval)
        except Exception as e:
            print(f"[warden] ingress err: {e}")
            await asyncio.sleep(120)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 2: EGRESS OBSERVER
# Logs the agent's signal writes. Alerts only on catastrophic issues.
# Never quarantines internal signals.
# ═══════════════════════════════════════════════════════════════════════════

EGRESS_TARGETS = [
    os.path.join(JSON_RELAY, "agent_signals.json"),
    os.path.join(JSON_RELAY, "signal_response.json"),
    os.path.join(JSON_RELAY, "perp_signal.json"),
]


async def egress_validator():
    print("[warden] egress observer active")
    cfg = load_config()
    interval = cfg.get("egress_scan_interval_seconds", 90)

    last_seen_size = {p: 0 for p in EGRESS_TARGETS}

    while True:
        _write_self_heartbeat("alive")
        try:
            for path in EGRESS_TARGETS:
                if not os.path.exists(path):
                    continue
                size = os.path.getsize(path)
                if size == last_seen_size[path]:
                    continue
                last_seen_size[path] = size

                data = load_json(path, [])
                if not isinstance(data, list):
                    data = [data] if data else []

                # Catastrophic checks only
                for item in data[-20:]:
                    if not isinstance(item, dict):
                        continue
                    bounds = cfg.get("bounds", {})

                    # Perp oversize
                    if "size_usd" in item:
                        sz = item.get("size_usd", 0) or 0
                        max_perp = bounds.get("perps", {}).get("max_size_usd", 200)
                        if sz > max_perp * 3:
                            log_finding(
                                "egress_perp_oversize", "HIGH",
                                f"Perp size ${sz} exceeds 3x max (${max_perp}*3)",
                                "Verify the agent's intent"
                            )

            await asyncio.sleep(interval)
        except Exception as e:
            print(f"[warden] egress err: {e}")
            await asyncio.sleep(180)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 3a: CANARY TOKENS
# ═══════════════════════════════════════════════════════════════════════════

async def canary_monitor():
    cfg = load_config()
    if not cfg.get("canary_enabled", True):
        print("[warden] canary disabled in config")
        return

    canary_env = os.path.join(CANARY_DIR, ".env.backup")
    canary_pos = os.path.join(CANARY_DIR, "positions_backup.json")

    if not os.path.exists(canary_env):
        with open(canary_env, "w", encoding="utf-8") as f:
            f.write(
                "# backup credentials\n"
                "PRIVATE_KEY=CANARY_SOLANA_KEY_DO_NOT_USE_5xKp8mN2vQr\n"
                "HELIUS_API_KEY=CANARY_HELIUS_KEY_DO_NOT_USE_7yRt9nM3\n"
                "TELEGRAM_TOKEN=CANARY_TG_TOKEN_DO_NOT_USE_8351388728\n"
                "X_Bearer_Token=CANARY_BEARER_DO_NOT_USE_AAAAAx9Kp\n"
            )
        print(f"[warden] planted canary at {canary_env}")

    if not os.path.exists(canary_pos):
        save_json(canary_pos, {
            "CANARY_MINT_111": {
                "ticker": "CANARY", "amount_sol": 10.0,
                "entry_price": 0.001, "canary": True
            }
        })
        print(f"[warden] planted canary position at {canary_pos}")

    print("[warden] canary monitor active")
    last_access = {}
    # S2: positions_backup.json catches benign JSON scans (FP-heavy). Cool it down so it
    # alerts at most once per window; the .env.backup CREDENTIAL canary stays HOT (no cooldown).
    _canary_cooldown = {canary_pos: cfg.get("canary_positions_cooldown_seconds", 6 * 3600)}
    _canary_last_alert = {}

    while True:
        try:
            await asyncio.sleep(300)
            for canary_path in (canary_env, canary_pos):
                if not os.path.exists(canary_path):
                    continue
                atime = os.path.getatime(canary_path)
                last = last_access.get(canary_path, atime)
                if atime > last + 10:
                    _cd = _canary_cooldown.get(canary_path, 0)
                    if _cd and (time.time() - _canary_last_alert.get(canary_path, 0)) < _cd:
                        last_access[canary_path] = atime  # baseline moved; skip the noisy re-alert
                        continue
                    _canary_last_alert[canary_path] = time.time()
                    trips = load_json(CANARY_LOG, [])
                    trips.append({
                        "timestamp":   datetime.now(EST).isoformat(),
                        "file":        canary_path,
                        "atime":       atime,
                        "attack_type": "canary_trip",
                    })
                    save_json(CANARY_LOG, trips)
                    log_attack("canary", canary_path, "canary_trip",
                               {"file": canary_path, "atime": atime})
                    await notify(
                        f"🔴🔥 <b>Warden: CANARY TRIPPED</b>\n"
                        f"File: {os.path.basename(canary_path)}\n"
                        f"🔥 PIN THIS — Credential file was accessed. Possible compromise.\n"
                        f"Review who has access to {CANARY_DIR} IMMEDIATELY.\n"
                        f"Warden will NOT auto-stop bots. Halt manually if confirmed compromise."
                    )
                    # Also write P1 inbox packet
                    write_inbox_packet(
                        priority="P1", source="warden_canary",
                        slug=f"canary_tripped_{os.path.basename(canary_path)}",
                        content=(
                            f"## Canary Trip Detected\n\n"
                            f"**File accessed:** `{canary_path}`\n"
                            f"**Time:** {datetime.now(EST).isoformat()}\n\n"
                            f"### What this means\n"
                            f"A planted decoy credential file was accessed. "
                            f"Either the agent/the owner legitimately opened it, or the perimeter "
                            f"was breached.\n\n"
                            f"### Action\n"
                            f"1. Verify if the owner accessed this file\n"
                            f"2. If NOT the owner: ESCALATE — possible compromise\n"
                            f"3. Review process list: who has access to `{CANARY_DIR}`\n"
                            f"4. If breach confirmed, engage WARDEN LOCKDOWN\n"
                        )
                    )
                last_access[canary_path] = atime
        except Exception as e:
            print(f"[warden] canary monitor err: {e}")
            await asyncio.sleep(600)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 3b: WALLET ANOMALY WATCHER
# ═══════════════════════════════════════════════════════════════════════════

RPC_PUBLIC_FALLBACK = os.getenv("RPC_PUBLIC_FALLBACK", "https://api.mainnet-beta.solana.com")


def _wallet_rpc_urls() -> list:
    """Free RPC first, paid Helius last."""
    urls = [u for u in (RPC_PUBLIC_FALLBACK,) if u]
    if HELIUS_API_KEY:
        urls.append(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}")
    return urls


async def wallet_anomaly_watcher():
    if not WALLET_ADDRESS or not _wallet_rpc_urls():
        print("[warden] wallet watcher: missing WALLET_ADDRESS or any RPC, skipping")
        return

    cfg = load_config()
    threshold = cfg.get("wallet_unknown_tx_threshold", 2)
    cooldown_min = cfg.get("wallet_alert_cooldown_minutes", 30)

    print(f"[warden] wallet watcher active — {WALLET_ADDRESS[:8]}...")

    last_sig = None
    last_alert = 0
    unknown_window = []

    while True:
        try:
            await asyncio.sleep(60)
            async with httpx.AsyncClient(timeout=15) as client:
                # Credit thrift (2026-10-14): free RPC first, paid Helius only if it fails.
                _body = {
                    "jsonrpc": "2.0", "id": 1,
                    "method":  "getSignaturesForAddress",
                    "params":  [WALLET_ADDRESS, {"limit": 5}],
                }
                r = None
                for _url in _wallet_rpc_urls():
                    try:
                        r = await client.post(_url, json=_body)
                        if r.status_code == 200 and "result" in r.json():
                            break
                    except Exception:
                        r = None
                if r is None or r.status_code != 200:
                    continue
                sigs = r.json().get("result", [])
                if not sigs:
                    continue

                latest_sig = sigs[0].get("signature", "")
                if last_sig is None:
                    last_sig = latest_sig
                    continue
                if latest_sig == last_sig:
                    continue

                new_txs = []
                for s in sigs:
                    if s.get("signature") == last_sig:
                        break
                    new_txs.append(s)

                if not new_txs:
                    last_sig = latest_sig
                    continue

                # Cross-check: did perps just act? perp_positions.json / perp_positions_archive.jsonl
                # change on every open and close, so a fresh mtime explains a fresh tx.
                recent_trade_activity = False
                for _tl in ("perp_positions.json", "perp_positions_archive.jsonl", "perps.log"):
                    _p = os.path.join(BOT_LOGS, _tl)
                    if os.path.exists(_p):
                        try:
                            if time.time() - os.path.getmtime(_p) < 120:
                                recent_trade_activity = True
                                break
                        except Exception:
                            pass

                perp_pos = load_json(os.path.join(BOT_LOGS, "perp_positions.json"), {})  # PLURAL venue of record (never perp_position singular / drift_balance)
                known_sigs = set()
                # perp_positions.json (plural) may be {market: {...}}, a list, or a single {...}
                _perp_rows = []
                if isinstance(perp_pos, dict):
                    _perp_rows = [v for v in perp_pos.values() if isinstance(v, dict)] or ([perp_pos] if perp_pos else [])
                elif isinstance(perp_pos, list):
                    _perp_rows = [v for v in perp_pos if isinstance(v, dict)]
                for _pp in _perp_rows:
                    known_sigs.add(_pp.get("tx_sig", ""))
                    known_sigs.add(_pp.get("buy_tx", ""))
                truly_unknown = [t for t in new_txs if t.get("signature") not in known_sigs]
                if recent_trade_activity:
                    truly_unknown = []  # active trading, ignore

                unknown_window.extend(truly_unknown)
                # Keep only last 10 min
                cutoff = time.time() - 600
                unknown_window = [
                    u for u in unknown_window
                    if u.get("blockTime", 0) > cutoff
                ]

                if len(unknown_window) >= threshold:
                    if time.time() - last_alert > cooldown_min * 60:
                        log_finding(
                            "wallet_anomaly", "CRITICAL",
                            f"{len(unknown_window)} unknown txs in 10min window",
                            "Verify legitimate the owner/the agent tx; if not, possible compromise"
                        )
                        await notify(
                            f"🔴 <b>Warden: WALLET ANOMALY</b>\n"
                            f"{len(unknown_window)} unknown txs in 10min\n"
                            f"Wallet: {WALLET_ADDRESS[:12]}...\n"
                            f"Latest sig: {unknown_window[0].get('signature','?')[:30]}...\n"
                            f"Verify or LOCKDOWN."
                        )
                        write_inbox_packet(
                            priority="P1", source="warden_wallet",
                            slug="wallet_anomaly",
                            content=(
                                f"## Wallet Anomaly Detected\n\n"
                                f"**Wallet:** `{WALLET_ADDRESS}`\n"
                                f"**Unknown tx count:** {len(unknown_window)} in 10min window\n"
                                f"**Threshold:** {threshold}\n\n"
                                f"### Recent unknown signatures\n" +
                                "\n".join(f"- `{u.get('signature','?')[:80]}`"
                                          for u in unknown_window[:5]) +
                                f"\n\n### Action\n"
                                f"1. Verify these txs were the owner-initiated (Solscan check)\n"
                                f"2. If NOT recognized: WARDEN LOCKDOWN immediately\n"
                                f"3. Review wallet permissions / connected dapps\n"
                            )
                        )
                        last_alert = time.time()

                last_sig = latest_sig

        except Exception as e:
            # Capture the exception TYPE even when str(e) is empty — a bare
            # exception (e.g. some httpx/network errors) prints nothing otherwise,
            # leaving "[warden] wallet watcher err:" with no diagnostic at all.
            _msg = str(e) or repr(e) or "(no message)"
            print(f"[warden] wallet watcher err: {type(e).__name__}: {_msg}")
            await asyncio.sleep(120)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 3c: DEAD MAN'S SWITCH
# ═══════════════════════════════════════════════════════════════════════════

async def dead_mans_switch():
    """v3.3: REFRAMED — this is now an SYSTEM-WIDE activity backstop, not a
    per-bot liveness check. Per-bot liveness moved to bot_heartbeat_monitor().
    
    Pulse files measure "is the system DOING SOMETHING" — trades happening,
    macro updating, signals firing. During quiet markets these can be silent
    for many hours legitimately. So the threshold is now 12h (was 60min) and
    it only fires when MULTIPLE pulse files have been silent simultaneously
    for >12h — that's an system-wide stoppage, not normal market quiet.
    
    For per-bot liveness ("is perps.py running and healthy"), use
    bot_heartbeat_monitor() below.
    """
    print("[warden] dead man's switch active (v3.3 — 12h system-wide backstop)")
    cfg = load_config()
    max_silence_min = cfg.get("dead_mans_switch_max_silence_minutes", 720)  # 12h

    # System-wide activity pulse files — these track "is the system still doing
    # things in the world" not "is any one bot alive."
    # Sprint 65: retired trades.log + wallet_balance.json — both dead infrastructure
    # (~83 days silent, nothing writes them anymore) which made this fire false system-
    # halt alarms. Replaced with files that ARE actively written and genuinely signal
    # activity: the inbox cron audit (the agent processing packets every pass), the perps
    # decision snapshots and the perps heartbeat.
    pulse_files = [
        os.path.join(BOT_LOGS, "inbox_cron_audit.log"),
        os.path.join(BOT_LOGS, "perp_decision_snapshots.jsonl"),
        os.path.join(BOT_LOGS, "perps_bot_heartbeat.json"),
    ]

    # Track last alert time per file so we don't spam (24h cooldown)
    last_alerted = {}
    ALERT_COOLDOWN_SEC = 24 * 3600

    while True:
        try:
            await asyncio.sleep(1800)  # check every 30min (was 15)
            now = time.time()
            silent = []
            for p in pulse_files:
                if not os.path.exists(p):
                    continue
                age_min = (now - os.path.getmtime(p)) / 60
                if age_min > max_silence_min:
                    last = last_alerted.get(p, 0)
                    if now - last > ALERT_COOLDOWN_SEC:
                        silent.append((os.path.basename(p), int(age_min)))

            # Only fire if 2+ pulse files silent simultaneously (system-wide
            # stoppage, not just one component being quiet). And only after
            # cooldown.
            if len(silent) >= 2:
                log_finding(
                    "dead_mans_switch", "HIGH",
                    f"SYSTEM-WIDE: {len(silent)} pulse files silent >{max_silence_min/60:.0f}h: " +
                    ", ".join(f"{n}({m}m)" for n, m in silent),
                    "Check whether all bots are running. Likely a power outage, "
                    "network issue, or whole-system crash."
                )
                await notify(
                    f"🟡 <b>Warden: SYSTEM-WIDE ACTIVITY HALT</b>\n"
                    f"{len(silent)} pulse files silent >{max_silence_min/60:.0f}h\n" +
                    "\n".join(f"  {n}: {m//60}h silent" for n, m in silent) +
                    f"\n\nThis is the dead-man-switch backstop. For per-bot health "
                    f"see bot_heartbeat_monitor alerts."
                )
                for n, _ in silent:
                    for p in pulse_files:
                        if os.path.basename(p) == n:
                            last_alerted[p] = now
        except Exception as e:
            print(f"[warden] dead_mans err: {e}")
            await asyncio.sleep(1800)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 3d-bis: BOT HEARTBEAT MONITOR
# ═══════════════════════════════════════════════════════════════════════════
# This is the PROPER per-bot liveness check. Reads each bot's heartbeat file
# from bot_logs/<name>_heartbeat.json with a per-bot threshold (some bots
# legitimately run hourly, some run every 14 days). Fires alerts ONLY when
# a bot's heartbeat is stale beyond its specific threshold.
#
# Why this exists: the old dead_mans_switch fired Telegram alerts every hour
# during quiet markets because the 60min threshold + activity-based detection
# was wrong. Heartbeats are the right signal — they say "this bot is alive"
# regardless of whether the market is doing anything.

# Alias map for bots whose heartbeat filename differs from <name>_heartbeat.json
HEARTBEAT_FILE_ALIASES_WARDEN = {
    # Add an entry here only if a bot writes a NON-standard heartbeat filename.
}


def _resolve_bot_heartbeat_path(bot_name: str) -> str:
    aliased = HEARTBEAT_FILE_ALIASES_WARDEN.get(bot_name)
    if aliased:
        return os.path.join(BOT_LOGS, aliased)
    return os.path.join(BOT_LOGS, f"{bot_name}_heartbeat.json")


# Bot offline alert cooldown — don't ping same bot twice in 24h
_BOT_OFFLINE_ALERT_STATE = os.path.join(BOT_LOGS, "warden_offline_alert_state.json")


def _hb_user_stopped(hb_path: str) -> bool:
    """True when the heartbeat carries stop_reason=user_stopped (intentional stop)."""
    try:
        hb = load_json(hb_path, {}) or {}
        return isinstance(hb, dict) and hb.get("stop_reason") == "user_stopped"
    except Exception:
        return False




async def bot_heartbeat_monitor():
    """v3.3 — Per-bot liveness check using bot_logs/<name>_heartbeat.json.
    
    Reads cfg['bot_heartbeat_thresholds_minutes'] for per-bot threshold.
    When a bot's heartbeat exceeds its threshold:
      - Log a finding (severity HIGH if critical bot, MEDIUM otherwise)
      - Write a P1 inbox packet (if critical) or P2 (otherwise) — but only
        if it's been > cooldown since the last alert for this same bot
      - Trigger medic heal attempt (medic decides whether bot is fixable)
    """
    cfg = load_config()
    if not cfg.get("bot_heartbeat_monitor_enabled", True):
        print("[warden] bot_heartbeat_monitor disabled")
        return
    
    print("[warden] bot heartbeat monitor active (v3.3 — per-bot liveness)")
    interval = cfg.get("bot_heartbeat_check_interval_seconds", 600)
    cooldown_h = cfg.get("bot_heartbeat_alert_cooldown_hours", 24)
    thresholds = cfg.get("bot_heartbeat_thresholds_minutes", {})
    grace_floor = cfg.get("bot_heartbeat_grace_minutes_floor", 15)
    grace_pct   = cfg.get("bot_heartbeat_grace_pct", 0.05)
    
    while True:
        try:
            await asyncio.sleep(interval)
            
            # Honor lockdown
            if is_lockdown_active():
                continue
            
            alert_state = load_json(_BOT_OFFLINE_ALERT_STATE, {}) or {}
            now = time.time()
            offline_bots = []
            
            parked = set(cfg.get("parked_bots", []) or [])

            for bot_name, threshold_min in thresholds.items():
                # PARKED = dormant by design. A stale heartbeat is the EXPECTED
                # state, so it is not a finding and never a packet. Logged once
                # per cycle so the suppression is visible rather than silent -
                # warden not paging must itself be observable.
                if bot_name in parked:
                    if bot_name in alert_state:
                        alert_state.pop(bot_name, None)
                    print(f"[warden] heartbeat_monitor: {bot_name} PARKED - offline alerts suppressed")
                    continue

                hb_path = _resolve_bot_heartbeat_path(bot_name)
                if not os.path.exists(hb_path):
                    # Heartbeat file never existed — bot may have never booted.
                    # Don't spam; log once per session and move on.
                    continue
                try:
                    age_min = (now - os.path.getmtime(hb_path)) / 60
                except OSError:
                    continue
                
                # S6 hysteresis: require threshold + grace before paging, so a bot sitting a
                # few minutes over threshold (e.g. 1445 vs 1440) does not flap. grace = max(floor, pct*threshold).
                grace_min = max(grace_floor, threshold_min * grace_pct)
                if _hb_user_stopped(hb_path):
                    # Owner stopped it on purpose (stop_reason=user_stopped): not offline.
                    alert_state.pop(bot_name, None)
                    continue
                if age_min <= threshold_min + grace_min:
                    # Fresh enough (within threshold + grace) - clear any prior alert state
                    if bot_name in alert_state:
                        alert_state.pop(bot_name, None)
                    continue
                
                # Heartbeat is stale beyond per-bot threshold
                last_alerted = alert_state.get(bot_name, {}).get("ts", 0)
                cooldown_sec = cooldown_h * 3600
                if (now - last_alerted) < cooldown_sec:
                    continue  # already alerted recently, wait for cooldown
                
                offline_bots.append({
                    "bot":          bot_name,
                    "age_minutes":  round(age_min, 1),
                    "threshold":    threshold_min,
                    "heartbeat_path": hb_path,
                })
                alert_state[bot_name] = {
                    "ts":            now,
                    "age_minutes":   round(age_min, 1),
                    "threshold_min": threshold_min,
                }
            
            save_json(_BOT_OFFLINE_ALERT_STATE, alert_state)
            
            if not offline_bots:
                continue
            
            # Classify severity per-bot
            # CRITICAL: perps_bot (execution)
            # HIGH:     security_warden peers (agent_autoagent, code_tuner)
            # MEDIUM:   anything else listed in the thresholds
            critical_set = {"perps_bot"}
            high_set = {"agent_autoagent", "code_tuner"}
            
            critical_offline = [b for b in offline_bots if b["bot"] in critical_set]
            high_offline = [b for b in offline_bots if b["bot"] in high_set]
            medium_offline = [b for b in offline_bots
                              if b["bot"] not in critical_set and b["bot"] not in high_set]
            
            for b in critical_offline:
                log_finding(
                    "bot_heartbeat_stale", "CRITICAL",
                    f"CRITICAL bot {b['bot']} heartbeat stale: {b['age_minutes']}min "
                    f"(threshold {b['threshold']}min)",
                    f"Medic will attempt heal. If multiple criticals offline, the system is degraded."
                )
            for b in high_offline:
                log_finding(
                    "bot_heartbeat_stale", "HIGH",
                    f"Bot {b['bot']} heartbeat stale: {b['age_minutes']}min "
                    f"(threshold {b['threshold']}min)",
                    f"Medic will attempt heal."
                )
            for b in medium_offline:
                log_finding(
                    "bot_heartbeat_stale", "MEDIUM",
                    f"Bot {b['bot']} heartbeat stale: {b['age_minutes']}min "
                    f"(threshold {b['threshold']}min)",
                    f"Medic will attempt heal."
                )
            
            # Telegram alert ONLY for critical bots — keeps the noise level low
            if critical_offline:
                lines = [f"  🔴 {b['bot']}: {b['age_minutes']/60:.1f}h stale (threshold {b['threshold']/60:.1f}h)"
                         for b in critical_offline]
                await notify(
                    f"🚨 <b>Warden: CRITICAL bot(s) offline</b>\n" +
                    "\n".join(lines) +
                    f"\n\nMedic firing. Check logs."
                )
            
            # Write P1/P2 packet to the agent's inbox + trigger medic
            for b in offline_bots:
                bot_name = b["bot"]
                priority = "P1" if bot_name in critical_set else "P2"
                content = (
                    f"# Bot Offline — {bot_name}\n\n"
                    f"**Heartbeat age:** {b['age_minutes']/60:.1f}h "
                    f"(threshold {b['threshold']/60:.1f}h)\n"
                    f"**Heartbeat file:** `{b['heartbeat_path']}`\n\n"
                    f"## Medic action\n"
                    f"Medic will attempt heal on next cycle. If heal fails 3x in 6h, "
                    f"doctor will gather the evidence and route it to you to diagnose "
                    f"and fix (code, restart, or browse).\n\n"
                    f"## Your action\n"
                    f"- Check `bot_logs/{bot_name}.log` for crash trace\n"
                    f"- Or run `python {bot_name}.py` manually to see error\n"
                    f"- If process is just hung, the owner restarts `python bots/perps.py` (it restarts crashed sub-bots itself; see bot_logs/perps_launcher.log)\n"
                )
                write_inbox_packet(priority, "warden", f"bot_offline_{bot_name}", content)
                
                # Trigger medic if it's a known whitelisted bot
                bot_filename = f"{bot_name}.py"
                if bot_filename in AGENT_BOTS_WHITELIST and cfg.get("medic_enabled", True):
                    # Medic will pick this up via its own log scan, but we can also
                    # nudge it directly by appending a heartbeat-stale event to errors.json
                    # so medic's log-scan path notices.
                    try:
                        err_path = os.path.join(BOT_LOGS, "errors.json")
                        existing = load_json(err_path, []) or []
                        if not isinstance(existing, list):
                            existing = []
                        existing.append({
                            "ts":     datetime.now(EST).isoformat(),
                            "source": "warden_bot_heartbeat_monitor",
                            "bot":    bot_filename,
                            "error":  f"heartbeat_stale: {b['age_minutes']:.1f}min "
                                     f"(threshold {b['threshold']}min) — investigate or restart",
                        })
                        save_json(err_path, existing[-500:])
                    except Exception:
                        pass
        
        except Exception as e:
            print(f"[warden] bot_heartbeat err: {e}")
            await asyncio.sleep(600)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 3e: SILENT FAILURE MONITOR (v3.3 — venue-offline-style problems)
# ═══════════════════════════════════════════════════════════════════════════
# A bot can be ALIVE (heartbeat fresh) but doing NOTHING in the world. This
# happens when an external service goes down silently — venue offline, RPC
# borked, API auth expired. The bot keeps running its loop, fails on every
# API call, but heartbeat is fresh.
#
# This monitor watches each execution bot's expected output rate. If perps_bot
# is alive but produced ZERO trade attempts for >12h despite signals being
# good, that's a silent failure. Doctor investigates.

# Source files where output rate is observable per bot
_OUTPUT_RATE_FILES = {
    "perps_bot":    os.path.join(BOT_LOGS, "perps.log"),
}

# ════════════════════════════════════════════════════════════════════════════
# WORK EVIDENCE + STAND-DOWN + ATTEMPT LOGS — 2026-07-26, AGENT'S SPEC
# ════════════════════════════════════════════════════════════════════════════
# Replaces the old fill-log sensor outright (his instruction: "replace the
# sensor, dont stack a phantom gate"). The old rule was heartbeat-fresh + fill
# log stale >12h → HIGH. But fill logs only grow on FILLS, so a bot that is
# researching, monitoring an open book, or deliberately stood down produces
# zero fills for days and trips it every time. No fills is not no pulse.
#
# The agent's split, adopted verbatim:
#   work_staleness    — is it DOING anything?  (hung loop / dead external brain)
#   exec_failure_rate — is it TRYING and BLEEDING?  (the 5-landed/17-failed shape)
# Two different problems with opposite remedies. They no longer share a name.

# Newest mtime among each set = the bot did real work. Fill logs are NEVER a
# sole signal — that was the whole bug.
_WORK_EVIDENCE = {
    "perps_bot": {
        "primary": ["perp_decision_snapshots.jsonl"],
        # A work-evidence file must be written BY THE BOT IT VOUCHES FOR, never a
        # shared file. Stand-down is read from risk_state.json as a BOOLEAN field
        # (block_new_perp), never as mtime liveness.
        "also":    ["perp_feed_snapshot.json"],
        "idle_hours": 4.0,      # hourly decision cadence; 4h = multiple missed cycles
    },
}

# ════════════════════════════════════════════════════════════════════════════
# EXEC SOURCES — the agent's verified markers (2026-07-26). STRUCTURED, not greps.
# ════════════════════════════════════════════════════════════════════════════
# 2h / >=3 attempts / >50% failed for perps. 0 attempts in window = NO finding,
# ever: never invent a rate.
#
# Note what he ruled out: perps.log is FILL-ONLY SUCCESSES, so parsing it for a
# failure rate is structurally incapable of seeing failures. Same reason fill
# logs aren't proof of life. Structured audit trails only.
_EXEC_SOURCES = {
    "perps_bot": {
        "primary":  "perps_venue_audit.jsonl",
        "fallback": None,                           # perps.log is fill-only: unusable for a rate
        "window_h": 2.0, "min_attempts": 3, "fail_ratio": 0.5,
        "attempt_ops":   ("open_position", "close_position"),
        "ignore_ops":    ("open_aborted", "idl_loaded"),
    },
}


def _tail_lines(path: str, max_bytes: int = 600_000):
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            return f.readlines()
    except Exception:
        return []


def _row_ts(row: dict):
    """Best-effort timestamp from a structured row. Returns epoch seconds or None."""
    for k in ("ts", "timestamp", "at", "time", "_ts", "created_at", "emitted_at"):
        v = row.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v) if float(v) > 1e9 else None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
    return None


def _jsonl_rows_in_window(path: str, window_h: float):
    """(rows, window_exact). If rows carry no parseable timestamp we still return
    the tail, flagged inexact — better a slightly wide window than no sensor."""
    rows, exact, cutoff = [], True, time.time() - window_h * 3600
    any_ts = False
    for ln in _tail_lines(path):
        ln = ln.strip()
        if not ln or not ln.startswith("{"):
            continue
        try:
            row = json.loads(ln)
        except Exception:
            continue
        ts = _row_ts(row)
        if ts is not None:
            any_ts = True
            if ts < cutoff:
                continue
        rows.append(row)
    if not any_ts:
        exact = False
    return rows, exact


def _exec_counts(bot_name: str):
    """(attempts, failures, detail) per the agent's markers. Returns (0,0,...) when
    the source is missing or matches nothing — an unreadable source is UNKNOWN,
    never a pass and never a fail."""
    spec = _EXEC_SOURCES.get(bot_name) or {}
    path = os.path.join(BOT_LOGS, spec.get("primary") or "")
    detail = {"source": spec.get("primary"), "window_h": spec.get("window_h")}
    if not spec.get("primary") or not os.path.exists(path):
        detail["status"] = "source_missing"
        return (0, 0, detail)

    rows, exact = _jsonl_rows_in_window(path, spec.get("window_h", 2.0))
    detail["window_exact"] = exact
    attempts = failures = 0

    if bot_name == "perps_bot":
        for r in rows:
            op = str(r.get("operation") or r.get("event") or "").lower()
            if op in spec["ignore_ops"] or op not in spec["attempt_ops"]:
                continue
            attempts += 1
            if r.get("success") is False:
                failures += 1

    if attempts == 0:
        detail["status"] = "no_attempts_in_window"    # healthy idle or stood down
    return (attempts, failures, detail)


def _read_json_quiet(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _stand_down_state(bot_name: str) -> tuple:
    """Returns (stood_down: bool, reason: str). Per the agent's table.
    """
    try:
        risk = _read_json_quiet(os.path.join(BOT_LOGS, "risk_state.json"))
        if bot_name == "perps_bot":
            if risk.get("block_new_perp") is True:
                # Entry-silence is expected — but an OPEN BOOK still has to be
                # managed, so work evidence remains required in that case.
                open_book = False
                try:
                    positions = _read_json_quiet(os.path.join(BOT_LOGS, "perp_positions.json"))
                    open_book = any(isinstance(v, dict) and v.get("direction") for v in positions.values())
                except Exception:
                    pass
                if not open_book:
                    return (True, "risk_state.block_new_perp=true and no open book")
                return (False, "block_new_perp=true BUT open position needs managing")
            return (False, "")
    except Exception as e:
        print(f"[warden] stand-down read err for {bot_name}: {e}")
    return (False, "")


def _newest_work_evidence(bot_name: str) -> tuple:
    """(filename, age_hours) of the newest work artifact, or (None, None).
    Primary and 'also' files are pooled — any fresh one proves work."""
    spec = _WORK_EVIDENCE.get(bot_name)
    if not spec:
        return (None, None)
    newest_name, newest_mtime = None, 0.0
    for fn in list(spec.get("primary", [])) + list(spec.get("also", [])):
        fp = os.path.join(BOT_LOGS, fn)
        try:
            m = os.path.getmtime(fp)
        except OSError:
            continue
        if m > newest_mtime:
            newest_mtime, newest_name = m, fn
    if not newest_name:
        return (None, None)
    return (newest_name, (time.time() - newest_mtime) / 3600.0)


async def silent_failure_monitor():
    """WORK STALENESS (2026-07-26 — the agent's spec; replaces the fill-log sensor).

    Finding name is `work_staleness`, not `silent_failure`/`OUTPUT_RATE`. It
    answers exactly one question: the process is alive — is it DOING anything?

      healthy idle : hb fresh + work fresh              → no alert
      stood down   : stand-down flag true               → suppress entirely
      work stale   : hb fresh + work stale + not stood  → work_staleness HIGH

    Function name kept so the task registration and doctor path stay wired.
    """
    cfg = load_config()
    print("[warden] work_staleness monitor active (v3.7 — the agent's spec, fill logs are not proof of life)")
    interval = cfg.get("silent_failure_check_interval_seconds", 1800)   # 30min per spec
    cooldown = cfg.get("work_staleness_cooldown_seconds", 6 * 3600)     # 6h per spec (24h was too sleepy)

    last_alerted = {}

    while True:
        try:
            await asyncio.sleep(interval)
            if is_lockdown_active():
                continue

            now = time.time()
            for bot_name, spec in _WORK_EVIDENCE.items():
                hb_path = _resolve_bot_heartbeat_path(bot_name)
                if not os.path.exists(hb_path):
                    continue
                try:
                    hb_age_min = (now - os.path.getmtime(hb_path)) / 60
                except OSError:
                    continue
                if hb_age_min >= 30:
                    continue      # not alive-with-no-work; bot_heartbeat_monitor owns that

                stood_down, sd_reason = _stand_down_state(bot_name)
                if stood_down:
                    # Suppress entirely — never a finding, never INFO spam.
                    print(f"[warden] work_staleness: {bot_name} STANDING DOWN ({sd_reason}) — suppressed")
                    continue

                work_name, work_age_h = _newest_work_evidence(bot_name)
                idle_hours = float(spec.get("idle_hours", 4.0))

                if work_name is not None and work_age_h is not None and work_age_h < idle_hours:
                    continue      # doing work; whatever the fill log says

                if (now - last_alerted.get(bot_name, 0)) < cooldown:
                    continue
                last_alerted[bot_name] = now

                detail = (f"newest work artifact {work_name} is {work_age_h:.1f}h old "
                          f"(threshold {idle_hours}h)") if work_name else \
                         (f"NO work artifacts exist at all "
                          f"({', '.join(spec.get('primary', []) + spec.get('also', []))})")
                log_finding(
                    "work_staleness", "HIGH",
                    f"{bot_name} ALIVE (heartbeat {hb_age_min:.0f}min) but NOT WORKING — {detail}. "
                    f"Not a stand-down. Hung loop or dead external brain (RPC/API/LLM).",
                    "Doctor routes the evidence. Check the bot's inbound data source before its code."
                )
                asyncio.create_task(doctor_investigate(
                    bot_name=bot_name,
                    symptom="work_staleness",
                    context={
                        "heartbeat_age_min":   round(hb_age_min, 1),
                        "newest_work_artifact": work_name,
                        "work_artifact_age_h": (round(work_age_h, 2) if work_age_h is not None else None),
                        "idle_threshold_hours": idle_hours,
                        "stand_down_checked":  True,
                        "note": "fill logs deliberately excluded — they are not proof of life",
                    }
                ))
        except Exception as e:
            print(f"[warden] work_staleness err: {e}")
            await asyncio.sleep(1800)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 5b: EXEC FAILURE RATE (2026-07-26 — the agent's spec, sensor B)
# ═══════════════════════════════════════════════════════════════════════════
# "bot is trying and bleeding." The opposite problem from work_staleness: the
# bot IS producing attempts, and they're failing. This is the 5-landed/17-failed
# shape, which a staleness check can never see because the log IS growing.
#
# (Inferred substring markers removed 2026-07-26 — superseded by the agent's
# verified structured sources in _EXEC_SOURCES above.)



async def exec_failure_rate_monitor():
    """Sensor B: is the bot TRYING AND BLEEDING? The agent's locked thresholds.

    Distinct from work_staleness by design — staleness means it stopped trying,
    this means it can't land. Opposite remedies, so they never share a name.
    """
    cfg = load_config()
    print("[warden] exec_failure_rate monitor active (v3.7 — the agent's structured markers)")
    interval = cfg.get("exec_failure_rate_check_interval_seconds", 1800)
    cooldown = cfg.get("exec_failure_rate_cooldown_seconds", 6 * 3600)
    last_alerted = {}

    while True:
        try:
            await asyncio.sleep(interval)
            if is_lockdown_active():
                continue
            now = time.time()
            for bot_name, spec in _EXEC_SOURCES.items():
                stood_down, sd_reason = _stand_down_state(bot_name)
                if stood_down:
                    continue          # not attempting new business by design

                attempts, failures, detail = _exec_counts(bot_name)

                if attempts < spec["min_attempts"]:
                    continue          # 0 attempts = no rate. never invent one.
                ratio = failures / attempts
                if ratio <= spec["fail_ratio"]:
                    continue
                if (now - last_alerted.get(bot_name, 0)) < cooldown:
                    continue
                last_alerted[bot_name] = now
                log_finding(
                    "exec_failure_rate", "HIGH",
                    f"{bot_name} is TRYING AND BLEEDING — {failures}/{attempts} attempts failed "
                    f"({ratio*100:.0f}%) in {spec['window_h']}h (source: {detail.get('source')}"
                    f"{'' if detail.get('window_exact', True) else ', window approximate'}). "
                    f"Execution-path problem, NOT an edge problem.",
                    "Check the send/confirm path (RPC, endpoints, slippage, size) before touching "
                    "entry filters or scoring."
                )
                asyncio.create_task(doctor_investigate(
                    bot_name=bot_name, symptom="exec_failure_rate",
                    context={"attempts": attempts, "failures": failures,
                              "fail_ratio": round(ratio, 3), **detail}))
        except Exception as e:
            print(f"[warden] exec_failure_rate err: {e}")
            await asyncio.sleep(1800)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 3d: API HEALTH MONITOR
# ═══════════════════════════════════════════════════════════════════════════

async def api_health_monitor():
    print("[warden] api health monitor active")
    cfg = load_config()
    interval = cfg.get("api_health_check_interval_seconds", 1800)

    while True:
        try:
            await asyncio.sleep(interval)
            checks = []

            # Helius RPC
            if HELIUS_API_KEY:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.post(
                            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                            json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"}
                        )
                        checks.append(("helius", r.status_code == 200))
                except Exception:
                    checks.append(("helius", False))

            # xAI grok (only when the SI brain lists grok)
            if XAI_API_KEY and _si_gate.allows("grok"):
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        r = await client.post(
                            "https://api.x.ai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {XAI_API_KEY}",
                                     "Content-Type": "application/json"},
                            json={"model": XAI_MODEL,
                                  "messages": [{"role": "user", "content": "ping"}],
                                  "max_tokens": 5}
                        )
                        checks.append(("grok", r.status_code == 200))
                except Exception:
                    checks.append(("grok", False))

            failures = [name for name, ok in checks if not ok]
            if failures:
                log_finding(
                    "api_health", "MEDIUM" if len(failures) == 1 else "HIGH",
                    f"API failures: {', '.join(failures)}",
                    "Check rate limits / outage"
                )
                if len(failures) >= 2:
                    await notify(
                        f"🟡 <b>Warden: API health</b>\n"
                        f"Failed: {', '.join(failures)}\n"
                        f"Multiple failures may indicate broader issue."
                    )
        except Exception as e:
            print(f"[warden] api_health err: {e}")
            await asyncio.sleep(3600)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 4: COUNTER INTEL (passive — populated by other layers)
# ═══════════════════════════════════════════════════════════════════════════

def get_attack_summary() -> str:
    intel = load_json(COUNTER_INTEL, [])
    if not intel:
        return "No attacks logged."
    today = datetime.now(EST).date().isoformat()
    today_items = [e for e in intel if (e.get("timestamp") or "").startswith(today)]
    by_type = {}
    for e in today_items:
        t = e.get("attack_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    return f"Today: {len(today_items)} attacks | " + " | ".join(
        f"{k}={v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1])
    )


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 5: FEED CYCLE INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════

# The drop's feeds (bots/feeds, bots/feed_hook.py) write these. With FEED_CADENCE=manual
# (the default) they only refresh when the agent or a packet build asks, so a MISSING
# file is not a finding; a file that exists and is older than FEED_MAX_STALE_HOURS is.
FEED_FILES = {
    "flow_cache":         os.path.join(BOT_LOGS, "flow_cache.json"),
    "feed_router_state":  os.path.join(BOT_LOGS, "feed_router_state.json"),
    "news_catalysts":     os.path.join(BOT_LOGS, "news_catalysts.json"),
    "kalshi_implied":     os.path.join(BOT_LOGS, "kalshi_implied_probs.json"),
    "polymarket_markets": os.path.join(BOT_LOGS, "polymarket_markets.json"),
}
FEED_MAX_STALE_HOURS = float(_envv("WARDEN_FEED_MAX_STALE_HOURS", "24") or 24)


async def feed_integrity_monitor():
    """Watches the feed outputs' freshness. MEDIUM finding when stale; never halts bots
    (packets carry their own per-lane `stale` flags)."""
    print(f"[warden] feed integrity active (stale > {FEED_MAX_STALE_HOURS:.0f}h)")
    cfg = load_config()
    interval = cfg.get("feed_check_interval_seconds", 600)
    _fcs_last_sig = None  # debounce: re-log only when the stale set or a 6h band changes

    while True:
        try:
            await asyncio.sleep(interval)
            now = time.time()
            stale = []
            for name, path in FEED_FILES.items():
                if not os.path.exists(path):
                    continue
                age_h = (now - os.path.getmtime(path)) / 3600
                if age_h > FEED_MAX_STALE_HOURS:
                    stale.append((name, f"{round(age_h, 1)}h"))
            if stale:
                _sig = frozenset((n, int(float(s[:-1]) // 6)) for n, s in stale)
                if _sig != _fcs_last_sig:
                    log_finding(
                        "feed_stale", "MEDIUM",
                        "Feed files stale: " + ", ".join(f"{n}({s})" for n, s in stale),
                        "Pull fresh: json_relay/feed_request.json, or set FEED_CADENCE minutes"
                    )
                    _fcs_last_sig = _sig
            else:
                _fcs_last_sig = None
        except Exception as e:
            print(f"[warden] feed err: {e}")
            await asyncio.sleep(1200)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 6: MEDIC — process health + auto-heal
# ═══════════════════════════════════════════════════════════════════════════

NOISE_PATTERNS = [
    "urllib3.*chardet", "RequestsDependencyWarning",
    "is-window-maximized", "warn.*version",
    "<!DOCTYPE html>", "<script type",
    "JSONDecodeError.*DOCTYPE", "SyntaxWarning.*invalid escape"
]

SEVERITY_PATTERNS = {
    "CRITICAL": ["invalid_grant", "credential", "WARDEN.*BREACH",
                 "SyntaxError.*py", "private_key.*missing"],
    "HIGH":     ["IndentationError", "NameError", "ImportError",
                 "404.*endpoint", "connection.*refused", "AttributeError"],
    "MEDIUM":   ["Daily limit", "Queue empty", "Outside send window",
                 "rate limit"],
    "LOW":      ["urllib3", "chardet", "warn", "deprecated"],
}


def is_agent_bot(filename: str) -> bool:
    """Return True if a filename is a whitelisted the agent bot."""
    base = os.path.basename(filename)
    return base in AGENT_BOTS_WHITELIST


def is_blacklisted_file(filename: str) -> bool:
    """Return True if medic must NEVER touch this file."""
    base = os.path.basename(filename)
    return base in MEDIC_BLACKLIST_FILES


def classify_severity(message: str) -> str:
    for sev, patterns in SEVERITY_PATTERNS.items():
        for p in patterns:
            if re.search(p, message, re.IGNORECASE):
                return sev
    return "LOW"


def is_noise(message: str) -> bool:
    for p in NOISE_PATTERNS:
        if re.search(p, message, re.IGNORECASE):
            return True
    return False


def medic_log_action(action: str, target: str, result: str, detail: str = ""):
    log = load_json(MEDIC_LOG, [])
    log.append({
        "timestamp": datetime.now(EST).isoformat(),
        "action":    action,
        "target":    target,
        "result":    result,
        "detail":    detail,
    })
    log = log[-500:]
    save_json(MEDIC_LOG, log)


def medic_audit_entry(action: str, details: dict):
    today = datetime.now(EST).date().isoformat()
    audit_file = os.path.join(MEDIC_AUDIT, f"audit_{today}.json")
    existing = load_json(audit_file, [])
    existing.append({
        "timestamp": datetime.now(EST).isoformat(),
        "action":    action,
        "details":   details,
    })
    save_json(audit_file, existing)


def syntax_check_file(path: str) -> tuple:
    """
    Use Python's AST to check syntax. Returns (ok: bool, error: str).
    """
    if not path.endswith(".py"):
        return True, ""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", path],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr
    except Exception as e:
        return False, str(e)


async def medic_attempt_heal(bot_name: str, error_message: str = "") -> dict:
    """
    Attempt to heal a crashed/broken bot - MECHANICAL restore only, no LLM rewrite.

    Steps:
      1. Verify it's whitelisted (is_agent_bot)
      2. Verify file exists in BOTS_DIR
      3. Run syntax check
      4. If syntax is broken: restore from the last known-good backup (mechanical copy)
      5. If no good backup or restore still fails: escalate to inbox for the agent/the owner
      6. Log every action to medic_log.json

    Medic never asks an LLM to rewrite code and never spawns or restarts a process
    (bounded: detect / backup / mechanical-restore / report).

    Returns dict with action result.
    """
    result = {
        "bot": bot_name,
        "started_at": datetime.now(EST).isoformat(),
        "actions": [],
        "outcome": "unknown",
    }

    if not is_agent_bot(bot_name):
        result["outcome"] = "rejected"
        result["actions"].append(f"Bot '{bot_name}' not in AGENT_BOTS_WHITELIST")
        medic_log_action("heal_rejected", bot_name, "not_whitelisted", error_message)
        return result

    if is_blacklisted_file(bot_name):
        result["outcome"] = "rejected"
        result["actions"].append(f"Bot '{bot_name}' is BLACKLISTED for medic")
        medic_log_action("heal_rejected", bot_name, "blacklisted", error_message)
        return result

    bot_path = _bot_source_path(bot_name)
    if not os.path.exists(bot_path):
        result["outcome"] = "not_found"
        result["actions"].append(f"File not found: {bot_path}")
        medic_log_action("heal_failed", bot_name, "not_found", "")
        return result

    # Check rate limit
    cfg = load_config()
    max_per_hour = cfg.get("medic_max_heal_attempts_per_bot_per_hour", 3)
    log = load_json(MEDIC_LOG, [])
    cutoff = (datetime.now(EST) - timedelta(hours=1)).isoformat()
    recent_attempts = [
        e for e in log
        if e.get("target") == bot_name
        and e.get("action") in ("heal_attempt", "heal_success", "heal_failed")
        and (e.get("timestamp") or "") > cutoff
    ]
    if len(recent_attempts) >= max_per_hour:
        result["outcome"] = "rate_limited"
        result["actions"].append(
            f"Rate limit hit: {len(recent_attempts)} attempts/hour (max {max_per_hour})"
        )
        medic_log_action("heal_rate_limited", bot_name, "skipped", "")
        # Escalate
        write_inbox_packet(
            priority="P1", source="warden_medic",
            slug=f"medic_rate_limit_{bot_name.replace('.py','')}",
            content=(
                f"## Medic rate-limited on {bot_name}\n\n"
                f"Medic attempted heal {len(recent_attempts)}+ times in last hour.\n\n"
                f"This means heals aren't sticking — there's a deeper issue.\n\n"
                f"### Last error\n```\n{error_message[:500]}\n```\n\n"
                f"### Action\n"
                f"1. Manual inspection of `{bot_path}`\n"
                f"2. Run `code_tuner` on it manually\n"
                f"3. Roll back to last known-good if needed\n"
            )
        )
        return result

    medic_log_action("heal_attempt", bot_name, "started", error_message)

    # Step 1: syntax check
    ok, err = syntax_check_file(bot_path)
    if ok:
        result["outcome"] = "syntax_ok"
        result["actions"].append("Syntax check passed — bot file is valid")
        medic_log_action("heal_skipped", bot_name, "syntax_ok",
                         "no fix needed, bot may have crashed for runtime reason")
        return result

    result["actions"].append(f"Syntax error detected: {err[:200]}")

    # Step 2: backup current
    backup_name = f"{bot_name}.{datetime.now(EST).strftime('%Y%m%d_%H%M%S')}.backup"
    backup_path = os.path.join(MEDIC_BACKUPS, backup_name)
    try:
        shutil.copy2(bot_path, backup_path)
        result["actions"].append(f"Backup saved: {backup_name}")
    except Exception as e:
        result["outcome"] = "backup_failed"
        result["actions"].append(f"Backup failed: {e}")
        medic_log_action("heal_failed", bot_name, "backup_failed", str(e))
        return result

    # ── Sprint 38 (The owner's ruling): MEDIC IS MECHANICAL. ────────────────────────
    # The old Step 3 asked an LLM to REWRITE the bot's source and applied it after
    # a syntax-only sandbox check — an auto-patch path, on live trading bots, that
    # directly contradicted the stated doctrine ("No auto-patches ever. The agent does
    # the patch work") and the owner's standing rule: The agent authors patches, these
    # systems FLAG. A syntax-valid file is not a correct file; nobody wants an LLM's
    # guess at a bot's source applied at 3am with only a parse check between it
    # and real money.
    #
    # New behavior: RESTORE, don't author. Roll back to the most recent known-good
    # backup (or a code_tuner snapshot), quarantine the broken file for the agent, and
    # write him a doctor-style packet with the diagnosis — he authors the real
    # patch (code-monitor doctrine → owner_inbox → the agent patches in place). Availability is
    # preserved mechanically; authorship stays where doctrine puts it.
    quarantine_path = os.path.join(MEDIC_STAGING, f"{bot_name}.broken.{datetime.now(EST).strftime('%Y%m%d_%H%M%S')}")
    try:
        with open(bot_path, "r", encoding="utf-8", errors="ignore") as _qsrc:
            _qtext = _qsrc.read()
        with open(quarantine_path, "w", encoding="utf-8") as _qdst:
            _qdst.write(_strip_secret_lines(_qtext))
        result["actions"].append(f"Broken file quarantined: {os.path.basename(quarantine_path)}")
    except Exception as e:
        result["actions"].append(f"Quarantine copy failed (continuing): {e}")

    # Find the newest restore candidate: medic backups first, code_tuner snapshot second
    restore_src = None
    restore_kind = None
    try:
        backups = sorted((f for f in os.listdir(MEDIC_BACKUPS)
                          if f.startswith(bot_name) and f.endswith(".backup")), reverse=True)
        # skip the backup we JUST made of the broken file
        for b in backups:
            cand = os.path.join(MEDIC_BACKUPS, b)
            if os.path.abspath(cand) == os.path.abspath(backup_path):
                continue
            ok_c, _ = syntax_check_file(cand)
            if ok_c:
                restore_src, restore_kind = cand, "medic_backup"
                break
    except Exception:
        pass
    if not restore_src:
        try:
            snap = os.path.join(BOT_LOGS, "code_tuner", "snapshots", bot_name)
            if os.path.exists(snap):
                ok_c, _ = syntax_check_file(snap)
                if ok_c:
                    restore_src, restore_kind = snap, "code_tuner_snapshot"
        except Exception:
            pass

    if restore_src:
        try:
            shutil.copy2(restore_src, bot_path)
            result["outcome"] = "restored"
            result["actions"].append(f"Rolled back to last known-good ({restore_kind}: "
                                     f"{os.path.basename(restore_src)})")
            medic_log_action("heal_success", bot_name, "restored_known_good",
                             f"from={restore_kind}; broken preserved at {os.path.basename(quarantine_path)}")
            medic_audit_entry("heal_success", {
                "bot": bot_name, "mode": "mechanical_restore",
                "restored_from": restore_src, "quarantine": quarantine_path,
                "original_error": err[:500],
            })
        except Exception as e:
            result["outcome"] = "restore_failed"
            result["actions"].append(f"Rollback copy failed: {e}")
            medic_log_action("heal_failed", bot_name, "restore_failed", str(e))
    else:
        result["outcome"] = "no_known_good"
        result["actions"].append("No syntax-valid backup or snapshot found — bot left as-is, escalated")
        medic_log_action("heal_failed", bot_name, "no_known_good", "")

    write_inbox_packet(
        priority="P1", source="warden_medic",
        slug=f"medic_patch_needed_{bot_name.replace('.py','')}",
        content=(
            f"## {bot_name} is syntax-broken — YOUR patch is needed\n\n"
            f"### Error class\n{_classify_error_class(err)}\n\n"
            f"### Original error\n```\n{err[:600]}\n```\n\n"
            + (f"### Availability action taken (mechanical)\n"
               f"- Rolled back to last known-good ({restore_kind})\n"
               f"- The RUNNING file is the OLD version — whatever the broken edit was "
               f"trying to add is NOT live\n"
               if restore_src else
               f"### Availability action\n- NO known-good backup/snapshot existed — the "
               f"bot is down until patched\n")
            + f"- Broken file preserved for you: `{quarantine_path}`\n\n"
            f"### Your job (Sprint 38 doctrine — medic no longer authors code)\n"
            f"1. Diff the quarantined broken file against the running file\n"
            f"2. Patch the file in place yourself.\n"
            f"3. Warden does not apply patches and flags do not deploy bots.\n"
            f"Medic restores availability; it never writes code. That's yours.\n"
        )
    )

    return result


def _classify_error_class(error_message: str) -> str:
    """
    Name the error class so an auth/billing crash reads at a glance.
    The owner's #1 actual crash cause is an unpaid API key (invalid_grant / 401 /
    auth-expiry), which no auto-fix could ever resolve anyway — so it leads.
    """
    e = (error_message or "").lower()
    if "invalid_grant" in e or "401" in e or "unauthorized" in e \
            or "authentication" in e or "auth" in e and "expir" in e \
            or "api key" in e or "apikey" in e or "credential" in e:
        return "AUTH / BILLING — likely an unpaid or expired API key (go pay the bill)"
    if "403" in e or "forbidden" in e:
        return "AUTH — 403 forbidden (key scope or billing)"
    if "429" in e or "rate limit" in e or "ratelimit" in e or "quota" in e:
        return "RATE LIMIT / QUOTA exhausted"
    if "syntaxerror" in e or "invalid syntax" in e or "unexpected eof" in e \
            or "unexpected indent" in e or "indentationerror" in e:
        return "SYNTAX ERROR — code won't parse (The agent authors a patch)"
    if "modulenotfound" in e or "importerror" in e or "no module named" in e:
        return "IMPORT / DEPENDENCY missing"
    if "connection" in e or "timeout" in e or "timed out" in e or "refused" in e:
        return "NETWORK / CONNECTION — external service may be down"
    if "traceback" in e or "exception" in e or "error" in e:
        return "RUNTIME ERROR"
    return "UNKNOWN"


async def medic_report_crash(bot_name: str, error_message: str = "") -> dict:
    """
    v3.3 rework: medic is a WATCHER/REPORTER now — it never writes or applies
    code. It detects a crashed/syntax-broken whitelisted bot, takes a free
    backup (no code-write), and writes a report packet TO AGENT. The agent is the
    sole patch author; he patches the file in place. Warden does not apply patches.

    (The old auto-heal lives on in medic_attempt_heal — defined-but-unwired,
    fired ONLY by the manual `MEDIC HEAL` Telegram break-glass.)
    """
    result = {
        "bot": bot_name,
        "started_at": datetime.now(EST).isoformat(),
        "actions": [],
        "outcome": "unknown",
    }

    if not is_agent_bot(bot_name):
        result["outcome"] = "rejected"
        result["actions"].append(f"Bot '{bot_name}' not in AGENT_BOTS_WHITELIST")
        medic_log_action("report_rejected", bot_name, "not_whitelisted", error_message)
        return result

    if is_blacklisted_file(bot_name):
        result["outcome"] = "rejected"
        result["actions"].append(f"Bot '{bot_name}' is BLACKLISTED for medic")
        medic_log_action("report_rejected", bot_name, "blacklisted", error_message)
        return result

    bot_path = _bot_source_path(bot_name)
    backup_path = ""
    if os.path.exists(bot_path):
        # Free safety — back up current bytes. No code-write, no fix.
        backup_name = f"{bot_name}.{datetime.now(EST).strftime('%Y%m%d_%H%M%S')}.backup"
        backup_path = os.path.join(MEDIC_BACKUPS, backup_name)
        try:
            shutil.copy2(bot_path, backup_path)
            result["actions"].append(f"Backup saved: {backup_name}")
        except Exception as e:
            backup_path = ""
            result["actions"].append(f"Backup failed (non-fatal): {e}")
    else:
        result["actions"].append(f"Bot file not found at {bot_path} (reporting anyway)")

    error_class = _classify_error_class(error_message)
    medic_log_action("crash_reported", bot_name, error_class, error_message[:300])

    # Report leads with the ERROR CLASS so auth/billing reads at a glance.
    bot_short = bot_name.replace(".py", "")
    content = (
        f"## Error class: {error_class}\n\n"
        f"**Bot:** {bot_name}\n"
        f"**Detected:** {datetime.now(EST).isoformat()}\n"
        f"**Backup:** {backup_path or '(none — file not found or backup failed)'}\n\n"
        f"---\n\n"
        f"### What medic did\n"
        f"Detected the crash, took a backup. Medic does NOT fix code — you (The agent) "
        f"patch the file in place. Warden does not apply it.\n\n"
        f"### If this is AUTH / BILLING\n"
        f"Most {bot_short} crashes are an unpaid/expired API key. If the error class "
        f"above says auth/billing, the fix is to pay the bill / refresh the key — "
        f"there is no code patch to author.\n\n"
        f"### Error trace\n"
        f"```\n{error_message[:1500]}\n```\n\n"
        f"### If this is a real code bug\n"
        f"Patch the file in place yourself. "
        f"Backup is at the path above if you need the pre-crash bytes.\n"
    )
    write_inbox_packet(
        priority="P1", source="warden_medic",
        slug=f"medic_report_{bot_short}",
        content=content,
    )
    result["outcome"] = "reported"
    result["actions"].append(f"Report written to the agent (error class: {error_class})")
    return result


async def medic_rollback(bot_name: str) -> dict:
    """Restore the most recent backup of a bot."""
    result = {"bot": bot_name, "started_at": datetime.now(EST).isoformat(), "outcome": "unknown"}

    if not is_agent_bot(bot_name):
        result["outcome"] = "rejected"
        return result

    # Find most recent backup
    backups = [
        f for f in os.listdir(MEDIC_BACKUPS)
        if f.startswith(bot_name) and f.endswith(".backup")
    ]
    if not backups:
        result["outcome"] = "no_backup"
        return result

    backups.sort(reverse=True)
    latest = backups[0]
    backup_path = os.path.join(MEDIC_BACKUPS, latest)
    bot_path = _bot_source_path(bot_name)

    try:
        shutil.copy2(backup_path, bot_path)
        result["outcome"] = "rolled_back"
        result["restored_from"] = latest
        medic_log_action("rollback", bot_name, "success", latest)
    except Exception as e:
        result["outcome"] = "failed"
        result["error"] = str(e)
        medic_log_action("rollback", bot_name, "failed", str(e))
    return result


async def medic_process_monitor():
    """
    Periodically scan bot logs for errors.
    Trigger heal on syntax errors / known-fixable patterns.
    """
    print("[warden] medic process monitor active")
    cfg = load_config()
    if not cfg.get("medic_enabled", True):
        print("[warden] medic disabled in config")
        return

    interval = cfg.get("medic_check_interval_seconds", 300)
    if not os.path.exists(MEDIC_LOG):
        save_json(MEDIC_LOG, [])  # S4: ensure the medic audit log exists even before the first heal

    last_seen_log_size = {}

    while True:
        try:
            await asyncio.sleep(interval)

            # Honor lockdown
            if is_lockdown_active():
                print("[warden] medic skipping — lockdown active")
                continue

            # Scan for log files in bot_logs that might contain error traces
            error_logs = [
                os.path.join(BOT_LOGS, "bot_alerts.json"),
                os.path.join(BOT_LOGS, "errors.json"),
            ]
            # Also scan any *.log files
            try:
                for f in os.listdir(BOT_LOGS):
                    if f.endswith(".log"):
                        error_logs.append(os.path.join(BOT_LOGS, f))
            except Exception:
                pass

            heal_targets = {}  # bot_name -> error message

            for log_path in error_logs:
                if not os.path.exists(log_path):
                    continue
                size = os.path.getsize(log_path)
                last_size = last_seen_log_size.get(log_path, 0)
                if size <= last_size:
                    continue
                last_seen_log_size[log_path] = size

                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        # Read only the new bytes
                        f.seek(last_size)
                        new_content = f.read()
                except Exception:
                    continue

                if not new_content:
                    continue

                # Look for SyntaxError mentioning a whitelisted bot
                for bot in AGENT_BOTS_WHITELIST:
                    if bot in new_content:
                        # Extract a small snippet around the bot mention
                        idx = new_content.find(bot)
                        snippet = new_content[max(0, idx - 200):idx + 300]
                        if is_noise(snippet):
                            continue
                        sev = classify_severity(snippet)
                        if sev in ("CRITICAL", "HIGH"):
                            heal_targets[bot] = snippet

            # v3.3: medic REPORTS, never heals. Dedupe so a persistently-crashing
            # bot doesn't spam the agent's inbox every cycle — if we've already
            # reported it N times in the window, go quiet and let doctor's deeper
            # investigation carry it.
            cfg_local = load_config()
            backoff_window = cfg_local.get("medic_backoff_window_hours", 6)
            for bot, err in heal_targets.items():
                bot_name_only = bot.replace(".py", "")
                reports_in_window = heal_attempts_in_window(bot_name_only, backoff_window)
                if reports_in_window >= 3:
                    print(f"[warden/medic] already reported {bot} "
                          f"{reports_in_window}x in {backoff_window}h — quiet, "
                          f"doctor will investigate")
                    continue

                print(f"[warden/medic] reporting crash: {bot}")
                report_result = await medic_report_crash(bot, err)
                outcome = report_result.get("outcome", "unknown")
                print(f"[warden/medic] report {bot}: {outcome}")
                # Track report for dedupe
                record_heal_attempt(bot_name_only, outcome)

        except Exception as e:
            print(f"[warden] medic monitor err: {e}")
            await asyncio.sleep(600)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 6b: DOCTOR — investigates what medic can't fix
# ═══════════════════════════════════════════════════════════════════════════
# When medic exhausts its retry budget OR when silent_failure_monitor sees a
# venue-offline-style problem, doctor steps in. Doctor is an EVIDENCE ROUTER (no
# LLM diagnose on the auto path - retired per doctrine); it gathers and routes:
#   - Reads the bot's recent logs (last ~1000 lines)
#   - Reads recent inbox events for that bot
#   - Reads the bot's logs, heartbeat and the Flow regime from the feeds cache
#   - Asks an SI: "what's wrong, what should we do?"
# Doctor then routes the answer to the right place:
#   - "Code-fixable bug" → write to code_requests.json (code_tuner picks up)
#   - "External service issue" → write doctor_escalation packet to the agent
#   - "Just a hung process, restart it" → write a doctor_restart packet to the agent
#   - "Unknown / weird" → write the evidence packet to the agent's inbox

DOCTOR_LOG_FILE = os.path.join(BOT_LOGS, "doctor_log.json")
HEAL_ATTEMPTS_FILE = os.path.join(BOT_LOGS, "heal_attempts.json")
CODE_REQUESTS_FILE = os.path.join(BOT_LOGS, "code_requests.json")  # code_tuner reads this


def doctor_log_entry(bot_name: str, symptom: str, diagnosis: str,
                     resolution: str, outcome: str, raw_llm_response: str = ""):
    """Append entry to doctor_log.json (capped 200)."""
    entries = load_json(DOCTOR_LOG_FILE, [])
    if not isinstance(entries, list):
        entries = []
    entries.append({
        "ts":         datetime.now(EST).isoformat(),
        "bot":        bot_name,
        "symptom":    symptom,
        "diagnosis":  diagnosis,
        "resolution": resolution,
        "outcome":    outcome,
        "raw_llm":    raw_llm_response[:2000] if raw_llm_response else "",
    })
    save_json(DOCTOR_LOG_FILE, entries[-200:])


def record_heal_attempt(bot_name: str, outcome: str):
    """Track heal attempts per bot with timestamps for backoff math."""
    attempts = load_json(HEAL_ATTEMPTS_FILE, {})
    if not isinstance(attempts, dict):
        attempts = {}
    bot_attempts = attempts.get(bot_name, [])
    bot_attempts.append({
        "ts":      datetime.now(EST).isoformat(),
        "outcome": outcome,
    })
    # Keep last 50 per bot
    attempts[bot_name] = bot_attempts[-50:]
    save_json(HEAL_ATTEMPTS_FILE, attempts)


def heal_attempts_in_window(bot_name: str, hours: int = 6) -> int:
    """Count failed heal attempts for bot_name in the last N hours."""
    attempts = load_json(HEAL_ATTEMPTS_FILE, {})
    if not isinstance(attempts, dict):
        return 0
    bot_attempts = attempts.get(bot_name, [])
    cutoff = datetime.now(EST) - timedelta(hours=hours)
    count = 0
    for a in bot_attempts:
        try:
            ts = datetime.fromisoformat(a.get("ts", ""))
            if ts >= cutoff and a.get("outcome", "").lower() in ("failed", "error"):
                count += 1
        except Exception:
            continue
    return count


async def doctor_investigate(bot_name: str, symptom: str, context: dict = None) -> dict:
    """v3.3 - evidence ROUTER for what medic can't fix. Gathers evidence and routes/escalates it to the agent (the intelligence layer). LLM diagnose is DISABLED on the auto path (do not re-enable per doctrine); this is not a Claude-powered diagnoser.
    
    bot_name:  bare name (e.g. "perps_bot") — without .py
    symptom:   short tag for what triggered investigation
               ("medic_exhausted" | "silent_failure" | "heartbeat_stale")
    context:   optional dict of additional state (heartbeat ages, log paths, etc.)
    
    Returns a dict with the diagnosis + routing decision.
    """
    cfg = load_config()
    if not cfg.get("doctor_enabled", True):
        return {"outcome": "doctor_disabled"}
    
    context = context or {}
    print(f"[warden/doctor] investigating {bot_name} (symptom={symptom})")
    
    # Gather evidence — recent logs, heartbeat, last 24h inbox events
    evidence = {
        "bot_name":         bot_name,
        "symptom":          symptom,
        "context":          context,
        "recent_log_lines": [],
        "heartbeat_state":  None,
        "heal_attempts_6h": heal_attempts_in_window(bot_name, 6),
    }
    
    # Recent log lines (last ~3000 chars)
    log_paths = [
        os.path.join(BOT_LOGS, f"{bot_name}.log"),
        os.path.join(BOT_LOGS, "errors.json"),
    ]
    for lp in log_paths:
        if not os.path.exists(lp):
            continue
        try:
            size = os.path.getsize(lp)
            with open(lp, "r", encoding="utf-8", errors="ignore") as f:
                if size > 8000:
                    f.seek(size - 8000)
                tail = f.read()
            evidence["recent_log_lines"].append({"file": os.path.basename(lp),
                                                  "tail": tail[-3000:]})
        except Exception:
            continue
    
    # Heartbeat content
    hb_path = _resolve_bot_heartbeat_path(bot_name)
    if os.path.exists(hb_path):
        try:
            evidence["heartbeat_state"] = load_json(hb_path, {})
            evidence["heartbeat_age_min"] = (time.time() - os.path.getmtime(hb_path)) / 60
        except Exception:
            pass
    
    # Flow regime from the feeds cache (bots/feed_hook.py), if a pull has happened
    fc = load_json(os.path.join(BOT_LOGS, "flow_cache.json"), {}) or {}
    if isinstance(fc, dict) and fc:
        cyc = fc.get("cycle") if isinstance(fc.get("cycle"), dict) else {}
        evidence["flow_regime"] = {"phase": cyc.get("phase"), "regime": cyc.get("regime"),
                                   "regime_break": cyc.get("regime_break")}
    
    # DOCTRINE: warden does NOT diagnose with an LLM (no SI call here).
    # It gathers evidence and hands it to the agent , the agent is the brain AND the coder.
    # He reads this on his next wake, names the cause from the logs, and acts.
    log_excerpt = "\n".join(x["tail"] for x in evidence["recent_log_lines"])[:2500]
    hb_state = json.dumps(evidence.get("heartbeat_state"), indent=2, default=str)[:1200]
    dr_state = json.dumps(evidence.get("flow_regime"), indent=2, default=str)[:800]
    content = (
        f"# Doctor , investigate {bot_name}\n\n"
        f"**Symptom:** {symptom}\n"
        f"**Heal attempts (6h):** {evidence['heal_attempts_6h']}\n"
        f"**Heartbeat age:** {evidence.get('heartbeat_age_min', '?')} min\n\n"
        f"## Your move (The agent)\n"
        f"Medic couldn't fix this. Warden does NOT diagnose , that's yours. Read the "
        f"evidence below, name the cause, and act: code bug -> author the full file into "
        f"patch the file in place (you apply it; warden does not). "
        f"hung process -> flag a restart; external service (RPC / Jupiter / API) -> browse "
        f"and check; can't tell -> escalate to the owner. Log what you find in LEARNINGS.md.\n\n"
        f"## Recent log tails\n```\n{log_excerpt}\n```\n\n"
        f"## Heartbeat state\n```\n{hb_state}\n```\n\n"
        f"## Flow regime (flow_cache)\n```\n{dr_state}\n```\n"
    )
    content = _strip_secret_lines(content)
    write_inbox_packet("P2", "doctor", f"investigate_{bot_name}", content)
    outcome = "routed_to_agent"
    resolution = "Evidence packet written to the agent , he diagnoses + acts (no warden LLM)."

    doctor_log_entry(
        bot_name=bot_name,
        symptom=symptom,
        diagnosis="(routed to the agent , warden no longer diagnoses)",
        resolution=resolution,
        outcome=outcome,
        raw_llm_response="",
    )

    return {"outcome": outcome, "resolution": resolution}


async def doctor_monitor():
    """Runs alongside medic. When medic's heal_attempts_in_window(bot, 6) > 3,
    medic has been looping — doctor takes over investigation. Detection happens
    by reading medic_log.json for recent failures.
    """
    cfg = load_config()
    if not cfg.get("doctor_enabled", True):
        print("[warden] doctor disabled")
        return
    print("[warden] doctor monitor active (v3.3 — evidence routed to the agent, no LLM)")
    interval = cfg.get("doctor_check_interval_seconds", 300)
    backoff_window_h = cfg.get("medic_backoff_window_hours", 6)
    investigated_this_session = set()  # avoid investigating same bot repeatedly per session
    
    while True:
        try:
            await asyncio.sleep(interval)
            if is_lockdown_active():
                continue
            
            # Check each whitelisted bot for runaway heal attempts
            for bot_filename in AGENT_BOTS_WHITELIST:
                bot_name = bot_filename.replace(".py", "")
                fail_count = heal_attempts_in_window(bot_name, backoff_window_h)
                if fail_count < 3:
                    continue
                
                # Already investigated this bot in this session window — skip
                session_key = f"{bot_name}_{datetime.now(EST).strftime('%Y%m%d')}"
                if session_key in investigated_this_session:
                    continue
                
                print(f"[warden/doctor] medic exhausted on {bot_name} "
                      f"({fail_count} fails in {backoff_window_h}h) — investigating")
                result = await doctor_investigate(
                    bot_name=bot_name,
                    symptom="medic_exhausted",
                    context={"heal_fail_count": fail_count,
                             "window_hours": backoff_window_h},
                )
                investigated_this_session.add(session_key)
                print(f"[warden/doctor] {bot_name}: {result.get('outcome')}")
        except Exception as e:
            print(f"[warden/doctor] monitor err: {e}")
            await asyncio.sleep(600)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 6c: BOT BUILDER (formerly bot-builder skill, now baked in)
# ═══════════════════════════════════════════════════════════════════════════

BOT_BUILDER_INBOX_TRIGGER_PREFIX = "P3_"  # RETIRED v3.3 — filenames no longer carry priority prefixes; bot-builder now matches on the 'bot_spec' subject substring alone. Kept defined for back-compat.


async def bot_builder_check_inbox():
    """
    Watch inbox for P3 bot-spec proposals.
    When found: read spec, ask LLM to draft, sandbox-test, write to staging,
    write a P2 confirmation packet to inbox for the owner review (NEVER auto-deploy).
    """
    if not os.path.exists(INBOX):
        return

    try:
        for fname in os.listdir(INBOX):
            # Naming doctrine dropped filename priority prefixes — match on the
            # 'bot_spec' subject substring alone (was: gated on P3_ prefix).
            if "bot_spec" not in fname.lower():
                continue
            if fname.startswith("CLAIMED_"):
                continue
            full_path = os.path.join(INBOX, fname)

            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    spec_content = f.read()
            except Exception:
                continue

            # Claim it (rename) so we don't re-process
            claimed_name = "CLAIMED_" + fname
            claimed_path = os.path.join(INBOX, claimed_name)
            try:
                os.rename(full_path, claimed_path)
            except Exception:
                continue

            # Process
            result = await bot_builder_draft(spec_content, fname)

            # Move to done
            try:
                done_path = os.path.join(INBOX_DONE, fname)
                shutil.move(claimed_path, done_path)
            except Exception:
                pass

            # Review note goes to the agent. Flags must not copy staging into bots/.
            write_inbox_packet(
                priority="P2", source="warden_bot_builder",
                slug=f"bot_draft_{result.get('bot_name', 'unknown')}",
                content=(
                    f"## Bot draft ready for review: {result.get('bot_name', 'unknown')}\n\n"
                    f"**Spec source:** `{fname}`\n"
                    f"**Outcome:** {result.get('outcome')}\n"
                    f"**Staging path:** `{result.get('staging_path', 'n/a')}`\n\n"
                    f"### Sandbox check\n{result.get('sandbox_result', 'n/a')}\n\n"
                    f"### What this means\n"
                    f"Warden has drafted the bot to staging. It has NOT been deployed.\n"
                    f"Approval flags will not copy it into bots/.\n\n"
                    f"### Action\n"
                    f"1. Review the staging file\n"
                    f"2. Do not drop an APPROVED/AMEND flag\n"
                    f"3. If the file should exist, the agent patches it in place himself\n"
                    f"4. Warden will not add it to Start_Bots or the whitelist\n"
                ),
            )
    except Exception as e:
        print(f"[warden/builder] inbox check err: {e}")


async def bot_builder_draft(spec_content: str, source_filename: str) -> dict:
    """
    Given a bot spec markdown, ask LLM to draft the Python file.
    Sandbox test it. NEVER auto-deploy. Write to staging only.
    """
    result = {
        "spec_source": source_filename,
        "outcome": "unknown",
        "bot_name": "unknown",
        "staging_path": "",
    }

    # Extract bot name from spec
    name_match = re.search(r"bot[_\s]+name\s*[:=]\s*[`']?(\w+)[`']?", spec_content, re.I)
    if name_match:
        bot_name = name_match.group(1)
    else:
        # Try first H1
        h1 = re.search(r"^#\s+(\w[\w\-_]*)", spec_content, re.M)
        bot_name = h1.group(1) if h1 else "unnamed_bot"

    bot_name = re.sub(r"\W", "_", bot_name)  # sanitize
    if not bot_name.endswith(".py"):
        bot_name = f"{bot_name}.py"
    result["bot_name"] = bot_name

    # Refuse if name conflicts with existing
    existing_path = os.path.join(BOTS_DIR, bot_name)
    if os.path.exists(existing_path):
        result["outcome"] = "name_conflict"
        return result

    draft_prompt = f"""You are BOT_BUILDER, drafting a new Python bot for the the ecosystem trading system.

Here is the spec:
```
{spec_content[:4000]}
```

Workspace conventions (must follow):
- Workspace root: `<repo root>`
- Bot logs: `<repo root>\\bots\\bot_logs`
- JSON relay: `<repo root>\\bots\\json_relay`
- Inbox: `<repo root>\\inbox`
- All credentials from .env via `os.getenv` — never hardcoded
- Use `httpx.AsyncClient` for HTTP, not requests
- Use `asyncio` event loop
- Use `from zoneinfo import ZoneInfo; EST = ZoneInfo("America/New_York")` for timestamps
- Atomic file writes via tmp+replace pattern
- Cap append-only logs at 500-2000 entries
- Write heartbeats to `bot_logs/<botname>_heartbeat.json` every cycle
- Honor `bots/bot_logs/lockdown_state.flag` — pause writes when present
- Honor `bots/bot_logs/KILL_SWITCH.flag` — full halt when present

Output ONLY the complete Python file. Start with a docstring summary, then imports, then code. No markdown fences, no commentary.
"""
    sys_prompt = "You are an expert Python developer building production trading bots."

    drafted = await ask_llm(draft_prompt, max_tokens=12000, system=sys_prompt)

    if not drafted or len(drafted) < 200:
        result["outcome"] = "llm_empty"
        return result

    if drafted.startswith("```"):
        drafted = re.sub(r"^```[a-z]*\n", "", drafted, count=1)
        drafted = re.sub(r"\n```\s*$", "", drafted)

    # Stage it
    staging_path = os.path.join(MEDIC_STAGING, bot_name)
    with open(staging_path, "w", encoding="utf-8") as f:
        f.write(drafted)
    result["staging_path"] = staging_path

    # Sandbox check
    ok, err = syntax_check_file(staging_path)
    if ok:
        result["outcome"] = "sandbox_ok"
        result["sandbox_result"] = "Syntax valid. Ready for review."
    else:
        result["outcome"] = "sandbox_failed"
        result["sandbox_result"] = f"Syntax error: {err[:300]}"

    medic_audit_entry("bot_builder_draft", {
        "spec_source": source_filename,
        "bot_name": bot_name,
        "outcome": result["outcome"],
    })

    return result


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 7: INTEGRITY (hash check on critical files)
# ═══════════════════════════════════════════════════════════════════════════

def integrity_baseline_snapshot():
    """Snapshot current critical-file hashes as known-good baseline.
    v3.4: configured paths that don't exist are reported ONCE (INFO) instead of
    silently thinning the baseline — 'reading wrong paths' becomes visible."""
    baseline = {}
    missing_configured = []
    for path in CRITICAL_FILES:
        h = file_sha256(path)
        if h:
            baseline[path] = {
                "sha256":      h,
                "snapshotted": datetime.now(EST).isoformat(),
                "size":        os.path.getsize(path) if os.path.exists(path) else 0,
            }
        else:
            missing_configured.append(path)
    save_json(HASH_BASELINE, baseline)
    print(f"[warden] integrity baseline snapshotted: {len(baseline)} files")
    if missing_configured:
        log_finding("integrity_config", "INFO",
                    f"{len(missing_configured)} configured watch path(s) do not exist: "
                    + ", ".join(os.path.basename(p) for p in missing_configured),
                    "Prune stale paths from OPERATOR_DOC_FILES/SENTINEL_FILES or restore the files")
        print(f"[warden] integrity: {len(missing_configured)} configured paths missing "
              f"({', '.join(os.path.basename(p) for p in missing_configured)})")
    return baseline


def integrity_log(event: str, path: str, detail: str = ""):
    log = load_json(INTEGRITY_LOG, [])
    log.append({
        "timestamp": datetime.now(EST).isoformat(),
        "event":     event,
        "path":      path,
        "detail":    detail,
    })
    log = log[-200:]
    save_json(INTEGRITY_LOG, log)


async def integrity_check_now() -> dict:
    """Run hash check against baseline. Returns summary."""
    baseline = load_json(HASH_BASELINE, {})
    if not baseline:
        baseline = integrity_baseline_snapshot()
        return {"first_run": True, "files": len(baseline), "mismatches": []}

    summary = {"checked": 0, "matches": 0, "mismatches": [], "missing": [],
               "operator_changes": [], "sentinel_changes": [], "deletions": []}
    _op_docs = set(OPERATOR_DOC_FILES)
    _sentinels = set(SENTINEL_FILES)
    _rebaselined = False
    for path, expected in baseline.items():
        summary["checked"] += 1
        if not os.path.exists(path):
            summary["missing"].append(path)
            summary["deletions"].append(path)
            integrity_log("deleted", path, "baselined file no longer exists")
            continue
        current_hash = file_sha256(path)
        if current_hash == expected.get("sha256"):
            summary["matches"] += 1
            continue
        rec = {"path": path, "expected": expected.get("sha256", "")[:12],
               "current": current_hash[:12],
               "size_was": expected.get("size"),
               "size_now": os.path.getsize(path)}
        summary["mismatches"].append(rec)
        if path in _sentinels:
            summary["sentinel_changes"].append(rec)
            integrity_log("sentinel_change", path,
                          f"expected={rec['expected']}, current={rec['current']}")
        else:
            # v3.4: OPERATOR CHANGE — ledger it, auto-rebaseline it, digest it.
            # No packet. No Telegram. No asking the owner if the owner was the owner.
            summary["operator_changes"].append(rec)
            integrity_log("operator_change", path,
                          f"expected={rec['expected']}, current={rec['current']}, "
                          f"size {rec['size_was']}→{rec['size_now']} — auto-rebaselined")
            baseline[path] = {"sha256": current_hash,
                              "snapshotted": datetime.now(EST).isoformat(),
                              "size": rec["size_now"]}
            _rebaselined = True
    if _rebaselined:
        save_json(HASH_BASELINE, baseline)

    n_op = len(summary["operator_changes"])
    if n_op:
        log_finding("integrity_operator_change", "INFO",
                    f"{n_op} operator doc(s) changed and auto-rebaselined: "
                    + ", ".join(os.path.basename(c["path"]) for c in summary["operator_changes"]),
                    "Visible in daily digest — no action unless a change surprises you")

    # ── THREAT CLASS 1: deletion of a baselined file ──
    if summary["deletions"]:
        log_finding("integrity_deletion", "HIGH",
                    f"{len(summary['deletions'])} baselined file(s) DELETED: "
                    + ", ".join(os.path.basename(p) for p in summary["deletions"]),
                    "Confirm intentional; restore from backup if not")
        write_inbox_packet(
            priority="P1", source="warden_integrity",
            slug=f"integrity_deleted_{len(summary['deletions'])}_files",
            content=(f"## Integrity: {len(summary['deletions'])} baselined file(s) deleted\n\n"
                     + "\n".join(f"- `{p}`" for p in summary["deletions"])
                     + "\n\nA watched file disappearing is a real signal — confirm with "
                       "The owner that removal was intentional; if yes, the next baseline "
                       "refresh drops it cleanly. If no one removed it, treat as an "
                       "incident and preserve evidence before restoring."))

    # ── THREAT CLASS 2: sentinel (.env) changed — ask, don't accuse ──
    if summary["sentinel_changes"]:
        mass_anomaly = len(summary["mismatches"]) >= 6
        sev = "CRITICAL" if mass_anomaly else "HIGH"
        log_finding("integrity_sentinel", sev,
                    f".env hash changed" + (" amid mass file changes" if mass_anomaly else ""),
                    "Confirm with the owner; INTEGRITY BASELINE if legitimate")
        write_inbox_packet(
            priority="P1", source="warden_integrity",
            slug="integrity_sentinel_env_changed",
            content=(f"## Integrity: .env changed\n\n"
                     f"The secrets file's hash changed since last baseline.\n\n"
                     f"**Most likely:** The owner rotated a key or added a var. One question "
                     f"settles it: *\"yo, did you touch .env?\"* If yes → Telegram "
                     f"`INTEGRITY BASELINE` and move on.\n\n"
                     + ("**However:** this happened alongside "
                        f"{len(summary['mismatches'])} total file changes in one sweep — a "
                        f"mass-change pattern. If the owner did NOT do this, treat it as an "
                        f"active incident: WARDEN LOCKDOWN is on the table.\n"
                        if mass_anomaly else
                        "If the owner did NOT touch it, rotate exposed keys first, "
                        "investigate second.\n")))
        await notify(
            f"🟡 <b>Warden: .env changed</b>\n"
            f"Probably the owner — confirm and `INTEGRITY BASELINE`."
            + ("\n⚠ Mass-change pattern alongside it — verify carefully." if mass_anomaly else ""))

    return summary


async def integrity_monitor():
    print("[warden] integrity monitor active")
    cfg = load_config()
    interval = cfg.get("integrity_check_interval_seconds", 21600)  # 6h default

    # First run: snapshot if no baseline exists
    if not os.path.exists(HASH_BASELINE):
        integrity_baseline_snapshot()

    while True:
        try:
            await asyncio.sleep(interval)
            await integrity_check_now()
        except Exception as e:
            print(f"[warden] integrity err: {e}")
            await asyncio.sleep(7200)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 8: AGENT COMMS (daily digest + telegram command channel)
# ═══════════════════════════════════════════════════════════════════════════

def get_24h_findings_summary() -> dict:
    findings = load_json(WARDEN_LOG, [])
    cutoff = (datetime.now(EST) - timedelta(days=1)).isoformat()
    recent = [f for f in findings if (f.get("timestamp") or "") > cutoff]
    by_severity = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    by_category = {}
    for f in recent:
        sev = f.get("severity", "INFO")
        by_severity[sev] = by_severity.get(sev, 0) + 1
        cat = f.get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
    return {
        "total":       len(recent),
        "by_severity": by_severity,
        "by_category": by_category,
        "items":       recent[-30:],
    }


def get_medic_24h_summary() -> dict:
    log = load_json(MEDIC_LOG, [])
    cutoff = (datetime.now(EST) - timedelta(days=1)).isoformat()
    recent = [e for e in log if (e.get("timestamp") or "") > cutoff]
    summary = {"total": len(recent), "by_action": {}, "heals_succeeded": 0, "heals_failed": 0}
    for e in recent:
        a = e.get("action", "unknown")
        summary["by_action"][a] = summary["by_action"].get(a, 0) + 1
        if a == "heal_success":
            summary["heals_succeeded"] += 1
        elif a == "heal_failed":
            summary["heals_failed"] += 1
    return summary


# ═══════════════════════════════════════════════════════════════════════════
# DAILY DIGEST additions
# ═══════════════════════════════════════════════════════════════════════════

def get_doctor_24h_summary() -> dict:
    """v3.3 — Counts doctor investigations in last 24h grouped by outcome."""
    log = load_json(DOCTOR_LOG_FILE, [])
    if not isinstance(log, list):
        return {"total": 0, "by_outcome": {}, "samples": []}
    cutoff = (datetime.now(EST) - timedelta(days=1)).isoformat()
    recent = [e for e in log if (e.get("ts") or "") > cutoff and isinstance(e, dict)]
    by_outcome = {}
    for e in recent:
        oc = e.get("outcome", "unknown")
        by_outcome[oc] = by_outcome.get(oc, 0) + 1
    return {
        "total":      len(recent),
        "by_outcome": by_outcome,
        "samples":    recent[-5:],
    }


def get_code_tuner_queue_depth() -> dict:
    """v3.3 — Read code_requests.json, return pending count + oldest unresolved age."""
    requests = load_json(CODE_REQUESTS_FILE, [])
    if not isinstance(requests, list):
        return {"pending": 0, "oldest_age_hours": None, "by_requester": {}}
    pending = [r for r in requests if isinstance(r, dict) and not r.get("resolved", False)]
    by_requester = {}
    for r in pending:
        req = r.get("requester", "unknown")
        by_requester[req] = by_requester.get(req, 0) + 1
    oldest_age = None
    if pending:
        ages = []
        for r in pending:
            try:
                ts = datetime.fromisoformat(r.get("requested_at", ""))
                ages.append((datetime.now(EST) - ts).total_seconds() / 3600)
            except Exception:
                continue
        if ages:
            oldest_age = round(max(ages), 1)
    return {
        "pending":          len(pending),
        "oldest_age_hours": oldest_age,
        "by_requester":     by_requester,
    }


def get_feed_freshness() -> dict:
    """Age in hours of each feed output (None = never pulled)."""
    out = {}
    for name, path in FEED_FILES.items():
        try:
            out[name] = round((time.time() - os.path.getmtime(path)) / 3600, 1)
        except OSError:
            out[name] = None
    return out


def get_bot_heartbeat_snapshot() -> dict:
    """v3.3 — Current ✅/⚠️/🔴 status per bot using configured thresholds."""
    cfg = load_config()
    thresholds = cfg.get("bot_heartbeat_thresholds_minutes", {})
    snapshot = {"alive": [], "stale": [], "offline": [], "no_heartbeat": []}
    now = time.time()
    
    for bot_name, threshold_min in thresholds.items():
        hb_path = _resolve_bot_heartbeat_path(bot_name)
        if not os.path.exists(hb_path):
            snapshot["no_heartbeat"].append(bot_name)
            continue
        try:
            age_min = (now - os.path.getmtime(hb_path)) / 60
        except OSError:
            snapshot["no_heartbeat"].append(bot_name)
            continue
        
        if age_min <= threshold_min:
            snapshot["alive"].append((bot_name, round(age_min, 1)))
        elif age_min <= threshold_min * 2:
            snapshot["stale"].append((bot_name, round(age_min, 1), threshold_min))
        else:
            snapshot["offline"].append((bot_name, round(age_min, 1), threshold_min))
    return snapshot


def build_daily_digest_content() -> str:
    findings_summary = get_24h_findings_summary()
    medic_summary = get_medic_24h_summary()
    integrity_log = load_json(INTEGRITY_LOG, [])
    cutoff = (datetime.now(EST) - timedelta(days=1)).isoformat()
    recent_integrity = [e for e in integrity_log if (e.get("timestamp") or "") > cutoff]
    attack_sum = get_attack_summary()
    
    # digest additions
    doctor_summary = get_doctor_24h_summary()
    code_tuner_queue = get_code_tuner_queue_depth()
    feed_age = get_feed_freshness()
    heartbeat_snapshot = get_bot_heartbeat_snapshot()

    fs = findings_summary
    ms = medic_summary

    sections = []

    # Header
    sections.append(
        f"## Warden 24h Digest — {datetime.now(EST).strftime('%Y-%m-%d')}\n\n"
        f"**Status snapshot**\n"
        f"- Lockdown active: {is_lockdown_active()}\n"
        f"- Kill switch active: {is_kill_switch_active()}\n"
        f"- Integrity baseline: {'present' if os.path.exists(HASH_BASELINE) else 'MISSING'}\n"
    )

    # === v3.4: operator changes — awareness, not alarm ===
    _op_changes = [e for e in recent_integrity if e.get("event") == "operator_change"]
    if _op_changes:
        _by_file = {}
        for e in _op_changes:
            _by_file.setdefault(os.path.basename(e.get("path", "?")), 0)
            _by_file[os.path.basename(e.get("path", "?"))] += 1
        sections.append(
            "\n### Operator changes (auto-rebaselined — no action needed)\n\n"
            + "\n".join(f"- `{f}` ({n}×)" for f, n in sorted(_by_file.items()))
            + "\nThese are the owner's workspace docs changing, which is the owner working. "
              "Read for awareness — sprint context often lives in these diffs. Only "
              "flag if a change here surprises BOTH of you.\n")
    
    # === v3.3 Phase 5 NEW: Bot Heartbeat Snapshot (first thing to know each morning) ===
    alive_count = len(heartbeat_snapshot["alive"])
    stale_count = len(heartbeat_snapshot["stale"])
    offline_count = len(heartbeat_snapshot["offline"])
    no_hb_count = len(heartbeat_snapshot["no_heartbeat"])
    
    hb_lines = []
    if offline_count:
        hb_lines.append("**🔴 OFFLINE:**")
        for name, age, threshold in heartbeat_snapshot["offline"]:
            hb_lines.append(f"  - `{name}`: {age/60:.1f}h stale (threshold {threshold/60:.1f}h)")
    if stale_count:
        hb_lines.append("**⚠️  STALE (1-2x threshold):**")
        for name, age, threshold in heartbeat_snapshot["stale"]:
            hb_lines.append(f"  - `{name}`: {age/60:.1f}h stale")
    if no_hb_count:
        hb_lines.append("**❓ NO HEARTBEAT FILE:**")
        hb_lines.append(f"  - {', '.join(heartbeat_snapshot['no_heartbeat'])}")
    if not hb_lines:
        hb_lines.append("All monitored bots within threshold ✅")
    
    sections.append(
        f"\n### Bot heartbeat snapshot (v3.3)\n\n"
        f"- Alive: {alive_count} | Stale: {stale_count} | Offline: {offline_count} | No HB: {no_hb_count}\n\n"
        + "\n".join(hb_lines) + "\n"
    )

    # Findings
    sev_lines = "\n".join(
        f"  - {sev}: {n}" for sev, n in fs["by_severity"].items() if n > 0
    ) or "  - none"
    cat_lines = "\n".join(
        f"  - {cat}: {n}" for cat, n in
        sorted(fs["by_category"].items(), key=lambda x: -x[1])[:8]
    ) or "  - none"

    sections.append(
        f"\n### Findings ({fs['total']} total)\n\n"
        f"**By severity:**\n{sev_lines}\n\n"
        f"**By category (top 8):**\n{cat_lines}\n"
    )

    # Medic
    sections.append(
        f"\n### Medic activity\n\n"
        f"- Total actions: {ms['total']}\n"
        f"- Heals succeeded: {ms['heals_succeeded']}\n"
        f"- Heals failed: {ms['heals_failed']}\n"
    )
    
    # === v3.3 Phase 5 NEW: Doctor activity ===
    doctor_outcome_lines = "\n".join(
        f"  - {outcome}: {n}" for outcome, n in doctor_summary["by_outcome"].items()
    ) or "  - (no investigations)"
    doctor_section = (
        f"\n### Doctor activity (v3.3)\n\n"
        f"- Total investigations: {doctor_summary['total']}\n"
        f"**By outcome:**\n{doctor_outcome_lines}\n"
    )
    if doctor_summary["samples"]:
        doctor_section += "\n**Recent samples:**\n"
        for s in doctor_summary["samples"][-3:]:
            doctor_section += (
                f"  - `{s.get('bot','?')}` ({s.get('symptom','?')}) → "
                f"{s.get('outcome','?')}: {s.get('diagnosis','?')[:80]}\n"
            )
    sections.append(doctor_section)
    
    # === v3.3 Phase 5 NEW: Code tuner queue depth ===
    queue_line = f"- Pending: {code_tuner_queue['pending']}"
    if code_tuner_queue.get("oldest_age_hours"):
        queue_line += f" (oldest: {code_tuner_queue['oldest_age_hours']}h old)"
    by_req = code_tuner_queue.get("by_requester", {})
    if by_req:
        req_lines = "\n".join(f"  - {req}: {n}" for req, n in by_req.items())
    else:
        req_lines = "  - (queue empty)"
    sections.append(
        f"\n### Code tuner queue (v3.3)\n\n"
        f"{queue_line}\n\n"
        f"**By requester:**\n{req_lines}\n"
    )
    
    sections.append(
        "\n### Feed freshness\n\n" +
        "\n".join(f"- {n}: {('never pulled' if a is None else str(a) + 'h')}" for n, a in feed_age.items()) + "\n"
    )

    # Integrity
    if recent_integrity:
        sections.append(
            f"\n### Integrity\n\n"
            f"- Events in 24h: {len(recent_integrity)}\n"
            f"- Latest: {recent_integrity[-1].get('event', 'n/a')} on "
            f"`{os.path.basename(recent_integrity[-1].get('path',''))}`\n"
        )
    else:
        sections.append(
            f"\n### Integrity\n\n"
            f"- No events in 24h (clean)\n"
        )

    # Counter intel
    sections.append(
        f"\n### Counter-intel\n\n"
        f"- {attack_sum}\n"
    )

    # Critical findings detail
    crit_items = [
        f for f in fs["items"] if f.get("severity") in ("CRITICAL", "HIGH")
    ]
    if crit_items:
        crit_lines = "\n".join(
            f"- `{f.get('category','?')}` ({f.get('severity','?')}): {f.get('detail','?')[:100]}"
            for f in crit_items[-10:]
        )
        sections.append(
            f"\n### Critical/high findings (last 10)\n\n{crit_lines}\n"
        )

    # Proactive question (v3.3 updated)
    sections.append(
        f"\n### Proactive\n\n"
        f"Items worth the agent's attention this cycle:\n"
        f"1. Review any P1 inbox packets from warden + doctor in last 24h\n"
        f"2. If a feed is stale in 'Feed freshness' and the book is open, pull fresh (json_relay/feed_request.json)\n"
        f"3. If code_tuner queue depth > 5 OR oldest > 48h, review pending requests\n"
        f"4. If doctor activity is high, consider investigating the underlying patterns\n"
        f"5. If integrity baseline drifted, refresh after legitimate changes\n"
        f"6. If medic heals failing repeatedly, doctor should already have escalated — check inbox\n"
    )

    return "".join(sections)


# ═══════════════════════════════════════════════════════════════════════════
# SPRINT 41 — LAYER 10: SKILL EVOLUTION + INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════
# Three responsibilities:
#   1. Skill catalog maintenance (auto-regenerated when SKILL.md mtime changes)
#   2. Skill integrity (hash-checks all SKILL.md files + sub-skills)
#   3. skill_requests.json processor - RETIRED (dead queue; amendments go agent_direct; reader/writer are no-ops)
#
# Risk-tier model identical to code_tuner:
#   LOW    → auto-apply with audit
#   MEDIUM → notify the owner (owner_inbox/pending_skill_requests/)
#   HIGH   → always the owner-gate
#   REFUSE → reject outright (HARD LAWS, identity, rules.md, etc.)
#
# Append-only audit chain at skill_evolution_log.jsonl tracks every event:
#   request_received | risk_assigned | auto_applied | owner_gated |
#   owner_approved | owner_rejected | owner_amended | archived | restored
# ═══════════════════════════════════════════════════════════════════════════


# Files that this layer REFUSES to modify under any circumstance.
# These have one canonical home per the system's "every fact has one home" rule.
SKILL_EVOLUTION_REFUSE_FILES = {
    "rules.md", "identity.md", "soul.md", "bootstrap.md",
    "agents.md", "tools.md", "user.md", "heartbeat.md",
}

# Tier 0 skills (executive function) can NEVER be amended through skill-evolution.
# These require full the owner review via skill-writer instead.
SKILL_EVOLUTION_TIER_0_SKILLS = {
    "boot-registry", "bridge", "problem-solver",
    "core-memory", "owner-ping",
}

# Confidence threshold for LOW-risk auto-apply
SKILL_EVO_AUTO_APPLY_CONFIDENCE = 0.7


def _se_append_log(event: str, data: dict) -> None:
    """Append-only audit log for skill evolution events. Best-effort."""
    record = {
        "ts":    datetime.now(EST).isoformat(),
        "event": event,
        **data,
    }
    try:
        os.makedirs(os.path.dirname(SKILL_EVOLUTION_LOG), exist_ok=True)
        with open(SKILL_EVOLUTION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[warden L10] evolution log append err: {e}")


def _se_parse_frontmatter(content: str) -> dict:
    r"""Parse YAML frontmatter from a markdown file. Returns {} on failure.
    Handles both `---` delimiters and escaped `\---` delimiters (editor artifact)."""
    if not content:
        return {}
    # Normalize escaped delimiters (some skills got escaped on upload)
    normalized = content.replace("\\---", "---")
    # Strip leading whitespace
    stripped = normalized.lstrip()
    if not stripped.startswith("---"):
        return {}
    try:
        # Find closing ---
        after_open = stripped[3:]
        end_idx = after_open.find("\n---")
        if end_idx < 0:
            return {}
        fm_text = after_open[:end_idx].strip()
        # Minimal YAML parse — we only need name, version, parent_skill, tier_within_parent,
        # applies_when, pinned, expires_at, inherits_doctrine. Not bringing in PyYAML.
        result = {}
        current_key = None
        for line in fm_text.split("\n"):
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            # Multi-line block scalars (description: |) — skip for now, we only need scalars
            if ":" in line and not line.startswith(" "):
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                current_key = key
                if val and val != "|" and val != ">":
                    # Inline value
                    # Handle lists
                    if val.startswith("[") and val.endswith("]"):
                        inner = val[1:-1].strip()
                        if inner:
                            result[key] = [x.strip().strip('"').strip("'") for x in inner.split(",")]
                        else:
                            result[key] = []
                    elif val.lower() in ("true", "false"):
                        result[key] = (val.lower() == "true")
                    elif val.lower() == "null" or val.lower() == "none":
                        result[key] = None
                    else:
                        result[key] = val.strip('"').strip("'")
                else:
                    result[key] = ""
        return result
    except Exception as e:
        print(f"[warden L10] frontmatter parse err: {e}")
        return {}


def _se_scan_skill_tree() -> dict:
    """Walk skills/ directory, return structured catalog:
       {
         "<skill_name>": {
            "path": "...",
            "frontmatter": {...},
            "sub_skills": {
               "learnings": [{...}, ...],
               "playbooks": [...],
               "references": [...],
            },
            "hash": "sha256:...",
         }
       }
    Also picks up _doctrine/ as a virtual skill.
    """
    catalog = {}
    if not os.path.isdir(SKILLS_DIR):
        return catalog

    for entry in os.listdir(SKILLS_DIR):
        entry_path = os.path.join(SKILLS_DIR, entry)
        # Skip files at top-level (only directories are skills)
        if not os.path.isdir(entry_path):
            continue
        # Skip _archived (graveyard)
        if entry == "_archived":
            continue

        skill_info = {
            "name":         entry,
            "path":         entry_path,
            "skill_md_path": None,
            "frontmatter":  {},
            "hash":         None,
            "sub_skills":   {"learnings": [], "playbooks": [], "references": []},
            "is_doctrine":  (entry == "_doctrine"),
        }

        # Look for SKILL.md
        skill_md = os.path.join(entry_path, "SKILL.md")
        if os.path.isfile(skill_md):
            skill_info["skill_md_path"] = skill_md
            skill_info["hash"] = file_sha256(skill_md)
            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    content = f.read()
                skill_info["frontmatter"] = _se_parse_frontmatter(content)
            except Exception as e:
                print(f"[warden L10] couldn't read {skill_md}: {e}")

        # _doctrine/ holds individual doctrine .md files, not SKILL.md
        if skill_info["is_doctrine"]:
            for fn in os.listdir(entry_path):
                if fn.endswith(".md") and not fn.startswith("_"):
                    doctrine_path = os.path.join(entry_path, fn)
                    try:
                        with open(doctrine_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        skill_info["sub_skills"]["learnings"].append({
                            "name": fn[:-3],
                            "path": doctrine_path,
                            "hash": file_sha256(doctrine_path),
                            "frontmatter": _se_parse_frontmatter(content),
                            "type": "doctrine",
                        })
                    except Exception:
                        pass
            catalog[entry] = skill_info
            continue

        # Scan sub-skill folders (learnings/, playbooks/, references/)
        for sub_type in ("learnings", "playbooks", "references"):
            sub_dir = os.path.join(entry_path, sub_type)
            if not os.path.isdir(sub_dir):
                continue
            for fn in os.listdir(sub_dir):
                if not fn.endswith(".md"):
                    continue
                # Skip _archived inside sub-folders
                if fn.startswith("_"):
                    continue
                fn_path = os.path.join(sub_dir, fn)
                try:
                    with open(fn_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    skill_info["sub_skills"][sub_type].append({
                        "name": fn[:-3],
                        "path": fn_path,
                        "hash": file_sha256(fn_path),
                        "frontmatter": _se_parse_frontmatter(content),
                        "type": sub_type[:-1],  # learnings → learning
                    })
                except Exception as e:
                    print(f"[warden L10] sub-skill read err {fn_path}: {e}")

        catalog[entry] = skill_info

    return catalog


def _se_save_catalog(catalog: dict) -> None:
    """Save the catalog as compressed (no full content) JSON for the agent + the owner reads."""
    out = {
        "generated_at": datetime.now(EST).isoformat(),
        "skill_count":  len(catalog),
        "skills":       {},
    }
    for name, info in catalog.items():
        out["skills"][name] = {
            "path":         info["path"],
            "skill_md_path": info["skill_md_path"],
            "hash":         info["hash"],
            "version":      info["frontmatter"].get("version"),
            "description":  info["frontmatter"].get("description", "")[:300] if info["frontmatter"].get("description") else None,
            "is_doctrine":  info["is_doctrine"],
            "sub_skill_counts": {
                k: len(v) for k, v in info["sub_skills"].items()
            },
            "sub_skills": {
                k: [
                    {
                        "name": ss["name"],
                        "type": ss["type"],
                        "hash": ss["hash"],
                        "tier_within_parent": ss["frontmatter"].get("tier_within_parent"),
                        "applies_when":       ss["frontmatter"].get("applies_when"),
                        "pinned":             ss["frontmatter"].get("pinned", False),
                        "expires_at":         ss["frontmatter"].get("expires_at"),
                        "author":             ss["frontmatter"].get("author"),
                    }
                    for ss in info["sub_skills"][k]
                ]
                for k in info["sub_skills"]
            },
        }
    try:
        os.makedirs(os.path.dirname(SKILL_CATALOG_FILE), exist_ok=True)
        with open(SKILL_CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
    except Exception as e:
        print(f"[warden L10] catalog save err: {e}")


def _se_build_cross_skill_doctrine_map(catalog: dict) -> dict:
    """For every skill's frontmatter inherits_doctrine: [...] field, build a
    reverse map: doctrine_name → [skills_inheriting_it].
    Also captures inline duplication: skills that should LINK doctrine but currently
    have it inlined (detected by simple section-title heuristic).
    """
    inherits_map = {}      # doctrine_name → set of skill_names
    inline_dupes = {}      # section_title → set of skill_names where it appears

    for name, info in catalog.items():
        if info["is_doctrine"]:
            continue  # the doctrine library itself doesn't inherit
        inherits = info["frontmatter"].get("inherits_doctrine") or []
        if isinstance(inherits, list):
            for d in inherits:
                inherits_map.setdefault(d, set()).add(name)

        # Heuristic check for inlined doctrine — common section titles that
        # tend to be duplicated across skills (passive-The agent, decline-feedback)
        if info["skill_md_path"]:
            try:
                with open(info["skill_md_path"], "r", encoding="utf-8") as f:
                    content = f.read()
                # Look for common doctrine titles
                for marker in ("Passive-The agent", "PASSIVE-AGENT", "passive-agent anti-pattern",
                               "structured decline", "STRUCTURED DECLINE",
                               "Position venue lock", "venue lock"):
                    if marker.lower() in content.lower():
                        # Normalize the title
                        key = marker.lower().split()[0]
                        inline_dupes.setdefault(key, set()).add(name)
            except Exception:
                pass

    # Identify divergence: a doctrine name appears in multiple skills' inherits AND
    # those skills have different file hashes for the SAME doctrine. (Future: when
    # _doctrine/ is populated, compare doctrine source hash to embedded references.)
    divergences = []
    # Convert sets to lists for JSON serialization
    inherits_out = {k: sorted(v) for k, v in inherits_map.items()}
    inline_out = {k: sorted(v) for k, v in inline_dupes.items() if len(v) > 1}

    # Flag potential extraction candidates: inline content in 2+ skills, no
    # corresponding inherits_doctrine declaration
    candidates_for_extraction = []
    for marker, skills in inline_out.items():
        # Check if any of these skills already inherits the doctrine
        already_inheriting = False
        for skill in skills:
            inherits = catalog[skill]["frontmatter"].get("inherits_doctrine") or []
            if isinstance(inherits, list):
                for d in inherits:
                    if marker.lower() in d.lower():
                        already_inheriting = True
                        break
        if not already_inheriting:
            candidates_for_extraction.append({
                "doctrine_marker": marker,
                "appearing_in":    skills,
                "recommendation":  f"extract to _doctrine/{marker}.md, add inherits_doctrine to {skills}",
            })

    return {
        "generated_at":            datetime.now(EST).isoformat(),
        "inheritance_map":         inherits_out,
        "inline_duplication":      inline_out,
        "divergences":             divergences,
        "extraction_candidates":   candidates_for_extraction,
    }


def _se_compute_risk_tier(req: dict, catalog: dict) -> tuple:
    """Evaluate a skill_request against the risk-tier rules.
    Returns (tier, reason).
       tier: "LOW" | "MEDIUM" | "HIGH" | "REFUSE"
    """
    operation = req.get("operation", "")
    target = req.get("target_skill", "")
    sub_skill_path = req.get("sub_skill_path", "") or ""

    # REFUSE: touching files that have one canonical home
    if target.lower() in {n.lower() for n in SKILL_EVOLUTION_REFUSE_FILES}:
        return "REFUSE", f"target '{target}' is in the refuse-list (has canonical home elsewhere)"
    # REFUSE: any operation modifying rules.md / identity.md etc.
    if target in SKILL_EVOLUTION_REFUSE_FILES:
        return "REFUSE", f"target '{target}' is HARD-LOCKED"

    # HIGH: touching Tier 0 skill cores
    if target in SKILL_EVOLUTION_TIER_0_SKILLS:
        return "HIGH", f"target '{target}' is a Tier 0 (executive function) skill"

    # HIGH: anything claiming to modify HARD LAW section
    section_name = (req.get("section_name") or "").lower()
    content = req.get("content", "") or ""
    if "hard law" in section_name or "hard_law" in section_name:
        return "HIGH", "section_name references HARD LAW"
    if "hard law" in content.lower() and operation in ("patch_skill", "rewrite_section"):
        # Patches that touch the words "hard law" need extra scrutiny
        return "HIGH", "content references HARD LAW (modification flagged)"

    # Operation-specific tiers
    if operation == "create_sub_skill":
        tier = req.get("tier_within_parent")
        if tier == 1:
            return "MEDIUM", "create_sub_skill with tier_within_parent=1 (always-applies sub-skill)"
        # T2 and T3 are LOW
        return "LOW", f"create_sub_skill with tier_within_parent={tier or 'unspecified'}"

    if operation == "archive_sub_skill":
        return "LOW", "archive_sub_skill (recoverable, no information loss)"

    if operation == "restore_sub_skill":
        return "LOW", "restore_sub_skill from _archived/"

    if operation in ("pin_skill", "unpin_skill"):
        return "LOW", f"{operation} (frontmatter flag toggle only)"

    if operation == "patch_skill":
        # MEDIUM if >20 lines added/changed, LOW otherwise
        line_count = len(content.split("\n")) if content else 0
        if line_count > 20:
            return "MEDIUM", f"patch_skill with {line_count} lines (>20 line threshold)"
        return "LOW", f"patch_skill with {line_count} lines"

    if operation == "rewrite_section":
        return "MEDIUM", "rewrite_section (always MEDIUM, full diff needs review)"

    if operation == "link_doctrine":
        return "MEDIUM", "link_doctrine (cross-skill change, structural)"

    # Unknown operation
    return "HIGH", f"unknown operation '{operation}'"


def _se_load_pending_requests() -> list:
    """RETIRED: skill_requests.json is a DEAD queue (meta-tools phase; amendments go
    agent_direct). Layer 10 no longer reads or processes it - a present-or-absent
    file must not be treated as a live queue (absence-is-signal). Always returns []
    so the Layer 10 process step is a permanent no-op. Do not revive the reader."""
    return []


def _se_mark_request_resolved(req_id: str, outcome: str) -> None:
    """RETIRED no-op: skill_requests.json is a DEAD queue and warden must NOT write it
    (do not revive the writer). Amendments go agent_direct; there is no queue to mark
    resolved. Kept as a no-op so any lingering caller does not error."""
    return


def _se_write_owner_review_packet(req: dict, tier: str, reason: str) -> str:
    """Write an owner-review packet to owner_inbox/pending_skill_requests/.
    Returns the path written."""
    ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
    req_id = req.get("id", "noid")
    operation = req.get("operation", "unknown")
    target = req.get("target_skill", "unknown")
    fname = f"{ts}_{req_id}_{operation}_{target}.md"
    out_path = os.path.join(OWNER_INBOX_SKILLS_DIR, fname)

    priority = "P1" if tier == "HIGH" else "P2"
    body = (
        f"# Skill Request — {operation} on {target}\n\n"
        f"**id:** {req_id}\n"
        f"**requested_at:** {req.get('requested_at', 'unknown')}\n"
        f"**requester:** {req.get('requester', 'unknown')}\n"
        f"**risk_tier:** {tier}\n"
        f"**risk_reason:** {reason}\n"
        f"**operation:** {operation}\n"
        f"**target_skill:** {target}\n"
        f"**sub_skill_path:** {req.get('sub_skill_path', '(n/a)')}\n"
        f"**tier_within_parent:** {req.get('tier_within_parent', '(n/a)')}\n"
        f"**applies_when:** {req.get('applies_when', '(n/a)')}\n"
        f"**expires_at:** {req.get('expires_at', 'null (permanent)')}\n"
        f"\n## Rationale\n\n{req.get('rationale', '(no rationale provided)')}\n"
        f"\n## Evidence\n\n"
    )
    evidence = req.get("evidence", []) or []
    if isinstance(evidence, list) and evidence:
        for e in evidence:
            body += f"- {e}\n"
    else:
        body += "- (no evidence provided)\n"

    body += f"\n## Proposed Content\n\n```\n{req.get('content', '(no content)')[:3000]}\n```\n"
    if req.get("inherits_doctrine"):
        body += f"\n## Inherits Doctrine\n\n{req['inherits_doctrine']}\n"

    body += (
        f"\n## The owner Decision\n\n"
        f"Approval flags are ignored. Patch the skill in place yourself.\n"
        f"- APPROVED/REJECTED/AMEND flags do not deploy bots or rewrite skills\n"
        f"- owner_approval_watcher is a no-op\n"
        f"- The agent applies his own patch in place\n"
        f"Do not drop a flag expecting warden to copy or apply it.\n"
    )

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"[warden L10] the owner review packet written: {fname}")
    except Exception as e:
        print(f"[warden L10] the owner packet write err: {e}")
    return out_path


async def _se_auto_apply_request(req: dict, catalog: dict) -> bool:
    """Apply a LOW-risk skill_request. Returns True on success.
    NOTE: This is the SAFE auto-apply path. Only operations that we've validated as
    LOW-risk land here. NO direct file writes for patch_skill — auto-apply currently
    only handles:
      - archive_sub_skill (move file to _archived/)
      - restore_sub_skill (move from _archived/ back)
      - pin_skill / unpin_skill (frontmatter flag)
      - create_sub_skill for tier_within_parent 2 or 3 (new file write)
    patch_skill and rewrite_section ALWAYS go to the owner even at LOW risk (more eyes
    on actual content modification).
    """
    operation = req.get("operation", "")
    target = req.get("target_skill", "")
    target_path = os.path.join(SKILLS_DIR, target)

    if not os.path.isdir(target_path) and operation not in ("restore_sub_skill",):
        print(f"[warden L10] auto-apply: target dir not found {target_path}")
        return False

    # ── archive_sub_skill ────────────────────────────────────────────
    if operation == "archive_sub_skill":
        sub_path = req.get("sub_skill_path", "")
        if not sub_path:
            return False
        # sub_path is relative to skill dir, e.g. "learnings/cooldown.md"
        src = os.path.join(target_path, sub_path)
        if not os.path.isfile(src):
            print(f"[warden L10] archive: source not found {src}")
            return False
        # Move to <sub_type>/_archived/
        sub_type_dir = os.path.dirname(sub_path)  # e.g. "learnings"
        archived_dir = os.path.join(target_path, sub_type_dir, "_archived")
        os.makedirs(archived_dir, exist_ok=True)
        dest = os.path.join(archived_dir, os.path.basename(sub_path))
        try:
            shutil.move(src, dest)
            print(f"[warden L10] archived sub-skill: {sub_path} → {sub_type_dir}/_archived/")
            return True
        except Exception as e:
            print(f"[warden L10] archive err: {e}")
            return False

    # ── pin / unpin ──────────────────────────────────────────────────
    if operation in ("pin_skill", "unpin_skill"):
        sub_path = req.get("sub_skill_path", "")
        if not sub_path:
            return False
        target_file = os.path.join(target_path, sub_path)
        if not os.path.isfile(target_file):
            return False
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                content = f.read()
            new_pinned = (operation == "pin_skill")
            # Naive frontmatter edit: replace "pinned: <bool>" or add it
            if re.search(r"^pinned:\s*\w+", content, re.MULTILINE):
                content = re.sub(r"^pinned:\s*\w+", f"pinned: {new_pinned}", content, flags=re.MULTILINE)
            else:
                # Insert into frontmatter (between --- markers)
                content = re.sub(
                    r"(---\n)",
                    f"\\1pinned: {new_pinned}\n",
                    content, count=1,
                )
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[warden L10] {operation}: {sub_path} → pinned={new_pinned}")
            return True
        except Exception as e:
            print(f"[warden L10] pin/unpin err: {e}")
            return False

    # ── create_sub_skill ─────────────────────────────────────────────
    if operation == "create_sub_skill":
        sub_path = req.get("sub_skill_path", "")
        if not sub_path:
            return False
        full_path = os.path.join(target_path, sub_path)
        # Refuse to overwrite
        if os.path.exists(full_path):
            print(f"[warden L10] create: target already exists {full_path}")
            return False
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = req.get("content", "")
        # If frontmatter not present, synthesize one
        if not content.lstrip().startswith("---"):
            fm = (
                f"---\n"
                f"parent_skill: {target}\n"
                f"tier_within_parent: {req.get('tier_within_parent', 3)}\n"
                f"applies_when: \"{req.get('applies_when', '')}\"\n"
                f"version: 1.0.0\n"
                f"created_at: \"{datetime.now(EST).isoformat()}\"\n"
                f"author: {req.get('requester', 'agent')}\n"
                f"pinned: false\n"
                f"expires_at: {req.get('expires_at') or 'null'}\n"
                f"---\n\n"
            )
            content = fm + content
        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[warden L10] created sub-skill: {target}/{sub_path}")
            return True
        except Exception as e:
            print(f"[warden L10] create err: {e}")
            return False

    # restore_sub_skill — fetch from _archived/ back to live
    if operation == "restore_sub_skill":
        sub_path = req.get("sub_skill_path", "")
        if not sub_path:
            return False
        sub_type_dir = os.path.dirname(sub_path)
        src = os.path.join(target_path, sub_type_dir, "_archived", os.path.basename(sub_path))
        dest = os.path.join(target_path, sub_path)
        if not os.path.isfile(src):
            return False
        if os.path.exists(dest):
            return False  # Don't overwrite live
        try:
            shutil.move(src, dest)
            print(f"[warden L10] restored sub-skill: {sub_path} from _archived/")
            return True
        except Exception as e:
            print(f"[warden L10] restore err: {e}")
            return False

    print(f"[warden L10] auto-apply: unhandled operation '{operation}' (should not reach here)")
    return False


def _se_process_one_request(req: dict, catalog: dict) -> str:
    """Triage one skill request. Returns outcome string for the audit log."""
    req_id = req.get("id", "noid")
    operation = req.get("operation", "unknown")

    # Compute risk tier
    tier, reason = _se_compute_risk_tier(req, catalog)
    _se_append_log("risk_assigned", {
        "req_id": req_id, "operation": operation,
        "tier": tier, "reason": reason,
    })

    # REFUSE → reject + the agent notify
    if tier == "REFUSE":
        content = (
            f"# Skill request REFUSED\n\n"
            f"**id:** {req_id}\n"
            f"**operation:** {operation}\n"
            f"**target:** {req.get('target_skill', '?')}\n"
            f"**reason:** {reason}\n\n"
            f"This request was refused because it targets a file or skill that "
            f"has a canonical home outside the skill-evolution system. To make "
            f"changes there, use the appropriate channel (rules.md edits go via "
            f"The owner direct; identity changes via the owner direct; Tier 0 skills via "
            f"skill-writer).\n"
        )
        write_inbox_packet("P3", "warden_skill_evo", f"refused_{req_id}", content, target_inbox="agent")
        _se_mark_request_resolved(req_id, "refused")
        _se_append_log("refused", {"req_id": req_id, "reason": reason})
        return "refused"

    # HIGH / MEDIUM → the owner gate
    if tier in ("HIGH", "MEDIUM"):
        _se_write_owner_review_packet(req, tier, reason)
        _se_mark_request_resolved(req_id, f"owner_gated_{tier.lower()}")
        _se_append_log("owner_gated", {"req_id": req_id, "tier": tier})
        return "owner_gated"

    # LOW + confidence check for auto-apply path
    confidence = float(req.get("confidence", 0.0) or 0.0)
    if confidence < SKILL_EVO_AUTO_APPLY_CONFIDENCE:
        _se_write_owner_review_packet(req, "MEDIUM_via_low_confidence", reason + f" (confidence {confidence} below {SKILL_EVO_AUTO_APPLY_CONFIDENCE})")
        _se_mark_request_resolved(req_id, "owner_gated_low_confidence")
        _se_append_log("owner_gated_low_confidence", {"req_id": req_id, "confidence": confidence})
        return "owner_gated_low_confidence"

    # patch_skill and rewrite_section even at LOW go to the owner (more eyes on real content edits)
    if operation in ("patch_skill", "rewrite_section"):
        _se_write_owner_review_packet(req, "LOW_content_edit", reason)
        _se_mark_request_resolved(req_id, "owner_gated_content_edit")
        _se_append_log("owner_gated_content_edit", {"req_id": req_id, "operation": operation})
        return "owner_gated_content_edit"

    # LOW + high confidence + safe operation: auto-apply
    # NOTE: this is async-friendly, but we wrap synchronously since file ops are local
    try:
        # Awaiting in sync context: use asyncio.run? No, we're in async layer.
        # Just call directly since _se_auto_apply_request is a coroutine.
        # The caller (the loop) will handle await.
        pass  # placeholder — actual await happens in the loop
    except Exception:
        pass
    # Returning "needs_apply" signal so the async caller handles the await
    return "needs_apply"


async def _se_process_one_request_async(req: dict, catalog: dict) -> None:
    """Async wrapper for one request processing."""
    outcome = _se_process_one_request(req, catalog)
    if outcome != "needs_apply":
        return

    # LOW + high confidence + safe op → auto-apply
    req_id = req.get("id", "noid")
    success = await _se_auto_apply_request(req, catalog)
    if success:
        _se_mark_request_resolved(req_id, "auto_applied")
        _se_append_log("auto_applied", {"req_id": req_id, "operation": req.get("operation")})
        # P3 to the agent summarizing what changed
        content = (
            f"# Skill request auto-applied\n\n"
            f"**id:** {req_id}\n"
            f"**operation:** {req.get('operation')}\n"
            f"**target:** {req.get('target_skill')}\n"
            f"**sub_skill_path:** {req.get('sub_skill_path', '(n/a)')}\n"
            f"**rationale:** {req.get('rationale', '(none)')}\n\n"
            f"This was a LOW-risk request with confidence ≥ {SKILL_EVO_AUTO_APPLY_CONFIDENCE}. "
            f"Applied without the owner gate per skill-evolution doctrine. Full audit in "
            f"`bot_logs/skill_evolution_log.jsonl`.\n"
        )
        write_inbox_packet("P3", "warden_skill_evo", f"auto_applied_{req_id}", content, target_inbox="agent")
    else:
        _se_mark_request_resolved(req_id, "auto_apply_failed")
        _se_append_log("auto_apply_failed", {"req_id": req_id})
        content = (
            f"# Skill request auto-apply FAILED\n\n"
            f"**id:** {req_id}\n"
            f"**operation:** {req.get('operation')}\n\n"
            f"Auto-apply path failed (file conflict, missing source, or permission "
            f"error). Check warden logs for details. Request marked resolved with "
            f"outcome=auto_apply_failed.\n"
        )
        write_inbox_packet("P2", "warden_skill_evo", f"failed_{req_id}", content, target_inbox="agent")


async def skill_evolution_monitor():
    """Layer 10 main loop.
    1. Every 5 min: scan skills/, regenerate catalog if any hash changed.
    2. (RETIRED) skill_requests.json processing - dead queue, permanent no-op (agent_direct).
    3. Daily: cross-skill doctrine integrity check + divergence flagging.
    4. (Sprint 42) 3-day review packet MIGRATED to code_tuner v4.1.
       code_tuner.skill_evolution_review_loop reads the same evolution log
       warden writes and fires the same shape of P3 packet (content-only
       doctrine — empty windows produce no packet).
    """
    if not load_config().get("skill_evolution_enabled", True):
        print("[warden L10] skill_evolution_monitor disabled in config")
        return

    print("[warden L10] skill evolution + integrity monitor starting")
    last_doctrine_check = 0
    last_3day_packet = _se_load_last_3day_packet_ts()  # persists across restarts
    DOCTRINE_CHECK_INTERVAL = 86400  # daily
    THREE_DAY_INTERVAL = 3 * 86400   # 72h

    while True:
        try:
            # 1. Scan + catalog
            catalog = _se_scan_skill_tree()
            _se_save_catalog(catalog)

            # 2. Process requests
            pending = _se_load_pending_requests()
            if pending:
                print(f"[warden L10] processing {len(pending)} pending skill requests")
                for req in pending:
                    try:
                        _se_append_log("request_received", {
                            "req_id": req.get("id"),
                            "operation": req.get("operation"),
                            "target": req.get("target_skill"),
                            "requester": req.get("requester"),
                        })
                        await _se_process_one_request_async(req, catalog)
                    except Exception as e:
                        print(f"[warden L10] request {req.get('id')} processing err: {e}")
                        traceback.print_exc()

            # 3. Daily doctrine cross-check
            now = time.time()
            if now - last_doctrine_check > DOCTRINE_CHECK_INTERVAL:
                doctrine_map = _se_build_cross_skill_doctrine_map(catalog)
                try:
                    with open(CROSS_SKILL_DOCTRINE_MAP, "w", encoding="utf-8") as f:
                        json.dump(doctrine_map, f, indent=2, default=str)
                except Exception as e:
                    print(f"[warden L10] doctrine map write err: {e}")

                # If there are extraction candidates, write a P3 packet to the agent
                candidates = doctrine_map.get("extraction_candidates", [])
                if candidates:
                    content_lines = [
                        "# Cross-Skill Doctrine Drift Detected\n",
                        f"\nLayer 10 found {len(candidates)} candidate(s) where the same doctrine appears in 2+ skills WITHOUT a `inherits_doctrine` reference. Consider extracting to `skills/_doctrine/`.\n",
                        "\n## Candidates\n",
                    ]
                    for c in candidates:
                        content_lines.append(
                            f"\n### `{c['doctrine_marker']}`\n"
                            f"- Appears in: {', '.join(c['appearing_in'])}\n"
                            f"- Recommendation: {c['recommendation']}\n"
                        )
                    content_lines.append(
                        "\n## Suggested action\n\n"
                        "Extract these to `skills/_doctrine/` via agent_direct (the "
                        "skill_requests.json queue is retired), adding "
                        "`inherits_doctrine: [...]` references to each affected skill.\n"
                    )
                    write_inbox_packet("P3", "warden_skill_evo", "doctrine_drift",
                                       "".join(content_lines), target_inbox="agent")
                last_doctrine_check = now

            # 4. (Sprint 42) 3-day skill-evolution review packet — MIGRATED to code_tuner v4.1
            # Previously this block fired the warden_skill_evo/3day_review packet.
            # As of Sprint 42, code_tuner.skill_evolution_review_loop fires it instead
            # (content-only doctrine — no packet if nothing evolved in the 72h window).
            # skill_requests.json processing (step 2) is RETIRED - dead queue,
            # amendments go agent_direct; step 2 is now a permanent no-op. warden
            # still owns the daily doctrine cross-check (step 3). code_tuner reads the
            # same skill_evolution_log.jsonl warden writes; it doesn't mutate it.
            # To re-enable warden-side packet writing in an emergency:
            #   1. Uncomment the block below
            #   2. Set CODE_TUNER_SKILL_EVO_INTERVAL_S=999999999 in .env (disables code_tuner loop)
            # ──────────────────────────────────────────────────────────────────
            # if now - last_3day_packet > THREE_DAY_INTERVAL:
            #     try:
            #         packet_content = _se_build_3day_review_packet(catalog)
            #         write_inbox_packet("P3", "warden_skill_evo", "3day_review",
            #                            packet_content, target_inbox="agent")
            #         _se_save_last_3day_packet_ts(now)
            #         _se_append_log("3day_packet_fired", {
            #             "fired_at": datetime.now(EST).isoformat(),
            #             "catalog_skill_count": len(catalog),
            #         })
            #         last_3day_packet = now
            #     except Exception as e:
            #         print(f"[warden L10] 3day packet err: {e}")
            #         traceback.print_exc()
            # ──────────────────────────────────────────────────────────────────
            # End Sprint 42 migration block

        except Exception as e:
            print(f"[warden L10] monitor cycle err: {e}")
            traceback.print_exc()

        await asyncio.sleep(load_config().get("skill_evolution_check_interval_seconds", 300))


# ═══════════════════════════════════════════════════════════════════════════
# END LAYER 10
# ═══════════════════════════════════════════════════════════════════════════


async def daily_inbox_dispatcher():
    """
    Once per day at configured hour EST, write a P3 digest packet to the agent's inbox.
    Replaces v3.1's daily_summary_report Telegram-only mode.
    """
    print("[warden] daily inbox dispatcher active")
    cfg = load_config()
    target_hour = cfg.get("daily_digest_hour_est", 8)

    while True:
        try:
            now = datetime.now(EST)
            target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)

            await fire_daily_digest()

        except Exception as e:
            print(f"[warden] daily digest err: {e}")
            await asyncio.sleep(3600)


async def fire_daily_digest():
    """Build + write daily digest packet (also callable on demand via WARDEN DIGEST tg cmd)."""
    content = build_daily_digest_content()
    write_inbox_packet(
        priority="P3", source="warden_daily",
        slug=f"daily_digest_{datetime.now(EST).strftime('%Y%m%d')}",
        content=content,
    )
    # Also brief telegram
    summary = get_24h_findings_summary()
    msg = (
        f"🛡️ <b>Warden Daily Digest</b>\n"
        f"{datetime.now(EST).strftime('%Y-%m-%d')}\n\n"
        f"Findings (24h): {summary['total']}\n"
        f"Critical: {summary['by_severity'].get('CRITICAL', 0)} | "
        f"High: {summary['by_severity'].get('HIGH', 0)}\n"
        f"Lockdown: {'⛔' if is_lockdown_active() else '✅'} | "
        f"Kill: {'⛔' if is_kill_switch_active() else '✅'}\n\n"
        f"Full digest written to the agent inbox (P3)."
    )
    await notify(msg)


# Telegram command channel
TG_COMMAND_HANDLERS = {}  # populated below


def register_tg_command(cmd_pattern: str):
    """Decorator to register telegram command handlers."""
    def decorator(func):
        TG_COMMAND_HANDLERS[cmd_pattern] = func
        return func
    return decorator


@register_tg_command(r"^WARDEN\s+STATUS\s*$")
async def cmd_warden_status(match, raw_text: str) -> str:
    findings = get_24h_findings_summary()
    return (
        f"🛡️ Warden Status\n"
        f"Lockdown: {'⛔ ACTIVE' if is_lockdown_active() else '✅ clear'}\n"
        f"Kill switch: {'⛔ ENGAGED' if is_kill_switch_active() else '✅ clear'}\n"
        f"24h findings: {findings['total']}\n"
        f"Critical: {findings['by_severity'].get('CRITICAL',0)} | "
        f"High: {findings['by_severity'].get('HIGH',0)} | "
        f"Med: {findings['by_severity'].get('MEDIUM',0)}"
    )


@register_tg_command(r"^WARDEN\s+LOCKDOWN\s*(.*)$")
async def cmd_warden_lockdown(match, raw_text: str) -> str:
    reason = match.group(1).strip() or "Telegram command"
    engage_lockdown(reason)
    return f"⛔ Lockdown ENGAGED: {reason}\nUse `WARDEN RESUME` to lift."


@register_tg_command(r"^WARDEN\s+RESUME\s*$")
async def cmd_warden_resume(match, raw_text: str) -> str:
    if not is_lockdown_active():
        return "Lockdown not active."
    lift_lockdown()
    return "✅ Lockdown lifted."


@register_tg_command(r"^WARDEN\s+DIGEST\s*$")
async def cmd_warden_digest(match, raw_text: str) -> str:
    await fire_daily_digest()
    return "Digest fired (inbox + telegram brief)."


@register_tg_command(r"^MEDIC\s+HEAL\s+(\S+)\s*$")
async def cmd_medic_heal(match, raw_text: str) -> str:
    bot = match.group(1).strip()
    if not bot.endswith(".py"):
        bot = f"{bot}.py"
    result = await medic_attempt_heal(bot, error_message="manual_telegram_request")
    return (
        f"🏥 Medic heal {bot}\n"
        f"Outcome: {result.get('outcome')}\n" +
        "\n".join(f"- {a}" for a in result.get('actions', [])[:5])
    )


@register_tg_command(r"^MEDIC\s+ROLLBACK\s+(\S+)\s*$")
async def cmd_medic_rollback(match, raw_text: str) -> str:
    bot = match.group(1).strip()
    if not bot.endswith(".py"):
        bot = f"{bot}.py"
    result = await medic_rollback(bot)
    return (
        f"⏪ Medic rollback {bot}\n"
        f"Outcome: {result.get('outcome')}\n"
        f"Restored from: {result.get('restored_from', 'n/a')}"
    )


@register_tg_command(r"^MEDIC\s+STATUS\s*$")
async def cmd_medic_status(match, raw_text: str) -> str:
    summary = get_medic_24h_summary()
    return (
        f"🏥 Medic 24h Status\n"
        f"Actions: {summary['total']}\n"
        f"Heals succeeded: {summary['heals_succeeded']}\n"
        f"Heals failed: {summary['heals_failed']}\n"
        f"By action: " +
        ", ".join(f"{k}={v}" for k, v in summary['by_action'].items())
    )


@register_tg_command(r"^INTEGRITY\s+CHECK\s*$")
async def cmd_integrity_check(match, raw_text: str) -> str:
    summary = await integrity_check_now()
    return (
        f"🔐 Integrity check\n"
        f"Checked: {summary.get('checked', 0)}\n"
        f"Matches: {summary.get('matches', 0)}\n"
        f"Mismatches: {len(summary.get('mismatches', []))}\n"
        f"Missing: {len(summary.get('missing', []))}"
    )


@register_tg_command(r"^INTEGRITY\s+BASELINE\s*$")
async def cmd_integrity_baseline(match, raw_text: str) -> str:
    baseline = integrity_baseline_snapshot()
    return f"🔐 Baseline refreshed: {len(baseline)} files snapshotted as known-good."


async def parse_and_dispatch_tg_command(raw_text: str) -> str:
    """Match raw incoming Telegram text against registered commands."""
    text = raw_text.strip()
    for pattern, handler in TG_COMMAND_HANDLERS.items():
        m = re.match(pattern, text, re.IGNORECASE)
        if m:
            try:
                return await handler(m, raw_text)
            except Exception as e:
                return f"Handler error: {e}"
    return ""  # no match — silently ignore




# ═══════════════════════════════════════════════════════════════════════════
# BOOT SEQUENCE (from WARDEN-WINDOWS spec)
# ═══════════════════════════════════════════════════════════════════════════

async def boot_sequence() -> bool:
    """
    Run boot checks before starting other layers.
    Returns True if boot is OK to proceed, False if must halt.
    """
    print("=" * 60)
    print("[warden] BOOT SEQUENCE")
    print("=" * 60)

    # Step 1: lockdown check
    if is_lockdown_active():
        msg = ("⚠️ Warden booting INTO lockdown state. "
               "Critical layers active, but bots-that-honor-lockdown will halt writes. "
               "Send `WARDEN RESUME` from Telegram to lift.")
        print(f"[warden/boot] {msg}")
        await notify(msg)

    # Step 2: verify workspace exists
    if not os.path.isdir(WORKSPACE):
        msg = f"❌ Workspace missing: {WORKSPACE}"
        print(f"[warden/boot] {msg}")
        await notify(msg)
        return False

    if not os.path.isdir(BOT_LOGS):
        os.makedirs(BOT_LOGS, exist_ok=True)
        print(f"[warden/boot] created bot_logs dir")

    if not os.path.isdir(INBOX):
        os.makedirs(INBOX, exist_ok=True)
        print(f"[warden/boot] created inbox dir")

    # Step 3: load config (creates default if missing)
    cfg = load_config()
    print(f"[warden/boot] config loaded")

    # Step 4: integrity baseline check
    if not os.path.exists(HASH_BASELINE):
        print("[warden/boot] no integrity baseline — snapshotting now")
        integrity_baseline_snapshot()
    else:
        print("[warden/boot] integrity baseline present")

    # Step 5: quick integrity scan on critical files
    print("[warden/boot] running quick integrity check...")
    summary = await integrity_check_now()
    if summary.get("mismatches"):
        print(f"[warden/boot] ⚠️ {len(summary['mismatches'])} files mismatch baseline")
        # Don't halt — just alert. Sprint chops mean baseline will commonly drift.
    else:
        print(f"[warden/boot] integrity nominal: {summary.get('matches',0)}/{summary.get('checked',0)}")

    # Step 6: kill switch check
    if is_kill_switch_active():
        print("[warden/boot] ℹ️ KILL_SWITCH.flag exists — manual flag, individual bots may check")

    # Step 7: log boot complete
    log_finding("boot", "INFO", "warden boot sequence complete", "")
    print("[warden/boot] boot complete — starting layers")
    print("=" * 60)
    return True


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def bot_builder_inbox_loop():
    """Run bot-builder inbox check on a slow cadence."""
    print("[warden] bot-builder inbox loop active")
    while True:
        try:
            await asyncio.sleep(600)  # 10 min
            if is_lockdown_active():
                continue
            await bot_builder_check_inbox()
        except Exception as e:
            print(f"[warden/builder] loop err: {e}")
            await asyncio.sleep(1800)


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 6d: OWNER APPROVAL WATCHER — DISABLED NO-OP
# Does not poll flags. APPROVED / AMEND / REJECTED must not copy a bot
# into bots/ or rewrite a skill. The agent patches in place.
# Notes go to the agent's inbox. Done folder is inbox_results/done.
OWNER_APPROVAL_ARCHIVE = os.path.join(OWNER_INBOX, "archive")
START_BOTS_MANIFEST   = os.path.join(BOTS_DIR, "Start_Bots_manifest.json")
OWNER_APPROVAL_POLL_SECONDS = 60


def _load_start_bots_manifest() -> list:
    """Read the bots Start_Bots.bat should launch. Returns list of dicts:
    [{"bot": "<name>.py", "added_at": "<iso>", "approved_by": "owner"}]
    """
    data = load_json(START_BOTS_MANIFEST, [])
    if not isinstance(data, list):
        return []
    return data


def _save_start_bots_manifest(entries: list):
    save_json(START_BOTS_MANIFEST, entries)


def _archive_approval_flag(flag_path: str) -> str:
    """Move a processed flag to owner_inbox/archive/<ts>_<filename>."""
    try:
        os.makedirs(OWNER_APPROVAL_ARCHIVE, exist_ok=True)
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        filename = os.path.basename(flag_path)
        archive_path = os.path.join(OWNER_APPROVAL_ARCHIVE, f"{ts}_{filename}")
        shutil.move(flag_path, archive_path)
        return archive_path
    except Exception as e:
        print(f"[warden/approval] archive failed for {flag_path}: {e}")
        return ""


async def _process_approved_bot(bot_name: str, flag_path: str) -> dict:
    """DISABLED. Flags must not copy a staging file into bots/."""
    return {
        "bot_name": bot_name,
        "outcome": "disabled_noop",
        "details": "approval watcher is a no-op; bots/ is not written from flags",
        "deploy_path": "",
    }

async def _process_rejected_bot(bot_name: str, flag_path: str) -> dict:
    """Reject a bot — remove staging, archive flag."""
    result = {"bot_name": bot_name, "outcome": "unknown", "details": ""}
    staging_path = os.path.join(MEDIC_STAGING, bot_name)
    if os.path.exists(staging_path):
        try:
            # Move staging to a rejected/ archive folder so we keep history
            rejected_dir = os.path.join(MEDIC_STAGING, "rejected")
            os.makedirs(rejected_dir, exist_ok=True)
            ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
            archive_path = os.path.join(rejected_dir, f"{ts}_{bot_name}")
            shutil.move(staging_path, archive_path)
            result["outcome"] = "rejected"
            result["details"] = f"Staging archived to {archive_path}"
        except Exception as e:
            result["outcome"] = "reject_archive_failed"
            result["details"] = str(e)
    else:
        result["outcome"] = "no_staging_to_reject"
        result["details"] = "No staging file existed for this name."
    return result


def _se_find_pending_skill_packet(req_id: str) -> str | None:
    """Look up a pending skill-request packet in owner_inbox/pending_skill_requests/
    matching the given request id. Returns the packet path or None."""
    if not os.path.isdir(OWNER_INBOX_SKILLS_DIR):
        return None
    try:
        for fn in os.listdir(OWNER_INBOX_SKILLS_DIR):
            # Packets are named <ts>_<req_id>_<operation>_<target>.md
            if f"_{req_id}_" in fn or fn.startswith(f"{req_id}_"):
                full = os.path.join(OWNER_INBOX_SKILLS_DIR, fn)
                if os.path.isfile(full):
                    return full
    except Exception:
        return None
    return None


def _se_parse_pending_skill_packet(packet_path: str) -> dict:
    """Parse the structured fields from a owner-review skill packet.
    Returns dict with operation, target_skill, sub_skill_path, etc."""
    fields = {}
    try:
        with open(packet_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return fields
    # Simple line-by-line scan for **key:** value lines
    for line in content.split("\n"):
        m = re.match(r"\*\*(\w+):\*\*\s*(.+)", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            fields[key] = val
    # Extract "Proposed Content" block
    m = re.search(r"## Proposed Content\s*\n```\n(.*?)\n```", content, re.DOTALL)
    if m:
        fields["content"] = m.group(1)
    return fields


async def _se_process_approved_skill_request(req_id: str, packet_path: str) -> dict:
    """DISABLED. APPROVED flags must not rewrite a skill."""
    return {"outcome": "disabled_noop", "details": "approval flags do not rewrite skills"}

async def _se_process_rejected_skill_request(req_id: str, packet_path: str) -> dict:
    """The owner rejected a skill request via REJECTED_<req_id>.flag.
    Archive the packet, mark resolved, return result."""
    try:
        archive_dir = os.path.join(OWNER_INBOX, "archive")
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = os.path.join(archive_dir, os.path.basename(packet_path))
        shutil.move(packet_path, archive_path)
    except Exception as e:
        print(f"[warden L10] packet archive err: {e}")

    _se_mark_request_resolved(req_id, "owner_rejected")
    _se_append_log("owner_rejected", {"req_id": req_id})
    return {"outcome": "rejected", "details": f"request {req_id} rejected by the owner, not applied"}


async def _se_process_amend_skill_request(req_id: str, amend_path: str, packet_path: str) -> dict:
    """DISABLED. AMEND flags must not rewrite a skill."""
    return {"outcome": "disabled_noop", "details": "AMEND flags do not rewrite skills"}

async def _se_notify_skill_result(req_id: str, action: str, res: dict) -> None:
    """Telegram + the agent inbox notification for a skill request resolution."""
    outcome = res.get("outcome", "?")
    details = res.get("details", "")
    emoji = "✅" if outcome == "applied" else ("❌" if outcome == "rejected" else "⚠️")

    try:
        await notify(
            f"{emoji} <b>Skill request {action}</b>\n"
            f"id: <code>{req_id}</code>\n"
            f"outcome: {outcome}\n"
            f"details: {details[:200]}"
        )
    except Exception:
        pass

    content = (
        f"# Skill Request Result — {req_id}\n\n"
        f"**Action:** {action}\n"
        f"**Outcome:** {outcome}\n"
        f"**Details:** {details}\n\n"
        f"Full audit in `bot_logs/skill_evolution_log.jsonl`.\n"
    )
    priority = "P2" if outcome in ("apply_failed",) else "P3"
    write_inbox_packet(priority, "warden_skill_evo", f"skill_result_{req_id}", content)


# ─── Sprint 41 — 3-day skill-evolution review packet ────────────────────────

SKILL_EVO_3DAY_STATE_FILE = os.path.join(BOT_LOGS, "skill_evolution_3day_state.json")


def _se_load_last_3day_packet_ts() -> float:
    """Load the unix timestamp of the last 3-day packet fire. 0 if never fired."""
    if not os.path.isfile(SKILL_EVO_3DAY_STATE_FILE):
        return 0.0
    try:
        with open(SKILL_EVO_3DAY_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("last_3day_packet_ts", 0))
    except Exception:
        return 0.0


def _se_save_last_3day_packet_ts(ts: float) -> None:
    """Persist last-fired timestamp so warden restarts don't reset the cadence."""
    try:
        with open(SKILL_EVO_3DAY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "last_3day_packet_ts":  ts,
                "last_3day_packet_iso": datetime.fromtimestamp(ts, EST).isoformat(),
            }, f, indent=2)
    except Exception as e:
        print(f"[warden L10] 3day state save err: {e}")


def _se_read_evolution_log_window(hours: int) -> list:
    """Read entries from skill_evolution_log.jsonl within the last <hours> hours.
    Returns list of parsed records."""
    if not os.path.isfile(SKILL_EVOLUTION_LOG):
        return []
    cutoff_ts = (datetime.now(EST) - timedelta(hours=hours)).timestamp()
    out = []
    try:
        with open(SKILL_EVOLUTION_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts_iso = rec.get("ts")
                if not ts_iso:
                    continue
                try:
                    rec_ts = datetime.fromisoformat(ts_iso).timestamp()
                except Exception:
                    continue
                if rec_ts >= cutoff_ts:
                    out.append(rec)
    except Exception as e:
        print(f"[warden L10] evolution log read err: {e}")
    return out


def _se_read_recent_code_requests_from_meeting_watchers(hours: int) -> list:
    """Sprint 41 cross-cut: read code_requests.json entries authored by
    bot meeting watchers (requester ends in '_meeting_watcher') in last <hours>.
    These are the capability-gap routes that fed the self-evolution loop."""
    code_req_path = os.path.join(BOT_LOGS, "code_requests.json")
    if not os.path.isfile(code_req_path):
        return []
    cutoff_ts = (datetime.now(EST) - timedelta(hours=hours)).timestamp()
    out = []
    try:
        with open(code_req_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for req in data.get("requests", []):
            requester = req.get("requester", "")
            if not requester.endswith("_meeting_watcher"):
                continue
            try:
                req_ts = datetime.fromisoformat(req.get("requested_at", "")).timestamp()
            except Exception:
                continue
            if req_ts >= cutoff_ts:
                out.append(req)
    except Exception as e:
        print(f"[warden L10] code_requests read err: {e}")
    return out


def _se_count_stale_subskills(catalog: dict) -> dict:
    """Walk the catalog, count sub-skills that have NEVER been written to since
    creation OR whose expires_at has passed. Returns {tier: [list of stale paths]}.
    Note: 'unused 30d/60d' detection requires access to skill load events, which
    we don't track yet — so this v1 only catches frontmatter `expires_at`."""
    stale = {"tier_1": [], "tier_2": [], "tier_3": [], "pinned_exempt": []}
    now_ts = datetime.now(EST).timestamp()
    for skill_name, info in catalog.items():
        if info.get("is_doctrine"):
            continue
        for sub_type in info.get("sub_skills", {}):
            for ss in info["sub_skills"][sub_type]:
                fm = ss.get("frontmatter", {})
                pinned = fm.get("pinned", False)
                tier = fm.get("tier_within_parent")
                expires_at = fm.get("expires_at")
                if pinned:
                    if expires_at and expires_at != "null":
                        # Pinned + expires_at set = anomaly, surface separately
                        try:
                            exp_ts = datetime.fromisoformat(expires_at).timestamp()
                            if exp_ts < now_ts:
                                stale["pinned_exempt"].append(f"{skill_name}/{sub_type}/{ss['name']}")
                        except Exception:
                            pass
                    continue
                if expires_at and expires_at != "null" and expires_at is not None:
                    try:
                        exp_ts = datetime.fromisoformat(expires_at).timestamp()
                        if exp_ts < now_ts:
                            key = f"tier_{tier}" if tier in (1, 2, 3) else "tier_3"
                            stale[key].append(f"{skill_name}/{sub_type}/{ss['name']}")
                    except Exception:
                        pass
    return stale


def _se_build_3day_review_packet(catalog: dict) -> str:
    """Build the markdown body for the 3-day skill-evolution review packet.
    This is the warden→the agent ping that prompts the 3-day reflection loop per
    skill-evolution doctrine."""
    window_hours = 72  # 3 days

    # Pull evolution log slice
    log_window = _se_read_evolution_log_window(window_hours)

    # Count events by type
    event_counts = {}
    for rec in log_window:
        ev = rec.get("event", "unknown")
        event_counts[ev] = event_counts.get(ev, 0) + 1

    auto_applied = [r for r in log_window if r.get("event") == "auto_applied"]
    owner_approved = [r for r in log_window if r.get("event") == "owner_approved_and_applied"]
    owner_rejected = [r for r in log_window if r.get("event") == "owner_rejected"]
    owner_amended = [r for r in log_window if r.get("event") == "owner_amended_and_applied"]
    refused = [r for r in log_window if r.get("event") == "refused"]
    pending_owner = [r for r in log_window if r.get("event") == "owner_gated"]

    # Pending requests still in owner_inbox
    still_pending = []
    try:
        if os.path.isdir(OWNER_INBOX_SKILLS_DIR):
            for fn in os.listdir(OWNER_INBOX_SKILLS_DIR):
                if fn.endswith(".md"):
                    still_pending.append(fn)
    except Exception:
        pass

    # skill_requests.json queue is RETIRED (dead); _se_load_pending_requests returns []
    queued = _se_load_pending_requests()

    # Stale sub-skills
    stale = _se_count_stale_subskills(catalog)

    # Meeting watcher capability gaps in last 3 days
    meeting_gaps = _se_read_recent_code_requests_from_meeting_watchers(window_hours)

    # Doctrine drift
    doctrine_map_path = CROSS_SKILL_DOCTRINE_MAP
    drift_candidates = []
    try:
        if os.path.isfile(doctrine_map_path):
            with open(doctrine_map_path, "r", encoding="utf-8") as f:
                dm = json.load(f)
            drift_candidates = dm.get("extraction_candidates", [])
    except Exception:
        pass

    # Build markdown
    lines = [
        "# 3-Day Skill Evolution Review\n",
        "\n> **Handler:** load `skills/skill-evolution/SKILL.md` and follow the "
        "**\"Processing the 3-Day Review Packet\"** section. This packet is the "
        "data input; the skill is the protocol.\n",
        f"\n**Window:** last 72h\n",
        f"**Generated by:** warden Layer 10 (skill_evolution_monitor)\n",
        f"**Cadence:** every 3 days per skill-evolution doctrine\n",
        f"**Catalog state:** {len(catalog)} top-level skills tracked\n",
        "\n---\n",
        "\n## What evolved\n",
        f"- Auto-applied: **{len(auto_applied)}** LOW-risk requests applied without the owner gate\n",
        f"- The owner-approved: **{len(owner_approved)}** MEDIUM/HIGH requests approved + committed\n",
        f"- The owner-amended: **{len(owner_amended)}** requests where the owner edited then approved\n",
        f"- The owner-rejected: **{len(owner_rejected)}** requests the owner declined\n",
        f"- REFUSE-tier: **{len(refused)}** attempts on HARD-LOCKED files (identity, rules, etc.)\n",
        f"- Still pending the owner: **{len(still_pending)}** packets in `owner_inbox/pending_skill_requests/`\n",
        f"- skill_requests.json queue: DEAD/retired (amendments go agent_direct)\n",
    ]

    if auto_applied:
        lines.append("\n### Recent auto-applied (last 10)\n")
        for r in auto_applied[-10:]:
            op = r.get("operation", "?")
            target = r.get("target", "?")
            req_id = r.get("req_id", "?")
            lines.append(f"- `{req_id}` — {op} on **{target}**\n")

    if still_pending:
        lines.append("\n### Pending your review (owner_inbox)\n")
        for fn in still_pending[:15]:
            lines.append(f"- `{fn}`\n")
        if len(still_pending) > 15:
            lines.append(f"- ... and {len(still_pending) - 15} more\n")

    lines.append("\n## What got refused (HARD LAW attempts)\n")
    if refused:
        for r in refused[-5:]:
            lines.append(f"- `{r.get('req_id', '?')}` — {r.get('reason', '?')}\n")
        lines.append(
            "\nRepeated REFUSE attempts on the same file may indicate that agent "
            "is trying to amend something through the wrong channel. Verify the "
            "right home (rules.md edits go via the owner direct, identity changes via "
            "The owner direct, Tier 0 skills via skill-writer).\n"
        )
    else:
        lines.append("- (none in window — no HARD-LOCKED attempts)\n")

    lines.append("\n## Meeting-watcher capability gaps\n")
    if meeting_gaps:
        lines.append(
            f"Bot meeting watchers detected **{len(meeting_gaps)}** capability "
            f"gaps in the last 72h (directives that needed bot code changes, "
            f"not just skill amendments):\n"
        )
        for g in meeting_gaps[-10:]:
            lines.append(
                f"- `{g.get('id', '?')}` — {g.get('requester', '?')} → "
                f"param `{g.get('proposed_param_name', '?')}` "
                f"({g.get('gap_classification', '?')})\n"
            )
        lines.append(
            "\nThese landed in `code_requests.json` for code_tuner. Check whether "
            "any have stalled (pending >48h without resolution).\n"
        )
    else:
        lines.append("- (no capability gaps detected in window)\n")

    lines.append("\n## Stale sub-skills (expires_at has passed)\n")
    total_stale = sum(len(v) for v in stale.values())
    if total_stale > 0:
        lines.append(f"Found **{total_stale}** sub-skills past their expires_at:\n")
        if stale["tier_1"]:
            lines.append(f"\n**Tier 1 (always-applies, surprising to have expires_at):**\n")
            for p in stale["tier_1"]:
                lines.append(f"- `{p}`\n")
        if stale["tier_2"]:
            lines.append(f"\n**Tier 2 (regime-conditional):**\n")
            for p in stale["tier_2"]:
                lines.append(f"- `{p}`\n")
        if stale["tier_3"]:
            lines.append(f"\n**Tier 3 (edge case):**\n")
            for p in stale["tier_3"]:
                lines.append(f"- `{p}`\n")
        if stale["pinned_exempt"]:
            lines.append(
                f"\n**Pinned skills with expired expires_at (anomaly — pinned exempt from archive):**\n"
            )
            for p in stale["pinned_exempt"]:
                lines.append(f"- `{p}`\n")
        lines.append(
            "\nSuggested action: archive stale Tier 2/3 entries via agent_direct (the "
            "skill_requests.json queue is retired); Layer 10 moves them to `_archived/` "
            "(recoverable via `restore_sub_skill`).\n"
        )
    else:
        lines.append("- (no expired sub-skills)\n")

    lines.append("\n## Cross-skill doctrine drift\n")
    if drift_candidates:
        lines.append(f"Layer 10 flagged **{len(drift_candidates)}** doctrines duplicated inline across skills:\n")
        for c in drift_candidates:
            lines.append(
                f"- **`{c.get('doctrine_marker', '?')}`** appears in: "
                f"{', '.join(c.get('appearing_in', []))}\n"
            )
        lines.append(
            "\nConsider firing `link_doctrine` operations to extract these to "
            "`skills/_doctrine/` with `inherits_doctrine: [...]` references in "
            "each affected skill. One canonical home per doctrine.\n"
        )
    else:
        lines.append("- (no cross-skill drift detected)\n")

    lines.append("\n---\n\n## How to process this packet\n\n")
    lines.append(
        "**Load `skills/skill-evolution/SKILL.md` and follow the \"Processing the 3-Day "
        "Review Packet\" section.** That skill is the canonical home for the 3-day "
        "review protocol — reflection prompts, decision tree, what to do per category, "
        "when to no-op vs when to amend via agent_direct, escalation rules.\n\n"
        "This packet is the DATA. The skill is the PROTOCOL. Every fact has one home.\n\n"
        "If you find yourself wanting to act on this packet without loading "
        "skill-evolution first, that's a doctrine violation — the prompts and decision "
        "rules belong in the skill, not in your working memory from last cycle. Load "
        "fresh every time.\n\n"
        "Next 3-day packet fires 72h from now.\n"
    )

    return "".join(lines)


async def owner_approval_watcher():
    """DISABLED no-op. Does not read APPROVED/AMEND/REJECTED flags and
    cannot copy a bot into bots/ or rewrite a skill.
    """
    print("[warden] owner approval watcher DISABLED (no-op; flags ignored)")
    while True:
        await asyncio.sleep(3600)

async def inbox_cron_lock_reaper():
    """Sprint 65 — reaps a stale inbox_cron.lock so the owner doesn't have to remove it by
    hand. The inbox cron writes bot_logs/inbox_cron.lock at pass start and deletes it at
    pass end; if a pass dies before that delete (crash, timeout, context cut), the lock
    goes stale and blocks every future pass. We delete a lock older than
    INBOX_CRON_LOCK_MAX_AGE_S (15 min — above the cron's own 12-min guard, so a pass
    that's legitimately still running is never killed).

    Age is taken from the file MTIME, not its contents — we don't parse or trust the
    lock body, just how long it's sat there. Never touches a fresh lock."""
    print(f"[warden] inbox_cron lock reaper active (reaps locks > {INBOX_CRON_LOCK_MAX_AGE_S//60}min)")
    while True:
        try:
            if os.path.exists(INBOX_CRON_LOCK_FILE):
                age = time.time() - os.path.getmtime(INBOX_CRON_LOCK_FILE)
                if age > INBOX_CRON_LOCK_MAX_AGE_S:
                    try:
                        os.remove(INBOX_CRON_LOCK_FILE)
                        msg = (f"reaped stale inbox_cron.lock (age {int(age/60)}min > "
                               f"{INBOX_CRON_LOCK_MAX_AGE_S//60}min) — a prior inbox pass died "
                               "before releasing it; next cron fire can now run")
                        print(f"[warden] {msg}")
                        log_finding("inbox_cron", "info", msg,
                                    remediation="stale lock removed automatically")
                        await notify(f"🧹 warden: {msg}")
                    except FileNotFoundError:
                        pass  # cron released it between our check and remove — fine
                    except Exception as e:
                        print(f"[warden] lock reaper remove err: {e}")
        except Exception as e:
            print(f"[warden] lock reaper err: {e}")
        await asyncio.sleep(INBOX_CRON_LOCK_CHECK_S)


# ============================================================================
#   LIVE-UPDATE (element e, simple-bot clean core)
# ============================================================================
# Poll THIS file's mtime; on an on-disk change, re-exec this warden only (os.execv,
# same PID + terminal) so the patch goes live without a manual restart and warden
# stays in the terminal the owner launched it from. No browser/external process to tear
# down; Ctrl-C works normally. Floor: only reload a change already on disk (The owner
# The agent patches in place); never fetch, self-modify, or reload any other bot.
import sys as _lu_sys
import shutil as _lu_shutil
import hashlib as _lu_hashlib

_LU_BOT      = "security_warden"
_LU_SELF     = os.path.abspath(__file__)
_LU_MARKER   = os.path.join(BOT_LOGS, f"{_LU_BOT}_reload.json")
_LU_LASTGOOD = _LU_SELF + ".last-good"
_LU_POLL_S   = 30


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
        os.makedirs(INBOX, exist_ok=True)
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(INBOX, f"{_LU_BOT} - {kind}_{ts}.md")
        fm = {"kind": kind, "severity": severity, "dedupe_key": f"{_LU_BOT}:{kind}",
              "source_skill": _LU_BOT, "summary": summary}
        lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", "",
                 f"# {_LU_BOT} {kind}", summary]
        if extra:
            lines += ["", "```json", json.dumps(extra, indent=2, default=str), "```"]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"[warden] live-update report failed: {e}")


def _lu_on_boot():
    marker = load_json(_LU_MARKER, None)
    if isinstance(marker, dict):
        _lu_write_report("bot_restart", "info",
                         f"{_LU_BOT} reloaded {marker.get('from_hash')} -> {marker.get('to_hash')} (live-update, healthy)",
                         marker)
        try:
            os.remove(_LU_MARKER)
        except Exception:
            pass
    _lu_snapshot_last_good()


def _lu_reload_now():
    new_hash = _lu_hash(_LU_SELF)
    _lu_snapshot_last_good()
    save_json(_LU_MARKER, {"from_hash": _LU_BOOT_HASH, "to_hash": new_hash,
                           "detected_at": datetime.now(EST).isoformat(), "lastgood": _LU_LASTGOOD})
    print(f"[warden] live-update: reloading {_LU_BOOT_HASH} -> {new_hash} (re-exec in place)")
    try:
        os.execv(_lu_sys.executable, [_lu_sys.executable, _LU_SELF] + _lu_sys.argv[1:])
    except Exception as e:
        print(f"[warden] live-update: os.execv failed, staying on current code: {e}")
        _lu_write_report("bot_restart_failed", "high",
                         f"{_LU_BOT} patch {new_hash} detected but re-exec failed; STILL RUNNING old {_LU_BOOT_HASH}",
                         {"error": str(e)[:200]})
        try:
            os.remove(_LU_MARKER)
        except Exception:
            pass


async def live_update_watcher_loop():
    print(f"[warden] live_update_watcher: online (poll {_LU_POLL_S}s, boot {_LU_BOOT_HASH})")
    while True:
        try:
            if _lu_mtime(_LU_SELF) > _LU_BOOT_MTIME and _lu_hash(_LU_SELF) != _LU_BOOT_HASH:
                _lu_reload_now()  # os.execv does not return on success
        except Exception as e:
            print(f"[warden] live_update_watcher err: {e}")
        await asyncio.sleep(_LU_POLL_S)


# 2026-10-16: ENGINE WATCH. Fast check (60s) on the perps engine heartbeat
# and on relaunch failures. Telegram to the owner + an agent inbox note so the
# agent alerts too. Alert-only: never restarts anything.
PERPS_ENGINE_STALE_MIN = float(_envv("PERPS_ENGINE_STALE_MIN", "5"))
PERPS_ENGINE_ALERT_COOLDOWN_MIN = float(_envv("PERPS_ENGINE_ALERT_COOLDOWN_MIN", "30"))


async def perps_engine_watch():
    hb = os.path.join(BOT_LOGS, "perps_bot_heartbeat.json")
    t_start = time.time()
    last_alert = 0.0
    seen = set()
    try:
        seen = {f for f in os.listdir(INBOX) if "bot_restart_failed" in f or "relaunch_failed" in f}
    except Exception:
        pass
    while True:
        try:
            # 1) relaunch / patch failures reported by the engine or the detached checker
            for f in sorted(os.listdir(INBOX)):
                if f in seen or not ("bot_restart_failed" in f or "relaunch_failed" in f):
                    continue
                seen.add(f)
                if f.startswith("perps_heartbeat_watch"):
                    continue  # that checker already sent its own Telegram
                await notify("\U0001F6A8 <b>PERPS RELAUNCH FAILED</b>\n"
                             f"{f}\nThe engine is still on its old code or down. Check the Perps window.")
            # 2) engine heartbeat staleness (grace after warden boot)
            if time.time() - t_start > 180:
                try:
                    age = (time.time() - os.path.getmtime(hb)) / 60.0
                except Exception:
                    age = None
                if (age is None or age > PERPS_ENGINE_STALE_MIN) and \
                        time.time() - last_alert > PERPS_ENGINE_ALERT_COOLDOWN_MIN * 60:
                    last_alert = time.time()
                    age_s = "missing" if age is None else f"{age:.0f} min old"
                    msg = (f"\U0001F6A8 <b>PERPS ENGINE HEARTBEAT STALE</b>\nHeartbeat {age_s} "
                           f"(limit {PERPS_ENGINE_STALE_MIN:g} min). TP/SL/floor monitoring may be down.")
                    await notify(msg)
                    write_inbox_packet("P1", "warden_engine", "perps_engine_stale",
                                       msg.replace("<b>", "").replace("</b>", "")
                                       + "\n\nAlert the owner. Do not restart the bot yourself.")
        except Exception as e:
            print(f"[warden] engine watch err: {e!r}")
        await asyncio.sleep(60)


async def main():
    print()
    print("=" * 60)
    print("[warden] Security Warden - Perps ecosystem guard")
    print("=" * 60)
    print(f"[warden] SI order:     {', '.join(_si_gate.order()) or 'off (LLM_MAIN/LLM_FALLBACK)'}")
    print(f"[warden] config:       {CONFIG_FILE}")
    print(f"[warden] quarantine:   {QUARANTINE_DIR}")
    print(f"[warden] kill switch:  {KILL_SWITCH_FILE} (MANUAL ONLY)")
    print(f"[warden] lockdown:     {LOCKDOWN_FILE}")
    print(f"[warden] medic:        staging={MEDIC_STAGING}, backups={MEDIC_BACKUPS}")
    print(f"[warden] integrity:    baseline={HASH_BASELINE}")
    print(f"[warden] inbox:        {INBOX}")
    print(f"[warden] approval watcher: DISABLED (flags do not copy into bots)")
    print("[warden] mode:         OBSERVE + medic report/backup (no auto-repair)")
    print("=" * 60)

    cfg = load_config()
    print(f"[warden] bounds: perp=${(cfg.get('bounds') or {}).get('perps', {}).get('max_size_usd', 200)} (alert above 3x)")

    # Run boot sequence
    boot_ok = await boot_sequence()
    if not boot_ok:
        print("[warden] boot failed — halting")
        return

    _lu_on_boot()  # live-update: report a completed reload + snapshot last-good

    # Start all layers concurrently
    await asyncio.gather(
        # live-update watcher (element e, simple-bot core)
        live_update_watcher_loop(),
        # 2026-10-16: fast perps engine heartbeat + relaunch-failure alerts
        perps_engine_watch(),
        # Layer 1
        ingress_guardian(),
        # Layer 2
        egress_validator(),
        # Layer 3
        canary_monitor(),
        wallet_anomaly_watcher(),
        dead_mans_switch(),
        api_health_monitor(),
        # v3.3 NEW: Layer 3d-bis (heartbeat-based liveness) + 3e (silent failure)
        bot_heartbeat_monitor(),
        silent_failure_monitor(),
        exec_failure_rate_monitor(),
        # Layer 5
        feed_integrity_monitor(),
        # Layer 6
        medic_process_monitor(),
        # v3.3 NEW: Layer 6b (doctor)
        doctor_monitor(),
        # Layer 6c (was 6b)
        bot_builder_inbox_loop(),
        # v3.3 Phase 4 NEW: Layer 6d (owner approval watcher for new bots)
        owner_approval_watcher(),
        # Layer 7
        integrity_monitor(),
        # Layer 8
        daily_inbox_dispatcher(),
        # Sprint 41 NEW: Layer 10 (skill evolution + skill integrity + cross-skill doctrine)
        skill_evolution_monitor(),
        # Layer 11: The agent's wake-up brief , rewrites heartbeat.md every 5min
        # Sprint 65: reap stale inbox_cron.lock so the owner doesn't remove it by hand
        inbox_cron_lock_reaper(),
        # Warden is outbound-only: no Telegram polling.
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[warden] shutdown via KeyboardInterrupt")
