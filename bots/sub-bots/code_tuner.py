"""
code_tuner.py - Perps ecosystem performance monitor + source tracker + setup orchestrator
================================================================================
Observes only the Perps ecosystem: perps.py (perps_bot) and bots/sub-bots.

Sprint 42 base (v4.0/v4.1):
  ALL LLM-based patch generation has been removed. This bot no longer writes
  code, no longer calls an SI for code, no longer auto-deploys anything.

NEW ROLE (per Sprint 42 spec v2):
  - Tracks every bot's source code via full-file snapshots (30-min cadence)
  - Maintains the canonical bots_list.json registry (dual-ownership with warden)
  - AST-scans bots for dependency graph (no manual list maintenance)
  - Detects source changes → writes source delta packets to the agent
  - Runs setup mode to let the agent curate per-bot monitoring config
  - Fires 2-day performance packets (raw stats, no narration)
  - Fires 30-day deep audit packets (longer window aggregates)
  - meeting_watcher (local Ollama, only if LLM_MAIN/LLM_FALLBACK list ollama) interprets the owner's casual directives
  - Code-area search lets the agent request specific source sections

NEW DOCTRINE:
  - All code patches are the agent-authored. The agent applies his own patch.
    code_tuner observes only. Floor: never self-modify code_tuner.py; only
    reload a change already on disk. Do not edit other bots' .py. Do not auto-deploy.
  - Observations, not prescriptions: code_tuner surfaces raw data, the agent reasons.
  - Setup mode: The agent configures monitoring one bot at a time, persistent across boots
  - Dependency graph auto-discovered via AST scan (no manual list)
  - Source snapshots are full file copies (cheap, ~4-5MB total)

STAGE PROGRESSION:
  Stage 1 (THIS FILE): core refactor + bots_list.json + AST deps + source snapshots
  Stage 2 (next): setup mode (discovery packets, response handler, metric catalog)
  Stage 3 (after): output collectors + metric aggregator + 2-day perf packets +
                   metrics_current.json + 30-day deep audit + source delta packets
  Stage 4 (after): meeting_watcher (local Ollama) + code-area search
  Stage 5 (last): skill rewrite (tuner-monitor → code-monitor v3.0.0)

FILES OWNED:
  Reads (canonical):
    - bots/*.py                                  (all bot sources)
    - skills/*/SKILL.md                          (all skill files)
    - bots_list.json                             (the registry, dual-owned)

  Writes (canonical):
    - bot_logs/source_snapshots/<bot>.py.snapshot          (full file copy)
    - bot_logs/source_snapshots/<bot>.py.snapshot.previous (one rotation back)
    - bot_logs/source_snapshots/<skill>_SKILL.md.snapshot  (paired skill copies)
    - bot_logs/code_tuner_heartbeat.json                   (warden Layer 4 input)
    - bot_logs/code_tuner.log                              (operations log)
    - bots/json_relay/code_tuner_state.json                (internal cycle state)

  Coordinates with warden:
    - warden owns `_managed_by_warden` writes to bots_list.json
    - code_tuner reads bots_list.json but does NOT mutate it directly
    - For Stage 1, if warden hasn't seeded bots_list.json yet, code_tuner
      writes an initial template with `_managed_by_warden` populated and
      `_curated_by_agent: {}` for every bot, then waits for warden to take over.

  Inboxes:
    - inbox/                  the agent's inbox (we write P3 packets here)
    - owner_inbox/             not a landing zone for code-request or config_drift packets
    - bots/json_relay/code_tuner_setup.json     the agent writes setup responses
    - bots/json_relay/code_tuner_meeting.json   the owner writes casual directives

WHAT GOT DELETED FROM v3.3:
  - tune_bot() and all LLM patch generation
  - SI-based code generation
  - syntax_check, risk_check, diff_summary for new code
  - deploy_fix() and rollback() — no auto-deploy
  - HIGH_RISK_KEYWORDS, NEVER_TOUCH_PATTERNS (no LLM = no risk gates)
  - tuner_scheduler() 9am/9pm cron
  - run_tuning_session()
  - bot_watchdog (warden Layer 4 already does this)
  - SKILL_FILES sub-agent routing (sub-agents dead per Sprint 29.5)
  - monthly_upgrade_ping (replaced by 30-day deep audit, Stage 3)

WHAT GOT KEPT (refactored where needed):
  - load_json / save_json / log_tune / notify helpers
  - get_market_mode reader
  - read_* performance data readers (will become Stage 3 collectors)
  - read_code_requests / mark_request_resolved (now: return unfinished work to inbox, don't fulfill)
  - urgent_request_processor name (body changes — Stage 3)
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
import ast
import time
import asyncio
import hashlib
import shutil
import subprocess
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, Any

import httpx
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════════════════════
#                            CONFIG + PATHS
# ════════════════════════════════════════════════════════════════════════════

EST = ZoneInfo("America/New_York")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Workspace paths (relative to the repo root)
BOTS_DIR        = WS_BOTS
BOT_LOGS        = WS_BOT_LOGS
JSON_RELAY      = WS_JSON_RELAY
WORKSPACE       = WS_ROOT
INBOX           = os.path.join(WS_ROOT, "inbox")
OWNER_INBOX      = os.path.join(WS_ROOT, "owner_inbox")
SKILLS_DIR      = os.path.join(WS_ROOT, "skills")
SKILLS_METADATA = os.path.join(WS_ROOT, "skills", "skills_metadata.json")

# Canonical registry
BOTS_LIST_FILE = os.path.join(BOT_LOGS, "bots_list.json")

# Source snapshot tree
SOURCE_SNAPSHOTS_DIR = os.path.join(BOT_LOGS, "source_snapshots")

# v4.2 (Sprint 44) — Directory bot entry-point registry. Maps bot_name → entry
# point file (relative to bots/<bot_name>/). For single-file bots, the path is
# resolved as bots/<bot_name>.py; for directory bots in this map, it's resolved
# as bots/<bot_name>/<entry_point>. The entry point is what gets snapshotted,
# BOT_OUTPUTS-scanned, and AST-walked for the dependency graph.
#
# Add an entry here whenever a new directory bot ships. Single-file bots need
# no entry — the default resolution is bots/<bot_name>.py, then bots/sub-bots/<bot_name>.py;
# perps_bot resolves to bots/perps.py.
_DIRECTORY_BOT_ENTRYPOINTS: dict[str, str] = {
}

# Internal state + logs
HB_FILE         = os.path.join(BOT_LOGS, "code_tuner_heartbeat.json")
TUNE_LOG        = os.path.join(BOT_LOGS, "code_tuner.log")
STATE_FILE      = os.path.join(JSON_RELAY, "code_tuner_state.json")
MARKET_MODE_RELAY = os.path.join(JSON_RELAY, "market_mode.json")

# Code requests (dormant fallback — stays for context-overflow path)
CODE_REQUESTS_FILE = os.path.join(BOT_LOGS, "code_requests.json")

# Setup mode response file (The agent writes here during setup)
SETUP_RESPONSE_FILE = os.path.join(JSON_RELAY, "code_tuner_setup.json")

# meeting_watcher file (The owner writes casual directives here — Stage 4)
MEETING_FILE = os.path.join(JSON_RELAY, "code_tuner_meeting.json")

# Warden config file — the authoritative bot list lives in
# warden's `bot_heartbeat_thresholds_minutes` field. code_tuner reads
# the same source of truth warden does instead of scanning bots/ directory.
WARDEN_CONFIG_FILE = os.path.join(BOT_LOGS, "warden_config.json")

# Warden's heartbeat filename alias map (some bots have non-standard names).
# Kept in sync with security_warden.py HEARTBEAT_FILE_ALIASES_WARDEN.
HEARTBEAT_FILE_ALIASES = {
}

# Cadences (seconds)
SNAPSHOT_INTERVAL_S = int(_envv("CODE_TUNER_SNAPSHOT_INTERVAL_S", "1800"))  # 30 min
REGISTRY_REFRESH_S  = int(_envv("CODE_TUNER_REGISTRY_REFRESH_S", "300"))    # 5 min
HEARTBEAT_INTERVAL_S = int(_envv("CODE_TUNER_HEARTBEAT_INTERVAL_S", "60"))

# Bot detection heuristics
# A .py file in bots/ is treated as a bot if ANY of these are true:
#   - It has a known heartbeat file in bot_logs/<name>_heartbeat.json
#   - It imports _self_description (Sprint 41 boot hook)
#   - It is listed in BOT_NEVER_TOUCH (excluded explicitly below)
#   - It is in BOTS_DIR (top-level, not in subdirectory)
# We exclude utility modules (no main loop, no heartbeat, just helpers)
BOT_NEVER_TOUCH = [
    "position_decider.py",  # retired
    "agent_brain.py",      # retired
    "auto_compounder.py",   # retired
    "reminders.py",         # retired
]

# Files in BOTS_DIR that are NOT bots (utility modules used by other bots)
KNOWN_UTILITY_MODULES = [
    "_self_description.py",      # Sprint 41 boot hook (imported library)
    "_si_gate.py",               # LLM_MAIN / LLM_FALLBACK gate (imported library)
    "agent_ack.py",              # ack helper (imported library)
    "feed_hook.py",              # Flow reader (imported by perps.py)
]

# ════════════════════════════════════════════════════════════════════════════
#                            UTILITY HELPERS (kept from v3.3)
# ════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(EST).isoformat()

def _short_ts() -> str:
    return datetime.now(EST).strftime("%Y%m%d_%H%M%S")

def load_json(path, default):
    """Read JSON, return default on any error."""
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path, data):
    """Atomic write."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)

def log_tune(msg: str):
    """Log to file + stdout."""
    ts = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")
    line = f"{ts} | {msg}\n"
    print(f"[tuner] {msg}", flush=True)
    try:
        os.makedirs(BOT_LOGS, exist_ok=True)
        with open(TUNE_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

async def notify(msg: str):
    """Telegram ping. No-op if Telegram not configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
            )
    except Exception as e:
        log_tune(f"notify error: {e}")

def get_market_mode() -> str:
    """Read current market mode (regular | hot) from relay."""
    try:
        if os.path.exists(MARKET_MODE_RELAY):
            relay = load_json(MARKET_MODE_RELAY, {})
            age_mins = (time.time() - relay.get("set_at", 0)) / 60
            expires = relay.get("expires_mins", 120)
            if age_mins < expires:
                return relay.get("mode", "regular")
    except Exception:
        pass
    return "regular"

def write_heartbeat(stage: str = "idle", extra: dict = None):
    """Heartbeat for warden Layer 4."""
    payload = {
        "ts": _now_iso(),
        "stage": stage,
        "status": "ok",
        "version": "v4.2",
    }
    if extra:
        payload.update(extra)
    try:
        save_json(HB_FILE, payload)
    except Exception as e:
        log_tune(f"heartbeat err: {e}")

# ════════════════════════════════════════════════════════════════════════════
#                            BOT DISCOVERY (warden config = source of truth)
# ════════════════════════════════════════════════════════════════════════════
# Warden owns the authoritative bot list via its `bot_heartbeat_thresholds_minutes`
# config field. code_tuner reads from there instead of scanning bots/ directory.
#
# Why this works:
#   - The owner edits warden's threshold list when adding/removing bots → one place
#   - Bots without heartbeat files are still tracked (warden treats them the
#     same way — listed but flagged stale)
#   - No dual-source-of-truth race conditions between code_tuner and warden
#
# Fallback chain:
#   1. Try reading warden's runtime config from bot_logs/warden_config.json
#   2. If warden config not available, fall back to parsing security_warden.py
#      directly for the threshold dict (less ideal but works on cold boot)
#   3. If both fail, return empty list and log a warning (code_tuner will
#      keep retrying every cycle until warden config appears)

def _resolve_bot_heartbeat_path(bot_name: str) -> str:
    """Mirror of security_warden._resolve_bot_heartbeat_path()."""
    aliased = HEARTBEAT_FILE_ALIASES.get(bot_name)
    if aliased:
        return os.path.join(BOT_LOGS, aliased)
    return os.path.join(BOT_LOGS, f"{bot_name}_heartbeat.json")

def _parse_warden_threshold_from_source() -> dict:
    """
    Fallback: parse security_warden.py source directly to extract the
    bot_heartbeat_thresholds_minutes dict. Used when warden's runtime
    config file isn't available yet (e.g., warden hasn't started).

    Returns dict of {bot_name: threshold_minutes} or empty dict on failure.
    """
    warden_path = os.path.join(BOTS_DIR, "security_warden.py")
    if not os.path.exists(warden_path):
        warden_path = os.path.join(BOTS_DIR, "sub-bots", "security_warden.py")
    if not os.path.exists(warden_path):
        return {}
    try:
        with open(warden_path, "r", encoding="utf-8-sig") as f:
            source = f.read()

        # AST-walk for the threshold dict — find the assignment pattern
        # `"bot_heartbeat_thresholds_minutes": { ... }` inside a dict literal.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key_node, val_node in zip(node.keys, node.values):
                if (isinstance(key_node, ast.Constant)
                    and key_node.value == "bot_heartbeat_thresholds_minutes"
                    and isinstance(val_node, ast.Dict)):
                    result = {}
                    for k, v in zip(val_node.keys, val_node.values):
                        if (isinstance(k, ast.Constant)
                            and isinstance(v, ast.Constant)
                            and isinstance(k.value, str)
                            and isinstance(v.value, (int, float))):
                            result[k.value] = int(v.value)
                    if result:
                        return result
        return {}
    except Exception as e:
        log_tune(f"warden source parse err: {e}")
        return {}

def _read_warden_thresholds() -> dict:
    """
    Get the authoritative bot list from warden's threshold config.

    Returns dict of {bot_name: threshold_minutes}.

    Tries warden's live config file first, falls back to source parse.
    """
    # Try 1: live config (if warden has written it out somewhere code_tuner can read)
    cfg = load_json(WARDEN_CONFIG_FILE, default=None)
    if isinstance(cfg, dict):
        thresholds = cfg.get("bot_heartbeat_thresholds_minutes")
        if isinstance(thresholds, dict) and thresholds:
            return thresholds

    # Try 2: parse warden's source
    thresholds = _parse_warden_threshold_from_source()
    if thresholds:
        return thresholds

    log_tune("warden thresholds unavailable (live config + source parse failed)")
    return {}

def discover_bots() -> dict[str, dict]:
    """
    Discover bots from warden's threshold config (source of truth).

    v4.2 (Sprint 44): handles directory bots via _DIRECTORY_BOT_ENTRYPOINTS.
    For single-file bots, path = bots/<bot_name>.py.
    For directory bots (entry in the map), path = bots/<bot_name>/<entrypoint>.

    Returns dict mapping bot_short_name (no .py) → {
        "filename": "<name>.py" or "<entrypoint>.py",
        "path": "C:\\...\\<name>.py" or "C:\\...\\<name>\\<entrypoint>.py",
        "heartbeat_path": "<resolved path>",
        "heartbeat_exists": True | False,
        "threshold_minutes": int,
        "source": "warden_config",
        "is_directory_bot": bool,        # v4.2 NEW
        "bot_dir": str | None,            # v4.2 NEW (only set for directory bots)
    }
    """
    bots = {}
    thresholds = _read_warden_thresholds()
    if not thresholds:
        log_tune("discover_bots: warden thresholds empty — no bots discovered")
        return bots

    for bot_name, threshold_min in thresholds.items():
        # v4.2: directory bot path resolution
        if bot_name in _DIRECTORY_BOT_ENTRYPOINTS:
            entrypoint = _DIRECTORY_BOT_ENTRYPOINTS[bot_name]
            bot_dir = os.path.join(BOTS_DIR, bot_name)
            bot_path = os.path.join(bot_dir, entrypoint)
            filename = entrypoint
            is_dir_bot = True
        else:
            filename = "perps.py" if bot_name == "perps_bot" else f"{bot_name}.py"
            bot_path = os.path.join(BOTS_DIR, filename)
            if not os.path.exists(bot_path) and os.path.exists(os.path.join(BOTS_DIR, "sub-bots", filename)):
                bot_path = os.path.join(BOTS_DIR, "sub-bots", filename)
            bot_dir = None
            is_dir_bot = False

        # If the entry-point file doesn't exist at the expected path, the bot
        # is broken/missing — still record it so code_tuner can flag it
        path_exists = os.path.exists(bot_path)
        hb_path = _resolve_bot_heartbeat_path(bot_name)
        bots[bot_name] = {
            "filename": filename,
            "path": bot_path,
            "path_exists": path_exists,
            "heartbeat_path": hb_path,
            "heartbeat_exists": os.path.exists(hb_path),
            "threshold_minutes": threshold_min,
            "source": "warden_config",
            "is_directory_bot": is_dir_bot,
            "bot_dir": bot_dir,
        }
    return bots

# ════════════════════════════════════════════════════════════════════════════
#                            DEPENDENCY SCANNER (AST-based)
# ════════════════════════════════════════════════════════════════════════════

def _build_system_module_set() -> set[str]:
    """
    Build the set of repo-owned Python module names (every .py file in BOTS_DIR
    + entry points of known directory bots).

    Includes directory bots. For
    a directory bot (bots/<name>/<entry>.py), both '<name>' (the dir
    name, treated as module-ish) and '<entry>' (the actual entry file)
    are added so AST import detection works in both directions.

    Used to filter imports — third-party libs like httpx/pandas don't count
    as system dependencies.
    """
    modules = set()
    if not os.path.isdir(BOTS_DIR):
        return modules
    for entry in os.scandir(BOTS_DIR):
        if entry.is_file() and entry.name.endswith(".py"):
            module_name = entry.name[:-3]
            modules.add(module_name)
        elif entry.is_dir() and entry.name in _DIRECTORY_BOT_ENTRYPOINTS:
            # Directory bot: add the dir name itself + the entry point stem
            modules.add(entry.name)
            entrypoint = _DIRECTORY_BOT_ENTRYPOINTS[entry.name]
            if entrypoint.endswith(".py"):
                modules.add(entrypoint[:-3])
    return modules

def scan_dependencies(bot_filepath: str) -> tuple[list[str], dict]:
    """
    AST-scan a bot's source to find imports of repo-owned modules.

    Returns:
        (deps_list, metadata)
        deps_list: sorted list of system module names this bot imports
        metadata:  {scan_method, scanned_at, errors}
    """
    metadata = {
        "scan_method": "ast_imports",
        "scanned_at": _now_iso(),
        "errors": [],
    }

    try:
        with open(bot_filepath, "r", encoding="utf-8-sig") as f:
            source = f.read()
    except Exception as e:
        metadata["errors"].append(f"read err: {e}")
        return [], metadata

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        metadata["errors"].append(f"parse err: {e}")
        return [], metadata

    system_modules = _build_system_module_set()
    found_modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import foo.bar` → top-level module is 'foo'
                top = alias.name.split(".")[0]
                if top in system_modules:
                    found_modules.add(top)
        elif isinstance(node, ast.ImportFrom):
            # `from foo import bar` → 'foo' is the module
            if node.module:
                top = node.module.split(".")[0]
                if top in system_modules:
                    found_modules.add(top)

    return sorted(found_modules), metadata

# ════════════════════════════════════════════════════════════════════════════
#                            SOURCE SNAPSHOT MANAGER
# ════════════════════════════════════════════════════════════════════════════

def _file_hash(filepath: str) -> str:
    """SHA256 of file contents. Empty string on error."""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        log_tune(f"hash err on {filepath}: {e}")
        return ""

def _snapshot_path_for_bot(bot_name: str) -> str:
    return os.path.join(SOURCE_SNAPSHOTS_DIR, f"{bot_name}.py.snapshot")

def _snapshot_path_for_skill(skill_name: str) -> str:
    safe = skill_name.replace("/", "_").replace("\\", "_")
    return os.path.join(SOURCE_SNAPSHOTS_DIR, f"{safe}_SKILL.md.snapshot")

def _previous_path(snapshot_path: str) -> str:
    return snapshot_path + ".previous"

def take_snapshot(source_path: str, snapshot_path: str) -> dict:
    """
    Copy source_path to snapshot_path. If a current snapshot already exists
    and the hashes differ, rotate the current snapshot to .previous first.

    Returns:
        {
            "changed": bool,         # was the snapshot updated (hash differed)?
            "new_hash": str,
            "old_hash": str | None,
            "snapshot_path": str,
            "lines": int,
            "errors": list[str],
        }
    """
    result = {
        "changed": False,
        "new_hash": "",
        "old_hash": None,
        "snapshot_path": snapshot_path,
        "lines": 0,
        "errors": [],
    }

    if not os.path.exists(source_path):
        result["errors"].append(f"source not found: {source_path}")
        return result

    try:
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    except Exception as e:
        result["errors"].append(f"snapshot dir create err: {e}")
        return result

    new_hash = _file_hash(source_path)
    result["new_hash"] = new_hash

    # Count lines
    try:
        with open(source_path, "r", encoding="utf-8-sig") as f:
            result["lines"] = sum(1 for _ in f)
    except Exception:
        pass

    old_hash = None
    if os.path.exists(snapshot_path):
        old_hash = _file_hash(snapshot_path)
        result["old_hash"] = old_hash

    if old_hash and old_hash == new_hash:
        # No change — nothing to do
        return result

    # Rotate current → .previous (if exists)
    if os.path.exists(snapshot_path):
        prev_path = _previous_path(snapshot_path)
        try:
            shutil.copy2(snapshot_path, prev_path)
        except Exception as e:
            result["errors"].append(f"previous rotation err: {e}")

    # Copy fresh source → snapshot
    try:
        shutil.copy2(source_path, snapshot_path)
        result["changed"] = True
    except Exception as e:
        result["errors"].append(f"snapshot copy err: {e}")

    return result

def snapshot_all_bots(bots: dict[str, dict]) -> dict[str, dict]:
    """
    Take snapshots of every bot. Returns map bot_name → snapshot result.
    Bots whose .py file doesn't exist get an empty result with an error.
    """
    results = {}
    for bot_name, info in bots.items():
        if not info.get("path_exists", True):
            results[bot_name] = {
                "changed": False,
                "new_hash": "",
                "old_hash": None,
                "snapshot_path": "",
                "lines": 0,
                "errors": [f"bot file does not exist: {info.get('path')}"],
            }
            continue
        snap_path = _snapshot_path_for_bot(bot_name)
        results[bot_name] = take_snapshot(info["path"], snap_path)
    return results

def snapshot_skill(skill_name: str, skill_path: str) -> dict:
    """Take snapshot of a skill file."""
    snap_path = _snapshot_path_for_skill(skill_name)
    return take_snapshot(skill_path, snap_path)

# ════════════════════════════════════════════════════════════════════════════
#                            SKILLS METADATA READER
# ════════════════════════════════════════════════════════════════════════════
# We DON'T rebuild the skill catalog. boot-registry + skills_metadata.json
# are the source of truth. We just read paths from there when needed.

def load_skills_metadata() -> dict:
    """Load skills_metadata.json (machine-readable skill catalog from boot-registry)."""
    return load_json(SKILLS_METADATA, default={})

def resolve_skill_path(skill_name: str) -> Optional[str]:
    """
    Given a skill name (e.g., 'perps-boss'), return absolute path to its
    SKILL.md, or None if not found.

    Tries skills_metadata.json first, falls back to convention:
    workspace/skills/<skill_name>/SKILL.md
    """
    metadata = load_skills_metadata()

    # Try structured lookup if metadata has it
    skills = metadata.get("skills", {}) if isinstance(metadata, dict) else {}
    entry = skills.get(skill_name) if isinstance(skills, dict) else None
    if isinstance(entry, dict) and entry.get("path"):
        return entry["path"]

    # Convention fallback
    conv_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    if os.path.exists(conv_path):
        return conv_path

    return None

# ════════════════════════════════════════════════════════════════════════════
#                            BOTS_LIST.JSON MANAGER
# ════════════════════════════════════════════════════════════════════════════
# Schema (per Sprint 42 spec v2):
#   {
#     "version": "2.0",
#     "last_updated": "<iso>",
#     "bots": {
#       "<bot_name>": {
#         "_managed_by_warden": {
#           path, version, source_hash, source_lines, last_modified_ts,
#           heartbeat_file, self_description_file, snapshot_path,
#           last_snapshot_ts, last_seen
#         },
#         "_curated_by_agent": { } | { role, category, primary_skill, ... },
#         "output_files": { } | { ... },
#         "dependencies": [ ... ],
#         "dependency_scan_at": "<iso>",
#         "dependency_scan_method": "ast_imports"
#       }
#     }
#   }
#
# OWNERSHIP RULES:
#   - warden owns `_managed_by_warden` writes
#   - The agent owns `_curated_by_agent` and `output_files` writes
#   - code_tuner reads everything, writes via the seed/refresh path described below
#
# SEEDING:
#   On Sprint 42 first boot, if bots_list.json doesn't exist or has fewer bots
#   than discover_bots() finds, code_tuner seeds it with:
#     - `_managed_by_warden` populated (warden refreshes later anyway)
#     - `_curated_by_agent` = {} (The agent fills during setup mode)
#     - `output_files` = {} (The agent fills during setup mode)
#     - `dependencies` from AST scan
#
# REFRESHING:
#   Every REGISTRY_REFRESH_S, code_tuner re-discovers bots, takes fresh
#   snapshots, re-runs AST scans, and updates `_managed_by_warden` fields
#   IF the bot exists in the registry. For new bots not yet in the registry,
#   they get added with `_curated_by_agent: {}` (triggers setup mode entry).
# ════════════════════════════════════════════════════════════════════════════

def _empty_curated() -> dict:
    """Empty curated half — the agent fills during setup mode."""
    return {}

def _empty_outputs() -> dict:
    return {}

def _build_managed_section(bot_name: str, bot_info: dict,
                           snapshot_result: dict) -> dict:
    """Build the _managed_by_warden section for a bot."""
    hb_path = bot_info.get("heartbeat_path") or _resolve_bot_heartbeat_path(bot_name)
    hb_exists = bot_info.get("heartbeat_exists", os.path.exists(hb_path))
    sd_path = os.path.join(BOT_LOGS, f"{bot_name}_self_description.json")
    path_exists = bot_info.get("path_exists", True)

    # Try to read version from self_description if available
    version = "unknown"
    sd_data = load_json(sd_path, {})
    if isinstance(sd_data, dict):
        version = sd_data.get("version") or sd_data.get("bot_version") or "unknown"

    last_modified_ts = ""
    if path_exists:
        try:
            mtime = os.path.getmtime(bot_info["path"])
            last_modified_ts = datetime.fromtimestamp(mtime, EST).isoformat()
        except Exception:
            pass

    last_seen = ""
    if hb_exists:
        try:
            mtime = os.path.getmtime(hb_path)
            last_seen = datetime.fromtimestamp(mtime, EST).isoformat()
        except Exception:
            pass

    return {
        "path": bot_info["path"],
        "path_exists": path_exists,
        "version": version,
        "source_hash": snapshot_result.get("new_hash", ""),
        "source_lines": snapshot_result.get("lines", 0),
        "last_modified_ts": last_modified_ts,
        "heartbeat_file": hb_path,
        "heartbeat_exists": hb_exists,
        "heartbeat_threshold_minutes": bot_info.get("threshold_minutes"),
        "self_description_file": sd_path if os.path.exists(sd_path) else None,
        "snapshot_path": snapshot_result.get("snapshot_path", ""),
        "last_snapshot_ts": _now_iso() if snapshot_result.get("changed") else None,
        "last_seen": last_seen,
        "warden_managed": True,
        "source_of_truth": "warden.bot_heartbeat_thresholds_minutes",
    }

def seed_or_refresh_registry() -> dict:
    """
    Read bots_list.json, discover bots, take snapshots, AST-scan deps.

    For each discovered bot:
      - If bot not in registry: add entry with empty curated/outputs sections
        (triggers setup mode entry on next setup_invitation scan)
      - If bot in registry: refresh _managed_by_warden + dependencies; leave
        _curated_by_agent and output_files UNTOUCHED (The agent's domain)

    Returns the updated registry dict (also writes to disk).
    """
    registry = load_json(BOTS_LIST_FILE, default={
        "version": "2.0",
        "last_updated": _now_iso(),
        "bots": {},
    })

    if not isinstance(registry, dict) or "bots" not in registry:
        registry = {
            "version": "2.0",
            "last_updated": _now_iso(),
            "bots": {},
        }

    bots = discover_bots()
    snapshot_results = snapshot_all_bots(bots)

    # Build system module set once for AST scans
    for bot_name, bot_info in bots.items():
        snap_result = snapshot_results.get(bot_name, {})

        # AST dependency scan (only if file exists)
        if bot_info.get("path_exists", True):
            deps, dep_meta = scan_dependencies(bot_info["path"])
        else:
            deps = []
            dep_meta = {
                "scan_method": "ast_imports",
                "scanned_at": _now_iso(),
                "errors": [f"bot file does not exist: {bot_info.get('path')}"],
            }

        managed = _build_managed_section(bot_name, bot_info, snap_result)

        existing = registry["bots"].get(bot_name)
        if existing is None:
            # New bot — seed entry
            registry["bots"][bot_name] = {
                "_managed_by_warden": managed,
                "_curated_by_agent": _empty_curated(),
                "output_files": _empty_outputs(),
                "dependencies": deps,
                "dependency_scan_at": dep_meta["scanned_at"],
                "dependency_scan_method": dep_meta["scan_method"],
            }
            log_tune(f"registry: seeded new bot '{bot_name}' (deps: {deps})")
        else:
            # Existing bot — refresh managed + deps only
            existing["_managed_by_warden"] = managed
            existing["dependencies"] = deps
            existing["dependency_scan_at"] = dep_meta["scanned_at"]
            existing["dependency_scan_method"] = dep_meta["scan_method"]
            # _curated_by_agent and output_files: UNTOUCHED

    # Detect bots in registry that are no longer in filesystem (deleted/renamed)
    registry_bots = set(registry["bots"].keys())
    discovered_bots = set(bots.keys())
    orphaned = registry_bots - discovered_bots
    for orphan in orphaned:
        # Don't auto-delete — mark as orphaned, the agent/the owner decides what to do
        entry = registry["bots"][orphan]
        if not entry.get("_orphaned"):
            entry["_orphaned"] = True
            entry["_orphaned_at"] = _now_iso()
            log_tune(f"registry: bot '{orphan}' is orphaned (file not found)")

    registry["last_updated"] = _now_iso()

    try:
        save_json(BOTS_LIST_FILE, registry)
    except Exception as e:
        log_tune(f"registry write err: {e}")

    return registry

def count_unconfigured_bots(registry: dict) -> tuple[int, list[str]]:
    """
    Returns (count, list_of_unconfigured_bot_names).

    A bot is "unconfigured" if _curated_by_agent is empty AND not orphaned.
    """
    unconfigured = []
    bots = registry.get("bots", {})
    for bot_name, entry in bots.items():
        if entry.get("_orphaned"):
            continue
        curated = entry.get("_curated_by_agent", {})
        if not curated:
            unconfigured.append(bot_name)
    return len(unconfigured), unconfigured

# ════════════════════════════════════════════════════════════════════════════
#                            SKILL SNAPSHOT REFRESH
# ════════════════════════════════════════════════════════════════════════════
# For every bot in registry with a primary_skill or secondary_skills set,
# we take snapshots of those skill files too. Source delta detection works
# the same way as bots — change detected on next pass = packet to the agent.

def snapshot_all_associated_skills(registry: dict) -> dict[str, dict]:
    """
    For each bot in registry, look at _curated_by_agent.primary_skill +
    secondary_skills, resolve paths via skills_metadata.json, snapshot each.

    Returns map skill_name → snapshot result.
    """
    skills_seen: set[str] = set()
    results: dict[str, dict] = {}
    bots = registry.get("bots", {})

    for bot_name, entry in bots.items():
        curated = entry.get("_curated_by_agent", {})
        if not curated:
            continue
        primary = curated.get("primary_skill")
        if primary and primary not in skills_seen:
            skills_seen.add(primary)
            path = resolve_skill_path(primary)
            if path:
                results[primary] = snapshot_skill(primary, path)
            else:
                results[primary] = {
                    "errors": [f"skill path not found for '{primary}'"]
                }
        for sec in curated.get("secondary_skills", []) or []:
            if sec in skills_seen:
                continue
            skills_seen.add(sec)
            path = resolve_skill_path(sec)
            if path:
                results[sec] = snapshot_skill(sec, path)
            else:
                results[sec] = {
                    "errors": [f"skill path not found for '{sec}'"]
                }
    return results

# ════════════════════════════════════════════════════════════════════════════
#                            CODE_REQUESTS - UNFINISHED PATCH RETURN
# ============================================================================
# The agent authors and applies his own patches. code_tuner does not edit other
# bots' .py and does not auto-deploy.
#
# If a request cannot be finished by this bot, write it back to the agent's
# inbox as his own unfinished patch. Do not write upgrade packets to
# owner_inbox.
#
# code_tuner does NOT fulfill the request.

def read_pending_code_requests() -> list:
    """Read pending code requests (dormant in v4, kept for fallback path)."""
    requests = load_json(CODE_REQUESTS_FILE, [])
    if isinstance(requests, dict):
        requests = [requests]
    return [r for r in requests if not r.get("resolved", False)]

def mark_code_request_returned(request_id: str, packet_path: str):
    """
    Mark a code_request as returned to the agent's inbox as his own unfinished
    patch. Not resolved. This bot did not apply it.
    """
    requests = load_json(CODE_REQUESTS_FILE, [])
    if isinstance(requests, dict):
        requests = [requests]
    for r in requests:
        if r.get("id") == request_id or r.get("requested_at") == request_id:
            r["returned_at"] = _now_iso()
            r["returned_to"] = packet_path
            r["status"] = "returned_to_agent"
    save_json(CODE_REQUESTS_FILE, requests)

def return_unfinished_code_request(request: dict) -> str:
    """
    A request this bot cannot finish. It still does not edit other bots' .py
    and does not auto-deploy. Write the request back to the agent's inbox as his
    own unfinished patch. The agent applies his own patch.

    Returns path to written packet (or empty string on failure).
    """
    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    req_id = request.get("id") or request.get("requested_at") or ts

    bot = request.get("bot", "?")
    description = request.get("description", "(no description)")
    priority = request.get("priority", "medium")
    reasoning = request.get("reasoning", request.get("reason", ""))
    data_backing = request.get("data_backing", "")
    proposed_change = request.get("proposed_change", "")
    tuner_suggestion = request.get("tuner_suggestion", "")

    fname = f"code_tuner - agent_unfinished_patch_{bot}_{ts}.md"
    out_path = os.path.join(INBOX, fname)

    lines = [
        "# The agent unfinished patch",
        "",
        "**From:** code_tuner (could not finish this request)",
        f"**Returned at:** {_now_iso()}",
        f"**Request ID:** `{req_id}`",
        f"**Target bot:** `{bot}`",
        f"**Priority:** {priority}",
        "",
        "## What you were working on",
        "",
        description,
        "",
    ]
    if reasoning:
        lines += ["## Why", "", reasoning, ""]
    if data_backing:
        lines += ["## Data backing", "", data_backing, ""]
    if proposed_change:
        lines += ["## Proposed change so far", "", "```", proposed_change, "```", ""]
    if tuner_suggestion:
        lines += ["## code_tuner's read", "", tuner_suggestion, ""]

    lines += [
        "## What to do",
        "",
        "This bot did not finish the patch. It does not edit other bots' .py",
        "and it does not auto-deploy. This file is your own unfinished patch.",
        "You apply it yourself. Nothing was sent to owner_inbox.",
        "",
    ]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_tune(f"returned unfinished code_request '{req_id}' -> The agent inbox: {fname}")
        return out_path
    except Exception as e:
        log_tune(f"unfinished-patch write err: {e}")
        return ""


async def process_pending_code_requests():
    """
    If a code_request cannot be finished here, write it back to the agent's inbox
    as his own unfinished patch. Skip rows already returned.
    """
    pending = read_pending_code_requests()
    pending = [
        r for r in pending
        if r.get("status") != "returned_to_agent" and not r.get("returned_at") and not r.get("routed_at")
    ]
    if not pending:
        return

    for req in pending:
        packet_path = return_unfinished_code_request(req)
        if packet_path:
            req_id = req.get("id") or req.get("requested_at") or ""
            mark_code_request_returned(req_id, packet_path)


#                            MAIN LOOPS
# ════════════════════════════════════════════════════════════════════════════

async def registry_refresh_loop():
    """
    Periodic registry refresh. Re-discover bots, take snapshots, AST-scan,
    update _managed_by_warden + dependencies.

    Cadence: REGISTRY_REFRESH_S (default 5 min).
    """
    log_tune(f"registry_refresh_loop: starting (every {REGISTRY_REFRESH_S}s)")
    while True:
        try:
            registry = seed_or_refresh_registry()
            n_total = len(registry.get("bots", {}))
            n_unconfigured, _ = count_unconfigured_bots(registry)
            write_heartbeat("registry_refresh", {
                "total_bots": n_total,
                "unconfigured": n_unconfigured,
            })
            log_tune(f"registry refresh: {n_total} bots ({n_unconfigured} unconfigured)")
        except Exception as e:
            log_tune(f"registry_refresh err: {e}")
            write_heartbeat("registry_refresh", {"error": str(e)[:200]})
        await asyncio.sleep(REGISTRY_REFRESH_S)

async def snapshot_loop():
    """
    Source snapshot cron. Every SNAPSHOT_INTERVAL_S (default 30 min):
      - Take bot snapshots
      - Take associated skill snapshots
      - Detect changes (Stage 3 will write delta packets; Stage 1 just logs)
    """
    log_tune(f"snapshot_loop: starting (every {SNAPSHOT_INTERVAL_S}s)")
    while True:
        try:
            registry = load_json(BOTS_LIST_FILE, {"bots": {}})
            bots = discover_bots()
            bot_results = snapshot_all_bots(bots)
            skill_results = snapshot_all_associated_skills(registry)

            changes = []
            for name, result in bot_results.items():
                if result.get("changed") and result.get("old_hash"):
                    changes.append(("bot", name, result))
            for name, result in skill_results.items():
                if result.get("changed") and result.get("old_hash"):
                    changes.append(("skill", name, result))

            if changes:
                registry_now = load_json(BOTS_LIST_FILE, {"bots": {}})
                for kind, name, result in changes:
                    log_tune(
                        f"snapshot CHANGE: {kind} '{name}' "
                        f"({result.get('old_hash','?')[:8]} → "
                        f"{result.get('new_hash','?')[:8]})"
                    )
                    # Stage 3: fire source delta packet
                    try:
                        write_source_delta_packet(kind, name, result, registry_now)
                    except Exception as e:
                        log_tune(f"delta packet err for {kind} '{name}': {e}")

            write_heartbeat("snapshot_loop", {
                "snapshots_taken": len(bot_results) + len(skill_results),
                "changes_detected": len(changes),
            })
        except Exception as e:
            log_tune(f"snapshot_loop err: {e}")
            write_heartbeat("snapshot_loop", {"error": str(e)[:200]})
        await asyncio.sleep(SNAPSHOT_INTERVAL_S)

async def heartbeat_loop():
    """Standalone heartbeat for warden Layer 4 (in case other loops stall)."""
    while True:
        try:
            write_heartbeat("heartbeat")
        except Exception:
            pass
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)

async def code_request_router_loop():
    """
    Polls code_requests.json every 15 min. Unfinished requests are written
    back to the agent's inbox as his own patch. Nothing is written to owner_inbox.
    """
    while True:
        try:
            await process_pending_code_requests()
        except Exception as e:
            log_tune(f"code_request_router err: {e}")
        await asyncio.sleep(15 * 60)

# ════════════════════════════════════════════════════════════════════════════
#                            STAGE 2 — SETUP MODE
# ════════════════════════════════════════════════════════════════════════════
# When a bot in bots_list.json has _curated_by_agent == {} (unconfigured),
# The agent needs to walk through setup for that bot. Feed:
#
#   1. On boot: count unconfigured bots. If > 0, fire a SETUP INVITATION packet
#      listing them. The agent picks one.
#   2. The agent writes to bots/json_relay/code_tuner_setup.json:
#        {"action": "configure", "bot": "<bot_name>"}
#      OR
#        {"action": "skip", "bot": "<bot_name>", "reason": "..."}
#      OR
#        {"action": "reset", "bot": "<bot_name>"}
#   3. code_tuner picks up the response and acts:
#        - configure → write DISCOVERY PACKET for that bot (source ToC,
#          output file inventory, metric catalog for its category)
#        - skip → mark _curated_by_agent with monitoring_active: false and
#          a reason field
#        - reset → wipe _curated_by_agent and output_files for that bot
#   4. After the agent reads the discovery packet, the agent writes a follow-up
#      to code_tuner_setup.json:
#        {"action": "configure_response", "bot": "<name>", ...full config...}
#   5. code_tuner validates and writes to _curated_by_agent + output_files.
#   6. If more unconfigured bots remain, fire a follow-up invitation.
#
# Source-of-truth doctrine: code_tuner WRITES to _curated_by_agent and
# output_files during setup mode. _managed_by_warden stays warden's territory.

# ── Metric catalog by category ────────────────────────────────────────────
# The agent picks metrics from the catalog for his bot's category.
# Each entry: (metric_name, one_line_description, expected_output_format)
# Stage 3 will register actual computer functions in METRIC_REGISTRY.

CATEGORY_METRIC_CATALOG = {
    "trading": [
        ("pnl",                       "Sum of trade PnL in window",
                                      "requires: trade events with PnL field"),
        ("win_rate",                  "wins / (wins + losses)",
                                      "requires: trade events with outcome field"),
        ("hold_time_median",          "Median position hold duration",
                                      "requires: position events with open/close timestamps"),
        ("hold_time_p95",             "95th percentile hold duration",
                                      "requires: position events with open/close timestamps"),
        ("drawdown_max",              "Worst single-trade loss in window",
                                      "requires: trade events with PnL field"),
        ("small_buy_ratio",           "Count(buys where size<X) / count(buys)",
                                      "requires: trade events with size field; configurable threshold"),
        ("trade_frequency",           "Trades per day in window",
                                      "requires: trade events with timestamp"),
        ("decline_rate_by_reason",    "Declines grouped by skip_reason field",
                                      "requires: decline events with reason field"),
        ("decline_drift_2d",          "Change in decline rate vs prior 2 days",
                                      "requires: decline events with timestamps"),
        ("signal_to_trade_conversion","Trades fired / signals received",
                                      "requires: signal events + trade events"),
    ],
    "signal": [
        ("signal_frequency",          "Emissions per day in window",
                                      "requires: signal_contract events"),
        ("signal_tier_distribution",  "% A/B/S of emitted signals",
                                      "requires: signal_contract events with tier field"),
        ("confluence_rate",           "Overlap with other signal bots on same topic",
                                      "requires: signal_contract events + confluence audit"),
        ("signal_to_move_correlation","Did signals precede actual price moves",
                                      "requires: signal events + price candles"),
        ("per_source_yield",          "Which underlying sources contribute most",
                                      "requires: signal events with source attribution"),
    ],
    "agent": [
        ("heartbeat_uptime",          "% time bot's heartbeat was fresh",
                                      "requires: heartbeat file presence over window"),
        ("error_rate",                "Error log lines per day",
                                      "requires: log file with error markers"),
        ("throughput",                "Tasks/cycles completed per day",
                                      "requires: bot-specific output (varies)"),
        ("task_completion_rate",      "Tasks completed / tasks attempted",
                                      "requires: task ledger or decision log"),
    ],
    "infra": [
        ("heartbeat_uptime",          "% time bot's heartbeat was fresh",
                                      "requires: heartbeat file presence over window"),
        ("error_rate",                "Error log lines per day",
                                      "requires: log file with error markers"),
    ],
}

VALID_CATEGORIES = set(CATEGORY_METRIC_CATALOG.keys())

VALID_TIME_SCALES = [
    "rolling_2d", "rolling_7d", "rolling_14d", "rolling_30d",
    "daily", "weekly", "monthly",
    "since_last_amendment", "since_last_source_change",
]

# ── Bot output discovery ──────────────────────────────────────────────────
# Scan bot_logs/ for files matching the bot's naming pattern. Returns an
# inventory: {filename, size_bytes, ~write_freq_estimate, suggested_format,
# suggested_track}.
#
# Suggested format/track are heuristic hints. The agent overrides during setup.

KNOWN_FORMAT_HINTS = {
    # Filename pattern → format hint
    "_trades.log":          ("jsonl_trades",        True),
    ".jsonl":               ("jsonl_trades",        True),
    "_trades.json":         ("json_rolling",        True),
    "_decisions.json":      ("json_rolling",        True),
    "autopsy":              ("json_rolling",        True),
    "_signal":              ("signal_contract_v1",  True),
    "_heartbeat.json":      ("heartbeat",           False),
    "_self_description.json": ("self_description",  False),
    ".log":                 ("freeform_text",       False),
    "_pnl.json":            ("json_rolling",        True),
}

def _discover_bot_outputs(bot_name: str) -> dict[str, dict]:
    """
    Scan bot_logs/ for files this bot is likely writing to. Returns map of
    short_key → output_descriptor.

    Heuristic matching:
      - File starts with bot_name → almost certainly this bot's output
      - File contains a related keyword (e.g., 'perp_decisions' for perps_bot)
    """
    outputs = {}
    if not os.path.isdir(BOT_LOGS):
        return outputs

    bot_name_lower = bot_name.lower()
    # Some bots write to files that don't start with their full name —
    # e.g., perps_bot writes perp_decisions.json, perp_pnl.json.
    # Hand-curated mapping of bot → also-search-for patterns.
    extra_patterns: dict[str, list[str]] = {
        "perps_bot":       ["perp_decisions", "perp_pnl", "perp_positions", "perp_episodes",
                            "perp_autopsy", "perps.log"],
    }
    extras = extra_patterns.get(bot_name, [])

    for entry in os.scandir(BOT_LOGS):
        if not entry.is_file():
            continue
        fname = entry.name
        fname_lower = fname.lower()
        matched = False

        # Direct prefix match
        if fname_lower.startswith(bot_name_lower):
            matched = True

        # Extra pattern match
        if not matched:
            for pat in extras:
                if pat.lower() in fname_lower:
                    matched = True
                    break

        if not matched:
            continue

        # Format + track suggestion
        suggested_format = "freeform_text"
        suggested_track = False
        for pattern, (fmt, track) in KNOWN_FORMAT_HINTS.items():
            if pattern in fname_lower:
                suggested_format = fmt
                suggested_track = track
                break

        try:
            size = entry.stat().st_size
            mtime = entry.stat().st_mtime
        except Exception:
            size = 0
            mtime = 0

        # Estimate write frequency from file size + age
        write_freq = "unknown"
        if size > 0 and mtime > 0:
            age_days = max((time.time() - mtime) / 86400, 0.01)
            # crude — large file + recent mtime suggests frequent writes
            if size > 1024 * 1024 and age_days < 1:
                write_freq = "high (>100/day est)"
            elif size > 100 * 1024:
                write_freq = "medium (~10-100/day est)"
            else:
                write_freq = "low (<10/day est)"

        # Reason if not tracked
        reason_not_tracked = ""
        if not suggested_track:
            if suggested_format == "freeform_text":
                reason_not_tracked = "verbose debug, no signal density"
            elif suggested_format == "heartbeat":
                reason_not_tracked = "warden owns this; no need to duplicate"
            elif suggested_format == "self_description":
                reason_not_tracked = "Sprint 41 capability surface, not perf data"

        # Build short key from filename (strip extension, strip bot_name prefix)
        key = fname
        for ext in (".json", ".log", ".jsonl", ".md"):
            if key.endswith(ext):
                key = key[:-len(ext)]
                break
        if key.lower().startswith(bot_name_lower + "_"):
            key = key[len(bot_name) + 1:]
        key = key or "main"

        outputs[key] = {
            "filename": fname,
            "path": entry.path,
            "size_bytes": size,
            "write_freq_estimate": write_freq,
            "suggested_format": suggested_format,
            "suggested_track": suggested_track,
            "reason_not_tracked": reason_not_tracked,
        }

    return outputs

# ── Source ToC extractor (for big bots in discovery packets) ──────────────

def _extract_source_toc(bot_filepath: str) -> list[dict]:
    """
    AST-based extraction of function table-of-contents from a bot's source.
    Returns list of {name, start_line, end_line, type, docstring_one_liner}.

    Used in discovery packets for big bots — instead of dumping 14k lines,
    we present the function ToC and let the agent request specific sections.
    """
    if not os.path.exists(bot_filepath):
        return []
    try:
        with open(bot_filepath, "r", encoding="utf-8-sig") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception as e:
        log_tune(f"source toc extract err on {bot_filepath}: {e}")
        return []

    toc = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else (
                "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            )
            # Extract one-line docstring summary
            doc_one_liner = ""
            docstring = ast.get_docstring(node)
            if docstring:
                # Take first non-empty line, trim to 100 chars
                for line in docstring.splitlines():
                    line = line.strip()
                    if line:
                        doc_one_liner = line[:120]
                        break

            end_line = getattr(node, "end_lineno", node.lineno)
            toc.append({
                "name": node.name,
                "start_line": node.lineno,
                "end_line": end_line,
                "type": kind,
                "docstring_one_liner": doc_one_liner,
            })

    # Sort by line number
    toc.sort(key=lambda x: x["start_line"])
    return toc

# ── Setup invitation packet writer ────────────────────────────────────────

def write_setup_invitation_packet(unconfigured_bots: list[str],
                                   registry: dict) -> str:
    """
    Write the initial setup invitation listing unconfigured bots. Returns
    path of written packet.
    """
    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    fname = f"code_tuner - setup_invitation_{ts}.md"
    out_path = os.path.join(INBOX, fname)

    lines = [
        "# code_tuner Setup Mode — bots awaiting configuration",
        "",
        f"**Time:** {_now_iso()}",
        f"**Unconfigured bots:** {len(unconfigured_bots)} of {len(registry.get('bots', {}))}",
        "",
        "code_tuner has been refactored into a performance monitor (Sprint 42). To set",
        "up monitoring for the Perps ecosystem, walk through each bot and configure what you",
        "want tracked. Configuration persists across boots.",
        "",
        "For each bot, you'll:",
        "1. Pick one bot to configure",
        "2. Receive a discovery packet with source context + output file inventory + metric catalog",
        "3. Respond with category / skills / which outputs to track / which metrics",
        "",
        "## Bots awaiting configuration",
        "",
    ]

    for i, bot_name in enumerate(unconfigured_bots, 1):
        entry = registry.get("bots", {}).get(bot_name, {})
        managed = entry.get("_managed_by_warden", {})
        deps = entry.get("dependencies", []) or []
        deps_str = ", ".join(deps) if deps else "none"
        lines_count = managed.get("source_lines", 0)
        version = managed.get("version", "unknown")
        path_exists = managed.get("path_exists", True)
        # 2026-10-16 chair fix: the registry value is a snapshot taken at boot, often
        # before the bot wrote its first heartbeat. Check the file live.
        _hbf = managed.get("heartbeat_file") or _resolve_bot_heartbeat_path(bot_name)
        hb_exists = bool(managed.get("heartbeat_exists")) or os.path.exists(_hbf)

        bot_meta_parts = [f"{lines_count} lines", f"version {version}"]
        if deps:
            bot_meta_parts.append(f"deps: {deps_str}")
        if not path_exists:
            bot_meta_parts.append("⚠ FILE MISSING")
        if not hb_exists:
            bot_meta_parts.append("no heartbeat file yet")

        lines.append(f"{i}. **{bot_name}** — " + " | ".join(bot_meta_parts))

    lines += [
        "",
        "## How to respond",
        "",
        "Write to `bots/json_relay/code_tuner_setup.json`:",
        "",
        "**To configure a bot:**",
        "```json",
        '{"action": "configure", "bot": "perps_bot"}',
        "```",
        "→ I'll send you a discovery packet with source ToC + outputs + metric catalog",
        "",
        "**To skip a bot (mark monitoring inactive):**",
        "```json",
        '{"action": "skip", "bot": "owner_commands", "reason": "command responder, no perf metrics needed"}',
        "```",
        "",
        "**To reset a previously-configured bot (re-enter setup):**",
        "```json",
        '{"action": "reset", "bot": "perps_bot"}',
        "```",
        "",
        "You can configure one bot per session or batch through several — whatever pace suits you.",
        "",
        "---",
        "",
        "*Packet from code_tuner v4.2 (setup mode). I will re-send this list",
        "if you haven't responded after 24h, but won't spam you — one reminder, then silence.*",
    ]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_tune(f"setup invitation written: {fname}")
        return out_path
    except Exception as e:
        log_tune(f"setup invitation write err: {e}")
        return ""

# ── Discovery packet writer (per-bot) ─────────────────────────────────────

# Threshold for "big bot" — above this line count, we don't dump full source
DISCOVERY_BIG_BOT_LINE_THRESHOLD = 2000

def write_discovery_packet(bot_name: str, registry: dict) -> str:
    """
    Write a per-bot discovery packet. Returns path of written packet.
    """
    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    fname = f"code_tuner - discovery_{bot_name}_{ts}.md"
    out_path = os.path.join(INBOX, fname)

    entry = registry.get("bots", {}).get(bot_name, {})
    managed = entry.get("_managed_by_warden", {})
    deps = entry.get("dependencies", []) or []
    bot_path = managed.get("path", "")
    lines_count = managed.get("source_lines", 0)
    version = managed.get("version", "unknown")
    src_hash = managed.get("source_hash", "")
    snap_path = managed.get("snapshot_path", "")
    path_exists = managed.get("path_exists", True)
    _hbf = managed.get("heartbeat_file") or _resolve_bot_heartbeat_path(bot_name)
    hb_exists = bool(managed.get("heartbeat_exists")) or os.path.exists(_hbf)   # live check (2026-10-16)
    threshold = managed.get("heartbeat_threshold_minutes")

    # Inventory bot outputs
    outputs = _discover_bot_outputs(bot_name)

    lines = [
        f"# Configure {bot_name}",
        "",
        f"**Bot:** {bot_name}.py",
        f"**Version:** {version}",
        f"**Lines:** {lines_count}",
        f"**Source hash:** `{src_hash[:16]}...`" if src_hash else "**Source hash:** (no snapshot yet)",
        f"**Source snapshot:** `{snap_path}`" if snap_path else "",
        f"**Dependencies (auto-detected via AST):** {', '.join(deps) if deps else 'none'}",
        f"**Heartbeat file exists:** {'yes' if hb_exists else 'NO — bot may not have booted yet'}",
        f"**Heartbeat threshold (warden):** {threshold}min" if threshold else "",
        "",
    ]
    if not path_exists:
        lines += [
            "⚠ **WARNING:** The bot's .py file does not exist at the expected path. ",
            "Source-based features (ToC, full source dump) will be empty until this is resolved.",
            "",
        ]

    # Source ToC for big bots, full source for small bots
    if path_exists and lines_count > 0:
        if lines_count > DISCOVERY_BIG_BOT_LINE_THRESHOLD:
            lines += [
                f"## Source ToC ({lines_count} lines — too large for full dump)",
                "",
                "Function table-of-contents extracted via AST. To see any specific function's",
                "source, request via meeting_watcher (Stage 4 feature):",
                "",
                "```",
                f'show me {bot_name}\'s <function_name>',
                "```",
                "",
                "| # | Name | Type | Lines | One-line purpose |",
                "|---|------|------|-------|------------------|",
            ]
            toc = _extract_source_toc(bot_path)
            for i, item in enumerate(toc, 1):
                doc = item.get("docstring_one_liner", "")
                # Escape pipes for markdown table
                doc = doc.replace("|", "\\|")
                name = item["name"].replace("|", "\\|")
                lines.append(
                    f"| {i} | `{name}` | {item['type']} | "
                    f"{item['start_line']}-{item['end_line']} | {doc} |"
                )
            lines.append("")
        else:
            # Small bot — dump full source
            lines += [
                f"## Full source ({lines_count} lines)",
                "",
                "```python",
            ]
            try:
                with open(bot_path, "r", encoding="utf-8-sig") as f:
                    src = f.read()
                lines.append(src)
            except Exception as e:
                lines.append(f"# (could not read source: {e})")
            lines.append("```")
            lines.append("")

    # Output file inventory
    if outputs:
        lines += [
            "## Output files detected in bot_logs/",
            "",
            "| Output | Filename | Format (suggested) | Track? | Write freq | Notes |",
            "|--------|----------|--------------------|--------|-------------|-------|",
        ]
        for key, info in outputs.items():
            track_str = "✓" if info["suggested_track"] else "✗"
            notes = info.get("reason_not_tracked", "")
            lines.append(
                f"| `{key}` | `{info['filename']}` | "
                f"`{info['suggested_format']}` | {track_str} | "
                f"{info['write_freq_estimate']} | {notes} |"
            )
        lines.append("")
        lines.append("*Suggested track is heuristic. Override per file in your response below.*")
        lines.append("")
    else:
        lines += [
            "## Output files",
            "",
            "**No output files detected** for this bot in bot_logs/. Either the bot",
            "hasn't booted yet, or it writes to non-standard paths. Skip the outputs",
            "section in your response, or specify paths explicitly.",
            "",
        ]

    # Metric catalog — show all categories so the agent can pick the right one
    lines += [
        "## Metric catalog — pick one category for this bot",
        "",
    ]
    for cat, metric_list in CATEGORY_METRIC_CATALOG.items():
        lines.append(f"### Category: `{cat}`")
        lines.append("")
        lines.append("| Metric | Description | Requires |")
        lines.append("|--------|-------------|----------|")
        for metric_name, desc, requires in metric_list:
            lines.append(f"| `{metric_name}` | {desc} | {requires} |")
        lines.append("")

    # Time scales
    lines += [
        "## Time scales available",
        "",
        "Pick which time scales to compute each metric on (you can pick multiple per metric).",
        "",
    ]
    for scale in VALID_TIME_SCALES:
        lines.append(f"- `{scale}`")
    lines.append("")

    # Response shape
    lines += [
        "## How to respond",
        "",
        "Write to `bots/json_relay/code_tuner_setup.json`:",
        "",
        "```json",
        "{",
        '  "action": "configure_response",',
        f'  "bot": "{bot_name}",',
        '  "category": "trading",',
        '  "primary_skill": "perps-boss",',
        '  "secondary_skills": ["forensics", "pnl-tracker"],',
        '  "tier": "trading_critical",',
        '  "role": "Jupiter perps trading",',
        '  "monitoring_active": true,',
        '  "outputs": {',
        '    "perp_episodes": {',
        '      "track": true,',
        '      "metrics": ["pnl", "win_rate", "hold_time_median"],',
        '      "time_scales": ["rolling_2d", "rolling_7d"]',
        '    },',
        '    "perp_decisions": {',
        '      "track": true,',
        '      "metrics": ["decline_rate_by_reason", "decline_drift_2d"],',
        '      "time_scales": ["rolling_2d"]',
        '    },',
        '    "raw_debug_log": {',
        '      "track": false,',
        '      "reason": "verbose debug, no signal"',
        '    }',
        "  }",
        "}",
        "```",
        "",
        "**Fields:**",
        "- `category` (required): one of trading | signal | agent | infra",
        "- `primary_skill` (required, may be `null` if no skill): name from boot-registry",
        "- `secondary_skills` (optional): array of skill names",
        "- `tier` (free-form): your tier label (e.g., trading_critical, intelligence_high)",
        "- `role` (free-form): one-line description of what the bot does",
        "- `monitoring_active` (default true): false = mark as 'tracked but skip perf packets'",
        "- `outputs` (optional): per-output-file config — `track`, `metrics` (from catalog),",
        "  `time_scales` (from list), or `reason` if not tracked",
        "",
        "If `primary_skill` is `null`, perf packets for this bot will route to no skill —",
        "you'll see them in your generic inbox feed without a skill-load header.",
        "",
        "---",
        "",
        f"*P3 discovery packet for {bot_name}. Generated {_now_iso()}.*",
    ]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_tune(f"discovery packet written: {fname}")
        return out_path
    except Exception as e:
        log_tune(f"discovery packet write err: {e}")
        return ""

# ── Setup response handler ────────────────────────────────────────────────

def _validate_configure_response(data: dict, registry: dict) -> tuple[bool, str]:
    """Validate the agent's configure_response shape. Returns (ok, error_msg)."""
    bot = data.get("bot")
    if not bot or bot not in registry.get("bots", {}):
        return False, f"bot '{bot}' not in registry"
    category = data.get("category")
    if category not in VALID_CATEGORIES:
        return False, f"category '{category}' not in valid set: {sorted(VALID_CATEGORIES)}"
    primary_skill = data.get("primary_skill")
    if primary_skill is not None and not isinstance(primary_skill, str):
        return False, "primary_skill must be string or null"
    secondary = data.get("secondary_skills", [])
    if not isinstance(secondary, list):
        return False, "secondary_skills must be a list"
    if not all(isinstance(s, str) for s in secondary):
        return False, "secondary_skills entries must be strings"

    # Validate metrics against category catalog
    cat_metrics = {m[0] for m in CATEGORY_METRIC_CATALOG.get(category, [])}
    outputs = data.get("outputs", {})
    if not isinstance(outputs, dict):
        return False, "outputs must be a dict"
    for out_key, out_cfg in outputs.items():
        if not isinstance(out_cfg, dict):
            return False, f"outputs.{out_key} must be a dict"
        if out_cfg.get("track"):
            metrics = out_cfg.get("metrics", [])
            if not isinstance(metrics, list):
                return False, f"outputs.{out_key}.metrics must be a list"
            for m in metrics:
                if m not in cat_metrics:
                    return False, (
                        f"outputs.{out_key}.metrics: '{m}' not in catalog for "
                        f"category '{category}'. Available: {sorted(cat_metrics)}"
                    )
            scales = out_cfg.get("time_scales", [])
            if not isinstance(scales, list):
                return False, f"outputs.{out_key}.time_scales must be a list"
            for s in scales:
                if s not in VALID_TIME_SCALES:
                    return False, (
                        f"outputs.{out_key}.time_scales: '{s}' not in "
                        f"valid scales: {VALID_TIME_SCALES}"
                    )
    return True, ""

def _write_setup_reject_packet(reason: str, raw: dict) -> str:
    """Tell the agent his setup response failed validation."""
    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    fname = f"code_tuner - setup_rejected_{ts}.md"
    out_path = os.path.join(INBOX, fname)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(
                f"# Setup response rejected\n\n"
                f"**Time:** {_now_iso()}\n\n"
                f"**Error:** {reason}\n\n"
                f"## Your response (raw)\n\n"
                f"```json\n{json.dumps(raw, indent=2)[:4000]}\n```\n\n"
                f"## What to do\n\n"
                f"Fix the issue and write a new response to "
                f"`bots/json_relay/code_tuner_setup.json`. The original response "
                f"file has been deleted. Bot's existing config is unchanged.\n"
            )
        return out_path
    except Exception as e:
        log_tune(f"reject packet write err: {e}")
        return ""

def _apply_configure_response(data: dict, registry: dict) -> str:
    """
    Write the agent's configure_response to bots_list.json under _curated_by_agent
    and output_files. Returns status message.
    """
    bot = data["bot"]
    entry = registry["bots"][bot]

    curated = {
        "role":               data.get("role", ""),
        "category":           data["category"],
        "primary_skill":      data.get("primary_skill"),
        "secondary_skills":   data.get("secondary_skills", []),
        "tier":               data.get("tier", ""),
        "monitoring_active":  data.get("monitoring_active", True),
        "configured_at":      _now_iso(),
        "configured_in_session": "setup_mode",
    }
    entry["_curated_by_agent"] = curated

    # Build output_files entries with metrics_from_this for Stage 3 aggregator
    out_files = {}
    raw_outputs = data.get("outputs", {})
    # Try to look up the discovered outputs for path / format
    discovered = _discover_bot_outputs(bot)
    for out_key, out_cfg in raw_outputs.items():
        disc = discovered.get(out_key, {})
        path = disc.get("path", os.path.join(BOT_LOGS, disc.get("filename", "")))
        fmt = disc.get("suggested_format", "unknown")
        rec = {
            "path": path,
            "format": fmt,
            "track": out_cfg.get("track", False),
        }
        if out_cfg.get("track"):
            rec["metrics_from_this"] = out_cfg.get("metrics", [])
            rec["time_scales"] = out_cfg.get("time_scales", [])
        else:
            rec["reason_not_tracked"] = out_cfg.get("reason", "")
        out_files[out_key] = rec
    entry["output_files"] = out_files

    save_json(BOTS_LIST_FILE, registry)
    return f"configured '{bot}': category={data['category']}, " \
           f"{len(out_files)} output files, " \
           f"{sum(1 for o in out_files.values() if o.get('track'))} tracked"

def _apply_skip_action(data: dict, registry: dict) -> str:
    """Mark a bot as monitoring_active: false with a reason. Doesn't fail
    validation if no config exists yet — creates a minimal curated entry."""
    bot = data.get("bot")
    if not bot or bot not in registry.get("bots", {}):
        return f"bot '{bot}' not in registry"
    entry = registry["bots"][bot]
    entry["_curated_by_agent"] = {
        "role": "(skipped by the agent)",
        "category": "infra",
        "primary_skill": None,
        "secondary_skills": [],
        "monitoring_active": False,
        "skip_reason": data.get("reason", "(no reason given)"),
        "configured_at": _now_iso(),
        "configured_in_session": "setup_mode_skip",
    }
    entry["output_files"] = {}
    save_json(BOTS_LIST_FILE, registry)
    return f"skipped '{bot}': {data.get('reason', '(no reason)')}"

def _apply_reset_action(data: dict, registry: dict) -> str:
    """Wipe _curated_by_agent + output_files for a bot. Bot re-enters setup."""
    bot = data.get("bot")
    if not bot or bot not in registry.get("bots", {}):
        return f"bot '{bot}' not in registry"
    entry = registry["bots"][bot]
    entry["_curated_by_agent"] = {}
    entry["output_files"] = {}
    save_json(BOTS_LIST_FILE, registry)
    return f"reset '{bot}': cleared curated config + outputs; will re-enter setup mode"

async def _process_setup_response_file() -> None:
    """
    Read code_tuner_setup.json, validate, apply, delete file, fire next packet.

    Action types:
      - configure       → write discovery packet for the named bot
      - configure_response → apply config to bots_list.json
      - skip            → mark monitoring_active: false
      - reset           → clear curated half (re-enter setup)
    """
    if not os.path.exists(SETUP_RESPONSE_FILE):
        return

    raw = load_json(SETUP_RESPONSE_FILE, default=None)
    if raw is None:
        log_tune("setup response file unreadable — deleting")
        try: os.remove(SETUP_RESPONSE_FILE)
        except Exception: pass
        return

    action = raw.get("action") if isinstance(raw, dict) else None
    if action not in {"configure", "configure_response", "skip", "reset"}:
        _write_setup_reject_packet(
            f"unknown action '{action}'. Valid: configure | configure_response | skip | reset",
            raw,
        )
        try: os.remove(SETUP_RESPONSE_FILE)
        except Exception: pass
        return

    # Re-read registry fresh so we don't write stale data
    registry = load_json(BOTS_LIST_FILE, default={"bots": {}})

    bot = raw.get("bot")
    if not bot or bot not in registry.get("bots", {}):
        _write_setup_reject_packet(
            f"bot '{bot}' not found in registry. Registry may be empty if warden "
            f"hasn't seeded yet — wait 5 min and retry.",
            raw,
        )
        try: os.remove(SETUP_RESPONSE_FILE)
        except Exception: pass
        return

    if action == "configure":
        # The agent picked a bot to configure → fire discovery packet
        write_discovery_packet(bot, registry)
        log_tune(f"setup: discovery packet sent for '{bot}'")

    elif action == "configure_response":
        ok, err = _validate_configure_response(raw, registry)
        if not ok:
            _write_setup_reject_packet(err, raw)
        else:
            status = _apply_configure_response(raw, registry)
            log_tune(f"setup: {status}")
            # If more bots remain unconfigured, fire a follow-up invitation
            new_registry = load_json(BOTS_LIST_FILE, default={"bots": {}})
            n_unconf, unconf_names = count_unconfigured_bots(new_registry)
            if n_unconf > 0:
                write_setup_invitation_packet(unconf_names, new_registry)

    elif action == "skip":
        status = _apply_skip_action(raw, registry)
        log_tune(f"setup: {status}")
        new_registry = load_json(BOTS_LIST_FILE, default={"bots": {}})
        n_unconf, unconf_names = count_unconfigured_bots(new_registry)
        if n_unconf > 0:
            write_setup_invitation_packet(unconf_names, new_registry)

    elif action == "reset":
        status = _apply_reset_action(raw, registry)
        log_tune(f"setup: {status}")
        # Single-bot reset doesn't fire a fresh invitation — the agent can pick
        # this bot back up whenever via 'configure' action.

    # Delete the response file after processing
    try: os.remove(SETUP_RESPONSE_FILE)
    except Exception as e: log_tune(f"setup response delete err: {e}")

async def setup_mode_loop():
    """
    File-presence watcher for code_tuner_setup.json. Same cheap-when-idle
    pattern as the perps meeting watcher.

    Also fires initial setup invitation on first boot if unconfigured bots exist.
    """
    log_tune("setup_mode_loop: starting (file-presence watcher)")

    # First-boot invitation: fire once if there are unconfigured bots
    initial_invitation_sent = False

    while True:
        try:
            # Initial invitation (one-time on first boot)
            if not initial_invitation_sent:
                registry = load_json(BOTS_LIST_FILE, default={"bots": {}})
                n_unconf, unconf_names = count_unconfigured_bots(registry)
                if n_unconf > 0:
                    write_setup_invitation_packet(unconf_names, registry)
                initial_invitation_sent = True

            # File-presence watch
            if os.path.exists(SETUP_RESPONSE_FILE):
                await _process_setup_response_file()
            else:
                write_heartbeat("setup_mode", {"watching": True})
        except Exception as e:
            log_tune(f"setup_mode_loop err: {e}")
            write_heartbeat("setup_mode", {"error": str(e)[:200]})
        await asyncio.sleep(10)

# ════════════════════════════════════════════════════════════════════════════
#                            STAGE 3 — PERF MONITORING
# ════════════════════════════════════════════════════════════════════════════
# Pipeline:
#   - PARSER_REGISTRY: format string → parser function (raw file → event list)
#   - METRIC_REGISTRY: metric name → computer function (events, window → value)
#   - collect_outputs_for_bot(): runs PARSERS for tracked files
#   - compute_metrics_for_bot(): runs METRIC computers per the agent's config
#   - metrics_current.json: persistent stable file, refreshed every perf cycle
#                            (morning-brief + the agent consume from here)
#   - 2-day perf packet: per-bot, fires every 48h on rotating cadence
#   - 30-day deep audit: per-bot, fires every 30d
#   - Source delta packet: fires when snapshot detects source change
#
# Doctrine reminders:
#   - Observations, NOT prescriptions. Raw stats only, the agent interprets.
#   - Code patches authored by the agent. The agent applies his own patch. code_tuner observes.
#     Floor: never self-modify code_tuner.py; only reload a change already on disk.
#     Do not edit other bots' .py and do not auto-deploy.

# ── Time window resolver ──────────────────────────────────────────────────

def _resolve_time_window(scale: str) -> tuple[float, float]:
    """
    Convert a time scale ('rolling_2d', 'daily', etc.) to a (start_ts, end_ts)
    pair in unix seconds. end_ts is always 'now'.

    For anchor-based scales (since_last_amendment, since_last_source_change),
    falls back to rolling_7d if the anchor isn't found.
    """
    now = time.time()
    if scale.startswith("rolling_") and scale.endswith("d"):
        try:
            days = int(scale[len("rolling_"):-1])
            return (now - days * 86400, now)
        except ValueError:
            pass
    if scale == "daily":
        return (now - 86400, now)
    if scale == "weekly":
        return (now - 7 * 86400, now)
    if scale == "monthly":
        return (now - 30 * 86400, now)
    if scale == "since_last_amendment":
        # Stage 4+ wiring — for now, default to rolling_7d
        return (now - 7 * 86400, now)
    if scale == "since_last_source_change":
        # Stage 4+ wiring — for now, default to rolling_7d
        return (now - 7 * 86400, now)
    # Unknown scale — default to rolling_2d
    return (now - 2 * 86400, now)

def _parse_event_ts(event: dict) -> Optional[float]:
    """
    Best-effort extraction of a unix timestamp from an event dict.
    Tries common field names: ts, timestamp, time, opened_at, closed_at, fired_at.
    Handles ISO 8601 strings and unix-seconds floats/ints.
    """
    for key in ("ts", "timestamp", "time", "opened_at", "closed_at",
                "fired_at", "executed_at", "created_at", "resolved_at"):
        v = event.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            # Could be seconds or milliseconds — heuristic split at year 2200
            return v if v < 7.3e9 else v / 1000.0
        if isinstance(v, str):
            # Try ISO format
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=EST)
                return dt.timestamp()
            except ValueError:
                continue
    return None

def _filter_events_to_window(events: list, start_ts: float,
                              end_ts: float) -> list:
    """Filter event list to those within the window. Drops events without timestamps."""
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        ts = _parse_event_ts(e)
        if ts is None:
            continue
        if start_ts <= ts <= end_ts:
            out.append(e)
    return out

# ── PARSER_REGISTRY ───────────────────────────────────────────────────────
# Each parser reads its file format and returns a list of event dicts.
# Missing files / empty files return []. Errors logged, [] returned.

def _parse_jsonl_trades(path: str) -> list:
    """Parse a JSONL trade log. One event per line."""
    if not os.path.exists(path):
        return []
    events = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log_tune(f"_parse_jsonl_trades err on {path}: {e}")
    return events

def _parse_json_rolling(path: str) -> list:
    """
    Parse a JSON file expected to be a list of events (e.g., trades.json,
    perp_decisions.json, autopsy_reports.json). If it's a dict, look for
    common wrapper keys.
    """
    if not os.path.exists(path):
        return []
    data = load_json(path, default=None)
    if data is None:
        return []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        # Try common wrapper keys
        for key in ("events", "trades", "decisions", "entries", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)]
        # Treat dict-of-events as values
        return [v for v in data.values() if isinstance(v, dict)]
    return []

def _parse_signal_contract_v1(path: str) -> list:
    """Parse signal_contract v1.1.0 envelope events."""
    return _parse_json_rolling(path)

def _parse_freeform_text(path: str) -> list:
    """
    Parse a freeform text log. Returns events with {ts, line} where ts is the
    file's mtime (no per-line timestamps). Mostly used for error_rate counting.
    """
    if not os.path.exists(path):
        return []
    events = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                events.append({"line": line})
        # Tag with file mtime as fallback ts (can't be filtered by window
        # accurately, but error_rate sums over all lines anyway)
    except Exception as e:
        log_tune(f"_parse_freeform_text err on {path}: {e}")
    return events

def _parse_heartbeat(path: str) -> list:
    """
    Heartbeat file isn't event-shaped. We synthesize a single event with the
    file's mtime, used for uptime calc.
    """
    if not os.path.exists(path):
        return []
    try:
        mtime = os.path.getmtime(path)
        return [{"ts": mtime, "kind": "heartbeat", "fresh": True}]
    except Exception:
        return []

PARSER_REGISTRY = {
    "jsonl_trades":           _parse_jsonl_trades,
    "json_rolling":           _parse_json_rolling,
    "signal_contract_v1":     _parse_signal_contract_v1,
    "freeform_text":          _parse_freeform_text,
    "heartbeat":              _parse_heartbeat,
    "self_description":       lambda p: [],  # never tracked for perf
}

def collect_outputs_for_bot(bot_name: str, registry_entry: dict) -> dict[str, list]:
    """
    For each tracked output file in the bot's config, parse and return events.
    Returns {output_key: events_list}.
    """
    out = {}
    output_files = registry_entry.get("output_files", {})
    for out_key, out_cfg in output_files.items():
        if not out_cfg.get("track"):
            continue
        fmt = out_cfg.get("format", "unknown")
        parser = PARSER_REGISTRY.get(fmt)
        if parser is None:
            out[out_key] = []
            continue
        path = out_cfg.get("path", "")
        try:
            events = parser(path)
            out[out_key] = events
        except Exception as e:
            log_tune(f"collect: parser err for {bot_name}.{out_key} ({fmt}): {e}")
            out[out_key] = []
    return out

# ── METRIC_REGISTRY ───────────────────────────────────────────────────────
# Each computer takes (events, start_ts, end_ts) and returns a value
# (number, dict, or {"value": ..., "detail": ...} for richer reporting).
# Computers must be defensive — empty event lists return safe defaults.

def _safe_pnl_of(event: dict) -> float:
    """Extract PnL from a trade event, trying common field names."""
    for key in ("pnl", "profit_loss", "pnl_sol", "pnl_usd", "realized_pnl",
                "net_pnl"):
        v = event.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0

def _is_win(event: dict) -> Optional[bool]:
    """
    Return True if event represents a win, False if loss, None if undetermined.
    """
    outcome = event.get("outcome") or event.get("result") or event.get("status")
    if isinstance(outcome, str):
        ol = outcome.lower()
        if ol in ("win", "wins", "won", "profit", "tp"):
            return True
        if ol in ("loss", "lost", "loser", "stop", "sl"):
            return False
    # Fallback: positive pnl = win
    pnl = _safe_pnl_of(event)
    if pnl > 0:
        return True
    if pnl < 0:
        return False
    return None

def _hold_seconds(event: dict) -> Optional[float]:
    """Compute hold duration in seconds. Returns None if can't determine."""
    # Try direct field
    for key in ("hold_seconds", "duration_s", "duration_seconds"):
        v = event.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    # Try open/close timestamps
    open_ts = None
    close_ts = None
    for key in ("opened_at", "open_ts", "buy_ts", "entry_ts"):
        v = event.get(key)
        if v is not None:
            if isinstance(v, (int, float)):
                open_ts = v if v < 7.3e9 else v / 1000.0
            elif isinstance(v, str):
                try:
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    open_ts = dt.timestamp()
                except ValueError:
                    pass
            if open_ts is not None:
                break
    for key in ("closed_at", "close_ts", "sell_ts", "exit_ts"):
        v = event.get(key)
        if v is not None:
            if isinstance(v, (int, float)):
                close_ts = v if v < 7.3e9 else v / 1000.0
            elif isinstance(v, str):
                try:
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    close_ts = dt.timestamp()
                except ValueError:
                    pass
            if close_ts is not None:
                break
    if open_ts is not None and close_ts is not None and close_ts > open_ts:
        return close_ts - open_ts
    return None

# ── Trading category metric computers ──────────────────────────────────────

def compute_pnl(events, start_ts, end_ts) -> float:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    return round(sum(_safe_pnl_of(e) for e in in_window), 6)

def compute_win_rate(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    wins = 0
    losses = 0
    for e in in_window:
        w = _is_win(e)
        if w is True:
            wins += 1
        elif w is False:
            losses += 1
    total = wins + losses
    rate = wins / total if total > 0 else 0.0
    return {"value": round(rate, 4), "wins": wins, "losses": losses,
            "total_decided": total}

def compute_hold_time_median(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    durations = [d for d in (_hold_seconds(e) for e in in_window) if d is not None]
    if not durations:
        return {"value": None, "sample_size": 0}
    durations.sort()
    mid = len(durations) // 2
    if len(durations) % 2 == 0:
        median = (durations[mid - 1] + durations[mid]) / 2
    else:
        median = durations[mid]
    return {"value": round(median, 1), "sample_size": len(durations)}

def compute_hold_time_p95(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    durations = sorted(
        d for d in (_hold_seconds(e) for e in in_window) if d is not None
    )
    if not durations:
        return {"value": None, "sample_size": 0}
    idx = int(0.95 * (len(durations) - 1))
    return {"value": round(durations[idx], 1), "sample_size": len(durations)}

def compute_drawdown_max(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    worst = 0.0
    worst_event = None
    for e in in_window:
        pnl = _safe_pnl_of(e)
        if pnl < worst:
            worst = pnl
            worst_event = e
    detail = ""
    if worst_event:
        detail = (worst_event.get("ticker") or worst_event.get("symbol")
                  or worst_event.get("market") or "")
    return {"value": round(worst, 6), "worst_trade": detail}

def compute_small_buy_ratio(events, start_ts, end_ts,
                            small_threshold: float = 0.1) -> dict:
    """
    Buys where size < threshold / all buys. Threshold defaults to 0.1 SOL.
    """
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    buys = []
    for e in in_window:
        side = (e.get("side") or e.get("action") or "").lower()
        if side in ("buy", "long", "entry") or "buy" in side:
            buys.append(e)
    if not buys:
        return {"value": None, "buys_total": 0, "small_threshold": small_threshold}
    small = 0
    for b in buys:
        for key in ("size", "amount", "size_sol", "amount_sol", "qty"):
            v = b.get(key)
            if isinstance(v, (int, float)) and v < small_threshold:
                small += 1
                break
    ratio = small / len(buys)
    return {"value": round(ratio, 4), "small_count": small,
            "buys_total": len(buys), "small_threshold": small_threshold}

def compute_trade_frequency(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    days = max((end_ts - start_ts) / 86400, 0.01)
    return {"value": round(len(in_window) / days, 2),
            "trades_in_window": len(in_window),
            "window_days": round(days, 2)}

def compute_decline_rate_by_reason(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    counts: dict[str, int] = {}
    for e in in_window:
        reason = (e.get("skip_reason") or e.get("reason") or e.get("decline_reason")
                  or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    total = sum(counts.values())
    return {"value": counts, "total": total}

def compute_decline_drift_Nd(events, start_ts, end_ts, days: int = 2) -> dict:
    """
    Compare decline count this window vs prior equal-length window.
    Returns % change.
    """
    prior_start = start_ts - (end_ts - start_ts)
    prior_end = start_ts
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    prior_window = _filter_events_to_window(events, prior_start, prior_end)
    curr = len(in_window)
    prior = len(prior_window)
    if prior == 0:
        return {"value": None, "current_window": curr, "prior_window": prior,
                "note": "no prior window data"}
    delta_pct = (curr - prior) / prior * 100
    return {"value": round(delta_pct, 1), "current_window": curr,
            "prior_window": prior, "direction": "up" if delta_pct > 0 else "down"}

def compute_signal_to_trade_conversion(events, start_ts, end_ts) -> dict:
    """
    Of events tagged as signals, how many became trades. Heuristic — events
    with side='buy' or 'sell' are trades, others are signals.
    """
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    signals = 0
    trades = 0
    for e in in_window:
        kind = (e.get("type") or e.get("kind") or "").lower()
        side = (e.get("side") or "").lower()
        if side in ("buy", "sell", "long", "short") or kind == "trade":
            trades += 1
        elif kind == "signal" or "signal" in kind:
            signals += 1
    total = signals + trades
    ratio = trades / total if total > 0 else None
    return {"value": ratio, "signals": signals, "trades": trades}

# ── Signal category metric computers ────────────────────────────────────────

def compute_signal_frequency(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    days = max((end_ts - start_ts) / 86400, 0.01)
    return {"value": round(len(in_window) / days, 2),
            "signals_in_window": len(in_window),
            "window_days": round(days, 2)}

def compute_signal_tier_distribution(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    tiers: dict[str, int] = {}
    for e in in_window:
        tier = (e.get("tier") or e.get("priority") or "unknown")
        tier = str(tier).upper()
        tiers[tier] = tiers.get(tier, 0) + 1
    total = sum(tiers.values())
    pct = {k: round(v / total * 100, 1) if total else 0 for k, v in tiers.items()}
    return {"value": pct, "raw_counts": tiers, "total": total}

def compute_confluence_rate(events, start_ts, end_ts) -> dict:
    """
    Approximation: count events with a 'confluence' or 'confluent_with' field set.
    """
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    confluent = 0
    for e in in_window:
        if e.get("confluence") or e.get("confluent_with") or e.get("multi_source"):
            confluent += 1
    total = len(in_window)
    ratio = confluent / total if total else None
    return {"value": ratio, "confluent": confluent, "total": total}

def compute_signal_to_move_correlation(events, start_ts, end_ts) -> dict:
    """
    Stub for Sprint 42 — requires cross-referencing with price candles, which
    is Sprint 42.5 work. Returns placeholder.
    """
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    return {"value": None,
            "signals_in_window": len(in_window),
            "note": "cross-reference with price candles not implemented in v1; Sprint 42.5"}

def compute_per_source_yield(events, start_ts, end_ts) -> dict:
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    sources: dict[str, int] = {}
    for e in in_window:
        source = e.get("source") or e.get("origin") or e.get("provider") or "unknown"
        source = str(source)
        sources[source] = sources.get(source, 0) + 1
    return {"value": sources, "total": sum(sources.values())}

# ── Agent/infra category metric computers ───────────────────────────────────

def compute_heartbeat_uptime(events, start_ts, end_ts) -> dict:
    """
    Approximate uptime: % of expected heartbeat windows where file was fresh.
    Events here are just [{ts: mtime, fresh: True}] from _parse_heartbeat — so
    we measure 'fresh enough' based on whether mtime is within threshold of now.
    For full uptime we'd need historical heartbeat samples; this is a v1 stub.
    """
    if not events:
        return {"value": None, "note": "no heartbeat data"}
    latest = max(e.get("ts", 0) for e in events)
    age_minutes = (time.time() - latest) / 60
    return {"value": "fresh" if age_minutes < 60 else "stale",
            "last_heartbeat_age_min": round(age_minutes, 1)}

def compute_error_rate(events, start_ts, end_ts) -> dict:
    """
    Count error/warn marker lines in events. Events from _parse_freeform_text
    are {line: <text>}. We count lines matching common error markers.
    """
    error_markers = ("ERROR", "EXCEPTION", "Traceback", "FATAL", "err:")
    warn_markers = ("WARN", "WARNING")
    err = 0
    warn = 0
    for e in events:
        line = (e.get("line") or "")
        if any(m in line for m in error_markers):
            err += 1
        elif any(m in line for m in warn_markers):
            warn += 1
    days = max((end_ts - start_ts) / 86400, 0.01)
    return {"value": round(err / days, 2),
            "errors_total": err,
            "warnings_total": warn,
            "window_days": round(days, 2)}

def compute_throughput(events, start_ts, end_ts) -> dict:
    """Generic throughput: events per day."""
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    days = max((end_ts - start_ts) / 86400, 0.01)
    return {"value": round(len(in_window) / days, 2),
            "events_in_window": len(in_window),
            "window_days": round(days, 2)}

def compute_task_completion_rate(events, start_ts, end_ts) -> dict:
    """
    Of task events, how many marked complete vs attempted.
    """
    in_window = _filter_events_to_window(events, start_ts, end_ts)
    completed = 0
    attempted = 0
    for e in in_window:
        status = (e.get("status") or e.get("state") or "").lower()
        if status in ("completed", "complete", "done", "success"):
            completed += 1
            attempted += 1
        elif status in ("failed", "error", "abort", "skipped"):
            attempted += 1
        elif status in ("attempted", "tried"):
            attempted += 1
    rate = completed / attempted if attempted else None
    return {"value": rate, "completed": completed, "attempted": attempted}

METRIC_REGISTRY = {
    # trading
    "pnl":                       compute_pnl,
    "win_rate":                  compute_win_rate,
    "hold_time_median":          compute_hold_time_median,
    "hold_time_p95":             compute_hold_time_p95,
    "drawdown_max":              compute_drawdown_max,
    "small_buy_ratio":           compute_small_buy_ratio,
    "trade_frequency":           compute_trade_frequency,
    "decline_rate_by_reason":    compute_decline_rate_by_reason,
    "decline_drift_2d":          lambda e, s, t: compute_decline_drift_Nd(e, s, t, days=2),
    "signal_to_trade_conversion": compute_signal_to_trade_conversion,
    # signal
    "signal_frequency":          compute_signal_frequency,
    "signal_tier_distribution":  compute_signal_tier_distribution,
    "confluence_rate":           compute_confluence_rate,
    "signal_to_move_correlation": compute_signal_to_move_correlation,
    "per_source_yield":          compute_per_source_yield,
    # agent / infra
    "heartbeat_uptime":          compute_heartbeat_uptime,
    "error_rate":                compute_error_rate,
    "throughput":                compute_throughput,
    "task_completion_rate":      compute_task_completion_rate,
}

# ── Compute metrics for a bot ──────────────────────────────────────────────

def compute_metrics_for_bot(bot_name: str,
                            registry_entry: dict,
                            parsed_outputs: dict[str, list]) -> dict:
    """
    Given parsed events per output, run the configured metric computers across
    the configured time scales. Returns nested dict:
        {output_key: {metric_name: {scale: value}}}
    """
    results: dict[str, dict[str, dict]] = {}
    output_files = registry_entry.get("output_files", {})

    for out_key, events in parsed_outputs.items():
        out_cfg = output_files.get(out_key, {})
        if not out_cfg.get("track"):
            continue
        metrics = out_cfg.get("metrics_from_this", []) or out_cfg.get("metrics", []) or []
        scales = out_cfg.get("time_scales", []) or ["rolling_2d"]

        out_results: dict[str, dict] = {}
        for metric_name in metrics:
            computer = METRIC_REGISTRY.get(metric_name)
            if computer is None:
                out_results[metric_name] = {"error": f"no computer for metric '{metric_name}'"}
                continue
            per_scale: dict = {}
            for scale in scales:
                start_ts, end_ts = _resolve_time_window(scale)
                try:
                    val = computer(events, start_ts, end_ts)
                    per_scale[scale] = val
                except Exception as e:
                    per_scale[scale] = {"error": f"compute err: {e}"}
            out_results[metric_name] = per_scale
        results[out_key] = out_results

    return results

# ── metrics_current.json — the stable consumable layer ────────────────────
# Refreshed every perf-packet cycle. Morning-brief + the agent read
# from here for the bidirectional pull surface.

METRICS_CURRENT_FILE = os.path.join(BOT_LOGS, "metrics_current.json")

def write_metrics_current(registry: dict) -> dict:
    """
    Compute current metrics for every bot with monitoring_active and
    write to metrics_current.json. Returns the written payload.
    """
    payload = {
        "version": "1.0",
        "last_updated": _now_iso(),
        "bots": {},
    }
    for bot_name, entry in registry.get("bots", {}).items():
        curated = entry.get("_curated_by_agent", {})
        if not curated:
            continue  # unconfigured
        if not curated.get("monitoring_active", False):
            continue
        try:
            parsed = collect_outputs_for_bot(bot_name, entry)
            metrics = compute_metrics_for_bot(bot_name, entry, parsed)
            managed = entry.get("_managed_by_warden", {})
            payload["bots"][bot_name] = {
                "category": curated.get("category"),
                "primary_skill": curated.get("primary_skill"),
                "version": managed.get("version", "unknown"),
                "source_hash": managed.get("source_hash", "")[:16],
                "metrics": metrics,
                "computed_at": _now_iso(),
            }
        except Exception as e:
            payload["bots"][bot_name] = {
                "error": f"compute failed: {e}",
                "computed_at": _now_iso(),
            }
            log_tune(f"metrics_current err for {bot_name}: {e}")

    try:
        save_json(METRICS_CURRENT_FILE, payload)
        log_tune(f"metrics_current.json: refreshed ({len(payload['bots'])} bots)")
    except Exception as e:
        log_tune(f"metrics_current write err: {e}")
    return payload

# ── Perf packet writer (per bot, 2-day window) ────────────────────────────

def _format_metric_value(metric_name: str, val: Any) -> str:
    """Format a metric value for markdown display, handling dict/None/numeric."""
    if val is None:
        return "(no data)"
    if isinstance(val, dict):
        if "value" in val:
            inner = val["value"]
            extras = [f"{k}={v}" for k, v in val.items() if k != "value"]
            extra_str = f" ({', '.join(extras)})" if extras else ""
            if isinstance(inner, dict):
                # Nested dict like decline_rate_by_reason → format as inline
                items = ", ".join(f"{k}: {v}" for k, v in inner.items())
                return f"{{{items}}}{extra_str}"
            if isinstance(inner, float):
                if "rate" in metric_name or "ratio" in metric_name:
                    return f"{inner:.1%}{extra_str}"
                return f"{inner:.4f}{extra_str}"
            return f"{inner}{extra_str}"
        return json.dumps(val, default=str)[:200]
    if isinstance(val, float):
        return f"{val:.4f}"
    return str(val)

def write_perf_packet(bot_name: str, registry: dict) -> str:
    """
    Write a 2-day perf packet for a single bot. Pure stats, no LLM narration.
    Returns path of written packet (or empty on failure / skip).
    """
    entry = registry.get("bots", {}).get(bot_name, {})
    curated = entry.get("_curated_by_agent", {})
    if not curated.get("monitoring_active", False):
        log_tune(f"perf_packet: skip '{bot_name}' (monitoring_active=false)")
        return ""

    managed = entry.get("_managed_by_warden", {})
    primary_skill = curated.get("primary_skill")
    category = curated.get("category", "unknown")

    # Compute metrics
    try:
        parsed = collect_outputs_for_bot(bot_name, entry)
        metrics = compute_metrics_for_bot(bot_name, entry, parsed)
    except Exception as e:
        log_tune(f"perf_packet compute err for {bot_name}: {e}")
        return ""

    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    fname = f"code_tuner - perf_review_{bot_name}_{ts}.md"
    out_path = os.path.join(INBOX, fname)

    lines = [
        f"# Performance Review — {bot_name} — 2-day window",
        "",
        f"**Window end:** {_now_iso()}",
        f"**Window:** rolling 48 hours (other scales as configured)",
        f"**Primary skill (for context):** {primary_skill or '(none — no skill associated)'}",
        f"**Bot version:** {managed.get('version', 'unknown')}",
        f"**Source hash:** `{managed.get('source_hash', '')[:16]}`",
        f"**Source lines:** {managed.get('source_lines', 0)}",
        f"**Heartbeat last seen:** {managed.get('last_seen', '(never)')}",
        f"**Category:** {category}",
        "",
    ]

    # Per output, per metric, per scale
    if not metrics:
        lines += [
            "## Metrics",
            "",
            "*(no output files marked `track: true` for this bot)*",
            "",
        ]
    else:
        lines += ["## Metrics", ""]
        for out_key, out_metrics in metrics.items():
            output_files = entry.get("output_files", {})
            path_for_key = output_files.get(out_key, {}).get("path", "(unknown)")
            lines.append(f"### Output: `{out_key}` (`{path_for_key}`)")
            lines.append("")
            for metric_name, per_scale in out_metrics.items():
                lines.append(f"**{metric_name}:**")
                if "error" in per_scale:
                    lines.append(f"- ERROR: {per_scale['error']}")
                else:
                    for scale, val in per_scale.items():
                        formatted = _format_metric_value(metric_name, val)
                        lines.append(f"- *{scale}:* {formatted}")
                lines.append("")
            lines.append("")

    # Source surface (capability summary for reference)
    sd_path = managed.get("self_description_file")
    if sd_path and os.path.exists(sd_path):
        sd = load_json(sd_path, {})
        if isinstance(sd, dict):
            lines += [
                "## Source surface (for context if you're considering a patch)",
                "",
                f"- Tunables: {len(sd.get('tunables', []) or [])}",
                f"- Actions: {len(sd.get('actions', []) or [])}",
                f"- Loops: {len(sd.get('loops', []) or [])}",
                "",
            ]

    # Raw event counts (audit detail)
    lines += ["## Raw event counts (per tracked output)", ""]
    for out_key, events_list in parsed.items() if 'parsed' in locals() else []:
        lines.append(f"- `{out_key}`: {len(events_list)} events parsed total "
                     f"(before window filter)")
    lines.append("")

    lines += [
        "---",
        "",
        "*Observation packet — raw data only, no prescription. You decide whether anything",
        "here warrants action. To request the source for a specific function, write to",
        "`code_tuner_meeting.json` (Stage 4). The agent authors and applies patches in his own",
        "session; anomaly/reload notes go in `inbox\\`. To reset this bot's monitoring config, write `{\"action\": \"reset\",",
        "\"bot\": \"" + bot_name + "\"}` to `code_tuner_setup.json`.*",
    ]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_tune(f"perf packet written: {fname}")
        return out_path
    except Exception as e:
        log_tune(f"perf packet write err: {e}")
        return ""

# ── 30-day deep audit packet ──────────────────────────────────────────────

def write_deep_audit_packet(bot_name: str, registry: dict) -> str:
    """
    30-day deep audit. Same shape as 2-day perf packet but:
      - 30-day window
      - Includes prior-30-day comparison for trending
      - Includes source change history over the window
      - Includes capability surface evolution
    """
    entry = registry.get("bots", {}).get(bot_name, {})
    curated = entry.get("_curated_by_agent", {})
    if not curated.get("monitoring_active", False):
        return ""

    managed = entry.get("_managed_by_warden", {})
    primary_skill = curated.get("primary_skill")
    category = curated.get("category", "unknown")

    # Compute 30-day metrics (and store with `rolling_30d` scale override)
    try:
        parsed = collect_outputs_for_bot(bot_name, entry)
        # Force all scales to rolling_30d + add prior_30d for comparison
        results_30d = {}
        results_prior_30d = {}
        output_files = entry.get("output_files", {})
        now = time.time()
        start_30 = now - 30 * 86400
        start_prior = now - 60 * 86400
        end_prior = now - 30 * 86400

        for out_key, events in parsed.items():
            out_cfg = output_files.get(out_key, {})
            if not out_cfg.get("track"):
                continue
            metrics = out_cfg.get("metrics_from_this", []) or out_cfg.get("metrics", []) or []
            r30: dict = {}
            rp: dict = {}
            for m in metrics:
                computer = METRIC_REGISTRY.get(m)
                if not computer:
                    continue
                try:
                    r30[m] = computer(events, start_30, now)
                    rp[m] = computer(events, start_prior, end_prior)
                except Exception as e:
                    r30[m] = {"error": str(e)}
                    rp[m] = {"error": str(e)}
            results_30d[out_key] = r30
            results_prior_30d[out_key] = rp
    except Exception as e:
        log_tune(f"deep audit compute err for {bot_name}: {e}")
        return ""

    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    fname = f"code_tuner - deep_audit_{bot_name}_{ts}.md"
    out_path = os.path.join(INBOX, fname)

    lines = [
        f"# 30-Day Deep Audit — {bot_name}",
        "",
        f"**Window:** rolling 30 days ending {_now_iso()}",
        f"**Prior window for comparison:** prior 30 days (days 30-60 ago)",
        f"**Primary skill:** {primary_skill or '(none)'}",
        f"**Bot version:** {managed.get('version', 'unknown')}",
        f"**Source hash:** `{managed.get('source_hash', '')[:16]}`",
        f"**Category:** {category}",
        "",
        "## Metrics: 30-day current vs prior 30-day",
        "",
    ]

    for out_key, current_metrics in results_30d.items():
        prior_metrics = results_prior_30d.get(out_key, {})
        output_files = entry.get("output_files", {})
        path_for_key = output_files.get(out_key, {}).get("path", "(unknown)")
        lines.append(f"### Output: `{out_key}` (`{path_for_key}`)")
        lines.append("")
        for metric_name, val in current_metrics.items():
            prior_val = prior_metrics.get(metric_name)
            curr_str = _format_metric_value(metric_name, val)
            prior_str = _format_metric_value(metric_name, prior_val) if prior_val else "(no prior data)"
            lines.append(f"**{metric_name}:**")
            lines.append(f"- *current 30d:* {curr_str}")
            lines.append(f"- *prior 30d:* {prior_str}")
            lines.append("")
        lines.append("")

    # Source change history over the window (Sprint 42.5 expansion)
    lines += [
        "## Source / capability surface notes",
        "",
        f"- Bot version at packet time: {managed.get('version', 'unknown')}",
        f"- Last source modification: {managed.get('last_modified_ts', '(unknown)')}",
        "",
        "*Source diff history over the 30d window is a Sprint 42.5 expansion —",
        "for v1, the source hash above is the current state.*",
        "",
        "---",
        "",
        "*Deep audit packet — raw data only, no prescription. Significant deltas",
        "between current and prior windows are worth investigating but code_tuner",
        "won't tell you which ones matter. That's your call.*",
    ]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_tune(f"deep audit packet written: {fname}")
        return out_path
    except Exception as e:
        log_tune(f"deep audit write err: {e}")
        return ""

# ── Source delta packet writer ────────────────────────────────────────────

def write_source_delta_packet(kind: str, name: str,
                               snapshot_result: dict,
                               registry: dict) -> str:
    """
    Fired when snapshot_loop detects a source change.

    kind: 'bot' or 'skill'
    name: bot_name or skill_name
    snapshot_result: dict with new_hash, old_hash, snapshot_path, lines, etc.
    """
    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    safe_name = name.replace("/", "_").replace("\\", "_")
    fname = f"code_tuner - source_updated_{kind}_{safe_name}_{ts}.md"
    out_path = os.path.join(INBOX, fname)

    old_hash = snapshot_result.get("old_hash", "")
    new_hash = snapshot_result.get("new_hash", "")
    snap_path = snapshot_result.get("snapshot_path", "")
    prev_path = _previous_path(snap_path) if snap_path else ""
    line_count = snapshot_result.get("lines", 0)

    # Try to compute a line-count delta if previous exists
    delta_lines = None
    try:
        if prev_path and os.path.exists(prev_path):
            with open(prev_path, "r", encoding="utf-8-sig") as f:
                prev_count = sum(1 for _ in f)
            delta_lines = line_count - prev_count
    except Exception:
        pass

    # Route header for bot updates
    route_skill = None
    if kind == "bot":
        entry = registry.get("bots", {}).get(name, {})
        curated = entry.get("_curated_by_agent", {})
        route_skill = curated.get("primary_skill")

    lines = [
        f"# Source Updated — {kind}: `{name}`",
        "",
        f"**Detected at:** {_now_iso()}",
        f"**Old hash:** `{old_hash[:16]}...`" if old_hash else "**Old hash:** (no prior snapshot)",
        f"**New hash:** `{new_hash[:16]}...`",
        f"**Current line count:** {line_count}",
    ]
    if delta_lines is not None:
        lines.append(f"**Line delta:** {'+' if delta_lines >= 0 else ''}{delta_lines}")
    if route_skill:
        lines.append(f"**Routes to skill:** {route_skill}")
    lines.append("")

    # Capability surface comparison (for bots only, if self_description available)
    if kind == "bot":
        managed = registry.get("bots", {}).get(name, {}).get("_managed_by_warden", {})
        sd_path = managed.get("self_description_file")
        if sd_path and os.path.exists(sd_path):
            sd = load_json(sd_path, {})
            if isinstance(sd, dict):
                lines += [
                    "## Capability surface (current — self_description)",
                    "",
                    f"- Tunables: {len(sd.get('tunables', []) or [])}",
                    f"- Actions: {len(sd.get('actions', []) or [])}",
                    f"- Loops: {len(sd.get('loops', []) or [])}",
                    "",
                ]

    lines += [
        "## What you might want to do",
        "",
        "- Review the diff: previous snapshot at `" + (prev_path or "(none)") + "`",
        "- If you authored this change, no action needed",
        "- If this was unexpected, check git log or ask the owner",
        "- If the change adds new tunables / metrics you want tracked, reset this bot's",
        "  config via `code_tuner_setup.json` to re-enter setup mode",
        "",
        "---",
        "",
        f"*Auto-fired when snapshot detected hash change. {kind.capitalize()} version tracking",
        "is part of Sprint 42 source monitoring.*",
    ]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_tune(f"source delta packet written: {fname}")
        return out_path
    except Exception as e:
        log_tune(f"source delta packet write err: {e}")
        return ""

# ── Stage 3 loop schedulers ────────────────────────────────────────────────
# Perf packets fire every 48h, staggered so we don't blast all bots in one minute.
# Implementation: pick N bots per cycle, where N is sized so we cycle through
# all monitored bots over ~48h. With ~16 bots and a 6h cycle, N = ~2 bots/cycle.
#
# Deep audits fire every 30d per bot. Same staggering principle.
#
# metrics_current.json refreshes every 30 min (with snapshot_loop cadence)
# so morning-brief always has fresh-ish data when it pulls.

PERF_PACKET_CYCLE_S = int(_envv("CODE_TUNER_PERF_CYCLE_S", str(48 * 3600)))
PERF_PACKET_FILE_STATE = os.path.join(BOT_LOGS, "code_tuner_perf_state.json")
DEEP_AUDIT_FILE_STATE = os.path.join(BOT_LOGS, "code_tuner_deep_audit_state.json")
METRICS_REFRESH_INTERVAL_S = int(_envv("CODE_TUNER_METRICS_REFRESH_S", "1800"))  # 30 min

def _load_perf_state() -> dict:
    return load_json(PERF_PACKET_FILE_STATE, default={"last_fired": {}})

def _save_perf_state(state: dict):
    save_json(PERF_PACKET_FILE_STATE, state)

def _load_deep_audit_state() -> dict:
    return load_json(DEEP_AUDIT_FILE_STATE, default={"last_fired": {}})

def _save_deep_audit_state(state: dict):
    save_json(DEEP_AUDIT_FILE_STATE, state)

async def perf_packet_loop():
    """
    Every 6h, scan registry for bots whose 2-day perf cycle is due.
    Fire perf packets for bots where (now - last_fired) >= PERF_PACKET_CYCLE_S.
    """
    log_tune(f"perf_packet_loop: starting (cycle target: {PERF_PACKET_CYCLE_S}s per bot)")
    # Initial delay so we don't fire on first boot before metrics warmed
    await asyncio.sleep(60)

    while True:
        try:
            registry = load_json(BOTS_LIST_FILE, default={"bots": {}})
            state = _load_perf_state()
            now = time.time()
            fired = 0

            for bot_name, entry in registry.get("bots", {}).items():
                curated = entry.get("_curated_by_agent", {})
                if not curated.get("monitoring_active", False):
                    continue
                last = state["last_fired"].get(bot_name, 0)
                if (now - last) < PERF_PACKET_CYCLE_S:
                    continue
                out_path = write_perf_packet(bot_name, registry)
                if out_path:
                    state["last_fired"][bot_name] = now
                    fired += 1

            if fired > 0:
                _save_perf_state(state)
                log_tune(f"perf_packet_loop: fired {fired} packets this cycle")

            write_heartbeat("perf_packet_loop", {"last_check": _now_iso(),
                                                 "packets_this_cycle": fired})
        except Exception as e:
            log_tune(f"perf_packet_loop err: {e}")
            write_heartbeat("perf_packet_loop", {"error": str(e)[:200]})
        # Check every 6h — fast enough that even with 19 bots we hit each
        # within ~6h of its 48h cycle anniversary
        await asyncio.sleep(6 * 3600)

async def deep_audit_loop():
    """
    Every 24h, scan registry for bots whose 30-day deep audit is due.
    Fire deep audit packets for bots where (now - last_fired) >= 30 days.
    """
    log_tune("deep_audit_loop: starting (30-day cycle per bot)")
    # Initial delay
    await asyncio.sleep(300)

    while True:
        try:
            registry = load_json(BOTS_LIST_FILE, default={"bots": {}})
            state = _load_deep_audit_state()
            now = time.time()
            fired = 0
            THIRTY_DAYS = 30 * 86400

            for bot_name, entry in registry.get("bots", {}).items():
                curated = entry.get("_curated_by_agent", {})
                if not curated.get("monitoring_active", False):
                    continue
                last = state["last_fired"].get(bot_name, 0)
                if (now - last) < THIRTY_DAYS:
                    continue
                out_path = write_deep_audit_packet(bot_name, registry)
                if out_path:
                    state["last_fired"][bot_name] = now
                    fired += 1

            if fired > 0:
                _save_deep_audit_state(state)
                log_tune(f"deep_audit_loop: fired {fired} packets this cycle")

            write_heartbeat("deep_audit_loop", {"last_check": _now_iso(),
                                                 "packets_this_cycle": fired})
        except Exception as e:
            log_tune(f"deep_audit_loop err: {e}")
        await asyncio.sleep(24 * 3600)

async def metrics_current_refresh_loop():
    """
    Every 30 min, refresh metrics_current.json so morning-brief and
    the agent have fresh data to pull on demand.
    """
    log_tune(f"metrics_current_refresh_loop: starting (every {METRICS_REFRESH_INTERVAL_S}s)")
    await asyncio.sleep(30)  # initial delay

    while True:
        try:
            registry = load_json(BOTS_LIST_FILE, default={"bots": {}})
            payload = write_metrics_current(registry)
            write_heartbeat("metrics_current_refresh",
                            {"bots_with_metrics": len(payload.get("bots", {}))})
        except Exception as e:
            log_tune(f"metrics_current_refresh err: {e}")
        await asyncio.sleep(METRICS_REFRESH_INTERVAL_S)

# ════════════════════════════════════════════════════════════════════════════
#                  STAGE 4 — meeting_watcher + code-area search
# ════════════════════════════════════════════════════════════════════════════
# Local Ollama interprets the owner's (or the agent's) casual directives written to
# bots/json_relay/code_tuner_meeting.json. The LLM is for INTENT PARSING only,
# not for deciding what data is relevant. Data relevance was decided by the agent
# during setup mode. The LLM just translates casual prose into a structured
# action (e.g., "audit perps for the last week" → {intent: trigger_perf,
# target_bot: perps_bot, window: 7d}).
#
# Supported directive types:
#   1. show_source        — fetch a code section from a bot
#   2. trigger_perf       — fire a 2-day perf packet immediately
#   3. trigger_audit      — fire a 30-day deep audit immediately
#   4. reset              — clear _curated_by_agent for a bot (re-enter setup)
#   5. pause              — set monitoring_active: false
#   6. resume             — set monitoring_active: true
#   7. add_metric         — add a metric to a bot's output config
#
# Gap classifier (Sprint 41 pattern):
#   - hard_locked: directive targets something forbidden (e.g., "delete X's
#     heartbeat") → status=failed
#   - capability_gap: directive needs a new parser/metric that doesn't exist
#     → record it for the agent. The agent applies his own patch. This bot does not.
#   - ambiguous: low LLM confidence → status=failed, ask for clarification

OLLAMA_HOST          = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
# Local Ollama classifier only (optional; skipped when Ollama is not running).
OLLAMA_CLASSIFIER_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:14b-q4_K_M")
OLLAMA_TIMEOUT_S     = int(_envv("CODE_TUNER_OLLAMA_TIMEOUT_S", "60"))

MEETING_POLL_INTERVAL_S = int(_envv("CODE_TUNER_MEETING_POLL_S", "30"))

# Directive intents — the LLM picks one of these
VALID_INTENTS = {
    "show_source", "trigger_perf", "trigger_audit",
    "reset", "pause", "resume", "add_metric",
}

# Hard-locked terms — directives mentioning these are refused outright
HARD_LOCKED_TERMS = [
    "delete heartbeat", "remove heartbeat",
    "delete bot", "remove bot",  # use reset, not delete
    "wipe registry", "wipe bots_list",
    "drop snapshots", "delete snapshots",
    "private_key", "wallet_key", "execute_buy", "execute_sell",
    "send_sol", "LEDGER_ADDRESS",
]

# ── Ollama call ───────────────────────────────────────────────────────────

async def _ollama_call(model: str, system_prompt: str, user_prompt: str,
                       json_format: bool = True) -> tuple[bool, str]:
    """
    Single Ollama call. Returns (success, response_text_or_err).
    """
    url = f"{OLLAMA_HOST}/api/chat"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 512,
        },
    }
    if json_format:
        body["format"] = "json"

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_S) as client:
            r = await client.post(url, json=body)
            if r.status_code != 200:
                return False, f"ollama HTTP {r.status_code}: {r.text[:200]}"
            data = r.json()
            content = data.get("message", {}).get("content", "")
            if not content:
                return False, "ollama returned empty content"
            return True, content
    except Exception as e:
        return False, f"ollama call err: {e}"

async def _ollama_call_with_fallback(system_prompt: str,
                                      user_prompt: str) -> tuple[bool, str, str]:
    """
    Local classifier only: the local Ollama model (OLLAMA_MODEL). No second model and no cloud model.
    Returns (success, response_text, model_used).
    """
    if not _si_gate.allows("ollama"):  # LLM_MAIN / LLM_FALLBACK do not list ollama
        return False, "si_off: ollama not selected in LLM_MAIN/LLM_FALLBACK", ""
    ok, out = await _ollama_call(OLLAMA_CLASSIFIER_MODEL, system_prompt, user_prompt)
    if ok:
        return True, out, OLLAMA_CLASSIFIER_MODEL
    log_tune(f"ollama classifier ({OLLAMA_CLASSIFIER_MODEL}) failed: {out[:120]}")
    return False, out, ""

# ── Directive interpretation ──────────────────────────────────────────────

def _build_intent_prompt(directive: str, registry: dict) -> str:
    """Build the LLM prompt for intent classification."""
    bot_names = sorted(registry.get("bots", {}).keys())
    bot_list = ", ".join(bot_names) if bot_names else "(no bots in registry yet)"

    return f"""You are code_tuner's directive parser. The user (The owner or the agent) wrote a
casual prose directive about system bot monitoring. Your job is to classify
it into ONE structured intent.

User directive:
"{directive}"

Available bots (registry):
{bot_list}

Valid intents:
- show_source: user wants to see a code section from a bot.
    Required: target_bot. Optional: target_function (name), target_lines (e.g. "412-580"),
    target_description (a phrase like "the volume threshold logic")
- trigger_perf: user wants a fresh 2-day perf packet for a bot.
    Required: target_bot
- trigger_audit: user wants a fresh 30-day deep audit for a bot.
    Required: target_bot
- reset: user wants to clear a bot's monitoring config (re-enter setup).
    Required: target_bot
- pause: user wants to stop monitoring a bot (monitoring_active=false).
    Required: target_bot
- resume: user wants to re-enable monitoring for a bot.
    Required: target_bot
- add_metric: user wants to add a metric to a bot's tracked outputs.
    Required: target_bot. Optional: target_metric (name), target_output (key in output_files)

Respond with ONLY a JSON object, no prose, no preamble:
{{
  "intent": "<one of valid intents>",
  "target_bot": "<bot name from registry>",
  "target_function": "<function name or null>",
  "target_lines": "<line range like '412-580' or null>",
  "target_description": "<phrase the user used to refer to a code area, or null>",
  "target_metric": "<metric name or null>",
  "target_output": "<output_key name or null>",
  "confidence": <float 0-1>
}}

If the directive is ambiguous, requests something forbidden, or you can't
match a bot name, respond:
{{
  "intent": "unclear",
  "reason": "<one short sentence>",
  "confidence": <low float>
}}
"""

async def interpret_directive(directive: str, registry: dict) -> dict:
    """
    Classify a casual directive into a structured intent. Returns:
    {
      status: 'applied' | 'failed' | 'recorded' | 'clarification_needed',
      intent: '<one of VALID_INTENTS or unclear>',
      target_bot: '...',
      ...other fields per intent...,
      confidence: float,
      interpreted_by: model name,
      raw_llm_output: str (only if parse error)
    }
    """
    # Hard-locked term check (refuse before LLM)
    directive_lower = directive.lower()
    for term in HARD_LOCKED_TERMS:
        if term.lower() in directive_lower:
            return {
                "status": "failed",
                "intent": "hard_locked",
                "error": f"directive contains hard-locked term: '{term}'",
                "gap_classification": "hard_locked",
                "confidence": 1.0,
                "interpreted_by": "static_filter",
            }

    system_prompt = (
        "You are a strict JSON-emitting directive classifier. You MUST respond "
        "with valid JSON only. No prose, no backticks, no preamble. Single object."
    )
    user_prompt = _build_intent_prompt(directive, registry)

    ok, raw, model_used = await _ollama_call_with_fallback(system_prompt, user_prompt)
    if not ok:
        return {
            "status": "failed",
            "intent": "unclear",
            "error": f"all ollama models failed: {raw}",
            "confidence": 0.0,
            "interpreted_by": "none",
        }

    # Try to parse JSON from output
    parsed = None
    # Strip code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Look for the largest JSON-looking substring
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

    if not isinstance(parsed, dict):
        return {
            "status": "failed",
            "intent": "unclear",
            "error": "LLM output not parseable as JSON",
            "raw_llm_output": raw[:500],
            "confidence": 0.0,
            "interpreted_by": model_used,
        }

    intent = parsed.get("intent")
    confidence = parsed.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)):
        confidence = 0.0

    # Unclear or low confidence → clarification
    if intent == "unclear" or confidence < 0.4:
        return {
            "status": "clarification_needed",
            "intent": "unclear",
            "reason": parsed.get("reason", "low confidence or ambiguous"),
            "confidence": confidence,
            "interpreted_by": model_used,
        }

    if intent not in VALID_INTENTS:
        return {
            "status": "failed",
            "intent": "unclear",
            "error": f"LLM picked unknown intent: {intent}",
            "confidence": confidence,
            "interpreted_by": model_used,
        }

    # Validate target_bot if required
    target_bot = parsed.get("target_bot")
    if intent in VALID_INTENTS and not target_bot:
        return {
            "status": "clarification_needed",
            "intent": intent,
            "reason": "missing target_bot in classified intent",
            "confidence": confidence,
            "interpreted_by": model_used,
        }
    if target_bot and target_bot not in registry.get("bots", {}):
        return {
            "status": "clarification_needed",
            "intent": intent,
            "reason": f"target_bot '{target_bot}' not in registry. Available: "
                      f"{sorted(registry.get('bots', {}).keys())}",
            "confidence": confidence,
            "interpreted_by": model_used,
        }

    return {
        "status": "applied",
        "intent": intent,
        "target_bot": target_bot,
        "target_function": parsed.get("target_function"),
        "target_lines": parsed.get("target_lines"),
        "target_description": parsed.get("target_description"),
        "target_metric": parsed.get("target_metric"),
        "target_output": parsed.get("target_output"),
        "confidence": confidence,
        "interpreted_by": model_used,
    }

# ── Code-area search (three flavors) ──────────────────────────────────────

def _read_bot_source(bot_name: str, registry: dict) -> tuple[str, list[str]]:
    """
    Read bot source from snapshot (preferred, stable) or directly from disk.
    Returns (source_text, source_lines).
    """
    entry = registry.get("bots", {}).get(bot_name, {})
    managed = entry.get("_managed_by_warden", {})
    snap_path = managed.get("snapshot_path", "")

    # Prefer snapshot (it's the version we tracked when packet fired)
    if snap_path and os.path.exists(snap_path):
        path = snap_path
    else:
        path = managed.get("path", "")
    if not path or not os.path.exists(path):
        return "", []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
        return text, text.splitlines()
    except Exception as e:
        log_tune(f"_read_bot_source err on {path}: {e}")
        return "", []

def find_function_by_name(bot_name: str, function_name: str,
                          registry: dict) -> Optional[dict]:
    """
    Flavor (a): find a function by exact name via AST.
    Returns {name, type, start_line, end_line, source_text, source_path} or None.
    """
    text, _lines = _read_bot_source(bot_name, registry)
    if not text:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        log_tune(f"find_function: bot source unparseable: {e}")
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == function_name:
                kind = "class" if isinstance(node, ast.ClassDef) else (
                    "async_function" if isinstance(node, ast.AsyncFunctionDef)
                    else "function"
                )
                end_line = getattr(node, "end_lineno", node.lineno)
                # Extract source from the line range
                src_lines = text.splitlines()
                start_idx = max(node.lineno - 1, 0)
                end_idx = min(end_line, len(src_lines))
                section = "\n".join(src_lines[start_idx:end_idx])
                managed = registry.get("bots", {}).get(bot_name, {}).get(
                    "_managed_by_warden", {})
                return {
                    "name": node.name,
                    "type": kind,
                    "start_line": node.lineno,
                    "end_line": end_line,
                    "source_text": section,
                    "source_path": managed.get("snapshot_path", managed.get("path", "")),
                }
    return None

def find_by_line_range(bot_name: str, line_range: str,
                       registry: dict) -> Optional[dict]:
    """
    Flavor (c): return source for a line range (e.g., '412-580').
    Returns {start_line, end_line, source_text, source_path} or None.
    """
    text, lines = _read_bot_source(bot_name, registry)
    if not text:
        return None
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", line_range or "")
    if not m:
        return None
    start = max(int(m.group(1)), 1)
    end = min(int(m.group(2)), len(lines))
    if end < start:
        return None
    section = "\n".join(lines[start - 1:end])
    managed = registry.get("bots", {}).get(bot_name, {}).get("_managed_by_warden", {})
    return {
        "start_line": start,
        "end_line": end,
        "source_text": section,
        "source_path": managed.get("snapshot_path", managed.get("path", "")),
    }

async def find_by_description(bot_name: str, description: str,
                               registry: dict) -> Optional[dict]:
    """
    Flavor (b): LLM interprets the description to identify relevant identifiers,
    then we grep for them and return the matching function block(s).

    Returns a dict with `matches` array (function-shaped entries) and the
    identifiers the LLM picked.
    """
    text, lines = _read_bot_source(bot_name, registry)
    if not text:
        return None

    # Build a function-name list to bias the LLM
    toc = _extract_source_toc(
        registry.get("bots", {}).get(bot_name, {}).get(
            "_managed_by_warden", {}).get("snapshot_path", "")
        or registry.get("bots", {}).get(bot_name, {}).get(
            "_managed_by_warden", {}).get("path", "")
    )
    func_names = [t["name"] for t in toc if t["type"] in
                  ("function", "async_function")]

    func_list_str = ", ".join(func_names[:60]) if func_names else "(none)"

    system_prompt = (
        "You are an identifier-extraction helper. Given a code area description, "
        "return JSON listing the most relevant function names and config constant "
        "names to look at. Respond with JSON only."
    )
    user_prompt = f"""User asked about: "{description}"
Bot: {bot_name}

Available function names in this bot:
{func_list_str}

Respond with JSON only:
{{
  "function_names": ["<from list above, most relevant first, max 3>"],
  "config_constants": ["<UPPER_CASE_CONSTANT names that might match the topic, max 5>"],
  "search_terms": ["<lowercase keywords to grep for if no functions match>"]
}}
"""
    ok, raw, model_used = await _ollama_call_with_fallback(system_prompt, user_prompt)
    if not ok:
        return None

    parsed = None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    if not isinstance(parsed, dict):
        return None

    function_names = parsed.get("function_names", []) or []
    constants = parsed.get("config_constants", []) or []
    search_terms = parsed.get("search_terms", []) or []

    # Resolve picked function names to source sections
    matches = []
    for fname in function_names[:3]:
        if not isinstance(fname, str):
            continue
        section = find_function_by_name(bot_name, fname, registry)
        if section:
            matches.append(section)

    # If no function matches, grep for constants + search terms
    constant_locations: list[dict] = []
    if not matches and (constants or search_terms):
        all_terms = (constants or []) + (search_terms or [])
        for i, line in enumerate(lines, 1):
            for term in all_terms:
                if not isinstance(term, str) or not term:
                    continue
                if term in line:
                    constant_locations.append({
                        "term": term,
                        "line": i,
                        "text": line.strip()[:200],
                    })
                    if len(constant_locations) >= 20:
                        break
            if len(constant_locations) >= 20:
                break

    return {
        "matches": matches,
        "constant_locations": constant_locations[:20],
        "function_names_picked": function_names,
        "constants_picked": constants,
        "search_terms_picked": search_terms,
        "interpreted_by": model_used,
    }

# ── Source-fetch response packet writer ───────────────────────────────────

def write_source_fetch_packet(bot_name: str, query: dict, result: dict) -> str:
    """
    Write a P3 packet returning a source section that agent asked for via
    meeting_watcher. Returns path of written packet.

    query: {'function': str, 'lines': str, 'description': str} — whichever
           flavor was used
    result: from find_function_by_name / find_by_line_range / find_by_description
    """
    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    fname = f"code_tuner - source_fetch_{bot_name}_{ts}.md"
    out_path = os.path.join(INBOX, fname)

    flavor = query.get("flavor", "unknown")

    lines = [
        f"# Source — {bot_name}.py",
        "",
        f"**Requested via:** meeting_watcher casual directive",
        f"**Flavor:** {flavor}",
        f"**Time:** {_now_iso()}",
        "",
    ]

    if flavor == "function":
        if result is None:
            lines += [
                f"## Function not found",
                "",
                f"No function matching `{query.get('function')}` in {bot_name}.",
                "Check the function name (use `show me {bot}'s functions` to list them).",
            ]
        else:
            lines += [
                f"## Function: `{result['name']}` ({result['type']})",
                "",
                f"**Lines:** {result['start_line']}-{result['end_line']}",
                f"**Source path:** `{result['source_path']}`",
                "",
                "```python",
                result["source_text"],
                "```",
            ]

    elif flavor == "lines":
        if result is None:
            lines += [
                f"## Line range not resolvable",
                "",
                f"Could not extract lines `{query.get('lines')}` from {bot_name}. "
                "Check the format (e.g., `412-580`).",
            ]
        else:
            lines += [
                f"## Lines {result['start_line']}-{result['end_line']}",
                "",
                f"**Source path:** `{result['source_path']}`",
                "",
                "```python",
                result["source_text"],
                "```",
            ]

    elif flavor == "description":
        if result is None:
            lines += [
                f"## Description-based search failed",
                "",
                f"Could not resolve description `{query.get('description')}` to source. "
                "Try a more specific phrase or use function-name lookup.",
            ]
        else:
            picks = result.get("function_names_picked", [])
            consts = result.get("constants_picked", [])
            terms = result.get("search_terms_picked", [])
            interpreted_by = result.get("interpreted_by", "?")
            lines += [
                f"## Description: \"{query.get('description', '')}\"",
                "",
                f"**LLM picked function names:** {', '.join(picks) if picks else '(none)'}",
                f"**LLM picked constants:** {', '.join(consts) if consts else '(none)'}",
                f"**LLM picked search terms:** {', '.join(terms) if terms else '(none)'}",
                f"**Interpreter:** {interpreted_by}",
                "",
            ]
            matches = result.get("matches", [])
            if matches:
                lines.append(f"### Matched functions ({len(matches)})")
                lines.append("")
                for m in matches:
                    lines.append(f"#### `{m['name']}` (lines {m['start_line']}-{m['end_line']})")
                    lines.append("")
                    lines.append("```python")
                    lines.append(m["source_text"])
                    lines.append("```")
                    lines.append("")

            const_locs = result.get("constant_locations", [])
            if const_locs and not matches:
                lines.append("### Constant / term locations")
                lines.append("")
                lines.append("| Line | Term | Context |")
                lines.append("|------|------|---------|")
                for loc in const_locs:
                    ctx = loc["text"].replace("|", "\\|")
                    lines.append(f"| {loc['line']} | `{loc['term']}` | `{ctx}` |")
                lines.append("")
                lines.append(
                    "*No function matched directly — these are lines where the LLM-picked "
                    "constants or search terms appear. Use `lines:N-M` to fetch a specific area.*"
                )
                lines.append("")

    lines += [
        "",
        "---",
        "",
        "*Need a different section? Write a follow-up directive to "
        "`bots/json_relay/code_tuner_meeting.json`. Examples: "
        "`show me <bot>'s <function_name>`, `show me <bot> lines 412-580`, "
        "`show me <bot>'s rug check logic`.*",
    ]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_tune(f"source fetch packet written: {fname}")
        return out_path
    except Exception as e:
        log_tune(f"source fetch packet write err: {e}")
        return ""

# ── Directive handlers ────────────────────────────────────────────────────

async def _handle_show_source(interp: dict, registry: dict) -> str:
    """Dispatch show_source to one of the three flavors."""
    bot = interp["target_bot"]
    function_name = interp.get("target_function")
    line_range = interp.get("target_lines")
    description = interp.get("target_description")

    # Precedence: function > lines > description
    if function_name:
        result = find_function_by_name(bot, function_name, registry)
        return write_source_fetch_packet(
            bot, {"flavor": "function", "function": function_name}, result
        )
    if line_range:
        result = find_by_line_range(bot, line_range, registry)
        return write_source_fetch_packet(
            bot, {"flavor": "lines", "lines": line_range}, result
        )
    if description:
        result = await find_by_description(bot, description, registry)
        return write_source_fetch_packet(
            bot, {"flavor": "description", "description": description}, result
        )
    # Nothing specific given — return the full ToC as a fallback
    bot_path = registry.get("bots", {}).get(bot, {}).get(
        "_managed_by_warden", {}).get("snapshot_path") or registry.get(
        "bots", {}).get(bot, {}).get("_managed_by_warden", {}).get("path", "")
    toc = _extract_source_toc(bot_path)
    fake_result = {
        "function_names_picked": [t["name"] for t in toc[:30]],
        "constants_picked": [],
        "search_terms_picked": [],
        "matches": [],
        "constant_locations": [],
        "interpreted_by": "fallback_toc",
    }
    return write_source_fetch_packet(
        bot, {"flavor": "description",
              "description": "(no specific area — returning function ToC)"},
        fake_result,
    )

async def _handle_trigger_perf(interp: dict, registry: dict) -> str:
    """Fire a fresh perf packet immediately, bypassing the 48h cycle."""
    bot = interp["target_bot"]
    return write_perf_packet(bot, registry)

async def _handle_trigger_audit(interp: dict, registry: dict) -> str:
    """Fire a fresh deep audit immediately."""
    bot = interp["target_bot"]
    return write_deep_audit_packet(bot, registry)

async def _handle_reset(interp: dict, registry: dict) -> str:
    """Reset a bot's monitoring config — same as setup_response action='reset'."""
    bot = interp["target_bot"]
    fresh = load_json(BOTS_LIST_FILE, default={"bots": {}})
    status = _apply_reset_action({"bot": bot}, fresh)
    log_tune(f"meeting: {status}")
    return status

async def _handle_pause(interp: dict, registry: dict) -> str:
    """Set monitoring_active: false for a bot."""
    bot = interp["target_bot"]
    fresh = load_json(BOTS_LIST_FILE, default={"bots": {}})
    entry = fresh.get("bots", {}).get(bot)
    if not entry:
        return f"bot '{bot}' not in registry"
    curated = entry.get("_curated_by_agent", {})
    if not curated:
        # Treat as skip with reason
        return _apply_skip_action({"bot": bot, "reason": "paused via meeting_watcher"},
                                  fresh)
    curated["monitoring_active"] = False
    curated["paused_at"] = _now_iso()
    save_json(BOTS_LIST_FILE, fresh)
    log_tune(f"meeting: paused monitoring for '{bot}'")
    return f"paused '{bot}': monitoring_active=false"

async def _handle_resume(interp: dict, registry: dict) -> str:
    """Set monitoring_active: true for a bot."""
    bot = interp["target_bot"]
    fresh = load_json(BOTS_LIST_FILE, default={"bots": {}})
    entry = fresh.get("bots", {}).get(bot)
    if not entry:
        return f"bot '{bot}' not in registry"
    curated = entry.get("_curated_by_agent", {})
    if not curated:
        return f"bot '{bot}' has no curated config yet — use setup mode first"
    curated["monitoring_active"] = True
    curated.pop("paused_at", None)
    curated["resumed_at"] = _now_iso()
    save_json(BOTS_LIST_FILE, fresh)
    log_tune(f"meeting: resumed monitoring for '{bot}'")
    return f"resumed '{bot}': monitoring_active=true"

async def _handle_add_metric(interp: dict, registry: dict) -> str:
    """
    Add a metric to a bot's output config. Validates against the bot's
    category catalog. If the metric doesn't exist in the catalog, routes
    as a capability_gap to code_requests for the agent.
    """
    bot = interp["target_bot"]
    metric = interp.get("target_metric")
    output_key = interp.get("target_output")
    if not metric:
        return f"add_metric for '{bot}' missing target_metric"

    fresh = load_json(BOTS_LIST_FILE, default={"bots": {}})
    entry = fresh.get("bots", {}).get(bot)
    if not entry:
        return f"bot '{bot}' not in registry"
    curated = entry.get("_curated_by_agent", {})
    category = curated.get("category")
    if not category:
        return f"bot '{bot}' has no category set — use setup mode first"

    # Validate metric against category catalog
    cat_metrics = {m[0] for m in CATEGORY_METRIC_CATALOG.get(category, [])}
    if metric not in cat_metrics:
        # Capability gap — the agent applies his own patch; do not edit code here
        log_tune(f"meeting: capability_gap — metric '{metric}' not in {category} catalog")
        return (f"capability_gap: metric '{metric}' not in '{category}' catalog. "
                f"Available: {sorted(cat_metrics)}. To add new metrics, write a "
                f"code_request describing the new metric computer.")

    # Resolve target_output — if not given, pick the first tracked output
    outputs = entry.get("output_files", {})
    if output_key and output_key in outputs:
        out_cfg = outputs[output_key]
    else:
        tracked = [k for k, v in outputs.items() if v.get("track")]
        if not tracked:
            return f"bot '{bot}' has no tracked outputs to add metric to"
        output_key = tracked[0]
        out_cfg = outputs[output_key]

    existing = out_cfg.get("metrics_from_this", []) or out_cfg.get("metrics", []) or []
    if metric in existing:
        return f"metric '{metric}' already in {bot}.{output_key}"
    existing.append(metric)
    out_cfg["metrics_from_this"] = existing
    save_json(BOTS_LIST_FILE, fresh)
    log_tune(f"meeting: added metric '{metric}' to {bot}.{output_key}")
    return f"added '{metric}' to {bot}.{output_key}.metrics_from_this"

# ── Meeting response writer ───────────────────────────────────────────────

def _write_meeting_response_packet(directive: str, interp: dict,
                                    handler_status: str = "",
                                    output_path: str = "") -> str:
    """
    Write a P2 response packet to the agent's inbox summarizing what the
    meeting_watcher did. Source-fetch flavor already wrote its own packet;
    this is the meta-response.
    """
    os.makedirs(INBOX, exist_ok=True)
    ts = _short_ts()
    fname = f"code_tuner - meeting_response_{ts}.md"
    out_path = os.path.join(INBOX, fname)

    status = interp.get("status", "failed")
    intent = interp.get("intent", "unclear")
    target_bot = interp.get("target_bot", "?")
    confidence = interp.get("confidence", 0)
    interpreted_by = interp.get("interpreted_by", "?")

    lines = [
        "# code_tuner Meeting Response",
        "",
        f"**Time:** {_now_iso()}",
        f"**Status:** {status}",
        f"**Intent:** {intent}",
        f"**Confidence:** {confidence:.2f}",
        f"**Interpreter:** {interpreted_by}",
        "",
        "## Original directive",
        "",
        f"> {directive}",
        "",
    ]

    if status == "applied":
        lines += [
            "## Action taken",
            "",
            f"- **Intent:** `{intent}`",
            f"- **Target bot:** `{target_bot}`",
        ]
        for k in ("target_function", "target_lines", "target_description",
                  "target_metric", "target_output"):
            v = interp.get(k)
            if v:
                lines.append(f"- **{k}:** `{v}`")
        if handler_status:
            lines.append("")
            lines.append(f"**Handler result:** {handler_status}")
        if output_path:
            lines.append(f"**Output packet:** `{os.path.basename(output_path)}`")

    elif status == "clarification_needed":
        lines += [
            "## Clarification needed",
            "",
            f"Reason: {interp.get('reason', '(none given)')}",
            "",
            "Write a more specific directive to `bots/json_relay/code_tuner_meeting.json`.",
            "Include the bot name explicitly if you didn't.",
        ]

    elif status == "recorded":
        lines += [
            "## Recorded, registry not changed",
            "",
            interp.get("error") or (
                "Local classifier does not reset, pause, resume, or add_metric. "
                "The agent applies his own patch."
            ),
            "",
            "To change registry state, put the action in the meeting file yourself",
            "(`intent` or `action`, plus `bot`). A prose note classified locally is only recorded.",
        ]

    elif status == "failed":
        gap_class = interp.get("gap_classification", "unclear")
        lines += [
            "## Failed",
            "",
            f"- **Gap classification:** {gap_class}",
            f"- **Error:** {interp.get('error', '(none)')}",
        ]
        if "raw_llm_output" in interp:
            lines.append("")
            lines.append("**Raw LLM output (first 500 chars):**")
            lines.append("")
            lines.append("```")
            lines.append(interp["raw_llm_output"])
            lines.append("```")
        if gap_class == "capability_gap":
            lines += [
                "",
                "**Capability gap detected.** The agent applies his own patch. This bot",
                "does not edit other bots' .py and does not auto-deploy.",
            ]

    lines += [
        "",
        "---",
        "",
        "*The meeting file at `bots/json_relay/code_tuner_meeting.json` has been deleted. ",
        "Write a fresh meeting note to send another directive.*",
    ]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log_tune(f"meeting response written: {fname}")
        return out_path
    except Exception as e:
        log_tune(f"meeting response write err: {e}")
        return ""

# ── meeting_watcher_loop ──────────────────────────────────────────────────


_REGISTRY_MUTATION_INTENTS = {"reset", "pause", "resume", "add_metric"}


def _agent_authored_registry_action(raw: dict):
    """
    Registry mutation is allowed only when the meeting file already carries
    the action the agent wrote (intent or action, plus bot). A local-model
    classification is not that result.
    """
    if not isinstance(raw, dict):
        return None
    intent = raw.get("intent") if raw.get("intent") in _REGISTRY_MUTATION_INTENTS else None
    if intent is None and raw.get("action") in _REGISTRY_MUTATION_INTENTS:
        intent = raw.get("action")
    if intent is None:
        return None
    bot = raw.get("target_bot") or raw.get("bot")
    if not isinstance(bot, str) or not bot.strip():
        return None
    return {
        "status": "applied",
        "intent": intent,
        "target_bot": bot.strip(),
        "target_function": None,
        "target_lines": None,
        "target_description": None,
        "target_metric": raw.get("target_metric") or raw.get("metric"),
        "target_output": raw.get("target_output") or raw.get("output"),
        "confidence": 1.0,
        "interpreted_by": "agent_authored",
        "agent_authored": True,
    }


async def _process_meeting_file() -> None:
    """
    Read code_tuner_meeting.json, classify intent, dispatch, write response,
    delete file.
    """
    if not os.path.exists(MEETING_FILE):
        return

    raw = load_json(MEETING_FILE, default=None)
    if not isinstance(raw, dict):
        log_tune("meeting file unreadable — deleting")
        try: os.remove(MEETING_FILE)
        except Exception: pass
        return

    directive = raw.get("message") or raw.get("directive") or raw.get("text") or ""
    authored = _agent_authored_registry_action(raw)
    if not directive.strip() and authored is None:
        log_tune("meeting file empty - deleting")
        try: os.remove(MEETING_FILE)
        except Exception: pass
        return
    if not directive.strip() and authored is not None:
        directive = "%s %s" % (authored.get("intent"), authored.get("target_bot"))

    log_tune("meeting: processing directive (%s chars)" % len(directive))

    registry = load_json(BOTS_LIST_FILE, default={"bots": {}})

    if authored is not None:
        interp = authored
    else:
        interp = await interpret_directive(directive, registry)
        if interp.get("intent") in _REGISTRY_MUTATION_INTENTS:
            interp["status"] = "recorded"
            interp["registry_mutated"] = False
            interp["agent_authored"] = False
            interp["error"] = (
                "Local classifier does not reset, pause, resume, or add_metric a bot. "
                "Directive recorded. Registry not changed. The agent applies his own patch."
            )
    log_tune(f"meeting: intent={interp.get('intent')}, status={interp.get('status')}, "
             f"conf={interp.get('confidence', 0):.2f}")

    handler_status = ""
    output_path = ""

    if interp.get("status") == "applied":
        intent = interp["intent"]
        try:
            if intent == "show_source":
                output_path = await _handle_show_source(interp, registry)
                handler_status = "source fetch packet written"
            elif intent == "trigger_perf":
                output_path = await _handle_trigger_perf(interp, registry)
                handler_status = "perf packet fired" if output_path else "perf packet skipped (bot not monitored?)"
            elif intent == "trigger_audit":
                output_path = await _handle_trigger_audit(interp, registry)
                handler_status = "deep audit fired" if output_path else "audit skipped"
            elif intent in _REGISTRY_MUTATION_INTENTS:
                if not interp.get("agent_authored"):
                    interp["status"] = "recorded"
                    interp["registry_mutated"] = False
                    handler_status = "recorded only; registry not changed"
                elif intent == "reset":
                    handler_status = await _handle_reset(interp, registry)
                elif intent == "pause":
                    handler_status = await _handle_pause(interp, registry)
                elif intent == "resume":
                    handler_status = await _handle_resume(interp, registry)
                elif intent == "add_metric":
                    handler_status = await _handle_add_metric(interp, registry)
                # add_metric may return a capability_gap string — flag it
                if handler_status.startswith("capability_gap:"):
                    interp["status"] = "failed"
                    interp["gap_classification"] = "capability_gap"
                    interp["error"] = handler_status
        except Exception as e:
            log_tune(f"meeting handler err for intent '{intent}': {e}")
            interp["status"] = "failed"
            interp["error"] = f"handler crashed: {e}"

    _write_meeting_response_packet(directive, interp, handler_status, output_path)

    try:
        os.remove(MEETING_FILE)
    except Exception as e:
        log_tune(f"meeting file delete err: {e}")

async def meeting_watcher_loop():
    """
    File-presence watcher for code_tuner_meeting.json. Same cheap-when-idle
    doctrine as the perps meeting watcher. Local Ollama stays dormant
    until a file appears.
    """
    log_tune(f"meeting_watcher_loop: starting (poll {MEETING_POLL_INTERVAL_S}s, "
             f"LLM dormant until file appears)")
    while True:
        try:
            if os.path.exists(MEETING_FILE):
                await _process_meeting_file()
            else:
                write_heartbeat("meeting_watcher", {"watching": True})
        except Exception as e:
            log_tune(f"meeting_watcher_loop err: {e}")
            write_heartbeat("meeting_watcher", {"error": str(e)[:200]})
        await asyncio.sleep(MEETING_POLL_INTERVAL_S)

# ════════════════════════════════════════════════════════════════════════════
#       SPRINT 42 — SKILL EVOLUTION REVIEW (migrated from warden Layer 10)
# ════════════════════════════════════════════════════════════════════════════
# This work moved from warden because code_tuner is now the system's
# self-observation organ. Warden owns perimeter / immune-system / heartbeat
# Layer 4. Skill ecosystem observation fits cleanly in code_tuner alongside
# bot ecosystem observation. Two bots doing the same job was wasteful.
#
# What this loop does:
#   - Every 72h, read skill_evolution_log.jsonl for the window
#   - Aggregate: auto_applied, owner_approved, owner_rejected, owner_amended, refused
#   - Count open skill-patch files. The agent handles his own skill patches.
#   - If there's content to report, write a P3 packet routed to skill-evolution
#   - If the window is empty (nothing evolved), write NO packet (content-only doctrine)
#
# What stays in warden:
#   - skill_requests.json processing - RETIRED fleet-wide (dead queue; amendments go agent_direct)
#   - Doctrine cross-check + extraction candidate detection (Layer 10 daily check)
#   - Cross-skill doctrine map writing
#
# code_tuner only OBSERVES the evolution log; doesn't process requests or
# mutate skill files.

# Paths shared with warden
SKILL_EVOLUTION_LOG       = os.path.join(BOT_LOGS, "skill_evolution_log.jsonl")
SKILL_REQUESTS_FILE       = os.path.join(BOT_LOGS, "skill_requests.json")  # RETIRED dead constant - no longer read (agent_direct)
OWNER_INBOX_SKILLS_DIR     = os.path.join(OWNER_INBOX, "pending_skill_requests")
CROSS_SKILL_DOCTRINE_MAP  = os.path.join(BOT_LOGS, "cross_skill_doctrine_map.json")

# State file — persists "last fired ts" across boots so we don't re-fire on every restart
SKILL_EVO_REVIEW_STATE = os.path.join(BOT_LOGS, "code_tuner_skill_evo_review_state.json")

SKILL_EVO_REVIEW_INTERVAL_S = int(_envv(
    "CODE_TUNER_SKILL_EVO_INTERVAL_S", str(3 * 86400)
))  # 72h default
SKILL_EVO_REVIEW_WINDOW_HOURS = 72

def _se_read_evolution_log_window(hours: int) -> list[dict]:
    """Read JSONL evolution log entries within the last <hours>."""
    if not os.path.isfile(SKILL_EVOLUTION_LOG):
        return []
    cutoff_ts = (datetime.now(EST) - timedelta(hours=hours)).timestamp()
    out: list[dict] = []
    try:
        with open(SKILL_EVOLUTION_LOG, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Try common timestamp fields
                ts_val = rec.get("ts") or rec.get("timestamp") or rec.get("fired_at")
                rec_ts = None
                if isinstance(ts_val, (int, float)):
                    rec_ts = ts_val if ts_val < 7.3e9 else ts_val / 1000.0
                elif isinstance(ts_val, str):
                    try:
                        dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=EST)
                        rec_ts = dt.timestamp()
                    except ValueError:
                        pass
                if rec_ts is None or rec_ts >= cutoff_ts:
                    out.append(rec)
    except Exception as e:
        log_tune(f"_se_read_evolution_log_window err: {e}")
    return out

def _se_count_pending_owner_packets() -> list[str]:
    """List markdown files in owner_inbox/pending_skill_requests/."""
    if not os.path.isdir(OWNER_INBOX_SKILLS_DIR):
        return []
    try:
        return [fn for fn in os.listdir(OWNER_INBOX_SKILLS_DIR) if fn.endswith(".md")]
    except Exception:
        return []

def _se_count_queued_requests() -> list[dict]:
    """RETIRED: skill_requests.json is a DEAD queue (meta-tools phase; amendments go
    agent_direct). Never read it - a present-or-absent file must not be treated as a
    live queue (absence-is-signal). Always returns [] so the review reports the dead
    queue as dead, never laundered as 'clear'."""
    return []

def _se_load_review_state() -> dict:
    return load_json(SKILL_EVO_REVIEW_STATE, default={"last_fired_ts": 0})

def _se_save_review_state(state: dict):
    save_json(SKILL_EVO_REVIEW_STATE, state)

def _build_skill_evo_review_packet(window_hours: int = 72) -> tuple[str, dict]:
    """
    Build the 3-day skill evolution review packet body. Returns
    (markdown_content, summary_stats). If nothing happened in the window,
    summary_stats['has_content'] is False (caller decides whether to write).
    """
    log_window = _se_read_evolution_log_window(window_hours)

    # Bucket events by type
    event_counts: dict[str, int] = {}
    for rec in log_window:
        ev = rec.get("event", "unknown")
        event_counts[ev] = event_counts.get(ev, 0) + 1

    auto_applied   = [r for r in log_window if r.get("event") == "auto_applied"]
    owner_approved  = [r for r in log_window if r.get("event") == "owner_approved_and_applied"]
    owner_rejected  = [r for r in log_window if r.get("event") == "owner_rejected"]
    owner_amended   = [r for r in log_window if r.get("event") == "owner_amended_and_applied"]
    refused        = [r for r in log_window if r.get("event") == "refused"]
    owner_gated     = [r for r in log_window if r.get("event") == "owner_gated"]

    still_pending = _se_count_pending_owner_packets()
    queued        = _se_count_queued_requests()

    # Doctrine drift candidates (warden writes this map daily — code_tuner just reads)
    drift_candidates: list[dict] = []
    try:
        if os.path.isfile(CROSS_SKILL_DOCTRINE_MAP):
            with open(CROSS_SKILL_DOCTRINE_MAP, "r", encoding="utf-8") as f:
                dm = json.load(f)
            if isinstance(dm, dict):
                drift_candidates = dm.get("extraction_candidates", []) or []
    except Exception:
        pass

    # Content-only doctrine: skip the packet if nothing meaningful happened
    total_evolution_events = (
        len(auto_applied) + len(owner_approved) + len(owner_amended)
        + len(owner_rejected) + len(refused) + len(owner_gated)
    )
    has_content = (
        total_evolution_events > 0
        or len(still_pending) > 0
        or len(queued) > 0
        or len(drift_candidates) > 0
    )

    summary = {
        "has_content": has_content,
        "auto_applied": len(auto_applied),
        "owner_approved": len(owner_approved),
        "owner_amended": len(owner_amended),
        "owner_rejected": len(owner_rejected),
        "refused": len(refused),
        "owner_gated": len(owner_gated),
        "still_pending_owner": len(still_pending),
        "queued_requests": len(queued),
        "drift_candidates": len(drift_candidates),
        "total_log_entries": len(log_window),
    }

    if not has_content:
        return "", summary

    lines = [
        f"# {window_hours // 24}-Day Skill Evolution Review",
        "",
        "> **Handler:** load `skills/skill-evolution/SKILL.md` and follow the "
        "**\"Processing the 3-Day Review Packet\"** section. This packet is the "
        "data input; the skill is the protocol.",
        "",
        f"**Window:** last {window_hours}h",
        f"**Generated by:** code_tuner (Sprint 42 — migrated from warden Layer 10)",
        f"**Cadence:** every {window_hours // 24} days per skill-evolution doctrine",
        "",
        "**Doctrine note:** this packet fires only when there's content to report. "
        "Empty windows produce no packet. If you haven't seen one of these in a "
        "while, it means the skill ecosystem has been stable.",
        "",
        "---",
        "",
        "## What evolved",
        "",
        f"- Auto-applied: **{len(auto_applied)}**. The agent handles his own skill patches.",
        f"- The owner-approved: **{len(owner_approved)}** MEDIUM/HIGH requests approved + committed",
        f"- The owner-amended: **{len(owner_amended)}** requests where the owner edited then approved",
        f"- The owner-rejected: **{len(owner_rejected)}** requests the owner declined",
        f"- REFUSE-tier: **{len(refused)}** attempts on HARD-LOCKED files (identity, rules, etc.)",
        f"- Open skill-patch files: **{len(still_pending)}**. The agent handles his own skill patches.",
        f"- skill_requests.json queue: DEAD/retired (amendments go agent_direct)",
        "",
    ]

    if auto_applied:
        lines.append("### Recent auto-applied (last 10)")
        lines.append("")
        for r in auto_applied[-10:]:
            op = r.get("operation", "?")
            target = r.get("target", "?")
            req_id = r.get("req_id", "?")
            lines.append(f"- `{req_id}` — {op} on **{target}**")
        lines.append("")

    if still_pending:
        lines.append("### The agent handles his own skill patches")
        lines.append("")
        for fn in still_pending[:15]:
            lines.append(f"- `{fn}`")
        if len(still_pending) > 15:
            lines.append(f"- ... and {len(still_pending) - 15} more")
        lines.append("")

    if refused:
        lines.append("## What got refused (HARD LAW attempts)")
        lines.append("")
        for r in refused[-5:]:
            lines.append(f"- `{r.get('req_id', '?')}` — {r.get('reason', '?')}")
        lines.append("")
        lines.append(
            "Repeated REFUSE attempts on the same file may indicate that agent "
            "is trying to amend something through the wrong channel. Verify the "
            "right home (rules.md edits go via the owner direct, identity changes via "
            "The owner direct, Tier 0 skills via skill-writer)."
        )
        lines.append("")

    if drift_candidates:
        lines.append("## Doctrine drift candidates (from warden's daily cross-check)")
        lines.append("")
        lines.append(
            f"{len(drift_candidates)} candidate(s) where the same doctrine appears in 2+ "
            "skills WITHOUT an `inherits_doctrine` reference. Consider extracting to "
            "`skills/_doctrine/`."
        )
        lines.append("")
        for c in drift_candidates[:5]:
            marker = c.get("doctrine_marker", "?")
            appearing = ", ".join(c.get("appearing_in", []))
            lines.append(f"- `{marker}` — appears in: {appearing}")
        if len(drift_candidates) > 5:
            lines.append(f"- ... and {len(drift_candidates) - 5} more")
        lines.append("")
        lines.append(
            "**Suggested action:** extract these to `skills/_doctrine/` via agent_direct "
            "(the skill_requests.json queue is retired). code_tuner just surfaces the "
            "candidates here so you can prioritize."
        )
        lines.append("")

    if event_counts:
        lines.append("## Raw event counts (audit)")
        lines.append("")
        for event_name, count in sorted(event_counts.items(),
                                          key=lambda kv: kv[1], reverse=True):
            lines.append(f"- `{event_name}`: {count}")
        lines.append("")

    lines += [
        "---",
        "",
        "*This is an observation packet. Read the data, decide what to act on. "
        "Acting means amending skills via agent_direct / the skill-evolution "
        "skill's protocol (the skill_requests.json queue is retired).*",
    ]

    return "\n".join(lines), summary

async def skill_evolution_review_loop():
    """
    Every 72h, build a skill evolution review packet. If the window has content,
    write it to the agent's inbox routed to skill-evolution skill. If empty, write
    nothing (content-only doctrine).
    """
    log_tune(f"skill_evolution_review_loop: starting "
             f"(cycle {SKILL_EVO_REVIEW_INTERVAL_S}s = "
             f"{SKILL_EVO_REVIEW_INTERVAL_S // 86400}d, window "
             f"{SKILL_EVO_REVIEW_WINDOW_HOURS}h, content-only doctrine)")

    # Initial delay so we don't fire mid-boot
    await asyncio.sleep(600)  # 10 min

    while True:
        try:
            state = _se_load_review_state()
            last_fired = state.get("last_fired_ts", 0) or 0
            now = time.time()

            if (now - last_fired) >= SKILL_EVO_REVIEW_INTERVAL_S:
                content, summary = _build_skill_evo_review_packet(
                    SKILL_EVO_REVIEW_WINDOW_HOURS
                )
                if summary["has_content"] and content:
                    os.makedirs(INBOX, exist_ok=True)
                    ts = _short_ts()
                    fname = f"code_tuner - skill_evolution_review_{ts}.md"
                    out_path = os.path.join(INBOX, fname)
                    try:
                        with open(out_path, "w", encoding="utf-8") as f:
                            f.write(content)
                        log_tune(
                            f"skill_evolution_review: packet written ({fname}) — "
                            f"{summary['auto_applied']} auto + "
                            f"{summary['owner_approved']} approved + "
                            f"{summary['still_pending_owner']} pending"
                        )
                    except Exception as e:
                        log_tune(f"skill_evo_review write err: {e}")
                else:
                    log_tune(
                        "skill_evolution_review: empty window, no packet "
                        "(content-only doctrine)"
                    )

                state["last_fired_ts"] = now
                state["last_summary"] = summary
                _se_save_review_state(state)

            write_heartbeat("skill_evolution_review", {
                "last_fired_ts": last_fired,
                "next_check_in_s": SKILL_EVO_REVIEW_INTERVAL_S,
            })
        except Exception as e:
            log_tune(f"skill_evolution_review_loop err: {e}")
            write_heartbeat("skill_evolution_review", {"error": str(e)[:200]})

        # Check every 6h — granular enough that even a missed boot doesn't
        # stretch the cadence by much
        await asyncio.sleep(6 * 3600)

# ════════════════════════════════════════════════════════════════════════════
#                            BOOT + MAIN
# ════════════════════════════════════════════════════════════════════════════

# ============================================================================
#   CONFIG-VS-CODE DRIFT DETECTOR (cascade: to_build, code-monitor 3.2.0)
# ============================================================================
# Compare documented knob values in bot_config.md against the real source constants /
# getenv defaults in each trading bot. Emits a config_drift packet when docs say X but
# source says Y (the trail_activate 20-vs-30 class of bug). AST-based (not LLM),
# event-driven (re-scan on bot_config.md mtime or a bot source change), deduped per bot
# until the finding-set changes. PROVE-IT: it SURFACES packets for the agent to verify;
# it never edits code and never claims a fix.

import re as _cd_re
import ast as _cd_ast

_CD_CONFIG_PATH = os.path.join(BOTS_DIR, "bot_config.md")
_CD_STATE_FILE  = os.path.join(BOT_LOGS, "code_tuner_config_drift_state.json")
_CD_INTERVAL_S  = int(_envv("CODE_TUNER_CONFIG_DRIFT_INTERVAL_S", "600"))
# The detector only OBSERVES. The agent reads the packets.
_CD_BOTS = ["perps_bot"]
_CD_ALIASES = {
    "PERP_TRAIL_ACTIVATE": "TRAIL_ACTIVATE_PCT", "PERP_TRAIL_DROP": "TRAIL_DROP_PCT",
    "PERP_POLL_SECONDS": "PRICE_POLL_SECONDS", "PERP_FAST_POLL": "FAST_POLL_SECONDS",
    "PERP_SIGNAL_POLL": "SIGNAL_POLL", "PERP_AUTO_SIZE_USD": "AUTO_SIZE_USD",
    "PERP_AUTO_LEVERAGE": "AUTO_LEVERAGE",
}


def _cd_parse_config_knobs(path):
    """Two doc styles in bot_config.md: `NAME` (default V, ...) and `NAME` = `V` (...)."""
    knobs = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = _cd_re.search(r"`([A-Za-z_][A-Za-z0-9_]*)`\s*\(default\s+([0-9]+(?:\.[0-9]+)?)", line)
                if m:
                    knobs.setdefault(m.group(1), float(m.group(2)))
                m = _cd_re.search(r"`([A-Za-z_][A-Za-z0-9_]*)`\s*=\s*`([0-9]+(?:\.[0-9]+)?)`", line)
                if m:
                    knobs.setdefault(m.group(1), float(m.group(2)))
    except Exception as e:
        log_tune(f"config_drift: bot_config parse err: {e}")
    return knobs


def _cd_num(node):
    if isinstance(node, _cd_ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    return None


def _cd_getenv_default(node):
    """Unwrap float()/int(); pull the last numeric positional arg of a getenv-style call."""
    if isinstance(node, _cd_ast.Call):
        fn = node.func
        name = fn.attr if isinstance(fn, _cd_ast.Attribute) else (fn.id if isinstance(fn, _cd_ast.Name) else "")
        if name in ("float", "int") and node.args:
            inner = _cd_getenv_default(node.args[0])
            if inner is not None:
                return inner
            n = _cd_num(node.args[0])
            return (n, True) if n is not None else None
        if name in ("getenv", "get", "_env_float", "_perp_get_override", "get_override_sync"):
            for a in reversed(node.args):
                n = _cd_num(a)
                if n is not None:
                    return (n, True)
            return (None, True)  # reads env but no numeric default found
    return None


def _cd_scan_source(path):
    """AST: module-level SYMBOL = <literal> | os.getenv(..., default). Returns
    {symbol: {value, getenv, lineno}}."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            tree = _cd_ast.parse(f.read())
    except Exception as e:
        log_tune(f"config_drift: source parse err {os.path.basename(path)}: {e}")
        return out
    for node in tree.body:
        if isinstance(node, _cd_ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], _cd_ast.Name):
            sym = node.targets[0].id
            lit = _cd_num(node.value)
            if lit is not None:
                out[sym] = {"value": lit, "getenv": False, "lineno": node.lineno}
            else:
                g = _cd_getenv_default(node.value)
                if g is not None:
                    out[sym] = {"value": g[0], "getenv": True, "lineno": node.lineno}
    return out


def _cd_detect(config, source):
    """Value-mismatch findings, deduped per code symbol (multiple config aliases merge)."""
    by_symbol = {}
    for cfg_name, cfg_val in config.items():
        sym = _CD_ALIASES.get(cfg_name, cfg_name)
        s = source.get(sym) or source.get(cfg_name)
        if not s or s["value"] is None:
            continue  # docs-only, no code twin -> do not emit (SPEC 4)
        if abs(s["value"] - cfg_val) > 1e-9:
            rec = by_symbol.setdefault(sym, {
                "code_symbol": sym, "config_keys": [], "config_value": cfg_val,
                "source_value": s["value"], "getenv": s["getenv"], "lineno": s["lineno"],
            })
            if cfg_name not in rec["config_keys"]:
                rec["config_keys"].append(cfg_name)
    return list(by_symbol.values())


def _cd_write_packet(bot_name, findings):
    try:
        os.makedirs(INBOX, exist_ok=True)
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(INBOX, f"code_tuner - config_drift_{bot_name}_{ts}.md")
        keys = ", ".join(sorted({k for f in findings for k in f["config_keys"]}))
        out = ["---", "kind: config_drift", "severity: high",
               f"dedupe_key: code_tuner:config_drift:{bot_name}",
               "source_skill: code_tuner",
               f"summary: {len(findings)} config-vs-code drift(s) in {bot_name} ({keys})",
               "---", "",
               f"# config-vs-code drift - {bot_name}", "",
               f"**source:** `bots/{bot_name}.py`  ",
               "**config:** `bots/bot_config.md`  ",
               "**detector:** code_tuner config_drift (AST) - PROVE-IT mode, verify before trusting", "",
               "| config key(s) | code symbol | documented | source | reads env? | line |",
               "|---|---|---|---|---|---|"]
        for f in findings:
            src = "never_read" if f["source_value"] is None else f["source_value"]
            out.append(f"| {', '.join(f['config_keys'])} | `{f['code_symbol']}` | {f['config_value']} | "
                       f"{src} | {'yes' if f['getenv'] else 'NO (bare)'} | L{f['lineno']} |")
        out += ["",
                "The detector does NOT judge which side is right - it surfaces the mismatch.",
                "The agent reads this packet and applies his own patch (fix code, or correct the doc).",
                "Trading knobs default HIGH. Do NOT loosen SL / raise leverage under cover of a drift fix."]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
        log_tune(f"config_drift: wrote packet for {bot_name} ({len(findings)} drift(s))")
        return path
    except Exception as e:
        log_tune(f"config_drift: packet write err {bot_name}: {e}")
        return None


async def config_drift_loop():
    """Event-driven config-vs-code drift detector. Re-scans when bot_config.md or a bot
    source changes; dedupes per bot+finding-set so it does not spam. PROVE-IT: emits
    packets for the agent to verify; never edits code, never claims a fix."""
    log_tune(f"config_drift_loop: starting (poll {_CD_INTERVAL_S}s, AST, PROVE-IT mode)")
    while True:
        try:
            await asyncio.sleep(_CD_INTERVAL_S)
            if not os.path.exists(_CD_CONFIG_PATH):
                continue
            config = _cd_parse_config_knobs(_CD_CONFIG_PATH)
            if not config:
                continue
            try:
                cfg_mtime = os.path.getmtime(_CD_CONFIG_PATH)
            except OSError:
                cfg_mtime = 0
            state = load_json(_CD_STATE_FILE, {}) or {}
            for bot in _CD_BOTS:
                sp = os.path.join(BOTS_DIR, "perps.py" if bot == "perps_bot" else f"{bot}.py")
                if not os.path.exists(sp):
                    continue
                try:
                    src_mtime = os.path.getmtime(sp)
                except OSError:
                    continue
                findings = _cd_detect(config, _cd_scan_source(sp))
                sig = f"{cfg_mtime:.0f}:{src_mtime:.0f}:" + ";".join(
                    sorted(f"{f['code_symbol']}={f['source_value']}vs{f['config_value']}" for f in findings))
                prev = state.get(bot, {})
                if findings and prev.get("sig") != sig:
                    _cd_write_packet(bot, findings)
                    state[bot] = {"sig": sig, "last_emit": _now_iso(), "count": len(findings)}
                elif not findings and prev.get("sig"):
                    state[bot] = {"sig": "", "last_emit": _now_iso(), "count": 0}
            save_json(_CD_STATE_FILE, state)
        except Exception as e:
            log_tune(f"config_drift_loop err: {e}")
            await asyncio.sleep(1200)


# ============================================================================
#   LIVE-UPDATE (element e, simple-bot clean core)
# ============================================================================
# Poll THIS file's mtime. When the agent has already applied his own patch on disk,
# re-exec in place (os.execv, same PID + terminal) so that change loads without a
# separate restart. No browser/external process to tear down; Ctrl-C works normally.
# Floor: never self-modify code_tuner.py; only reload a change already on disk.
# Never fetch or choose code. Do not edit other bots' .py. Do not auto-deploy.
import sys as _lu_sys
import shutil as _lu_shutil
import hashlib as _lu_hashlib

_LU_BOT      = "code_tuner"
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
        log_tune(f"live-update report failed: {e}")


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
    log_tune(f"live-update: reloading {_LU_BOOT_HASH} -> {new_hash} (re-exec in place)")
    try:
        os.execv(_lu_sys.executable, [_lu_sys.executable, _LU_SELF] + _lu_sys.argv[1:])
    except Exception as e:
        log_tune(f"live-update: os.execv failed, staying on current code: {e}")
        _lu_write_report("bot_restart_failed", "high",
                         f"{_LU_BOT} patch {new_hash} detected but re-exec failed; STILL RUNNING old {_LU_BOOT_HASH}",
                         {"error": str(e)[:200]})
        try:
            os.remove(_LU_MARKER)
        except Exception:
            pass


async def live_update_watcher_loop():
    log_tune(f"live_update_watcher: online (poll {_LU_POLL_S}s, boot {_LU_BOOT_HASH})")
    while True:
        try:
            if _lu_mtime(_LU_SELF) > _LU_BOOT_MTIME and _lu_hash(_LU_SELF) != _LU_BOOT_HASH:
                _lu_reload_now()  # os.execv does not return on success
        except Exception as e:
            log_tune(f"live_update_watcher err: {e}")
        await asyncio.sleep(_LU_POLL_S)


async def main():
    print(f"[tuner] code_tuner v4.2 — perf monitor + source tracker + setup + metrics + meeting_watcher")
    print(f"[tuner] BOTS_DIR:           {BOTS_DIR}")
    print(f"[tuner] BOT_LOGS:           {BOT_LOGS}")
    print(f"[tuner] SOURCE_SNAPSHOTS:   {SOURCE_SNAPSHOTS_DIR}")
    print(f"[tuner] BOTS_LIST_FILE:     {BOTS_LIST_FILE}")
    print(f"[tuner] METRICS_CURRENT:    {METRICS_CURRENT_FILE}")
    print(f"[tuner] SKILLS_DIR:         {SKILLS_DIR}")
    print(f"[tuner] SKILLS_METADATA:    {SKILLS_METADATA}")
    print(f"[tuner] SETUP_RESPONSE:     {SETUP_RESPONSE_FILE}")
    print(f"[tuner] MEETING_FILE:       {MEETING_FILE}")
    print(f"[tuner] OLLAMA:             {OLLAMA_HOST} {OLLAMA_CLASSIFIER_MODEL} (local classifier only)")
    print(f"[tuner] Bot source:         warden.bot_heartbeat_thresholds_minutes")
    print(f"[tuner] Snapshot cadence:   {SNAPSHOT_INTERVAL_S}s")
    print(f"[tuner] Registry cadence:   {REGISTRY_REFRESH_S}s")
    print(f"[tuner] Perf cycle target:  {PERF_PACKET_CYCLE_S}s per bot (~48h)")
    print(f"[tuner] Metrics refresh:    {METRICS_REFRESH_INTERVAL_S}s")
    print(f"[tuner] Meeting poll:       {MEETING_POLL_INTERVAL_S}s")
    print(f"[tuner] Skill evo review:   {SKILL_EVO_REVIEW_INTERVAL_S}s (content-only)")

    # Ensure dirs exist
    for p in [BOT_LOGS, JSON_RELAY, INBOX, OWNER_INBOX, SOURCE_SNAPSHOTS_DIR]:
        os.makedirs(p, exist_ok=True)

    write_heartbeat("boot")
    _lu_on_boot()  # live-update: report a completed reload + snapshot last-good

    # First-boot seed: discover bots, build initial registry, take initial snapshots
    log_tune("first-boot seed: discovering bots + seeding registry")
    initial_registry = seed_or_refresh_registry()
    n_total = len(initial_registry.get("bots", {}))
    n_unconf, unconf_names = count_unconfigured_bots(initial_registry)
    log_tune(f"first-boot seed: {n_total} bots in registry ({n_unconf} unconfigured)")
    if unconf_names:
        log_tune(f"unconfigured bots: {', '.join(unconf_names[:10])}"
                 + ("..." if len(unconf_names) > 10 else ""))

    # Take initial skill snapshots (will be empty until the agent configures bots)
    skill_results = snapshot_all_associated_skills(initial_registry)
    log_tune(f"first-boot seed: {len(skill_results)} skill snapshots taken")

    write_heartbeat("boot_complete", {
        "total_bots": n_total,
        "unconfigured": n_unconf,
        "skill_snapshots": len(skill_results),
    })

    log_tune("code_tuner v4.2 online — entering steady-state loops")

    # All Stage 1-4 loops + Sprint 42 skill_evolution_review (migrated from warden)
    await asyncio.gather(
        registry_refresh_loop(),
        snapshot_loop(),
        heartbeat_loop(),
        code_request_router_loop(),
        setup_mode_loop(),
        perf_packet_loop(),
        deep_audit_loop(),
        metrics_current_refresh_loop(),
        meeting_watcher_loop(),
        skill_evolution_review_loop(),
        config_drift_loop(),
        live_update_watcher_loop(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_tune("interrupted, shutting down")
    except Exception as e:
        log_tune(f"fatal: {e}")
        raise
