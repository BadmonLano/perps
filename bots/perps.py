import sys as _pyver_sys
if _pyver_sys.version_info < (3, 12):
    _pyver_sys.exit("Perps needs Python 3.12 or newer (this is %d.%d). Install 3.12+ from python.org, recreate the venv, then run again." % _pyver_sys.version_info[:2])
import json
"""
perps_bot.py v5.7 — Sprint 46 / per-chain packets (SOL/BTC/ETH), Build B removed
(v5.7, 2026-07-26: LIQ THREE-NUMBER SPLIT — liq_price now holds the VENUE's real
liquidation read off the on-chain position account (offsets verified against a
live position), liq_price_source carries provenance, liq_exit_floor is ours and
recomputed as a function every pass. The liq guard's AUTO-CLOSE IS GONE: it fired
at a double-buffered level ~14% above real liq and 1.5% under the SL, closing
positions before the agent could act. It is now a NOTIFICATION carrying the full
chain packet plus a deposit ladder. THE BOT HAS ZERO AUTOMATIC CLOSES.
Also: hold_hours hoisted out of the reversal branch — it was unbound on every
clean scan, killing intelligence_scanner before the take-profit and
entry-proposal paths could run. DRIFT FULLY PURGED: dead drift_client parameter
removed from 11 signatures and ~30 call sites, dead module ref and helper
deleted, live app.drift.trade URLs repointed to jup.ag, stale driftpy claims
and Drift-era changelog removed.
Version bumped per the agent: a changed hash with an unchanged version string makes
every patch_landed_ack ambiguous.)
═════════════════════════════════════════════════════════════════════════════

WHAT'S NEW IN v5.2 (Sprint 41):
  Three Sprint 41 additions:
    1. Boot-time _self_description.json generation via _self_description module.
       AST introspection extracts TUNABLE_PARAMS, action handlers, loops,
       path constants, file hash, version. Cached in bot_logs/.
    2. Post-patch detection. On boot, compares current source hash against
       cached self-description. On mismatch, writes P2 patch_landed_ack to
       The agent's inbox + Telegram notify with capability surface delta.
    3. Meeting watcher capability gap routing. When the owner writes a directive
       and the LLM proposes a TUNABLE_PARAMS name that doesn't exist,
       perps_bot now classifies the gap (hard_locked / capability_gap /
       ambiguous) and fires capability_gap PACKETS to the agent's inbox (Cert 007; code_requests.json retired).
       The gap stays the agent's own unfinished patch. He applies it himself.
       Previously the directive died silently with
       "HARD-LOCKED".

SUPERSEDED CHANGELOG (v5.1 Sprint 40, v5.0 Sprint 39, v4 Sprint 27) — REMOVED
2026-07-26 with the Drift purge. Those entries documented a Drift-first
architecture (Drift as strategy venue, Jupiter as failover, Drift health checks,
Drift resting-order controls). NONE of it is live: Drift was torn out in Sprint
57 and Jupiter is the only venue. The text was left in place long after it
stopped being true and kept re-teaching a dead model to anyone reading the top
of this file. History is in git; doctrine lives here, and doctrine must be true.
"""
import math
import os, json, traceback, asyncio, httpx, re, struct, hashlib, secrets

def _envv(key, default=None):
    """os.getenv, but a blank value (KEY= in .env) counts as unset and returns the default."""
    v = os.environ.get(key)
    return default if v is None or not str(v).strip() else v


# ── Repo root (public build) ──────────────────────────────────────────────
# WORKSPACE_DIR in the environment (or bots/.env) wins; otherwise the repo root is
# the folder that contains bots/. Every path in this file hangs off it.
def _resolve_workspace_root():
    _env = (os.environ.get("WORKSPACE_DIR") or "").strip()
    if _env:
        return os.path.abspath(os.path.expanduser(_env))
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
WS_SUB_BOTS = os.path.join(WS_BOTS, "sub-bots")
WS_BOT_LOGS = os.path.join(WS_ROOT, "bot_logs")
WS_JSON_RELAY = os.path.join(WS_ROOT, "json_relay")
for _d in (WS_BOT_LOGS, WS_JSON_RELAY, os.path.join(WS_ROOT, "inbox"), os.path.join(WS_ROOT, "inbox_results", "done"),
           os.path.join(WS_ROOT, "inbox_results", "failed"), os.path.join(WS_ROOT, "owner_inbox")):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass
# sub-bots (packet_analyzers, _self_description, agent_ack, ...) import by bare name
import sys as _ws_sys
for _pth in (WS_SUB_BOTS, os.path.dirname(os.path.abspath(__file__))):
    if _pth not in _ws_sys.path:
        _ws_sys.path.insert(0, _pth)
# ──────────────────────────────────────────────────────────────────────────

# ── Command line (--help / --version exit before anything starts) ───────────
if __name__ == "__main__":
    import argparse as _ap
    _parser = _ap.ArgumentParser(
        prog="perps.py",
        description="Perps engine: Jupiter Perps (SOL/BTC/ETH) packets, gates and execution for your SI agent. "
                    "Paper mode by default (PERPS_DRY_RUN=true in bots/.env): nothing is signed or sent.",
        epilog="No flags: run the setup wizard if bots/.env is missing or incomplete, then start the engine, "
               "console and sub-bots (Ctrl+C stops everything). Console only: python bots/perps_ui.py. "
               "Docs: README.md and SETUP.md.")
    _parser.add_argument("--setup", action="store_true", help="run the setup wizard, write bots/.env and exit")
    _parser.add_argument("--version", action="version", version="Perps v1.0.4")
    _parser.parse_known_args()

# ── First-run setup wizard (bots/perps_setup.py) ─────────────────────────────
# Runs before bots/.env is loaded: setup.json (agent) is applied, a complete .env is left
# alone, an incomplete or missing one opens the wizard in a terminal.
# `python bots/perps.py --setup` re-runs the wizard and exits. Never on import.
if __name__ == "__main__":
    try:
        import perps_setup as _perps_setup
        if "--setup" in _ws_sys.argv[1:]:
            _perps_setup.first_run_check(force=True)
            raise SystemExit(0)
        _perps_setup.first_run_check()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("setup stopped by user")
        raise SystemExit(130)
    except Exception as _pse:
        print(f"[setup] wizard unavailable ({type(_pse).__name__}); using bots/.env as it is")

# ── Windows reload supervisor (2026-10-16) ─────────────────────────────
# On Windows os.execv does not replace the process: it starts a new one and the
# old one exits, so PowerShell takes the console back while the bot keeps drawing
# on it and Ctrl+C goes to the shell. On Windows this file now starts as a small
# supervisor that stays in the console and runs the engine as a CHILD process.
# A live-update reload makes the child exit with PERPS_RELOAD_EXIT_CODE and the
# supervisor starts the new code in the SAME window. Ctrl+C stops the engine and
# then the supervisor (a second Ctrl+C forces it). PERPS_NO_SUPERVISOR=1 opts out.
# POSIX keeps os.execv (it really replaces the process there).
PERPS_RELOAD_EXIT_CODE = 75


def _perps_supervise():
    import subprocess as _sv_sp, sys as _sv_sys, time as _sv_t
    _env = dict(os.environ, PERPS_SUPERVISED="1")
    _cmd = [_sv_sys.executable, os.path.abspath(__file__)] + _sv_sys.argv[1:]
    _reloads = []
    while True:
        _child = _sv_sp.Popen(_cmd, env=_env)
        _stopping, _rc = False, None
        while _rc is None:
            try:
                _rc = _child.wait(timeout=1.0)
            except _sv_sp.TimeoutExpired:
                continue
            except KeyboardInterrupt:
                if _stopping:
                    print("[supervisor] second Ctrl+C - force-stopping the engine")
                    try:
                        _child.kill()
                    except Exception:
                        pass
                else:
                    _stopping = True
                    print("[supervisor] Ctrl+C - engine is shutting down (Ctrl+C again to force)")
        if _stopping:
            return 0
        if _rc == PERPS_RELOAD_EXIT_CODE:
            _now = _sv_t.time()
            _reloads = [t for t in _reloads if _now - t < 300] + [_now]
            if len(_reloads) > 5:
                print("[supervisor] 6 reloads inside 5 min - stopping to avoid a reload loop")
                return 1
            print("[supervisor] live-update reload - starting the new code in this window")
            continue
        return _rc


if (__name__ == "__main__" and os.name == "nt" and os.environ.get("PERPS_SUPERVISED") != "1"
        and _envv("PERPS_NO_SUPERVISOR", "").strip().lower() not in ("1", "true", "yes")):
    raise SystemExit(_perps_supervise() or 0)

# Sprint 41 — self-awareness module. Generates _self_description.json at boot
# and detects post-patch reboots via source hash comparison.
try:
    from _self_description import generate_self_description, detect_source_patched
    _SELF_DESC_AVAILABLE = True
except ImportError:
    _SELF_DESC_AVAILABLE = False
    generate_self_description = None
    detect_source_patched = None

_TRACKER_AVAILABLE = False

import time
# polish-2026-10-15 (item 9): Windows consoles default to cp1252 ('charmap') and
# crashed prints carrying arrows/emoji (IDL normalization line). Re-wrap stdio as
# UTF-8 with replacement so a print can never raise.
try:
    import sys as _pp_sys
    for _pp_stream in (_pp_sys.stdout, _pp_sys.stderr):
        if _pp_stream is not None and hasattr(_pp_stream, "reconfigure"):
            _pp_stream.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Finalized
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.message import Message
from solders.instruction import Instruction, AccountMeta
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.transaction import VersionedTransaction
from solders.system_program import transfer as _sys_transfer, TransferParams as _SysTransferParams
import base64 as _b64
from anchorpy import Provider, Wallet
# Sprint 39: anchorpy Program + Idl for Jupiter Perps. The IDL is fetched at
# startup via anchorpy.Program.fetch_idl(program_id, provider) and used to
# build CreateIncreasePositionMarketRequest / CreateDecreasePositionRequest2.
try:
    from anchorpy import Program, Idl
    _ANCHORPY_PROGRAM_AVAILABLE = True
except Exception:
    _ANCHORPY_PROGRAM_AVAILABLE = False
    Program = None
    Idl = None
# Sprint 57: driftpy imports REMOVED — Drift is gone, nothing in this file uses
# DriftClient / DriftUser / driftpy types or constants anymore. The bot no longer
# depends on the driftpy package at all.
load_dotenv(os.path.join(WS_BOTS, ".env"))
PRIVATE_KEY      = os.getenv("PRIVATE_KEY")
RPC_URL          = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
# Paper mode (PERPS_DRY_RUN=true): fills simulate at the oracle mark, positions and P/L go to
# bot_logs/paper_*.json, protection runs as usual, and NOTHING is ever signed or sent.
PERPS_DRY_RUN    = str(os.getenv("PERPS_DRY_RUN", "false")).strip().lower() in ("1", "true", "yes", "on", "paper")


# ── Helius credit meter + optional daily cap (2026-10-17, HELIUS_AUDIT) ─────────
# Counts every JSON-RPC call this process sends to a Helius endpoint. All
# AsyncClient / anchorpy traffic goes through solana-py's HTTP provider, so one
# hook covers every read and send. Numbers are ESTIMATES from the cost table
# (HELIUS_CREDIT_COSTS); compare with the Helius dashboard.
# Written to bot_logs/perps_helius_meter.json .
# The cap NEVER blocks a trade, a close, or a position-safety read. When it trips,
# only background reads slow down: the shared mark-price cache for the owner
# loop / limit watcher stretches to PERP_MARK_CACHE_CAPPED_S, and the hourly
# packet reuses the last wallet balances. HELIUS_DAILY_CREDIT_CAP=0 = off (unset = Free plan, 30000/day).
# The setup wizard sets this from your Helius plan (Free 30000, Developer 300000,
# Business 3000000, custom, or 0 = off). Unset = the Free-plan cap, 30000.
HELIUS_FREE_PLAN_DAILY_CAP = 30000
HELIUS_DAILY_CREDIT_CAP = int(float(os.getenv("HELIUS_DAILY_CREDIT_CAP", "").strip() or HELIUS_FREE_PLAN_DAILY_CAP))
HELIUS_CAP_MODE         = (os.getenv("HELIUS_CAP_MODE", "soft") or "soft").strip().lower()   # soft | hard | log
HELIUS_SOFT_CAP_PCT     = float(os.getenv("HELIUS_SOFT_CAP_PCT", "80") or 80)
_PERP_HM_COSTS = {"default": 1, "getProgramAccounts": 10}
try:
    _PERP_HM_COSTS.update(json.loads(os.getenv("HELIUS_CREDIT_COSTS", "{}") or "{}"))
except Exception:
    pass
_PERP_HM = {"day": None, "total": 0.0, "calls": 0, "by_method": {}, "flushed": 0.0, "dirty": False}
_PERP_HM_INSTALLED = False


def _perp_hm_file() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_logs", "perps_helius_meter.json")


def _perp_hm_day() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _perp_hm_roll() -> None:
    """New local day -> start fresh; on first use, resume today's count from disk
    so a restart does not reset the cap."""
    day = _perp_hm_day()
    if _PERP_HM["day"] == day:
        return
    first = _PERP_HM["day"] is None
    _PERP_HM.update({"day": day, "total": 0.0, "calls": 0, "by_method": {}})
    if first:
        try:
            with open(_perp_hm_file(), encoding="utf-8") as f:
                d = (json.load(f).get("days") or {}).get(day) or {}
            _PERP_HM["total"] = float(d.get("total_credits_est") or 0)
            _PERP_HM["calls"] = int(d.get("calls") or 0)
            _PERP_HM["by_method"] = dict(d.get("by_method") or {})
        except Exception:
            pass


def _perp_hm_flush(force: bool = False) -> None:
    if not _PERP_HM["dirty"] or (not force and time.time() - _PERP_HM["flushed"] < 30):
        return
    _PERP_HM["flushed"] = time.time()
    _PERP_HM["dirty"] = False
    fp = _perp_hm_file()
    try:
        try:
            with open(fp, encoding="utf-8") as f:
                hist = json.load(f) or {}
        except Exception:
            hist = {}
        days = hist.get("days") or {}
        days[_PERP_HM["day"]] = {"total_credits_est": round(_PERP_HM["total"], 1),
                                 "calls": _PERP_HM["calls"],
                                 "by_method": dict(sorted(_PERP_HM["by_method"].items(), key=lambda kv: -kv[1]))}
        for k in sorted(days)[:-14]:
            days.pop(k, None)
        hist = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "cap": HELIUS_DAILY_CREDIT_CAP, "cap_mode": HELIUS_CAP_MODE,
                "budget_state": perp_helius_budget_state(),
                "note": "perps estimates from HELIUS_CREDIT_COSTS; compare with the Helius dashboard",
                "days": days}
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, indent=2)
        os.replace(tmp, fp)
    except Exception as e:
        print(f"[helius meter] write failed (non-fatal): {e}")


def perp_helius_meter(method: str, n: int = 1) -> None:
    """Count estimated Helius credits for one (or n) calls of `method`."""
    try:
        _perp_hm_roll()
        c = float(_PERP_HM_COSTS.get(method, _PERP_HM_COSTS.get("default", 1))) * n
        _PERP_HM["total"] += c
        _PERP_HM["calls"] += n
        _PERP_HM["by_method"][method] = _PERP_HM["by_method"].get(method, 0) + c
        _PERP_HM["dirty"] = True
        _perp_hm_flush()
    except Exception:
        pass


def perp_helius_budget_state() -> str:
    if HELIUS_DAILY_CREDIT_CAP <= 0:
        return "off"
    _perp_hm_roll()
    used = _PERP_HM["total"]
    if used >= HELIUS_DAILY_CREDIT_CAP:
        return "hard"
    if used >= HELIUS_DAILY_CREDIT_CAP * HELIUS_SOFT_CAP_PCT / 100.0:
        return "soft"
    return "ok"


def perp_helius_restricted() -> bool:
    """True when BACKGROUND Helius reads should back off. Trades / closes /
    position-safety reads never consult this."""
    st = perp_helius_budget_state()
    if HELIUS_CAP_MODE == "log":
        return False
    if HELIUS_CAP_MODE == "soft":
        return st in ("soft", "hard")
    return st == "hard"


def _perp_hm_method(body) -> str:
    n = type(body).__name__ or "unknown"
    return n[:1].lower() + n[1:]


def _perp_hm_install() -> None:
    """Hook solana-py's HTTP provider once. Pure observation: the request itself
    is built and sent exactly as before."""
    global _PERP_HM_INSTALLED
    if _PERP_HM_INSTALLED:
        return
    try:
        from solana.rpc.providers import core as _sp_core
        _orig_one = _sp_core._HTTPProviderCore._before_request
        _orig_batch = _sp_core._HTTPProviderCore._before_batch_request

        def _before_request(self, body, *a, **kw):
            try:
                if "helius" in str(getattr(self, "endpoint_uri", "") or "").lower():
                    perp_helius_meter(_perp_hm_method(body))
            except Exception:
                pass
            return _orig_one(self, body, *a, **kw)

        def _before_batch_request(self, reqs, *a, **kw):
            try:
                if "helius" in str(getattr(self, "endpoint_uri", "") or "").lower():
                    for b in reqs:
                        perp_helius_meter(_perp_hm_method(b))
            except Exception:
                pass
            return _orig_batch(self, reqs, *a, **kw)

        _sp_core._HTTPProviderCore._before_request = _before_request
        _sp_core._HTTPProviderCore._before_batch_request = _before_batch_request
        _PERP_HM_INSTALLED = True
        import atexit as _hm_atexit
        _hm_atexit.register(lambda: _perp_hm_flush(force=True))
    except Exception as e:
        print(f"[helius meter] hook not installed (non-fatal): {e}")


_perp_hm_install()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
for name, val in [("PRIVATE_KEY", PRIVATE_KEY)]:
    if not val and not PERPS_DRY_RUN:
        print(f"FATAL: {name} not set in .env")
        exit(1)
if PERPS_DRY_RUN:
    if not PRIVATE_KEY:
        # Paper mode needs no wallet: a throwaway key in memory only, never saved, never funded.
        PRIVATE_KEY = str(Keypair())
    print("=" * 64 + "\n PAPER MODE (PERPS_DRY_RUN=true): no transaction will be signed or sent.\n"
          " Paper positions: bot_logs/paper_perp_positions.json  P/L: bot_logs/paper_perp_pnl.json\n" + "=" * 64)
MAX_LEVERAGE       = 100.0     # legacy reference value only; the live leverage gate is PERP_MAX_LEVERAGE below (unset = 20x)

# ── MINIMUM OPENING — the owner's ruling 2026-07-26 ──────────────────────────────
# The 2026-07-26 SOL long was RIGHT (panic floor, held 47h, thesis paid) and
# returned $0.60 on +15%, because $5 collateral at 5x is $25 of notional — small
# in BOTH dimensions at once. A position that cannot produce a readable outcome
# teaches the autopsy nothing whether it wins or loses.
# The floor is $10 at 10x across all three chains, long or short. It does not
# dictate style: spike-flip at 60x on $10 clears it, so does a 10x thesis hold.
# It only forbids being small in collateral AND leverage simultaneously.
# DECLINE, NEVER CLAMP — "what he requests is what gets landed."
PERP_MIN_COLLATERAL_USD = 10.0
PERP_MIN_LEVERAGE       = 10.0
PERP_MAX_LEVERAGE       = min(250.0, max(10.0, float(_envv("PERP_MAX_LEVERAGE") or 20.0)))  # user cap (bots/.env); unset = 20x, same as .env.example and the wizard; clamped to 10x..250x venue max
# Optional per-position collateral cap in SOL (bots/.env). Unset or 0 = no extra cap beyond the wallet and risk rules.
try:
    PERP_MAX_COLLATERAL_SOL = float(_envv("PERP_MAX_COLLATERAL_SOL") or 0) or None
except ValueError:
    PERP_MAX_COLLATERAL_SOL = None
if PERP_MAX_COLLATERAL_SOL is not None and PERP_MAX_COLLATERAL_SOL <= 0:
    PERP_MAX_COLLATERAL_SOL = None
PERP_MIN_NOTIONAL_USD   = PERP_MIN_COLLATERAL_USD * PERP_MIN_LEVERAGE  # $100
MIN_COLLATERAL_USD = 5.0    # venue floor — the gate FLOORS UP to this after scaling, never rejects a fireable intent against it
# ── LIQ / EXIT FLOOR — the owner's doctrine 2026-07-26 (the three-number split) ──
# BEFORE: one name ("liq_price") held a 5%-buffered ESTIMATE, and the guard
# applied ANOTHER 10% on top. Net effect on the live SOL 5x: venue liq 59.51,
# stored "liq" 62.95, auto-close firing at 69.25 — 14% of real room unused and
# only 1.54% under the SL, which quietly nullified "The agent decides". Now:
#   liq_price      -> the VENUE's real liquidation (on-chain read; est. fallback)
#   liq_exit_floor -> OUR floor, LIQ_EXIT_FLOOR_PCT above it
#   breach         -> a NOTIFICATION carrying the full chain packet. NEVER a close.
LIQ_EXIT_FLOOR_PCT   = 10.0   # house floor this % above the venue's real liq
LIQ_FLOOR_REARM_PCT  = 2.0    # must recover this % past the floor before re-firing
LIQ_FEE_RESERVE_FRAC = 0.00317  # fee reserve as frac of size_usd — MEASURED against the
                                # live SOL long 2026-07-26 (chain: entry 74.019664, size 25.0,
                                # collateral 4.981678; jup.ag liq 59.51 -> implied $0.0793).
                                # NOT constant: ~$0.063 of that is fixed (close fee + maintenance)
                                # and ~$0.016 is accrued borrow, which GROWS with hold time. At
                                # 0.0014%/hr on $25 that adds ~$0.035 per 100h -> liq drifts up
                                # ~$0.10. Immaterial for a floor sitting 10% away; matters if this
                                # number is ever used for anything tighter.
LIQ_GUARD_BUFFER_PCT = LIQ_EXIT_FLOOR_PCT  # back-compat alias: any live override under the old name still resolves
MIN_SOL_RESERVE    = 0.05

# ---- env/config-driven knobs (axiom_log_edge_v1 the owner-ratified 2026-08-02) ----
# bot_config.md documents PERP_* env names; read them here so config == runtime.
# Prefer PERP_* env, fall back to the module/TUNABLE name, then the ratified default.
def _env_float(perp_name, alt_name, default):
    v = os.environ.get(perp_name, os.environ.get(alt_name, None))
    return float(v) if v is not None else float(default)

# polls (timing affects exit responsiveness)
PRICE_POLL_SECONDS  = int(_env_float("PERP_POLL_SECONDS", "PRICE_POLL_SECONDS", 30))
FAST_POLL_SECONDS   = int(_env_float("PERP_FAST_POLL", "FAST_POLL_SECONDS", 8))
SIGNAL_POLL         = int(_env_float("PERP_SIGNAL_POLL", "SIGNAL_POLL", 10))
# exits (money path) - trail arm ratified 20 (was bare 30); drop 15 unchanged
TRAIL_ACTIVATE_PCT  = _env_float("PERP_TRAIL_ACTIVATE", "TRAIL_ACTIVATE_PCT", 20)
TRAIL_DROP_PCT      = _env_float("PERP_TRAIL_DROP", "TRAIL_DROP_PCT", 15)
# polish-2026-10-15 (item 7): the trail tightens once a position is well in
# profit, keyed on PEAK leveraged gain. (peak_at_least_pct, giveback_pct).
# The widest matching tier wins; below the first tier TRAIL_DROP_PCT applies.
# Sensor only: a trail level is an alert, never a close.
PERP_TRAIL_TIERS = [(50.0, 12.0), (100.0, 10.0), (200.0, 8.0)]

# Refuse any open/add while the bot-side monitor (the only thing watching TP/SL) is not ticking.
PERP_OPEN_REQUIRE_MONITOR = _env_float("PERP_OPEN_REQUIRE_MONITOR", "OPEN_REQUIRE_MONITOR", 1) >= 1
PERP_MONITOR_STALE_S      = _env_float("PERP_MONITOR_STALE_S", "MONITOR_STALE_S", 180)
# Refuse an add whose estimated post-add liquidation sits within this % of the stop.
PERP_ADD_MIN_SL_LIQ_GAP_PCT = _env_float("PERP_ADD_MIN_SL_LIQ_GAP_PCT", "ADD_MIN_SL_LIQ_GAP_PCT", 3.0)
_PERP_MONITOR_LAST_TICK = 0.0


def _perp_monitor_alive(stale_s=None):
    """True if monitor_position() ticked within stale_s seconds."""
    try:
        lim = float(stale_s if stale_s is not None else PERP_MONITOR_STALE_S)
        last = float(globals().get("_PERP_MONITOR_LAST_TICK") or 0.0)
        return last > 0 and (time.time() - last) <= lim
    except Exception:
        return False


def _perp_est_liq(direction, entry, size_usd, collateral_usd, factor=0.9):
    """Conservative liq estimate (factor < 1 puts it closer than the venue's)."""
    if not (entry and size_usd and collateral_usd):
        return None
    m = (collateral_usd / size_usd) * factor
    return entry * (1 - m) if direction == "long" else entry * (1 + m)


def _perp_sl_liq_gap_pct(direction, sl, liq):
    if not (sl and liq):
        return None
    return ((sl - liq) / sl * 100.0) if direction == "long" else ((liq - sl) / sl * 100.0)


# v1.0.3: the trail is measured in % of collateral (margin/ROE), so a fixed 20/15
# is a 4%/3% price move at 5x but 0.4%/0.3% at 50x (noise). Scale both by leverage
# relative to PERP_TRAIL_REF_LEVERAGE (5x keeps the base values exactly).
#   PERP_TRAIL_LEV_SCALE: sqrt (default) | linear (same price move at every leverage) | off
# Factor never drops below 1 (low leverage keeps the base values); giveback capped at 50.
PERP_TRAIL_REF_LEVERAGE = _env_float("PERP_TRAIL_REF_LEVERAGE", "TRAIL_REF_LEVERAGE", 5.0)
PERP_TRAIL_LEV_SCALE    = str(os.environ.get("PERP_TRAIL_LEV_SCALE", "sqrt") or "sqrt").strip().lower()
PERP_TRAIL_DROP_MAX     = 50.0
# v1.0.3: warn at open when the SL sits past this fraction of the entry->liq distance (0 = off). Warn only.
PERP_SL_LIQ_WARN_FRAC   = _env_float("PERP_SL_LIQ_WARN_FRAC", "SL_LIQ_WARN_FRAC", 0.6)


def trail_lev_factor(leverage, mode=None, ref=None):
    """Multiplier for margin-% trail levels at this leverage (>= 1)."""
    try:
        m = (mode or PERP_TRAIL_LEV_SCALE or "sqrt").lower()
        r = float(ref if ref is not None else PERP_TRAIL_REF_LEVERAGE) or 5.0
        lv = float(leverage or 0)
        if m == "off" or lv <= 0:
            return 1.0
        x = lv / r
        f = x if m == "linear" else math.sqrt(x)
        return max(1.0, f)
    except Exception:
        return 1.0


def trail_levels(leverage, peak_pct=0.0, pos=None, base_activate=None, base_drop=None):
    """(activate_pct, drop_pct) in % of collateral after fees, scaled by leverage.
    A per-position trail_activate_pct / trail_drop_pct (update_params) is used as given."""
    pos = pos or {}
    f = trail_lev_factor(leverage)
    if pos.get("trail_activate_pct"):
        act = float(pos["trail_activate_pct"])
    else:
        act = float(base_activate if base_activate is not None else TRAIL_ACTIVATE_PCT) * f
    if pos.get("trail_drop_pct"):
        drop = float(pos["trail_drop_pct"])
    else:
        drop = min(PERP_TRAIL_DROP_MAX, float(base_drop if base_drop is not None else TRAIL_DROP_PCT) * f)
    try:
        for _pk, _dr in PERP_TRAIL_TIERS:
            if float(peak_pct or 0) >= _pk * f:
                drop = min(drop, min(PERP_TRAIL_DROP_MAX, _dr * f))
    except Exception:
        pass
    return round(act, 2), round(drop, 2)


def margin_pct(price_move_pct, leverage):
    """Price move % -> % of collateral (before fees)."""
    try:
        return float(price_move_pct) * float(leverage or 1)
    except Exception:
        return None


def tp_sl_label(kind, level, entry, direction, leverage):
    """'TP $182.50 (+2.0% price, +40% margin at 20x)'. kind: 'tp' | 'sl'."""
    k = kind.upper()
    try:
        if not level:
            return f"{k}: none"
        lv, e = float(level), float(entry)
        mv = (lv - e) / e * 100 if direction == "long" else (e - lv) / e * 100
        mg = margin_pct(mv, leverage)
        return f"{k}: ${lv:,.2f} ({mv:+.1f}% price, {mg:+.0f}% margin at {float(leverage):g}x)"
    except Exception:
        return f"{k}: ${level}"


def tp_sl_pcts(level, entry, direction, leverage):
    """(price_move_pct, margin_pct) for a TP/SL level, or (None, None)."""
    try:
        lv, e = float(level), float(entry)
        mv = (lv - e) / e * 100 if direction == "long" else (e - lv) / e * 100
        return round(mv, 3), round(mv * float(leverage or 1), 2)
    except Exception:
        return None, None


def sl_liq_fraction(entry, sl, liq, direction):
    """How far the SL sits along entry->liq (0 = at entry, 1 = at liq). None if unknown."""
    try:
        e, s_, l = float(entry), float(sl), float(liq)
        span = (e - l) if direction == "long" else (l - e)
        dist = (e - s_) if direction == "long" else (s_ - e)
        if span <= 0:
            return None
        return dist / span
    except Exception:
        return None


def effective_trail_drop(peak_pct, base_drop=None, pos=None):
    """Giveback (pp) that fires the trail sensor at this peak. A per-position
    trail_drop_pct set via update_params overrides the base, tiers still tighten."""
    try:
        base = float((pos or {}).get("trail_drop_pct") or
                     (base_drop if base_drop is not None else TRAIL_DROP_PCT))
        d = base
        for _pk, _dr in PERP_TRAIL_TIERS:
            if float(peak_pct or 0) >= _pk:
                d = min(d, _dr)
        return d
    except Exception:
        return float(TRAIL_DROP_PCT)

# ============================================================================
#   LIVE-UPDATE (element e) - TRADING-EXECUTOR variant, STRICT safe-point
# ============================================================================
# perps_bot manages live leveraged positions, so it must NEVER re-exec mid-trade.
# The reload fires ONLY at a safe point: between monitor cycles, when
# _PERP_OP_IN_FLIGHT == 0 (no open/close/adjust and no on-chain send running) and
# past a min-uptime. Every money entry point is decorated to hold the counter up for
# the whole async operation. Otherwise the simple-bot core (no browser, Ctrl-C normal).
# Floor: reload only a patch already on disk; never fetch/self-modify/choose code.
import sys as _lu_sys
import shutil as _lu_shutil
import hashlib as _lu_hashlib
import time as _lu_time

_PERP_OP_IN_FLIGHT = 0  # >0 while any position open/close/adjust (incl tx sign+send) runs


def _perp_inflight(fn):
    """Decorator: hold the in-flight counter up for the whole async op so live-update
    cannot re-exec mid-trade. try/finally guarantees the decrement even on error.
    2026-10-16: sync functions are wrapped synchronously (a sync fn wrapped as async
    returned an un-awaited coroutine and never ran)."""
    import inspect as _insp
    if not _insp.iscoroutinefunction(fn):
        def _ws(*a, **k):
            global _PERP_OP_IN_FLIGHT
            _PERP_OP_IN_FLIGHT += 1
            try:
                return fn(*a, **k)
            finally:
                _PERP_OP_IN_FLIGHT -= 1
        _ws.__name__ = getattr(fn, "__name__", "wrapped")
        _ws.__doc__ = fn.__doc__
        return _ws
    async def _w(*a, **k):
        global _PERP_OP_IN_FLIGHT
        _PERP_OP_IN_FLIGHT += 1
        try:
            return await fn(*a, **k)
        finally:
            _PERP_OP_IN_FLIGHT -= 1
    _w.__name__ = getattr(fn, "__name__", "wrapped")
    _w.__doc__ = fn.__doc__
    return _w


_LU_SELF     = os.path.abspath(__file__)
_LU_MARKER   = os.path.join(WS_BOT_LOGS, "perps_bot_reload.json")  # main bot_logs/ (not bots/bot_logs)
_LU_LASTGOOD = _LU_SELF + ".last-good"
_LU_MIN_UPTIME_S = 120  # do not reload in the first 2 min (let positions/state settle)


def _lu_mtime(p):
    try:
        return os.path.getmtime(p)
    except Exception:
        return 0.0


def _lu_hash(p):
    try:
        with open(p, "rb") as f:
            return _lu_hashlib.sha256(f.read()).hexdigest()[:8]
    except Exception:
        return ""


_LU_BOOT_MTIME = _lu_mtime(_LU_SELF)
_LU_BOOT_HASH  = _lu_hash(_LU_SELF)
_LU_BOOT_TS    = _lu_time.time()


def _lu_snapshot_last_good():
    try:
        _lu_shutil.copy2(_LU_SELF, _LU_LASTGOOD)
    except Exception:
        pass


def _lu_write_report(kind, severity, summary, extra=None):
    try:
        # Live-update reports stay in the agent's inbox. Never owner_inbox.
        _inbox = os.path.join(os.path.dirname(os.path.dirname(_LU_SELF)), "inbox")
        os.makedirs(_inbox, exist_ok=True)
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        fpath = os.path.join(_inbox, f"perps_bot - {kind}_{ts}.md")
        fm = {"kind": kind, "severity": severity, "dedupe_key": f"perps_bot:{kind}",
              "source_skill": "perps_bot", "summary": summary}
        lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", "",
                 f"# perps_bot {kind}", summary]
        if extra:
            lines += ["", "```json", json.dumps(extra, indent=2, default=str), "```"]
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"[perps] live-update report failed: {e}")


def _lu_on_boot():
    marker = None
    try:
        if os.path.exists(_LU_MARKER):
            with open(_LU_MARKER, "r", encoding="utf-8") as f:
                marker = json.load(f)
    except Exception:
        marker = None
    if isinstance(marker, dict):
        try:
            with open(PERP_POSITIONS_FILE, encoding="utf-8-sig") as _pf:
                _st = json.load(_pf) or {}
            _ad = {k: {"direction": v.get("direction"), "entry": v.get("entry_price"), "size_usd": v.get("size_usd"),
                       "leverage": v.get("leverage"), "liq": v.get("liq_price"), "tp": v.get("tp_price"),
                       "sl": v.get("sl_price")} for k, v in _st.items() if isinstance(v, dict) and v.get("direction")}
        except Exception as _ae:
            _ad = {"_error": str(_ae)[:200]}
        marker["readopted_positions"] = _ad
        print(f"[boot] live-update: re-adopted {len([k for k in _ad if not k.startswith('_')])} position(s) "
              f"from perp_positions.json: {', '.join(f'{k} {v.get('direction')}' for k, v in _ad.items() if isinstance(v, dict)) or 'none'} - monitor resumes")
        try:
            with open(os.path.join(os.path.dirname(_LU_MARKER), "perps_reload_log.jsonl"), "a", encoding="utf-8") as _rl:
                _rl.write(json.dumps({"at": datetime.now(EST).isoformat(), **marker}, default=str) + "\n")
        except Exception:
            pass
        # Folded into the single "started" alert (no separate inbox item).
        globals()["_BOOT_RELOAD_NOTE"] = (f"Live-update reload {marker.get('from_hash')} -> {marker.get('to_hash')}, "
                                          f"re-adopted {len([k for k in (marker.get('readopted_positions') or {}) if not str(k).startswith('_')])} position(s)")
        try:
            os.remove(_LU_MARKER)
        except Exception:
            pass
    _lu_snapshot_last_good()


def _lu_spawn_relaunch_check(new_hash):
    """2026-10-16: the sub-bots (warden included) stop during a relaunch, so a
    detached checker watches the NEW process: if it has not booted (marker still
    present) or its heartbeat is stale after the wait, it sends a Telegram alert
    and writes an inbox note so the agent alerts too."""
    try:
        import subprocess as _lu_sp2
        _w = os.path.join(os.path.dirname(_LU_SELF), "perps_heartbeat_watch.py")
        if not os.path.exists(_w):
            print("[perps] live-update: perps_heartbeat_watch.py missing - no relaunch check")
            return
        _hb = os.path.join(globals().get("WS_BOT_LOGS") or os.path.dirname(_LU_MARKER), "perps_bot_heartbeat.json")
        _ib = os.path.join(os.path.dirname(os.path.dirname(_LU_SELF)), "inbox")
        _cmd = [_lu_sys.executable, _w, "--relaunch-check", "--hash", str(new_hash), "--from-hash", str(_LU_BOOT_HASH),
                "--marker", _LU_MARKER, "--hb", _hb, "--inbox", _ib, "--wait", "180"]
        _fl = 0
        if os.name == "nt":
            _fl = getattr(_lu_sp2, "DETACHED_PROCESS", 0x8) | getattr(_lu_sp2, "CREATE_NEW_PROCESS_GROUP", 0x200)
        _lu_sp2.Popen(_cmd, cwd=os.path.dirname(_LU_SELF), creationflags=_fl,
                      stdout=_lu_sp2.DEVNULL, stderr=_lu_sp2.DEVNULL, stdin=_lu_sp2.DEVNULL, close_fds=True)
        print("[perps] live-update: relaunch checker started (alerts if the new process does not come up)")
    except Exception as _we:
        print(f"[perps] live-update: relaunch checker spawn err: {_we}")


def _lu_reload_now():
    new_hash = _lu_hash(_LU_SELF)
    _lu_snapshot_last_good()
    try:
        os.makedirs(os.path.dirname(_LU_MARKER), exist_ok=True)
        with open(_LU_MARKER, "w", encoding="utf-8") as f:
            json.dump({"from_hash": _LU_BOOT_HASH, "to_hash": new_hash,
                       "detected_at": datetime.now(EST).isoformat(), "lastgood": _LU_LASTGOOD}, f)
    except Exception:
        pass
    print(f"[perps] live-update: reloading {_LU_BOOT_HASH} -> {new_hash} (safe point, no op in flight, re-exec)")
    # Stop this process's sub-bots first so the re-exec never orphans them.
    _stopper = globals().get("_STOP_ALL")
    try:
        if callable(_stopper):
            _stopper("live_update")
    except Exception as _se:
        print(f"[perps] live-update: sub-bot stop err: {_se}")
    try:
        _ui = globals().get("_PERPS_UI_MOD")
        if _ui is not None:
            _ui.deactivate()
    except Exception:
        pass
    _lu_spawn_relaunch_check(new_hash)
    # 2026-10-16: under the Windows supervisor, exit with the reload code and let
    # the supervisor start the new code in the same console (no os.execv).
    if os.environ.get("PERPS_SUPERVISED") == "1":
        print("[perps] live-update: handing back to the supervisor for a same-window restart")
        try:
            _lu_sys.stdout.flush()
        except Exception:
            pass
        os._exit(globals().get("PERPS_RELOAD_EXIT_CODE", 75))
    # 2026-10-16: reload via os.execv in the SAME
    # window (no new console). Sub-bots were stopped above; the new process starts
    # them again at boot and re-adopts positions from perp_positions.json.
    try:
        _lu_sys.stdout.flush()
        _argv = [_lu_sys.executable, _LU_SELF] + _lu_sys.argv[1:]
        if os.name == "nt":
            # Windows execv does not quote: a path with spaces ("GitHub Launches")
            # would split and the new process would die at start.
            _argv = [f'"{a}"' if (" " in a and not a.startswith('"')) else a for a in _argv]
        os.execv(_lu_sys.executable, _argv)
    except Exception as e:
        print(f"[perps] live-update: relaunch failed, staying on current code: {e}")
        try:
            if callable(globals().get("start_all")):
                globals()["_STOP_ALL"] = start_all()   # bring the sub-bots back
        except Exception:
            pass
        _lu_write_report("bot_restart_failed", "high",
                         f"perps_bot patch {new_hash} detected but re-exec failed; STILL RUNNING old {_LU_BOOT_HASH}",
                         {"error": str(e)[:200]})
        try:
            os.remove(_LU_MARKER)
        except Exception:
            pass


def _lu_maybe_reload():
    """Trading-executor safe-point check. Reloads ONLY when a patch is on disk AND no
    position operation is in flight AND past min-uptime. Called from the heartbeat loop
    (between cycles), never from an execution path. A reload with an OPEN position but
    no op in flight is safe: the position lives on-chain and the reloaded bot re-reads
    positions.json and resumes monitoring."""
    if (_lu_time.time() - _LU_BOOT_TS) < _LU_MIN_UPTIME_S:
        return
    if _PERP_OP_IN_FLIGHT != 0:
        return  # a trade is executing - NEVER reload mid-operation
    if _lu_mtime(_LU_SELF) > _LU_BOOT_MTIME and _lu_hash(_LU_SELF) != _LU_BOOT_HASH:
        # 2026-10-16: reload even with positions open. They live on-chain and the
        # new process re-adopts them from perp_positions.json (logged at boot). Only an
        # op in flight waits (checked above). A patch that does not compile never relaunches.
        _h = _lu_hash(_LU_SELF)
        if globals().get("_LU_BAD_HASH") == _h:
            return
        try:
            with open(_LU_SELF, encoding="utf-8") as _sf:
                compile(_sf.read(), _LU_SELF, "exec")
        except Exception as _ce:
            globals()["_LU_BAD_HASH"] = _h
            print(f"[perps] live-update: patch {_h} does NOT compile, staying on {_LU_BOOT_HASH}: {_ce}")
            _lu_write_report("bot_restart_failed", "high",
                             f"perps_bot patch {_h} does not compile; STILL RUNNING old {_LU_BOOT_HASH}",
                             {"error": str(_ce)[:300]})
            return
        _lu_reload_now()
TP_ALERT_BUFFER_PCT = 2.0
# 2026-10-16: warnings fire ONCE per position (persisted on the position,
# survives reboots) and only when actually near. Distances are % of price.
PERP_LIQ_WARN_PCT = float(_envv("PERP_LIQ_WARN_PCT", "5"))  # absolute cap
# 2026-10-16: the liq warning scales with leverage. It fires when the distance to
# liq drops under this FRACTION of the distance at entry (0.5 = half the cushion
# gone), capped by PERP_LIQ_WARN_PCT. A 40x open (~2.2% cushion) no longer warns
# the instant it fills.
PERP_LIQ_WARN_FRAC = float(_envv("PERP_LIQ_WARN_FRAC", "0.5"))
PERP_SL_WARN_PCT = float(_envv("PERP_SL_WARN_PCT", "0.5"))
SL_ALERT_BUFFER_PCT = 2.0
SOL_PERP_MARKET_INDEX = 0
EST  = ZoneInfo("America/New_York")
PERP_RELAY          = os.path.join(WS_JSON_RELAY, "perp_signal.json")
# Sprint 46: per-chain signal files so the agent can fire SOL/BTC/ETH simultaneously
# without one overwriting another. The chain is forced from the filename.
PERP_RELAY_SOL      = os.path.join(WS_JSON_RELAY, "perp_sol_signal.json")
PERP_RELAY_BTC      = os.path.join(WS_JSON_RELAY, "perp_btc_signal.json")
PERP_RELAY_ETH      = os.path.join(WS_JSON_RELAY, "perp_eth_signal.json")
_PERP_RELAYS = [
    (PERP_RELAY,     None),        # legacy single-signal; market comes from the payload
    (PERP_RELAY_SOL, "SOL-PERP"),  # per-chain; market forced from filename
    (PERP_RELAY_BTC, "BTC-PERP"),
    (PERP_RELAY_ETH, "ETH-PERP"),
]
# ── PATH ANCHOR (2026-07-24, the two-bot_logs incident): ten constants below
# were CWD-relative ("bot_logs/...") while the s27 lane was absolute — launch
# the bot from any directory but bots\ and the relative set silently forks to
# a SECOND bot_logs (The owner's perp_positions.json frozen at 7/14 was the stale
# twin). Every file now anchors to the script's own directory. The launch
# location must never matter again.
_BOT_HOME = os.path.dirname(os.path.abspath(__file__))

BOT_LOGS            = os.path.join(WS_BOT_LOGS, "perps.log")
PNL_FILE            = os.path.join(WS_BOT_LOGS, "paper_perp_pnl.json" if PERPS_DRY_RUN else "perp_pnl.json")

# ── FEED HOOK ──────────────────────────────────────────────────────────────
# Every outside-feed read goes through _feed(name) -> bots/feed_hook.py read_feed(),
# which serves the last Flow pull from bot_logs/flow_cache.json (never the network).
# Pulls happen only on demand (FEED_CADENCE=manual) or on the user's FEED_CADENCE
# minutes; live odds are fetched at every packet build (bots/feeds/feed_router.py).
try:
    from feed_hook import read_feed as _read_feed, cached as _flow_cached
except Exception:
    def _read_feed(name):
        return None

    def _flow_cached():
        return None
try:
    from feeds import feed_router as _feed_router
except Exception as _fr_err:
    _feed_router = None
    print(f"[feeds] router unavailable: {_fr_err}")


def _feed(name, default=None):
    """Value from the feed hook, or `default` when the hook returns nothing. Never raises."""
    try:
        v = _read_feed(name)
    except Exception:
        v = None
    return default if v is None else v


def _feed_with_age(name):
    """(data_or_None, age_minutes_or_None) for the packet's freshness tags."""
    v = _feed(name)
    if v is None:
        return (None, None)
    return (v, (_flow_cached() or {}).get("age_min"))
# ──────────────────────────────────────────────────────────────────────────
NEWS_CATALYSTS_FILE = os.path.join(WS_BOT_LOGS, "news_catalysts.json")
POLYMARKET_FILE     = os.path.join(WS_BOT_LOGS, "polymarket_markets.json")
PERP_DECISION_LOG   = os.path.join(WS_BOT_LOGS, "perp_decisions.json")

# ════════════════════════════════════════════════════════════════════════════
# Sprint 43 / Build B: feed-driven proposal pipeline (RETIRED: not started)
# ════════════════════════════════════════════════════════════════════════════
# Historical: proposed per-market trades to the agent from the feed's regime
# read. Its loops are no longer started; the only entry path is an agent
# signal file through process_signal (gated by NEW_POSITIONS_ENABLED).

# Cadence: how often feed_poller_loop checks macro.json and proposes

# Drift perp market indices — env-overridable so the owner can correct any wrong
# defaults at launch without code edits. SOL_PERP_MARKET_INDEX (existing
# constant = 0) stays as-is for the legacy process_signal path.
MARKET_INDEX_SOL = 0
MARKET_INDEX_BTC = 1
MARKET_INDEX_ETH = 2
MARKET_INDEX_JUP = 24

# All perp markets perps_bot trades + tracks. Every one is LIVE — no demo,
# no stubs. Priced off the Jupiter/Pyth oracle on every read.
# NOTE: verify these indexes against Drift mainnet if a market ever fails to
# subscribe/fetch (esp. JUP=24) — a wrong index degrades to fetch_error, not a crash.
ALL_PERP_MARKETS = [
    {"symbol": "SOL-PERP", "index": MARKET_INDEX_SOL, "base": "SOL", "tier": "major"},
    {"symbol": "BTC-PERP", "index": MARKET_INDEX_BTC, "base": "BTC", "tier": "major"},
    {"symbol": "ETH-PERP", "index": MARKET_INDEX_ETH, "base": "ETH", "tier": "major"},
]
ALL_PERP_INDEXES = [m["index"] for m in ALL_PERP_MARKETS]

# Regime → leverage budget multiplier (matches feed horse_race output)
# Source: build_B_perps_pipeline_spec.md §3
REGIME_LEVERAGE_BUDGETS = {
    "zero":                       0.0,   # don't trade
    "small":                      0.3,
    "moderate":                   0.6,
    "moderate_with_forecast":     0.6,
    "moderate_against_forecast":  0.6,
    "neutral":                    0.5,
    "high":                       1.0,
    "high_directional_long":      1.0,
    "high_directional_short":     1.0,
}

# Base leverage (Drift max). Multiplied by regime_multiplier × per_market_scale.
BASE_LEVERAGE_FOR_REGIME   = 40.0

# Per-market scales — JUP gets dialed down because liquidity is thin
REGIME_SCALE_BTC = 1.0
REGIME_SCALE_SOL = 0.85
REGIME_SCALE_ETH = 0.9
REGIME_SCALE_JUP = 0.6

# Proposal expiry — how long the agent has to act before proposal expires
PROPOSAL_EXPIRY_HOURS      = 3.0

# Default trade size (The owner's micro-bet model)
DEFAULT_SIZE_USD           = 5.0

# Inbox for proposal packets (The agent reads these)

# State file: track when we last wrote a proposal so we don't spam the agent

# ── Build B action watcher ────────────────────────────────────────────────
# The agent's response file. He writes per-market decisions here.

# Brain attribution log — JSONL mapping packet_id → fired trade → outcome.
# Build A's manipulation_reveal trigger consumes this.

# Action watcher polling cadence

# Mark-price drift warning: if mark moved this % between proposal write and
# The agent reply, log a warning. Real divergence between proposal-time and
# fire-time mark; useful for tuning proposal expiry.


# ────────────────────────────────────────────────────────────────────────────
# Sprint 33 (this session): DAILY_READ.JSON CONSUMPTION
# ────────────────────────────────────────────────────────────────────────────
# feed writes daily_read.json every sweep (~4h). perps_bot reads it
# before opening positions and gates behavior on:
#   - trade_recommendation.perps: flat/neutral/long_*/short_*  + _small/_medium/_aggressive
#   - trade_recommendation.abort_conditions: list (non-empty = halt new positions)
#   - trade_recommendation.exit_priority_signals: list (force-exit triggers)
#   - regime_break_signal: bool (halt)
#
# Doctrine (locked memory entry):
#   - daily_read is the BASELINE read, not an absolute order
#   - The agent's meeting files / explicit overrides still win
#   - Every gating decision writes an audit packet to inbox_results/done/ for
#     The agent to review, NEVER back into inbox/ (per locked inbox hygiene)
#   - Cache daily_read for ~10min (it updates every 4h, no point re-reading per signal)

INBOX_RESULTS_DONE  = os.path.join(WS_ROOT, "inbox_results", "done")

# Sprint 44+: The agent's published editorial calls (his feed-site thesis). Same
# dir as daily_read. Surfaced in the perp packet so the agent sees his OWN public
# call when deciding , every perp trade validates or contradicts it. REFERENCE
# ONLY, never a gate. Best-effort: absent/malformed file -> no editorial context.
# Items relevant to a directional perp decision: his published directional lean,
# cycle phase, and synthesized read. (curated/voice/winning_line/pattern_match omitted.)
_EDITORIAL_PERP_ITEMS = ("leverage_bias", "phase", "the_read")

# Daily read cache (in-memory, refreshed every 10min OR on force_refresh)
_DAILY_READ_CACHE: dict = {
    "_loaded_at_ts": 0.0,    # Unix timestamp of last load
    "_file_mtime":   0.0,    # File mtime at last load (for change detection)
    "data":          None,   # Cached daily_read dict (None if never loaded or load failed)
}
_DAILY_READ_TTL_SECONDS = 600  # 10min cache

# Aggression tier → size multiplier (applied to requested size_usd in new positions)
# The agent's upgrade #7 (2026-07-23): soft perp budget. None = no cap (size off
# usable share as always); a number = the mental split is now a field, so the
# perps has its own number. Advisory — never a hard gate.
PERP_BUDGET_USD = None

# PERP_SOL_SHARE = fraction of the SOL wallet it's reasonable to commit to a
# perp, leaving the rest as dry powder. Conservative — a perp should
# NOT consume the whole wallet. Not a hard cap (no code-level split exists yet);
# it's the "usable" figure surfaced in the packet so the agent sizes against a shared
# pool, not the full balance.
_PERP_SOL_SHARE = 0.50

# Conservative defaults — the system's win rate is still being established, so even aggressive
# is capped at 1.0x (no upsize, only downsize from full request).
_AGGRESSION_SIZE_MULTIPLIER = {
    "micro":      0.15,   # Sprint 50: anticipatory micro-lean (feed flat-trend front-run).
    "small":      0.25,   #   Smallest, least-confirmed entry: score is flat, only the
    "medium":     0.50,   #   regime vote is leaning. Tight stop, graded on its own lane.
    "aggressive": 1.00,
}

# ── Sprint 43: daily_read degraded-mode policy ──────────────────────────────
# When feed's daily_read is MISSING (never loaded) or STALE (older than
# N sweep-intervals), perps doesn't trade blind at full size, and it doesn't
# freeze either. It enters DEGRADED mode: caps new entries to small-tier
# notional (the conservative middle). The agent can still override per-signal with
# override_daily_read:true. Self-recovers the moment a fresh daily_read lands.
_DAILY_READ_STALE_MAX_HOURS        = 24     # degrade on stale only if older than this
                                            # (feed healthy at any cadence ≤24h is always fresher;
                                            #  this fires only when feed is genuinely dead ~a day)
_DEGRADED_DIRECTION_TIER           = "small"  # treat missing/stale as small-tier in requested direction

# Module-level transition tracker so the degrade/recovery ping fires ONCE per
# transition, not every signal (no ping spam while degraded).
_DAILY_READ_DEGRADE_STATE = {"degraded": False}


def _daily_read_freshness(dr: dict | None) -> tuple[str, float | None, float | None]:
    """Classify daily_read freshness.

    Returns (state, age_sec, threshold_sec) where state is one of:
      "missing" — dr is None (never loaded / file absent)
      "stale"   — dr exists but _updated is older than the staleness threshold
      "fresh"   — dr exists and is within threshold

    Threshold is a flat ceiling (_DAILY_READ_STALE_MAX_HOURS, default 24h). At
    any healthy feed cadence (<=24h) daily_read is always fresher than this, so
    "stale" only fires when feed has been genuinely dead for ~a day. The
    "missing" case (None) is immediate regardless — that's the harder failure.
    """
    if dr is None:
        return ("missing", None, None)

    threshold_sec = _DAILY_READ_STALE_MAX_HOURS * 3600.0

    updated = dr.get("_updated")
    if not updated:
        # No timestamp — can't verify freshness; treat as stale (degraded, safe)
        return ("stale", None, threshold_sec)
    try:
        from datetime import datetime as _dt
        u = updated[:-1] + "+00:00" if updated.endswith("Z") else updated
        updated_dt = _dt.fromisoformat(u)
        age_sec = (datetime.now(timezone.utc) - updated_dt.astimezone(timezone.utc)).total_seconds()
    except Exception:
        return ("stale", None, threshold_sec)

    if age_sec > threshold_sec:
        return ("stale", round(age_sec, 1), threshold_sec)
    return ("fresh", round(age_sec, 1), threshold_sec)

# Sprint 32 v2.7: morning packet (sequenced after feed's 6:00am at 6:05am EST)
# Per the owner's lock: perps_bot WRITES this brief to the agent's inbox. It does NOT
# create meeting files. The agent is the only initiator of meeting files.
INBOX_DIR                   = os.path.join(WS_ROOT, "inbox")
PERPS_MORNING_PACKET_FILE   = os.path.join(INBOX_DIR, "perps_morning_brief.json")
PERPS_MORNING_STATE_FILE    = os.path.join(WS_BOT_LOGS, "perps_morning_state.json")
FEED_MORNING_PACKET_FILE    = None  # morning thesis comes from the Flow feed hook
PERP_SIGNAL_FILE_FOR_HORSE  = os.path.join(WS_JSON_RELAY, "perp_signal.json")

# ── Sprint 32+: meeting files (The agent-initiated only, transient) ─────────────
# DOCTRINE: perps_bot does NOT auto-create perp_meeting.json. The file appears ONLY
# when the agent writes one. Bot's meeting watcher polls os.path.exists (cheap, no LLM).
# When file appears: wake the SI brain (optional) → interpret → apply override to runtime
# memory → write reply packet to inbox → DELETE meeting file. Folder returns to empty.
# LLM stays asleep when no meeting file is present (VRAM is free).
PERP_MEETING_FILE        = os.path.join(WS_JSON_RELAY, "perp_meeting.json")
PERP_OVERRIDES_SNAPSHOT  = os.path.join(WS_BOT_LOGS, "perp_overrides_snapshot.json")
PERP_MEETING_AUDIT_LOG   = os.path.join(WS_BOT_LOGS, "perp_meeting_audit.jsonl")

# ── Sprint 44 — TUNING SYSTEM. bots\tuner is the source of truth. ───────────
# Resolution order (see _perp_get_override): active TTL override > saved default
# (tuner) > code default (TUNABLE_PARAMS). Saved defaults survive expiry + restart.
TUNER_DIR                = os.path.join(WS_BOTS, "tuner")
PERPS_PARAMS_FILE         = os.path.join(TUNER_DIR, "perps_params.json")
PERPS_TUNED_DEFAULTS_FILE = os.path.join(TUNER_DIR, "perps_tuned_defaults.json")
PERPS_TUNE_HISTORY_FILE   = os.path.join(TUNER_DIR, "tune history", "perps_tune_history.json")

# Ollama for meeting interpretation. Stays asleep until a meeting file appears.
OLLAMA_URL              = os.getenv("OLLAMA_URL", "http://localhost:11434")   # structural (locked) — LOCAL fallback
PERPS_OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "qwen3:14b-q4_K_M")   # any local Ollama model (OLLAMA_MODEL)
PERPS_OLLAMA_FALLBACK   = PERPS_OLLAMA_MODEL
PERPS_OLLAMA_TIMEOUT_S  = 60.0
PERPS_MEETING_POLL_S    = 30   # Sprint 44: code default

PERPS_GROK_TIMEOUT_S = 45.0   # Grok call timeout. LLM chain: Grok main, local Ollama fallback (OLLAMA_URL/OLLAMA_MODEL).
print("[llm] SI brain: LLM_MAIN=" + (os.getenv("LLM_MAIN") or "grok") + ", LLM_FALLBACK=" + (os.getenv("LLM_FALLBACK") or "ollama") + " (off = brain jobs skip; trading unaffected)")

# ── Sprint 39 — Jupiter failover + decline feedback loop ────────────────────
# Audit + state files
_BOT_LOGS_ROOT_S39       = WS_BOT_LOGS
PERPS_DECLINE_LOG_FILE          = os.path.join(_BOT_LOGS_ROOT_S39, "perps_decline_log.jsonl")
PERPS_DECLINE_PATTERNS_FILE     = os.path.join(_BOT_LOGS_ROOT_S39, "perps_decline_patterns.json")
PERPS_CONFIRM_SIGNAL_LOG_FILE   = os.path.join(_BOT_LOGS_ROOT_S39, "perps_confirm_signal_log.jsonl")
PERPS_AUTO_TIGHTEN_AUDIT_LOG    = os.path.join(_BOT_LOGS_ROOT_S39, "perps_auto_tighten_audit.jsonl")
PERPS_VENUE_AUDIT_LOG           = os.path.join(_BOT_LOGS_ROOT_S39, "perps_venue_audit.jsonl")
PERPS_VENUE_HEALTH_STATE_FILE   = os.path.join(_BOT_LOGS_ROOT_S39, "perps_venue_health.json")

# Decline schema enum (the 7 valid skip reasons for perps)
# Filter-related = could be fixed by tuning a threshold; counts toward auto-tighten strikes
# Calibration-justified = correct response, do NOT count toward strikes
PERPS_VALID_SKIP_REASONS = {
    # Filter-related (count toward strikes):
    "score_outside_threshold",       # score didn't cross LONG_SCORE_MAX / SHORT_SCORE_MIN
    "fg_outside_threshold",          # fear/greed didn't cross LONG_FG_MAX / SHORT_FG_MIN
    "edge_below_costs",              # edge < estimated fees/slippage
    "data_freshness_stale",          # feed/macro >90min old (and probe didn't help)

    # Calibration-justified (excluded from strikes):
    "low_conviction_correct_skip",   # signal exists but conviction low; sizing-down was correct
    "authority_threshold_violated",  # AGENT AUTHORITY THRESHOLD breach (liq_in_vols < 2.0 or lev > 8x)
    "venue_unavailable",             # both Drift and Jupiter unhealthy; can't execute anyway
}
PERPS_FILTER_RELATED_SKIP_REASONS = {
    "score_outside_threshold",
    "fg_outside_threshold",
    "edge_below_costs",
    "data_freshness_stale",
}
PERPS_CALIBRATION_SKIP_REASONS = {
    "low_conviction_correct_skip",
    "authority_threshold_violated",
    "venue_unavailable",
}
# Map decline_reason → tunable param the auto-tighten would adjust
PERPS_AUTO_TIGHTEN_PARAM_MAP = {
    # decline_reason: (tunable_param, delta)
    "score_outside_threshold": [
        ("LONG_SCORE_MAX",  -2.0),   # lower long max = stricter long entry
        ("SHORT_SCORE_MIN", +2.0),   # raise short min = stricter short entry
    ],
    "fg_outside_threshold": [
        ("LONG_FG_MAX",  -2.0),
        ("SHORT_FG_MIN", +2.0),
    ],
    # edge_below_costs + data_freshness_stale don't auto-map cleanly — handled
    # via per-market auto-disable rather than threshold tweak
}

# Auto-tighten thresholds (kept gentle because perps is
# leverage; tightening = fewer trades = real opportunity cost)
PERPS_AUTO_TIGHTEN_STRIKE_THRESHOLD = 4
PERPS_AUTO_TIGHTEN_STRIKE_WINDOW_H  = 48
PERPS_AUTO_TIGHTEN_COOLDOWN_H       = 24
# Per-market auto-disable thresholds
PERPS_AUTO_DISABLE_STRIKE_THRESHOLD = 5    # 5 consecutive 100%-skip scans
PERPS_AUTO_DISABLE_WINDOW_H         = 48
PERPS_AUTO_DISABLE_COOLDOWN_H       = 24
# Loop cadences
PERPS_DECLINE_PATTERNS_REFRESH_S    = 30 * 60
PERPS_AUTO_TIGHTEN_TICK_S           = 30 * 60
PERPS_VENUE_HEALTH_PROBE_S          = 30 * 60

# ── Venue config — SPRINT 57: DRIFT REMOVED. Jupiter is the only venue. ──────
# The whole Drift-primary/Jupiter-failover apparatus is gone: there is no second
# venue to fail over TO, no health probe, no "both venues down" state. Jupiter is
# not "primary" — it is the venue. The failover/primary constants below are kept
# only so any stale override file or leftover reference resolves to a Jupiter-only
# answer instead of raising; nothing branches on them anymore.
PERPS_FAILOVER_DEFAULT_ENABLED      = False  # dead: nothing to fail over to
PERPS_PRIMARY_VENUE_DEFAULT         = "jupiter"  # dead: Jupiter is the only venue
JUPITER_PERPS_MAX_SLIPPAGE_BPS      = 50

# Jupiter venue knobs. REGIME_SCALE was 0.75 to shrink FAILOVER trades (degraded
# confidence when Drift fell over). With Jupiter as THE venue there is nothing to
# shrink against — full size is correct. Now 1.0.
JUPITER_MAX_LEVERAGE       = PERP_MAX_LEVERAGE   # follows the user cap (bots/.env PERP_MAX_LEVERAGE); not a separate gate
# Jupiter's PHYSICAL max leverage (250x on majors as of 2026). This is a FACT about
# the venue — informational, shown on boot so the real headroom is visible. It is NOT
# the operational cap: that is PERP_MAX_LEVERAGE (bots/.env, unset = 20x). Two-tier:
# the owner's cap gates every signal; the venue owns the physical max.
JUPITER_VENUE_MAX_LEVERAGE = 250.0
JUPITER_MAX_POSITION_USD   = 500.0     # DEAD as a gate (kept for old references). The live caps are PERP_MAX_LEVERAGE and the optional PERP_MAX_COLLATERAL_SOL, applied in route_open_position and execute_position_add. Do not revive this flat usd ceiling.
JUPITER_REGIME_SCALE       = 1.0       # Sprint 57: was 0.75 (failover shrink); now full size
JUPITER_ENABLED_MARKETS    = ["SOL-PERP", "BTC-PERP", "ETH-PERP"]   # tunable: JUPITER_ENABLED_MARKETS

# ── Sprint 43: Jupiter auto-funding pipeline — SOL is the universal fuel ──────
# A failover trade funds itself from native SOL: wrap for longs, swap SOL->USDC
# for shorts, downsize to whatever's affordable. It just goes through.
JUPITER_SWAP_API_BASE      = "https://api.jup.ag/swap/v1"   # v6 quote-api is dead DNS; was the ExactOut USDC/short funding path
# 2026-07-23: Jupiter has been migrating off quote-api v6 — if the primary is
# dead/degraded the swap fails QUIETLY and funding returns $0. Ladder: try each
# base in order; same /quote + /swap shapes on both.
JUPITER_SWAP_API_LADDER    = ("https://api.jup.ag/swap/v1",)  # 2026-10: api.jup.ag is the only live host (quote-api v6 dead, lite-api retiring)
# Optional Jupiter Developer Platform key (x-api-key). Blank = keyless api.jup.ag (rate-limited, ~0.5 req/s),
# which is enough for the occasional funding swap. With a key we try keyed first, then keyless on failure.
JUPITER_API_KEY            = (os.getenv("JUPITER_API_KEY") or "").strip()
def _jup_attempts():
    """(base_url, headers) pairs to try in order; never logs the key."""
    out = []
    for _b in JUPITER_SWAP_API_LADDER:
        if JUPITER_API_KEY:
            out.append((_b, {"x-api-key": JUPITER_API_KEY}))
        out.append((_b, {}))
    return out
JUPITER_SWAP_SLIPPAGE_BPS  = 100   # 1% on the SOL->USDC funding swap
JUPITER_FUND_BUFFER_BPS    = 150     # overshoot funding 1.5% so rounding/fees don't leave us short
JUPITER_SOL_FEE_RESERVE    = 0.02  # native SOL kept back to pay tx fees/rent (physically required to submit)


# Markets covered (used by decline tracking + per-market auto-disable)
PERPS_COVERED_MARKETS = ("SOL-PERP", "BTC-PERP", "ETH-PERP")
PERPS_COVERED_MODES   = ("directional", "funding_arb", "squeeze_setup", "stand_down")

# ─── Jupiter Perps on-chain constants (Sprint 39 Option A — REAL execution) ─
# Verify at deploy via Solscan if any failures with InvalidAccountOwner / etc.
# These were the live mainnet addresses as of the Sprint 39 build research.
#
# Program ID — the deployed Jupiter Perpetuals program.
# Reference Solscan tag: "Jupiter Perpetuals Program"
JUPITER_PERPS_PROGRAM_ID = "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu"

# Pool account — the single global JLP pool used for all perp trades.
# Solscan: 5BUwFW4nRbftYTDMbgxykoFWqWHPzahFSNAaaaJtVKsq
JUPITER_PERPS_POOL_ACCOUNT = "5BUwFW4nRbftYTDMbgxykoFWqWHPzahFSNAaaaJtVKsq"

# Perpetuals authority/config account (PDA from program with seed "perpetuals")
# Solscan: H4ND9aYttUVLFmNypZqLjZ52FYiGvdEB45GmwNoKEjTj
JUPITER_PERPS_PERPETUALS_AUTHORITY = "H4ND9aYttUVLFmNypZqLjZ52FYiGvdEB45GmwNoKEjTj"

# Custody accounts (one per asset, both long and short collateral side).
# These are PDAs of the program — derived deterministically from pool + mint.
# Source: live mainnet Custody PDAs published in Jupiter docs as of Sprint 39 build.
# Verify at deploy: each is a `Custody` account owned by JUPITER_PERPS_PROGRAM_ID.
JUPITER_CUSTODIES = {
    # SOL custody (used for SOL longs; collateral_custody also = this for SOL longs)
    "SOL":   "7xS2gz2bTp3fwCC7knJvUWTEU9Tycczu6VhJYKgi1wdz",
    # ETH custody (used for ETH longs)
    "ETH":   "AQCGyheWPLeo6Qp9WpYS9m3Qj479t7R636N9ey1rEjEn",
    # wBTC custody (used for BTC longs)
    # 2026-07-29: corrected from dead 5Pv6wMkq... (did not exist on-chain, failed
    # every BTC open with AccountNotInitialized 3012). Live value read off Pool.
    "BTC":   "5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm",
    # USDC custody (used as collateral for all short positions)
    "USDC":  "G18jKKXQwBbrHeiK3C9MRXhkHsLHf7XgCSisykV46EZa",
    # USDT custody (alternative collateral for shorts when USDC custody is full)
    "USDT":  "4vkusE5pY7CvFVMqbVqyhmaCAo2vy79r5MGRpVKZmTfk",
}

# SPL token mints (mainnet)
JUPITER_TOKEN_MINTS = {
    "SOL":  "So11111111111111111111111111111111111111112",  # wrapped SOL
    "ETH":  "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # Wormhole ETH on Solana
    "BTC":  "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # Wormhole wBTC on Solana
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

# Doves oracle program (Edge by Chaos Labs) and oracle accounts per asset.
# These are the price oracle accounts the Jupiter Perps program reads for
# each token. Required for any open/close instruction.
JUPITER_DOVES_ORACLE_PROGRAM_ID = "DoVEsk76QybCEHQGzkvYPWLQu9gzNoZZZt3TPiL597e"
JUPITER_ORACLES = {
    "SOL": "39cWjvHrpHNz2SbXv6ME4NPhqBDBd4KsjUYv5JkHEAJU",
    "ETH": "5URYohbPy32nxK1t3jAHVNfdWY2xTubHiFvLrE3VhXEp",
    "BTC": "4HBbPx9QJdjJ7GUe6bsiJjGybvfpDhQMMPXP1UEa7VT5",
}

# Standard Solana program IDs needed for instruction building
SOLANA_SYSTEM_PROGRAM        = "11111111111111111111111111111111"
SOLANA_TOKEN_PROGRAM         = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOLANA_ASSOCIATED_TOKEN_PROG = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

# Position request configuration
JUPITER_REQUEST_TYPE_MARKET  = 0   # u8 enum in CreateIncreasePositionMarketRequestParams
JUPITER_SIDE_LONG            = 1   # u8 enum value for Side::Long
JUPITER_SIDE_SHORT           = 2   # u8 enum value for Side::Short

# Compute unit budget for Jupiter Perps instructions (empirical, conservative)
JUPITER_PERPS_CU_LIMIT       = 400_000
JUPITER_PERPS_CU_PRICE_MICRO = 50_000   # priority fee micro-lamports per CU

# Jupiter keeper execution window — keepers usually pick up within seconds.
# We poll for execution every 2s up to this many total seconds before giving up.
JUPITER_KEEPER_TIMEOUT_S     = 60

# IDL URL — fetched at startup to build instructions via anchorpy.
# This is the on-chain Anchor IDL; we use `anchor idl fetch` semantics via RPC.
# Fallback: a JSON file at this path can be used if RPC fetch fails.
JUPITER_PERPS_IDL_FALLBACK_PATH = os.path.join(WS_BOTS, "jupiter_perps_idl.json")

SCAN_INTERVAL_SECONDS = 900
# Two switches (tunable; code defaults from bots/.env, both true when unset):
#   NEW_POSITIONS_ENABLED - false = agent long/short/add signals are declined (reason new_positions_off).
#                           Closes, adjust, update_params, close_pct and pull_initial always work.
#   ENTRY_ALERTS_ENABLED  - false = the intel scanner stops sending entry alerts. Nothing else.
# The bot never opens a trade on its own; every trade is an agent signal file.
# Deprecated: AUTO_TRADE_ENABLED (old single switch) still sets BOTH when the new keys are unset.
def _perp_switch_env(name):
    v = _envv(name)
    return None if v is None else str(v).strip().lower() in ("1", "true", "yes", "on")
_NP_ENV, _EA_ENV, _LEGACY_SWITCH_ENV = (_perp_switch_env("NEW_POSITIONS_ENABLED"),
                                        _perp_switch_env("ENTRY_ALERTS_ENABLED"),
                                        _perp_switch_env("AUTO_TRADE_ENABLED"))
if _LEGACY_SWITCH_ENV is not None and (_NP_ENV is None or _EA_ENV is None):
    print("[perps] deprecated: AUTO_TRADE_ENABLED in bots/.env is read as NEW_POSITIONS_ENABLED + "
          "ENTRY_ALERTS_ENABLED; set those instead")
NEW_POSITIONS_ENABLED = _NP_ENV if _NP_ENV is not None else (_LEGACY_SWITCH_ENV if _LEGACY_SWITCH_ENV is not None else True)
ENTRY_ALERTS_ENABLED  = _EA_ENV if _EA_ENV is not None else (_LEGACY_SWITCH_ENV if _LEGACY_SWITCH_ENV is not None else True)
LONG_SCORE_MAX    = 30.0
SHORT_SCORE_MIN   = 72.0
LONG_FG_MAX       = 28.0
SHORT_FG_MIN      = 72.0
FUNDING_SHORT_MIN = 0.04
SCALP_SCORE_MOVE  = 8.0
AUTO_CLOSE_MIN_HOLD_HOURS = 4.0
LONG_SCORE_MAX_HOT    = 25
LONG_SCORE_MAX_DEAD   = 35
SHORT_SCORE_MIN_HOT   = 78
SHORT_SCORE_MIN_DEAD  = 68
SCALP_SCORE_MOVE_HOT  = 6
SCALP_SCORE_MOVE_DEAD = 10
SCALP_TP_PCT_HOT      = 0.10
SCALP_TP_PCT_REGULAR  = 0.15
def get_perp_thresholds() -> dict:
    MARKET_MODE_RELAY = os.path.join(WS_JSON_RELAY, "market_mode.json")
    mode = "regular"
    try:
        if os.path.exists(MARKET_MODE_RELAY):
            import time as _t, json as _j
            with open(MARKET_MODE_RELAY) as f:
                relay = _j.load(f)
            age = (_t.time() - relay.get("set_at", 0)) / 60
            if age < relay.get("expires_mins", 120):
                mode = relay.get("mode", "regular")
    except Exception:
        pass
    if mode == "hot":
        return {"long_score_max": LONG_SCORE_MAX_HOT, "short_score_min": SHORT_SCORE_MIN_HOT, "scalp_score_move": SCALP_SCORE_MOVE_HOT, "scalp_tp_pct": SCALP_TP_PCT_HOT, "mode": "hot"}
    elif mode == "dead":
        return {"long_score_max": LONG_SCORE_MAX_DEAD, "short_score_min": SHORT_SCORE_MIN_DEAD, "scalp_score_move": SCALP_SCORE_MOVE_DEAD, "scalp_tp_pct": SCALP_TP_PCT_REGULAR, "mode": "dead"}
    else:
        return {"long_score_max": LONG_SCORE_MAX, "short_score_min": SHORT_SCORE_MIN, "scalp_score_move": _perp_get_override("SCALP_SCORE_MOVE", SCALP_SCORE_MOVE), "scalp_tp_pct": SCALP_TP_PCT_REGULAR, "mode": "regular"}
AUTO_SIZE_USD   = _env_float("PERP_AUTO_SIZE_USD", "AUTO_SIZE_USD", 150)
AUTO_LEVERAGE   = _env_float("PERP_AUTO_LEVERAGE", "AUTO_LEVERAGE", 5)
AUTO_COLLATERAL = 30.0
os.makedirs("bot_logs", exist_ok=True)
_kp = Keypair.from_base58_string(PRIVATE_KEY)
def log(action, direction, size_usd, price, reason, tx_sig=None):
    ts    = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")
    entry = f"{ts} | {action} | {direction} | ${size_usd} | @{price} | {reason}"
    if tx_sig:
        entry += f" | tx: {tx_sig}"
    entry += "\n"
    with open(BOT_LOGS, "a", encoding="utf-8") as f:
        f.write(entry)
    print(entry.strip())
    if tx_sig and not str(action).startswith("open"):
        _perp_set_outcome("filled", tx=tx_sig, event=action, price=price, size_usd=size_usd)


# 2026-10-16: per-signal outcome. process_signal records what really happened
# (filled / declined / failed + tx); the relay loop turns it into the ack.
_PERP_SIG_OUTCOME = None


def _perp_set_outcome(status, **kw):
    global _PERP_SIG_OUTCOME
    cur = _PERP_SIG_OUTCOME
    if isinstance(cur, dict) and cur.get("status") in ("filled", "failed") and status == "declined":
        return  # a real result outranks a later decline note
    rec = {"status": status, "at": datetime.now(EST).isoformat()}
    rec.update({k: v for k, v in kw.items() if v is not None})
    _PERP_SIG_OUTCOME = rec


_PERP_SIG_NOTES = None
_PERP_REFUSAL_RX = re.compile(r"refused|rejected|declined|no open|nothing to update|bad number|already in|cannot|failed|\u26a0|\U0001f6ab", re.I)


def _perp_clean_note(m):
    t = re.sub(r"<[^>]+>", "", str(m))
    return re.sub(r"\s+", " ", t).strip()


def _perp_note_reason(notes):
    """(is_refusal, reason) from the messages a signal produced."""
    notes = [n for n in (notes or []) if n]
    ref = [n for n in notes if _PERP_REFUSAL_RX.search(str(n))]
    pick = ref or notes
    txt = " | ".join(_perp_clean_note(n) for n in pick)[:500]
    return bool(ref), txt


def _perp_write_signal_ack(relay_name, data, outcome):
    """bot_logs/perp_signal_acks/<signal>_<ts>.json + <signal>.latest.json + acks.jsonl."""
    try:
        d = os.path.join(WS_BOT_LOGS, "perp_signal_acks")
        os.makedirs(d, exist_ok=True)
        stem = os.path.splitext(os.path.basename(relay_name))[0]
        rec = {"signal_file": relay_name, "signal": data, **(outcome or {"status": "unknown"})}
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        for fn in (f"{stem}_{ts}.json", f"{stem}.latest.json"):
            with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, default=str)
        with open(os.path.join(d, "acks.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        print(f"[perp ack] write err: {e!r}")


_PERP_SIG_ACTIONS = {"long", "short", "close", "adjust", "add", "update_params", "scan",
                     "open_long", "open_short", "buy", "sell", "exit", "modify", "set_params",
                     "params", "update_levels", "skip", "ignore", "decline", "trim", "reduce",
                     "partial_close", "close_pct", "pull_initial"}  # documented vocabulary (reference list)


def _perp_signal_schema_errors(data, opening):
    """Clear decline reasons for a malformed signal. Empty list = shape is fine."""
    errs = []
    act = str(data.get("action") or data.get("direction") or data.get("signal") or data.get("side") or "").lower().strip()
    if not act:
        errs.append("action is missing (long | short | close | adjust | add | update_params)")
    mk = str(data.get("market") or "SOL-PERP").upper()
    if mk.replace("-PERP", "") not in ("SOL", "BTC", "ETH"):
        errs.append(f"market {mk} is not SOL-PERP, BTC-PERP or ETH-PERP")
    if opening:
        if not _perps_order_flag_true(data):
            errs.append("perps_is_order must be true on an open (long/short/add); without it nothing is sent")
        sz = [k for k in ("collateral_sol", "collateral_usd", "size_usd") if data.get(k) not in (None, "")]
        if not sz and data.get("size") in (None, ""):
            errs.append("no size: give ONE of collateral_sol, collateral_usd or size_usd")
        elif len(sz) > 1:
            errs.append(f"more than one size field ({', '.join(sz)}); give exactly one")
        for k in ("leverage", "collateral_sol", "collateral_usd", "size_usd", "tp_pct", "sl_pct", "tp_price", "sl_price"):
            v = data.get(k)
            if v not in (None, ""):
                try:
                    if float(v) <= 0:
                        errs.append(f"{k} must be > 0 (got {v})")
                except Exception:
                    errs.append(f"{k} is not a number (got {v!r})")
    return errs


def _perp_lev_ladder():
    """PERP_LEV_LADDER: 'collateral_sol:leverage,...' (less collateral -> more leverage).
    Resolution: saved/active override > bots/.env > default. Sorted by collateral."""
    raw = _perp_get_override("PERP_LEV_LADDER", None) or os.getenv("PERP_LEV_LADDER") or "0.1:20,1:15,10:12,50:10"
    pts = []
    try:
        for part in str(raw).replace(";", ",").split(","):
            if ":" in part:
                a, b = part.split(":", 1)
                pts.append((float(a), float(b)))
    except Exception:
        pts = []
    return sorted(pts) or [(0.1, 20.0), (1.0, 15.0), (10.0, 12.0), (50.0, 10.0)]


def _perp_lev_for_collateral_sol(col_sol):
    """Leverage from the ladder (log-interpolated), inside the 10x..PERP_MAX_LEVERAGE band."""
    import math
    pts = _perp_lev_ladder()
    try:
        c = float(col_sol)
    except Exception:
        c = 0.0
    if c <= pts[0][0]:
        lev = pts[0][1]
    elif c >= pts[-1][0]:
        lev = pts[-1][1]
    else:
        lev = pts[-1][1]
        for (c0, l0), (c1, l1) in zip(pts, pts[1:]):
            if c0 <= c <= c1:
                t = (math.log(c) - math.log(c0)) / (math.log(c1) - math.log(c0)) if c0 > 0 else 0.0
                lev = l0 + (l1 - l0) * t
                break
    hi = float(_perp_get_override("PERP_MAX_LEVERAGE", PERP_MAX_LEVERAGE))
    lo = float(PERP_MIN_LEVERAGE)
    return float(round(max(lo, min(hi, lev))))


def _perp_lev_for_collateral_usd(col_usd, sol_price=None):
    try:
        px = float(sol_price or globals().get("_LAST_SOL_PRICE") or 0)
        return _perp_lev_for_collateral_sol(float(col_usd) / px) if px > 0 else float(PERP_MIN_LEVERAGE)
    except Exception:
        return float(PERP_MIN_LEVERAGE)
def _notify_agent(msg: str, kind: str = "alert"):
    """Mirror a perps alert into the agent's inbox so the BRAIN sees fills, failures,
    and position events, not just the owner's Telegram. Fail-silent. This is what closes
    the confirmation loop: a failed fire now reaches the agent, not just the phone."""
    try:
        clean = (msg or "").replace("<b>", "").replace("</b>", "")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        pkt = {
            "_type":   "perps_alert",
            "kind":    kind,
            "message": clean,
            "at":      datetime.now(EST).isoformat(),
            "_doctrine": ("Mirror of a perps Telegram alert. Fills, failures, TP/SL/liq/trail "
                          "events land here so you can react. Not an order."),
        }
        with open(os.path.join(INBOX_PATH, f"perps_bot - alert_{ts}.json"), "w") as _f:
            json.dump(pkt, _f, indent=2)
    except Exception as _e:
        print(f"[notify->agent] err: {_e}")


# ── REALIZED SUMMARY (2026-07-27) ────────────────────────────────────────────
# The agent's only view of perps PnL was /perpstats — a Telegram command OWNER types.
# Nothing in any packet told him how he was actually doing, so the bot making the
# trades could not see its own record.
#
# THIS BOT OWNS THIS NUMBER. It reads perp_pnl.json — its own ledger, USD-
# denominated, nothing from any feed. Anything else that displays perps
# PnL is a READER of this, not an authority over it.
#
# Perps has no partial-leg problem (one row per closed trade), but it DOES need
# the scratch band: a trade closing inside +/-SCRATCH_PCT paid fees and funding
# and expressed no view. Neither a win nor a loss — counting it as either
# distorts the record in whichever direction it happened to land.
_PERPS_SCRATCH_PCT = 5.0


def perps_realized_summary() -> dict:
    """Position-level realized USD PnL, scratch-banded."""
    try:
        with open(PNL_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return {"net_usd": 0.0, "closed": 0, "wins": 0, "losses": 0, "scratches": 0,
                "win_rate_pct": None, "_note": "no perp pnl ledger yet"}
    trades = [t for t in (d.get("trades") or []) if isinstance(t, dict)
              and t.get("pnl_usd") is not None]

    def _pct(t):
        c = float(t.get("collateral_usd") or t.get("size_usd") or 0)
        return (float(t.get("pnl_usd") or 0) / c * 100) if c else 0.0

    wins    = [t for t in trades if _pct(t) > _PERPS_SCRATCH_PCT]
    losses  = [t for t in trades if _pct(t) < -_PERPS_SCRATCH_PCT]
    scratch = [t for t in trades if abs(_pct(t)) <= _PERPS_SCRATCH_PCT]
    decided = len(wins) + len(losses)
    aw = (sum(float(t["pnl_usd"]) for t in wins) / len(wins)) if wins else None
    al = (abs(sum(float(t["pnl_usd"]) for t in losses)) / len(losses)) if losses else None
    net = d.get("total_realized_usd")
    if net is None:
        net = sum(float(t.get("pnl_usd") or 0) for t in trades)
    best  = max(trades, key=lambda t: float(t.get("pnl_usd") or 0), default=None)
    worst = min(trades, key=lambda t: float(t.get("pnl_usd") or 0), default=None)
    return {
        "net_usd": round(float(net), 2),
        "fees_usd": round(float(d.get("total_fees_usd") or 0), 2),
        "closed": len(trades),
        "wins": len(wins), "losses": len(losses), "scratches": len(scratch),
        "win_rate_pct": round(len(wins) / decided * 100, 1) if decided else None,
        "avg_win_usd": round(aw, 2) if aw else None,
        "avg_loss_usd": round(-al, 2) if al else None,
        "payoff_ratio": round(al / aw, 2) if (aw and al) else None,
        "breakeven_win_pct": round(al / (aw + al) * 100, 1) if (aw and al) else None,
        "best": {"market": best.get("market"), "pnl_usd": round(float(best.get("pnl_usd") or 0), 2)} if best else None,
        "worst": {"market": worst.get("market"), "pnl_usd": round(float(worst.get("pnl_usd") or 0), 2)} if worst else None,
        "_note": (f"Trades inside +/-{_PERPS_SCRATCH_PCT:.0f}% of collateral are SCRATCHES "
                  f"(fees + funding, no view expressed) and sit outside the win rate. "
                  f"Under ~30 closed trades treat any rate as a rumour."),
    }

async def notify(msg: str):
    # 2026-10-16: while a signal runs, every message is captured so a refusal
    # always reaches the ack with its reason.
    if PERPS_DRY_RUN and not str(msg).startswith("[PAPER]"):
        msg = "[PAPER] " + str(msg)
    _sn = globals().get("_PERP_SIG_NOTES")
    if isinstance(_sn, list):
        _sn.append(str(msg))
    # Sprint 46: every Telegram alert also lands in the agent's inbox.
    _notify_agent(msg)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"})
        except Exception as e:
            print(f"telegram error: {e}")
async def notify_error(context: str, err):
    msg = f"🔥 <b>[PERPS ERROR] {context}</b>\n{str(err)[:300]}"
    print(msg)
    await notify(msg)
# ── Sprint 68: JUPITER FEE MODEL. The Drift constants are retired — Sprint 57
# removed the venue, but the ledger was still paying its fees (0.07% round trip).
# The file's own header always knew the truth: "0.06% open + 0.06% close + hourly
# borrow (dual-slope)." Borrow is charged on NOTIONAL per hour held and is often
# the LARGEST cost of a multi-hour leveraged hold — it was never accounted
# anywhere. The hourly rate below is an ESTIMATE of Jupiter's dual-slope average
# (tunable; live-rate reads can replace it). Estimated honestly beats ignored.
JUPITER_OPEN_FEE_PCT          = 0.06    # % of notional at open
JUPITER_CLOSE_FEE_PCT         = 0.06    # % of notional at close
JUPITER_TRADE_FEE_PCT         = JUPITER_OPEN_FEE_PCT + JUPITER_CLOSE_FEE_PCT  # round trip
JUPITER_BORROW_HOURLY_PCT_EST = 0.010   # % of notional per hour held (dual-slope est.)
def calc_net_pnl(gross_pnl_usd: float, size_usd: float, hold_hours: float = 0.0) -> tuple[float, float]:
    """Net = gross − (round-trip trade fees + estimated borrow accrual).
    size_usd is NOTIONAL — fees and borrow are charged on notional.
    hold_hours=0 keeps legacy call-sites safe (trade fees only)."""
    trade_fees = size_usd * (JUPITER_TRADE_FEE_PCT / 100)
    borrow_est = size_usd * (JUPITER_BORROW_HOURLY_PCT_EST / 100) * max(0.0, float(hold_hours or 0.0))
    fees = trade_fees + borrow_est
    return round(gross_pnl_usd - fees, 4), round(fees, 4)
def calc_breakeven_price(entry: float, direction: str, size_usd: float, base_amount: float) -> float:
    # Trade fees only — borrow accrues with time, so true breakeven drifts further
    # out the longer the hold. The monitor's net readout carries the live accrual.
    fees = size_usd * (JUPITER_TRADE_FEE_PCT / 100)
    fee_per_sol = fees / base_amount if base_amount else 0
    if direction == "long":
        return round(entry + fee_per_sol, 4)
    else:
        return round(entry - fee_per_sol, 4)
# -- LEVERAGE FIX 2026-10-16: one place that turns a signal into sizing + TP/SL --
# Vocabulary (also in the perps skill):
#   collateral = the money put up (the most you can lose)
#   size_usd   = the POSITION (notional) = collateral x leverage
#   "0.25 SOL 50x long" = collateral 0.25 SOL, leverage 50, position 12.5 SOL
# Field precedence for collateral: collateral_sol / collateral_usd (explicit) >
# bare "size" sent WITH leverage (read as collateral in SOL) > collateral /
# amount / col (legacy: under 5 = SOL, 5 or more = USD).
# size_sol keeps its legacy meaning: POSITION size in SOL (notional).
def _perp_num(v):
    try:
        if v is None or v == "" or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _perp_parse_open_sizing(data: dict, sol_price: float) -> dict:
    """Pure: no network, no trading. Returns size_usd (position), collateral_usd,
    leverage, leverage_given, collateral_source, notes."""
    d = data or {}
    px = float(sol_price or 0)
    notes = []
    lev = _perp_num(d.get("leverage"))
    if lev is None:
        lev = _perp_num(d.get("lev"))
    lev_given = lev is not None
    size_usd = _perp_num(d.get("size_usd")) or _perp_num(d.get("notional_usd")) or _perp_num(d.get("amount_usd")) or 0.0
    col_usd, col_src = 0.0, None
    c_usd, c_sol = _perp_num(d.get("collateral_usd")), _perp_num(d.get("collateral_sol"))
    bare_size = _perp_num(d.get("size"))
    legacy = _perp_num(d.get("collateral"))
    if legacy is None:
        legacy = _perp_num(d.get("amount"))
    if legacy is None:
        legacy = _perp_num(d.get("col"))
    if c_usd and c_usd > 0:
        col_usd, col_src = c_usd, "collateral_usd"
    elif c_sol and c_sol > 0 and px > 0:
        col_usd, col_src = c_sol * px, "collateral_sol"
    elif bare_size and bare_size > 0 and lev_given and px > 0 and not size_usd:
        col_usd, col_src = bare_size * px, "size_as_collateral_sol"
        notes.append(f"bare size {bare_size:g} with {lev:g}x read as {bare_size:g} SOL collateral")
    elif legacy and legacy > 0:
        if legacy < 5 and px > 0:
            col_usd, col_src = legacy * px, "collateral_heuristic_sol"
        else:
            col_usd, col_src = legacy, "collateral_heuristic_usd"
        notes.append(f"collateral {legacy:g} read as {'SOL' if col_src.endswith('sol') else 'USD'} "
                     f"(under 5 = SOL). Send collateral_sol or collateral_usd to be explicit.")
    if not size_usd:
        ssol = _perp_num(d.get("size_sol"))
        if ssol is None and bare_size and col_src != "size_as_collateral_sol":
            ssol = bare_size   # legacy: bare size without leverage = position in SOL
        if ssol and ssol > 0 and px > 0:
            size_usd = ssol * px
    L = lev if (lev_given and lev > 0) else None
    if col_usd and not size_usd and L:
        size_usd = col_usd * L
    elif size_usd and not col_usd:
        col_usd = size_usd / L if L else size_usd
    return {"size_usd": round(size_usd, 6), "collateral_usd": round(col_usd, 6),
            "leverage": lev if lev_given else 1.0, "leverage_given": lev_given,
            "collateral_source": col_src, "notes": notes}


def _perp_pct_to_price(ref: float, pct, direction: str, kind: str):
    """Side-aware % (of PRICE, from ref) -> price. Sign of pct is ignored:
    long: TP above, SL below. short: TP below, SL above."""
    p = abs(float(pct)) / 100.0
    up = (kind == "tp") == (direction == "long")
    return round(ref * (1 + p) if up else ref * (1 - p), 4)


def _perp_resolve_tp_sl(data: dict, direction: str, ref_price):
    """Pure. Prices: tp_price/sl_price (aliases tp/sl). Percents: tp_pct/sl_pct
    (aliases take_profit_pct/stop_loss_pct) = PRICE move from ref, side-aware.
    A level on the wrong side of ref, or over 50% away, is dropped with a warning."""
    d = data or {}
    out, warns = {}, []
    for kind, pk, alias, pcts in (("tp", "tp_price", "tp", ("tp_pct", "take_profit_pct")),
                                  ("sl", "sl_price", "sl", ("sl_pct", "stop_loss_pct"))):
        price = _perp_num(d.get(pk))
        if price is None:
            price = _perp_num(d.get(alias))
        if price is None:
            pv = next((_perp_num(d.get(k)) for k in pcts if _perp_num(d.get(k)) is not None), None)
            if pv is not None:
                if ref_price:
                    price = _perp_pct_to_price(float(ref_price), pv, direction, kind)
                else:
                    warns.append(f"{kind}_pct {pv:g} ignored: no price for this market")
        if price is not None and ref_price and direction in ("long", "short"):
            r = float(ref_price)
            above = price > r
            want_above = (kind == "tp") == (direction == "long")
            if price <= 0 or above != want_above or price == r:
                warns.append(f"{kind.upper()} {price:g} is on the wrong side of {r:g} for a {direction}; ignored")
                price = None
            elif abs(price - r) / r > 0.5:
                warns.append(f"{kind.upper()} {price:g} is over 50% from {r:g} (a % sent as a price?); ignored")
                price = None
        out[kind] = price
    return out.get("tp"), out.get("sl"), warns


def calc_liq_price(entry: float, direction: str, leverage: float, buffer: float = 0.0) -> float:
    """
    ESTIMATE of the VENUE's liquidation price, from entry + leverage only.

    2026-07-26: buffer default was 0.05, which meant every stored "liq_price" was
    silently a FLOOR, not a liquidation — understating real room ~14% and biasing
    every leverage decision conservative. Want a cushion? ask for it by name via
    calc_exit_floor(). This function now answers exactly one question honestly.

    Prefer calc_liq_from_account() when the live account is readable: this one
    can't see fees already eaten out of collateral.
    """
    margin_pct = 1 / leverage
    if direction == "long":
        return round(entry * (1 - margin_pct + buffer), 2)
    else:
        return round(entry * (1 + margin_pct - buffer), 2)


def calc_liq_from_account(entry: float, size_usd: float, collateral_usd: float,
                          direction: str, fee_reserve_frac: float | None = None):
    """
    Liquidation from the REAL on-chain position fields — live collateral, which
    fees have already eaten into, not the leverage the position was opened at.

    ⚠ CALIBRATION: fee_reserve_frac is FITTED TO ONE OBSERVATION —
      SOL long, entry 74.02, size $25.00, collateral $4.98 → jup.ag showed 59.51.
      0.003 reproduces 59.50 (off by $0.01). That is a fit, NOT Jupiter's spec.
      Re-check against the UI on the next position; tune via LIQ_FEE_RESERVE_FRAC.
    """
    try:
        entry = float(entry); size_usd = float(size_usd); collateral_usd = float(collateral_usd)
        if size_usd <= 0 or entry <= 0:
            return None
        frac = (fee_reserve_frac if fee_reserve_frac is not None
                else float(_perp_get_override("LIQ_FEE_RESERVE_FRAC", LIQ_FEE_RESERVE_FRAC)))
        eff = max(collateral_usd - (frac * size_usd), 0.0)
        r = eff / size_usd
        return round(entry * (1 - r), 2) if direction == "long" else round(entry * (1 + r), 2)
    except Exception:
        return None


def calc_exit_floor(venue_liq, direction: str, pct: float | None = None,
                    entry=None, tp=None, sl=None):
    """OUR floor, sitting above the venue's real liq. A notification line, never a close.

    polish-2026-10-15 (item 5): at high leverage a fixed % above liq put the
    floor ABOVE entry (and above TP on a short), so it fired the moment the
    position opened. The floor is clamped strictly between liq and the nearer of
    entry / TP; when the % would cross that, it sits at the midpoint."""
    try:
        p = (pct if pct is not None
             else float(_perp_get_override("LIQ_EXIT_FLOOR_PCT", LIQ_EXIT_FLOOR_PCT))) / 100.0
        v = float(venue_liq)
        f = v * (1 + p) if direction == "long" else v * (1 - p)
        return clamp_exit_floor(f, v, direction, entry=entry, tp=tp, sl=sl)
    except Exception:
        return None


def clamp_exit_floor(floor, liq, direction: str, entry=None, tp=None, sl=None):
    """Keep a floor strictly between liq and min(entry, TP, SL) (long) / max(...) (short).
    2026-10-16: the SL is a cap too, so the floor always sits beyond the SL (between SL
    and liq) and never fires before the stop."""
    try:
        f, v = float(floor), float(liq)
        caps = [float(x) for x in (entry, tp, sl) if x]
        if direction == "long":
            if f <= v:
                f = v * 1.001
            if caps:
                hi = min(caps)
                if hi > v and f >= hi:
                    f = v + (hi - v) * 0.5
        else:
            if f >= v:
                f = v * 0.999
            if caps:
                lo = max(caps)
                if lo < v and f <= lo:
                    f = v - (v - lo) * 0.5
        return round(f, 4 if f < 10 else 2)
    except Exception:
        return None


def _perp_fill_mismatch(req_size, req_lev, venue_read, booked_size, tol=0.10):
    """polish-2026-10-15 (item 2): compare the ordered notional/leverage with the
    filled ones (on-chain position read when available). Returns a message or None."""
    try:
        msgs = []
        rs, rl = float(req_size or 0), float(req_lev or 0)
        fs = float((venue_read or {}).get("size_usd") or booked_size or 0)
        fl = (venue_read or {}).get("leverage")
        if rs > 0 and fs > 0 and abs(fs - rs) / rs > tol:
            msgs.append(f"notional ordered ${rs:,.2f}, filled ${fs:,.2f}")
        if rl > 0 and fl and abs(float(fl) - rl) / rl > tol:
            msgs.append(f"leverage ordered {rl:g}x, filled {float(fl):g}x")
        if not msgs:
            return None
        return "; ".join(msgs) + ". Nothing was changed; the book records what filled."
    except Exception:
        return None


async def fetch_venue_liq_price(market: str, direction: str):
    if PERPS_DRY_RUN:
        return None  # paper: liq is estimated, never read off a real account
    """
    Read the LIVE Jupiter position account and derive the venue's REAL liquidation.

    Returns {"liq","source","entry","size_usd","collateral_usd","leverage"} or None.
    None is a legitimate answer — the caller MUST fall back to the estimate AND
    label it. A guessed liq wearing the name of a real one is the exact bug this
    whole patch exists to kill, so provenance travels with the number everywhere.
    """
    try:
        accts = _resolve_position_accounts(market, direction)
        pda = accts.get("position") or accts.get("position_pda")
        if pda is None:
            print("[liq] no position account key in _resolve_position_accounts result")
            return None
        conn = AsyncClient(RPC_URL, commitment=Confirmed)
        try:
            resp = await conn.get_account_info(pda, commitment=Confirmed)
        finally:
            try: await conn.close()
            except Exception: pass
        val = getattr(resp, "value", None)
        if not val or not getattr(val, "data", None):
            return None
        raw = val.data if isinstance(val.data, (bytes, bytearray)) else bytes(val.data)
        if len(raw) < 210:
            return None
        # Jupiter Perps `Position` (8-byte anchor discriminator first).
        # VERIFY AT DEPLOY against the fetched IDL — offsets are guarded by the
        # sanity block below so a layout change degrades to None, never to a lie.
        u64 = lambda a, b: int.from_bytes(raw[a:b], "little", signed=False)
        entry_p   = u64(153, 161) / 1e6
        size_usd  = u64(161, 169) / 1e6
        coll_usd  = u64(169, 177) / 1e6
        side      = raw[152]
        # ── sanity: anything off means we did NOT understand the bytes ──
        if not (entry_p > 0 and size_usd > 0 and coll_usd > 0):
            return None
        if coll_usd > size_usd:
            return None
        if not (0 < size_usd / coll_usd <= 150):
            return None
        want_long = (direction == "long")
        if side in (1, 2) and ((side == 1) != want_long):
            return None
        liq = calc_liq_from_account(entry_p, size_usd, coll_usd, direction)
        if liq is None or not (0 < liq < entry_p * 3):
            return None
        return {"liq": liq, "source": "onchain_position_account", "entry": round(entry_p, 4),
                "size_usd": round(size_usd, 2), "collateral_usd": round(coll_usd, 4),
                "leverage": round(size_usd / coll_usd, 2)}
    except Exception as _e:
        print(f"[liq] venue read failed ({market} {direction}): {_e!r}")
        return None
def calc_auto_tp_sl(entry: float, direction: str, leverage: float) -> tuple[float, float]:
    sl_move = min(0.05, 0.8 / leverage)
    tp_move = sl_move * 2
    if direction == "long":
        return round(entry * (1 + tp_move), 2), round(entry * (1 - sl_move), 2)
    else:
        return round(entry * (1 - tp_move), 2), round(entry * (1 + sl_move), 2)
# ── Sprint 46: PER-MARKET POSITION STORE ────────────────────────────────────
# Was a single global file (one position, SOL-priced). Now a dict keyed by
# market so SOL / BTC / ETH can be open at once, one per chain. Back-compat:
# every no-arg load/save/clear defaults to SOL-PERP, so legacy callers
# (telegram relay, intel scanner) behave EXACTLY as before until their manual
# multi-market UX is decided. New callers pass market= to go multi-market.
PERP_POSITIONS_FILE = os.path.join(WS_BOT_LOGS, "paper_perp_positions.json" if PERPS_DRY_RUN else "perp_positions.json")  # {market: position_dict}

_MARKET_INDEX_BY_SYMBOL = {"SOL-PERP": 0, "BTC-PERP": 1, "ETH-PERP": 2}


def _load_all_positions() -> dict:
    """Full per-market store {market: pos}. One-time migrates the legacy
    single-position file (perp_position.json) into the keyed store."""
    store = {}
    try:
        if os.path.exists(PERP_POSITIONS_FILE):
            # 2026-07-24 (The owner's "the file is empty"): PowerShell's UTF8 writes
            # a BOM; a bare open() + json.load chokes on the invisible prefix
            # and the store reads as {} — file full, bot blind. utf-8-sig
            # tolerates BOM'd and plain files alike. Manual repairs must load.
            with open(PERP_POSITIONS_FILE, encoding="utf-8-sig") as f:
                store = json.load(f) or {}
    except Exception:
        store = {}
    return store if isinstance(store, dict) else {}


def _save_all_positions(store: dict):
    with open(PERP_POSITIONS_FILE, "w") as f:
        json.dump(store, f, indent=2)


def _pos_base(k) -> str:
    """Store key -> market. A second, opposite-side position on the same market is
    keyed MARKET#SIDE (only when PERP_ONE_PER_MARKET=false)."""
    return str(k or "SOL-PERP").upper().split("#")[0]


def _perp_one_per_market() -> bool:
    """PERP_ONE_PER_MARKET: true = one position per market (a long blocks a short).
    false (public default) = whatever Jupiter allows: long and short on the same
    market as separate positions. Same-side re-open is always action=add."""
    return str(os.getenv("PERP_ONE_PER_MARKET", "false")).strip().lower() in ("1", "true", "yes", "on")


def _pos_key_for(store: dict, market, side=None):
    m = str(market or "SOL-PERP").upper()
    if "#" in m:
        return m if m in store else None
    s = str(side or "").lower()
    cands = [k for k, v in store.items() if _pos_base(k) == m and isinstance(v, dict) and v.get("direction")]
    if s in ("long", "short"):
        for k in cands:
            if str(store[k].get("direction") or "").lower() == s:
                return k
        return None
    if m in store:
        return m
    return cands[0] if cands else None


def _positions_for_market(market) -> list:
    b = _pos_base(market)
    return [v for k, v in _load_all_positions().items()
            if _pos_base(k) == b and isinstance(v, dict) and v.get("direction")]


def load_position(market: str = "SOL-PERP", side: str | None = None) -> dict | None:
    """Open position for `market` (and `side` when given), or None. Default
    SOL-PERP keeps every legacy no-arg caller behaving exactly as before."""
    store = _load_all_positions()
    k = _pos_key_for(store, market, side)
    pos = store.get(k) if k else None
    return pos if pos and pos.get("direction") else None


def active_position(market: str | None = None):
    """FIELD FIX (2026-07-24, the owner: "chained to something not used anymore"):
    bare load_position() defaults to the legacy SOL-PERP slot, leaving every
    no-arg caller blind in the multi-market era — the Telegram close/tp/sl
    commands would have found 'no position' while the ETH long rode. Resolution
    order: explicit market → the only open position → the largest by size.
    Returns (market, pos) or (None, None)."""
    store = load_positions()
    if market:
        m = market.upper()
        return (m, store.get(m)) if store.get(m) else (None, None)
    if not store:
        return (None, None)
    if len(store) == 1:
        m = next(iter(store))
        return (m, store[m])
    m = max(store, key=lambda k: float(store[k].get("size_usd") or 0))
    return (m, store[m])


def load_positions() -> dict:
    """All live positions keyed by market (only slots with a direction)."""
    return {k: v for k, v in _load_all_positions().items() if v and v.get("direction")}


def save_position(data: dict, market: str | None = None):
    """Write one market's position into the store. Market resolves from the arg,
    then data['market'], then SOL-PERP."""
    raw = (market or data.get("market") or "SOL-PERP").upper()
    base = _pos_base(raw)
    if not data.get("market") or "#" in str(data.get("market")):
        data["market"] = base
    store = _load_all_positions()
    d = str(data.get("direction") or "").lower()
    if "#" in raw:
        key = raw
    elif not d:
        key = base
    else:
        key = _pos_key_for(store, base, d)
        if key is None:
            prim = store.get(base)
            busy = isinstance(prim, dict) and prim.get("direction") and str(prim.get("direction")).lower() != d
            key = f"{base}#{d.upper()}" if busy else base
    store[key] = data
    _save_all_positions(store)


PERP_POS_ARCHIVE = os.path.join(WS_BOT_LOGS, "perp_positions_archive.jsonl")

def _archive_position_row(market: str, pos: dict, why: str) -> None:
    """2026-07-24 (the phantom-close incident): every cleared record is archived
    so a wrong clear is REVERSIBLE — the reconciler restores full metadata
    instead of flagging an 'unknown' chain position."""
    try:
        row = dict(pos or {}); row["_archived_at"] = datetime.now(EST).isoformat()
        row["_market"] = market; row["_why"] = why
        # polish-2026-10-15 (item 9): dedupe. The same position (same market +
        # open timestamp + open tx) archived twice made the restore path and the
        # review see two positions. Skip if the identical record is already there.
        _key = (market, (pos or {}).get("timestamp"), (pos or {}).get("tx_sig"),
                (pos or {}).get("entry_price"))
        try:
            if os.path.exists(PERP_POS_ARCHIVE):
                with open(PERP_POS_ARCHIVE, encoding="utf-8") as _af:
                    for _ln in _af:
                        try:
                            _r = json.loads(_ln)
                        except Exception:
                            continue
                        if (_r.get("_market"), _r.get("timestamp"), _r.get("tx_sig"),
                                _r.get("entry_price")) == _key:
                            print(f"[positions] archive dedupe: {market} already archived")
                            return
        except Exception:
            pass
        with open(PERP_POS_ARCHIVE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"[positions] archive err: {e}")


def clear_position(market: str = "SOL-PERP", side: str | None = None):
    """Remove one market's position from the store (default SOL-PERP; `side`
    picks long/short when both are open). Archives what it clears — clears must
    be reversible. Clearing the primary slot promotes the other side into it."""
    raw = (market or "SOL-PERP").upper()
    store = _load_all_positions()
    mkt = _pos_key_for(store, raw, side) or (raw if raw in store else None)
    if mkt and mkt in store:
        try:
            if store[mkt] and store[mkt].get("direction"):
                _archive_position_row(_pos_base(mkt), store[mkt], "clear_position")
        except Exception:
            pass
        store.pop(mkt, None)
        base = _pos_base(mkt)
        if mkt == base:
            for _k2 in [k for k in list(store) if "#" in k and _pos_base(k) == base]:
                store[base] = store.pop(_k2)
                break
        _save_all_positions(store)
        try:
            _perp_exit_suggest_prune(store)  # 2026-10-16: a closed position takes its suggestion with it
        except Exception:
            pass


async def _market_mark_price(market: str, fallback_sol=None):
    """Mark price for a perp market — from the SAME Pyth oracle Jupiter prices against.

    -- SPRINT 56b: THE MONITOR WAS LYING -----------------------------------------
    This used to read the DRIFT oracle, and for SOL it fell back to CoinGecko. But
    our positions live on JUPITER. Those three sources do not agree:

        entry (Jupiter fill):  $77.076
        CoinGecko (monitor):   $83.945   -> monitor reported "+17.8%"
        Pyth (Jupiter's own):  $77.37    -> actual P&L was ~+0.4%

    So the monitor was showing a ~17% gain on a position that had barely moved, and
    -- worse -- TP/SL were being evaluated against a price the exchange does not use.
    The auto-TP at $84.78 would have "taken profit" on a move that never happened,
    firing a real close on a phantom gain.

    A position must be priced off the venue that holds it. Now it is.
    """
    m = _pos_base(market or "SOL-PERP")
    asset = m.replace("-PERP", "").upper()

    client = AsyncClient(RPC_URL, commitment=Confirmed)
    try:
        px = await _jupiter_mark_price(client, asset)
        if px and px > 0:
            return px
    except Exception as e:
        print(f"[s56b][mark] {m} jupiter mark failed: {e}")
    finally:
        try:
            await client.close()
        except Exception:
            pass

    # No silent CoinGecko fallback for a Jupiter position — a price the venue does
    # not agree with is worse than no price. SOL keeps a fallback ONLY because the
    # caller may already hold a fresh value; everything else returns None and the
    # caller must handle it.
    if m == "SOL-PERP" and fallback_sol:
        print(f"[s56b][mark] WARN: falling back to non-Jupiter SOL price ${fallback_sol:.4f}")
        return fallback_sol
    return None


# ── Shared mark-price cache (2026-10-17, HELIUS_AUDIT) ─────────────────────────
# One Pyth read per market serves the position monitor, the owner-position loop
# and the parked-limit watcher. Only real Jupiter/Pyth prices are cached (never
# None, never a fallback). The monitor accepts at most a (FAST_POLL - 1)s-old
# price, so its TP/SL timing is unchanged; the owner loop and limit watcher take
# up to PERP_MARK_CACHE_S (15-30s). Trades / closes keep calling
# _market_mark_price directly (always fresh).
try:
    PERP_MARK_CACHE_S = min(30.0, max(15.0, float(os.getenv("PERP_MARK_CACHE_S", "20") or 20)))
except Exception:
    PERP_MARK_CACHE_S = 20.0
try:
    PERP_MARK_CACHE_CAPPED_S = max(PERP_MARK_CACHE_S, float(os.getenv("PERP_MARK_CACHE_CAPPED_S", "120") or 120))
except Exception:
    PERP_MARK_CACHE_CAPPED_S = 120.0
_PERP_MARK_CACHE: dict = {}    # base market -> (monotonic ts, price)
_PERP_MARK_LOCKS: dict = {}
_PERP_MARK_STATS = {"hit": 0, "miss": 0}


async def _market_mark_price_cached(market: str, max_age: "float | None" = None):
    """_market_mark_price through the shared cache. max_age caps staleness for
    callers that need it tighter than the default TTL."""
    m = _pos_base(market or "SOL-PERP")
    ttl = PERP_MARK_CACHE_CAPPED_S if perp_helius_restricted() else PERP_MARK_CACHE_S
    if max_age is not None:
        ttl = min(ttl, max(0.0, float(max_age)))
    hit = _PERP_MARK_CACHE.get(m)
    if hit and time.monotonic() - hit[0] < ttl:
        _PERP_MARK_STATS["hit"] += 1
        return hit[1]
    lock = _PERP_MARK_LOCKS.get(m)
    if lock is None:
        lock = _PERP_MARK_LOCKS[m] = asyncio.Lock()
    async with lock:
        hit = _PERP_MARK_CACHE.get(m)   # another caller may have refreshed while we waited
        if hit and time.monotonic() - hit[0] < ttl:
            _PERP_MARK_STATS["hit"] += 1
            return hit[1]
        _PERP_MARK_STATS["miss"] += 1
        px = await _market_mark_price(market)
        if px and px > 0:
            _PERP_MARK_CACHE[m] = (time.monotonic(), float(px))
        return px


def _perp_monitor_mark_max_age() -> float:
    try:
        return max(1.0, float(_perp_get_override("FAST_POLL_SECONDS", FAST_POLL_SECONDS)) - 1.0)
    except Exception:
        return 7.0

# ════════════════════════════════════════════════════════════════════════════
# Sprint 58: EPISODE MEMORY — precedent, not pattern.
# ════════════════════════════════════════════════════════════════════════════
# The autopsy engine learns AGGREGATE behavior (you oversize in chop, you fade
# low-fear setups). This is different and complementary: it records every closed
# position as an EPISODE with the situation it happened in, so that when the agent
# is LIVE in a similar situation, the managing view can tell him "last time this
# exact shape hit, here's what happened." Episodic precedent —
# a deterministic W-L scoreboard per situation-type,
# with the agent reasoning FROM the record, not hunting for it.
#
# An episode's FINGERPRINT is stamped at OPEN (funding phase + news lean + regime
# at entry are only knowable then) and carried on the position; the OUTCOME is
# written at CLOSE through record_pnl (the one chokepoint every exit funnels
# through). Match is rule-based on the fingerprint; the warning is the agent's to write.

PERP_EPISODES_FILE = os.path.join(WS_BOT_LOGS, "perp_episodes.jsonl")

def _perp_lev_bucket(leverage) -> str:
    """Coarse leverage buckets so the fingerprint matches on shape, not exact x."""
    try:
        lev = float(leverage or 0)
    except Exception:
        return "unknown"
    if lev <= 3:   return "low"      # <=3x
    if lev <= 8:   return "mid"      # 4-8x
    if lev <= 15:  return "high"     # 9-15x
    return "extreme"                 # 16x+

def _news_lean_from_bucketed(news: dict) -> str:
    """Compress a chain's bucketed news into one lean tag for the fingerprint."""
    net = ((news or {}).get("counts") or {}).get("net")
    if net is None:
        return "unknown"
    if net >= 2:  return "bullish"
    if net <= -2: return "bearish"
    return "mixed"

def build_entry_fingerprint(chain: str, direction: str, leverage, macro: dict = None) -> dict:
    """The situation an entry happens in — stamped at OPEN, carried on the position.
    Cheap and deterministic. funding phase + news lean + regime are the levers that
    make two entries 'the same kind of situation' for precedent matching."""
    chain = (chain or "").upper()
    try:
        macro = macro if macro is not None else (load_daily_read() or {})
    except Exception:
        macro = {}
    _clow = chain.lower()
    funding = None
    try:
        _f = (macro or {}).get(f"{_clow}_funding")
        if isinstance(_f, dict):
            funding = _f.get("phase") or _f.get("state")
        elif _f is not None:
            # numeric funding → coarse phase
            fv = float(_f)
            funding = "blowout" if abs(fv) >= 0.05 else ("elevated" if abs(fv) >= 0.02 else "normal")
    except Exception:
        funding = None
    try:
        news_lean = _news_lean_from_bucketed(_perp_chain_news(chain))
    except Exception:
        news_lean = "unknown"
    regime = None
    try:
        regime = (macro or {}).get("vol_regime") or (macro or {}).get("current_phase")
    except Exception:
        regime = None
    return {
        "chain": chain,
        "direction": direction,
        "lev_bucket": _perp_lev_bucket(leverage),
        "funding_phase": funding or "unknown",
        "news_lean": news_lean,
        "regime": regime or "unknown",
    }

def build_entry_decision_context(gate_audit: dict, signal_data: dict, reason: str) -> dict:
    """Sprint 60: capture WHY
    The agent entered — the decision context that spawned the trade — and stamp it on the
    position so the close path can fold it into the episode. Without this, an episode
    knows the SITUATION (funding/news/regime) but not the CONVICTION behind the entry,
    so 'this shape burned me' can't become the sharper 'this shape burned me AND I
    walked in on a weak conviction-4 override.' Closes the why→outcome feedback loop.

    Sources: the gate_audit's directive (aggression/size scaling the daily_read gate
    applied) and the signal the agent wrote (his own conviction/coin_call/feed_lean, if
    he included them — perps-boss is coached to). All best-effort; a thin context is
    fine for older signals or bare overrides."""
    gate_audit = gate_audit or {}
    directive = gate_audit.get("directive", {}) or {}
    sd = signal_data or {}
    ctx = {
        "reason": (reason or "")[:200],
        # The agent's own stated read, if he put it in the signal
        "conviction": sd.get("conviction"),
        "coin_call": sd.get("coin_call"),
        "feed_lean": sd.get("feed_lean") or sd.get("size_tilt"),
        # what the daily_read gate did to the entry
        "aggression": directive.get("aggression"),
        "size_multiplier": directive.get("size_multiplier"),
        "directive_label": directive.get("label"),
        "micro_lean": bool(directive.get("micro_lean", False)),
        "daily_read_freshness": gate_audit.get("daily_read_freshness"),
        "was_override": bool(sd.get("override_daily_read") or gate_audit.get("override")),
    }
    # drop keys that came back None so the stamp stays compact
    return {k: v for k, v in ctx.items() if v is not None}


def _fingerprint_key(fp: dict) -> str:
    """Canonical match key: the dimensions that define 'this kind of situation'.
    Deliberately coarse (bucketed lev, phase tags) so precedents actually accrue."""
    return "|".join([
        (fp.get("chain") or "?"), (fp.get("direction") or "?"),
        (fp.get("lev_bucket") or "?"), (fp.get("funding_phase") or "?"),
        (fp.get("news_lean") or "?"),
    ])

def record_episode(pos: dict, exit_price, pnl_pct, pnl_usd, category, reason, peak_pct=None):
    """Write one closed position to the episode log — entry fingerprint (stamped at
    open) fused with the outcome. Fail-silent; this is memory, not load-bearing."""
    try:
        fp = pos.get("entry_fingerprint")
        if not fp:
            # older positions opened before stamping — reconstruct what we can
            fp = build_entry_fingerprint(
                (pos.get("market") or "").split("-")[0], pos.get("direction"),
                pos.get("leverage"))
        liquidated = (category == "liquidated")
        won = (pnl_pct or 0) > 0
        episode = {
            "ts": datetime.now(EST).isoformat(),
            "fingerprint": fp,
            "fp_key": _fingerprint_key(fp),
            "market": pos.get("market"),
            "direction": pos.get("direction"),
            "leverage": pos.get("leverage"),
            "entry_price": pos.get("entry_price"),
            "exit_price": exit_price,
            "size_usd": pos.get("size_usd"),
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "pnl_usd": pnl_usd,
            "peak_pct": round(peak_pct, 2) if peak_pct is not None else None,
            "category": category,
            "reason": reason,
            "won": won,
            "liquidated": liquidated,
            "hold_minutes": _episode_hold_minutes(pos),
            "mode": pos.get("mode") or "perp",
            "full_bag_fit": False if any(k in (reason or "").lower() for k in ("velocity", "sensor", "fade", "trail")) else True,
            # Sprint 60: WHY the agent entered, carried from open → close. Turns
            # "this shape burned me" into "this shape burned me AND I walked in
            # on a conviction-4 override with news already turning."
            "entry_decision_context": pos.get("entry_decision_context") or {},
        }
        _s39_append_jsonl(PERP_EPISODES_FILE, episode)
        perp_autopsy_enqueue(pos, episode)   # trade autopsy queue (never raises)
    except Exception as e:
        print(f"[episode] record err: {e}")

def _episode_hold_minutes(pos: dict):
    ts = pos.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        return int((datetime.now(EST) - dt).total_seconds() / 60)
    except Exception:
        return None

def match_precedent(fp: dict, max_episodes: int = 4) -> dict | None:
    """Given the CURRENT live position's fingerprint, pull past episodes that match
    the same situation-shape and compute the scoreboard. Deterministic — same input,
    same output every cycle. Returns None when there's no precedent (don't bloat the
    packet). Reference only: The agent writes the read FROM this record."""
    try:
        key = _fingerprint_key(fp)
        rows = []
        if os.path.exists(PERP_EPISODES_FILE):
            with open(PERP_EPISODES_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ep = json.loads(line)
                    except Exception:
                        continue
                    if ep.get("fp_key") == key:
                        rows.append(ep)
        if not rows:
            return None
        n = len(rows)
        wins = [r for r in rows if r.get("won")]
        liqs = [r for r in rows if r.get("liquidated")]
        wr = len(wins) / n * 100
        avg_pnl = sum(float(r.get("pnl_pct", 0) or 0) for r in rows) / n
        recent_form = "".join("W" if r.get("won") else ("X" if r.get("liquidated") else "L") for r in rows[-6:])
        # the most relevant past episodes (most recent first), trimmed for the packet
        recent = sorted(rows, key=lambda r: r.get("ts", ""), reverse=True)[:max_episodes]
        episodes = [{
            "when": r.get("ts", "")[:10], "outcome": r.get("category"),
            "pnl_pct": r.get("pnl_pct"), "peak_pct": r.get("peak_pct"),
            "hold_min": r.get("hold_minutes"), "reason": r.get("reason"),
            "full_bag_fit": r.get("full_bag_fit", True), "mode": r.get("mode"),
            # Sprint 60: WHY he entered this past match — the conviction/aggression
            # behind the trade that ended this way. Lets him see if the losers share
            # an entry signature (e.g. all overrides, all low-conviction).
            "entry_context": r.get("entry_decision_context") or {},
        } for r in recent]
        # a raw, reference-framed read 
        if n < 3:
            read = f"thin precedent (n={n}) — a couple past matches, not a record yet"
        elif liqs and len(liqs) / n >= 0.4:
            read = (f"DANGER shape — {len(liqs)}/{n} of these LIQUIDATED "
                    f"(form {recent_form}). This exact situation has burned you before.")
        elif avg_pnl <= 0:
            read = (f"negative-EV shape — {wr:.0f}% WR but avg {avg_pnl:+.0f}% over {n} "
                    f"(form {recent_form}). Losers outrun winners here.")
        elif wr >= 60:
            read = f"favorable precedent — {wr:.0f}% over {n}, avg {avg_pnl:+.0f}% (form {recent_form})"
        else:
            read = f"mixed precedent — {wr:.0f}% over {n}, avg {avg_pnl:+.0f}% (form {recent_form})"
        return {
            "fingerprint": fp,
            "record": f"{len(wins)}-{n - len(wins)}",
            "sample_size": n,
            "liquidation_count": len(liqs),
            "win_rate_pct": round(wr, 1),
            "avg_pnl_pct": round(avg_pnl, 1),
            "recent_form": recent_form,
            "episodes": episodes,
            "read": read,
            "_doctrine": ("Precedent for THIS situation-shape (chain+direction+leverage+funding+news), "
                          "not a behavioral pattern. Reference, never a gate: a bad record is a reason to "
                          "look harder at the exit plan, not an auto-cut. 'X' in recent_form = a liquidation. "
                          "Check each episode's entry_context — if the losers share a signature "
                          "(all overrides, all low-conviction, all news-already-turning), THAT's the "
                          "lesson, not just the shape. You write the read; this is the scoreboard behind it."),
        }
    except Exception as e:
        print(f"[episode] match err: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# Sprint 59: FLIP DETECTOR — "the situation changed while you're holding."
# ════════════════════════════════════════════════════════════════════════════
# The autopsy mirrors habits; the episode memory recalls precedent; this catches
# an EVENT: a condition that has TURNED AGAINST the live position since you opened
# it, or since last cycle. cycle_check says "feed currently disagrees" (a state);
# this says "feed CHANGED ITS MIND, N minutes in" (an event) — a sharper, more
# actionable tell. All inputs come from feed's own outputs (macro.json +
# per-chain news), which feed already sweeps and writes — this only reads them
# and diffs. Two baselines: ENTRY (drift since you opened) and LAST CYCLE (what
# moved in the last hour). Raw deltas are computed deterministically and ALWAYS
# surfaced; the local ollama then digests them into one line before the agent reads
# it (graceful: if the LLM is busy/down, the raw deltas stand alone).

PERP_FEED_SNAPSHOT = os.path.join(WS_BOT_LOGS, "perp_feed_snapshot.json")

def _flip_current_chain_state(chain: str, macro: dict) -> dict:
    """The dimensions the flip detector tracks, read from feed's macro + news.
    Deliberately the same axes as the entry fingerprint so ENTRY drift is a direct
    field-by-field compare."""
    chain = (chain or "").upper()
    _clow = chain.lower()
    funding = None
    try:
        _f = (macro or {}).get(f"{_clow}_funding")
        if isinstance(_f, dict):
            funding = _f.get("phase") or _f.get("state")
        elif _f is not None:
            fv = float(_f)
            funding = "blowout" if abs(fv) >= 0.05 else ("elevated" if abs(fv) >= 0.02 else "normal")
    except Exception:
        funding = None
    try:
        news_lean = _news_lean_from_bucketed(_perp_chain_news(chain))
    except Exception:
        news_lean = "unknown"
    regime = None
    try:
        regime = (macro or {}).get("vol_regime") or (macro or {}).get("current_phase")
    except Exception:
        regime = None
    fg = None
    try:
        fg = _pfi_num((macro or {}).get("fear_greed"))
    except Exception:
        fg = None
    return {"funding_phase": funding or "unknown", "news_lean": news_lean,
            "regime": regime or "unknown", "fear_greed": fg}

def _load_feed_snapshot() -> dict:
    try:
        if os.path.exists(PERP_FEED_SNAPSHOT):
            with open(PERP_FEED_SNAPSHOT) as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}

def save_feed_snapshot(macro: dict):
    """Persist this cycle's per-chain feed state so NEXT cycle can diff against it
    for the last-hour delta. Called once per packet-build. Fail-silent."""
    try:
        snap = {"saved_at": datetime.now(EST).isoformat(), "chains": {}}
        for chain in PERP_MARKETS_COVERED:
            c = chain.split("-")[0]
            snap["chains"][c] = _flip_current_chain_state(c, macro)
        with open(PERP_FEED_SNAPSHOT, "w") as f:
            json.dump(snap, f, indent=2)
    except Exception as e:
        print(f"[flip] snapshot save err: {e}")

# funding severity ordering, for judging whether a funding move is "worse"
_FUNDING_SEVERITY = {"normal": 0, "elevated": 1, "blowout": 2, "unknown": 0}

def _flip_deltas(direction: str, baseline: dict, current: dict, baseline_label: str) -> list:
    """Compare one baseline (entry or last-cycle) to current. Return a list of raw
    delta dicts, each tagged for/against the position. Only dimensions that CHANGED
    appear. against=True means the change is a headwind for THIS direction."""
    out = []
    if not baseline or not current:
        return out
    # funding: a move UP in severity is against you regardless of direction
    b_fund, c_fund = baseline.get("funding_phase"), current.get("funding_phase")
    if b_fund != c_fund and c_fund and b_fund:
        worse = _FUNDING_SEVERITY.get(c_fund, 0) > _FUNDING_SEVERITY.get(b_fund, 0)
        out.append({"dim": "funding", "from": b_fund, "to": c_fund,
                    "against": bool(worse), "baseline": baseline_label,
                    "note": f"funding {b_fund}→{c_fund}"})
    # news lean: bullish/bearish flip; against if it moved opposite your direction
    b_news, c_news = baseline.get("news_lean"), current.get("news_lean")
    if b_news != c_news and c_news not in (None, "unknown") and b_news not in (None, "unknown"):
        if direction == "long":
            against = (c_news == "bearish" and b_news != "bearish")
        else:
            against = (c_news == "bullish" and b_news != "bullish")
        out.append({"dim": "news", "from": b_news, "to": c_news,
                    "against": bool(against), "baseline": baseline_label,
                    "note": f"news {b_news}→{c_news}"})
    # regime: any change is worth flagging; high-vol shift is against (liq math worse)
    b_reg, c_reg = baseline.get("regime"), current.get("regime")
    if b_reg != c_reg and c_reg not in (None, "unknown") and b_reg not in (None, "unknown"):
        against = ("high" in str(c_reg).lower() and "high" not in str(b_reg).lower())
        out.append({"dim": "regime", "from": b_reg, "to": c_reg,
                    "against": bool(against), "baseline": baseline_label,
                    "note": f"vol regime {b_reg}→{c_reg}"})
    # fear/greed: a big swing (>=15 pts) either way is context; against if it moved
    # toward the crowd fading your direction (fear on a long, greed on a short)
    b_fg, c_fg = baseline.get("fear_greed"), current.get("fear_greed")
    try:
        if b_fg is not None and c_fg is not None and abs(c_fg - b_fg) >= 15:
            if direction == "long":
                against = c_fg < b_fg   # sentiment cooling under a long
            else:
                against = c_fg > b_fg   # sentiment heating under a short
            out.append({"dim": "fear_greed", "from": round(b_fg), "to": round(c_fg),
                        "against": bool(against), "baseline": baseline_label,
                        "note": f"fear/greed {round(b_fg)}→{round(c_fg)}"})
    except Exception:
        pass
    return out

async def _flip_llm_digest(chain, direction, hold_min, deltas) -> str:
    """Local-ollama one-liner summarizing the flips before the agent reads them.
    Graceful: returns '' if the model is busy/down and the raw deltas stand alone."""
    if not _perp_brain_enabled():
        return ""
    try:
        against = [d for d in deltas if d.get("against")]
        lines = "; ".join(f"{d['note']} ({d['baseline']}, {'AGAINST' if d['against'] else 'neutral'})" for d in deltas)
        prompt = (
            "You summarize how a live crypto perp position's CONDITIONS have changed. "
            "One tight sentence, factual, no advice, no preamble. Reply as JSON "
            '{\"summary\": \"...\"}.\n'
            f"Position: {direction.upper()} {chain}, held {hold_min or '?'} min.\n"
            f"Condition changes since baselines: {lines}\n"
            f"{len(against)} of these are headwinds for this {direction}."
        )
        raw = await _perp_ollama_generate(prompt, model=PERPS_OLLAMA_MODEL,
                                          format_json=True, max_tokens=160, timeout_s=20.0)
        if not raw:
            raw = await _perp_ollama_generate(prompt, model=PERPS_OLLAMA_MODEL,
                                              format_json=True, max_tokens=160, timeout_s=20.0)
        parsed = _perp_parse_llm_json(raw)
        return (parsed.get("summary") or "").strip()
    except Exception as e:
        print(f"[flip] llm digest err: {e}")
        return ""

async def build_flip_alert(chain, market, pos, macro) -> dict | None:
    """The flip detector for a live position. Compares current feed state to
    (a) the entry fingerprint and (b) last cycle's snapshot; returns the raw deltas
    plus a local-LLM one-liner. None when nothing changed (no packet bloat)."""
    try:
        direction = pos.get("direction") or "long"
        current = _flip_current_chain_state(chain, macro)
        # baseline 1: entry fingerprint (stamped at open)
        entry_fp = pos.get("entry_fingerprint") or {}
        entry_state = {"funding_phase": entry_fp.get("funding_phase"),
                       "news_lean": entry_fp.get("news_lean"),
                       "regime": entry_fp.get("regime"), "fear_greed": None}
        # baseline 2: last cycle's snapshot for this chain
        prev = (_load_feed_snapshot().get("chains") or {}).get(chain.split("-")[0], {})
        deltas = (_flip_deltas(direction, entry_state, current, "since_entry")
                  + _flip_deltas(direction, prev, current, "since_last_cycle"))
        if not deltas:
            return None
        hold_min = _episode_hold_minutes(pos)
        summary = await _flip_llm_digest(chain, direction, hold_min, deltas)
        against_ct = sum(1 for d in deltas if d.get("against"))
        if against_ct:
            _perp_pos_event(market, pos, "flip", detail="; ".join(d["note"] for d in deltas if d.get("against")))
        return {
            "changed": True,
            "against_count": against_ct,
            "deltas": deltas,                     # raw, always present (backup)
            "llm_summary": summary or None,       # digested one-liner, None if LLM down
            "headline": (summary or
                         ("; ".join(d["note"] for d in deltas if d["against"])
                          or "; ".join(d["note"] for d in deltas))),
            "_doctrine": ("Conditions that CHANGED since you opened (since_entry) or in the last "
                          "cycle (since_last_cycle). 'against' = headwind for your direction. This "
                          "is an EVENT (it turned), distinct from cycle_check (a current state). "
                          "Reference, not a gate — you decide if a flip warrants adjusting or holding."),
        }
    except Exception as e:
        print(f"[flip] build err: {e}")
        return None



async def build_position_synthesis(mgmt_view: dict) -> str | None:
    """Sprint 63: the SYNTHESIS pass — fuse the managing view's separate lenses
    (precedent, flip_alert, cycle_check, news-vs-position) into ONE two-sentence
    read, with the TENSION between them named. The managing view hands the agent five
    raw feeds; nothing currently reasons ACROSS them. This is light fusion, not deep
    reasoning, and it fires every in-position packet. Uses the optional SI brain
    (LLM_MAIN / LLM_FALLBACK: Grok and/or local Ollama); skipped when the brain is off.

    Informs, never decides: it's a legibility aid — 'here's what all your lenses
    say together' — not a recommendation. Returns one string or None (no lenses to
    fuse / LLM unavailable → the raw lenses stand on their own, unchanged).
    """
    if not _perp_brain_enabled():
        return None
    try:
        cc = (mgmt_view.get("chain_context") or {})
        cyc = cc.get("cycle_check") or {}
        news = cc.get("news") or {}
        prec = cc.get("precedent")
        flip = mgmt_view.get("flip_alert")
        # only synthesize if there's actually more than one signal to fuse
        signals = sum(1 for x in (
            cyc.get("still_confirms") is not None,
            news.get("vs_position") not in (None, "neutral"),
            prec is not None,
            flip is not None,
        ) if x)
        if signals < 2:
            return None

        facts = {
            "position": f"{mgmt_view.get('direction')} {mgmt_view.get('market')}",
            "unrealized_pct": mgmt_view.get("unrealized_pct"),
            "feed_still_confirms": cyc.get("still_confirms"),
            "feed_note": cyc.get("note"),
            "news_vs_position": news.get("vs_position"),
            "precedent": (prec or {}).get("read") if prec else None,
            "flip": (flip or {}).get("headline") if flip else None,
        }
        import json as _json
        prompt = (
            "You fuse a crypto perp position's separate signals into ONE brief. "
            "Two sentences max. First sentence: the net picture. Second sentence: "
            "the TENSION or agreement between the signals (e.g. 'feed still supports "
            "you but precedent says this shape has burned you'). Factual, no advice, "
            "no 'you should'. Reply as JSON {\"brief\": \"...\"}.\n\n"
            f"SIGNALS:\n{_json.dumps(facts)}"
        )
        # SI brain: LLM_MAIN then LLM_FALLBACK (Grok / local Ollama). Off = no brief.
        raw = await _perp_ollama_generate(prompt, model=PERPS_OLLAMA_MODEL,
                                          format_json=True, max_tokens=200, timeout_s=25.0)
        parsed = _perp_parse_llm_json(raw)
        brief = (parsed.get("brief") or "").strip()
        return brief or None
    except Exception as e:
        print(f"[synthesis] err: {e}")
        return None

def _migrate_pnl_ledger_v2(data: dict) -> dict:
    """Sprint 68 — ONE-TIME LEDGER CORRECTION. Every v1 trade stored its own facts
    (entry, exit, size=notional, leverage), so the truth is recoverable: recompute
    gross as notional × price-move (leverage once, not twice) and fees at Jupiter's
    trade rate (historical borrow is unknowable → 0, flagged). Originals preserved
    under _v1 for audit. Totals recomputed. Idempotent via _ledger_v2 flag.
    Expect the totals to SHRINK — that's the ledger telling the truth for the
    first time, not the past getting worse."""
    try:
        if not isinstance(data, dict) or data.get("_ledger_v2"):
            return data
        migrated = 0
        for t in data.get("trades", []) or []:
            if t.get("_ledger") == "v2":
                continue
            e, x, s, lv = t.get("entry_price"), t.get("exit_price"), t.get("size_usd"), t.get("leverage")
            d = (t.get("direction") or "").lower()
            if not (e and x and s and d in ("long", "short")):
                continue
            t["_v1"] = {"pnl_usd": t.get("pnl_usd"), "gross_pnl_usd": t.get("gross_pnl_usd"),
                        "fees_usd": t.get("fees_usd"), "pnl_pct": t.get("pnl_pct")}
            move = (x - e) / e * 100 if d == "long" else (e - x) / e * 100
            gross = s * (move / 100)
            fees = round(s * (JUPITER_TRADE_FEE_PCT / 100), 4)
            t["price_move_pct"] = round(move, 4)
            t["pnl_pct"] = round(move * (lv or 1), 2)
            t["gross_pnl_usd"] = round(gross, 4)
            t["fees_usd"] = fees
            t["pnl_usd"] = round(gross - fees, 4)
            t["hold_hours"] = t.get("hold_hours")   # unknowable for v1 — stays None
            t["_ledger"] = "v2_migrated"
            migrated += 1
        data["total_realized_usd"] = round(sum(t.get("pnl_usd", 0) or 0 for t in data.get("trades", []) or []), 4)
        data["total_fees_usd"] = round(sum(t.get("fees_usd", 0) or 0 for t in data.get("trades", []) or []), 4)
        data["_ledger_v2"] = True
        data["_ledger_v2_note"] = ("Sprint 68: v1 dollar P&L was inflated by the leverage factor "
                                   "(and paid the removed venue's fees). Recomputed from each "
                                   "trade's own entry/exit/notional. Originals under _v1. "
                                   "Historical borrow fees unknowable — omitted, flagged here.")
        if migrated:
            print(f"[ledger v2] migrated {migrated} v1 trades — totals recomputed honestly")
        return data
    except Exception as _me:
        print(f"[ledger v2] migration err: {_me}")
        return data


def record_pnl(direction, entry_price, exit_price, size_usd, leverage, reason, market=None, peak_pct=None, category=None, phase=None, micro_lean=False, position=None):
    if market:
        market = _pos_base(market)
    try:
        # ── Sprint 68: THE LEDGER FIX. size_usd is NOTIONAL (size = collateral ×
        # leverage at every open path). The old math did notional × move% × leverage —
        # leverage applied TWICE. A 10x $500-notional trade on a +2% move booked $100
        # gross; the true number is $10. Every dollar in perp_pnl.json was inflated by
        # the leverage factor, and near breakeven the inflated gross beat the flat
        # fee — booking losers as winners. Leverage is kept for the collateral-relative
        # % display (that IS "+30% on my margin"); dollars use the price move once.
        if direction == "long":
            price_move_pct = (exit_price - entry_price) / entry_price * 100
        else:
            price_move_pct = (entry_price - exit_price) / entry_price * 100
        pnl_pct = price_move_pct * leverage            # % on COLLATERAL (display truth)
        gross_pnl_usd = size_usd * (price_move_pct / 100)   # $ truth: notional × move, once
        # hold time → borrow accrual (position timestamp when available)
        hold_hours = 0.0
        try:
            _pos_ts = ((position or {}).get("timestamp")) or ((load_position(market) or {}).get("timestamp"))
            if _pos_ts:
                hold_hours = max(0.0, (datetime.now(EST) - datetime.fromisoformat(_pos_ts)).total_seconds() / 3600.0)
        except Exception:
            hold_hours = 0.0
        net_pnl_usd, fees_usd = calc_net_pnl(gross_pnl_usd, size_usd, hold_hours)
        # LEVERAGE FIX 2026-10-16: collateral = what was really put up (stored at
        # open), pnl_pct = NET (after fees) % of that collateral. gross kept too.
        gross_pnl_pct = pnl_pct
        _col_real = None
        try:
            _pp = position or load_position(market) or {}
            _col_real = float(_pp.get("collateral") or _pp.get("collateral_usd") or 0) or None
        except Exception:
            _col_real = None
        collateral_usd = _col_real or ((size_usd / leverage) if leverage else size_usd)
        if collateral_usd:
            pnl_pct = net_pnl_usd / collateral_usd * 100
        _cat = category or _classify_close(reason, pnl_pct, peak_pct)
        # micro_lean (Sprint 50): flags this trade as an anticipatory flat-trend
        # front-run so perp_close_report can grade the micro lane separately from
        # confirmed calls. We want its standalone hit-rate before it earns size.
        entry = {"timestamp": datetime.now(EST).isoformat(), "market": market, "direction": direction, "entry_price": entry_price, "exit_price": exit_price, "size_usd": size_usd, "leverage": leverage, "collateral_usd": round(collateral_usd, 4), "price_move_pct": round(price_move_pct, 4), "pnl_pct": round(pnl_pct, 2), "gross_pnl_pct": round(gross_pnl_pct, 2), "pnl_pct_basis": "net_on_collateral", "gross_pnl_usd": round(gross_pnl_usd, 4), "fees_usd": fees_usd, "hold_hours": round(hold_hours, 2), "pnl_usd": net_pnl_usd, "reason": reason, "category": _cat, "peak_pct": round(peak_pct, 2) if peak_pct is not None else None, "phase": phase, "micro_lean": bool(micro_lean), "_ledger": "v2"}
        data = {"trades": [], "total_realized_usd": 0, "total_fees_usd": 0}
        if os.path.exists(PNL_FILE):
            with open(PNL_FILE) as f:
                data = json.load(f)
        data = _migrate_pnl_ledger_v2(data)   # Sprint 68: correct the v1-inflated past
        data["trades"].append(entry)
        data["total_realized_usd"] = round(sum((t.get("pnl_usd") or 0) for t in data["trades"]), 4)
        data["total_fees_usd"] = round(sum((t.get("fees_usd") or 0) for t in data["trades"]), 4)
        with open(PNL_FILE, "w") as f:
            json.dump(data, f, indent=2)
        # Sprint 58: record this close as an episode (entry fingerprint + outcome)
        # so a future live position in the same situation-shape gets the precedent.
        try:
            _pos_for_ep = position if position is not None else (load_position(market) or {})
            _pos_for_ep = dict(_pos_for_ep)
            _pos_for_ep.setdefault("market", market)
            _pos_for_ep.setdefault("direction", direction)
            _pos_for_ep.setdefault("entry_price", entry_price)
            _pos_for_ep.setdefault("leverage", leverage)
            _pos_for_ep.setdefault("size_usd", size_usd)
            record_episode(_pos_for_ep, exit_price, pnl_pct, net_pnl_usd, _cat, reason, peak_pct=peak_pct)
        except Exception as _ee:
            print(f"[episode] hook err: {_ee}")
        return net_pnl_usd, pnl_pct
    except Exception as e:
        print(f"record_pnl error: {e}")
        return None, None


def record_unverified_close(market, pos, reason, category="closed_offbook",
                            mark_at_detection=None, exit_price=None, exit_source=None,
                            pnl_usd=None):
    """polish-2026-10-15 (item 1): EVERY close reaches the ledger.

    Liquidations, chain-side TP/SL, and closes discovered on restart used to be
    cleared from the book without a ledger row, so perp_pnl.json stopped at the
    last signal close. This writes the row anyway. When the exit price is not
    VERIFIED (read off a chain transaction), exit_price stays None and
    exit_verified is False: we record that a close happened, never an invented
    number. The mark seen at detection is kept separately, labelled as such.
    Totals skip None pnl. Never raises."""
    try:
        pos = dict(pos or {})
        entry_price = pos.get("entry_price")
        size_usd = pos.get("size_usd")
        lev = pos.get("leverage")
        row = {"timestamp": datetime.now(EST).isoformat(), "market": market,
               "direction": pos.get("direction"), "entry_price": entry_price,
               "exit_price": exit_price, "exit_verified": exit_price is not None and bool(exit_source),
               "exit_source": exit_source, "mark_at_detection": mark_at_detection,
               "size_usd": size_usd, "leverage": lev,
               "collateral_usd": pos.get("collateral"),
               "pnl_usd": pnl_usd, "fees_usd": None, "reason": reason, "category": category,
               "opened_at": pos.get("timestamp"), "open_tx": pos.get("tx_sig"),
               "peak_pct": pos.get("peak_pct"), "phase": pos.get("phase"),
               "micro_lean": bool(pos.get("micro_lean", False)), "_ledger": "v2",
               "_offbook": True}
        data = {"trades": [], "total_realized_usd": 0, "total_fees_usd": 0}
        if os.path.exists(PNL_FILE):
            with open(PNL_FILE) as f:
                data = json.load(f)
        data = _migrate_pnl_ledger_v2(data)
        # idempotent: one row per opened position
        for t in data.get("trades", []):
            if (t.get("market") == market and t.get("opened_at") and
                    t.get("opened_at") == row["opened_at"] and t.get("_offbook")):
                print(f"[ledger] {market} off-book close already recorded")
                return row
        data["trades"].append(row)
        data["total_realized_usd"] = round(sum((t.get("pnl_usd") or 0) for t in data["trades"]), 4)
        data["total_fees_usd"] = round(sum((t.get("fees_usd") or 0) for t in data["trades"]), 4)
        with open(PNL_FILE, "w") as f:
            json.dump(data, f, indent=2)
        try:
            log(f"close {pos.get('direction')} ({category})", pos.get("direction"), size_usd,
                exit_price if exit_price is not None else "unknown", reason)
        except Exception:
            pass
        return row
    except Exception as e:
        print(f"[ledger] record_unverified_close err: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# Sprint 46: CLOSE-REASON TAXONOMY + TRACK RECORD
# ════════════════════════════════════════════════════════════════════════════
# Win% tells the agent he lost, not WHY. This tags every close with an actionable
# category and turns the loss distribution into one coaching line he can act on.
_ROUND_TRIP_MIN_PEAK = 15.0  # leveraged % peak that counts as "was meaningfully green"


def _classify_close(reason, pnl_pct, peak_pct=None, hit_liq=False):
    """Map a close into a category so the record says WHY it lost. Heuristic:
    reads the reason string, the pnl, and how green it got before dying."""
    r = (reason or "").lower()
    won = (pnl_pct or 0) > 0
    if hit_liq or "liq" in r:
        return "liquidated"
    if "trail" in r:
        return "trailing_stop"                 # locked-in exit, usually green
    if "take profit" in r or r == "tp" or "tp hit" in r:
        return "take_profit"
    if "stop loss" in r or r == "sl" or "sl hit" in r:
        if peak_pct is not None and peak_pct >= _ROUND_TRIP_MIN_PEAK and not won:
            return "round_tripped"             # was green, stopped out red
        return "stopped_out"
    if not won and peak_pct is not None and peak_pct >= _ROUND_TRIP_MIN_PEAK:
        return "round_tripped"                 # went green, gave it all back
    if won:
        return "signal_win" if ("signal" in r or "agent" in r or r == "") else "win_other"
    return "just_wrong"                         # loss, never green: read inverted


def perp_close_report(last_n_losses=10, market=None):
    """Track record + the ONE actionable line. Win% is the scoreboard; the
    loss-category distribution is the coaching signal."""
    try:
        if not os.path.exists(PNL_FILE):
            return None
        with open(PNL_FILE) as f:
            data = json.load(f)
        if not data.get("_ledger_v2"):                      # Sprint 68: correct v1 past
            data = _migrate_pnl_ledger_v2(data)
            try:
                with open(PNL_FILE, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
        trades = data.get("trades", []) or []
        if market:
            trades = [t for t in trades if (t.get("market") or "").upper() == market.upper()]
        if not trades:
            return {"trades": 0, "market": market, "note": "clean slate — no closed trades yet",
                    "record_started": data.get("_reset_at")}
        wins   = [t for t in trades if (t.get("pnl_usd") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl_usd") or 0) < 0]
        # v2.1 field fix (2026-07-19 SOL packet): on a ~$23 account a real win can be
        # < $0.005 — 2dp rounding displayed "wins: 1, avg_win_usd: 0.0", which reads
        # like a broken meter. 4dp matches the ledger's own precision (total_realized
        # is 4dp); micro-accounts get honest cents.
        realized_usd = round(sum((t.get("pnl_usd") or 0) for t in trades), 4)  # THIS chain when filtered
        avg_win  = round(sum((t.get("pnl_usd") or 0) for t in wins) / len(wins), 4) if wins else 0.0
        avg_loss = round(sum((t.get("pnl_usd") or 0) for t in losses) / len(losses), 4) if losses else 0.0
        awl_ratio = round(avg_win / abs(avg_loss), 2) if avg_loss else None

        def _cat(t):
            return t.get("category") or _classify_close(t.get("reason"), t.get("pnl_pct"), t.get("peak_pct"))

        from collections import Counter
        cat_counts   = Counter(_cat(t) for t in trades)
        recent_loss  = losses[-last_n_losses:]
        loss_counts  = Counter(_cat(t) for t in recent_loss)

        # Sprint 50: micro-lean lane — grade the anticipatory flat-trend front-runs
        # SEPARATELY. This is the whole point of the micro tier: know whether flat-
        # state front-running has a real edge before it earns more size. A weak lane
        # here means the front-run is noise; a strong one means feed's flat-trend
        # confluence read is genuinely predictive and the tier can graduate.
        micro_trades = [t for t in trades if t.get("micro_lean")]
        micro_lane = None
        if micro_trades:
            m_wins = [t for t in micro_trades if (t.get("pnl_usd") or 0) > 0]
            m_real = round(sum((t.get("pnl_usd") or 0) for t in micro_trades), 2)
            micro_lane = {
                "trades":        len(micro_trades),
                "wins":          len(m_wins),
                "win_rate_pct":  round(len(m_wins) / len(micro_trades) * 100, 1),
                "realized_usd":  m_real,
                "note":          ("divergence lane — graded apart from confirmed calls. "
                                  "Needs a real sample before trusting; below ~50% = the flat-trend "
                                  "front-run isn't paying, tighten the confluence threshold in feed."),
            }

        insight = None
        if recent_loss:
            top_cat, top_n = loss_counts.most_common(1)[0]
            if top_n / len(recent_loss) >= 0.5:
                insight = {
                    "liquidated":    f"{top_n}/{len(recent_loss)} recent losses were LIQUIDATIONS. Leverage is too hot for your stop distance — size leverage down.",
                    "round_tripped": f"{top_n}/{len(recent_loss)} recent losses ROUND-TRIPPED from green. You're giving winners back — take profit sooner or tighten the trail.",
                    "stopped_out":   f"{top_n}/{len(recent_loss)} recent losses STOPPED OUT. Stops too tight or entries too early — widen SL or wait for confirmation.",
                    "just_wrong":    f"{top_n}/{len(recent_loss)} recent losses were the READ INVERTED (never went green). Re-check the cycle gate before entry.",
                }.get(top_cat, f"{top_n}/{len(recent_loss)} recent losses were '{top_cat}'.")

        return {
            "trades":               len(trades),
            "wins":                 len(wins),
            "losses":               len(losses),
            "win_rate_pct":         round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "realized_usd":         realized_usd,       # dollars on THIS chain (the honest ledger)
            "avg_win_usd":          avg_win,
            "avg_loss_usd":         avg_loss,
            "avg_win_loss_ratio":   awl_ratio,          # >1 winners bigger; <1 losers bigger
            "total_realized_usd":   data.get("total_realized_usd"),   # all chains (context)
            "category_distribution":     dict(cat_counts),
            "recent_loss_distribution":  dict(loss_counts),
            "micro_lean_lane":      micro_lane,   # Sprint 50: anticipatory front-run, graded apart (None if no micro trades yet)
            "coaching_insight":     insight,
            "record_started":       data.get("_reset_at") or (trades[0].get("timestamp") if trades else None),
            "_doctrine": "Win% is the scoreboard. The loss-category distribution is what to ACT on. Fix the dominant loss reason, not the number.",
        }
    except Exception as e:
        print(f"perp_close_report error: {e}")
        return None


def reset_perp_record(archive=True):
    """Clean-slate the perp track record. Archives the old file (timestamped)
    and starts fresh with a reset marker — for when everything's officially
    stamped and the old win% no longer reflects the current system."""
    try:
        archived = None
        if archive and os.path.exists(PNL_FILE):
            ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
            archived = PNL_FILE.replace(".json", f"_archive_{ts}.json")
            os.replace(PNL_FILE, archived)
        fresh = {"trades": [], "total_realized_usd": 0, "total_fees_usd": 0,
                 "_reset_at": datetime.now(EST).isoformat(), "_archived_to": archived}
        with open(PNL_FILE, "w") as f:
            json.dump(fresh, f, indent=2)
        print(f"[perp] track record reset. archived -> {archived}")
        return {"reset": True, "archived_to": archived}
    except Exception as e:
        print(f"reset_perp_record error: {e}")
        return {"reset": False, "error": str(e)}


async def get_sol_price() -> float | None:
    """2026-07-26 — the agent caught the boot banner printing SOL @ $94.33 while the
    live packet read $74.885, and called it cosmetic. It was NOT: this function
    also converts wallet SOL into ammo USD and size_sol into size_usd, so a 26%
    wrong price silently inflates every dollar figure downstream.

    Root cause: the old code took the SINGLE highest-liquidity SOL/USDC pair and
    trusted its priceUsd. One stale or manipulated pool — and DexScreener lists
    plenty — moves that number 26% with nothing to contradict it.

    Fix: consensus, not a single quote. Take the MEDIAN of the top USDC pairs
    (one bad pool cannot move a median), cross-check against CoinGecko, and when
    the two disagree beyond the band, SAY SO instead of silently picking one."""
    _SANITY_BAND = 0.08          # 8% disagreement between independent sources
    dex_px = None
    cg_px = None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112")
            if r.status_code == 200:
                pairs = r.json().get("pairs") or []
                usdc = [p for p in pairs
                         if (p.get("quoteToken") or {}).get("symbol", "").upper() == "USDC"
                         and p.get("chainId", "solana") == "solana"
                         and float((p.get("liquidity") or {}).get("usd", 0) or 0) > 50_000]
                usdc.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
                prices = []
                for p in usdc[:5]:
                    try:
                        v = float(p.get("priceUsd") or 0)
                        if v > 0:
                            prices.append(v)
                    except Exception:
                        continue
                if prices:
                    prices.sort()
                    dex_px = prices[len(prices) // 2]      # median of the deepest pools
                    if len(prices) >= 3 and (prices[-1] - prices[0]) / dex_px > 0.15:
                        print(f"[price] SOL pools disagree widely: {prices[0]:.2f}–{prices[-1]:.2f} "
                              f"→ using median {dex_px:.2f}")
    except Exception as e:
        print(f"sol price dexscreener: {e}")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "solana", "vs_currencies": "usd"})
            if r.status_code == 200:
                v = r.json().get("solana", {}).get("usd")
                if v:
                    cg_px = float(v)
    except Exception as e:
        print(f"sol price coingecko: {e}")

    if dex_px and cg_px:
        if abs(dex_px - cg_px) / cg_px > _SANITY_BAND:
            # Independent sources disagreeing is information, not noise. CoinGecko
            # is the harder one to manipulate, so it wins the tie — loudly.
            print(f"[price] ⚠ SOL SOURCES DISAGREE: dex median ${dex_px:.2f} vs coingecko "
                  f"${cg_px:.2f} ({abs(dex_px - cg_px) / cg_px * 100:.1f}%) — using coingecko. "
                  f"Every USD figure this sweep depends on this number.")
            return cg_px
        return dex_px
    if dex_px or cg_px:
        return dex_px or cg_px
    # Both primary quotes failed. An explicit open must not die on a flake.
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "SOLUSDT"})
            if r.status_code == 200:
                v = float((r.json() or {}).get("price") or 0)
                if v > 0:
                    print(f"[price] dex+coingecko failed, using binance SOLUSDT ${v:.2f}")
                    return v
    except Exception as e:
        print(f"sol price binance: {e}")
    # 2026-10-07: binance returns 451 from this host. coinbase and kraken still quote.
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.coinbase.com/v2/prices/SOL-USD/spot")
            if r.status_code == 200:
                v = float(((r.json() or {}).get("data") or {}).get("amount") or 0)
                if v > 0:
                    print(f"[price] dex+cg+binance failed, using coinbase SOL-USD ${v:.2f}")
                    return v
    except Exception as e:
        print(f"sol price coinbase: {e}")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.kraken.com/0/public/Ticker", params={"pair": "SOLUSD"})
            if r.status_code == 200:
                result = (r.json() or {}).get("result") or {}
                row = result.get("SOLUSD") or next(iter(result.values()), {})
                last = (row.get("c") or [None])[0]
                v = float(last or 0)
                if v > 0:
                    print(f"[price] using kraken SOLUSD ${v:.2f}")
                    return v
    except Exception as e:
        print(f"sol price kraken: {e}")
    return None


async def get_wallet_balance_usd() -> float:
    """Sprint 57: name kept for call-site compat, but this now returns the REAL
    ammo — the SOL wallet's USD value. Drift is gone; "collateral available" is
    whatever SOL the wallet holds (it funds every Jupiter open). The one caller
    left is the intel_scanner's SUGGESTED sizing for alert packets (never a live
    trade), so an approximate figure is fine and a real one is better than the
    dead wallet_balance.json this used to read.
    """
    try:
        _conn = AsyncClient(RPC_URL, commitment=Confirmed)
        try:
            _sol = await get_sol_balance(_conn)
        finally:
            try:
                await _conn.close()
            except Exception:
                pass
        _px = await get_sol_price()
        if _sol and _px:
            _usd = round(_sol * _px, 2)
            try:
                _maybe_stamp_wallet_balance_fallback(_sol, _usd)
            except Exception:
                pass
            return _usd
    except Exception as e:
        print(f"[perp] ammo read error: {e}")
    return 0.0


async def get_sol_balance(connection: AsyncClient) -> float:
    try:
        resp = await connection.get_balance(_kp.pubkey())
        return resp.value / 1e9
    except Exception as e:
        print(f"balance error: {type(e).__name__}: {e!r} (RPC_URL unreachable or wrong? check bots/.env)")
        return 0.0
# ============================================================================
# wallet_balance.json stamp — perps writes a live SOL stamp when the file is stale
# ============================================================================
WALLET_BALANCE_FILE = os.path.join(WS_BOT_LOGS, "wallet_balance.json")
_WALLET_STAMP_STALE_SEC = 3600  # age >60m = untrusted


def _wallet_stamp_age_sec(data):
    if not isinstance(data, dict):
        return None
    raw = data.get("updated_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=EST)
        return (datetime.now(EST) - ts.astimezone(EST)).total_seconds()
    except Exception:
        return None


def _wallet_stamp_stale(data) -> bool:
    """True when wallet_balance.json is (missing/unparseable/>60m)."""
    if not data or not isinstance(data, dict):
        return True
    if data.get("balance_sol") is None and data.get("balance_usd") is None:
        return True
    age = _wallet_stamp_age_sec(data)
    if age is None:
        return True
    return age > _WALLET_STAMP_STALE_SEC


def _maybe_stamp_wallet_balance_fallback(sol, usd=None) -> bool:
    """If wallet_balance.json is stale/missing (>60m), perps writes a live SOL stamp."""
    try:
        sol_f = float(sol or 0)
        if sol_f <= 0:
            return False
        cur = {}
        try:
            if os.path.exists(WALLET_BALANCE_FILE):
                with open(WALLET_BALANCE_FILE, encoding="utf-8-sig") as f:
                    cur = json.load(f) or {}
        except Exception:
            cur = {}
        if not _wallet_stamp_stale(cur):
            return False
        src = str((cur or {}).get("source") or "")
        age = _wallet_stamp_age_sec(cur)
        payload = dict(cur) if isinstance(cur, dict) else {}
        payload["balance_sol"] = round(sol_f, 9)
        if usd is not None:
            try:
                payload["balance_usd"] = round(float(usd), 2)
            except Exception:
                pass
        payload["updated_at"] = datetime.now(EST).isoformat()
        payload["source"] = "perps_wallet_fallback"
        payload["fallback_of"] = src or "missing"
        payload["prev_stamp_age_sec"] = round(age, 0) if age is not None else None
        payload["note"] = "perps stamp: SOL ammo is live RPC. age >60m = treat as untrusted"
        ok = bool(_s27_save_json(WALLET_BALANCE_FILE, payload))
        if ok:
            print("[perp] wallet fallback stamped %.4f SOL (stamp stale)" % sol_f)
        return ok
    except Exception as e:
        print("[perp] wallet fallback stamp err: %s" % e)
        return False

# ============================================================================
# SPRINT 27 HOTFIX 8 — Limit orders + position adjust + position scan + user init
# ============================================================================

# ============================================================================
# END SPRINT 27 HOTFIX 8 ADDITIONS
# ============================================================================


# ════════════════════════════════════════════════════════════════════════════
# SPRINT 33: DAILY_READ.JSON CONSUMPTION (this session)
# ════════════════════════════════════════════════════════════════════════════
# perps_bot reads feed's daily_read.json before opening positions.
# Decisions are gated on the recommendation + abort_conditions + regime_break.
# Every decision writes an audit packet to inbox_results/done/ for the agent to
# review. The agent's meeting files and explicit override flags still win.

def load_published_call() -> dict | None:
    """Best-effort read of the agent's published editorial calls (feed-site mirror).
    Returns a compact dict of the perp-relevant calls, or None on miss/error.
    REFERENCE ONLY , never gates a trade; absent editorial just means no context.
    """
    try:
        data = _feed("editorial_calls")
        if not isinstance(data, dict):
            return None
        out = {}
        for item in _EDITORIAL_PERP_ITEMS:
            call = data.get(item)
            if isinstance(call, dict) and call.get("value") not in (None, ""):
                out[item] = {
                    "value":     call.get("value"),
                    "reasoning": call.get("reasoning"),
                    "called_at": call.get("called_at"),
                }
        return out or None
    except Exception as e:
        print(f"[editorial] published_call load err: {e}")
        return None


def _load_daily_read_uncached() -> dict | None:
    """Force-read daily_read.json from disk. Returns dict on success, None on miss/error."""
    try:
        data = _feed("daily_read")
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        print(f"[daily_read] load err: {e}")
        return None


def load_daily_read(force_refresh: bool = False) -> dict | None:
    """Load daily_read.json with 10min in-memory cache + mtime change detection.
    
    Returns the daily_read dict, or None if file missing / unreadable / never written.
    
    Cache invalidates if:
      - force_refresh=True
      - cache is older than _DAILY_READ_TTL_SECONDS
      - file mtime has changed since last load (fresh sweep happened)
    """
    now_ts = time.time()
    
    # Check cache validity
    if not force_refresh and _DAILY_READ_CACHE.get("data") is not None:
        age = now_ts - _DAILY_READ_CACHE.get("_loaded_at_ts", 0)
        if age < _DAILY_READ_TTL_SECONDS:
            # Cache fresh — but verify file hasn't been updated (cheap mtime check)
            try:
                current_mtime = 0.0
                if current_mtime == _DAILY_READ_CACHE.get("_file_mtime", 0):
                    return _DAILY_READ_CACHE["data"]
            except OSError:
                # File may have disappeared — fall through to reload
                pass
    
    # Need a fresh load
    data = _load_daily_read_uncached()
    if data is None:
        # Don't poison cache on load failure — keep last good copy if any
        return _DAILY_READ_CACHE.get("data")
    
    try:
        mtime = 0.0
    except OSError:
        mtime = now_ts
    
    _DAILY_READ_CACHE["data"]          = data
    _DAILY_READ_CACHE["_loaded_at_ts"] = now_ts
    _DAILY_READ_CACHE["_file_mtime"]   = mtime
    return data


def _normalize_perps_recommendation(rec_str: str) -> dict:
    """Parse trade_recommendation.perps string → structured directive.
    
    Inputs (per feed's _compute_trade_recommendation):
      "flat" | "neutral"
      "long_micro" | "short_micro"                             (Sprint 50: anticipatory)
      "long_small" | "long_medium" | "long_aggressive"
      "short_small" | "short_medium" | "short_aggressive"
    
    micro tier = feed's flat-trend front-run: the score is flat but the regime
    confluence vote is leaning. Smallest size (0.15x), tightest stop, and flagged
    micro_lean=True so the grader tracks its hit-rate on a SEPARATE lane from
    confirmed calls — we want to know if flat-state front-running actually has an
    edge before it earns more size. The agent still owns the act/size/skip decision.
    
    Returns:
      {
        "allowed_directions": [],          # which directions can open new positions
        "size_multiplier":    1.0,         # scales requested size_usd
        "aggression":         "neutral",   # micro | small | medium | aggressive | neutral | flat
        "label":              <original string>,
        "block_new_positions": bool,       # true for flat/neutral
        "micro_lean":         bool,        # true for long_micro/short_micro (anticipatory, graded apart)
      }
    """
    rec = (rec_str or "neutral").lower().strip()
    out = {
        "allowed_directions":   [],
        "size_multiplier":      1.0,
        "aggression":           "neutral",
        "label":                rec,
        "block_new_positions":  False,
        "micro_lean":           False,
    }
    
    if rec == "flat":
        out["block_new_positions"] = True
        out["aggression"] = "flat"
        return out
    
    if rec == "neutral":
        out["block_new_positions"] = True  # neutral = stay flat on new entries
        out["aggression"] = "neutral"
        return out
    
    # long_* / short_*
    if rec.startswith("long_"):
        out["allowed_directions"] = ["long"]
        tier = rec.split("_", 1)[1] if "_" in rec else "medium"
    elif rec.startswith("short_"):
        out["allowed_directions"] = ["short"]
        tier = rec.split("_", 1)[1] if "_" in rec else "medium"
    else:
        # Unknown label — be defensive, treat as neutral (block)
        print(f"[daily_read] WARN: unknown perps recommendation '{rec}' — treating as neutral")
        out["block_new_positions"] = True
        return out
    
    out["aggression"]      = tier
    out["size_multiplier"] = _AGGRESSION_SIZE_MULTIPLIER.get(tier, 0.50)  # medium default
    out["micro_lean"]      = (tier == "micro")
    return out


def _evaluate_against_daily_read(action: str, requested_size_usd: float,
                                  override: bool = False, market: str = None) -> tuple:
    """Evaluate a proposed perps action against the latest daily_read recommendation.
    
    Args:
      action:             "long" | "short" (close/adjust/scan don't pass through here)
      requested_size_usd: the size_usd the agent requested
      override:           True if the agent's signal explicitly bypassed gating
    
    Returns:
      (allow: bool, scaled_size_usd: float, reason: str, audit_entry: dict)
    
    The audit_entry captures everything for the inbox_results write.
    """
    audit = {
        "action_requested":   action,
        "size_requested_usd": requested_size_usd,
        "size_final_usd":     requested_size_usd,
        "evaluated_at":       datetime.now(EST).isoformat(),
        "override_flag":      bool(override),
        "decision":           None,
        "reason":             None,
        "daily_read_loaded":  False,
        "daily_read_age_sec": None,
        "recommendation":     None,
        "abort_conditions":   [],
        "regime_break":       False,
    }
    
    # Override path — the agent's explicit "I know what I'm doing" flag
    if override:
        audit["decision"] = "allow"
        audit["reason"]   = "OVERRIDE: The agent flagged override_daily_read=true on signal — gating bypassed but audit recorded"
        return (True, requested_size_usd, audit["reason"], audit)
    
    dr = load_daily_read()

    # Sprint 43: classify freshness. Missing OR stale → DEGRADED mode (small-tier
    # cap, not a blind full-size trade and not a freeze). Fresh → normal gates.
    freshness, age_sec, threshold_sec = _daily_read_freshness(dr)
    audit["daily_read_freshness"]     = freshness
    audit["daily_read_age_sec"]       = age_sec
    audit["daily_read_stale_after_s"] = threshold_sec

    if freshness in ("missing", "stale"):
        # DEGRADED — GO SEEK THE INFO. OWNER'S RULING 2026-07-26:
        #   "if the daily read is degraded that doesn't mean scale down lmao. just
        #    go seek the info. he has the skills for it — bridge skill, proactive
        #    agent, problem solver. if the conclusion is inconclusive then sure decline"
        # This used to cap to small-tier notional, which is the worst of both: it
        # traded ANYWAY, just smaller, and nobody went and found out what was wrong.
        # Trading blind at 25% size is still trading blind. Now the request lands
        # intact and the blindness becomes a TASK — and if the agent can't resolve it,
        # HE declines. The gate does not decide that for him.
        degraded = _normalize_perps_recommendation(f"{action}_{_DEGRADED_DIRECTION_TIER}")
        audit["daily_read_loaded"]   = (dr is not None)
        audit["degraded_mode"]       = True
        audit["directive"]           = {**degraded, "aggression": f"degraded_{_DEGRADED_DIRECTION_TIER}"}
        audit["size_final_usd"]      = round(requested_size_usd, 2)
        audit["size_requested_usd"]  = round(requested_size_usd, 2)
        audit["decision"]            = "allow_degraded_investigate"
        audit["requires_investigation"] = True
        audit["investigation_paths"] = [
            "bridge skill — read feed's state directly, is the process alive?",
            "proactive agent — is macro.json fresh? did feed die or just lag?",
            "problem solver — reconstruct the regime read from packet macro/feed "
            "fields you already have",
        ]
        if freshness == "missing":
            why = "feed daily_read never loaded (feed down or first-boot gap)"
        else:
            why = f"feed daily_read STALE ({age_sec:.0f}s old > {threshold_sec:.0f}s threshold)"
        scaled = requested_size_usd
        audit["reason"] = (
            f"DEGRADED: {why}. Your requested ${requested_size_usd:.2f} LANDS AS ASKED — "
            f"the gate does not shrink you for being blind. But you ARE blind: GO FIND OUT. "
            f"Use the bridge skill / proactive agent / problem solver to resolve why the "
            f"read is {freshness}, then size with an actual regime view. If you investigate "
            f"and it stays inconclusive, DECLINE the trade yourself and say so — an "
            f"unresolved blind spot is a reason not to fire, not a reason to fire smaller."
        )
        # Transition tracking for one-shot ping (caller fires the actual notify)
        if not _DAILY_READ_DEGRADE_STATE["degraded"]:
            audit["degrade_transition"] = True
            _DAILY_READ_DEGRADE_STATE["degraded"] = True
        return (True, scaled, audit["reason"], audit)

    # Fresh daily_read — if we were degraded, mark recovery for a one-shot ping
    if _DAILY_READ_DEGRADE_STATE["degraded"]:
        audit["recovery_transition"] = True
        _DAILY_READ_DEGRADE_STATE["degraded"] = False

    audit["daily_read_loaded"] = True
    
    # Extract trade_recommendation block
    rec = dr.get("trade_recommendation") or {}
    perps_label = rec.get("perps", "neutral")
    abort_conditions = rec.get("abort_conditions") or []
    regime_break = bool(dr.get("regime_break_signal", False))
    
    audit["recommendation"]   = perps_label
    audit["abort_conditions"] = list(abort_conditions)
    audit["regime_break"]     = regime_break
    
    # GATE 1: regime_break → hard block
    if regime_break:
        audit["decision"] = "block"
        audit["reason"]   = "REGIME BREAK detected (daily_read.regime_break_signal=true) — halt new positions"
        return (False, 0.0, audit["reason"], audit)
    
    # ── GATE 2: SIDE-SMART ABORT (2026-08-08) ───────────────────────────────
    # Built to `owner_inbox/agent - patch_perps_side_smart_abort_20260808.md`.
    # Consumes the abort_policy its companion feed patch now emits.
    #
    # Receipt: a correctly-sized SOL long $10@10x was hard-blocked by an abort
    # whose own reasoning was SHORT protection - "everyone shorting, squeeze risk
    # high, trim short". This gate treated ANY non-empty abort_conditions as
    # block-all, so the abort killed the exact trade it was arguing for.
    #
    # BACKCOMPAT LADDER, and it only ever gets LOOSER when feed explicitly says
    # which side is at risk. No policy means the old block-all, unchanged.
    _pol = rec.get("abort_policy")
    _side = (action or "").lower()
    if isinstance(_pol, dict) and _pol.get("class"):
        audit["abort_policy_class"] = _pol.get("class")
        audit["abort_policy_confidence"] = _pol.get("confidence")
        if _pol.get("active"):
            _blocks = [str(x).lower() for x in (_pol.get("blocks") or [])]
            _allows = [str(x).lower() for x in (_pol.get("allows") or [])]
            _probe_only = False
            if _side in ("long", "short"):
                if _side in _allows:
                    pass                              # side explicitly cleared
                elif f"{_side}_probe_min" in _allows:
                    _probe_only = True                # cleared at MIN OPEN ONLY
                elif _side in _blocks:
                    audit["decision"] = "block"
                    audit["ladder_step"] = "abort_policy_side_block"
                    audit["reason"] = (f"ABORT [{_pol.get('class')}] blocks {_side}: "
                                       f"{str(_pol.get('reason') or '')[:120]}")
                    return (False, 0.0, audit["reason"], audit)
                else:
                    # Not named either way. An abort is live and this side was not
                    # explicitly cleared, so it stays blocked - silence is not a
                    # permission slip when a policy is active.
                    audit["decision"] = "block"
                    audit["ladder_step"] = "abort_policy_side_block"
                    audit["reason"] = (f"ABORT [{_pol.get('class')}] does not clear {_side} "
                                       f"(allows={_allows or 'none'})")
                    return (False, 0.0, audit["reason"], audit)

            if _probe_only:
                # PROBE MIN LAW: venue min open only, $10 collateral @ 10x. Nothing
                # - not reverse_size, not conviction shrink - may undercut min-open,
                # and nothing above it is permitted on a probe-only clearance.
                # The real min-open law: PERP_MIN_COLLATERAL_USD ($10) x
                # PERP_MIN_LEVERAGE (10x) = $100 notional. Resolved through the
                # override path so a meeting tune moves the probe ceiling with it,
                # rather than the probe silently drifting from the venue floor.
                _min_notional = float(_perp_get_override("PERP_MIN_NOTIONAL_USD",
                                                         PERP_MIN_NOTIONAL_USD))
                audit["ladder_step"] = "abort_policy_probe_only_size"
                audit["abort_probe_only"] = True
                audit["abort_probe_max_notional_usd"] = _min_notional
                if float(requested_size_usd or 0) > _min_notional * 1.05:
                    audit["decision"] = "block"
                    audit["reason"] = (f"ABORT [{_pol.get('class')}] clears {_side} as MIN PROBE ONLY "
                                       f"(${_min_notional:.0f} notional); ${float(requested_size_usd):.0f} "
                                       f"requested - resend at min open or not at all")
                    return (False, 0.0, audit["reason"], audit)
                audit["abort_note"] = (f"probe-min clearance under {_pol.get('class')}: "
                                       f"min open only, no size-up")
    elif abort_conditions:
        # LEGACY: no policy emitted (older feed build) - old block-all, unchanged.
        audit["decision"] = "block"
        audit["ladder_step"] = "abort_conditions_legacy_block_all"
        first_cond = abort_conditions[0] if isinstance(abort_conditions[0], str) else str(abort_conditions[0])
        audit["reason"]   = f"ABORT condition active: {first_cond[:150]}"
        return (False, 0.0, audit["reason"], audit)
    
    # PER-COIN MICRO-LEAN (Sprint 50): before the book-level direction gate, check
    # if THIS coin has its own micro-lean in daily_read.micro_lean_by_coin. Each
    # coin leans on its own confluence (shared BTC regime + that coin's projection),
    # so SOL can micro-lean long while the book-level perps field is flat. A per-coin
    # micro-lean BYPASSES the book-level direction gate (GATE 3) — it's small (0.15x),
    # self-graded, and intentionally independent. It does NOT bypass regime_break or
    # abort (GATES 1-2 above already returned block); an abort flattens everything.
    micro_by_coin = rec.get("micro_lean_by_coin") or {}
    _coin = None
    if market:
        _coin = (market or "").upper().split("-")[0]  # "SOL-PERP" -> "SOL"
    _coin_micro = micro_by_coin.get(_coin) if _coin else None
    if _coin_micro:
        micro_label = _coin_micro.get("perps", "")       # e.g. "long_micro"
        micro_dir_allowed = _coin_micro.get("direction") # "long" | "short"
        if action == micro_dir_allowed:
            directive = _normalize_perps_recommendation(micro_label)
            audit["directive"]       = directive
            audit["micro_lean"]      = True
            audit["micro_lean_coin"] = _coin
            # Advisory only (2026-07-26) — the micro-lean multiplier no
            # longer cuts the request. THIS is the path that produced the $150 ->
            # $25 haircut on the SOL long.
            scaled = requested_size_usd
            _sug   = round(requested_size_usd * directive["size_multiplier"], 2)
            audit["size_final_usd"]           = round(scaled, 2)
            audit["size_requested_usd"]       = round(requested_size_usd, 2)
            audit["size_advisory_usd"]        = _sug
            audit["size_advisory_multiplier"] = directive["size_multiplier"]
            audit["size_divergence"]          = (directive["size_multiplier"] < 1.0)
            audit["decision"]                 = "allow_micro_lean"
            audit["reason"] = (
                f"MICRO-LEAN {_coin} {action}: coin's own confluence leans "
                f"{micro_dir_allowed} (conviction {_coin_micro.get('conviction')}, "
                f"score {_coin_micro.get('score')}) while book is flat. Anticipatory "
                f"divergence reading — graded on its own lane. "
                f"Landing your REQUESTED ${requested_size_usd:.2f}; the micro tier would "
                f"suggest ${_sug:.2f} ({directive['size_multiplier']:.0%}) — advisory only, "
                f"say why in `reason`."
            )
            return (True, scaled, audit["reason"], audit)
        else:
            # Coin has a micro-lean but the agent requested the OPPOSITE direction.
            # Don't silently size him into a fight with the coin's own read — fall
            # through to the normal book-level gate (which may block or allow).
            audit["micro_lean_direction_mismatch"] = (
                f"{_coin} micro-leans {micro_dir_allowed} but signal requests {action} "
                f"— falling through to book-level gate"
            )

    # GATE 3: parse recommendation, check direction alignment + size scaling
    directive = _normalize_perps_recommendation(perps_label)
    audit["directive"] = directive
    
    # ── OWNER/AGENT RULING 2026-07-27: DISAGREEMENT IS NOT A KILL-SWITCH ────
    # Extends the 2026-07-26 ruling ("a gate may decline, a gate may not
    # resize") to direction itself. The agent:
    #
    #   "daily_read as cop — direction mismatch / flat / micro labels still
    #    feel like the real decider. override exists, but saying
    #    override_daily_read: true feels like breaking a rule instead of
    #    owning a call."
    #
    # That is the tell. An override that feels like rule-breaking is a gate
    # wearing a boss costume, and it teaches him to argue with the bot instead
    # of reading the tape. daily_read is a WEATHER REPORT. It observes; it does
    # not permit.
    #
    # A gate may still HARD DECLINE on a true kill-switch — regime_break and
    # abort_conditions above, which are about the machine being broken or the
    # market being untradeable, not about a difference of opinion. Those stay.
    #
    # Direction mismatch and flat/micro labels are OPINIONS. They now record a
    # CONFLICT with the facts attached and let the trade through. He owns the
    # call; the audit owns the receipt; the autopsy grades whether he was right
    # to disagree. No override flag needed to act on his own read.
    _conflicts = []
    if directive["block_new_positions"]:
        _conflicts.append({
            "type": "regime_label_discourages_new",
            "observed_bias": directive["label"],
            "requested": action,
            "fact": (f"daily_read reads '{directive['label']}' — mechanically that leans "
                     f"against opening new positions right now."),
        })
    if action not in directive["allowed_directions"]:
        _conflicts.append({
            "type": "direction_mismatch",
            "observed_bias": directive["label"],
            "mechanically_favoured": list(directive["allowed_directions"]),
            "requested": action,
            "fact": (f"daily_read leans {directive['allowed_directions']}; you are opening "
                     f"{action}. Front-running the read is a legitimate call — the tape is "
                     f"yours to read and the mechanical layer is one input."),
        })

    audit["conflicts"] = _conflicts
    audit["conflict"] = bool(_conflicts)
    if _conflicts:
        audit["conflict_note"] = (
            "CONFLICT RECORDED, NOT BLOCKED. You are trading against the mechanical read. "
            "That is allowed and does not require an override flag — disagreeing with a "
            "weather report is not rule-breaking. Put your reasoning in `reason`: the "
            "autopsy grades whether the disagreement was right, and a well-reasoned "
            "conflict that pays is the most valuable row in the record."
        )
    
    # GATE 4: ADVISORY ONLY — OWNER'S RULING 2026-07-26.
    # "nothing should auto decide the position. whatever the daily read says is
    #  just reasoning layers." This gate used to do
    #      scaled = requested_size_usd * directive["size_multiplier"]
    #  which silently cut the agent's request by up to 85% (micro = 0.15). On the
    #  2026-07-26 SOL long he asked for ~$150 and got $25 — then explained the
    #  small size as his OWN reasoning ("size_tilt down keeps lev at 5x"). He
    #  was rationalising a haircut another bot imposed without telling him.
    #  A GATE MAY DECLINE. A GATE MAY NOT RESIZE. Blocking is a boundary;
    #  sizing is the agent's decision and stays his.
    scaled = requested_size_usd
    _sug   = round(requested_size_usd * directive["size_multiplier"], 2)
    audit["size_final_usd"]          = round(scaled, 2)
    audit["size_requested_usd"]      = round(requested_size_usd, 2)
    audit["size_advisory_usd"]       = _sug
    audit["size_advisory_multiplier"] = directive["size_multiplier"]
    audit["size_divergence"]         = (directive["size_multiplier"] < 1.0)
    audit["decision"]                = "allow"
    # The owner/the agent 2026-07-27: `observed_bias` replaces the verb-shaped label.
    # "trade_recommendation / stand_down wording — same as EXIT_FULL. lands as
    #  verdict, not observation." A bias is something the layer OBSERVES. It is
    #  not a position it takes on what he should do.
    audit["observed_bias"] = directive["label"]
    if directive["size_multiplier"] < 1.0:
        # ── THE SCAR (The agent, 2026-07-27) ───────────────────────────────────
        # "after getting resized once, i start pre-shrinking myself so the bot
        #  'agrees'. size is mine now, but the scar is still there."
        #
        # The old copy read: "You are sizing above the regime read: your
        # `reason` field must say why." That is a JUSTIFICATION DEMAND. It
        # frames his size as a transgression needing defence — and the cheapest
        # way to avoid defending a number is to shrink it. The gate stopped
        # resizing him in July and he kept shrinking anyway, because the
        # sentence was still doing the resizing.
        #
        # `reason` is a RECORD, not a defence. Sizing above a mechanical
        # suggestion is ordinary: the suggestion is one input and cannot see his
        # thesis. There is nothing to justify.
        audit["reason"] = (
            f"Opening at YOUR size ${requested_size_usd:.2f}. For reference, the "
            f"{directive['label']} regime read would scale to ${_sug:.2f} "
            f"({directive['size_multiplier']:.0%}) — that is a reference point, not a "
            f"ceiling, and it is not applied. Your size is correct by definition because "
            f"it is yours. `reason` is a record for the autopsy, not a justification: "
            f"the mechanical layer has no standing to ask you to defend a number."
        )
    else:
        audit["reason"] = f"ALLOWED: daily_read {directive['label']} agrees with the requested size"
    return (True, scaled, audit["reason"], audit)


_RELAY_ACK_SEEN: dict = {}   # relay_name -> hash of the content we last acked


def relay_ack_once(relay_name: str, raw: str, kind: str, ok: bool, summary: str,
                   detail: dict | None = None) -> None:
    """Ack a relay file at most once per DISTINCT content.

    A bad-JSON or empty file is left in place for retry, so a naive ack fires on
    every poll — 360 packets an hour at SIGNAL_POLL=10. Keying on a hash of the
    content means the agent gets told once, and told AGAIN only if he changes the
    file and it is still wrong. Fixing it clears the entry naturally.
    """
    try:
        import hashlib
        h = hashlib.sha256((raw or "").encode("utf-8", "replace")).hexdigest()[:16]
        if _RELAY_ACK_SEEN.get(relay_name) == h:
            return
        _RELAY_ACK_SEEN[relay_name] = h
        relay_ack(relay_name, kind, ok, summary, detail)
    except Exception as e:
        print(f"[relay_ack_once] err: {e!r}")


def _perp_bus_ack(relay_name: str, kind: str, ok: bool, summary: str,
                  detail: dict | None = None, needs_owner: bool = False) -> None:
    """Mirror a relay ack onto the agent_ack_v1 BUS (2026-08-08, wire-ack closeout).

    relay_ack already told the agent by writing a packet - but a packet is not the
    bus. The agent's wire ledger checks fired signals off against
    bots/bot_logs/agent_acks.jsonl, so a perps relay consume produced a visible
    packet and NO checkoff: the ledger saw a fire with no reply and would have
    red-flagged a signal that actually landed.

    Parallel to the packet, never a replacement. Soft-fails - agent_ack.py may not
    be deployed, and an ack emit must never take down a trading loop.
    """
    try:
        import agent_ack as _jack
        _det = detail if isinstance(detail, dict) else {}
        _jack.write_ack(
            ack_of={"signal_type": kind,
                    "signal_id": (_det.get("signal_id") or _det.get("sig_id")
                                  or _det.get("id")),
                    "path": str(relay_name),
                    "fired_by": "agent"},
            stage=("applied" if ok else "rejected"),
            source="perps_bot",
            result=("ok" if ok else ("error" if needs_owner else "rejected")),
            summary=summary,
            resulting_state={**_det, "needs_owner": bool(needs_owner)},
        )
    except Exception:
        pass


def relay_ack(relay_name: str, kind: str, ok: bool, summary: str,
              detail: dict | None = None, needs_owner: bool = False, alerted: bool = False) -> None:
    """Tell the agent what happened to the thing he put in json_relay.

    OWNER'S DOCTRINE 2026-07-27: "everything that lands into the json_relay folder
    actually needs a success or fail message back to agent so he can confirm it
    landed or fail... if success it just moves to done folder."

    Before this, perps consumed a signal, deleted the file, and on a crash sent
    notify_error to OWNER — so the agent fired a signal into silence and could not
    tell landed from exploded. He was blind on his own instructions.

      SUCCESS    -> inbox_results/done/  archived, doesn't spend his attention
      FAILURE    -> inbox/              his to correct and resend
      needs_owner -> owner_inbox/         only when it needs CODE he can't apply
    """
    # Bus first: the packet below is for the agent's eyes, this is for his LEDGER.
    # Emitting before the packet write means a packet failure cannot silently cost
    # a checkoff - which would look to the wire ledger like a signal that vanished.
    _perp_bus_ack(relay_name, kind, ok, summary, detail, needs_owner)
    try:
        if needs_owner:
            dest = os.path.join(os.path.dirname(INBOX_DIR), "owner_inbox")
            tag = "needs_code"
        elif ok:
            dest, tag = INBOX_RESULTS_DONE, "success"
        else:
            # 2026-10-16 routing rule: exactly ONE inbox item per failed signal.
            # If the bot already sent an alert for it (mirrored into inbox/), the
            # ack is archived in inbox_results/failed/; otherwise the ack IS the
            # inbox item. Successes are always archived in inbox_results/done/.
            dest, tag = ((os.path.join(os.path.dirname(INBOX_RESULTS_DONE), "failed"), "failed")
                         if alerted else (INBOX_DIR, "failed"))
        os.makedirs(dest, exist_ok=True)
        pkt = {"from": "perps_bot", "to": "agent", "type": "relay_ack",
               "relay_file": relay_name, "kind": kind, "outcome": tag,
               "ok": bool(ok), "summary": summary, "detail": detail or {},
               "at": datetime.now(EST).isoformat(),
               "doctrine": ("Ack for something you put in json_relay. success -> archived; "
                            "failed -> yours to correct and resend; needs_code -> routed to "
                            "The owner because it needs a patch, not a retry.")}
        fn = f"perps_bot - relay_ack_{kind}_{datetime.now(EST).strftime('%Y%m%d_%H%M%S')}.json"
        with open(os.path.join(dest, fn), "w", encoding="utf-8") as f:
            json.dump(pkt, f, indent=2, default=str)
        print(f"[relay_ack:{tag}] {kind} — {summary[:120]}")
    except Exception as e:
        print(f"[relay_ack] err ({kind}): {e!r}")


def _write_audit_to_inbox_results(audit_entry: dict) -> str | None:
    """Write a daily_read gating audit packet to inbox_results/done/.
    
    DOCTRINE: bots NEVER write back into inbox/. inbox_results/done/ is the
    archive the agent reads to audit the bot's gating decisions. Filename includes
    timestamp + action so the agent can sort.
    
    Returns the written file path on success, None on failure.
    """
    try:
        os.makedirs(INBOX_RESULTS_DONE, exist_ok=True)
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        # Add millisecond suffix to avoid collisions on same-second writes
        ms = f"{int((time.time() % 1) * 1000):03d}"
        action = audit_entry.get("action_requested", "unknown")
        decision = audit_entry.get("decision", "unknown")
        filename = f"perps_daily_read_audit_{ts}_{ms}_{action}_{decision}.json"
        filepath = os.path.join(INBOX_RESULTS_DONE, filename)
        
        packet = {
            "_type":          "perps_daily_read_audit",
            "_source":        "perps_bot",
            "_written_at":    datetime.now(EST).isoformat(),
            "_doctrine":      "perps_bot evaluated a signal against daily_read.json and wrote this audit packet to inbox_results/done/. The agent reads inbox_results to audit the bot's gating decisions. Bots NEVER write back to inbox/.",
            "audit":          audit_entry,
        }
        
        _ps = globals().setdefault("_PERPS_SAVE_SEQ", 0) + 1

        
        globals()["_PERPS_SAVE_SEQ"] = _ps

        
        tmp = f"{filepath}.{os.getpid()}.{_ps}.tmp"  # Sprint 54: unique tmp (shared name raced)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(packet, f, indent=2, default=str)
        os.replace(tmp, filepath)
        return filepath
    except Exception as e:
        print(f"[daily_read audit] write err: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# END SPRINT 33 DAILY_READ CONSUMPTION HELPERS
# ════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# SPRINT 39 — JUPITER FAILOVER + DECLINE FEEDBACK LOOP + VENUE ROUTING
# ═══════════════════════════════════════════════════════════════════════════
# This module wraps the existing Drift execution layer with:
#   1. Drift health tracker (empirical failure-rate)
#   2. Jupiter Perps stub adapter (STUB, see TODO markers — the owner fills in SDK)
#   3. Venue router for new positions
#   4. Position venue lock (close always routes to opening venue)
#   5. Structured skip + confirm_signal ingestion 
#   6. Decline patterns rollup
#   7. Auto-tighten + per-market auto-disable
#   8. Throughput question packet
#
# Key files written:
#   bot_logs/perps_venue_audit.jsonl       — every order attempt, which venue, result
#   bot_logs/perps_venue_health.json       — current Drift + Jupiter health state
#   bot_logs/perps_decline_log.jsonl       — every the agent skip (structured)
#   bot_logs/perps_decline_patterns.json   — rolling 7d dashboard for the owner
#   bot_logs/perps_confirm_signal_log.jsonl— positive endorsements
#   bot_logs/perps_auto_tighten_audit.jsonl— meeting directives written by bot
#
# Position state extension:
#   positions.json entries get a new `venue: "drift" | "jupiter"` field.
#   Close ALWAYS routes through opening venue. No mid-position venue swap.
# ═══════════════════════════════════════════════════════════════════════════


def _s39_append_jsonl(path: str, record: dict) -> None:
    """Append a JSON line to a jsonl audit log. Best-effort, never raises."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[s39] _append_jsonl err on {os.path.basename(path)}: {e}")


def _s39_now_iso() -> str:
    return datetime.now(EST).isoformat()


def _now_iso() -> str:
    """Bare alias used by the missed_perp autopsy code (it calls _now_iso()
    while the rest of the bot uses sprint-prefixed _s39_/_perp_ variants). Same
    EST ISO format. Was undefined -> NameError every scan ('missed_perp evaluate
    err: name _now_iso is not defined'). Single definition fixes all call sites."""
    return datetime.now(EST).isoformat()


def _s39_now_ts() -> float:
    return datetime.now(EST).timestamp()


# ─── Venue health state ─────────────────────────────────────────────────────

def _load_venue_health() -> dict:
    """Load venue health state. Default: both healthy."""
    if not os.path.exists(PERPS_VENUE_HEALTH_STATE_FILE):
        return {
            "jupiter": {
                "healthy": True,
                "last_probe_at": None,
                "last_probe_ok": True,
            },
            "updated_at": _s39_now_iso(),
        }
    try:
        with open(PERPS_VENUE_HEALTH_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        return {
            "jupiter": {"healthy": True, "last_probe_at": None, "last_probe_ok": True},
            "updated_at": _s39_now_iso(),
        }


def _save_venue_health(state: dict) -> None:
    state["updated_at"] = _s39_now_iso()
    try:
        os.makedirs(os.path.dirname(PERPS_VENUE_HEALTH_STATE_FILE), exist_ok=True)
        with open(PERPS_VENUE_HEALTH_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"[s39] _save_venue_health err: {e}")




def is_jupiter_healthy() -> bool:
    """Read current Jupiter health state."""
    state = _load_venue_health()
    return state["jupiter"].get("healthy", True)


async def probe_jupiter_perps_health() -> bool:
    """
    Real Jupiter Perps health probe. Fetches the on-chain Pool account state.
    If the fetch succeeds and the account is owned by JUPITER_PERPS_PROGRAM_ID,
    Jupiter is healthy. If the fetch fails or returns wrong owner, unhealthy.
    """
    state_path_ok = False
    try:
        client = AsyncClient(RPC_URL, commitment=Confirmed)
        pool_pk = Pubkey.from_string(JUPITER_PERPS_POOL_ACCOUNT)
        try:
            resp = await client.get_account_info(pool_pk, commitment=Confirmed)
            await client.close()
        except Exception as e:
            try:
                await client.close()
            except Exception:
                pass
            print(f"[s39] jupiter probe RPC fetch err: {e}")
            state = _load_venue_health()
            state["jupiter"]["last_probe_at"] = _s39_now_iso()
            state["jupiter"]["last_probe_ok"] = False
            state["jupiter"]["healthy"] = False
            _save_venue_health(state)
            return False

        acct = resp.value if hasattr(resp, "value") else None
        if acct is None:
            print(f"[s39] jupiter probe: pool account not found")
            state = _load_venue_health()
            state["jupiter"]["last_probe_at"] = _s39_now_iso()
            state["jupiter"]["last_probe_ok"] = False
            state["jupiter"]["healthy"] = False
            _save_venue_health(state)
            return False

        # Confirm owner matches program id
        owner_str = str(acct.owner)
        if owner_str != JUPITER_PERPS_PROGRAM_ID:
            print(f"[s39] jupiter probe: pool owner mismatch (got {owner_str})")
            state = _load_venue_health()
            state["jupiter"]["last_probe_at"] = _s39_now_iso()
            state["jupiter"]["last_probe_ok"] = False
            state["jupiter"]["healthy"] = False
            _save_venue_health(state)
            return False

        state = _load_venue_health()
        state["jupiter"]["last_probe_at"] = _s39_now_iso()
        state["jupiter"]["last_probe_ok"] = True
        state["jupiter"]["healthy"] = True
        _save_venue_health(state)
        return True
    except Exception as e:
        print(f"[s39] jupiter probe outer err: {e}")
        try:
            state = _load_venue_health()
            state["jupiter"]["last_probe_at"] = _s39_now_iso()
            state["jupiter"]["last_probe_ok"] = False
            state["jupiter"]["healthy"] = False
            _save_venue_health(state)
        except Exception:
            pass
        return False


# ─── Jupiter Perps adapter (Sprint 39 Option A — REAL execution) ────────────
#
# This implements the request-based path that works without Jupiter API access:
#
#   1. Bot signs + submits a PositionRequest on-chain via createIncreasePositionMarketRequest
#      (open) or createDecreasePositionRequest2 (close).
#   2. Jupiter's off-chain keepers monitor for new PositionRequest accounts,
#      validate, and execute the actual position change. Typically 5-30s.
#   3. Bot polls for execution status by re-reading the Position account.
#
# Key architectural choices:
#   - We use anchorpy.Program with the on-chain Anchor IDL fetched at startup.
#     This handles instruction encoding + account discriminators correctly.
#   - Fallback path: if IDL fetch fails (e.g. transient RPC issue), we fall
#     back to manually constructing the instruction with hand-coded
#     discriminators. The discriminator is sha256("global:{instr_name}")[:8].
#   - PDA derivations follow the Jupiter on-chain program seeds (verified
#     against the live Position account at example FBLzd5VM67MEKkoWerXu7Nu1...).
#   - TP/SL: Jupiter requires Jupiter API co-signing for on-chain TP/SL via
#     instantCreateTpsl. We can't do that without API partnership. Workaround:
#     bot-side TP/SL monitoring — we save tp_price/sl_price into positions.json
#     and the existing monitor_position loop closes when price crosses.

_JUPITER_PROGRAM_CACHED = None  # Cached anchorpy.Program instance
_JUPITER_IDL_CACHED = None       # Cached IDL — an anchorpy Idl OBJECT, not a dict
                                 # (assuming dict here is what caused Custom(101))


async def _get_jupiter_program(rpc_client: AsyncClient = None):
    """Lazy-initialize the anchorpy Program for Jupiter Perps. Fetches IDL
    from chain on first call. Returns None if anchorpy.Program is unavailable
    or IDL fetch fails."""
    global _JUPITER_PROGRAM_CACHED, _JUPITER_IDL_CACHED

    # ── FIELD FIX (2026-07-20 live log: "[s56b][pool] load failed: Cannot send
    # a request, as the client has been closed" ×3 per scan, forever): the
    # Program was cached GLOBALLY with the first caller's client baked into its
    # provider — and both this function (own_client path) and the pool loader
    # close their own clients when done. One cache + any close = every later
    # caller fetches through a corpse, even when they pass a fresh client.
    # New shape: cache the IDL (the expensive on-chain fetch); rebuild the
    # Program per call, bound to the CALLER'S live client. Programs are cheap
    # once the IDL is cached. _JUPITER_PROGRAM_CACHED is retired.
    if not _ANCHORPY_PROGRAM_AVAILABLE:
        print("[s39][jupiter] anchorpy.Program not available — falling back to manual ix encoding")
        return None

    own_client = False
    if rpc_client is None:
        rpc_client = AsyncClient(RPC_URL, commitment=Confirmed)
        own_client = True

    try:
        keypair = Keypair.from_base58_string(PRIVATE_KEY)
        wallet = Wallet(keypair)
        provider = Provider(rpc_client, wallet)
        program_id = Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID)

        # IDL: serve from cache when we have it; fetch (chain, then fallback
        # file) only on the first call. The introspection logging below runs
        # only on a real fetch, so boots still print the instruction names once.
        idl_dict = _JUPITER_IDL_CACHED
        _idl_was_cached = idl_dict is not None
        if idl_dict is None:
            try:
                idl_dict = await Program.fetch_idl(program_id, provider)
            except Exception as e:
                print(f"[s39][jupiter] on-chain IDL fetch failed: {e}")
            # Try local fallback file
            try:
                with open(JUPITER_PERPS_IDL_FALLBACK_PATH, "r", encoding="utf-8") as f:
                    idl_dict = json.load(f)
                print(f"[s39][jupiter] loaded IDL from fallback path")
            except Exception as fe:
                print(f"[s39][jupiter] fallback IDL load failed: {fe}")
                idl_dict = None

        if idl_dict is None:
            if own_client:
                try:
                    await rpc_client.close()
                except Exception:
                    pass
            return None

        # ── ROOT FIX (2026-07-21, proven by the raw-discriminator diagnostic:
        # chain said f19a6d0411b16dbc = sha256("account:Pool"), and the ONLY
        # names the fallback IDL declares are lowercase — every derived
        # discriminator wrong by construction): the on-chain program is CLASSIC
        # Anchor (PascalCase struct names) while the fallback IDL is new-format
        # (lowercase names + explicit discriminator bytes old anchorpy ignores).
        # Normalize account names to PascalCase at load — map keys become
        # Pool/Custody/Position and name-derived discriminators match the chain
        # exactly. Layouts untouched; only name (key + discriminator) changes.
        try:
            _renamed = []
            for _acc in (idl_dict.get("accounts") or []):
                _an = _acc.get("name")
                if _an and _an[0].islower():
                    _acc["name"] = _an[0].upper() + _an[1:]
                    _renamed.append(f"{_an}->{_acc['name']}")
            if _renamed and not _idl_was_cached:
                print(f"[s39][jupiter] IDL account names normalized to classic-Anchor "
                      f"PascalCase: {', '.join(_renamed)}")
        except Exception as _ne:
            print(f"[s39][jupiter] account-name normalization err: {_ne}")

        _JUPITER_IDL_CACHED = idl_dict
        # Sprint 55: log the instruction names AT FETCH, every boot. The whole Custom(101)
        # saga cost days because we never actually LOOKED at what the program exposes —
        # and when we finally tried, the diagnostic had the same dict-vs-object bug and
        # printed an empty list. Print it once, up front, and this class of bug can never
        # hide again.
        if not _idl_was_cached:   # introspection prints once per real IDL fetch, not per call
            try:
                _ix_names = _idl_instruction_names(idl_dict)
                print(f"[s39][jupiter] IDL loaded — {len(_ix_names)} instructions: {_ix_names}")
                _resolved = _resolve_jupiter_increase_ix_name(idl_dict)
                print(f"[s39][jupiter] increase-position instruction resolved to: {_resolved!r}"
                      if _resolved else
                      "[s39][jupiter] ⚠ COULD NOT RESOLVE the increase-position instruction from "
                      "the names above. Opens will ABORT (safely). Add the correct name to "
                      "_JUPITER_INCREASE_IX_CANDIDATES.")
            except Exception as _ie:
                print(f"[s39][jupiter] idl name introspection failed: {_ie}")
        try:
            idl = Idl.from_json(json.dumps(idl_dict)) if isinstance(idl_dict, dict) else Idl.from_json(idl_dict)
        except Exception:
            # Some anchorpy versions accept dict directly
            try:
                idl = Idl(**idl_dict) if isinstance(idl_dict, dict) else idl_dict
            except Exception as e:
                print(f"[s39][jupiter] IDL parse failed: {e}")
                return None

        program = Program(idl, program_id, provider)
        if not _idl_was_cached:
            print(f"[s39][jupiter] anchorpy Program initialized OK (per-call build; IDL cached)")
        return program
    except Exception as e:
        print(f"[s39][jupiter] program init outer err: {e}")
        traceback.print_exc()
        if own_client:
            try:
                await rpc_client.close()
            except Exception:
                pass
        return None


async def _fetch_account_resilient(program, preferred_name: str, pubkey):
    """FIELD FIX (2026-07-21 log: "invalid discriminator" after the resolver
    matched a case-variant): anchorpy derives an account's expected discriminator
    FROM THE NAME IT WAS GIVEN — sha256("account:pool") can never equal the
    chain's sha256("account:Pool"). New-format (anchor 0.30) IDLs use lowercase
    names with EXPLICIT discriminator bytes that old anchorpy ignores, so a
    case-variant match decodes against the wrong 8 bytes. Ladder:
      1) try the preferred/resolved client;
      2) on a discriminator error, try EVERY account client the IDL declares —
         if any decodes, use it and log which (self-solving);
      3) if none decode, fetch the RAW account and print the chain's actual
         first-8 bytes next to every candidate's expected bytes — the next log
         is the answer, not a mystery.
    Returns the decoded account or None."""
    import hashlib as _hl
    _client = _resolve_account_client(program, preferred_name)
    _last_err = None
    if _client is not None:
        try:
            return await _client.fetch(pubkey)
        except Exception as e:
            _last_err = e
            if "discriminator" not in str(e).lower():
                raise
    # rung 2: try everything the IDL declares
    try:
        _accounts = program.account
        _names = list(_accounts.keys()) if hasattr(_accounts, "keys") else                  [a for a in dir(_accounts) if not a.startswith("_")]
    except Exception:
        _names = []
    for _n in _names:
        try:
            _c = _accounts[_n] if hasattr(_accounts, "keys") else getattr(_accounts, _n)
            _acct = await _c.fetch(pubkey)
            print(f"[s56b][pool] account decoded as IDL type '{_n}' "
                  f"(preferred '{preferred_name}' had a discriminator mismatch — "
                  f"consider renaming in the fallback IDL)")
            return _acct
        except Exception:
            continue
    # rung 3: raw diagnostic — print the truth
    try:
        _conn = getattr(getattr(program, "provider", None), "connection", None)
        if _conn is not None:
            _info = await _conn.get_account_info(pubkey)
            _val = getattr(_info, "value", None) or (_info.get("result", {}).get("value") if isinstance(_info, dict) else None)
            _data = getattr(_val, "data", None) if _val is not None else None
            if isinstance(_data, (list, tuple)):
                import base64 as _b64
                _data = _b64.b64decode(_data[0])
            if _data:
                _chain8 = bytes(_data[:8]).hex()
                _cands = {n: _hl.sha256(f"account:{n}".encode()).digest()[:8].hex() for n in _names}
                _cands[preferred_name] = _hl.sha256(f"account:{preferred_name}".encode()).digest()[:8].hex()
                print(f"[s56b][pool] RAW DISCRIMINATOR DIAGNOSTIC — chain says {_chain8}; "
                      f"candidates: {_cands}. The matching name is the correct account "
                      f"type for the fallback IDL.")
    except Exception as _de:
        print(f"[s56b][pool] raw diagnostic failed: {_de}")
    if _last_err:
        print(f"[s56b][pool] all account decodes failed (last: {_last_err})")
    return None


def _resolve_account_client(program, name: str):
    """FIELD FIX (2026-07-20 log: "[s56b][pool] load failed: 'Pool'"): the
    fallback IDL's account names don't match our hardcoded key — same disease
    as the Custom(101) instruction-name saga, account edition. anchorpy keys
    program.account by whatever the IDL declares (casing varies across anchor
    IDL versions/formats). Try the obvious variants; on total miss, print the
    ACTUAL available keys once so the next log tells the truth instead of a
    bare KeyError. Returns the account client or None."""
    try:
        accounts = program.account
    except Exception:
        return None
    for cand in (name, name.lower(), name.upper(),
                 name[:1].lower() + name[1:], name[:1].upper() + name[1:]):
        try:
            if cand in accounts:
                return accounts[cand]
        except TypeError:
            # some anchorpy versions expose attribute-style access only
            client = getattr(accounts, cand, None)
            if client is not None:
                return client
    try:
        _keys = list(accounts.keys()) if hasattr(accounts, "keys") else [a for a in dir(accounts) if not a.startswith("_")]
    except Exception:
        _keys = ["<unreadable>"]
    print(f"[s56b][pool] account '{name}' not in program.account — available: {_keys}")
    return None


def _anchor_instr_discriminator(instr_name: str) -> bytes:
    """Anchor instruction discriminator: first 8 bytes of sha256('global:<name>').

    2026-07-21 (same classic-vs-new-format lesson as the account fix): classic
    Anchor derives instruction discriminators from the SNAKE_CASE method name,
    while IDLs list camelCase. sha256("global:createIncreasePositionMarketRequest")
    would never match the chain; convert to snake first so manual instruction
    builds hit the real discriminator."""
    import re as _re
    _snake = _re.sub(r"(?<!^)(?=[A-Z])", "_", instr_name).lower()
    return hashlib.sha256(f"global:{_snake}".encode()).digest()[:8]

def _camel_to_snake(name: str) -> str:
    """createIncreasePositionMarketRequest -> create_increase_position_market_request

    THE camelCase/snake_case SPLIT -- the single root cause of this whole saga:
      * The IDL stores instruction names in camelCase (JS/TS ergonomics).
      * Anchor's REAL identifiers are snake_case: the Rust fn name, the
        discriminator preimage, AND anchorpy's `program.methods` dict keys.

    Hash the camelCase name       -> wrong discriminator -> Custom(101) / 0x65.
    Look it up in program.methods -> KeyError.

    Same root cause, two symptoms. We hit BOTH. Convert, always.
    """
    out = []
    for i, ch in enumerate(name):
        if ch.isupper():
            if i > 0:
                out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _resolve_program_method(program, ix_name: str):
    """Get a method builder from anchorpy's `program.methods`, tolerating whichever
    casing the installed anchorpy keys it under.

    Raises KeyError LISTING THE REAL KEYS if neither works -- so a future anchorpy
    change gives a readable error instead of a bare KeyError on a name that IS
    plainly in the IDL. (Which is exactly what bit us: the resolver correctly found
    'createIncreasePositionMarketRequest' in the IDL, PRINTED it, and then the dict
    lookup KeyError'd on that same name -- because anchorpy keys it snake_case.)
    """
    snake = _camel_to_snake(ix_name)
    for key in (snake, ix_name):
        try:
            return program.methods[key]
        except (KeyError, AttributeError):
            continue
    try:
        available = sorted(program.methods.keys())
    except Exception:
        available = ["<could not enumerate program.methods>"]
    raise KeyError(
        f"neither {snake!r} nor {ix_name!r} in program.methods. "
        f"Available ({len(available)}): {available}"
    )


# Sprint 52 fix: the open-position failure (Custom(101) / InstructionFallbackNotFound)
# was caused by a hardcoded instruction name that no longer matches the on-chain
# Jupiter Perps IDL. program.methods["createIncreasePositionMarketRequest"] raised
# KeyError, fell through to manual encoding, which computed a discriminator from the
# SAME wrong name -> the program saw an unknown discriminator -> 0x65. The real fix:
# resolve the actual instruction name from the loaded IDL, and if it can't be
# resolved, ABORT rather than submitting a garbage transaction to the chain.
_JUPITER_INCREASE_IX_CANDIDATES = [
    # known/observed names across Jupiter Perps program versions; first match wins
    "createIncreasePositionMarketRequest",
    "createIncreasePositionRequest",
    "increasePositionMarketRequest",
    "createIncreasePosition",
    "instantCreateIncreasePositionRequest",
    "instantIncreasePosition",
    "createMarketIncreaseRequest",
    "openPositionRequest",
    "createOpenPositionRequest",
    "increasePosition",
]

def _idl_instruction_names(idl) -> list:
    """Extract instruction names from an IDL, whatever shape it arrives in.

    ── Sprint 55 BUGFIX: THE RESOLVER WAS PARSING THE WRONG TYPE ────────────────────
    The old code did:

        names = [ix.get("name") for ix in (idl or {}).get("instructions", [])]

    But `anchorpy.Program.fetch_idl()` returns an **anchorpy_core.idl.Idl OBJECT**, not a
    dict. Calling `.get()` on it raises AttributeError, which the bare `except` swallowed
    — so the resolver returned None and we aborted, EVERY TIME.

    Which means the abort message ("could not resolve the increase-position instruction
    name") may have been a LIE. It wasn't that the name was missing from the IDL. It's
    that we never managed to READ the IDL at all. We were staring at an unopened envelope
    and concluding the letter was blank.

    (Confirmed by the owner's own diagnostic run: `TypeError: 'anchorpy_core.idl.Idl' object
    is not subscriptable`.)

    Handle both: the object form (idl.instructions[].name) and the dict form (raw JSON,
    which is what the fallback file cache would give us).
    """
    if not idl:
        return []
    # object form (anchorpy_core.idl.Idl)
    try:
        ixs = getattr(idl, "instructions", None)
        if ixs:
            out = []
            for ix in ixs:
                nm = getattr(ix, "name", None)
                if nm:
                    out.append(str(nm))
            if out:
                return out
    except Exception:
        pass
    # dict form (raw JSON from a cached idl file)
    try:
        if isinstance(idl, dict):
            return [ix.get("name") for ix in (idl.get("instructions") or [])
                    if isinstance(ix, dict) and ix.get("name")]
    except Exception:
        pass
    return []


def _resolve_jupiter_increase_ix_name(idl) -> "str | None":
    """Return the real 'increase/open position' instruction name from the IDL, or None.
    Matches known candidates first, then falls back to fuzzy matching. None means we
    could not resolve it — callers MUST abort rather than guess a name (a wrong name =
    wrong discriminator = the Custom(101) InstructionFallbackNotFound we're fixing)."""
    names = _idl_instruction_names(idl)
    if not names:
        return None
    for cand in _JUPITER_INCREASE_IX_CANDIDATES:
        if cand in names:
            return cand
    # fuzzy pass 1: increase + position
    for nm in names:
        low = nm.lower()
        if "increase" in low and "position" in low:
            return nm
    # fuzzy pass 2: any "increase" request (Jupiter may drop 'position' from the name)
    for nm in names:
        low = nm.lower()
        if "increase" in low and "request" in low:
            return nm
    # fuzzy pass 3: open-position style naming
    for nm in names:
        low = nm.lower()
        if "open" in low and "position" in low:
            return nm
    return None


def _derive_position_pda(
    owner: Pubkey, pool: Pubkey, custody: Pubkey,
    collateral_custody: Pubkey, side: int,
) -> Pubkey:
    """
    Position PDA derivation.
    Seeds: ['position', owner, pool, custody, collateral_custody, side_byte]
    side_byte: 1 = Long, 2 = Short
    """
    side_byte = bytes([side])
    seeds = [
        b"position",
        bytes(owner),
        bytes(pool),
        bytes(custody),
        bytes(collateral_custody),
        side_byte,
    ]
    program_id = Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID)
    pda, _bump = Pubkey.find_program_address(seeds, program_id)
    return pda


# PositionRequest request_change discriminant -- the trailing seed byte.
# 1 = Increase (opening / adding size), 2 = Decrease (closing / reducing).
JUPITER_REQUEST_CHANGE_INCREASE = 1
JUPITER_REQUEST_CHANGE_DECREASE = 2


def _derive_position_request_pda(position: Pubkey, counter: int,
                                 request_change: int = JUPITER_REQUEST_CHANGE_INCREASE) -> Pubkey:
    """
    PositionRequest PDA.

    Seeds: ['position_request', position, counter_le_u64, request_change_u8]

    -- THE Custom(2006) / ConstraintSeeds FIX ----------------------------------
    The old derivation omitted the trailing `request_change` byte:

        ['position_request', position, counter_le_u64]      <- WRONG, one seed short

    So the program re-derived a DIFFERENT PositionRequest PDA than the one we
    passed, and threw ConstraintSeeds (Left = ours, Right = theirs).

    Note this was the SECOND half of a two-part bug on the same account. Part one
    was failing to SEND `counter` in the params at all -- fixing that is what let
    the program get far enough to derive its own PDA and tell us they disagreed.
    Same account, same error code, two independent causes.

    Recovered by brute force, not by guessing: with (counter, Left, Right) captured
    from three real failed simulations, exactly ONE layout in the entire search
    space reproduces all three `Right` values. This is it.

    request_change:
      1 = Increase  -- opening a position or adding size
      2 = Decrease  -- closing a position or reducing size
    """
    seeds = [
        b"position_request",
        bytes(position),
        counter.to_bytes(8, "little"),
        bytes([request_change]),
    ]
    program_id = Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID)
    pda, _bump = Pubkey.find_program_address(seeds, program_id)
    return pda


def _derive_associated_token_account(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Standard SPL Associated Token Account derivation."""
    seeds = [bytes(owner), bytes(Pubkey.from_string(SOLANA_TOKEN_PROGRAM)), bytes(mint)]
    ata_program = Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROG)
    pda, _bump = Pubkey.find_program_address(seeds, ata_program)
    return pda


def _market_to_jupiter_asset(market: str) -> str:
    """Map a bot market label to the Jupiter asset key."""
    if market.startswith("SOL"):
        return "SOL"
    if market.startswith("ETH"):
        return "ETH"
    if market.startswith("BTC"):
        return "BTC"
    raise ValueError(f"market {market} not supported by Jupiter Perps (only SOL/ETH/BTC)")


# ─────────────────────────────────────────────────────────────────────────────
# SPRINT 56 — JUPITER OPEN PATH, REWRITTEN CLEAN
#
# The Custom(101)/Custom(2006) saga is over. Root causes, all confirmed against
# the live on-chain IDL (dumped 2026-07-14):
#
#   1. DISCRIMINATOR. The manual encoder hashed the IDL's camelCase name
#      ("createIncreasePositionMarketRequest"). Anchor's discriminator is
#      sha256("global:create_increase_position_market_request")[:8] — SNAKE_CASE,
#      always. The IDL stores camelCase for JS ergonomics; the Rust fn name is
#      what gets hashed. You can NEVER get a valid discriminator by hashing an
#      IDL name directly. That was the 0x65 / InstructionFallbackNotFound.
#
#   2. MISSING `counter`. The params struct's 6th field is `counter: u64`. We
#      generated it, seeded the positionRequest PDA with it, and then never sent
#      it. So the program re-derived a DIFFERENT positionRequest PDA than the one
#      we passed → ConstraintSeeds violation → Custom(2006). That was the
#      "different error" we mistook for progress.
#
#   3. FIVE MISSING ACCOUNTS. On-chain ix takes 16. We sent 11. Missing:
#      positionRequestAta, referral (optional), associatedTokenProgram,
#      eventAuthority, program.
#
#   4. `pool` WRITABLE. On-chain it is READONLY. We marked it mut.
#
#   5. THE META-BUG: a bare `except` around the anchorpy builder set
#      `program = None` and fell through to the hand-rolled encoder. So a typo in
#      an account key name silently became a garbage transaction submitted to
#      mainnet. That is why every fix moved the error sideways instead of
#      forward — we were never debugging the encoder we thought we were.
#
#   6. ETH/BTC PRICING. priceSlippage and collateral conversion both used
#      sol_price for EVERY market. The code literally said "placeholder".
#
#   7. ETH/BTC DECIMALS. Wormhole ETH and wBTC on Solana are BOTH 8 decimals.
#      The placeholder math used 1e9 for ETH. Off by 10x.
#
# THE FIX: ONE ENCODER. anchorpy only. No manual fallback — anchorpy computes the
# discriminator from the IDL correctly by construction, Borsh-encodes params from
# the IDL's own type defs, and cannot get either wrong. If it fails to build, we
# ABORT. We do not guess and we do not submit.
# ─────────────────────────────────────────────────────────────────────────────

# Token decimals. Wormhole ETH and wBTC on Solana are BOTH 8 decimals — NOT 18/9.
# The old placeholder math divided ETH by 1e9 (a 10x error) and BTC by
# sol_price*1000 (a guess). Both are gone.
JUPITER_TOKEN_DECIMALS = {
    "SOL":  9,
    "ETH":  8,   # Wormhole ETH (7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs)
    "BTC":  8,   # Wormhole wBTC (3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh)
    "USDC": 6,
    "USDT": 6,
}

# Jupiter's internal price representation is 6 decimals.
JUPITER_PRICE_DECIMALS = 6

# Doves oracle price accounts are small: [price: i64][expo: i32][conf: u64][ts: i64]
# after the 8-byte Anchor account discriminator. We read price + expo and rebuild
# a float. This is THE SAME oracle the Jupiter program itself validates against —
# which makes it strictly more correct than cross-quoting Drift's oracle at
# Jupiter (what the old _market_mark_price did, and the reason ETH/BTC could not
# survive Drift's removal).
_JUPITER_ORACLE_CACHE = {}   # asset -> (price_float, fetched_at_monotonic)
_JUPITER_ORACLE_TTL_S = 5.0


async def _jupiter_oracle_price(rpc_client: AsyncClient, asset: str) -> "float | None":
    """Spot price for an asset, from the SAME Pyth oracle the Jupiter program validates
    against. Resolves the oracle pubkey from the live Custody account.

    -- WHY THIS FUNCTION WAS REWRITTEN -------------------------------------------
    The first version read the DOVES oracle from a hardcoded JUPITER_ORACLES map.
    That feed is FROZEN: the identical raw value (6,782,862,018 -> $67.83) came back
    across 51 slots and never moved. It reported SOL ~12% below spot, uniformly,
    across all three assets -- which is the tell. A wrong byte offset gives you
    garbage; a *uniformly* low, correctly-scaled price means you decoded a real
    field of a dead account.

    We only got away with it because a LONG's priceSlippage is a CEILING -- a low
    mark is merely conservative, so the $77 fill sailed through a $67.83 bound. On a
    SHORT that same bound is a FLOOR. A 12%-low floor means accepting a fill 12%
    below market. That was a live loss waiting on the next short signal.

    The Custody account told us the truth: oracle_type = Pyth, and it carries
    max_price_age_sec = 5 -- so the program itself would reject the stale Doves price.
    We now read the Pyth PriceUpdateV2 account the custody actually points at, and we
    enforce a staleness check, because a stale price is exactly the failure we just
    spent hours chasing.
    """
    return await _jupiter_mark_price(rpc_client, asset)


def _derive_event_authority() -> Pubkey:
    """Anchor's standard event-authority PDA. Seeds: ['__event_authority'].
    Required account #15 on createIncreasePositionMarketRequest — we were never
    sending it."""
    pda, _bump = Pubkey.find_program_address(
        [b"__event_authority"],
        Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID),
    )
    return pda


def _usd_to_token_units(usd_amount: float, price_usd: float, asset: str) -> int:
    """Convert a USD amount to native token units using the asset's REAL decimals.

    Replaces _usd_to_lamports_collateral's SOL-only assumption and the two
    'placeholder' branches that divided ETH by 1e9 and BTC by sol_price*1000.
    """
    if price_usd is None or price_usd <= 0:
        raise ValueError(f"cannot size {asset} collateral: bad price {price_usd}")
    dec = JUPITER_TOKEN_DECIMALS.get((asset or "").upper())
    if dec is None:
        raise ValueError(f"no decimals mapping for asset {asset}")
    return int((usd_amount / price_usd) * (10 ** dec))


def _jupiter_slippage_price(mark_price: float, direction: str, slippage_bps: int) -> int:
    """priceSlippage: the worst price we'll accept, in Jupiter's 6-dec units.

    Long  → we're buying  → cap the max price we'll pay.
    Short → we're selling → floor the min price we'll accept.

    Computed off the MARKET'S OWN oracle (not SOL's). This is the field the old
    code fed sol_price for every market — the reason an ETH open priced its
    slippage bound off SOL and would have filled somewhere insane.
    """
    if direction == "long":
        bound = mark_price * (1.0 + slippage_bps / 10_000.0)
    else:
        bound = mark_price * (1.0 - slippage_bps / 10_000.0)
    return int(bound * (10 ** JUPITER_PRICE_DECIMALS))


# ─────────────────────────────────────────────────────────────────────────────
# SPRINT 56b — STOP HARDCODING THE POOL'S GUTS. READ THEM.
#
# The hardcoded JUPITER_CUSTODIES / JUPITER_ORACLES / JUPITER_TOKEN_DECIMALS maps
# were wrong in three places, and one of them was fatal:
#
#   * BTC custody 5Pv6wMkqgdLnxBMyAd8s1eBnSGc4mtJoFAjVjmyf6vbn DOES NOT EXIST
#     on-chain. Any BTC open would have failed no matter what else we fixed.
#     The live one is 5Pv3gM9JrFFH883SWAhvJC9RPYmo8UNxuFtv5bMMALkm.
#   * USDT custody was likewise stale.
#   * The ORACLES map pointed at DOVES accounts. Jupiter prices off PYTH.
#     The Doves feed we were reading is FROZEN — the identical raw value came back
#     across 51 slots. It also has max_price_age_sec=5, so even if the program HAD
#     read it, a stale price would be rejected. We were pricing off a dead feed and
#     only got away with it because a long's slippage bound is a CEILING (a low mark
#     is merely conservative). On a SHORT the same bound is a FLOOR — a 12%-low floor
#     means accepting a fill 12% under market. That was a live loss waiting to happen.
#
# THE FIX: the Pool account lists its own custodies. Each Custody names its own
# mint, decimals, and oracle. So we ask the chain instead of guessing. If Jupiter
# migrates a custody again, we follow automatically instead of silently breaking.
# ─────────────────────────────────────────────────────────────────────────────

_JUP_POOL_CACHE = None          # {asset: {custody, mint, decimals, oracle, is_stable}}
_JUP_POOL_CACHE_AT = 0.0
_JUP_POOL_CACHE_TTL_S = 3600.0  # re-read hourly; custodies change rarely but DO change


# Trading halt when the live Pool/Custody read fails. No fallback to the old hardcoded
# Doves oracle map (Jupiter moved to dovesAgOracle; the old feed froze): new orders are
# refused and the owner is alerted. The monitor keeps running on whatever it can price.
_POOL_HALT = {"reason": None, "since": None, "alerted_at": 0.0}
POOL_HALT_ALERT_EVERY_S = 600


def _pool_halt_set(reason: str) -> None:
    if not _POOL_HALT["reason"]:
        _POOL_HALT["since"] = time.time()
        print(f"[pool halt] TRADING HALTED: {reason}")
    _POOL_HALT["reason"] = reason


def _pool_halt_clear() -> None:
    if _POOL_HALT["reason"]:
        print("[pool halt] live pool read recovered; trading resumes")
    _POOL_HALT.update({"reason": None, "since": None, "alerted_at": 0.0})


def perps_trading_halted():
    """Reason string while new orders are halted, else None."""
    return _POOL_HALT["reason"]


async def _load_jupiter_pool_config(rpc_client: AsyncClient = None, force: bool = False) -> dict:
    """Live pool config, or {} on failure. Sets/clears the trading halt."""
    try:
        cfg = await _load_jupiter_pool_config_raw(rpc_client, force)
    except Exception as e:
        print(f"[pool halt] pool read raised {e!r}")
        cfg = {}
    if cfg:
        _pool_halt_clear()
    else:
        _pool_halt_set("live Jupiter Pool/Custody read failed")
    return cfg or {}


async def _pool_halt_alert(market: str) -> None:
    """Rate-limited owner alert + inbox packet for a refused order during a halt."""
    now = time.time()
    if now - float(_POOL_HALT.get("alerted_at") or 0) < POOL_HALT_ALERT_EVERY_S:
        return
    _POOL_HALT["alerted_at"] = now
    msg = (f"\u26d4 <b>Trading halted - {market} order refused</b>\n"
           f"{perps_trading_halted() or 'live pool read failed'}. The bot will not fall back to the old "
           f"oracle map. Existing positions stay watched; new orders resume when the read recovers.")
    try:
        os.makedirs(INBOX_DIR, exist_ok=True)
        with open(os.path.join(INBOX_DIR, f"perps_bot - trading_halted_{datetime.now(EST).strftime('%H%M%S')}.json"),
                  "w", encoding="utf-8") as _f:
            json.dump({"from": "perps_bot", "type": "trading_halted", "market": market,
                       "reason": perps_trading_halted(), "since": _POOL_HALT.get("since"),
                       "generated_at": datetime.now(EST).isoformat()}, _f, indent=2, default=str)
    except Exception as _e:
        print(f"[pool halt] packet err {_e!r}")
    try:
        await notify(msg)
    except Exception:
        pass


async def _load_jupiter_pool_config_raw(rpc_client: AsyncClient = None, force: bool = False) -> dict:
    """Read the live custody/oracle/decimals map from the Pool account.

    Returns {asset: {custody, mint, decimals, oracle, is_stable}} keyed by
    SOL / ETH / BTC / USDC / USDT (mapped from each custody's mint).

    This REPLACES the hardcoded JUPITER_CUSTODIES / JUPITER_ORACLES /
    JUPITER_TOKEN_DECIMALS maps, which had drifted out of date and would have
    failed every BTC trade. Returns {} on failure — callers MUST abort rather than
    fall back to the stale constants.
    """
    global _JUP_POOL_CACHE, _JUP_POOL_CACHE_AT

    if not force and _JUP_POOL_CACHE and (time.time() - _JUP_POOL_CACHE_AT) < _JUP_POOL_CACHE_TTL_S:
        return _JUP_POOL_CACHE

    own = False
    if rpc_client is None:
        rpc_client = AsyncClient(RPC_URL, commitment=Confirmed)
        own = True

    try:
        program = await _get_jupiter_program(rpc_client)
        if program is None:
            print("[s56b][pool] anchorpy Program unavailable — cannot read pool config")
            return _JUP_POOL_CACHE or {}

        pool = await _fetch_account_resilient(program, "Pool", Pubkey.from_string(JUPITER_PERPS_POOL_ACCOUNT))
        if pool is None:
            print("[s56b][pool] Pool account undecodable with current IDL — aborting load (see diagnostic above)")
            return _JUP_POOL_CACHE or {}
        custodies = list(getattr(pool, "custodies", []) or [])
        if not custodies:
            print("[s56b][pool] pool.custodies empty")
            return _JUP_POOL_CACHE or {}

        # map mint -> asset label
        MINT_TO_ASSET = {
            "So11111111111111111111111111111111111111112":  "SOL",
            "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs":  "ETH",
            "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh":  "BTC",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v":  "USDC",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB":  "USDT",
        }

        out = {}
        for cpk in custodies:
            try:
                cust = await _fetch_account_resilient(program, "Custody", cpk)
                if cust is None:
                    continue
            except Exception as e:
                print(f"[s56b][pool] custody {cpk} unreadable: {e}")
                continue
            mint = str(getattr(cust, "mint", ""))
            asset = MINT_TO_ASSET.get(mint)
            if not asset:
                continue  # e.g. the JupUSD custody — not a market we trade
            orc = getattr(cust, "oracle", None)
            oracle_acct = getattr(orc, "oracle_account", None) if orc else None
            out[asset] = {
                "custody":   cpk,
                "mint":      Pubkey.from_string(mint),
                "decimals":  int(getattr(cust, "decimals", 0)),
                "oracle":    oracle_acct,
                "is_stable": bool(getattr(cust, "is_stable", False)),
            }

        if not out:
            print("[s56b][pool] resolved no known assets from pool custodies")
            return _JUP_POOL_CACHE or {}

        _JUP_POOL_CACHE = out
        _JUP_POOL_CACHE_AT = time.time()
        # The owner (2026-07-23): "we only need btc, eth and sol." Correct for TRADING —
        # but USDC/USDT custodies are the COLLATERAL accounts for SHORT positions
        # on Jupiter (longs collateralize in the token custody, shorts in stables).
        # They stay in the config so the first short open doesn't die hunting its
        # collateral account; the log now says which is which instead of implying
        # five tradeable markets. Role tagged on each entry for downstream clarity.
        _TRADEABLE = ("BTC", "ETH", "SOL")
        for a in out:
            out[a]["role"] = "tradeable" if a in _TRADEABLE else "short_collateral"
        _trade = [a for a in sorted(out) if a in _TRADEABLE]
        _coll  = [a for a in sorted(out) if a not in _TRADEABLE]
        print(f"[s56b][pool] live config loaded — tradeable {_trade}:")
        for a in _trade:
            v = out[a]
            print(f"    {a:5s} custody={v['custody']} dec={v['decimals']} oracle={v['oracle']}")
        if _coll:
            print(f"[s56b][pool] + {_coll} kept as short-collateral custodies (not markets)")
        return out

    except Exception as e:
        print(f"[s56b][pool] load failed: {e}")
        return _JUP_POOL_CACHE or {}
    finally:
        if own:
            try:
                await rpc_client.close()
            except Exception:
                pass


async def _pyth_price(rpc_client: AsyncClient, oracle_pk: Pubkey, asset: str) -> "float | None":
    """Read a Pyth price-update account (the pull-oracle format Jupiter uses).

    Layout of PriceUpdateV2:
        [0:8]   anchor discriminator
        [8:40]  write_authority (pubkey)
        [40]    verification_level (enum)
        [41:73] price_message.feed_id (32 bytes)
        [73:81] price          : i64     <-- these two offsets are why the Doves
        [81:89] conf           : u64         decode "worked": the DOVES account
        [89:93] exponent       : i32         happens to have a plausible number at
        [93:101] publish_time  : i64         73 as well. Coincidence, not correctness.
    """
    try:
        resp = await rpc_client.get_account_info(oracle_pk, commitment=Confirmed)
        acct = getattr(resp, "value", None)
        if acct is None:
            print(f"[s56b][pyth] {asset} oracle account not found: {oracle_pk}")
            return None
        raw = acct.data
        if isinstance(raw, (list, tuple)):
            raw = raw[0]
        if isinstance(raw, str):
            raw = _b64.b64decode(raw)   # fix 2026-07-26: module is imported as _b64;
                                        # bare `base64` was an undefined name that would
                                        # crash the Pyth decode whenever the RPC returned
                                        # string-encoded account data.
        raw = bytes(raw)
        if len(raw) < 101:
            print(f"[s56b][pyth] {asset} data too short: {len(raw)}B")
            return None

        price_raw    = int.from_bytes(raw[73:81],  "little", signed=True)
        expo         = int.from_bytes(raw[89:93],  "little", signed=True)
        publish_time = int.from_bytes(raw[93:101], "little", signed=True)

        if price_raw <= 0 or not (-18 <= expo <= 0):
            print(f"[s56b][pyth] {asset} implausible price={price_raw} expo={expo}")
            return None

        price = float(price_raw) * (10.0 ** expo)

        # Custody sets max_price_age_sec=5 — the PROGRAM will reject a stale price.
        # Refuse to size off one too. (This is exactly the check that would have
        # caught the frozen Doves feed on day one.)
        age = time.time() - publish_time
        if age > 60:
            print(f"[s56b][pyth] {asset} price is {age:.0f}s STALE (published {publish_time}) "
                  f"— REFUSING to use it.")
            return None

        _bands = {"SOL": (1.0, 10_000.0), "ETH": (10.0, 100_000.0),
                  "BTC": (100.0, 1_000_000.0), "USDC": (0.5, 2.0), "USDT": (0.5, 2.0)}
        lo, hi = _bands.get(asset, (0.0, float("inf")))
        if not (lo <= price <= hi):
            print(f"[s56b][pyth] {asset} ${price} outside sanity band ({lo},{hi}) — REFUSING.")
            return None

        return price
    except Exception as e:
        print(f"[s56b][pyth] {asset} read failed: {e!r}")   # repr: empty-message exceptions still identify their class
        return None


async def _jupiter_mark_price(rpc_client: AsyncClient, asset: str) -> "float | None":
    """Live mark price for an asset, from the SAME Pyth oracle the Jupiter program
    validates against. Resolves the oracle pubkey from the custody account rather
    than a hardcoded map, so an oracle migration can't silently poison our sizing.
    """
    cfg = await _load_jupiter_pool_config(rpc_client)
    entry = cfg.get((asset or "").upper())
    if not entry or not entry.get("oracle"):
        print(f"[s56b][mark] no live oracle for {asset} in pool config")
        return None
    return await _pyth_price(rpc_client, entry["oracle"], asset.upper())


def _resolve_position_accounts(market: str, direction: str) -> dict:
    """
    Resolve all the on-chain accounts needed for a Jupiter Perps position
    on the given market and direction.

    For LONG: collateral is the market asset itself (SOL long → SOL custody).
    For SHORT: collateral is USDC (all shorts collateralize via USDC custody).

    Returns dict with: asset, owner, pool, custody, collateral_custody, mint,
                       collateral_mint, oracle, position, side_int.

    NOTE: the position account key is "position", NOT "position_pda". This
    docstring said "position_pda" until 2026-07-26 and cost a live debug cycle —
    a caller trusting it gets KeyError, or worse, a silent .get() miss.
    """
    market = _pos_base(market)  # 2026-10-16: accept MARKET#SIDE store keys
    asset = _market_to_jupiter_asset(market)
    side_int = JUPITER_SIDE_LONG if direction == "long" else JUPITER_SIDE_SHORT

    # ── 2026-07-29: PREFER THE LIVE POOL CACHE ──────────────────────────────
    # _load_jupiter_pool_config() reads the REAL custodies off the Pool account.
    # Its docstring says callers MUST abort rather than fall back to the stale
    # constants — but it had exactly ONE caller (_jupiter_mark_price). So the
    # PRICE came from live chain data while the CUSTODY ACCOUNT in the
    # instruction still came from JUPITER_CUSTODIES, where BTC pointed at a dead
    # account. Every BTC open died with AccountNotInitialized (3012); SOL only
    # worked because its hardcoded value happened to still be correct.
    #
    # Prefer the live cache and STAMP THE SOURCE so the money paths can refuse
    # to fire on guessed pubkeys while read-only paths keep their fallback.
    _live = _JUP_POOL_CACHE or {}
    _le = _live.get(asset)
    _lu = _live.get("USDC")
    _custody_source = "live_pool_account" if _le and _le.get("custody") else "hardcoded_fallback"

    if _le and _le.get("custody"):
        custody = _le["custody"]
        mint = _le.get("mint") or Pubkey.from_string(JUPITER_TOKEN_MINTS[asset])
        oracle = _le.get("oracle")  # live Pyth account from the custody; never the old Doves map
        if oracle is None:
            _custody_source = "no_live_oracle"
    else:
        if asset not in JUPITER_CUSTODIES:
            raise ValueError(f"no Jupiter custody mapping for asset {asset}")
        custody = Pubkey.from_string(JUPITER_CUSTODIES[asset])
        mint = Pubkey.from_string(JUPITER_TOKEN_MINTS[asset])
        oracle = None  # no live read: no oracle. Money paths refuse on this source.

    if direction == "long":
        collateral_custody = custody  # long uses the asset itself as collateral
        collateral_mint = mint
    else:
        # short uses USDC as collateral
        if _lu and _lu.get("custody"):
            collateral_custody = _lu["custody"]
            collateral_mint = _lu.get("mint") or Pubkey.from_string(JUPITER_TOKEN_MINTS["USDC"])
        else:
            _custody_source = "hardcoded_fallback"
            collateral_custody = Pubkey.from_string(JUPITER_CUSTODIES["USDC"])
            collateral_mint = Pubkey.from_string(JUPITER_TOKEN_MINTS["USDC"])

    keypair = Keypair.from_base58_string(PRIVATE_KEY)
    owner = keypair.pubkey()
    pool = Pubkey.from_string(JUPITER_PERPS_POOL_ACCOUNT)

    position_pda = _derive_position_pda(owner, pool, custody, collateral_custody, side_int)

    return {
        "asset":              asset,
        "owner":              owner,
        "pool":               pool,
        "custody":            custody,
        "collateral_custody": collateral_custody,
        "mint":               mint,
        "collateral_mint":    collateral_mint,
        "oracle":             oracle,
        "position":           position_pda,
        "side_int":           side_int,
        # Where these pubkeys came from. Money paths REFUSE to fire on
        # "hardcoded_fallback" — an unverified custody is how a funded order gets
        # rejected by the chain AFTER the funding swap already ran.
        "_custody_source":    _custody_source,
    }


def _usd_to_lamports_collateral(usd_amount: float, sol_price: float) -> int:
    """Convert USD collateral amount to lamports for SOL collateral (long SOL)."""
    if sol_price <= 0:
        raise ValueError("sol_price must be positive")
    sol_amount = usd_amount / sol_price
    return int(sol_amount * 1_000_000_000)


def _usd_to_usdc_units(usd_amount: float) -> int:
    """Convert USD to USDC token units (6 decimals)."""
    return int(usd_amount * 1_000_000)


def _price_to_jupiter_units(price_usd: float) -> int:
    """Convert a USD price to Jupiter's internal representation (6 decimals)."""
    return int(price_usd * 1_000_000)


async def _wait_for_keeper_execution(
    rpc_client: AsyncClient, position_request: Pubkey, timeout_s: int,
    position: Pubkey = None, expect_open: bool = True,
) -> bool:
    """Wait for a keeper to EXECUTE a PositionRequest — and VERIFY it against the chain.

    ── SPRINT 56c: THE BOT WAS REPORTING FILLS THAT NEVER HAPPENED ──────────────
    The old implementation was:

        if acct is None:
            # Account closed → keeper executed
            return True

    That is WRONG, and it is the most dangerous bug we have found. A PositionRequest
    account disappears for TWO reasons:

        1. the keeper EXECUTED it   -> position opened          (what we want)
        2. the keeper CANCELLED it  -> collateral refunded, NO position

    Both close the account. The old check could not tell them apart, so it called
    BOTH of them success.

    What actually happened on 2026-07-14 (tx Ysz9i25H...):
        t=0    CreateIncreasePositionMarketRequest   OK   -- $4.27 wSOL in
        t=+1s  ClosePositionRequest2                 OK   -- keeper CANCELLED it
        bot:   "keeper executed open"                     -- a lie
    The keeper cancelled because priceSlippage was computed off the frozen Doves
    oracle ($67.83) while SOL traded at $77 — we told it "fill this long at $67.83
    or better," which is unfillable. It refunded us and dropped the order.

    The bot then wrote a local position record and the monitor tracked a GHOST
    position for 108 minutes, reporting entry, P&L, peak and trailing stops on
    nothing. At $7.50 that is a funny story. At $500 with a TP that "fires" and a
    close that "executes," it is a catastrophe.

    ACCOUNT CLOSED IS NOT A FILL. The only proof of a fill is the Position account
    itself carrying nonzero size. That is what we check now.
    """
    deadline = time.time() + timeout_s
    poll_interval = 2.0
    request_gone = False

    while time.time() < deadline:
        try:
            resp = await rpc_client.get_account_info(position_request, commitment=Confirmed)
            acct = resp.value if hasattr(resp, "value") else None
            if acct is None:
                request_gone = True
                break   # resolved one way or the other — now find out WHICH
        except Exception as e:
            print(f"[s39][jupiter] keeper-poll err: {e}")
        await asyncio.sleep(poll_interval)

    if not request_gone:
        print(f"[s56c][jupiter] keeper did not touch the request within {timeout_s}s — "
              f"treating as NOT executed.")
        return False

    if position is None:
        # No position to verify against (shouldn't happen on the open path).
        print("[s56c][jupiter] WARN: request closed but no Position pubkey supplied — "
              "cannot verify fill. Refusing to claim success.")
        return False

    # THE VERIFICATION. Read the Position account. Nonzero size == real fill.
    # Give the chain a moment to settle before reading.
    for attempt in range(6):
        await asyncio.sleep(1.5)
        size_usd = await _read_position_size_usd(rpc_client, position)
        if size_usd is None:
            continue
        if expect_open and size_usd > 0:
            print(f"[s56c][jupiter] FILL VERIFIED — position size_usd={size_usd} (on-chain)")
            return True
        if (not expect_open) and size_usd == 0:
            print(f"[s56c][jupiter] CLOSE VERIFIED — position size_usd=0 (on-chain)")
            return True

    if expect_open:
        print("[s56c][jupiter] REQUEST WAS CANCELLED, NOT EXECUTED. The PositionRequest "
              "account closed but no position exists on-chain (size_usd=0). Your collateral "
              "was refunded. This is NOT a fill — refusing to record a phantom position.")
    else:
        print("[s56c][jupiter] close request closed but position still has size — not closed.")
    return False


async def _read_position_size_usd(rpc_client: AsyncClient = None, position: Pubkey = None):
    """Read `size_usd` straight off the on-chain Position account. None on failure.

    This is the ONLY source of truth about whether we hold a position. Local JSON
    state is a cache, not evidence — and it was the cache that let a ghost position
    survive for 108 minutes.

    ── SPRINT 56c FIX: OWN YOUR CLIENT ─────────────────────────────────────────
    The `rpc_client` arg is IGNORED for the account read. The close/open paths
    that call this run their verification AFTER their own `finally: rpc_client.close()`
    has fired (the keeper-wait outlives the send), so a passed-in client is already
    dead — every read threw "Cannot send a request, as the client has been closed"
    and a GOOD close got reported as "still has size." Verification is the one thing
    that must not depend on a client someone else can close underneath it, so it
    opens (and closes) its own. The arg stays in the signature for call-site
    compatibility but is deliberately unused.
    """
    own_client = AsyncClient(RPC_URL, commitment=Confirmed)
    try:
        program = await _get_jupiter_program(own_client)
        if program is None:
            return None
        # ── SPRINT 57 FIX: rebind the (possibly CACHED) Program to our LIVE client ──
        # _get_jupiter_program caches the Program bound to the provider/connection of
        # whichever client built it first. That first client has since been closed
        # (its own caller's finally fired). program.account[...].fetch() reads through
        # program.provider.connection — the DEAD one — so a cached Program throws
        # "Cannot send a request, as the client has been closed." This surfaced the
        # moment reconcile read three markets back-to-back: market 1 built+cached the
        # Program then closed its client, markets 2 and 3 hit the stale connection.
        # Point the Program's provider at our live own_client for this read.
        try:
            program.provider.connection = own_client
        except Exception:
            pass
        acct = await _fetch_account_resilient(program, "Position", position)
        if acct is None:
            raise RuntimeError("Position account undecodable with current IDL (see discriminator diagnostic in log)")
        return int(getattr(acct, "size_usd", 0) or 0)
    except Exception as e:
        _m = str(e).lower()
        if "does not exist" in _m or "not found" in _m:
            return 0   # no account == no position == size 0
        print(f"[s56c][jupiter] position read failed: {e}")
        return None
    finally:
        try:
            await own_client.close()
        except Exception:
            pass


# ── Sprint 43: Jupiter auto-funding helpers ──────────────────────────────────
# Single-position sequential bot, so a module-level fill record is safe to pass
# the ACTUAL (post-downsize) fill back to the open path for position recording.
_LAST_JUP_FILL = None


async def _get_token_ui_balance(rpc_client, owner, mint) -> float:
    """UI balance (human units) of an SPL token in owner's ATA. 0.0 if no ATA/error."""
    try:
        ata = _derive_associated_token_account(owner, mint)
        resp = await rpc_client.get_token_account_balance(ata, commitment=Confirmed)
        return float(resp.value.ui_amount or 0.0)
    except Exception as e:
        # "could not find account" / "invalid param" = the ATA doesn't exist yet
        # (wallet never wrapped SOL or never held this token). That's a legit 0
        # balance, not an error — return silently. Only log unexpected failures.
        _m = str(e).lower()
        if ("could not find account" in _m or "invalid param" in _m
                or "account not found" in _m or "found no" in _m):
            return 0.0
        print(f"[s43][fund] token balance read ({str(mint)[:6]}..): {type(e).__name__}: {e!r} (RPC_URL unreachable or wrong? check bots/.env)")
        return 0.0


@_perp_inflight
async def _wrap_native_sol_to_wsol(rpc_client, keypair, sol_to_wrap: float) -> bool:
    if PERPS_DRY_RUN:
        return False
    """Wrap `sol_to_wrap` native SOL into the owner's wSOL ATA.
    create-idempotent ATA + system transfer + sync_native (legacy tx). True on confirm."""
    try:
        owner     = keypair.pubkey()
        wsol_mint = Pubkey.from_string(JUPITER_TOKEN_MINTS["SOL"])
        wsol_ata  = _derive_associated_token_account(owner, wsol_mint)
        lamports  = int(sol_to_wrap * 1_000_000_000)
        if lamports <= 0:
            return True
        tok_prog = Pubkey.from_string(SOLANA_TOKEN_PROGRAM)
        sys_prog = Pubkey.from_string(SOLANA_SYSTEM_PROGRAM)
        ata_prog = Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROG)
        ixs = [
            set_compute_unit_limit(120_000),
            set_compute_unit_price(JUPITER_PERPS_CU_PRICE_MICRO),
            Instruction(  # CreateIdempotent wSOL ATA
                program_id=ata_prog,
                accounts=[
                    AccountMeta(pubkey=owner,     is_signer=True,  is_writable=True),
                    AccountMeta(pubkey=wsol_ata,  is_signer=False, is_writable=True),
                    AccountMeta(pubkey=owner,     is_signer=False, is_writable=False),
                    AccountMeta(pubkey=wsol_mint, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=sys_prog,  is_signer=False, is_writable=False),
                    AccountMeta(pubkey=tok_prog,  is_signer=False, is_writable=False),
                ],
                data=bytes([1]),
            ),
            _sys_transfer(_SysTransferParams(from_pubkey=owner, to_pubkey=wsol_ata, lamports=lamports)),
            Instruction(  # SyncNative
                program_id=tok_prog,
                accounts=[AccountMeta(pubkey=wsol_ata, is_signer=False, is_writable=True)],
                data=bytes([17]),
            ),
        ]
        bh  = (await rpc_client.get_latest_blockhash(commitment=Confirmed)).value.blockhash
        msg = Message.new_with_blockhash(ixs, owner, bh)
        tx  = Transaction.new_unsigned(msg)
        _dry_run_refuse("transaction")
        tx.sign([keypair], bh)
        resp = await rpc_client.send_transaction(
            tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=3))
        await rpc_client.confirm_transaction(resp.value, commitment=Confirmed)
        print(f"[s43][fund] wrapped {sol_to_wrap:.4f} SOL -> wSOL ({str(resp.value)[:16]}..)")
        return True
    except Exception as e:
        print(f"[s43][fund] wrap failed (continuing with existing wSOL): {e}")
        return False


async def _swap_sol_to_token(rpc_client, keypair, token: str, ui_needed: float,
                              decimals: int) -> float:
    if PERPS_DRY_RUN:
        return 0.0
    """Generalized SOL -> <token> swap via Jupiter ExactOut (2026-07-23 — built
    for the funding_account AccountNotInitialized fix: an ETH/BTC long from a
    SOL-only wallet had NO funding path; the chain's own Anchor log named the
    missing account. The Jupiter swap creates the output ATA as part of the
    swap tx, so this kills the 3012 at the root). Returns token UI received."""
    try:
        import httpx
        owner = keypair.pubkey()
        out_mint = JUPITER_TOKEN_MINTS[token]
        wsol_mint = JUPITER_TOKEN_MINTS["SOL"]
        target = ui_needed * (1 + _perp_get_override("JUPITER_FUND_BUFFER_BPS", JUPITER_FUND_BUFFER_BPS) / 10_000)
        out_units = int(target * (10 ** decimals))
        if out_units <= 0:
            return 0.0
        before = await _get_token_ui_balance(rpc_client, owner, Pubkey.from_string(out_mint))
        swap_tx_b64 = None
        _ladder_errs = []
        async with httpx.AsyncClient(timeout=20) as c:
            for _base, _jh in _jup_attempts():
                try:
                    q = await c.get(f"{_base}/quote", headers=_jh, params={
                        "inputMint": wsol_mint, "outputMint": out_mint,
                        "amount": out_units, "swapMode": "ExactOut",
                        "slippageBps": _perp_get_override("JUPITER_SWAP_SLIPPAGE_BPS", JUPITER_SWAP_SLIPPAGE_BPS),
                    })
                    q.raise_for_status()
                    quote = q.json()
                    s = await c.post(f"{_base}/swap", headers=_jh, json={
                        "quoteResponse": quote,
                        "userPublicKey": str(owner),
                        "wrapAndUnwrapSol": True,
                        "dynamicComputeUnitLimit": True,
                    })
                    s.raise_for_status()
                    swap_tx_b64 = s.json()["swapTransaction"]
                    if _ladder_errs:
                        print(f"[s43][fund] swap via fallback endpoint {_base} (primary failed: {_ladder_errs})")
                    break
                except Exception as _le:
                    _ladder_errs.append(f"{_base}: {_le!r}")
                    continue
        if not swap_tx_b64:
            print(f"[s43][fund] swap SOL->{token} FAILED on every endpoint: {_ladder_errs}")
            return 0.0
        vt = VersionedTransaction.from_bytes(_b64.b64decode(swap_tx_b64))
        _dry_run_refuse("transaction")
        signed = VersionedTransaction(vt.message, [keypair])
        resp = await rpc_client.send_raw_transaction(
            bytes(signed), opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3))
        await rpc_client.confirm_transaction(resp.value, commitment=Confirmed)
        after = await _get_token_ui_balance(rpc_client, owner, Pubkey.from_string(out_mint))
        received = max(0.0, after - before)
        print(f"[s43][fund] swapped SOL -> {received:.6f} {token} ({str(resp.value)[:16]}..)")
        return received
    except Exception as e:
        print(f"[s43][fund] swap SOL->{token} failed: {e!r}")
        return 0.0


async def _swap_sol_to_usdc(rpc_client, keypair, usdc_needed: float) -> float:
    if PERPS_DRY_RUN:
        return 0.0
    """Best-effort swap native SOL -> USDC via Jupiter (ExactOut). Returns USDC UI received.
    0.0 on any failure (caller downsizes to existing USDC)."""
    try:
        import httpx
        owner     = keypair.pubkey()
        usdc_mint = JUPITER_TOKEN_MINTS["USDC"]
        wsol_mint = JUPITER_TOKEN_MINTS["SOL"]
        target    = usdc_needed * (1 + _perp_get_override("JUPITER_FUND_BUFFER_BPS", JUPITER_FUND_BUFFER_BPS) / 10_000)
        out_units = int(target * 1_000_000)  # USDC 6 decimals
        if out_units <= 0:
            return 0.0
        usdc_before = await _get_token_ui_balance(rpc_client, owner, Pubkey.from_string(usdc_mint))
        async with httpx.AsyncClient(timeout=20) as c:
            q = await c.get(f"{JUPITER_SWAP_API_BASE}/quote", params={
                "inputMint": wsol_mint, "outputMint": usdc_mint,
                "amount": out_units, "swapMode": "ExactOut",
                "slippageBps": _perp_get_override("JUPITER_SWAP_SLIPPAGE_BPS", JUPITER_SWAP_SLIPPAGE_BPS),
            })
            q.raise_for_status()
            quote = q.json()
            s = await c.post(f"{JUPITER_SWAP_API_BASE}/swap", json={
                "quoteResponse": quote,
                "userPublicKey": str(owner),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
            })
            s.raise_for_status()
            swap_tx_b64 = s.json()["swapTransaction"]
        vt     = VersionedTransaction.from_bytes(_b64.b64decode(swap_tx_b64))
        _dry_run_refuse("transaction")
        signed = VersionedTransaction(vt.message, [keypair])
        resp = await rpc_client.send_raw_transaction(
            bytes(signed), opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3))
        await rpc_client.confirm_transaction(resp.value, commitment=Confirmed)
        usdc_after = await _get_token_ui_balance(rpc_client, owner, Pubkey.from_string(usdc_mint))
        received = max(0.0, usdc_after - usdc_before)
        print(f"[s43][fund] swapped SOL -> {received:.2f} USDC ({str(resp.value)[:16]}..)")
        return received
    except Exception as e:
        print(f"[s43][fund] swap SOL->USDC failed (continuing with existing USDC): {e}")
        return 0.0


USDC_FLOOR_USD = 5.0  # Sprint 48: always leave $5 USDC in wallet; sweep the rest to SOL


async def _swap_token_to_sol(rpc_client, keypair, input_mint: str, ui_amount: float, decimals: int) -> bool:
    if PERPS_DRY_RUN:
        return False
    """Best-effort ExactIn swap of a token -> native SOL via Jupiter. True on success."""
    try:
        import httpx
        owner = keypair.pubkey()
        wsol_mint = JUPITER_TOKEN_MINTS["SOL"]
        in_units = int(ui_amount * (10 ** decimals))
        if in_units <= 0:
            return False
        async with httpx.AsyncClient(timeout=20) as c:
            q = await c.get(f"{JUPITER_SWAP_API_BASE}/quote", params={
                "inputMint": input_mint, "outputMint": wsol_mint,
                "amount": in_units, "swapMode": "ExactIn",
                "slippageBps": _perp_get_override("JUPITER_SWAP_SLIPPAGE_BPS", JUPITER_SWAP_SLIPPAGE_BPS),
            })
            q.raise_for_status(); quote = q.json()
            s = await c.post(f"{JUPITER_SWAP_API_BASE}/swap", json={
                "quoteResponse": quote, "userPublicKey": str(owner),
                "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
            })
            s.raise_for_status(); swap_tx_b64 = s.json()["swapTransaction"]
        vt = VersionedTransaction.from_bytes(_b64.b64decode(swap_tx_b64))
        _dry_run_refuse("transaction")
        signed = VersionedTransaction(vt.message, [keypair])
        resp = await rpc_client.send_raw_transaction(bytes(signed), opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3))
        await rpc_client.confirm_transaction(resp.value, commitment=Confirmed)
        print(f"[sweep] {ui_amount:.4f} of {input_mint[:6]}.. -> SOL ({str(resp.value)[:16]}..)")
        return True
    except Exception as e:
        print(f"[sweep] token->SOL swap failed: {e}")
        return False


async def _sweep_wallet_to_sol(rpc_client, keypair):
    if PERPS_DRY_RUN:
        return None
    """Sprint 48: after a close / pull-initial, convert proceeds back to SOL.
    USDC sweeps down to the $5 floor; BTC/ETH collateral (from a long) sweeps
    fully. SOL is the one true balance."""
    try:
        owner = keypair.pubkey()
        usdc_mint = JUPITER_TOKEN_MINTS["USDC"]
        have_usdc = await _get_token_ui_balance(rpc_client, owner, Pubkey.from_string(usdc_mint))
        if have_usdc > USDC_FLOOR_USD + 0.01:
            await _swap_token_to_sol(rpc_client, keypair, usdc_mint, have_usdc - USDC_FLOOR_USD, 6)
        for sym, dec in (("BTC", 8), ("ETH", 8)):
            mint = JUPITER_TOKEN_MINTS.get(sym)
            if not mint:
                continue
            bal = await _get_token_ui_balance(rpc_client, owner, Pubkey.from_string(mint))
            if bal and bal > 0:
                await _swap_token_to_sol(rpc_client, keypair, mint, bal, dec)
    except Exception as e:
        print(f"[sweep] wallet sweep err: {e}")

# ── POST-CLOSE RESIDUAL ROUTER (2026-08-08) ─────────────────────────────────
# Built to `owner_inbox/patch_eth_close_route_residual_to_sol_20260808.md`.
# The owner: "that eth thing is because it didn't get routed back to sol when the
# position closed last night." SOL is the system's base carrier - open a BTC or
# ETH position and the proceeds come back to SOL whenever the agent closes it.
#
# THE ACTUAL BUG WAS A RACE, not a missing sweep. _sweep_wallet_to_sol already
# existed and was already called on close - but Jupiter Perps is a KEEPER model:
# we submit a close REQUEST and a keeper fills it later (the close log literally
# prints `executed=False`). The sweep fired the instant the request was submitted,
# found an empty wallet because the WETH had not landed yet, swept nothing, and
# returned. The keeper settled moments later, the residual arrived, and nothing
# ever looked again.
#
# So a single post-close sweep can never be enough. This retries on a backoff
# until the residual is actually gone, and a periodic reconcile catches whatever
# the retries miss - liquidations and force-settles especially, which produce no
# tx of ours and so never triggered the old sweep at all.
RESIDUAL_DUST_USD          = float(_envv("PERP_RESIDUAL_DUST_USD", "0.50"))
RESIDUAL_RETRY_SCHEDULE_S  = (20, 60, 180, 420, 900)   # ~15min of keeper patience
_RESIDUAL_ROUTER_ACTIVE    = set()      # markets with a router already in flight


async def _residual_route_to_sol(market: str, reason: str = "post_close") -> dict:
    if PERPS_DRY_RUN:
        return {"skipped": "paper_mode"}
    """Sweep non-SOL settlement residual back to SOL, retrying past the keeper lag.

    SAFETY, by construction and by intent:
      - touches ONLY the hot wallet's BTC / ETH / USDC ATAs. every other
        token is a different mint and is never enumerated here.
      - the ledger / home_base wallet is a different keypair entirely and is not
        reachable from this path.
      - USDC keeps its existing floor; this is not a stablecoin drain.
      - dust below RESIDUAL_DUST_USD is left alone and acked as such - paying a
        swap fee to move $0.20 is a loss, not hygiene.
    """
    ack = {"market": market, "reason": reason, "attempts": [], "result": None,
           "routed": [], "at": _s39_now_iso()}
    if market in _RESIDUAL_ROUTER_ACTIVE:
        ack["result"] = "already_running"
        return ack
    _RESIDUAL_ROUTER_ACTIVE.add(market)
    try:
        rpc_client = AsyncClient(RPC_URL, commitment=Confirmed)
        keypair = Keypair.from_base58_string(PRIVATE_KEY)
        owner = keypair.pubkey()

        async def _quote_sol_out(mint: str, ui: float, dec: int) -> float:
            """What this residual is actually WORTH in SOL, per Jupiter.

            The quote is the valuation. A mark price would tell us what the token
            is theoretically worth; the quote tells us what we would really
            receive, which is the only number that decides whether a swap is worth
            its fee. Same endpoint the swap uses, so no new dependency.
            """
            try:
                import httpx
                units = int(ui * (10 ** dec))
                if units <= 0:
                    return 0.0
                async with httpx.AsyncClient(timeout=15) as c:
                    for base, _jh in _jup_attempts():
                        try:
                            r = await c.get(f"{base}/quote", headers=_jh, params={
                                "inputMint": mint,
                                "outputMint": JUPITER_TOKEN_MINTS["SOL"],
                                "amount": units, "swapMode": "ExactIn",
                                "slippageBps": 100})
                            if r.status_code == 200:
                                return int((r.json() or {}).get("outAmount") or 0) / 1e9
                        except Exception:
                            continue
            except Exception:
                pass
            return 0.0

        _sol_px = 0.0
        try:
            _sol_px = float(await get_sol_price() or 0)
        except Exception:
            pass

        for i, delay in enumerate(RESIDUAL_RETRY_SCHEDULE_S):
            await asyncio.sleep(delay)
            found = []
            for sym, dec in (("ETH", 8), ("BTC", 8)):
                mint = JUPITER_TOKEN_MINTS.get(sym)
                if not mint:
                    continue
                try:
                    bal = await _get_token_ui_balance(rpc_client, owner, Pubkey.from_string(mint))
                except Exception:
                    bal = 0
                if not bal or bal <= 0:
                    continue
                # value the dust floor in USD, not in token units - 0.0001 ETH and
                # 0.0001 BTC are very different amounts of stranded capital.
                _sol_out = await _quote_sol_out(mint, bal, dec)
                usd = (_sol_out * _sol_px) if (_sol_out and _sol_px) else None
                if usd is not None and usd < RESIDUAL_DUST_USD:
                    ack["attempts"].append({"n": i + 1, "asset": sym, "ui": bal,
                                            "usd": round(usd, 4), "action": "below_dust"})
                    continue
                ok = await _swap_token_to_sol(rpc_client, keypair, mint, bal, dec)
                ack["attempts"].append({"n": i + 1, "asset": sym, "ui": bal,
                                        "usd": (round(usd, 2) if usd is not None else None),
                                        "action": "swapped" if ok else "swap_failed"})
                if ok:
                    ack["routed"].append({"asset": sym, "ui": bal})
                found.append((sym, bal, ok))

            if not found:
                # nothing left to move. If we routed something on an earlier pass
                # that is a success; if we never saw anything it was dust or the
                # close produced no foreign residual at all.
                ack["result"] = "residual_routed" if ack["routed"] else "residual_below_dust"
                break
            if all(ok for _s, _b, ok in found):
                ack["result"] = "residual_routed"
                # one more pass to catch a keeper that settled mid-sweep
                continue
        else:
            ack["result"] = ack["result"] or "residual_route_failed"

        if ack["result"] == "residual_route_failed":
            # ESCALATE ONCE, then leave it tagged. A retry storm on a broken route
            # burns fees; a silent failure strands capital. One alert, one tag.
            try:
                await notify(f"⚠️ <b>RESIDUAL ROUTE FAILED</b> - {market}\n"
                             f"non-SOL residual could not be swapped back to SOL after "
                             f"{len(RESIDUAL_RETRY_SCHEDULE_S)} attempts.\n"
                             f"tagged needs_manual_route - nothing was lost, it is sitting "
                             f"in the hot wallet as {', '.join(a['asset'] for a in ack['attempts']) or 'foreign token'}.")
            except Exception:
                pass
            ack["needs_manual_route"] = True
    except Exception as e:
        ack["result"] = "residual_route_failed"
        ack["error"] = str(e)[:300]
    finally:
        _RESIDUAL_ROUTER_ACTIVE.discard(market)
        try:
            _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                "ts": _s39_now_iso(), "venue": "jupiter",
                "operation": "residual_route", **ack})
        except Exception:
            pass
        print(f"[residual] {market} {ack.get('result')} "
              f"({len(ack.get('routed') or [])} routed, {len(ack.get('attempts') or [])} attempts)")
    return ack


async def _jupiter_fund_collateral(rpc_client, keypair, direction, asset, requested_collateral_usd, sol_price):
    """Fund the Jupiter collateral ATA from native SOL, return (affordable_collateral_usd, actions)."""
    owner   = keypair.pubkey()
    actions = []
    if requested_collateral_usd <= 0 or sol_price <= 0:
        return max(0.0, requested_collateral_usd), actions

    if direction == "long" and asset == "SOL":
        wsol_mint = Pubkey.from_string(JUPITER_TOKEN_MINTS["SOL"])
        have_sol  = await _get_token_ui_balance(rpc_client, owner, wsol_mint)   # wSOL, in SOL units
        need_sol  = requested_collateral_usd / sol_price
        if have_sol < need_sol:
            native    = await get_sol_balance(rpc_client)
            spendable = max(0.0, native - _perp_get_override("JUPITER_SOL_FEE_RESERVE", JUPITER_SOL_FEE_RESERVE))
            deficit   = (need_sol - have_sol) * (1 + _perp_get_override("JUPITER_FUND_BUFFER_BPS", JUPITER_FUND_BUFFER_BPS) / 10_000)
            to_wrap   = min(deficit, spendable)
            if to_wrap > 0 and await _wrap_native_sol_to_wsol(rpc_client, keypair, to_wrap):
                actions.append(f"wrapped {to_wrap:.4f} SOL → wSOL")
            have_sol = await _get_token_ui_balance(rpc_client, owner, wsol_mint)
        return min(requested_collateral_usd, have_sol * sol_price), actions

    if direction == "short":
        usdc_mint = Pubkey.from_string(JUPITER_TOKEN_MINTS["USDC"])
        have_usdc = await _get_token_ui_balance(rpc_client, owner, usdc_mint)   # USD
        _usdc0, received = have_usdc, 0.0
        if have_usdc < requested_collateral_usd:
            received = await _swap_sol_to_usdc(rpc_client, keypair, requested_collateral_usd - have_usdc)
            if received > 0:
                actions.append(f"swapped SOL → {received:.2f} USDC")
            have_usdc = await _get_token_ui_balance(rpc_client, owner, usdc_mint)
        _funded = min(requested_collateral_usd, have_usdc)
        globals()["_LAST_FUND_INFO"] = {"asset": "USDC", "on_hand_before_usd": round(_usdc0, 4),
                                        "swapped_usd": round(received, 4), "funded_usd": round(_funded, 4),
                                        "requested_usd": round(requested_collateral_usd, 4),
                                        "on_hand_after_swap_usd": round(have_usdc, 4),
                                        "usdc_floor_usd": globals().get("USDC_FLOOR_USD"),
                                        "source": "wallet token balance before/after (swap = balance delta)"}
        print(f"[s43][fund] short: funded ${_funded:.2f} of ${requested_collateral_usd:.2f} "
              f"(USDC on hand ${_usdc0:.2f} + swapped ${received:.2f})")
        return _funded, actions

    # ETH/BTC long — FIELD FIX (2026-07-23, first fire attempt died here):
    # "no SOL-funding path" + empty ATA = guaranteed AccountNotInitialized 3012
    # at the chain. Fund it like the shorts path funds USDC: check the token
    # balance, swap SOL -> token ExactOut for the deficit (the swap tx creates
    # the ATA), return what's actually funded.
    try:
        tok_mint = Pubkey.from_string(JUPITER_TOKEN_MINTS[asset])
        tok_dec = {"ETH": 8, "BTC": 8}.get(asset, 8)
        tok_px = None
        try:
            tok_px = await _jupiter_oracle_price(rpc_client, asset)
        except Exception:
            pass
        if not tok_px or tok_px <= 0:
            print(f"[s43][fund] long {asset}: no oracle mark — cannot size the funding swap; using existing ATA balance")
            return requested_collateral_usd, actions
        have_tok = await _get_token_ui_balance(rpc_client, owner, tok_mint)
        have_usd = have_tok * tok_px
        if have_usd < requested_collateral_usd:
            need_tok = (requested_collateral_usd - have_usd) / tok_px
            received = await _swap_sol_to_token(rpc_client, keypair, asset, need_tok, tok_dec)
            if received > 0:
                actions.append(f"swapped SOL → {received:.6f} {asset}")
            have_tok = await _get_token_ui_balance(rpc_client, owner, tok_mint)
            have_usd = have_tok * tok_px
        print(f"[s43][fund] long {asset}: funded ${min(requested_collateral_usd, have_usd):.2f} "
              f"of ${requested_collateral_usd:.2f} requested ({have_tok:.6f} {asset} in ATA)")
        return min(requested_collateral_usd, have_usd), actions
    except Exception as _fe:
        print(f"[s43][fund] long {asset} funding err ({_fe!r}) — declining, not continuing at requested size")
        raise


# ---- PAPER MODE (PERPS_DRY_RUN) ----------------------------------------------
PAPER_FILLS_FILE = os.path.join(WS_BOT_LOGS, "paper_fills.jsonl")


def _dry_run_refuse(what: str) -> None:
    """Last line of defence: every sign/send site calls this first."""
    if PERPS_DRY_RUN:
        raise RuntimeError(f"PERPS_DRY_RUN: refusing to sign/send {what}")


def _paper_record(event: dict) -> None:
    try:
        os.makedirs(os.path.dirname(PAPER_FILLS_FILE), exist_ok=True)
        with open(PAPER_FILLS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(EST).isoformat(), **event}, default=str) + "\n")
    except Exception as e:
        print(f"[paper] fill log err {e!r}")


async def _paper_open(direction, size_usd, leverage, collateral_usd, market):
    """Simulated fill at the oracle mark. Returns a PAPER-... id, or None if unpriced."""
    mark = await _market_mark_price(market)
    if not mark:
        print(f"[paper] open {market}: no oracle price, not filled")
        return None
    global _LAST_JUP_FILL
    _LAST_JUP_FILL = {"size_usd": float(size_usd), "collateral_usd": float(collateral_usd),
                      "fund_actions": ["paper: no funding swap"]}
    globals()["_LAST_OPEN_MARK"] = {"market": market, "mark": float(mark), "at": time.time()}
    sig = f"PAPER-open-{secrets.token_hex(6)}"
    _paper_record({"event": "open", "sig": sig, "market": market, "direction": direction,
                   "size_usd": size_usd, "leverage": leverage, "collateral_usd": collateral_usd, "mark": mark})
    print(f"[paper] OPEN {market} {direction} ${float(size_usd):.2f} {float(leverage):g}x @ {float(mark):,.4f}")
    return sig


async def _paper_close(pos: dict, fraction: float = 1.0):
    market = pos.get("market", "SOL-PERP")
    mark = await _market_mark_price(market)
    if not mark:
        print(f"[paper] close {market}: no oracle price, not filled")
        return None
    sig = f"PAPER-close-{secrets.token_hex(6)}"
    _paper_record({"event": "close", "sig": sig, "market": market, "direction": pos.get("direction"),
                   "fraction": fraction, "entry": pos.get("entry_price"), "mark": mark})
    print(f"[paper] CLOSE {market} {fraction*100:.0f}% @ {float(mark):,.4f}")
    return sig


@_perp_inflight
async def jupiter_open_position(
    direction: str, size_usd: float, leverage: float,
    collateral_usd: float, tp_price: float | None, sl_price: float | None,
    sol_price: float, market: str = "SOL-PERP",
) -> str | None:
    """
    Open a Jupiter Perps position via createIncreasePositionMarketRequest.

    Real on-chain execution:
      1. Resolve all account pubkeys (position PDA, custody, oracle, ATAs)
      2. Build createIncreasePositionMarketRequest instruction
      3. Wrap SOL → wSOL ATA if long SOL (collateral is native SOL → wSOL)
      4. Submit tx, wait for confirmation
      5. Poll for keeper execution (PositionRequest closing = executed)
      6. Note: TP/SL is bot-side, NOT on-chain (Jupiter API gate)

    Returns: tx_sig string on success, None on failure.
    """
    if direction not in ("long", "short"):
        print(f"[s39][jupiter] invalid direction: {direction}")
        return None
    if market not in ("SOL-PERP", "ETH-PERP", "BTC-PERP"):
        print(f"[s39][jupiter] market {market} not supported by Jupiter Perps")
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts": _s39_now_iso(), "venue": "jupiter", "operation": "open_position",
            "market": market, "success": False,
            "error": f"market_not_supported_by_jupiter",
        })
        return None

    if PERPS_DRY_RUN:
        return await _paper_open(direction, size_usd, leverage, collateral_usd, market)

    rpc_client = AsyncClient(RPC_URL, commitment=Confirmed)
    try:
        # ── 2026-07-29: LOAD LIVE CUSTODIES BEFORE BUILDING THE INSTRUCTION ──
        # The pool config is what makes accts["custody"] real rather than a
        # constant that may have migrated. Abort if we cannot get it: firing an
        # order with a guessed custody costs a funding swap and returns 3012.
        _pool_cfg = await _load_jupiter_pool_config(rpc_client)
        if not _pool_cfg:
            print("[s56b][custody] ABORT open: live pool config unavailable — refusing "
                  "to build an instruction from the hardcoded custody map (BTC's entry "
                  "was known dead). Fix the Pool read; do not bypass it.")
            return None
        accts = _resolve_position_accounts(market, direction)
        if accts.get("_custody_source") != "live_pool_account":
            print(f"[s56b][custody] ABORT open: {market} resolved custody from "
                  f"{accts.get('_custody_source')} — not the live Pool account. "
                  f"Refusing to fire on unverified pubkeys.")
            return None
        print(f"[s56b][custody] {market} custody={accts['custody']} "
              f"(source={accts.get('_custody_source')})")
        keypair = Keypair.from_base58_string(PRIVATE_KEY)

        # Random u64 counter for unique PositionRequest PDA
        counter = secrets.randbits(64)
        position_request_pda = _derive_position_request_pda(accts["position"], counter, JUPITER_REQUEST_CHANGE_INCREASE)
        # SPRINT 56 DIAG: print the exact seed inputs. The ConstraintSeeds error
        # (Custom 2006) tells us Left (ours) vs Right (the program's) — but without
        # the counter we cannot reverse-engineer which seed layout the program used.
        # With counter + position + both PDAs in hand, the layout falls out directly.
        print(f"[s56][pda] owner            = {accts['owner']}")
        print(f"[s56][pda] position         = {accts['position']}")
        print(f"[s56][pda] counter          = {counter}")
        print(f"[s56][pda] counter_le_hex   = {counter.to_bytes(8, 'little').hex()}")
        print(f"[s56][pda] position_request = {position_request_pda}  <-- what we pass (Left)")

        # Determine input mint + funding account
        # For SOL longs: input is wSOL; funding account is owner's wSOL ATA
        # For shorts: input is USDC; funding account is owner's USDC ATA
        input_mint = accts["collateral_mint"]
        funding_ata = _derive_associated_token_account(accts["owner"], input_mint)

        # Sprint 43: auto-fund collateral from native SOL (wrap longs / swap shorts),
        # then downsize to whatever's affordable. SOL is the universal fuel — it just goes thru.
        global _LAST_JUP_FILL
        _LAST_JUP_FILL = None
        globals()["_LAST_FUND_INFO"] = None
        _fund_actions = []
        try:
            _affordable, _fund_actions = await _jupiter_fund_collateral(
                rpc_client, keypair, direction, accts["asset"], collateral_usd, sol_price)
            if _affordable + 1e-9 < collateral_usd * 0.99:
                # 2026-10-16: DECLINE, never downsize. The number that lands is the
                # number asked for, or nothing lands.
                _why = (f"underfunded: funding produced ${_affordable:.2f} of the ${collateral_usd:.2f} "
                        f"collateral asked for (see [s43][fund] above). Declined, not downsized.")
                print(f"[s56][jupiter] DECLINE open: {_why}")
                globals()["_LAST_OPEN_ERR"] = "unfunded_after_swap: " + _why + " " + " | ".join(map(str, _fund_actions))
                try:
                    _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                        "ts": _s39_now_iso(), "venue": "jupiter", "event": "open_aborted",
                        "reason": "unfunded_after_swap", "market": market,
                        "funded_usd": round(_affordable, 2), "requested_usd": round(collateral_usd, 2)})
                except Exception:
                    pass
                return None
            if _affordable + 1e-9 < collateral_usd:
                _lev = (size_usd / collateral_usd) if collateral_usd > 0 else 1.0
                _orig_s, _orig_c = size_usd, collateral_usd
                collateral_usd = _affordable
                size_usd       = _affordable * _lev
                _fund_actions.append(
                    f"downsized ${_orig_s:.2f}→${size_usd:.2f} (collateral ${_orig_c:.2f}→${collateral_usd:.2f})")
                print(f"[s43][fund] downsized to affordable: size ${size_usd:.2f}, collateral ${collateral_usd:.2f}")
                # 2026-07-23 (field: funding swap failed quietly → downsized to
                # ~$0 → the open proceeded anyway → guaranteed 3012 at the
                # chain): an UNFUNDED open never leaves the building. Abort
                # with the cause instead of paying the chain to say no.
                _min_c = float(_perp_get_override("MIN_COLLATERAL_USD", MIN_COLLATERAL_USD))
                if collateral_usd < _min_c:
                    print(f"[s56][jupiter] ABORT open: funding produced ${collateral_usd:.2f} "
                          f"(< ${_min_c:.2f} min) — the SOL→{accts['asset']} swap failed or "
                          f"underfilled; see [s43][fund] error above. Not sending an unfunded "
                          f"order to the chain.")
                    _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                        "ts": _s39_now_iso(), "venue": "jupiter", "event": "open_aborted",
                        "reason": "unfunded_after_swap", "market": market,
                        "funded_usd": round(collateral_usd, 2)})
                    return None
        except Exception as e:
            print(f"[s43][fund] funding pipeline error (declining, not continuing at requested size): {e}")
            globals()["_LAST_OPEN_ERR"] = f"unfunded_after_swap: funding pipeline error {e}"
            try:
                _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                    "ts": _s39_now_iso(), "venue": "jupiter", "event": "open_aborted",
                    "reason": "funding_check_failed", "market": market,
                    "error": str(e)[:300]})
            except Exception:
                pass
            return None

        # ── SPRINT 56: REAL PER-MARKET PRICING ───────────────────────────────
        # The old block: SOL used sol_price; ETH divided by 1e9 and by sol_price
        # ("placeholder"); BTC divided by sol_price*1000 (a guess). And then
        # priceSlippage below was computed off sol_price for EVERY market — so
        # an ETH open bounded its slippage against the SOL price.
        #
        # Now: read the market's OWN Jupiter Doves oracle — the same oracle the
        # program validates against. If we can't get a price, we ABORT. A wrong
        # priceSlippage is a bad fill or an instant liquidation; there is no
        # "degrade gracefully" here.
        mark_price = await _jupiter_oracle_price(rpc_client, accts["asset"])
        if mark_price is None and accts["asset"] == "SOL":
            mark_price = sol_price   # SOL has a known-good CoinGecko fallback
        if mark_price is None or mark_price <= 0:
            msg = (f"[s56][jupiter] ABORT open: no usable mark price for "
                   f"{accts['asset']} — refusing to size a position blind.")
            print(msg)
            _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                "ts": _s39_now_iso(), "venue": "jupiter", "event": "open_aborted",
                "reason": "no_mark_price", "market": market, "detail": msg,
            })
            return None
        print(f"[s56][jupiter] {accts['asset']} mark (Jupiter oracle): ${mark_price:,.4f}")
        # 2026-07-23 (field: first green open booked ETH at SOL's price — entry
        # $76.30 on a $1,891 market → fictional +7149% "TP hit" → the monitor
        # nearly round-tripped the position): publish the REAL per-market mark
        # for the success-path bookkeeper. The mark that opened is the mark
        # that gets recorded.
        try:
            globals()["_LAST_OPEN_MARK"] = {"market": market, "mark": float(mark_price),
                                              "at": time.time()}
        except Exception:
            pass

        # Collateral in native token units, using the asset's REAL decimals.
        # Wormhole ETH and wBTC are BOTH 8 decimals — the old ETH branch used
        # 1e9, a silent 10x sizing error.
        if direction == "long":
            collateral_token_delta = _usd_to_token_units(
                collateral_usd, mark_price, accts["asset"])
        else:
            # SHORT: collateral is USDC (6 decimals), $1 == 1.0
            collateral_token_delta = _usd_to_usdc_units(collateral_usd)

        size_usd_delta = _price_to_jupiter_units(size_usd)
        slippage_bps = int(_perp_get_override("JUPITER_PERPS_MAX_SLIPPAGE_BPS",
                                                JUPITER_PERPS_MAX_SLIPPAGE_BPS))
        # priceSlippage off the MARKET'S OWN mark, not SOL's.
        slippage_price = _jupiter_slippage_price(mark_price, direction, slippage_bps)

        # ── SPRINT 56: ONE ENCODER. NO MANUAL FALLBACK. ──────────────────────
        # The old code had TWO encoders (anchorpy + hand-rolled) that disagreed
        # with each other AND with the chain. Worse, ANY exception in the
        # anchorpy builder silently set program=None and fell through to the
        # hand-rolled one — so a typo'd account key became a garbage tx on
        # mainnet. That fallthrough is why every fix moved the error sideways
        # (0x65 → 2006) instead of forward.
        #
        # anchorpy computes the discriminator from the IDL correctly by
        # construction (snake_case, which is what we could never get right by
        # hand) and Borsh-encodes params from the IDL's own type defs. It cannot
        # get either wrong. If it fails to build, we ABORT. We do not guess.
        program = await _get_jupiter_program(rpc_client)
        if program is None:
            msg = ("[s56][jupiter] ABORT open: anchorpy Program unavailable. Refusing to "
                   "hand-encode — the manual encoder is DELETED (it was the source of the "
                   "0x65 discriminator bug). Fix the IDL fetch, do not bypass it.")
            print(msg)
            _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                "ts": _s39_now_iso(), "venue": "jupiter", "event": "open_aborted",
                "reason": "anchorpy_program_unavailable", "detail": msg,
            })
            return None

        _increase_ix_name = _resolve_jupiter_increase_ix_name(_JUPITER_IDL_CACHED)
        if not _increase_ix_name:
            _names = _idl_instruction_names(_JUPITER_IDL_CACHED)
            print(f"[s56][jupiter] IDL INSTRUCTION NAMES ({len(_names)}): {_names}")
            msg = ("[s56][jupiter] ABORT open: could not resolve the increase-position "
                   "instruction name from the IDL. NOT submitting a guessed transaction.")
            print(msg)
            _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                "ts": _s39_now_iso(), "venue": "jupiter", "event": "open_aborted",
                "reason": "increase_ix_name_unresolved", "detail": msg,
            })
            return None

        instructions = []
        instructions.append(set_compute_unit_limit(JUPITER_PERPS_CU_LIMIT))
        instructions.append(set_compute_unit_price(JUPITER_PERPS_CU_PRICE_MICRO))

        # positionRequestAta — account #7. The ATA of the positionRequest PDA for
        # the input mint. This is where our collateral lands before the keeper
        # picks it up. We were NEVER sending this account.
        position_request_ata = _derive_associated_token_account(position_request_pda, input_mint)

        try:
            # ── anchorpy 0.21 MethodsBuilder API (verified against the installed lib) ──
            #   .args(List[Any])       <- POSITIONAL LIST, in IDL field order. Not a dict.
            #   .accounts(Dict)        <- dict, by account name
            #   .instruction()         <- SYNCHRONOUS. Not awaited.
            # The builder itself is NOT callable: `program.methods[name](...)` raises
            # TypeError: 'MethodsBuilder' object is not callable.
            #
            # Args order comes straight from CreateIncreasePositionMarketRequestParams:
            #   sizeUsdDelta, collateralTokenDelta, side, priceSlippage,
            #   jupiterMinimumOut, counter
            _Side = program.type["Side"]
            _side_variant = _Side.Long() if direction == "long" else _Side.Short()

            # ── anchorpy 0.21 MethodsBuilder — VERIFIED against mainnet ──────────
            # Proven by building the real ix off-chain before any tx was sent:
            #   16 account metas, 42 data bytes,
            #   discriminator b855c71869ab9c38
            #   == sha256("global:create_increase_position_market_request")[:8]
            #
            # Four API facts we got wrong, each one a separate failed run:
            #   1. The builder is NOT callable. program.methods[n](...) ->
            #      TypeError: 'MethodsBuilder' object is not callable.
            #   2. .args() takes a POSITIONAL LIST — and this ix takes exactly ONE
            #      arg: `params`. The six fields go INSIDE the struct, not flattened
            #      into the list. ("Expected ['params']".)
            #   3. .accounts() keys are snake_case, not the IDL's camelCase.
            #      ("funding_account not provided".)
            #   4. .instruction() is SYNCHRONOUS. Do not await it.
            #
            # And the optional `referral` account cannot be None — anchorpy 0.21
            # wants a real Pubkey. Anchor's convention for an unused optional
            # account is to pass the program's own ID.
            _Side   = program.type["Side"]
            _Params = program.type["CreateIncreasePositionMarketRequestParams"]

            _params = _Params(
                size_usd_delta         = size_usd_delta,
                collateral_token_delta = collateral_token_delta,
                side                   = _Side.Long() if direction == "long" else _Side.Short(),
                price_slippage         = slippage_price,
                jupiter_minimum_out    = None,
                # THE Custom(2006) FIX: `counter` seeds the position_request PDA.
                # We generated it, derived the PDA from it, and never sent it — so
                # the program re-derived a DIFFERENT PDA than the one we passed and
                # threw ConstraintSeeds. It must be the SAME u64 used above.
                counter                = counter,
            )

            ix = _resolve_program_method(program, _increase_ix_name).args(
                [_params]
            ).accounts({
                "owner":                    accts["owner"],
                "funding_account":          funding_ata,
                "perpetuals":               Pubkey.from_string(JUPITER_PERPS_PERPETUALS_AUTHORITY),
                "pool":                     accts["pool"],
                "position":                 accts["position"],
                "position_request":         position_request_pda,
                "position_request_ata":     position_request_ata,
                "custody":                  accts["custody"],
                "collateral_custody":       accts["collateral_custody"],
                "input_mint":               input_mint,
                # unused optional -> program ID (None raises: NoneType -> Pubkey)
                "referral":                 Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID),
                "token_program":            Pubkey.from_string(SOLANA_TOKEN_PROGRAM),
                "associated_token_program": Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROG),
                "system_program":           Pubkey.from_string(SOLANA_SYSTEM_PROGRAM),
                "event_authority":          _derive_event_authority(),
                "program":                  Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID),
            }).instruction()

            print(f"[s56][jupiter] ix built OK — {len(ix.accounts)} metas, "
                  f"{len(ix.data)} data bytes, disc={ix.data[:8].hex()}")
            instructions.append(ix)
        except Exception as e:
            # ABORT. Do NOT fall through to a hand-rolled encoder — that
            # fallthrough IS the bug we're removing. A build failure here means
            # our account names or param shape disagree with the IDL, and the
            # correct response is to fix them, not to submit something else.
            print(f"[s56][jupiter] ABORT open: anchorpy ix build failed: {e}")
            traceback.print_exc()
            _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                "ts": _s39_now_iso(), "venue": "jupiter", "event": "open_aborted",
                "reason": "anchorpy_ix_build_failed", "detail": str(e),
                "market": market, "direction": direction,
            })
            return None

        # Build + sign + send
        recent_blockhash_resp = await rpc_client.get_latest_blockhash(commitment=Confirmed)
        recent_blockhash = recent_blockhash_resp.value.blockhash
        message = Message.new_with_blockhash(instructions, keypair.pubkey(), recent_blockhash)
        tx = Transaction.new_unsigned(message)
        _dry_run_refuse("transaction")
        tx.sign([keypair], recent_blockhash)

        send_resp = await rpc_client.send_transaction(
            tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=3),
        )
        tx_sig = str(send_resp.value)

        # Wait for confirmation
        try:
            await rpc_client.confirm_transaction(send_resp.value, commitment=Confirmed)
        except Exception as e:
            print(f"[s39][jupiter] tx confirm err (may still execute): {e}")

        print(f"[s39][jupiter] PositionRequest submitted: {tx_sig[:32]}...")

        # Wait for keeper to execute
        executed = await _wait_for_keeper_execution(
            rpc_client, position_request_pda, JUPITER_KEEPER_TIMEOUT_S,
            position=accts["position"], expect_open=True,
        )

        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts":                _s39_now_iso(),
            "venue":             "jupiter",
            "operation":         "open_position",
            "market":            market,
            "direction":         direction,
            "size_usd":          size_usd,
            "leverage":          leverage,
            "collateral_usd":    collateral_usd,
            "tp_price":          tp_price,
            "sl_price":          sl_price,
            "tx_sig":            tx_sig,
            "position_pda":      str(accts["position"]),
            "request_pda":       str(position_request_pda),
            "keeper_executed":   executed,
            "success":           executed,
            "tp_sl_handling":    "bot_side_monitoring (Jupiter API gate prevents on-chain TPSL)",
        })

        _LAST_JUP_FILL = {
            "size_usd":       size_usd,
            "collateral_usd": collateral_usd,
            "fund_actions":   _fund_actions,
            "funding":        globals().get("_LAST_FUND_INFO"),
        }
        if executed:
            print(f"[s39][jupiter] keeper executed open: {tx_sig[:32]}...")
            return tx_sig
        else:
            print(f"[s39][jupiter] keeper timeout — request submitted but not executed in {JUPITER_KEEPER_TIMEOUT_S}s")
            # We still return tx_sig because the request is on-chain. The position
            # may execute moments later. Caller decides whether to retry.
            return tx_sig

    except Exception as e:
        err_str = str(e)
        try:
            globals()["_LAST_OPEN_ERR"] = err_str   # retry classifier reads this
        except Exception:
            pass
        print(f"[s39][jupiter] open_position err: {err_str}")
        traceback.print_exc()
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts":         _s39_now_iso(),
            "venue":      "jupiter",
            "operation":  "open_position",
            "market":     market,
            "direction":  direction,
            "size_usd":   size_usd,
            "success":    False,
            "error":      err_str[:500],
        })
        return None
    finally:
        try:
            await rpc_client.close()
        except Exception:
            pass


@_perp_inflight
async def jupiter_close_position(pos: dict, fraction: float = 1.0, withdraw_collateral_usd: float = 0.0) -> str | None:
    """
    Close a Jupiter Perps position via createDecreasePositionRequest2 with
    entirePosition=true.

    Returns: tx_sig string on success, None on failure.
    """
    market = pos.get("market", "SOL-PERP")
    direction = pos.get("direction", "long")
    if market not in ("SOL-PERP", "ETH-PERP", "BTC-PERP"):
        print(f"[s39][jupiter] close: market {market} not supported")
        return None

    if PERPS_DRY_RUN:
        return await _paper_close(pos, fraction)

    rpc_client = AsyncClient(RPC_URL, commitment=Confirmed)
    try:
        # ── 2026-07-29: LOAD LIVE CUSTODIES BEFORE BUILDING THE INSTRUCTION ──
        # A close is MORE important to get right than an open — refusing to guess.
        _pool_cfg = await _load_jupiter_pool_config(rpc_client)
        if not _pool_cfg:
            print("[s56b][custody] ABORT close: live pool config unavailable — refusing "
                  "to build a close instruction from the hardcoded custody map.")
            return None
        accts = _resolve_position_accounts(market, direction)
        if accts.get("_custody_source") != "live_pool_account":
            print(f"[s56b][custody] ABORT close: {market} custody came from "
                  f"{accts.get('_custody_source')} — not the live Pool account.")
            return None
        keypair = Keypair.from_base58_string(PRIVATE_KEY)

        counter = secrets.randbits(64)
        position_request_pda = _derive_position_request_pda(accts["position"], counter, JUPITER_REQUEST_CHANGE_DECREASE)

        # On close: receiving is the collateral mint (USDC for shorts, asset for longs)
        desired_mint = accts["collateral_mint"]
        receiving_ata = _derive_associated_token_account(accts["owner"], desired_mint)

        slippage_bps = int(_perp_get_override("JUPITER_PERPS_MAX_SLIPPAGE_BPS",
                                                JUPITER_PERPS_MAX_SLIPPAGE_BPS))

        program = await _get_jupiter_program(rpc_client)

        instructions = []
        instructions.append(set_compute_unit_limit(JUPITER_PERPS_CU_LIMIT))
        instructions.append(set_compute_unit_price(JUPITER_PERPS_CU_PRICE_MICRO))

        # ── SPRINT 56: CLOSE PATH, REWRITTEN TO MATCH THE PROVEN OPEN ────────
        # Two changes beyond the encoder surgery:
        #
        # 1. SWITCHED INSTRUCTION. The old code used createDecreasePositionRequest2,
        #    which needs EIGHTEEN accounts including custody_doves_price_account AND
        #    custody_pythnet_price_account — a second oracle this bot has never had
        #    mapped. Request2 is the TRIGGER/LIMIT path. We do MARKET closes. The
        #    right instruction is createDecreasePositionMarketRequest: 16 accounts,
        #    no oracle accounts, and params that mirror the open exactly.
        #
        # 2. Same anchorpy API rules proven on the open path:
        #      - builder is NOT callable; use .args([params])
        #      - ONE arg: the params struct. Fields go inside it, not flattened.
        #      - .accounts() keys are snake_case
        #      - .instruction() is synchronous
        #      - optional `referral` takes the program ID, not None
        #      - `counter` MUST be sent (it seeds position_request) or you get
        #        ConstraintSeeds / Custom(2006), exactly as on the open.
        #
        # The manual encoder is DELETED here too. It had the identical camelCase
        # discriminator bug and would have thrown 0x65 on the first close — you
        # just never reached it, because you could never open.
        if program is None:
            msg = ("[s56][jupiter] ABORT close: anchorpy Program unavailable. Refusing "
                   "to hand-encode.")
            print(msg)
            _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                "ts": _s39_now_iso(), "venue": "jupiter", "event": "close_aborted",
                "reason": "anchorpy_program_unavailable", "detail": msg,
            })
            return None

        position_request_ata = _derive_associated_token_account(position_request_pda, desired_mint)
        _entire = (fraction >= 0.999 and withdraw_collateral_usd <= 0)

        # priceSlippage off the market's OWN oracle. Closing a long = selling, so we
        # floor the min price we'll accept (mirror of the open's logic).
        _mark = await _jupiter_oracle_price(rpc_client, accts["asset"])
        if _mark is None or _mark <= 0:
            msg = (f"[s56][jupiter] ABORT close: no usable mark price for "
                   f"{accts['asset']}.")
            print(msg)
            _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                "ts": _s39_now_iso(), "venue": "jupiter", "event": "close_aborted",
                "reason": "no_mark_price", "market": market, "detail": msg,
            })
            return None
        # closing a long -> we are SELLING -> floor. closing a short -> BUYING -> cap.
        _close_dir = "short" if direction == "long" else "long"
        _slip = _jupiter_slippage_price(_mark, _close_dir, slippage_bps)

        try:
            _Params = program.type["CreateDecreasePositionMarketRequestParams"]
            _params = _Params(
                collateral_usd_delta = (_price_to_jupiter_units(withdraw_collateral_usd)
                                        if withdraw_collateral_usd > 0 else 0),
                size_usd_delta       = (_price_to_jupiter_units(pos.get("size_usd", 0) * fraction)
                                        if fraction < 0.999 else 0),
                price_slippage       = _slip,
                jupiter_minimum_out  = None,
                entire_position      = _entire,
                counter              = counter,
            )

            ix = _resolve_program_method(
                program, "createDecreasePositionMarketRequest"
            ).args([_params]).accounts({
                "owner":                    accts["owner"],
                "receiving_account":        receiving_ata,
                "perpetuals":               Pubkey.from_string(JUPITER_PERPS_PERPETUALS_AUTHORITY),
                "pool":                     accts["pool"],
                "position":                 accts["position"],
                "position_request":         position_request_pda,
                "position_request_ata":     position_request_ata,
                "custody":                  accts["custody"],
                "collateral_custody":       accts["collateral_custody"],
                "desired_mint":             desired_mint,
                "referral":                 Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID),
                "token_program":            Pubkey.from_string(SOLANA_TOKEN_PROGRAM),
                "associated_token_program": Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROG),
                "system_program":           Pubkey.from_string(SOLANA_SYSTEM_PROGRAM),
                "event_authority":          _derive_event_authority(),
                "program":                  Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID),
            }).instruction()

            print(f"[s56][jupiter] close ix built OK — {len(ix.accounts)} metas, "
                  f"{len(ix.data)} data bytes, disc={ix.data[:8].hex()}, "
                  f"entire={_entire}, mark=${_mark:,.4f}")
            instructions.append(ix)

        except Exception as e:
            print(f"[s56][jupiter] ABORT close: anchorpy ix build failed: {e}")
            traceback.print_exc()
            _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
                "ts": _s39_now_iso(), "venue": "jupiter", "event": "close_aborted",
                "reason": "anchorpy_ix_build_failed", "detail": str(e),
                "market": market,
            })
            return None

        recent_blockhash_resp = await rpc_client.get_latest_blockhash(commitment=Confirmed)
        recent_blockhash = recent_blockhash_resp.value.blockhash
        message = Message.new_with_blockhash(instructions, keypair.pubkey(), recent_blockhash)
        tx = Transaction.new_unsigned(message)
        _dry_run_refuse("transaction")
        tx.sign([keypair], recent_blockhash)

        send_resp = await rpc_client.send_transaction(
            tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=3),
        )
        tx_sig = str(send_resp.value)

        try:
            await rpc_client.confirm_transaction(send_resp.value, commitment=Confirmed)
        except Exception as e:
            print(f"[s39][jupiter] close confirm err: {e}")

        executed = await _wait_for_keeper_execution(
            rpc_client, position_request_pda, JUPITER_KEEPER_TIMEOUT_S,
            position=accts["position"], expect_open=False,
        )

        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts":              _s39_now_iso(),
            "venue":           "jupiter",
            "operation":       "close_position",
            "market":          market,
            "direction":       direction,
            "tx_sig":          tx_sig,
            "position_pda":    str(accts["position"]),
            "request_pda":     str(position_request_pda),
            "keeper_executed": executed,
            "success":         executed,
        })

        print(f"[s39][jupiter] close request submitted: {tx_sig[:32]}... executed={executed}")
        if tx_sig:
            try:
                await _sweep_wallet_to_sol(rpc_client, keypair)  # proceeds -> SOL, keep $5 USDC floor
            except Exception as _swe:
                print(f"[sweep] post-close sweep err: {_swe}")
        # 2026-08-08: the immediate sweep above runs the moment the close REQUEST
        # is submitted - before the keeper has filled it - so on a keeper-lagged
        # close it sweeps an empty wallet. Spawn the retrying router regardless of
        # tx_sig so a liquidation or force-settle (no tx of ours) is also covered.
        try:
            asyncio.create_task(_residual_route_to_sol(
                pos.get("market", "?"), reason=("post_close" if tx_sig else "no_tx_settle")))
        except Exception as _rre:
            print(f"[residual] spawn err: {_rre}")
        return tx_sig if (executed or tx_sig) else None

    except Exception as e:
        err_str = str(e)
        print(f"[s39][jupiter] close_position err: {err_str}")
        traceback.print_exc()
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts":         _s39_now_iso(),
            "venue":      "jupiter",
            "operation":  "close_position",
            "market":     market,
            "success":    False,
            "error":      err_str[:500],
        })
        return None
    finally:
        try:
            await rpc_client.close()
        except Exception:
            pass


@_perp_inflight
async def jupiter_adjust_position(
    market: str, new_tp_price: float | None, new_sl_price: float | None,
    add_size_usd: float | None, reduce_size_usd: float | None,
    sol_price: float | None,
) -> dict:
    """
    Adjust a Jupiter Perps position.

    Jupiter API gate: on-chain TP/SL update requires Jupiter's apiKeeper signer,
    which we don't have without API partnership. Workaround:
      - TP/SL changes: update positions.json (bot-side monitor handles execution)
      - add_size: do createIncreasePositionMarketRequest with sizeUsdDelta = add_size
      - reduce_size: do createDecreasePositionRequest2 with sizeUsdDelta = reduce_size
                     and entirePosition = false

    Returns: {"adjustments": [...], "errors": [...]}
    """
    adjustments = []
    errors = []

    # TP/SL: bot-side only (Jupiter API gate)
    if new_tp_price is not None or new_sl_price is not None:
        try:
            _am, existing = active_position()
            if existing and existing.get("venue") == "jupiter":
                if new_tp_price is not None:
                    existing["tp_price"] = float(new_tp_price)
                    adjustments.append(f"tp_price updated to ${new_tp_price:.2f} (bot-side monitoring)")
                if new_sl_price is not None:
                    existing["sl_price"] = float(new_sl_price)
                    adjustments.append(f"sl_price updated to ${new_sl_price:.2f} (bot-side monitoring)")
                save_position(existing)
            else:
                errors.append("no Jupiter position to update TP/SL on")
        except Exception as e:
            errors.append(f"tp/sl update err: {str(e)[:200]}")

    # Add size: open additional via createIncreasePositionMarketRequest
    if add_size_usd is not None and add_size_usd > 0:
        try:
            _am, existing = active_position()
            if not existing or existing.get("venue") != "jupiter":
                errors.append("no Jupiter position to add size to")
            else:
                direction = existing["direction"]
                # For adding size, no new collateral — leverage rises slightly
                # Caller can also pass add_collateral via separate adjust call
                price = sol_price or existing.get("entry_price")
                tx_sig = await jupiter_open_position(
                    direction=direction,
                    size_usd=add_size_usd,
                    leverage=existing.get("leverage", 1),
                    collateral_usd=0,  # 0 → no new collateral; just expand size
                    tp_price=None, sl_price=None,
                    sol_price=price, market=market,
                )
                if tx_sig:
                    adjustments.append(f"added ${add_size_usd:.2f} size; tx: {tx_sig[:20]}...")
                else:
                    errors.append("add_size submit failed")
        except Exception as e:
            errors.append(f"add_size err: {str(e)[:200]}")

    # Reduce size: createDecreasePositionRequest2 with entirePosition=false
    if PERPS_DRY_RUN and reduce_size_usd:
        errors.append("paper mode: reduce with a partial close (close_pct); adjust-reduce is not simulated")
        reduce_size_usd = None
    if reduce_size_usd is not None and reduce_size_usd > 0:
        try:
            _am, existing = active_position()
            if not existing or existing.get("venue") != "jupiter":
                errors.append("no Jupiter position to reduce size on")
            else:
                # Partial reduce — slightly different from close. We do a
                # createDecreasePositionRequest2 with entirePosition=false
                # and a specific sizeUsdDelta.
                rpc_client = AsyncClient(RPC_URL, commitment=Confirmed)
                try:
                    if not await _load_jupiter_pool_config(rpc_client):
                        raise RuntimeError("trading halted: live pool config unavailable, reduce refused")
                    accts = _resolve_position_accounts(market, existing["direction"])
                    if accts.get("_custody_source") != "live_pool_account":
                        raise RuntimeError(f"reduce refused: accounts from {accts.get('_custody_source')}")
                    keypair = Keypair.from_base58_string(PRIVATE_KEY)
                    counter = secrets.randbits(64)
                    position_request_pda = _derive_position_request_pda(accts["position"], counter, JUPITER_REQUEST_CHANGE_DECREASE)
                    receiving_ata = _derive_associated_token_account(accts["owner"], accts["collateral_mint"])

                    # ── SPRINT 56: third and last hand-rolled encoder, deleted ───
                    # Identical bug to the open and close: hardcoded camelCase name
                    # -> wrong discriminator -> 0x65. Plus 11 of 16 accounts and no
                    # `counter`. Switched to createDecreasePositionMarketRequest for
                    # the same reason as the close: Request2 is the trigger/limit
                    # path and needs two oracle accounts we never mapped.
                    program = await _get_jupiter_program(rpc_client)
                    if program is None:
                        errors.append("reduce_size: anchorpy Program unavailable — refusing to hand-encode")
                        raise RuntimeError("anchorpy unavailable")

                    position_request_ata = _derive_associated_token_account(
                        position_request_pda, accts["collateral_mint"])

                    _mark = await _jupiter_oracle_price(rpc_client, accts["asset"])
                    if _mark is None or _mark <= 0:
                        errors.append(f"reduce_size: no mark price for {accts['asset']}")
                        raise RuntimeError("no mark price")

                    _slip_bps = int(_perp_get_override("JUPITER_PERPS_MAX_SLIPPAGE_BPS",
                                                       JUPITER_PERPS_MAX_SLIPPAGE_BPS))
                    # reducing a long = selling -> floor; reducing a short = buying -> cap
                    _red_dir = "short" if existing["direction"] == "long" else "long"
                    _slip = _jupiter_slippage_price(_mark, _red_dir, _slip_bps)

                    _Params = program.type["CreateDecreasePositionMarketRequestParams"]
                    _params = _Params(
                        collateral_usd_delta = 0,
                        size_usd_delta       = _price_to_jupiter_units(reduce_size_usd),
                        price_slippage       = _slip,
                        jupiter_minimum_out  = None,
                        entire_position      = False,   # partial reduce
                        counter              = counter,
                    )

                    ix = _resolve_program_method(
                        program, "createDecreasePositionMarketRequest"
                    ).args([_params]).accounts({
                        "owner":                    accts["owner"],
                        "receiving_account":        receiving_ata,
                        "perpetuals":               Pubkey.from_string(JUPITER_PERPS_PERPETUALS_AUTHORITY),
                        "pool":                     accts["pool"],
                        "position":                 accts["position"],
                        "position_request":         position_request_pda,
                        "position_request_ata":     position_request_ata,
                        "custody":                  accts["custody"],
                        "collateral_custody":       accts["collateral_custody"],
                        "desired_mint":             accts["collateral_mint"],
                        "referral":                 Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID),
                        "token_program":            Pubkey.from_string(SOLANA_TOKEN_PROGRAM),
                        "associated_token_program": Pubkey.from_string(SOLANA_ASSOCIATED_TOKEN_PROG),
                        "system_program":           Pubkey.from_string(SOLANA_SYSTEM_PROGRAM),
                        "event_authority":          _derive_event_authority(),
                        "program":                  Pubkey.from_string(JUPITER_PERPS_PROGRAM_ID),
                    }).instruction()

                    print(f"[s56][jupiter] reduce ix built OK — {len(ix.accounts)} metas, "
                          f"{len(ix.data)} data bytes, disc={ix.data[:8].hex()}")
                    instructions = [
                        set_compute_unit_limit(JUPITER_PERPS_CU_LIMIT),
                        set_compute_unit_price(JUPITER_PERPS_CU_PRICE_MICRO),
                        ix,
                    ]
                    recent_blockhash = (await rpc_client.get_latest_blockhash(commitment=Confirmed)).value.blockhash
                    message = Message.new_with_blockhash(instructions, keypair.pubkey(), recent_blockhash)
                    tx = Transaction.new_unsigned(message)
                    _dry_run_refuse("transaction")
                    tx.sign([keypair], recent_blockhash)
                    send_resp = await rpc_client.send_transaction(
                        tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=3),
                    )
                    adjustments.append(f"reduced ${reduce_size_usd:.2f} size; tx: {str(send_resp.value)[:20]}...")
                finally:
                    try:
                        await rpc_client.close()
                    except Exception:
                        pass
        except Exception as e:
            errors.append(f"reduce_size err: {str(e)[:200]}")

    return {"adjustments": adjustments, "errors": errors}


# ─── Venue router ───────────────────────────────────────────────────────────

def _reverse_size_softwarn_perp(collateral_usd, size_usd):
    """Reverse-size SOFT WARN for perps (softwarn-B). Observe-only: warns when a NEW open
    goes bigger right after a loser (the over-engagement pattern). NEVER blocks or resizes;
    reads perp_pnl.json (own ledger) defensively; a thin book (n<5) is labelled thin_n, not
    treated as law. Fully isolated: any error returns None and cannot affect the trade."""
    try:
        _base = os.path.dirname(os.path.abspath(__file__))
        ppath = PNL_FILE if "PNL_FILE" in globals() else os.path.join(WS_BOT_LOGS, "perp_pnl.json")
        data = {}
        if os.path.exists(ppath):
            with open(ppath, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        trades = (data.get("trades") if isinstance(data, dict) else data) or []
        closed = [t for t in trades if isinstance(t, dict) and t.get("pnl_usd") is not None]
        if not closed:
            return None
        last_loser = (float(closed[-1].get("pnl_usd") or 0) <= 0)
        winners = [t for t in closed if (float(t.get("pnl_usd") or 0) > 0)]
        if not (last_loser and winners):
            return None
        lw = winners[-1]
        lw_collat = float(lw.get("collateral_usd") or lw.get("size_usd") or 0)
        lw_notional = float(lw.get("size_usd") or 0)
        if not ((collateral_usd > lw_collat > 0) or (size_usd > lw_notional > 0)):
            return None
        thin = " (thin_n)" if len(closed) < 5 else ""
        warn = (f"reverse_size_softwarn: NEW open collat ${collateral_usd:.2f}/notional "
                f"${size_usd:.2f} bigger than last winner (collat ${lw_collat:.2f}) right after "
                f"a loser{thin}. judgment kill still applies; not blocked.")
        _lp = os.path.join(WS_BOT_LOGS, "reverse_size_softwarn.jsonl")
        os.makedirs(os.path.dirname(_lp), exist_ok=True)
        with open(_lp, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(EST).isoformat(), "bot": "perps_bot",
                                "collateral_usd": collateral_usd, "notional_usd": size_usd,
                                "last_winner_collat": lw_collat, "last_was_loser": last_loser,
                                "n_closed": len(closed), "warn": warn}, default=str) + "\n")
        return warn
    except Exception:
        return None


async def route_open_position(
    direction: str, size_usd: float, leverage: float,
    collateral_usd: float, tp_price: float | None, sl_price: float | None,
    sol_price: float, market: str = "SOL-PERP",
) -> tuple[str | None, str]:
    """Open a position on Jupiter Perps. (Sprint 57: Drift removed — one venue.)

    Returns (tx_sig, venue_used). venue_used is "jupiter" on success, "none" on
    failure.

    What used to live here: a Drift-first / Jupiter-failover router with a
    both-venues-down refusal, health checks, and a "failover engaged" ping. There
    is no second venue now, so all of that is gone. The Jupiter per-market enable
    + leverage/size caps stay — they're real risk limits, not failover machinery.
    """
    if bool(_perp_get_override("PERP_OPEN_REQUIRE_MONITOR", PERP_OPEN_REQUIRE_MONITOR)) and not _perp_monitor_alive(
            _perp_get_override("PERP_MONITOR_STALE_S", PERP_MONITOR_STALE_S)):
        _age = time.time() - float(globals().get("_PERP_MONITOR_LAST_TICK") or 0.0)
        print(f"[perps guard] open REFUSED - monitor not ticking (last tick {_age:.0f}s ago); TP/SL would be unwatched")
        globals()["_LAST_OPEN_ERR"] = "monitor_not_running"
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts": _s39_now_iso(), "venue": "jupiter", "operation": "open_position_refused",
            "market": market, "reason": "monitor_not_running", "size_usd": size_usd,
        })
        try:
            await notify(f"\u26d4 <b>Open refused - {market}</b>\nThe position monitor is not running, "
                         f"so TP/SL would be unwatched. Nothing was sent.")
        except Exception:
            pass
        return None, "declined"
    if not await _load_jupiter_pool_config():
        print(f"[pool halt] open REFUSED - {market}: {perps_trading_halted()}")
        globals()["_LAST_OPEN_ERR"] = "trading_halted_pool_read"
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts": _s39_now_iso(), "venue": "jupiter", "operation": "open_position_refused",
            "market": market, "reason": "trading_halted_pool_read", "size_usd": size_usd,
        })
        await _pool_halt_alert(market)
        return None, "declined"
    jup_enabled = _perp_get_override("JUPITER_ENABLED_MARKETS", JUPITER_ENABLED_MARKETS) or JUPITER_ENABLED_MARKETS
    if market not in jup_enabled:
        print(f"[s57] open REFUSED — {market} not in JUPITER_ENABLED_MARKETS {jup_enabled}")
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts": _s39_now_iso(), "venue": "jupiter", "operation": "open_position_refused",
            "market": market, "reason": "market_not_enabled", "size_usd": size_usd,
        })
        return None, "none"

    # Risk caps: DECLINE, do not clamp. Derived from the user's settings (bots/.env):
    # leverage <= PERP_MAX_LEVERAGE (unset = 20x; never above the 250x venue max),
    # collateral <= PERP_MAX_COLLATERAL_SOL when set, and size <= that collateral x the
    # leverage cap. With PERP_MAX_COLLATERAL_SOL unset there is no extra collateral or
    # size cap beyond the wallet and the risk rules.
    # Regime scale stays 1.0 and is not used to silently resize the request.
    jup_max_lev  = float(min(float(_perp_get_override("PERP_MAX_LEVERAGE", PERP_MAX_LEVERAGE)),
                             JUPITER_VENUE_MAX_LEVERAGE))   # same cap the leverage resolver uses
    _env_lev = jup_max_lev
    _env_collat_sol = PERP_MAX_COLLATERAL_SOL
    _px = float(sol_price or 0)
    jup_max_size = (_px * _env_collat_sol * _env_lev) if (_px > 0 and _env_collat_sol) else None
    _collat_sol = (float(collateral_usd) / _px) if _px > 0 else None
    _over_lev = leverage > jup_max_lev
    _over_collat = (_env_collat_sol is not None and _collat_sol is not None
                    and _collat_sol > _env_collat_sol + 1e-6)
    _over_notional = jup_max_size is not None and size_usd > jup_max_size + 0.01
    if _over_lev or _over_collat or _over_notional:
        _why = []
        if _over_lev:
            _why.append(f"leverage {leverage:g}x exceeds the {jup_max_lev:g}x cap")
        if _over_collat:
            _why.append(f"collateral {_collat_sol:.4f} SOL exceeds the {_env_collat_sol:g} SOL cap")
        if _over_notional:
            _why.append(
                f"size ${size_usd:.2f} exceeds the ${jup_max_size:.2f} envelope "
                f"({_env_collat_sol:g} SOL x {_env_lev:g}x at ${_px:.2f})"
            )
        _reason = "; ".join(_why) + ". Not clamping. Re-request inside the cap or skip."
        print(f"[s57] open DECLINED — {_reason}")
        try:
            _mo = {
                "from": "perps_bot", "type": "minimum_opening_decline",
                "market": market, "action": direction,
                "requested": {"size_usd": size_usd, "leverage": leverage,
                              "collateral_usd": collateral_usd},
                "floor": {"max_leverage": jup_max_lev, "max_collateral_sol": _env_collat_sol,
                          "envelope_leverage": _env_lev, "max_position_usd": jup_max_size},
                "reason": _reason,
                "doctrine": ("NOT CLAMPED. What you request is what lands, so a request "
                             "over the cap is declined rather than quietly resized."),
                "ask": "re-request inside the cap, or skip",
                "generated_at": datetime.now(EST).isoformat(),
            }
            os.makedirs(INBOX_DIR, exist_ok=True)
            with open(os.path.join(
                    INBOX_DIR,
                    f"perps_bot - min_open_decline_{datetime.now(EST).strftime('%H%M%S')}.json"),
                    "w", encoding="utf-8") as _f:
                json.dump(_mo, _f, indent=2, default=str)
        except Exception as _e:
            print(f"[perp] cap decline packet err: {_e}")
        try:
            await notify("Signal declined — over cap\n" + _reason)
        except Exception:
            pass
        return None, "declined"

    try:
        tx_sig = await jupiter_open_position(
            direction=direction, size_usd=size_usd, leverage=leverage,
            collateral_usd=collateral_usd, tp_price=tp_price, sl_price=sl_price,
            sol_price=sol_price, market=market,
        )
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts": _s39_now_iso(), "venue": "jupiter", "operation": "open_position",
            "market": market, "direction": direction, "size_usd": size_usd,
            "leverage": leverage, "tx_sig": tx_sig, "success": tx_sig is not None,
        })
        return (tx_sig, "jupiter") if tx_sig else (None, "none")
    except Exception as e:
        err_str = str(e)
        print(f"[s57] jupiter open exception: {err_str[:200]}")
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts": _s39_now_iso(), "venue": "jupiter", "operation": "open_position",
            "market": market, "success": False, "error": err_str[:300],
        })
        return None, "none"


@_perp_inflight
async def route_adjust_position(data: dict, sol_price: float | None) -> dict:
    """Sprint 43: venue-aware position MODIFY. Routes an adjust (TP/SL change,
    add size, reduce size) to whichever venue OWNS the open position, read from
    positions.json `venue` tag.

    Before this fix, the adjust dispatch called Drift's adjust_position directly
    with no venue check — so jupiter_adjust_position was orphaned and Jupiter
    positions could be opened + closed but NEVER modified. This wires it.

    Returns the adjust result dict ({"adjustments": [...], "errors": [...]}) with
    a "venue" key added for the caller's notify.
    """
    _mkt, existing = active_position(data.get("market"))
    pos_venue  = (existing or {}).get("venue", "jupiter")  # Jupiter is the only venue
    market_str = (_mkt or data.get("market") or "SOL-PERP").upper()
    new_tp      = data.get("new_tp_price") or data.get("tp_price")
    new_sl      = data.get("new_sl_price") or data.get("sl_price")
    add_size    = data.get("add_size_usd")
    reduce_size = data.get("reduce_size_usd")

    _tp = float(new_tp) if new_tp is not None else None
    _sl = float(new_sl) if new_sl is not None else None
    _add = float(add_size) if add_size is not None else None
    _red = float(reduce_size) if reduce_size is not None else None

    # Sprint 57: Jupiter is the only venue — adjust always routes to Jupiter,
    # regardless of the position's stored `venue` tag (old positions may still
    # say "drift"; the Drift adjust path is gone).
    result = await jupiter_adjust_position(
        market=market_str, new_tp_price=_tp, new_sl_price=_sl,
        add_size_usd=_add, reduce_size_usd=_red, sol_price=sol_price,
    )
    result.setdefault("adjustments", [])
    result.setdefault("errors", [])
    result["venue"] = "jupiter"
    return result


@_perp_inflight
async def route_close_position(pos: dict, fraction: float = 1.0, withdraw_collateral_usd: float = 0.0) -> tuple[str | None, str]:
    """Close a position on Jupiter Perps. (Sprint 57: Drift removed — one venue.)

    Returns (tx_sig, venue_used).
    Old positions.json may still carry venue="drift" from before the teardown, but
    there is no Drift close path anymore — everything routes to Jupiter. Any truly
    Drift-held position from the old regime would have to be closed by hand on the
    Drift UI; the bot no longer speaks Drift.
    """
    try:
        tx_sig = await jupiter_close_position(pos, fraction=fraction, withdraw_collateral_usd=withdraw_collateral_usd)
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts": _s39_now_iso(), "venue": "jupiter", "operation": "close_position",
            "market": pos.get("market", "SOL-PERP"), "tx_sig": tx_sig,
            "success": tx_sig is not None,
        })
        return (tx_sig, "jupiter") if tx_sig else (None, "jupiter")
    except Exception as e:
        print(f"[s57] jupiter close exception: {str(e)[:200]}")
        _s39_append_jsonl(PERPS_VENUE_AUDIT_LOG, {
            "ts": _s39_now_iso(), "venue": "jupiter", "operation": "close_position",
            "market": pos.get("market", "SOL-PERP"), "success": False, "error": str(e)[:300],
        })
        return None, "jupiter"


@_perp_inflight
async def _execute_partial_close(pos, market, fraction, reason):
    """Sprint 48: close a FRACTION of a position (partial / pull-initial). Records
    the closed portion, updates the remaining size (or clears if ~full), and lets
    the venue close sweep proceeds back to SOL. Pure execution off the agent's call."""
    direction = pos["direction"]; entry = float(pos.get("entry_price") or 0)
    size_usd = float(pos.get("size_usd") or 0); lev = float(pos.get("leverage") or 1)
    # Sprint 68 — EXIT PRICE INTEGRITY. The old line fell back to ENTRY price when
    # the oracle read failed — recording a ~0% trade regardless of reality. Now:
    # read before, execute, read again AFTER keeper execution (that's the honest
    # exit mark), prefer post > pre; if BOTH fail, record an UNPRICED entry that
    # the ledger flags for manual pricing — a visible hole beats a fake zero.
    px_pre = await _market_mark_price(market)
    tx_sig, venue = await route_close_position(pos, fraction=fraction)
    if not tx_sig:
        await notify_error(f"partial close {market}", "route returned none")
        return
    px_post = await _market_mark_price(market)
    px = px_post or px_pre
    closed_size = size_usd * fraction
    if not px:
        try:
            data = {"trades": [], "total_realized_usd": 0, "total_fees_usd": 0}
            if os.path.exists(PNL_FILE):
                with open(PNL_FILE) as f:
                    data = json.load(f)
            data = _migrate_pnl_ledger_v2(data)
            data.setdefault("unpriced", []).append({
                "timestamp": datetime.now(EST).isoformat(), "market": market,
                "direction": direction, "entry_price": entry, "size_usd": closed_size,
                "leverage": lev, "reason": reason, "tx_sig": tx_sig,
                "note": "oracle unavailable at close — needs manual exit price",
            })
            with open(PNL_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as _ue:
            print(f"[unpriced close] record err: {_ue}")
        await notify(f"⚠️ {market} partial close EXECUTED (tx {tx_sig[:16]}…) but oracle "
                     f"unpriced — trade logged as UNPRICED for manual entry. No fake P&L recorded.")
        net_pnl, pnl_pct = None, None
    else:
        net_pnl, pnl_pct = record_pnl(direction, entry, px, closed_size, lev, reason, market=market, phase=pos.get("phase"), micro_lean=pos.get("micro_lean", False), position=pos)
    try:  # polish-2026-10-15 (item 9): every close lands in perps.log
        log(f"close {direction} {fraction*100:.0f}%", direction, closed_size,
            px if px else "unpriced", reason, tx_sig)
    except Exception:
        pass
    if fraction >= 0.999:
        clear_position(market, side=direction)
        _remaining = 0.0
    else:
        pos["size_usd"] = round(size_usd * (1 - fraction), 4)
        if pos.get("base_amount"):
            pos["base_amount"] = pos["base_amount"] * (1 - fraction)
        save_position(pos, market=market)
        _remaining = pos["size_usd"]
    _px_str  = f"${px:.4f}" if px else "UNPRICED"
    _net_str = f"${net_pnl:+.2f}" if net_pnl is not None else "pending manual price"
    await notify(
        f"✂️ <b>{market} {reason} — {venue}</b>\n"
        f"Closed {fraction*100:.0f}% @ {_px_str} | Net: {_net_str}\n"
        f"Remaining size: ${_remaining:.2f}\n"
        f"Proceeds swept to SOL (kept $5 USDC floor)."
    )


# ─── Decline feedback loop (parallel to Sprint 37/38) ────────────────────────

def load_decline_log_recent_perps(window_hours: float = 24) -> list:
    """Load skip log entries from last N hours."""
    if not os.path.exists(PERPS_DECLINE_LOG_FILE):
        return []
    cutoff = _s39_now_ts() - (window_hours * 3600)
    entries = []
    try:
        with open(PERPS_DECLINE_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("declined_at_ts", 0) >= cutoff:
                    entries.append(r)
    except Exception as e:
        print(f"[s39] decline_log read err: {e}")
    return entries


def ingest_skip_perps(decision: dict) -> tuple:
    """
    Process a structured skip from the agent.

    Required fields:
      action == "skip"
      packet_id
      market: SOL-PERP | BTC-PERP | ETH-PERP
      mode: directional | funding_arb | squeeze_setup | stand_down
      decline_reason: from PERPS_VALID_SKIP_REASONS enum
      would_have_traded_if: str (can be empty)
      filter_adjustment_suggested: {param, delta} dict OR null

    Returns (ok: bool, error_msg: str | None).
    """
    if not isinstance(decision, dict):
        return False, "decision not a dict"

    pid = decision.get("packet_id", "")
    if not pid:
        return False, "skip decision missing packet_id"

    decline_reason = decision.get("decline_reason")
    if not decline_reason or decline_reason not in PERPS_VALID_SKIP_REASONS:
        return False, (
            f"decline_reason '{decline_reason}' invalid. Must be one of: "
            f"{sorted(PERPS_VALID_SKIP_REASONS)}. See perps-boss SKILL.md "
            f"'Valid skip reasons' section."
        )

    market = decision.get("market", "SOL-PERP")
    if market not in PERPS_COVERED_MARKETS:
        return False, f"market '{market}' not in {PERPS_COVERED_MARKETS}"

    mode = decision.get("mode", "directional")
    if mode not in PERPS_COVERED_MODES:
        return False, f"mode '{mode}' not in {PERPS_COVERED_MODES}"

    # would_have_traded_if field is required to exist (can be empty string)
    if "would_have_traded_if" not in decision:
        return False, "skip requires would_have_traded_if field (can be empty string)"

    entry = {
        "declined_at":               _s39_now_iso(),
        "declined_at_ts":            _s39_now_ts(),
        "packet_id":                 pid,
        "market":                    market,
        "mode":                      mode,
        "decline_reason":            decline_reason,
        "would_have_traded_if":      decision.get("would_have_traded_if", ""),
        "filter_adjustment_suggested": decision.get("filter_adjustment_suggested"),
        "is_filter_related":         decline_reason in PERPS_FILTER_RELATED_SKIP_REASONS,
        "is_calibration_justified":  decline_reason in PERPS_CALIBRATION_SKIP_REASONS,
    }
    _s39_append_jsonl(PERPS_DECLINE_LOG_FILE, entry)
    print(
        f"[s39] skip ingested: pid={pid[:32]} market={market} mode={mode} "
        f"reason={decline_reason} filter_related={entry['is_filter_related']}"
    )
    return True, None


def ingest_confirm_signal_perps(decision: dict) -> tuple:
    """
    Process an agent endorsement when he WOULD trade but is blocked by hard gates.

    Required fields:
      action == "confirm_signal"
      packet_id
      market: from PERPS_COVERED_MARKETS
      would_trade_direction: "long" | "short"
      blocked_by: "balance_low" | "authority_threshold" | "daily_read_block" |
                  "venue_down" | "drawdown_halt" | "other:<freeform>"
      size_intended_usd: float | null
      leverage_intended: float | null
      note: str (optional)
    """
    if not isinstance(decision, dict):
        return False, "decision not a dict"

    pid = decision.get("packet_id", "")
    if not pid:
        return False, "confirm_signal missing packet_id"

    direction = decision.get("would_trade_direction")
    if direction not in ("long", "short"):
        return False, "confirm_signal requires would_trade_direction: 'long' or 'short'"

    blocked_by = decision.get("blocked_by")
    if not blocked_by:
        return False, (
            "confirm_signal requires blocked_by: balance_low | "
            "authority_threshold | daily_read_block | venue_down | "
            "drawdown_halt | other:<freeform>"
        )

    market = decision.get("market", "SOL-PERP")

    entry = {
        "confirmed_at":          _s39_now_iso(),
        "confirmed_at_ts":       _s39_now_ts(),
        "packet_id":             pid,
        "market":                market,
        "would_trade_direction": direction,
        "blocked_by":            blocked_by,
        "size_intended_usd":     decision.get("size_intended_usd"),
        "leverage_intended":     decision.get("leverage_intended"),
        "note":                  decision.get("note", ""),
    }
    _s39_append_jsonl(PERPS_CONFIRM_SIGNAL_LOG_FILE, entry)
    print(
        f"[s39] confirm_signal ingested: pid={pid[:32]} market={market} "
        f"dir={direction} blocked_by={blocked_by}"
    )
    return True, None


def compute_decline_patterns_perps(window_hours: float = 7 * 24) -> dict:
    """
    Roll up perps skip log into a dashboard segmented by market AND mode AND reason.
    Written every 30min to PERPS_DECLINE_PATTERNS_FILE.
    """
    entries = load_decline_log_recent_perps(window_hours=window_hours)

    if not entries:
        return {
            "computed_at":     _s39_now_iso(),
            "window_hours":    window_hours,
            "total_declines":  0,
            "by_market":       {},
            "by_mode":         {},
            "by_reason":       {},
            "by_market_mode":  {},
            "filter_related_count":        0,
            "calibration_justified_count": 0,
            "top_markets":     [],
            "top_modes":       [],
            "top_reasons":     [],
            "note":            "no skips in window",
        }

    by_market: dict = {}
    by_mode: dict = {}
    by_reason: dict = {}
    by_market_mode: dict = {}
    filter_related_count = 0
    calibration_justified_count = 0

    for e in entries:
        market = e.get("market", "unknown")
        mode = e.get("mode", "unknown")
        reason = e.get("decline_reason", "unspecified")
        market_mode_key = f"{market}::{mode}"

        bm = by_market.setdefault(market, {"count": 0, "by_reason": {},
                                            "filter_related": 0, "calibration_justified": 0})
        bm["count"] += 1
        bm["by_reason"][reason] = bm["by_reason"].get(reason, 0) + 1
        if e.get("is_filter_related"):
            bm["filter_related"] += 1
            filter_related_count += 1
        if e.get("is_calibration_justified"):
            bm["calibration_justified"] += 1
            calibration_justified_count += 1

        bmd = by_mode.setdefault(mode, {"count": 0})
        bmd["count"] += 1

        by_reason[reason] = by_reason.get(reason, 0) + 1

        bmm = by_market_mode.setdefault(market_mode_key, {"count": 0, "filter_related": 0})
        bmm["count"] += 1
        if e.get("is_filter_related"):
            bmm["filter_related"] += 1

    top_markets = sorted(
        [{"market": k, **v} for k, v in by_market.items()],
        key=lambda x: x["count"], reverse=True,
    )[:8]
    top_modes = sorted(
        [{"mode": k, "count": v["count"]} for k, v in by_mode.items()],
        key=lambda x: x["count"], reverse=True,
    )[:8]
    top_reasons = sorted(
        [{"reason": k, "count": v} for k, v in by_reason.items()],
        key=lambda x: x["count"], reverse=True,
    )[:8]
    top_market_modes = sorted(
        [{"market_mode": k, **v} for k, v in by_market_mode.items()],
        key=lambda x: x["count"], reverse=True,
    )[:8]

    return {
        "computed_at":                 _s39_now_iso(),
        "window_hours":                window_hours,
        "total_declines":              len(entries),
        "filter_related_count":        filter_related_count,
        "calibration_justified_count": calibration_justified_count,
        "by_market":                   by_market,
        "by_mode":                     by_mode,
        "by_reason":                   by_reason,
        "by_market_mode":              by_market_mode,
        "top_markets":                 top_markets,
        "top_modes":                   top_modes,
        "top_reasons":                 top_reasons,
        "top_market_modes":            top_market_modes,
    }


def _count_filter_related_strikes_perps(window_hours: float) -> dict:
    """
    Count filter-related (NOT calibration-justified) skips, grouped by decline_reason.
    Used by evaluate_auto_tighten_perps.
    """
    entries = load_decline_log_recent_perps(window_hours=window_hours)
    by_reason: dict = {}
    for e in entries:
        if not e.get("is_filter_related"):
            continue
        if e.get("is_calibration_justified"):
            continue
        reason = e.get("decline_reason", "unknown")
        by_reason.setdefault(reason, []).append(e)
    return by_reason


def _count_filter_related_strikes_per_market_perps(window_hours: float) -> dict:
    """
    For per-market auto-disable: count filter-related skips by market.
    """
    entries = load_decline_log_recent_perps(window_hours=window_hours)
    by_market: dict = {}
    for e in entries:
        if not e.get("is_filter_related"):
            continue
        if e.get("is_calibration_justified"):
            continue
        market = e.get("market", "unknown")
        if market not in PERPS_COVERED_MARKETS:
            continue
        by_market.setdefault(market, []).append(e)
    return by_market


async def evaluate_auto_tighten_perps() -> dict:
    """
    Check if any single decline_reason crossed the strike threshold within
    the window. If yes AND that reason maps to tunable params, write a
    perp_meeting.json directive programmatically.

    Returns audit entry if fired, else {}.
    """
    # Don't compete with an existing pending meeting
    if os.path.exists(PERP_MEETING_FILE):
        return {}

    by_reason = _count_filter_related_strikes_perps(
        window_hours=PERPS_AUTO_TIGHTEN_STRIKE_WINDOW_H
    )

    for reason, strike_entries in by_reason.items():
        if len(strike_entries) < PERPS_AUTO_TIGHTEN_STRIKE_THRESHOLD:
            continue
        param_map = PERPS_AUTO_TIGHTEN_PARAM_MAP.get(reason)
        if not param_map:
            continue  # reason doesn't have a clean param mapping; skip

        # Check 24h cooldown (per reason)
        try:
            if os.path.exists(PERPS_AUTO_TIGHTEN_AUDIT_LOG):
                with open(PERPS_AUTO_TIGHTEN_AUDIT_LOG, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if lines:
                    last = json.loads(lines[-1].strip())
                    if (last.get("decline_reason") == reason and
                        (_s39_now_ts() - last.get("triggered_at_ts", 0)) < PERPS_AUTO_TIGHTEN_COOLDOWN_H * 3600):
                        continue
        except Exception:
            pass

        # Build directive describing both param changes
        adjustments = []
        for param, delta in param_map:
            current = _perp_get_override(param, None)
            if current is None:
                # Fall back to default from TUNABLE_PARAMS
                spec = TUNABLE_PARAMS.get(param, {})
                current = spec.get("default")
                if current is None:
                    continue
            new_val = current + delta
            spec = TUNABLE_PARAMS.get(param, {})
            param_min = spec.get("min")
            param_max = spec.get("max")
            if param_min is not None and new_val < param_min:
                new_val = param_min
            if param_max is not None and new_val > param_max:
                new_val = param_max
            adjustments.append({"param": param, "from": current, "to": new_val, "delta": delta})

        if not adjustments:
            continue

        adj_summary = ", ".join(f"{a['param']} from {a['from']} to {a['to']}" for a in adjustments)
        directive_text = (
            f"Auto-tighten: {len(strike_entries)} skips with reason '{reason}' "
            f"in last {PERPS_AUTO_TIGHTEN_STRIKE_WINDOW_H}h. "
            f"Adjusting: {adj_summary}. "
            f"The owner can roll back via meeting. 24h cooldown applies."
        )

        meeting_note = {
            "note_id":     f"auto_tighten_perps_{reason}_{int(_s39_now_ts())}",
            "author":      "perps_bot_auto_tighten",
            "created_at":  _s39_now_iso(),
            "status":      "pending",
            "directive":   directive_text,
            "source":      "sprint39_auto_tighten",
            "adjustments_hint": adjustments,
        }

        try:
            os.makedirs(os.path.dirname(PERP_MEETING_FILE), exist_ok=True)
            with open(PERP_MEETING_FILE, "w") as f:
                json.dump(meeting_note, f, indent=2)
            audit_entry = {
                "triggered_at":     _s39_now_iso(),
                "triggered_at_ts":  _s39_now_ts(),
                "decline_reason":   reason,
                "strike_count":     len(strike_entries),
                "window_hours":     PERPS_AUTO_TIGHTEN_STRIKE_WINDOW_H,
                "adjustments":      adjustments,
                "directive":        directive_text,
            }
            _s39_append_jsonl(PERPS_AUTO_TIGHTEN_AUDIT_LOG, audit_entry)
            print(
                f"[s39] auto_tighten wrote meeting directive for reason='{reason}' "
                f"after {len(strike_entries)} strikes"
            )
            try:
                await notify(
                    f"⚙️ <b>PERPS AUTO-TIGHTEN</b>\n"
                    f"Reason: {reason} ({len(strike_entries)} skips in "
                    f"{PERPS_AUTO_TIGHTEN_STRIKE_WINDOW_H}h)\n"
                    f"Adjustments: {len(adjustments)} params"
                )
            except Exception:
                pass
            return audit_entry
        except Exception as e:
            print(f"[s39] auto_tighten meeting write err: {e}")
    return {}


async def evaluate_per_market_auto_disable() -> dict:
    """
    Check if any single market crossed the strike threshold AND has skips
    concentrated by filter-related reasons. If yes, write a perp_meeting.json
    directive to add that market to MARKET_BLOCKLIST.

    Returns audit entry if fired, else {}.
    """
    if not bool(_perp_get_override("PER_MARKET_AUTO_DISABLE_ENABLED", True)):
        return {}
    if os.path.exists(PERP_MEETING_FILE):
        return {}

    by_market = _count_filter_related_strikes_per_market_perps(
        window_hours=PERPS_AUTO_DISABLE_WINDOW_H
    )

    current_blocklist = _perp_get_override("MARKET_BLOCKLIST", []) or []
    current_blocklist_set = set(current_blocklist) if isinstance(current_blocklist, list) else set()

    for market, strike_entries in by_market.items():
        if market in current_blocklist_set:
            continue
        if len(strike_entries) < PERPS_AUTO_DISABLE_STRIKE_THRESHOLD:
            continue

        # 24h cooldown
        try:
            if os.path.exists(PERPS_AUTO_TIGHTEN_AUDIT_LOG):
                with open(PERPS_AUTO_TIGHTEN_AUDIT_LOG, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if lines:
                    last = json.loads(lines[-1].strip())
                    if (last.get("market_disabled") == market and
                        (_s39_now_ts() - last.get("triggered_at_ts", 0)) < PERPS_AUTO_DISABLE_COOLDOWN_H * 3600):
                        continue
        except Exception:
            pass

        proposed = sorted(set(current_blocklist) | {market})
        directive_text = (
            f"Per-market auto-disable: market '{market}' has {len(strike_entries)} "
            f"filter-related skips in {PERPS_AUTO_DISABLE_WINDOW_H}h. "
            f"Add to MARKET_BLOCKLIST. New blocklist: {proposed}. "
            f"The owner can roll back via meeting. 24h cooldown applies."
        )

        meeting_note = {
            "note_id":     f"auto_disable_market_{market}_{int(_s39_now_ts())}",
            "author":      "perps_bot_per_market_auto_disable",
            "created_at":  _s39_now_iso(),
            "status":      "pending",
            "directive":   directive_text,
            "source":      "sprint39_per_market_disable",
            "param_hint":  "MARKET_BLOCKLIST",
            "value_hint":  proposed,
        }

        try:
            with open(PERP_MEETING_FILE, "w") as f:
                json.dump(meeting_note, f, indent=2)
            audit_entry = {
                "triggered_at":     _s39_now_iso(),
                "triggered_at_ts":  _s39_now_ts(),
                "market_disabled":  market,
                "strike_count":     len(strike_entries),
                "window_hours":     PERPS_AUTO_DISABLE_WINDOW_H,
                "old_blocklist":    list(current_blocklist),
                "new_blocklist":    proposed,
                "directive":        directive_text,
            }
            _s39_append_jsonl(PERPS_AUTO_TIGHTEN_AUDIT_LOG, audit_entry)
            print(f"[s39] per_market_auto_disable wrote meeting directive for '{market}'")
            try:
                await notify(
                    f"⚙️ <b>PERPS AUTO-DISABLE</b>\n"
                    f"Market: {market} ({len(strike_entries)} filter-skips in "
                    f"{PERPS_AUTO_DISABLE_WINDOW_H}h)\n"
                    f"New blocklist: {proposed}"
                )
            except Exception:
                pass
            # Fire the throughput question packet
            try:
                await _write_perps_throughput_question_packet(
                    market=market,
                    strike_count=len(strike_entries),
                    strike_reasons=[e.get("decline_reason") for e in strike_entries],
                )
            except Exception as e:
                print(f"[s39] throughput question write err: {e}")
            return audit_entry
        except Exception as e:
            print(f"[s39] per_market_auto_disable meeting write err: {e}")
    return {}


async def _write_perps_throughput_question_packet(
    market: str, strike_count: int, strike_reasons: list,
) -> None:
    """Fire a packet asking the agent to confirm/reject the per-market auto-disable."""
    ts = int(_s39_now_ts())
    packet_id = f"perps_throughput_question_{market}_{ts}"

    reason_counts: dict = {}
    for r in strike_reasons:
        reason_counts[r] = reason_counts.get(r, 0) + 1
    reasons_summary = ", ".join(f"{k}={v}" for k, v in reason_counts.items())

    packet = {
        "id":          packet_id,
        "type":        "perps_throughput_question",
        "received_at": _s39_now_iso(),
        "ttl_hours":   24,
        "from_user": {
            "username":        "@perps_bot",
            "friendship_tier": "system",
            "user_id":         "perps_bot",
        },
        "context": {
            "market":                 market,
            "strike_count":           strike_count,
            "strike_window_hours":    PERPS_AUTO_DISABLE_WINDOW_H,
            "reasons_summary":        reasons_summary,
            "reason_breakdown":       reason_counts,
            "auto_action_taken":      f"added '{market}' to MARKET_BLOCKLIST via meeting",
            "question": (
                f"Perps bot auto-disabled '{market}' after {strike_count} filter-related "
                f"skips in {PERPS_AUTO_DISABLE_WINDOW_H}h (reasons: {reasons_summary})."
            ),
            "action_required": (
                "The agent: respond with one of:\n"
                f"  • 'market dead, keep disabled' → no change, keep '{market}' off scan\n"
                "  • 'filters wrong, tighten X' → write a perp_meeting.json directive\n"
                f"  • 're-enable, was a streak' → write a meeting to pop '{market}' from MARKET_BLOCKLIST\n"
                f"24h cooldown applies."
            ),
            "tier": "question",
        },
    }
    try:
        # Sprint 43: filename follows <bot_name> - <subject>_<ts> so the agent's
        # inbox-check picks it up by bot owner. (The X-event-shaped body fields
        # are vestigial — agent_listener only globs x_evt_*.json, never this.)
        fname = f"perps_bot - throughput_question_{market}_{ts}.json"
        packet_path = os.path.join(INBOX_DIR, fname)
        with open(packet_path, "w") as f:
            json.dump(packet, f, indent=2)
        print(f"[s39] throughput question packet written: {fname}")
    except Exception as e:
        print(f"[s39] throughput question packet write err: {e}")


async def perps_decline_patterns_writer_loop() -> None:
    """Refresh perps_decline_patterns.json every 30min."""
    while True:
        try:
            patterns = compute_decline_patterns_perps(window_hours=7 * 24)
            os.makedirs(os.path.dirname(PERPS_DECLINE_PATTERNS_FILE), exist_ok=True)
            with open(PERPS_DECLINE_PATTERNS_FILE, "w") as f:
                json.dump(patterns, f, indent=2, default=str)
        except Exception as e:
            print(f"[s39] decline_patterns writer err: {e}")
        await asyncio.sleep(PERPS_DECLINE_PATTERNS_REFRESH_S)


async def perps_auto_tighten_loop() -> None:
    """Every 30min, evaluate auto-tighten AND per-market auto-disable."""
    while True:
        try:
            await evaluate_auto_tighten_perps()
        except Exception as e:
            print(f"[s39] auto_tighten eval err: {e}")
        try:
            await evaluate_per_market_auto_disable()
        except Exception as e:
            print(f"[s39] per_market_auto_disable err: {e}")
        await asyncio.sleep(PERPS_AUTO_TIGHTEN_TICK_S)


# ═══════════════════════════════════════════════════════════════════════════
# END SPRINT 39 MODULE
# ═══════════════════════════════════════════════════════════════════════════


async def reconcile_jupiter_positions() -> dict:
    if PERPS_DRY_RUN:
        return {}  # paper book is local; the chain is not the source of truth here
    """Sprint 57: the Jupiter replacement for the old Drift reconcile/scan.

    For every covered market, read the REAL on-chain Position size off the Jupiter
    Position PDA and compare it to local perp_positions.json. The chain is the
    source of truth (this is the same principle that killed the 108-minute ghost):
    if local says a market is open but the chain says size 0, the local record is
    a phantom and gets cleared.

    Returns {market: {local, chain, action}} so the scan action can report it.
    We do NOT auto-adopt a chain position that local doesn't know about — a Jupiter
    position the bot didn't open (via UI) has no entry/tp/sl/venue metadata to
    manage safely; we flag it for the owner instead of inventing a record.
    """
    report = {}
    local = load_positions() or {}
    # 2026-10-16 (PERP_ONE_PER_MARKET=false): the second, opposite-side position
    # on a market is keyed MARKET#SIDE. Check its own PDA; flat chain = ghost.
    for _sk, _sp in list(local.items()):
        if "#" not in _sk:
            continue
        try:
            _acc = _resolve_position_accounts(_pos_base(_sk), _sp.get("direction"))
            _su = await _read_position_size_usd(None, _acc["position"])
        except Exception as _e:
            report[_sk] = {"local_open": True, "chain": "read_error", "action": "skipped", "error": str(_e)[:120]}
            continue
        if _su is not None and _su <= 0:
            try:
                record_unverified_close(_pos_base(_sk), _sp, "chain flat while book open (reconcile, second side)",
                                        category="closed_offbook", mark_at_detection=None)
            except Exception as _le:
                print(f"[s57 reconcile] {_sk} ledger-before-clear err: {_le!r}")
            clear_position(_sk)
            report[_sk] = {"local_open": True, "chain": "flat", "action": "cleared_local_ghost"}
            print(f"[s57 reconcile] {_sk}: local said open, chain is flat — cleared ghost")
    for market in PERP_MARKETS_COVERED:
        lpos = local.get(market)
        local_open = bool(lpos and lpos.get("direction"))
        try:
            direction = (lpos or {}).get("direction", "long")
            accts = _resolve_position_accounts(market, direction)
            size_usd_units = await _read_position_size_usd(None, accts["position"])
        except Exception as e:
            report[market] = {"local_open": local_open, "chain": "read_error", "action": "skipped", "error": str(e)[:120]}
            continue
        if size_usd_units is None:
            report[market] = {"local_open": local_open, "chain": "unknown", "action": "skipped"}
            continue
        chain_open = size_usd_units > 0
        if local_open and not chain_open:
            # polish-2026-10-15 (item 1): the chain closed it (liquidation, keeper
            # TP/SL, or a close while we were down). Book it BEFORE clearing.
            try:
                _mk = None
                try:
                    _mk = await _market_mark_price(market)
                except Exception:
                    _mk = None
                _liq = lpos.get("liq_price")
                _cat = "closed_offbook"
                try:
                    if _mk and _liq:
                        _d = (lpos.get("direction") or "long")
                        if (_d == "long" and float(_mk) <= float(_liq)) or (_d == "short" and float(_mk) >= float(_liq)):
                            _cat = "liquidated_suspected"
                except Exception:
                    pass
                record_unverified_close(market, lpos,
                                        "chain flat while book open (reconcile)",
                                        category=_cat, mark_at_detection=_mk)
                try:
                    await notify(f"\U0001f4d2 <b>{market} closed off-book</b>\n"
                                 f"Chain shows flat; booked to the ledger as {_cat} with exit "
                                 f"UNVERIFIED (mark at detection ${_mk if _mk else '?'}).")
                except Exception:
                    pass
            except Exception as _le:
                print(f"[s57 reconcile] ledger-before-clear err: {_le!r}")
            clear_position(market)
            report[market] = {"local_open": True, "chain": "flat", "action": "cleared_local_ghost"}
            print(f"[s57 reconcile] {market}: local said open, chain is flat — cleared ghost")
        elif chain_open and not local_open:
            # 2026-07-24 (phantom-close incident): before flagging, check the
            # clear archive — a chain position whose full metadata we archived
            # at clear time is a WRONG CLEAR, and wrong clears are reversible.
            _restored = False
            try:
                _rows = []
                if os.path.exists(PERP_POS_ARCHIVE):
                    with open(PERP_POS_ARCHIVE, encoding="utf-8") as _af:
                        for _ln in _af:
                            try:
                                _r = json.loads(_ln)
                                if _r.get("_market") == market and _r.get("direction"):
                                    _rows.append(_r)
                            except Exception:
                                continue
                if _rows:
                    _rec = dict(_rows[-1])
                    for _k in ("_archived_at", "_market", "_why"):
                        _rec.pop(_k, None)
                    save_position(_rec, market=market)
                    _restored = True
                    report[market] = {"local_open": False, "chain": f"open size_units={size_usd_units}",
                                       "action": "restored_from_archive"}
                    print(f"[s57 reconcile] {market}: chain open, book flat — RESTORED from "
                          f"clear-archive (phantom close reversed; entry ${_rec.get('entry_price')})")
                    try:
                        await notify(f"♻️ <b>POSITION RESTORED — {market}</b>\n"
                                      f"Chain shows it open; the book had a phantom clear. Full "
                                      f"metadata restored from the clear archive; monitor resumes.")
                    except Exception:
                        pass
            except Exception as _re_e:
                print(f"[s57 reconcile] restore err: {_re_e!r}")
            if _restored:
                continue
            report[market] = {"local_open": False, "chain": f"open size_units={size_usd_units}",
                              "action": "flagged_untracked"}
            print(f"[s57 reconcile] {market}: chain has a position local doesn't track — flagged")
            try:
                if await _owner_pos_perps_adopt(market, direction):
                    report[market]["action"] = "owner_position_pending"
            except Exception as _ope:
                print(f"[owner_pos] adopt err {market}: {_ope!r}")
        elif chain_open and local_open:
            # Sprint 68 — SIZE, not just existence. Partial closes / adjusts can drift
            # the local size from the chain's; a wrong local size mis-prices every
            # subsequent P&L, alert, and partial-close fraction. Jupiter stores
            # size_usd with 6 decimals (USDC scale). >5% divergence → adopt the
            # chain's number (chain is truth), scale base_amount proportionally.
            chain_size_usd = size_usd_units / 1e6
            local_size_usd = float(lpos.get("size_usd") or 0)
            if local_size_usd > 0 and abs(chain_size_usd - local_size_usd) / local_size_usd > 0.05:
                _scale = chain_size_usd / local_size_usd
                lpos["size_usd"] = round(chain_size_usd, 4)
                if lpos.get("base_amount"):
                    lpos["base_amount"] = lpos["base_amount"] * _scale
                save_position(lpos, market=market)
                report[market] = {"local_open": True, "chain": "open",
                                  "action": "size_synced_from_chain",
                                  "local_size_usd": round(local_size_usd, 2),
                                  "chain_size_usd": round(chain_size_usd, 2)}
                print(f"[s68 reconcile] {market}: size drift local ${local_size_usd:.2f} vs chain ${chain_size_usd:.2f} — adopted chain")
                # polish-2026-10-15 (item 2): a silent adopt hid the Oct 9 fill
                # ($100@10x requested, $686@25x on chain). Say it out loud.
                try:
                    await notify(f"\u26a0\ufe0f <b>FILL MISMATCH — {market}</b>\n"
                                 f"Book said ${local_size_usd:.2f}, chain says ${chain_size_usd:.2f} notional. "
                                 f"Book now follows the chain. Check leverage and collateral on Jupiter.")
                except Exception:
                    pass
            else:
                report[market] = {"local_open": True, "chain": "open", "action": "in_sync",
                                  "chain_size_usd": round(chain_size_usd, 2)}
        else:
            report[market] = {"local_open": local_open, "chain": "open" if chain_open else "flat",
                              "action": "in_sync"}
    return report



def _perps_order_flag_true(data: dict) -> bool:
    """True only when the signal explicitly marks itself as an order.
    Missing or false is not an order. The flag name is perps_is_order."""
    v = data.get("perps_is_order", None)
    if v is True:
        return True
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v == 1
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return False


_PERP_EXPLICIT_CLOSE_ACTIONS = {
    "close", "close_position", "exit", "close_long", "close_short",
    "adjust", "modify", "update", "edit",
    "add", "add_size", "scale_in", "increase", "add_long", "add_short",
    "close_pct", "partial_close", "close_partial", "scale_out",
    "pull_initial", "pull_init", "bank_initial", "recover_initial",
}
_PERP_ADD_ACTIONS = {"add", "add_size", "scale_in", "increase", "add_long", "add_short"}
_PERP_OPEN_ACTIONS = {
    "long", "short", "open_long", "buy_long", "open_short", "buy_short",
    "limit_long", "limit_short", "open",
}


async def execute_position_add(data: dict) -> None:
    """Add collateral and size onto a live same-side position.

    Two shapes:
      - target_collateral_sol + target_leverage: delta until the book equals that
      - collateral (SOL if <5) + leverage: add that slice on top

    A second long/short is still refused. This is the only stack lane.
    Same caps as an open: leverage <= PERP_MAX_LEVERAGE (never above the venue max)
    and, when set, collateral <= PERP_MAX_COLLATERAL_SOL with size <= that collateral
    x the leverage cap. Requests over the caps are declined, not clamped.
    """
    market = (data.get("market") or "SOL-PERP").upper()
    existing = load_position(market)
    if not existing or not existing.get("direction"):
        await notify(
            f"🚫 <b>Add refused</b>\nNo open {market} to add to. This lane stacks. It does not open."
        )
        return
    direction = str(existing.get("direction") or "").lower()
    side = str(data.get("side") or "").lower().strip()
    if side in ("long", "short") and side != direction:
        await notify(
            f"🚫 <b>Add refused</b>\n{market} is {direction}. An add cannot flip it. Close first."
        )
        return
    sol_price = await get_sol_price()
    if not sol_price:
        await notify_error("position add", "could not get SOL price")
        return

    old_size = float(existing.get("size_usd") or 0)
    old_col = float(existing.get("collateral") or 0)
    old_entry = float(existing.get("entry_price") or sol_price)
    old_col_sol = (old_col / sol_price) if sol_price else 0.0
    reason = data.get("reason") or "owner add"
    mode = "slice"
    # Caps from the user's settings (bots/.env), same as route_open_position.
    _cap_lev = float(min(float(_perp_get_override("PERP_MAX_LEVERAGE", PERP_MAX_LEVERAGE)),
                         JUPITER_VENUE_MAX_LEVERAGE))
    _cap_sol = PERP_MAX_COLLATERAL_SOL  # None = no extra collateral cap beyond the wallet

    target_sol = data.get("target_collateral_sol")
    target_lev = data.get("target_leverage")
    if target_sol not in (None, "", 0) and target_lev not in (None, "", 0):
        mode = "equate"
        target_sol = float(target_sol)
        target_lev = float(target_lev)
        if target_lev <= 0 or target_lev > _cap_lev + 1e-6:
            await notify(
                f"🚫 <b>Add declined</b>\n"
                f"target leverage {target_lev:g}x is outside 1x-{_cap_lev:g}x (PERP_MAX_LEVERAGE). Not clamping."
            )
            return
        if _cap_sol is not None and target_sol > _cap_sol + 1e-6:
            await notify(
                f"🚫 <b>Add declined</b>\n"
                f"target collateral {target_sol:.4f} SOL is over the {_cap_sol:g} SOL cap (PERP_MAX_COLLATERAL_SOL)."
            )
            return
        if target_sol <= old_col_sol + 1e-4:
            await notify(
                f"🚫 <b>Add declined</b>\n"
                f"{market} collateral is already {old_col_sol:.4f} SOL. "
                f"Target {target_sol:.4f} SOL is not an add."
            )
            return
        target_col = target_sol * sol_price
        target_size = target_col * target_lev
        if target_size <= old_size + 0.5:
            await notify(
                f"🚫 <b>Add declined</b>\n"
                f"Target notional ${target_size:.2f} does not clear current ${old_size:.2f}."
            )
            return
        add_col = target_col - old_col
        add_size = target_size - old_size
        add_lev = add_size / add_col if add_col > 0 else 0.0
    else:
        raw = float(data.get("add_collateral_sol") or data.get("collateral") or data.get("col") or 0)
        add_lev = float(data.get("leverage") or data.get("lev") or 0)
        if raw <= 0 or add_lev <= 0:
            await notify(
                "🚫 <b>Add declined</b>\n"
                "Need collateral + leverage, or target_collateral_sol + target_leverage."
            )
            return
        add_col = raw * sol_price if raw < 5 else raw
        add_size = add_col * add_lev

    if add_col < float(MIN_COLLATERAL_USD):
        await notify(
            f"🚫 <b>Add declined</b>\n"
            f"Add collateral ${add_col:.2f} is under the ${MIN_COLLATERAL_USD:.0f} venue floor."
        )
        return
    if add_lev > _cap_lev + 1e-6 or add_lev < float(PERP_MIN_LEVERAGE):
        await notify(
            f"🚫 <b>Add declined</b>\n"
            f"Add slice {add_lev:.2f}x is outside {PERP_MIN_LEVERAGE:g}x-{_cap_lev:g}x. Not clamping.\n"
            f"Book is {old_col_sol:.4f} SOL / ${old_size:.2f}. Re-request a slice inside the band."
        )
        return
    new_col_sol = (old_col + add_col) / sol_price
    if _cap_sol is not None and new_col_sol > _cap_sol + 1e-6:
        await notify(
            f"🚫 <b>Add declined</b>\n"
            f"Combined collateral {new_col_sol:.4f} SOL exceeds the {_cap_sol:g} SOL cap (PERP_MAX_COLLATERAL_SOL)."
        )
        return
    env_max = (sol_price * _cap_sol * _cap_lev) if _cap_sol is not None else None
    if env_max is not None and old_size + add_size > env_max + 0.01:
        await notify(
            f"🚫 <b>Add declined</b>\n"
            f"Combined size ${old_size + add_size:.2f} exceeds the ${env_max:.2f} envelope "
            f"({_cap_sol:g} SOL x {_cap_lev:g}x)."
        )
        return

    try:
        _gap_min = float(_perp_get_override("PERP_ADD_MIN_SL_LIQ_GAP_PCT", PERP_ADD_MIN_SL_LIQ_GAP_PCT) or 0)
        _sl = float(existing.get("sl_price") or 0)
        if _gap_min > 0 and _sl > 0:
            _mk = await _market_mark_price(market) if market != "SOL-PERP" else sol_price
            _base = (old_size / old_entry if old_entry else 0) + (add_size / _mk if _mk else 0)
            _avg = (old_size + add_size) / _base if _base else old_entry
            _liq = _perp_est_liq(direction, _avg, old_size + add_size, old_col + add_col)
            _gap = _perp_sl_liq_gap_pct(direction, _sl, _liq)
            if _gap is not None and _gap < _gap_min:
                print(f"[perps guard] add REFUSED - est liq {_liq:.4f} only {_gap:.2f}% from SL {_sl}")
                await notify(
                    f"\u26d4 <b>Add declined - {market}</b>\n"
                    f"After this add, est. liquidation ~{_liq:.4f} would sit {_gap:.2f}% from the stop {_sl:g} "
                    f"(minimum {_gap_min:g}%). A stop that close can be skipped straight into liquidation. "
                    f"Use less leverage, or move the stop first.")
                return
    except Exception as _ge:
        print(f"[perps guard] add gap check err {_ge!r} (not blocking)")
    print(f"[perp add] {market} {direction} mode={mode} add ${add_size:.2f} "
          f"collat ${add_col:.2f} {add_lev:.2f}x at ${sol_price:.2f}")
    tx_sig, venue_used = await route_open_position(
        direction=direction,
        size_usd=add_size,
        leverage=add_lev,
        collateral_usd=add_col,
        tp_price=None,
        sl_price=None,
        sol_price=sol_price,
        market=market,
    )
    if venue_used == "declined" or not tx_sig:
        await notify(
            f"🚫 <b>Add failed</b>\n{market} {direction} add did not land. Book unchanged."
        )
        return

    filled_size = add_size
    filled_col = add_col
    if _LAST_JUP_FILL:
        filled_size = float(_LAST_JUP_FILL.get("size_usd") or add_size)
        filled_col = float(_LAST_JUP_FILL.get("collateral_usd") or add_col)
    short_fill = filled_col + 0.5 < add_col
    new_size = old_size + filled_size
    new_col = old_col + filled_col
    new_lev = (new_size / new_col) if new_col else add_lev
    new_entry = ((old_size * old_entry) + (filled_size * sol_price)) / new_size if new_size else sol_price
    _vl = await fetch_venue_liq_price(market, direction)
    if _vl:
        liq, src = _vl["liq"], _vl["source"]
    else:
        liq, src = calc_liq_price(new_entry, direction, new_lev), "estimate_blended"
    existing.update({
        "entry_price": new_entry,
        "size_usd": round(new_size, 4),
        "leverage": round(new_lev, 4),
        "collateral": round(new_col, 4),
        "base_amount": (new_size / new_entry) if new_entry else existing.get("base_amount"),
        "liq_price": liq,
        "liq_price_source": src,
        "liq_exit_floor": calc_exit_floor(liq, direction, entry=new_entry, tp=existing.get("tp_price"), sl=existing.get("sl_price")),
        "breakeven": calc_breakeven_price(new_entry, direction, new_size, new_size / new_entry if new_entry else 0),
        "last_add_tx": tx_sig,
        "last_add_at": datetime.now(EST).isoformat(),
        "last_add": {
            "mode": mode,
            "add_size_usd": round(filled_size, 4),
            "add_collateral_usd": round(filled_col, 4),
            "add_leverage": round(add_lev, 4),
            "reason": reason,
        },
    })
    save_position(existing, market)
    note = ""
    if short_fill:
        note = "\nFill was short of the request. Book shows what landed, not the target."
    await notify(
        f"➕ <b>Added to {market} {direction.upper()}</b>\n"
        f"Slice: ${filled_size:.2f} at {add_lev:.1f}x, collateral ${filled_col:.2f}\n"
        f"Book: ${new_size:.2f} / ${new_col:.2f} collat / {new_lev:.1f}x\n"
        f"Blended entry ${new_entry:.2f} | liq ${float(liq):.2f}\n"
        f"tx: <code>{tx_sig}</code>{note}"
    )


# ── process signal ─────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════
# polish-2026-10-15 helpers: update_params, param-change log, decline log,
# committed-call candidates, mechanical-call retirement flag.
# ════════════════════════════════════════════════════════════════════════════
PERP_PARAM_CHANGE_LOG = os.path.join(WS_BOT_LOGS, "perps_param_changes.jsonl")
PERP_COMMITTED_DIVERGENCE_LOG = os.path.join(WS_BOT_LOGS, "perps_committed_divergence.jsonl")
# item 10: mechanical coin calls timed out ~98% of the time (161 of 164 graded).
# FLAGGED, not deleted: they still reach the packet, marked retired, and are
# never offered as a perps candidate. Flip to False to bring them back.
PERP_MECHANICAL_CALLS_RETIRED = True
_PERP_UPDATABLE_PARAMS = {"tp_price", "tp_pct", "take_profit_pct", "sl_price", "sl_pct",
                          "stop_loss_pct", "trail_activate_pct", "trail_drop_pct",
                          "exit_floor", "exit_floor_price", "liq_exit_floor"}
_PERP_FORBIDDEN_PARAMS = {"size_usd", "size", "size_sol", "leverage", "lev", "collateral",
                          "collateral_usd", "collateral_sol", "target_leverage",
                          "target_collateral_sol", "direction", "side"}


def _perp_mark_alert_sent(market_key, pos: dict, kind: str):
    """Persist a sent warning on the position so a reboot never repeats it."""
    try:
        pos.setdefault("alerts_sent", {})[kind] = datetime.now(EST).isoformat()
        save_position(pos, market=market_key)
    except Exception as _e:
        print(f"[perp monitor] alert flag save err: {_e}")


def _perp_log_param_change(market, key, old, new, reason, source):
    try:
        with open(PERP_PARAM_CHANGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(EST).isoformat(), "market": market,
                                "param": key, "old": old, "new": new,
                                "reason": reason, "source": source}, default=str) + "\n")
    except Exception as e:
        print(f"[params] change-log err: {e}")


async def execute_update_params_perps(data: dict):
    """Perps update_params. Changes SL, TP, trail and the exit
    floor on an OPEN position. Never size, leverage, collateral or direction, and
    never opens or closes anything. Every change is logged with its reason and
    announced as "Params updated"."""
    market = (data.get("market") or "SOL-PERP").upper()
    _uside = str(data.get("side") or data.get("position_side") or "").lower()
    _uside = _uside if _uside in ("long", "short") else None
    if not _uside and len(_positions_for_market(market)) > 1:
        await notify(f"\U0001f6ab <b>update_params rejected — {market}</b>\nLong and short are both open. "
                     f"Add side: long or short."); return
    pos = load_position(market, side=_uside)
    if not pos or not pos.get("direction"):
        await notify(f"⚠️ update_params: no open {market} {_uside or ''} position".replace("  ", " ")); return
    params = data.get("params") if isinstance(data.get("params"), dict) else {
        k: v for k, v in data.items() if k in _PERP_UPDATABLE_PARAMS or k in _PERP_FORBIDDEN_PARAMS}
    bad = sorted(k for k in params if k in _PERP_FORBIDDEN_PARAMS)
    if bad:
        await notify(f"🚫 <b>update_params rejected — {market}</b>\n"
                     f"{', '.join(bad)} cannot change here. update_params moves SL, TP, trail and "
                     f"the exit floor only. Size and leverage go through add/adjust.")
        return
    params = {k: v for k, v in params.items() if k in _PERP_UPDATABLE_PARAMS}
    if not params:
        await notify(f"⚠️ update_params: nothing to update for {market}"); return
    reason = (data.get("reason") or data.get("reasoning") or "").strip() or "no reason given"
    direction = pos["direction"]
    entry = float(pos.get("entry_price") or 0)
    new = {}
    try:
        if "tp_price" in params:
            new["tp_price"] = float(params["tp_price"])
        elif params.get("tp_pct") is not None or params.get("take_profit_pct") is not None:
            _p = abs(float(params.get("tp_pct", params.get("take_profit_pct"))))
            new["tp_price"] = round(entry * (1 + _p / 100) if direction == "long" else entry * (1 - _p / 100), 4)
        if "sl_price" in params:
            new["sl_price"] = float(params["sl_price"])
        elif params.get("sl_pct") is not None or params.get("stop_loss_pct") is not None:
            _p = abs(float(params.get("sl_pct", params.get("stop_loss_pct"))))
            new["sl_price"] = round(entry * (1 - _p / 100) if direction == "long" else entry * (1 + _p / 100), 4)
        if params.get("trail_activate_pct") is not None:
            new["trail_activate_pct"] = abs(float(params["trail_activate_pct"]))
        if params.get("trail_drop_pct") is not None:
            new["trail_drop_pct"] = min(50.0, max(3.0, abs(float(params["trail_drop_pct"]))))
        _fl = params.get("exit_floor", params.get("exit_floor_price", params.get("liq_exit_floor")))
        if _fl is not None:
            new["exit_floor_user"] = float(_fl)
    except (TypeError, ValueError) as e:
        await notify(f"⚠️ update_params: bad number ({e})"); return
    # sanity: TP/SL on the right side of the mark, floor clamped
    errs = []
    if "tp_price" in new and entry and ((direction == "long" and new["tp_price"] <= entry) or
                                        (direction == "short" and new["tp_price"] >= entry)):
        errs.append(f"TP ${new['tp_price']} is on the wrong side of entry ${entry}")
    _liq = pos.get("liq_price")
    if "sl_price" in new and _liq and ((direction == "long" and new["sl_price"] <= float(_liq)) or
                                       (direction == "short" and new["sl_price"] >= float(_liq))):
        errs.append(f"SL ${new['sl_price']} is past liquidation ${_liq}")
    if errs:
        await notify("🚫 <b>update_params rejected — " + market + "</b>\n" + "\n".join(errs)); return
    if "exit_floor_user" in new and _liq:
        _c = clamp_exit_floor(new["exit_floor_user"], _liq, direction, entry=entry,
                              tp=new.get("tp_price", pos.get("tp_price")))
        if _c is not None and abs(_c - new["exit_floor_user"]) > 1e-9:
            reason += f" (floor clamped {new['exit_floor_user']} -> {_c})"
        new["exit_floor_user"] = _c
        new["liq_exit_floor"] = _c
    # 2026-10-16: a TP/SL change keeps the exit floor beyond the new SL. A floor
    # the signal set itself is kept (re-clamped); otherwise it is recomputed.
    if ("tp_price" in new or "sl_price" in new) and "exit_floor_user" not in new and _liq:
        try:
            _tp_n = new.get("tp_price", pos.get("tp_price"))
            _sl_n = new.get("sl_price", pos.get("sl_price"))
            if pos.get("exit_floor_user") is not None:
                _nf = clamp_exit_floor(float(pos["exit_floor_user"]), _liq, direction, entry=entry, tp=_tp_n)
            else:
                _nf = calc_exit_floor(float(_liq), direction, entry=entry,
                                      tp=(float(_tp_n) if _tp_n else None), sl=(float(_sl_n) if _sl_n else None))
            if _nf is not None:
                new["liq_exit_floor"] = _nf
        except Exception as _fe:
            print(f"[params] floor recompute err: {_fe}")
    if not pos.get("liq_exit_floor") and "liq_exit_floor" not in new and _liq:
        try:
            new["liq_exit_floor"] = calc_exit_floor(float(_liq), direction, entry=entry,
                                                    tp=new.get("tp_price", pos.get("tp_price")),
                                                    sl=new.get("sl_price", pos.get("sl_price")))
        except Exception:
            pass
    transitions = []
    src = pos.setdefault("params_source", {})
    for k, v in new.items():
        old = pos.get(k)
        if old != v:
            transitions.append(f"{k}: {old} -> {v}")
            _perp_log_param_change(market, k, old, v, reason, "update_params")
        pos[k] = v
        src[k] = "signal"
    pos["params_rev"] = int(pos.get("params_rev") or 0) + 1
    save_position(pos, market=market)
    _perp_set_outcome("processed", market=_pos_base(market), side=direction, tp=pos.get("tp_price"),
                      sl=pos.get("sl_price"), exit_floor=pos.get("liq_exit_floor"),
                      trail_activate_pct=pos.get("trail_activate_pct"), trail_drop_pct=pos.get("trail_drop_pct"),
                      changes=transitions, params_rev=pos["params_rev"], liq=pos.get("liq_price"))
    if transitions:
        await notify(f"🔧 <b>Params updated: {market} {direction.upper()}</b>\n" +
                     "\n".join(" " + t for t in transitions[:6]) + f"\nReason: {reason[:200]}")
    else:
        await notify(f"🔧 Params updated: {market} (no changes, values matched)")


def _perp_decline_log_bot(market, action, why, kind):
    """item 8: bot-side declines (minimum opening, daily-read block) also land in
    perps_decline_log.jsonl so the decline record is complete."""
    _perp_set_outcome("declined", reason=f"{kind}: {why}", market=market)
    try:
        _s39_append_jsonl(PERPS_DECLINE_LOG_FILE, {
            "declined_at": _s39_now_iso(), "declined_at_ts": _s39_now_ts(),
            "packet_id": None, "market": market, "mode": "directional",
            "decline_reason": kind, "source": "bot_gate", "action": action,
            "detail": str(why)[:400], "would_have_traded_if": "",
            "filter_adjustment_suggested": None,
            "is_filter_related": False, "is_calibration_justified": False})
    except Exception as e:
        print(f"[decline_log] bot write err: {e}")


def _perp_decline_log_implicit(snap: dict, verdict: str):
    """item 8: a packet that fired with no trade is an implicit pass. When the
    counterfactual resolves it, write it to the decline log."""
    if verdict not in ("missed", "correct_pass", "inconclusive"):
        return
    try:
        _s39_append_jsonl(PERPS_DECLINE_LOG_FILE, {
            "declined_at": snap.get("ts"), "declined_at_ts": _s39_now_ts(),
            "packet_id": snap.get("packet_id"), "market": snap.get("market"),
            "mode": "directional", "decline_reason": "implicit_pass",
            "source": "autopsy_resolution", "verdict": verdict,
            "coin_call": snap.get("coin_call"), "would_have_traded_if": "",
            "filter_adjustment_suggested": None,
            "is_filter_related": False, "is_calibration_justified": False})
    except Exception as e:
        print(f"[decline_log] implicit write err: {e}")


def _committed_calls_source() -> dict:
    """item 4 STUB: committed directional calls by symbol, shaped
    {"SOL": {"direction": "SHORT", "entry": .., "tp": .., "sl": ..,
             "source": "committed", "opened_at": ..}}.
    Not wired in this build. A scraper for the public call feed plugs in here;
    until then there are no candidates and nothing changes."""
    return {}


def committed_call_candidates() -> list:
    """item 4: committed calls (shorts included) surfaced as perps CANDIDATES.
    Candidates only: nothing opens from this, the signal still decides."""
    out = []
    for sym, c in (_committed_calls_source() or {}).items():
        src = (c.get("source") or "").lower()
        if "committed" not in src:
            continue
        d = (c.get("direction") or "").lower()
        if sym.upper() not in ("SOL", "BTC", "ETH") or d not in ("long", "short"):
            continue
        out.append({"market": f"{sym.upper()}-PERP", "direction": d,
                    "entry": c.get("entry"), "tp": c.get("tp"), "sl": c.get("sl"),
                    "opened_at": c.get("opened_at"), "source": c.get("source"),
                    "note": "candidate only; the signal decides, nothing auto-opens"})
    return out


def check_committed_divergence(market: str, action: str, context: str = "open"):
    """item 4: log every time perps goes against a committed call. Returns the
    conflicting call or None. Logging only; never blocks."""
    try:
        for c in committed_call_candidates():
            if c["market"] == market.upper() and c["direction"] != action:
                with open(PERP_COMMITTED_DIVERGENCE_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": datetime.now(EST).isoformat(), "market": market,
                                        "perps_action": action, "committed": c,
                                        "context": context}, default=str) + "\n")
                return c
    except Exception as e:
        print(f"[committed] divergence log err: {e}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# OWNER POSITIONS (2026-10-15)
# A Jupiter position (or an add) the bot did not place through its own order
# path is an OWNER POSITION. On sight it is tagged, booked with source unknown,
# and NOT managed: alerts are limited to the liquidation warning. The bot asks:
# manage it or leave it to the owner?
#   manage -> normal management: auto TP/SL from calc_auto_tp_sl, trail, exit
#             floor. Exits stay ALERT-ONLY, like every perps position: the bot
#             alerts at TP / trail / floor / SL and a signal closes.
#   leave  -> liquidation warning only; a signal close needs owner_explicit_close
#   no answer in OWNER_POS_TIMEOUT_MIN, or price inside OWNER_POS_LIQ_EARLY_FRAC
#   of the entry-to-liquidation distance -> manage, announced.
# Size and leverage are never changed; nothing is opened.
# ════════════════════════════════════════════════════════════════════════════
OWNER_POS_ENABLED        = _envv("OWNER_POS_ENABLED", "true").lower() in ("true", "1", "yes")
OWNER_POS_TIMEOUT_MIN    = float(_envv("OWNER_POS_TIMEOUT_MIN", "60"))
OWNER_POS_LIQ_EARLY_FRAC = float(_envv("OWNER_POS_LIQ_EARLY_FRAC", "0.15"))
OWNER_POS_SCAN_S         = float(_envv("OWNER_POS_SCAN_S", "300"))
OWNER_POS_LOG_FILE       = os.path.join(WS_BOT_LOGS, "owner_positions.jsonl")
OWNER_POS_STATE_FILE     = os.path.join(WS_BOT_LOGS, "owner_positions_perps.json")
OWNER_POS_DECISIONS_FILE = os.getenv("OWNER_POS_DECISIONS_FILE") or os.path.join(
    _BOT_HOME, "bot_logs", "owner_position_decisions.jsonl")
_OWNER_POS_SEEN = {}   # market/direction -> consecutive untracked sightings


def _owner_pos_log(event: str, market: str, rec: dict, **extra):
    try:
        with open(OWNER_POS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(EST).isoformat(), "bot": "perps", "event": event,
                                "market": market, "id": rec.get("id"), "kind": rec.get("kind"),
                                "state": rec.get("state"), **extra}, default=str) + "\n")
    except Exception as e:
        print(f"[owner_pos] log err: {e}")


def _owner_pos_publish():
    """Mirror pending/left owner positions to a small state file for owner commands."""
    try:
        out = {}
        for m, p in (load_positions() or {}).items():
            o = (p or {}).get("owner_position")
            if o:
                out[m] = {**o, "market": m, "direction": p.get("direction"), "size_usd": p.get("size_usd")}
        with open(OWNER_POS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"records": out, "updated_at": datetime.now(EST).isoformat()}, f, indent=2, default=str)
    except Exception:
        pass


async def _owner_pos_ask(market: str, pos: dict, kind: str):
    o = pos["owner_position"]
    sym = market.replace("-PERP", "")
    what = (f"${float(pos.get('size_usd') or 0):,.0f} {sym} {pos.get('direction','?')} position"
            if kind == "position" else
            f"${float(o.get('added_usd') or 0):,.0f} add on {sym} {pos.get('direction','?')}")
    msg = f"I see a {what} I didn't open. Manage it or leave it to you?"
    try:
        pkt = {"from": "perps_bot", "type": "owner_position", "id": o["id"], "market": market,
               "kind": kind, "direction": pos.get("direction"), "size_usd": pos.get("size_usd"),
               "leverage": pos.get("leverage"), "entry_price": pos.get("entry_price"),
               "liq_price": pos.get("liq_price"), "source": o.get("source"), "state": "pending",
               "question": msg,
               "reply": {"relay": {"action": "owner_position", "id": o["id"], "decision": "manage | leave"},
                         "owner_command": f"/manage {sym}  or  /leave {sym}"},
               "timeout": (f"No answer in {OWNER_POS_TIMEOUT_MIN:g} min, or price within "
                           f"{OWNER_POS_LIQ_EARLY_FRAC*100:g}% of the entry-to-liquidation distance "
                           f"from liquidation -> the bot manages it."),
               "manage_means": "auto TP/SL, trail and exit floor; exits are alerts, a signal closes",
               "leave_means": "liquidation warning only",
               "generated_at": datetime.now(EST).isoformat()}
        with open(os.path.join(INBOX_DIR, f"perps_bot - owner_position_{sym}_{o['id'][-6:]}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(pkt, f, indent=2, default=str)
    except Exception as e:
        print(f"[owner_pos] packet err: {e}")
    try:
        await notify(f"👤 <b>Owner position: {market}</b>\n{msg}\nReply /manage {sym} or /leave {sym}. "
                     f"Auto-manage in {OWNER_POS_TIMEOUT_MIN:g} min if no answer (sooner near liquidation).")
    except Exception:
        pass


async def _owner_pos_perps_adopt(market: str, direction: str) -> bool:
    """Book an untracked on-chain position as a pending owner position."""
    if not OWNER_POS_ENABLED:
        return False
    cur = load_position(market)
    if cur and cur.get("direction"):
        return False
    vr = await fetch_venue_liq_price(market, direction)
    if not vr:
        return False
    entry, size, coll = float(vr["entry"]), float(vr["size_usd"]), float(vr["collateral_usd"])
    lev = float(vr.get("leverage") or (size / coll if coll else 0))
    now = datetime.now(EST)
    rec = {"id": f"op_{market.split('-')[0].lower()}_{int(now.timestamp())}", "kind": "position",
           "state": "pending", "source": "unknown", "tag": "owner_position",
           "detected_at": now.isoformat(), "detected_ts": now.timestamp(),
           "timeout_min": OWNER_POS_TIMEOUT_MIN}
    pos = {"direction": direction, "entry_price": entry, "size_usd": size, "leverage": round(lev, 2),
           "collateral": coll, "base_amount": size / entry if entry else 0,
           "tp_price": None, "sl_price": None,
           "breakeven": calc_breakeven_price(entry, direction, size, size / entry if entry else 0),
           "liq_price": vr["liq"], "liq_price_source": vr["source"],
           "liq_exit_floor": calc_exit_floor(vr["liq"], direction, entry=entry),
           "timestamp": now.isoformat(), "tx_sig": None, "venue": "jupiter", "market": market,
           "reason": "owner position: not opened by the bot", "owner_position": rec}
    save_position(pos, market=market)
    _owner_pos_log("detected", market, rec, size_usd=size, leverage=lev, entry=entry, liq=vr["liq"])
    _owner_pos_publish()
    await _owner_pos_ask(market, pos, "position")
    return True


async def _owner_pos_flag_add(market: str, pos: dict, added_usd: float):
    """A same-side size increase the bot did not make. The whole position waits
    for the owner's call."""
    now = datetime.now(EST)
    rec = {"id": f"op_{market.split('-')[0].lower()}_add_{int(now.timestamp())}", "kind": "add",
           "state": "pending", "source": "unknown", "tag": "owner_position", "added_usd": round(added_usd, 2),
           "prior_state": "managed", "detected_at": now.isoformat(), "detected_ts": now.timestamp(),
           "timeout_min": OWNER_POS_TIMEOUT_MIN}
    pos["owner_position"] = rec
    save_position(pos, market=market)
    _owner_pos_log("detected", market, rec, added_usd=added_usd, size_usd=pos.get("size_usd"))
    _owner_pos_publish()
    await _owner_pos_ask(market, pos, "add")


async def _owner_pos_take_over(market: str, pos: dict, source: str, reason: str):
    o = pos.get("owner_position") or {}
    old = {"tp_price": pos.get("tp_price"), "sl_price": pos.get("sl_price")}
    entry, d, lev = float(pos.get("entry_price") or 0), pos.get("direction"), float(pos.get("leverage") or 1)
    if entry and not (pos.get("tp_price") or pos.get("sl_price")):
        pos["tp_price"], pos["sl_price"] = calc_auto_tp_sl(entry, d, lev)
    if pos.get("liq_price"):
        pos["liq_exit_floor"] = calc_exit_floor(pos["liq_price"], d, entry=entry, tp=pos.get("tp_price"), sl=pos.get("sl_price"))
    o.update({"state": "managed", "decided_at": datetime.now(EST).isoformat(), "decided_by": source,
              "reason": reason})
    pos["owner_position"] = o
    pos["params_rev"] = int(pos.get("params_rev") or 0) + 1
    save_position(pos, market=market)
    for k in ("tp_price", "sl_price"):
        if old[k] != pos.get(k):
            _perp_log_param_change(market, k, old[k], pos.get(k), f"owner_position take-over: {reason}", "auto")
    _owner_pos_log("take_over", market, o, source=source, reason=reason,
                   tp=pos.get("tp_price"), sl=pos.get("sl_price"))
    _owner_pos_publish()
    try:
        await notify(f"🤖 <b>Managing {market} {str(d).upper()}</b> (owner position)\n{reason}.\n"
                     f"TP ${pos.get('tp_price')} | SL ${pos.get('sl_price')} | floor ${pos.get('liq_exit_floor')}\n"
                     f"Size and leverage untouched. Exits stay alert-only: I alert at TP, trail, floor "
                     f"and SL; a signal closes.")
    except Exception:
        pass


async def _owner_pos_decide(market: str, pos: dict, decision: str, source: str) -> bool:
    o = pos.get("owner_position") or {}
    decision = str(decision or "").strip().lower()
    if o.get("state") != "pending" or decision not in ("manage", "leave"):
        return False
    if decision == "manage":
        await _owner_pos_take_over(market, pos, source, f"owner said manage ({source})")
        return True
    o.update({"state": "leave", "decided_at": datetime.now(EST).isoformat(), "decided_by": source})
    pos["owner_position"] = o
    save_position(pos, market=market)
    _owner_pos_log("decision", market, o, decision="leave", source=source)
    _owner_pos_publish()
    try:
        await notify(f"👤 {market}: left to you. Liquidation warning only; a signal close needs "
                     f"owner_explicit_close.")
    except Exception:
        pass
    return True


def _owner_pos_matches(market: str, o: dict, target) -> bool:
    t = str(target or "").strip().lstrip("$").lower()
    sym = market.split("-")[0].lower()
    return bool(t) and t in (str(o.get("id") or "").lower(), sym, market.lower(), sym + "perp")


async def execute_owner_position_perps(data: dict):
    """Relay: {"action": "owner_position", "id"|"market": .., "decision": "manage"|"leave"}."""
    target = data.get("id") or data.get("market") or data.get("ticker")
    for m, p in (load_positions() or {}).items():
        o = (p or {}).get("owner_position") or {}
        if o.get("state") == "pending" and (_owner_pos_matches(m, o, target)):
            if not await _owner_pos_decide(m, p, data.get("decision"), "relay"):
                await notify("⚠️ owner_position: decision must be manage or leave")
            return
    await notify(f"⚠️ owner_position: no pending owner position matches {target!r}")


def _owner_pos_decisions() -> list:
    out = []
    try:
        if os.path.exists(OWNER_POS_DECISIONS_FILE):
            with open(OWNER_POS_DECISIONS_FILE, encoding="utf-8-sig") as f:
                for ln in f:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
    except Exception:
        pass
    return out


async def owner_position_perps_loop():
    if PERPS_DRY_RUN:
        print("[owner_pos] paper mode: owner-position scan off (real wallet positions are not touched)")
        return
    """Every 60s: owner-command decisions, timeouts, early take-over near liq.
    Every OWNER_POS_SCAN_S: look for on-chain positions or adds the bot did not make."""
    print(f"[owner_pos] loop starting (timeout {OWNER_POS_TIMEOUT_MIN:g} min, "
          f"early at {OWNER_POS_LIQ_EARLY_FRAC*100:g}% of liq distance)")
    last_scan = 0.0
    while True:
        try:
            if OWNER_POS_ENABLED:
                store = load_positions() or {}
                decs = None
                for m, p in list(store.items()):
                    o = (p or {}).get("owner_position") or {}
                    if o.get("state") != "pending":
                        continue
                    if decs is None:
                        decs = _owner_pos_decisions()
                    mine = [d for d in decs if float(d.get("ts") or 0) >= float(o.get("detected_ts") or 0)
                            and _owner_pos_matches(m, o, d.get("target"))]
                    if mine and await _owner_pos_decide(m, p, mine[-1].get("decision"), "owner_command"):
                        continue
                    if time.time() - float(o.get("detected_ts") or 0) >= OWNER_POS_TIMEOUT_MIN * 60:
                        _owner_pos_log("timeout", m, o, timeout_min=OWNER_POS_TIMEOUT_MIN)
                        await _owner_pos_take_over(m, p, "timeout", f"no answer in {OWNER_POS_TIMEOUT_MIN:g} min")
                        continue
                    try:
                        px = await _market_mark_price_cached(m)
                        en, lq = float(p.get("entry_price") or 0), float(p.get("liq_price") or 0)
                        if px and en and lq and abs(en - lq) > 0:
                            frac = (px - lq) / (en - lq)   # 1 at entry, 0 at liq (both sides)
                            if frac <= OWNER_POS_LIQ_EARLY_FRAC:
                                _owner_pos_log("early_take_over", m, o, mark=px, liq=lq, frac=round(frac, 3))
                                await _owner_pos_take_over(m, p, "near_liquidation",
                                                           f"price {px:,.2f} is within "
                                                           f"{OWNER_POS_LIQ_EARLY_FRAC*100:g}% of the distance to "
                                                           f"liquidation {lq:,.2f}")
                    except Exception as _pe:
                        print(f"[owner_pos] liq check err {m}: {_pe!r}")
                if time.time() - last_scan >= OWNER_POS_SCAN_S:
                    last_scan = time.time()
                    for m in PERP_MARKETS_COVERED:
                        p = store.get(m) or {}
                        for d in ("long", "short"):
                            key = f"{m}:{d}"
                            if p.get("direction") and p.get("direction") != d:
                                continue
                            vr = await fetch_venue_liq_price(m, d)
                            if not vr:
                                _OWNER_POS_SEEN.pop(key, None)
                                continue
                            if not p.get("direction"):
                                # two sightings in a row, so a fill the bot is still booking is not mistaken
                                _OWNER_POS_SEEN[key] = _OWNER_POS_SEEN.get(key, 0) + 1
                                if _OWNER_POS_SEEN[key] >= 2:
                                    _OWNER_POS_SEEN.pop(key, None)
                                    await _owner_pos_perps_adopt(m, d)
                            else:
                                local = float(p.get("size_usd") or 0)
                                chain = float(vr.get("size_usd") or 0)
                                own = (p.get("owner_position") or {}).get("state")
                                if local > 0 and chain > local * 1.05 and own not in ("pending",):
                                    _OWNER_POS_SEEN[key] = _OWNER_POS_SEEN.get(key, 0) + 1
                                    if _OWNER_POS_SEEN[key] >= 2:
                                        _OWNER_POS_SEEN.pop(key, None)
                                        added = chain - local
                                        p["size_usd"] = round(chain, 4)
                                        p["collateral"] = vr.get("collateral_usd", p.get("collateral"))
                                        p["leverage_onchain"] = vr.get("leverage")
                                        p["liq_price"], p["liq_price_source"] = vr["liq"], vr["source"]
                                        await _owner_pos_flag_add(m, p, added)
                                else:
                                    _OWNER_POS_SEEN.pop(key, None)
        except Exception as e:
            print(f"[owner_pos] loop err: {e!r}")
        await asyncio.sleep(60)


# ── PARKED LIMIT ENTRIES (bot_logs/perp_limit_order.json) ─────────────────────────
# 2026-10-17. A limit_long / limit_short (or order_type "limit") more than 1% away is
# parked in perp_limit_order.json. Jupiter has no on-chain limit, so the main loop polls
# the TRADED market's mark price and, when the target is reached, fires the parked signal
# as a normal market open through process_signal (every gate, cap and guard applies).
# One parked entry at a time (a new one replaces the old). One-shot: the file is renamed
# to .firing before the open, so it can never fill twice; a .firing left by a crash is
# reported and NOT retried. Expiry: expires_at (ISO) or expires_in_min on the signal,
# else PERP_LIMIT_TTL_HOURS (default 24). Every outcome writes an ack + a Telegram note.
_PERP_LIMIT_FILE = os.path.join(WS_BOT_LOGS, "perp_limit_order.json")
_PERP_LIMIT_POLL_S = 30
_PERP_LIMIT_LAST = 0.0
_PERP_LIMIT_META = ("limit_price", "order_type", "set_at", "price_at_park", "trigger",
                    "expires_at", "expires_in_min", "parked_via")


def _perp_limit_market(data):
    mk = str(data.get("market") or "SOL-PERP").upper()
    return mk if mk.endswith("-PERP") else mk + "-PERP"


def _perp_limit_ttl_hours():
    try:
        return max(0.1, float(os.getenv("PERP_LIMIT_TTL_HOURS") or 24))
    except Exception:
        return 24.0


def _perp_limit_expiry(data):
    """ISO expiry: expires_at on the signal, else expires_in_min, else the TTL."""
    now = datetime.now(EST)
    v = data.get("expires_at")
    if v:
        try:
            t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=EST)).astimezone(EST).isoformat()
        except Exception:
            pass
    try:
        m = float(data.get("expires_in_min") or 0)
    except Exception:
        m = 0
    return (now + timedelta(minutes=m if m > 0 else _perp_limit_ttl_hours() * 60)).isoformat()


async def _perp_limit_ref_price(mk):
    """Mark price of the traded market (the venue's own oracle); SOL falls back to get_sol_price."""
    px = None
    try:
        px = await _market_mark_price_cached(mk)
    except Exception as e:
        print(f"[perp limit] mark price err {mk}: {e!r}")
    if not px and mk == "SOL-PERP":
        try:
            px = await get_sol_price()
        except Exception:
            px = None
    try:
        return float(px) if px and float(px) > 0 else None
    except Exception:
        return None


async def _perp_limit_park(data, action, limit_price, via):
    """Park a limit entry. True = parked (caller returns); False = price is within 1%, open now."""
    mk = _perp_limit_market(data)
    cur = await _perp_limit_ref_price(mk)
    if cur and abs(cur - limit_price) / limit_price <= 0.01:
        print(f"[perp] {mk} ${cur:g} within 1% of limit ${limit_price:g} - opening market now")
        return False
    if cur:
        trig = "at_or_below" if cur > limit_price else "at_or_above"
    else:
        trig = "at_or_below" if action == "long" else "at_or_above"
    rec = {k: v for k, v in data.items() if k not in ("order_type",)}
    rec.update({"action": action, "market": mk, "limit_price": float(limit_price),
                "set_at": datetime.now(EST).isoformat(), "price_at_park": cur, "trigger": trig,
                "expires_at": _perp_limit_expiry(data), "parked_via": via})
    prev = None
    try:
        if os.path.exists(_PERP_LIMIT_FILE):
            with open(_PERP_LIMIT_FILE, encoding="utf-8") as f:
                prev = json.load(f)
    except Exception:
        prev = None
    os.makedirs(os.path.dirname(_PERP_LIMIT_FILE), exist_ok=True)
    tmp = _PERP_LIMIT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2, default=str)
    os.replace(tmp, _PERP_LIMIT_FILE)
    _sym = "<=" if trig == "at_or_below" else ">="
    _rep = (f"\nReplaced the earlier parked {str(prev.get('action', '?')).upper()} "
            f"{prev.get('market', '?')} @ ${prev.get('limit_price', '?')}") if isinstance(prev, dict) else ""
    why = (f"parked: market {action} {mk} fires when price {_sym} ${limit_price:g} "
           f"(now {('$%g' % cur) if cur else 'unknown'}), expires {rec['expires_at'][:16]}")
    print(f"[perp limit] {why}")
    try:
        _perp_set_outcome("parked", reason=why)
    except Exception:
        pass
    await notify(f"⏳ <b>Perp Limit Parked</b>\n{action.upper()} {mk} when price {_sym} ${limit_price:g}\n"
                 f"Now: {('$%g' % cur) if cur else 'unknown'} | expires {rec['expires_at'][:16]} ET\n"
                 f"Fires a market open through every normal gate.{_rep}")
    return True


def _perp_limit_ack(sig, out):
    ok = out.get("status") in ("filled", "processed")
    summ = (f"parked limit {sig.get('action', '?')} {sig.get('market', '?')} {out.get('status')}"
            + (f" tx {str(out.get('tx'))[:20]}..." if out.get("tx") else "")
            + (f" - {out.get('reason')}" if out.get("reason") else ""))
    try:
        relay_ack("perp_limit_order.json", "perp_signal", ok, summ,
                  detail={"action": sig.get("action"), "market": sig.get("market"), **out}, alerted=True)
    except Exception as e:
        print(f"[perp limit] relay_ack err: {e!r}")
    _perp_write_signal_ack("perp_limit_order.json", sig, out)


async def _perp_limit_watch_once():
    f = _PERP_LIMIT_FILE
    firing = f + ".firing"
    now = datetime.now(EST)
    if os.path.exists(firing):
        stuck = f[:-5] + now.strftime(".stuck_%Y%m%d_%H%M%S.json")
        try:
            with open(firing, encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            rec = {}
        os.replace(firing, stuck)
        why = ("the bot stopped while this parked limit was firing; NOT retried. "
               "Check open positions and re-send if still wanted. Kept as " + os.path.basename(stuck))
        _perp_limit_ack(rec if isinstance(rec, dict) else {}, {"status": "failed", "reason": why, "at": now.isoformat()})
        await notify(f"⚠️ <b>Parked limit interrupted</b>\n{why}")
        return
    if not os.path.exists(f):
        return
    try:
        with open(f, encoding="utf-8") as fh:
            rec = json.load(fh)
        if not isinstance(rec, dict):
            raise ValueError("not an object")
    except Exception as e:
        os.replace(f, f + ".bad")
        await notify(f"⚠️ <b>Parked limit unreadable</b>\n{e!r}"[:300] + "\nMoved to perp_limit_order.json.bad; nothing sent.")
        return
    action = str(rec.get("action") or "").lower().replace("limit_", "")
    mk = _perp_limit_market(rec)
    try:
        lp = float(rec.get("limit_price") or 0)
    except Exception:
        lp = 0.0
    if action not in ("long", "short") or lp <= 0:
        os.remove(f)
        why = f"parked limit declined: action {action!r} / limit_price {rec.get('limit_price')!r} invalid; nothing sent"
        _perp_limit_ack(rec, {"status": "declined", "reason": why, "at": now.isoformat()})
        await notify(f"⚠️ <b>Parked limit declined</b>\n{why}")
        return
    exp = None
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at")))
        exp = exp if exp.tzinfo else exp.replace(tzinfo=EST)
    except Exception:
        try:
            exp = datetime.fromisoformat(str(rec.get("set_at"))) + timedelta(hours=_perp_limit_ttl_hours())
            exp = exp if exp.tzinfo else exp.replace(tzinfo=EST)
        except Exception:
            exp = None
    if exp is not None and now >= exp:
        os.remove(f)
        why = f"parked {action} {mk} @ ${lp:g} expired unfilled ({exp.astimezone(EST).isoformat()[:16]} ET); nothing sent"
        _perp_limit_ack(rec, {"status": "expired", "reason": why, "at": now.isoformat()})
        await notify(f"⌛ <b>Parked limit expired</b>\n{why}")
        return
    px = await _perp_limit_ref_price(mk)
    if not px:
        return  # no trustworthy price this tick; try again next tick
    trig = rec.get("trigger") or ("at_or_below" if action == "long" else "at_or_above")
    if not ((px <= lp) if trig == "at_or_below" else (px >= lp)):
        return
    try:
        os.replace(f, firing)   # one-shot claim: a second tick can never see this file
    except FileNotFoundError:
        return
    sig = {k: v for k, v in rec.items() if k not in _PERP_LIMIT_META}
    sig.update({"action": action, "market": mk,
                "limit_fired": {"target": lp, "price": px, "trigger": trig, "at": now.isoformat()}})
    print(f"[perp limit] {mk} ${px:g} reached {trig} ${lp:g} - firing parked {action}")
    out = {}
    try:
        errs = _perp_signal_schema_errors(sig, True)
        if errs:
            out = {"status": "declined", "reason": "; ".join(errs)}
        else:
            globals()["_PERP_SIG_OUTCOME"] = None
            globals()["_PERP_SIG_NOTES"] = []
            try:
                await process_signal(sig)
            finally:
                _notes = globals().get("_PERP_SIG_NOTES") or []
                globals()["_PERP_SIG_NOTES"] = None
            out = dict(globals().get("_PERP_SIG_OUTCOME") or {})
            if not out:
                _isref, _nr = _perp_note_reason(_notes)
                out = {"status": "declined",
                       "reason": _nr or "the bot returned without a result or a message (silent path - report it)"}
    except Exception as e:
        traceback.print_exc()
        out = {"status": "failed", "reason": f"parked limit fire error: {e!r}"[:300]}
    finally:
        try:
            os.remove(firing)
        except FileNotFoundError:
            pass
    out.setdefault("at", datetime.now(EST).isoformat())
    out["limit"] = {"target": lp, "price": px, "trigger": trig}
    _perp_limit_ack(sig, out)
    await notify(f"🎯 <b>Parked limit triggered — {action.upper()} {mk}</b>\n"
                 f"Target ${lp:g}, price ${px:g}\nResult: {out.get('status')}"
                 + (f"\n{str(out.get('reason'))[:200]}" if out.get("reason") and out.get("status") != "filled" else ""))


async def perp_limit_watch_tick():
    """Main-loop hook (same loop as the relay, so it never overlaps a signal). Failure-isolated."""
    global _PERP_LIMIT_LAST
    if time.time() - _PERP_LIMIT_LAST < _PERP_LIMIT_POLL_S:
        return
    _PERP_LIMIT_LAST = time.time()
    try:
        await _perp_limit_watch_once()
    except Exception as e:
        print(f"[perp limit] watcher err (isolated, will retry): {e!r}")


async def process_signal(data: dict):
    # ── field name normalization — the agent is boss, accept ANY variation ──
    # Resolve action from every field the agent has ever used:
    #   action: long/short/close/open
    #   direction: long/short
    #   signal: open_long/open_short/close/long/short
    #   side: long/short (paired with action=open)
    raw_action = (
        data.get("action") or
        data.get("direction") or
        data.get("signal") or
        data.get("side") or
        ""
    ).lower().strip()

    # normalize compound forms
    if raw_action in ("open_long", "buy_long", "long"):
        action = "long"
    elif raw_action in ("open_short", "buy_short", "short"):
        action = "short"
    elif raw_action in ("close", "close_position", "exit", "close_long", "close_short"):
        action = "close"
    elif raw_action in ("update_params", "set_params", "params", "update_levels"):
        action = "update_params"
    elif raw_action in ("adjust", "modify", "update", "edit"):
        action = "adjust"
    elif raw_action in ("add", "add_size", "scale_in", "increase", "add_long", "add_short"):
        action = "add"
    elif raw_action in ("scan", "scan_positions", "reconcile", "sync"):
        action = "scan"
    elif raw_action == "open":
        side = (data.get("side") or data.get("direction") or "").lower()
        action = side if side in ("long", "short") else ""
    # Sprint 39: structured skip + confirm_signal
    elif raw_action in ("skip", "ignore", "decline"):
        action = "skip"
    elif raw_action in ("confirm_signal", "confirm", "endorse"):
        action = "confirm_signal"
    elif raw_action in ("reset_record", "reset_stats", "clean_slate"):
        action = "reset_record"
    elif raw_action in ("ack_lesson", "acknowledge", "ack"):
        action = "ack_lesson"
    elif raw_action in ("close_pct", "partial_close", "close_partial", "scale_out"):
        action = "close_pct"
    elif raw_action in ("pull_initial", "pull_init", "bank_initial", "recover_initial"):
        action = "pull_initial"
    else:
        action = raw_action

    if not action:
        print(f"[perp relay] empty signal {data} - could not resolve action, deleting")
        return

    # ── Sprint 39: structured skip ingestion ──
    if action == "skip":
        ok, err = ingest_skip_perps(data)
        if ok:
            print(f"[s39] skip ingested for {data.get('packet_id', '?')}")
        else:
            print(f"[s39] skip rejected: {err}")
            try:
                await notify(f"⚠️ <b>Perps skip rejected</b>\n{err[:300]}")
            except Exception:
                pass
        return

    # ── Sprint 39: confirm_signal (reverse endorsement) ──
    if action == "confirm_signal":
        ok, err = ingest_confirm_signal_perps(data)
        if ok:
            print(f"[s39] confirm_signal ingested for {data.get('packet_id', '?')}")
        else:
            print(f"[s39] confirm_signal rejected: {err}")
            try:
                await notify(f"⚠️ <b>Perps confirm_signal rejected</b>\n{err[:300]}")
            except Exception:
                pass
        return

    # ── HOTFIX 8: handle adjust + scan actions before falling into old logic ──
    if action == "update_params":
        await execute_update_params_perps(data)
        return

    if action == "owner_position":
        await execute_owner_position_perps(data)
        return

    if action == "adjust":
        market_str = (data.get("market") or "SOL-PERP").upper()
        sol_price = await get_sol_price()
        # Sprint 43: route to the venue that OWNS the position (Drift or Jupiter).
        result = await route_adjust_position(data, sol_price)
        venue = result.get("venue", "jupiter")
        print(f"[perp adjust] {market_str} ({venue}): {result}")
        # Notify
        if result["adjustments"]:
            adj_msg = "\n".join([f"  - {a}" for a in result["adjustments"]])
            await notify(f"🔧 <b>Position Adjusted</b> ({venue})\n{market_str}\n{adj_msg}")
        if result["errors"]:
            err_msg = "\n".join([f"  - {e}" for e in result["errors"]])
            await notify(f"⚠️ <b>Adjust Errors</b> ({venue})\n{market_str}\n{err_msg}")
        return

    # NEW_POSITIONS_ENABLED (tuner / bots/.env / setup wizard / directive): off = no new
    # exposure from agent signals (long / short / add). Close, adjust, update_params,
    # close_pct and pull_initial still work.
    if (action in ("long", "short", "add", "limit_long", "limit_short")
            and not _perp_get_override("NEW_POSITIONS_ENABLED", NEW_POSITIONS_ENABLED)):
        _mk = (data.get("market") or "SOL-PERP").upper()
        _why = ("new_positions_off: NEW_POSITIONS_ENABLED is off, no new opens or adds "
                "(closes, adjust, update_params, close_pct and pull_initial still work)")
        print(f"[perp] {action} {_mk} declined: {_why}")
        _perp_decline_log_bot(_mk, action, _why, "new_positions_off")
        await notify(f"⏸️ <b>Perps {action} declined — {_mk}</b>\n{_why}")
        return

    # v1.0.6 ACTIVE_HOURS (bots/active_hours.py): the loop runs 24/7; outside the owner's window it
    # idles on new exposure only. Checked before any order is built, so nothing is cut off mid-order;
    # closes, adjust, update_params, close_pct, TP/SL/trail and monitoring keep running. Resumes on its own.
    if action in ("long", "short", "add", "limit_long", "limit_short"):
        try:
            import active_hours as _ah
            _aok, _awhy = _ah.in_active_window()
        except Exception:
            _aok, _awhy = True, ""
        if not _aok:
            _mk = (data.get("market") or "SOL-PERP").upper()
            _why = f"outside_active_hours: {_awhy}; new opens/adds resume when the window opens (exits still run)"
            print(f"[perp] {action} {_mk} declined: {_why}")
            _perp_decline_log_bot(_mk, action, _why, "outside_active_hours")
            await notify(f"⏸️ <b>Perps {action} declined — {_mk}</b>\n{_why}")
            return

    if action == "add":
        await execute_position_add(data)
        return

    if action == "scan":
        # Sprint 57: scan/reconcile against JUPITER on-chain positions, not Drift.
        recon = await reconcile_jupiter_positions()
        scan_path = os.path.join(BOT_LOGS_PATH, "perp_scan_results.json") if "BOT_LOGS_PATH" in globals() else None
        if scan_path:
            with open(scan_path, "w", encoding="utf-8") as f:
                json.dump({
                    "scanned_at": _s27_now_iso() if "_s27_now_iso" in globals() else None,
                    "reconcile":  recon,
                }, f, indent=2)
        _open_now = [m for m, r in recon.items() if r.get("chain") in ("open",) or str(r.get("chain","")).startswith("open")]
        _cleared  = [m for m, r in recon.items() if r.get("action") == "cleared_local_ghost"]
        _flagged  = [m for m, r in recon.items() if r.get("action") == "flagged_untracked"]
        print(f"[perp scan] jupiter reconcile: open={_open_now} cleared={_cleared} flagged={_flagged}")
        if _open_now or _cleared or _flagged:
            _msg = "📊 <b>Jupiter position scan</b>\n"
            if _open_now: _msg += f"Open: {', '.join(_open_now)}\n"
            if _cleared:  _msg += f"Cleared ghosts: {', '.join(_cleared)}\n"
            if _flagged:  _msg += f"⚠️ Untracked on-chain: {', '.join(_flagged)}\n"
            await notify(_msg.rstrip())
        else:
            await notify(f"📊 <b>No open positions on-chain</b>")
        return
    # ── END HOTFIX 8 EARLY DISPATCH ──

    # Sprint 47: acknowledge an autopsy lesson (clears it until he backslides)
    if action == "ack_lesson":
        _lid = data.get("lesson_id") or data.get("id")
        ok = acknowledge_lesson(_lid) if _lid else False
        await notify(f"{'✅ lesson acknowledged' if ok else '⚠️ lesson not found'}: {_lid}")
        return

    # Sprint 46: clean-slate the track record on demand (archives old data)
    if action == "reset_record":
        _rr = reset_perp_record(archive=True)
        await notify(f"🧾 <b>Perp track record reset</b>\nArchived: {_rr.get('archived_to') or 'n/a'}\nClean slate — win% starts fresh.")
        return

    # limit order support — perps-boss can set a price target entry
    _is_limit = action in ("limit_long", "limit_short")
    if _is_limit:
        action = action.replace("limit_", "")
        try:
            _limit_price = float(data.get("limit_price") or 0)
        except Exception:
            _limit_price = 0.0
        if _limit_price <= 0:
            await notify(f"⚠️ <b>Perp limit_{action} declined</b>\nlimit_price is required and must be > 0; nothing sent.")
            return
        if await _perp_limit_park(data, action, _limit_price, "limit_action"):
            return

    # LEVERAGE FIX 2026-10-16: sizing and TP/SL go through the pure helpers
    # _perp_parse_open_sizing / _perp_resolve_tp_sl (see their docstrings).
    reason   = data.get("reason", "The agent signal")

    sol_price = await get_sol_price()
    if not sol_price:
        await notify_error("process_signal", "could not get SOL price - all sources failed")
        return

    _sz = _perp_parse_open_sizing(data, sol_price)
    globals()["_LAST_SOL_PRICE"] = sol_price
    # 2026-10-16 LEVERAGE LADDER: an open with collateral but no leverage takes the
    # ladder value (less collateral -> more leverage, capped at PERP_MAX_LEVERAGE).
    if (action in ("long", "short") and not _sz.get("leverage_given")
            and float(_sz.get("collateral_usd") or 0) > 0):
        _ll = _perp_lev_for_collateral_usd(_sz["collateral_usd"], sol_price)
        _sz["leverage"], _sz["leverage_given"] = _ll, True
        if not float(_sz.get("size_usd") or 0):
            _sz["size_usd"] = round(float(_sz["collateral_usd"]) * _ll, 2)
        _sz.setdefault("notes", []).append(
            f"leverage omitted -> ladder {_ll:g}x for ${float(_sz['collateral_usd']):.2f} collateral (PERP_LEV_LADDER)")
    size_usd       = _sz["size_usd"]
    collateral_usd = _sz["collateral_usd"]
    leverage       = _sz["leverage"]
    raw_col        = collateral_usd   # legacy name: > 0 means collateral was resolved
    for _n in _sz["notes"]:
        print(f"[perp] sizing: {_n}")

    # TP/SL: priced off the TRADED market (SOL/BTC/ETH), side-aware.
    tp_price = sl_price = None
    _tpsl_warns = []
    if action in ("long", "short"):
        _sig_mkt = (data.get("market") or "SOL-PERP").upper()
        if not _sig_mkt.endswith("-PERP"):
            _sig_mkt = _sig_mkt + "-PERP"
        _ref = None
        try:
            _ref = await _market_mark_price(_sig_mkt)
        except Exception as _rpe:
            print(f"[perp] mark price err {_sig_mkt}: {_rpe!r}")
        if not _ref and _sig_mkt == "SOL-PERP":
            _ref = sol_price
        tp_price, sl_price, _tpsl_warns = _perp_resolve_tp_sl(data, action, _ref)
        if tp_price:
            print(f"[perp] TP ${tp_price:.4f} ({_sig_mkt} ref {_ref})")
        if sl_price:
            print(f"[perp] SL ${sl_price:.4f} ({_sig_mkt} ref {_ref})")
        for _w in _tpsl_warns:
            print(f"[perp] TP/SL warning: {_w}")
        if _tpsl_warns:
            try:
                await notify("TP/SL warning: " + "; ".join(_tpsl_warns))
            except Exception:
                pass

    # ── LEVERAGE BAND + MINIMUM OPENING (2026-07-26) ────────────────────────────
    # DECLINE, never rewrite. This block used to do `leverage = 2.0` on an
    # out-of-range value — silently landing a trade the agent never asked for.
    # Same disease as the size multiplier: the bot deciding the position.
    _decline = None
    # Floor is for opening long/short only. Close and adjust must not be
    # declined because a close payload omitted leverage (it defaults to 1x).
    if action in ("long", "short"):
        _lo = float(PERP_MIN_LEVERAGE)
        _hi = float(PERP_MAX_LEVERAGE)
        _minc = float(PERP_MIN_COLLATERAL_USD)
        _minn = float(PERP_MIN_NOTIONAL_USD)
        if not _sz.get("leverage_given"):
            _decline = ("leverage is missing and no collateral was given, so the PERP_LEV_LADDER "
                        "cannot pick one. Give collateral_sol / collateral_usd (leverage optional) "
                        "or leverage with size_usd.")
        elif leverage <= 0:
            _decline = f"leverage {leverage}x is not a number I can trade"
        elif leverage < _lo:
            _decline = (f"leverage {leverage:g}x is below the {_lo:g}x floor. Under {_lo:g}x the "
                        f"position cannot produce a readable outcome at your ammo — the 5x SOL long "
                        f"was right for 47h and paid $0.60. Re-request at {_lo:g}x or higher, or "
                        f"skip the setup.")
        elif leverage > _hi:
            _decline = (f"leverage {leverage:g}x is above the {_hi:g}x cap. Not clamping you to the "
                        f"cap — re-request at {_hi:g}x or below so the number that lands is yours.")
        elif collateral_usd and collateral_usd < _minc:
            _decline = (f"collateral ${collateral_usd:.2f} is below the ${_minc:.2f} floor. "
                        f"A sub-floor position produces an unreadable outcome \u2014 it teaches the "
                        f"autopsy nothing either way.")
        elif size_usd and size_usd < _minn:
            _decline = (f"notional ${size_usd:.2f} is below the ${_minn:.2f} floor "
                        f"(${_minc:.0f} x {_lo:g}x). Collateral and leverage can be any mix "
                        f"that clears it — {_lo:g}x on ${_minc:.0f}, or 60x on ${_minc:.0f}. "
                        f"Being small in BOTH is the only thing this blocks.")
    if _decline:
        print(f"[perp] MINIMUM OPENING DECLINE: {_decline}")
        try:
            _mo = {
                "from": "perps_bot", "type": "minimum_opening_decline",
                "market": data.get("market") or "SOL-PERP", "action": action,
                "requested": {"size_usd": size_usd, "leverage": leverage,
                              "collateral_usd": collateral_usd},
                "floor": {"min_collateral_usd": _minc, "min_leverage": _lo,
                          "max_leverage": _hi, "min_notional_usd": _minn},
                "reason": _decline,
                "doctrine": ("NOT CLAMPED. What you request is what lands, so a request "
                             "below the floor is declined rather than quietly resized. "
                             "The floor constrains the OUTCOME, not the style: any mix of "
                             "collateral and leverage that clears $%.0f notional is fine."
                             % _minn),
                # The owner/the agent 2026-07-27: "lev floors / min_open declines — useful
                # risk rail, but the decline packet can feel like 'don't think,
                # policy already decided.'" The floor is a MEASURABILITY rail,
                # not a judgment on the setup: below it, the outcome can't be
                # read, so the trade teaches nothing either way. Say that, so a
                # decline reads as arithmetic rather than as disapproval.
                "not_a_judgment": ("This says nothing about whether the setup is good. The floor "
                                   "exists because a position this small produces an unreadable "
                                   "outcome — win or lose, it teaches nothing. Your read may be "
                                   "perfectly right; the size just can't prove it either way."),
                "ask": "re-request above the floor, or skip — both are fine, neither needs defending",
                "generated_at": datetime.now(EST).isoformat(),
            }
            with open(os.path.join(INBOX_DIR,
                      f"perps_bot - min_open_decline_{datetime.now(EST).strftime('%H%M%S')}.json"),
                      "w", encoding="utf-8") as _f:
                json.dump(_mo, _f, indent=2, default=str)
        except Exception as _e:
            print(f"[perp] min-open decline packet err: {_e}")
        _perp_decline_log_bot(data.get("market") or "SOL-PERP", action, _decline, "bot_min_opening")
        await notify(f"🚫 <b>Signal declined — below minimum opening</b>\n{_decline}")
        return

    # if size_usd not provided, calculate from collateral * leverage
    if size_usd == 0 and raw_col > 0:
        col_usd = raw_col   # already USD (resolved by _perp_parse_open_sizing)
        size_usd = col_usd * leverage
        print(f"[perp] size_usd auto-calculated: {col_usd:.2f} collateral × {leverage}x = ${size_usd:.2f}")

    # ── OPEN ──────────────────────────────────────────────────────────────────
    if action in ("long", "short"):
        # Sprint 46: per-chain gate. A position on ANOTHER market does not block
        # this one. Same-market re-fire IS blocked (adjust/close first) — holding a
        # long AND short on one chain = no read, not a hedge.
        _open_market = (data.get("market") or "SOL-PERP").upper()
        _cc_conf = check_committed_divergence(_open_market, action, "open_signal")
        if _cc_conf:
            try:
                await notify(f"📝 <b>{_open_market} {action.upper()} goes against a committed "
                             f"{_cc_conf['direction'].upper()} call</b>\nLogged. Not blocking; the signal decides.")
            except Exception:
                pass
        _same = load_position(_open_market, side=action)
        _mkt_open = _positions_for_market(_open_market)
        _block = None
        if _same:
            _block = (f"already in {_open_market} {action.upper()}. A same-side stack is action=add, not a second open")
        elif _mkt_open and _perp_one_per_market():
            _block = (f"already in {_open_market} {str(_mkt_open[0].get('direction','?')).upper()}. "
                      f"PERP_ONE_PER_MARKET=true allows one position per market: close it first, or set "
                      f"PERP_ONE_PER_MARKET=false to hold long and short as separate positions")
        if _block:
            print(f"[perp] DECLINED open {action} {_open_market}: {_block}")
            _perp_decline_log_bot(_open_market, action, _block, "position_exists")
            await notify(f"\U0001f6ab <b>Open refused - {_open_market} {action.upper()}</b>\n{_block}\n"
                         f"Signal: {action} ${size_usd} {leverage}x | {reason}")
            return
        existing = None

        # ─── SPRINT 33: daily_read.json gating (this session) ────────────────
        # Read feed's daily_read, evaluate the proposed action against
        # the trade_recommendation + abort_conditions + regime_break.
        #
        # Override path: The agent can bypass gating by setting "override_daily_read":true
        # in the signal payload. Audit is still written even on override.
        #
        # Block/scale audit writes to inbox_results/done/ — NEVER back to inbox/
        # per locked inbox hygiene doctrine.
        override_flag = bool(data.get("override_daily_read") or data.get("override"))
        gate_allow, gate_size, gate_reason, gate_audit = _evaluate_against_daily_read(
            action=action,
            requested_size_usd=size_usd,
            override=override_flag,
            market=_open_market,
        )
        # Carry gating context into audit
        gate_audit["leverage_requested"] = leverage
        gate_audit["collateral_requested_usd"] = collateral_usd
        gate_audit["signal_payload"] = data
        gate_audit["signal_reason"]  = reason
        
        # Always write the audit (allow or block) for the agent's review
        audit_path = _write_audit_to_inbox_results(gate_audit)
        if audit_path:
            print(f"[daily_read gate] audit written: {os.path.basename(audit_path)}")
        
        if not gate_allow:
            # BLOCKED by daily_read. Don't open. Tell the agent via Telegram.
            # 2026-07-27: a block here is now a TRUE KILL-SWITCH ONLY — regime
            # break or an active abort condition. Direction disagreement no
            # longer reaches this branch; it records a conflict and proceeds.
            print(f"[daily_read gate] KILL-SWITCH: {gate_reason}")
            _perp_decline_log_bot(_open_market, action, gate_reason, "bot_daily_read_killswitch")
            await notify(
                f"🛑 <b>Perps signal halted — kill-switch (not a disagreement)</b>\n"
                f"Action: {action} ${size_usd:.2f} {leverage}x\n"
                f"Reason: {gate_reason}\n"
                f"Audit: {os.path.basename(audit_path) if audit_path else 'write_failed'}\n"
                f"Override: set <code>\"override_daily_read\":true</code> in signal to bypass."
            )
            return

        # Conflict (not a block): The agent is trading against the mechanical read.
        # He gets told, the audit records it, and the trade GOES THROUGH — that
        # is the whole point. Silence here would be the bot pretending to agree;
        # blocking here would be the bot pretending to decide.
        if gate_audit.get("conflict"):
            _cl = gate_audit.get("conflicts") or []
            _lines = "\n".join(f"• {c.get('fact','')}" for c in _cl[:2])
            # 2026-10-16: folded into the single open alert (no separate message).
            globals().setdefault("_PERP_OPEN_NOTES", {})[_pos_base(_open_market)] = (
                time.time(), "⚖️ Against the read (logged, not blocked):\n" + _lines)
        
        # Sprint 43: degrade/recovery transition pings (one-shot, deduped in the gate).
        if gate_audit.get("degrade_transition"):
            await notify(
                f"⚠️ <b>[PERPS DEGRADED]</b> feed daily_read "
                f"{gate_audit.get('daily_read_freshness', '?')} — perps now capping new "
                f"entries to {_DEGRADED_DIRECTION_TIER}-tier size "
                f"({_AGGRESSION_SIZE_MULTIPLIER.get(_DEGRADED_DIRECTION_TIER, 0.25):.0%} notional) "
                f"until the macro overlay returns. Override per-signal with "
                f"<code>\"override_daily_read\":true</code>."
            )
        elif gate_audit.get("recovery_transition"):
            await notify(
                "✅ <b>[PERPS RECOVERED]</b> feed daily_read fresh again — "
                "perps back to normal daily_read gating."
            )

        # ALLOWED (possibly scaled). Apply the scaled size if it changed.
        if abs(gate_size - size_usd) > 0.01:
            _degraded = gate_audit.get("degraded_mode", False)
            _tag = "degraded (blind)" if _degraded else "aggression tier"
            print(f"[daily_read gate] size scaled by {_tag}: ${size_usd:.2f} → ${gate_size:.2f}")
            # Recompute collateral to match the new size at the same leverage
            old_size = size_usd
            size_usd = gate_size
            if leverage and leverage > 0:
                collateral_usd = size_usd / leverage
                print(f"[daily_read gate] collateral recomputed: ${collateral_usd:.2f} (size ${size_usd:.2f} / lev {leverage}x)")
                # ── FIELD FIX (2026-07-23, the agent's FIRST-EVER fire killed here):
                # the tier scaled his $20 → $3 → $0.60 collateral → rejected
                # against the $5 minimum. A gate that scales must not manufacture
                # an unfireable order from a fireable intent — the agent had ALREADY
                # self-scaled ("accept micro 0.15x") and the tier scaled him
                # AGAIN, below the venue floor. Rule: after scaling, FLOOR UP to
                # the minimum viable size. Small collateral is
                # fine; venue minimums are physics; physics rounds UP, not out.
                _min_coll = float(_perp_get_override("MIN_COLLATERAL_USD", MIN_COLLATERAL_USD))
                if collateral_usd < _min_coll and old_size / leverage >= _min_coll:
                    _floored_size = round(_min_coll * leverage, 2)
                    print(f"[daily_read gate] tier-scaled below venue minimum — FLOORED UP: "
                          f"${size_usd:.2f} → ${_floored_size:.2f} (min collateral ${_min_coll:.2f} × {leverage}x). "
                          f"The agent's intent was fireable; scaling may shrink it, never kill it.")
                    size_usd = _floored_size
                    collateral_usd = _min_coll
            # Notify on scaling — but in degraded mode the transition ping above
            # already covered it, so only send the generic scaling notify when NOT
            # degraded (avoids double-ping per signal while degraded).
            if not _degraded:
                await notify(
                    f"📐 <b>Perps size scaled by daily_read</b>\n"
                    f"{action} ${old_size:.2f} → ${size_usd:.2f} {leverage}x\n"
                    f"Aggression tier: {gate_audit.get('directive', {}).get('aggression', '?')} ({gate_audit.get('directive', {}).get('size_multiplier', 1.0):.0%})\n"
                    f"Audit: {os.path.basename(audit_path) if audit_path else 'write_failed'}"
                )
        elif override_flag:
            print(f"[daily_read gate] OVERRIDE: {gate_reason}")
        else:
            print(f"[daily_read gate] {gate_reason}")
        # ─── END SPRINT 33 daily_read gating ────────────────────────────────

        if size_usd <= 0 or collateral_usd < _perp_get_override("MIN_COLLATERAL_USD", MIN_COLLATERAL_USD):
            await notify_error("signal REJECTED", f"size=${size_usd} collateral=${collateral_usd:.2f} min=${_perp_get_override("MIN_COLLATERAL_USD", MIN_COLLATERAL_USD)}\nsignal: {data}")
            return

        # ── SPRINT 57: Drift collateral preflight DELETED ────────────────────
        # This used to query Drift's free_collateral and cap the trade to 80% of
        # it, hard-stopping with a "deposit USDC at drift.trade" ping when Drift
        # was empty. It gated every trade on a balance Jupiter never touches:
        # Jupiter funds from the SOL wallet (wrap SOL->wSOL, or swap SOL->USDC)
        # inside jupiter_open_position, which already downsizes to whatever the
        # wallet can actually afford. The Drift check was gating the wrong
        # account — noise at best, a false block at worst. The real affordability
        # gate is the Jupiter funding path plus the MIN_COLLATERAL_USD floor above.
        print(f"[perp] opening {action} ${size_usd} {leverage}x | collateral ${collateral_usd:.2f} | SOL @ ${sol_price:.2f}")

        # ── SPRINT 57: order_type="limit" redirected off the dead Drift path ──
        # This used to call open_limit_order() — a Drift-NATIVE post-only on-chain
        # limit order. Jupiter Perps has no on-chain limit through our path, and
        # the Drift path is gone (open_limit_order would now hit None.place_perp_order
        # and crash). But the bot ALREADY has a working limit mechanism: the
        # limit_long / limit_short actions up top, which park the signal in
        # perp_limit_order.json and fire a MARKET open through Jupiter once SOL
        # reaches the target. So an order_type="limit" signal is converted to that
        # poll-and-fire path instead of crashing.
        order_type_str = (data.get("order_type") or "market").lower()
        if order_type_str == "limit":
            limit_price = data.get("limit_price")
            if not limit_price:
                await notify_error("limit order rejected", "limit_price required for order_type=limit")
                return
            try:
                if await _perp_limit_park(data, action, float(limit_price), "order_type"):
                    return
            except Exception as _le:
                await notify_error("limit park failed", str(_le)[:200])
                return
            # within 1% of the target: fall through to the market open below

        retries = 0
        venue_used = "none"
        while retries < 3:
            # Sprint 39: route through venue router (Drift first, Jupiter failover)
            market_str = (data.get("market") or "SOL-PERP").upper()
            # reverse-size soft-warn B (restraint return-channel; WARN never blocks/resizes)
            if retries == 0:
                try:
                    _rs = _reverse_size_softwarn_perp(float(collateral_usd or 0), float(size_usd or 0))
                    if _rs:
                        print(f"[perp] {_rs}")
                except Exception:
                    pass  # a warn must NEVER affect the trade
            globals()["_LAST_OPEN_ERR"] = None
            tx_sig, venue_used = await route_open_position(
                direction=action,
                size_usd=size_usd,
                leverage=leverage,
                collateral_usd=collateral_usd,
                tp_price=float(tp_price) if tp_price else None,
                sl_price=float(sl_price) if sl_price else None,
                sol_price=sol_price,
                market=market_str,
            )
            if venue_used == "declined":
                _perp_set_outcome("declined", reason=str(globals().get("_LAST_OPEN_ERR") or "venue declined"))
                return
            if tx_sig:
                _req_size_pp, _req_lev_pp, _req_col_pp = size_usd, leverage, collateral_usd
                # Sprint 43: if Jupiter auto-funded/downsized, use the ACTUAL filled size
                # so positions.json + breakeven/liq/PnL reflect what really opened.
                _fund_note = ""
                if venue_used == "jupiter" and _LAST_JUP_FILL:
                    size_usd       = _LAST_JUP_FILL.get("size_usd", size_usd)
                    collateral_usd = _LAST_JUP_FILL.get("collateral_usd", collateral_usd)
                    _fa = _LAST_JUP_FILL.get("fund_actions") or []
                    if _fa:
                        _fund_note = "\n".join(f"  • {a}" for a in _fa)
                # 2026-07-23 UNIT FIX: every market books at ITS OWN mark, not
                # SOL's. The open path published the mark it actually used;
                # consume it (fresh + same market), else fall back to sol_price
                # (correct for SOL-PERP and the legacy venue).
                _lom = globals().get("_LAST_OPEN_MARK") or {}
                if (_lom.get("market") == market_str
                        and _lom.get("mark") and time.time() - (_lom.get("at") or 0) < 300):
                    entry_mark = float(_lom["mark"])
                else:
                    entry_mark = sol_price
                if abs(entry_mark - sol_price) > 0.01:
                    print(f"[perp] entry booked at {market_str} mark ${entry_mark:,.4f} "
                          f"(NOT sol_price ${sol_price:.2f} — the unit bug that minted +7149%)")
                base_amount = size_usd / entry_mark
                breakeven   = calc_breakeven_price(entry_mark, action, size_usd, base_amount)
                # venue-first: read the REAL liq off the position account. The
                # estimate is a LABELLED fallback — never a silent stand-in.
                _vl0 = await fetch_venue_liq_price(market_str, action)
                _col_src = "requested_after_funding"
                if _vl0:
                    liq_price, _liq_src0 = _vl0["liq"], _vl0["source"]
                    # 2026-10-16: book the collateral the chain actually holds
                    try:
                        if float(_vl0.get("collateral_usd") or 0) > 0:
                            collateral_usd = round(float(_vl0["collateral_usd"]), 4)
                            _col_src = "chain_position_account"
                    except Exception:
                        pass
                else:
                    liq_price, _liq_src0 = calc_liq_price(entry_mark, action, leverage), "estimate_entry_leverage"
                # polish-2026-10-15 (item 2): check what FILLED against what was
                # ordered. Alert only; size/leverage behavior is unchanged.
                _fill_check = _perp_fill_mismatch(_req_size_pp, _req_lev_pp, _vl0, size_usd)
                if _fill_check:
                    try:
                        await notify(f"\u26a0\ufe0f <b>FILL MISMATCH — {market_str} {action.upper()}</b>\n{_fill_check}")
                    except Exception:
                        pass
                if not tp_price and not sl_price:
                    auto_tp, auto_sl = calc_auto_tp_sl(entry_mark, action, leverage)
                    tp_price = auto_tp
                    sl_price = auto_sl
                    print(f"[perp] auto TP/SL: TP=${tp_price} SL=${sl_price}")
                liq_exit_floor = calc_exit_floor(liq_price, action, entry=entry_mark,
                                                 tp=(float(tp_price) if tp_price else None),
                                                 sl=(float(sl_price) if sl_price else None))
                # Sprint 50: was this entry a feed micro-lean? The gate directive
                # carries micro_lean (set in _normalize_perps_recommendation for the
                # long_micro/short_micro labels). Tag the position so the close path
                # can grade it on the separate anticipatory lane.
                _micro_lean_flag = bool((gate_audit.get("directive", {}) or {}).get("micro_lean", False))
                # Sprint 39: persist venue for close-side routing later
                save_position({"direction": action, "entry_price": entry_mark, "size_usd": size_usd, "leverage": leverage, "collateral": collateral_usd, "base_amount": base_amount, "tp_price": tp_price, "sl_price": sl_price, "breakeven": breakeven, "liq_price": liq_price, "liq_price_source": _liq_src0, "liq_exit_floor": liq_exit_floor, "timestamp": datetime.now(EST).isoformat(), "tx_sig": tx_sig, "reason": reason, "venue": venue_used, "market": market_str, "phase": (load_daily_read() or {}).get("current_phase"), "micro_lean": _micro_lean_flag, "entry_fingerprint": build_entry_fingerprint(market_str.split("-")[0], action, leverage), "entry_decision_context": build_entry_decision_context(gate_audit, data, reason), "entry_packet": _perp_entry_packet_ref(market_str), "events": []})
                emoji  = "📈" if action == "long" else "📉"
                tp_str = tp_sl_label("tp", tp_price, entry_mark, action, leverage)
                sl_str = tp_sl_label("sl", sl_price, entry_mark, action, leverage)
                _tpp, _tpm = tp_sl_pcts(tp_price, entry_mark, action, leverage) if tp_price else (None, None)
                _slp, _slm = tp_sl_pcts(sl_price, entry_mark, action, leverage) if sl_price else (None, None)
                _sl_frac = sl_liq_fraction(entry_mark, sl_price, liq_price, action) if (sl_price and liq_price) else None
                _sl_liq_warn = None
                if _sl_frac is not None and PERP_SL_LIQ_WARN_FRAC > 0 and _sl_frac > PERP_SL_LIQ_WARN_FRAC:
                    _sl_liq_warn = (f"SL sits {_sl_frac*100:.0f}% of the way from entry to liquidation "
                                    f"(warn above {PERP_SL_LIQ_WARN_FRAC*100:.0f}%). Fees and borrow move liq closer. Warning only.")
                fees   = size_usd * (JUPITER_TRADE_FEE_PCT / 100)
                log(f"open {action}", action, size_usd, entry_mark, reason, tx_sig)
                _perp_set_outcome("filled", tx=tx_sig, market=market_str, side=action, entry=entry_mark,
                                  size_usd=size_usd, collateral_usd=collateral_usd, leverage=leverage,
                                  liq=liq_price, tp=tp_price, sl=sl_price, exit_floor=liq_exit_floor,
                                  tp_price_pct=_tpp, tp_margin_pct=_tpm, sl_price_pct=_slp, sl_margin_pct=_slm,
                                  sl_liq_fraction=(round(_sl_frac, 3) if _sl_frac is not None else None),
                                  warning=_sl_liq_warn,
                                  collateral_requested_usd=_req_col_pp, collateral_source=_col_src,
                                  funding=(_LAST_JUP_FILL or {}).get("funding"))
                venue_tag = "Jupiter"  # Sprint 57: only venue
                # 2026-10-16: one alert per open. The against-the-read note and the
                # daily_read exit-watch state at entry ride on this alert; the exit-watch
                # key is seeded so it only re-alerts when that state CHANGES.
                _open_extra = ""
                try:
                    _on = (globals().get("_PERP_OPEN_NOTES") or {}).pop(_pos_base(market_str), None)
                    if _on and time.time() - float(_on[0]) < 300:
                        _open_extra += "\n" + str(_on[1])
                    _dr0 = load_daily_read() or {}
                    _rec0 = _dr0.get("trade_recommendation") or {}
                    _ep0 = _rec0.get("exit_priority_signals") or []
                    _ab0 = _rec0.get("abort_conditions") or []
                    _rb0 = bool(_dr0.get("regime_break_signal", False))
                    if _ep0 or _ab0 or _rb0:
                        _open_extra += (f"\n📌 daily_read at entry: {len(_ep0)} exit-priority, {len(_ab0)} abort"
                                        f"{', REGIME BREAK' if _rb0 else ''} (alerts again only if this changes)")
                    _p0 = load_position(market_str)
                    if _p0 and _p0.get("direction"):
                        _p0["_last_daily_read_alert_key"] = [_rb0, len(_ep0), len(_ab0)]
                        save_position(_p0, market=market_str)
                except Exception as _oe:
                    print(f"[perp] open-alert fold err: {_oe}")
                _fund_block = f"\nAuto-funded:\n{_fund_note}" if _fund_note else ""
                if _sl_liq_warn:
                    _open_extra += f"\n⚠️ {_sl_liq_warn}"
                await notify(f"{emoji} <b>{market_str} {action.upper()} — {venue_tag}</b>\nSize: ${size_usd:.2f} | {leverage}x\nCollateral: ${collateral_usd:.2f}\nEntry: ${entry_mark:,.2f}\nBreakeven: ${breakeven:.2f} | Liq: ${liq_price:.2f}\n{tp_str}\n{sl_str}\nFees (round trip): ${fees:.3f}{_fund_block}{_open_extra}\nReason: {reason}\ntx: {tx_sig[:20]}...")
                return
            # ── 429-aware retry backoff ────────────────────────────────────────
            # fetch_market_lookup_table_accounts inside driftpy hammers Helius
            # 429 needs real cooldown, not just 5s
            # 2026-07-23 (field: two retries burned on AccountNotInitialized —
            # a DETERMINISTIC failure; the account won't initialize itself
            # between attempts): classify before retrying. Anchor account/funds
            # errors fail fast with the actionable cause; only transient errors
            # (RPC, blockhash, 429) earn the backoff.
            _err_txt = str(globals().get("_LAST_OPEN_ERR") or "")
            _DETERMINISTIC = ("AccountNotInitialized", "Error Number: 3012",
                               "insufficient funds", "Error Number: 3007",
                               "InstructionError(2, Custom(3012))",
                               # 2026-10-16: funding/lookup failures do not fix themselves
                               "unfunded_after_swap", "getaddrinfo", "Errno 11001",
                               "Name or service not known", "nodename nor servname")
            if any(sig in _err_txt for sig in _DETERMINISTIC):
                # ── 2026-07-29: READ THE ACCOUNT THE CHAIN NAMED ─────────────
                # The old message ASSUMED every 3012 was a funding problem and
                # said so with total confidence: "the wallet lacks the collateral
                # token ... the SOL->token funding swap should have run". On the
                # BTC failure the swap HAD run and succeeded two log lines
                # earlier, and the chain had explicitly said:
                #     AnchorError caused by account: custody
                # So the message pointed at the wallet while the bug was a dead
                # constant. A confident wrong diagnosis costs more than none — it
                # sends you to the wrong file. Anchor names the account; read it.
                _acct = None
                _m = re.search(r"AnchorError caused by account:\s*([A-Za-z_][A-Za-z0-9_]*)", _err_txt)
                if _m:
                    _acct = _m.group(1)
                _why = {
                    "custody": ("the CUSTODY account for this market does not exist on-chain — "
                                "that is a stale address in JUPITER_CUSTODIES, NOT a wallet or "
                                "funding problem. Check the live Pool config; BTC's hardcoded "
                                "entry was dead until 2026-07-29."),
                    "collateral_custody": ("the COLLATERAL custody is stale/missing — same class "
                                           "as custody, not a funding problem."),
                    "funding_account": ("the funding ATA is missing/empty — the SOL->token swap "
                                        "did not land; check the [s43][fund] lines above."),
                    "receiving_account": "the receiving ATA does not exist yet.",
                }.get(_acct or "", None)
                if _acct:
                    _detail = (f"chain says the failing account is `{_acct}`. "
                               + (_why or "See the Anchor log above for what that account is."))
                elif "insufficient funds" in _err_txt:
                    _detail = "insufficient funds for this order."
                else:
                    _detail = _err_txt[:300]
                await notify_error(
                    f"open {action} FAILED (deterministic — no retry)",
                    f"${size_usd} {leverage}x — {_detail}")
                _perp_set_outcome("failed", reason=f"deterministic, no retry: {_detail}")
                return
            retries += 1
            print(f"[perp] open retry {retries}")
            await asyncio.sleep(20 * retries)  # 20s, 40s, 60s

        await notify_error(f"open {action} FAILED after 3 retries", f"${size_usd} {leverage}x — Jupiter route failed; check Jupiter status")

        _perp_set_outcome("failed", reason="failed after 3 retries: " + str(globals().get("_LAST_OPEN_ERR") or "Jupiter route failed")[:300])

    # ── CLOSE ─────────────────────────────────────────────────────────────────
    elif action == "close":
        # Sprint 46: close the position on the SPECIFIED market (default SOL-PERP),
        # and price the exit off THAT market's oracle, not SOL.
        _close_market = (data.get("market") or "SOL-PERP").upper()
        _cside = str(data.get("side") or data.get("position_side") or "").lower()
        _cside = _cside if _cside in ("long", "short") else None
        if not _cside and len(_positions_for_market(_close_market)) > 1:
            await notify(f"\U0001f6ab <b>{_close_market} close refused</b>\nLong and short are both open. "
                         f"Add side: long or short to the close.")
            return
        existing = load_position(_close_market, side=_cside)
        if not existing or not existing.get("direction"):
            await notify(f"⚠️ No open {_close_market} {_cside or ''} position to close".replace("  ", " "))
            return
        _own = existing.get("owner_position") or {}
        if _own.get("state") in ("pending", "leave") and not data.get("owner_explicit_close"):
            await notify(f"🚫 <b>{_close_market} close refused</b>\nThis is the owner's position "
                         f"({_own.get('state')}). Resend with owner_explicit_close true if the owner wants out.")
            return
        direction   = existing.get("direction")
        _exit_price = await _market_mark_price(_close_market, sol_price) or sol_price
        entry_price = existing.get("entry_price", _exit_price)
        size_usd    = existing.get("size_usd", size_usd)
        lev         = existing.get("leverage", leverage)
        # Sprint 39: route close through SAME venue that opened
        _px_pre = _exit_price
        tx_sig, close_venue = await route_close_position(existing)
        if tx_sig:
            # 2026-10-16: price the exit AFTER the keeper executed (the honest exit
            # mark); the pre-close read is only the fallback.
            _px_post = None
            try:
                _px_post = await _market_mark_price(_close_market)
            except Exception:
                _px_post = None
            _exit_src = "post_close_mark" if _px_post else "pre_close_mark"
            _exit_price = _px_post or _px_pre
            net_pnl, pnl_pct = record_pnl(direction, entry_price, _exit_price, size_usd, lev, reason, market=_close_market, category="signal_close", phase=existing.get("phase"), micro_lean=existing.get("micro_lean", False), position=existing)
            # Sprint 68: pnl_pct is collateral-relative — divide leverage back out for dollars
            gross_p = size_usd * ((pnl_pct or 0) / max(float(lev or 1), 1) / 100)
            fees_p  = size_usd * (JUPITER_TRADE_FEE_PCT / 100)
            pnl_str = f"Gross: ${gross_p:+.2f} | Fees: -${fees_p:.3f} | <b>Net: ${net_pnl:+.2f}</b>" if net_pnl else "PnL unknown"
            clear_position(_close_market, side=direction)
            log(f"close {direction}", direction, size_usd, _exit_price, reason, tx_sig)
            try:
                _ep, _xp = float(entry_price or 0), float(_exit_price or 0)
                _mv = ((_xp - _ep) / _ep if direction == "long" else (_ep - _xp) / _ep) if _ep else 0.0
                _gross = float(size_usd or 0) * _mv
                _fees = (_gross - float(net_pnl)) if net_pnl is not None else None
            except Exception:
                _gross, _fees = None, None
            _perp_set_outcome("filled", tx=tx_sig, event=f"close {direction}", market=_pos_base(_close_market),
                              side=direction, entry=entry_price, exit_price=_exit_price, exit_price_source=_exit_src,
                              exit_mark_equals_entry=(bool(_exit_price) and _exit_price == entry_price) or None,
                              size_usd=size_usd, collateral_usd=existing.get("collateral"),
                              gross_pnl_usd=(round(_gross, 4) if _gross is not None else None),
                              fees_usd=(round(_fees, 4) if _fees is not None else None),
                              pnl_usd=net_pnl, pnl_pct_on_collateral=pnl_pct)
            # 2026-10-16: the alert shows the real gross (price move on notional) and
            # the fees actually booked (gross - net), the same numbers as the ack.
            if _gross is not None and net_pnl is not None:
                pnl_str = (f"Gross: ${_gross:+.2f} | Fees: -${(_fees or 0.0):.3f} | "
                           f"<b>Net: ${float(net_pnl):+.2f}</b>")
            venue_tag = "Jupiter" if close_venue == "jupiter" else "?"
            await notify(f"🔒 <b>{_close_market} {direction.upper()} closed — {venue_tag}</b>\nExit: ${_exit_price:.2f}\n{pnl_str}\nReason: {reason}\ntx: {tx_sig[:20]}...")
        else:
            await notify_error("close FAILED", "check the position manually on Jupiter\nhttps://jup.ag/perps")

    elif action == "close_pct":
        _m = (data.get("market") or "SOL-PERP").upper()
        existing = load_position(_m)
        if not existing or not existing.get("direction"):
            await notify(f"⚠️ No open {_m} position to partial-close"); return
        pct = float(data.get("pct") or data.get("percent") or 0)
        if pct <= 0 or pct > 100:
            await notify("⚠️ close_pct needs pct between 1 and 100"); return
        await _execute_partial_close(existing, _m, min(1.0, pct/100.0), f"partial {pct:.0f}%")
        return

    elif action == "pull_initial":
        _m = (data.get("market") or "SOL-PERP").upper()
        existing = load_position(_m)
        if not existing or not existing.get("direction"):
            await notify(f"⚠️ No open {_m} position"); return
        _dir = existing["direction"]; _entry = float(existing.get("entry_price") or 0)
        _lev = float(existing.get("leverage") or 1)
        _px = await _market_mark_price(_m)
        if not _px:
            await notify(f"⚠️ {_m} oracle unavailable — can't verify the +50% unlock. Try again in a minute."); return
        _raw = (_px - _entry) / _entry if _dir == "long" else (_entry - _px) / _entry
        _equity_mult = 1 + _raw * _lev  # equity / initial collateral
        if _equity_mult < 1.5:
            await notify(f"⚠️ {_m} only up {(_equity_mult-1)*100:.0f}% — pull-initial unlocks at +50% (1.5x). Not yet."); return
        _frac = min(0.95, 1.0 / _equity_mult)  # 2x->50%, 4x->25%: close this much to bank ~initial
        await _execute_partial_close(existing, _m, _frac, "pull initial")
        return

    else:
        await notify_error("process_signal", f"unknown action: '{action}' — use long / short / close")

# ── position monitor ───────────────────────────────────────────────────────────
async def monitor_position():
    print(f"perp position monitor started (multi-market) — every {PRICE_POLL_SECONDS}s")
    # 2026-07-24: boot self-heal — reconcile the book against the chain before
    # the first tick, so a phantom clear from a prior run gets RESTORED (or a
    # true local ghost cleared) the moment the bot wakes. This is what re-adopts
    # the live ETH position the phantom SL-clear orphaned.
    try:
        await reconcile_jupiter_positions()
    except Exception as _bre:
        print(f"[perp monitor] boot reconcile err: {_bre!r}")
    # Sprint 46: per-market monitor. Each open chain (SOL / BTC / ETH) tracks its
    # OWN trailing peak + alert flags and is priced off ITS OWN oracle. Was single
    # position, SOL-priced. State is keyed by market and pruned when a chain closes.
    mstate = {}  # market -> per-market trailing/alert state

    def _fresh_state(pos: dict | None = None):
        # peak_pct + trail_active are RESTORED FROM THE POSITION (fix 2026-07-27).
        # They used to start at 0.0 in memory and were never persisted, so every
        # restart erased the true peak. That field is what _classify_close grades
        # on — a position that round-tripped a big gain would be categorised as a
        # clean close purely because the bot restarted mid-hold. Observed live:
        # the SOL long showed peak=+19.7%, restarted, came back reading peak=+0.0%.
        p = pos or {}
        return {"peak_pct": float(p.get("peak_pct") or 0.0),
                "trail_active": bool(p.get("trail_active") or False),
                "tp_alerted": False,
                "sl_alerted": bool((p.get("alerts_sent") or {}).get("sl")),
                "liq_alerted": bool((p.get("alerts_sent") or {}).get("liq")),
                "last_valid_price": None}

    while True:
        globals()["_PERP_MONITOR_LAST_TICK"] = time.time()
        try:
            positions = load_positions()
            if not positions:
                mstate.clear()
                await asyncio.sleep(_perp_get_override("PRICE_POLL_SECONDS", PRICE_POLL_SECONDS))
                continue

            # drop state for any chain that closed since last cycle
            for _m in list(mstate):
                if _m not in positions:
                    mstate.pop(_m, None)

            # one sleep per cycle; if ANY open chain is near its TP/SL, poll fast for all
            poll = _perp_get_override("PRICE_POLL_SECONDS", PRICE_POLL_SECONDS)

            for market, existing in list(positions.items()):
                st  = mstate.setdefault(market, _fresh_state(existing))
                sym = _pos_base(market).replace("-PERP", "")
                # polish-2026-10-15 (update_params): new levels re-arm their alerts
                if st.get("params_rev") != existing.get("params_rev"):
                    if st.get("params_rev") is not None:
                        st["tp_alerted"] = False; st["sl_alerted"] = False
                        st["floor_alerted"] = False; st["trail_drop_seen"] = None
                    st["params_rev"] = existing.get("params_rev")

                price = await _market_mark_price_cached(market, max_age=_perp_monitor_mark_max_age())
                if not price:
                    continue

                # per-market oracle sanity — reject a >25% one-cycle ghost spike
                lvp = st["last_valid_price"]
                if lvp and abs(price - lvp) / lvp > 0.25:
                    print(f"[perp monitor] ⚠️ {sym} oracle spike rejected: ${lvp:.4f} -> ${price:.4f} — skipping")
                    continue
                st["last_valid_price"] = price

                direction = existing["direction"]
                entry     = float(existing.get("entry_price", price))
                size_usd  = existing.get("size_usd", 0)
                leverage  = float(existing.get("leverage", 1))
                tp_price  = existing.get("tp_price")
                sl_price  = existing.get("sl_price")
                liq_price = existing.get("liq_price") or calc_liq_price(entry, direction, leverage)
                breakeven = existing.get("breakeven") or calc_breakeven_price(entry, direction, size_usd, existing.get("base_amount", (size_usd / entry) if entry else 0))
                opened_at = existing.get("timestamp")

                raw_pct       = (price - entry) / entry * 100 if direction == "long" else (entry - price) / entry * 100
                leveraged_pct = raw_pct * leverage
                # Sprint 68: dollars use the price move ONCE — leverage already lives
                # inside notional. Net carries live borrow accrual for the hold so far.
                gross_pnl_usd = size_usd * (raw_pct / 100)
                _hold_h = 0.0
                try:
                    if opened_at:
                        _hold_h = max(0.0, (datetime.now(EST) - datetime.fromisoformat(opened_at)).total_seconds() / 3600.0)
                except Exception:
                    _hold_h = 0.0
                net_pnl_usd, fees_usd = calc_net_pnl(gross_pnl_usd, size_usd, _hold_h)
                pnl_usd = net_pnl_usd  # the alert strings below read pnl_usd
                # v1.0.3: the trail runs on NET % of collateral (after fees + borrow).
                _col_m = float(existing.get("collateral") or existing.get("collateral_usd") or 0) or (
                    (float(size_usd) / leverage) if leverage else float(size_usd or 0))
                net_lev_pct = (net_pnl_usd / _col_m * 100) if _col_m else leveraged_pct

                # hold time
                hold_str = ""
                if opened_at:
                    try:
                        opened_dt = datetime.fromisoformat(opened_at)
                        if opened_dt.tzinfo is None:
                            opened_dt = opened_dt.replace(tzinfo=EST)
                        held_mins = int((datetime.now(EST) - opened_dt).total_seconds() / 60)
                        hold_str = f" | held {held_mins}m"
                    except Exception:
                        pass

                # near TP/SL -> fast poll this cycle
                near_tp = tp_price and (
                    (direction == "long"  and price >= float(tp_price) * (1 - _perp_get_override("TP_ALERT_BUFFER_PCT", TP_ALERT_BUFFER_PCT)/100)) or
                    (direction == "short" and price <= float(tp_price) * (1 + _perp_get_override("TP_ALERT_BUFFER_PCT", TP_ALERT_BUFFER_PCT)/100))
                )
                near_sl = sl_price and (
                    (direction == "long"  and price <= float(sl_price) * (1 + _perp_get_override("SL_ALERT_BUFFER_PCT", SL_ALERT_BUFFER_PCT)/100)) or
                    (direction == "short" and price >= float(sl_price) * (1 - _perp_get_override("SL_ALERT_BUFFER_PCT", SL_ALERT_BUFFER_PCT)/100))
                )
                if near_tp or near_sl:
                    poll = min(poll, _perp_get_override("FAST_POLL_SECONDS", FAST_POLL_SECONDS))

                # SPRINT 56: throttle the status line to once per PERP_MONITOR_LOG_INTERVAL_S.
                # The monitor still TICKS every `poll` seconds — TP/SL and trailing logic
                # are unchanged and still evaluated on every tick. Only the console print
                # is rate-limited, because a 30s tick was producing a wall of identical
                # lines. State changes (trail activation, exits, spikes) always print.
                _now_mono = time.monotonic()
                _last_log = _PERP_MONITOR_LAST_LOG.get(sym, 0.0)
                _trail_changed = st.get("trail_active") != _PERP_MONITOR_LAST_TRAIL.get(sym)
                if (_now_mono - _last_log) >= PERP_MONITOR_LOG_INTERVAL_S or _trail_changed:
                    _PERP_MONITOR_LAST_LOG[sym] = _now_mono
                    _PERP_MONITOR_LAST_TRAIL[sym] = st.get("trail_active")
                    print(f"[perp monitor] {sym} {direction.upper()} {leverage}x | entry=${entry:.4f} now=${price:.4f} | gross {leveraged_pct:+.1f}% net {net_lev_pct:+.1f}% (${net_pnl_usd:+.2f}) | peak={st['peak_pct']:+.1f}%{' TRAIL' if st['trail_active'] else ''}{hold_str}")

                # ── liq price warning ─────────────────────────────────────────
                liq_dist_pct = abs(price - liq_price) / price * 100 if price else 999
                _liq_entry_dist = abs(entry - float(liq_price)) / entry * 100 if (entry and liq_price) else 0.0
                _liq_warn_at = float(_perp_get_override("PERP_LIQ_WARN_PCT", PERP_LIQ_WARN_PCT))
                if _liq_entry_dist > 0:
                    _liq_warn_at = min(_liq_warn_at, _liq_entry_dist * float(_perp_get_override("PERP_LIQ_WARN_FRAC", PERP_LIQ_WARN_FRAC)))
                if liq_dist_pct < _liq_warn_at and not st["liq_alerted"]:
                    st["liq_alerted"] = True
                    _perp_mark_alert_sent(market, existing, "liq")
                    _perp_pos_event(market, existing, "liq_warn", price, leveraged_pct)
                    await notify(
                        f"🚨 <b>LIQUIDATION APPROACHING — {sym} {direction.upper()} {leverage}x</b>\n"
                        f"{sym}: ${price:.4f} | Liq: ${liq_price:.4f}"
                        f"{'' if (existing or {}).get('liq_price_source') == 'onchain_position_account' else ' (est.)'}"
                        f" | {liq_dist_pct:.1f}% away (was {_liq_entry_dist:.1f}% at entry, warns under {_liq_warn_at:.1f}%) | PnL {leveraged_pct:+.0f}%\n"
                        f"Your call: resize leverage DOWN (widens liq) / add margin / take the loss and close?\n"
                        f"No auto-action. If you don't act and price hits liq, you're wiped."
                    )

                # ── owner position (2026-10-15): pending or left -> alerts only ──
                if (existing.get("owner_position") or {}).get("state") in ("pending", "leave"):
                    continue

                # ── trailing stop ──────────────────────────────────────────────
                if net_lev_pct > st["peak_pct"]:
                    st["peak_pct"] = net_lev_pct
                    # persist so a restart can't erase it (throttled: only on a
                    # meaningful advance, not every 30s tick of noise)
                    if net_lev_pct - float(existing.get("peak_pct") or 0.0) >= 0.5:
                        existing["peak_pct"]   = round(net_lev_pct, 2)
                        existing["peak_pct_basis"] = "net_on_collateral"
                        existing["trail_active"] = bool(st.get("trail_active"))
                        save_position(existing, market=market)

                _t_act, _t_drop = trail_levels(leverage, st["peak_pct"], pos=existing)
                if not st["trail_active"] and st["peak_pct"] >= _t_act:
                    st["trail_active"] = True
                    _perp_pos_event(market, existing, "trail_active", price, net_lev_pct)
                    await notify(
                        f"🎯 <b>TRAILING STOP ACTIVE — {sym} {direction.upper()} {leverage}x</b>\n"
                        f"Peak gain (net, % of margin): {st['peak_pct']:+.1f}% | Activates at {_t_act:g}% "
                        f"(~{_t_act/leverage:.2f}% price at {leverage:g}x)\n"
                        f"Will fire if drops {_t_drop:g} pts from peak (~{_t_drop/leverage:.2f}% price)"
                    )
                if st["trail_active"] and _t_drop < float(st.get("trail_drop_seen") or 1e9):
                    if st.get("trail_drop_seen") is not None:
                        _perp_log_param_change(market, "trail_drop_pct_effective",
                                               st.get("trail_drop_seen"), _t_drop,
                                               f"auto: peak {st['peak_pct']:+.1f}% reached a tighter trail tier",
                                               "auto")
                        try:
                            await notify(f"🎯 <b>TRAIL TIGHTENED — {sym} {direction.upper()}</b>\n"
                                         f"Peak {st['peak_pct']:+.1f}%: giveback now {_t_drop:g}% "
                                         f"(was {float(st['trail_drop_seen']):g}%). Sensor only.")
                        except Exception:
                            pass
                    st["trail_drop_seen"] = _t_drop

                # ── Sprint 68 DOCTRINE (The owner ruling): TP/SL/trailing are the agent's
                # STANDING ORDERS — parameters he set (or accepted) at entry. The bot
                # executing them is the agent's own pre-authorization firing, not the bot
                # deciding an exit. The FLOOR stands: the bot never *decides* — it
                # executes decisions, live ones (signal files) and standing ones (these).
                if st["trail_active"] and (st["peak_pct"] - net_lev_pct) >= _t_drop:
                    # ── OWNER'S DOCTRINE (2026-07-24): trail firing is a LEVEL
                    # ALERT, not an auto-close — the giveback threshold hitting
                    # is a decision request; the agent fires the close in words.
                    _now_t = time.time()
                    if _now_t - (st.get("trail_alerted_at") or 0) > 1800:
                        st["trail_alerted_at"] = _now_t
                        _perp_pos_event(market, existing, "trail_hit", price, net_lev_pct)
                        # 2026-08-08 (patch B, soft_trail_sensor_v1): sensor wording.
                        # The old copy offered "take the exit" first, which reads as a
                        # nudge to close on a soft giveback. Same facts, no lean - the
                        # trail reports, it does not recommend.
                        await notify(
                            f"🔔 <b>TRAILING LEVEL REACHED (SENSOR) - {sym} {direction.upper()}</b>\n"
                            f"peak {st['peak_pct']:+.1f}% → now {net_lev_pct:+.1f}% net of fees "
                            f"(gave back {st['peak_pct'] - net_lev_pct:.1f} pts ≥ trail {_t_drop:g})\n"
                            f"<b>POSITION REMAINS OPEN.</b> Soft trail is not a close command. "
                            f"Close only on hard SL, thesis death, regime break, the owner order, or "
                            f"planned TP. Your call."
                        )
                    continue  # this chain handled this cycle

                # ── TP hit alert (Drift trigger handles execution) ─────────────
                if tp_price and not st["tp_alerted"] and (
                    (direction == "long"  and price >= float(tp_price)) or
                    (direction == "short" and price <= float(tp_price))
                ):
                    st["tp_alerted"] = True
                    _perp_pos_event(market, existing, "tp_reached", price, leveraged_pct)
                    await notify(
                        f"🎯 <b>TP LEVEL REACHED — {sym} {direction.upper()} {leverage}x</b>\n"
                        f"{sym} @ ${price:.4f} | your TP @ ${float(tp_price):.4f}\n"
                        f"Unrealized: {leveraged_pct:+.1f}% gross, {net_lev_pct:+.1f}% net of fees (${pnl_usd:+.2f})\n"
                        f"<b>POSITION REMAINS OPEN.</b> Take it, trail it, or raise the target — "
                        f"your call, in words. (The old 'will close automatically' was a Drift-era "
                        f"ghost; nothing was behind it.)"
                    )
                    continue  # closed; skip further checks for this chain

                # ── SL approaching warning ─────────────────────────────────────
                _sl_near_warn = bool(sl_price) and price and (
                    abs(price - float(sl_price)) / price * 100 <= _perp_get_override("PERP_SL_WARN_PCT", PERP_SL_WARN_PCT)
                    or (direction == "long" and price <= float(sl_price))
                    or (direction == "short" and price >= float(sl_price)))
                if sl_price and not st["sl_alerted"] and _sl_near_warn:
                    st["sl_alerted"] = True
                    _perp_mark_alert_sent(market, existing, "sl")
                    _perp_pos_event(market, existing, "sl_near", price, leveraged_pct)
                    await notify(
                        f"⚠️ <b>SL WARNING — {sym} {direction.upper()} {leverage}x</b>\n"
                        f"{sym} @ ${price:.4f} | SL @ ${float(sl_price):.4f}\n"
                        f"Leveraged P&L: {leveraged_pct:+.1f}% gross, {net_lev_pct:+.1f}% net of fees (${pnl_usd:+.2f})\n"
                        f"TP/SL monitor should close it soon."
                    )

                # ── EXIT FLOOR (The owner's doctrine 2026-07-26): NOTIFICATION, not a close.
                # This was the ONLY auto-close in the bot. It fired at liq x 1.10 on top
                # of an ALREADY-5%-buffered liq — on the live SOL 5x that meant closing
                # at 69.25 with 14% of real room unused, 1.54% under the SL. That made
                # "The agent decides" untrue for any position that reached its stop.
                # Now: breaching OUR floor is a DECISION POINT carrying the full chain
                # packet. One-shot + re-arm band so chop across the floor can't flood.
                _venue_liq = existing.get("liq_price")
                _liq_src   = existing.get("liq_price_source") or "unknown"
                _floor     = existing.get("liq_exit_floor")
                # PROVENANCE GATE: a pre-patch position carries a 5%-BUFFERED value
                # under the name "liq_price" with no source label. It is truthy, so
                # without this gate a FAILED venue read keeps that old broken number
                # and stamps it as venue truth. Unlabelled == absent, always.
                if _liq_src not in ("onchain_position_account", "estimate_entry_leverage"):
                    _venue_liq = None
                # Refresh EVERY pass: borrow fees move venue liq with no action taken,
                # and a collateral deposit moves it a long way. A floor stored as a
                # stale PRICE is the frozen-number-that-looks-fresh bug — recompute it
                # as a function of the live venue liq, always.
                # Cached: liq only moves on a collateral change or slow fee accrual,
                # so a read per monitor pass is wasted RPC. Refresh every 15 min, or
                # immediately whenever we have no trusted venue number at all.
                _now_ts = time.time()
                _vl_live = None
                if (_now_ts - float(st.get("liq_read_at", 0) or 0)) > 900 or not _venue_liq:
                    _vl_live = await fetch_venue_liq_price(market, direction)
                    if _vl_live:
                        st["liq_read_at"] = _now_ts
                if _vl_live:
                    _venue_liq, _liq_src = _vl_live["liq"], _vl_live["source"]
                elif not _venue_liq:
                    _venue_liq, _liq_src = calc_liq_price(entry, direction, leverage), "estimate_entry_leverage"
                if _venue_liq:
                    _fn = calc_exit_floor(_venue_liq, direction, entry=entry, tp=tp_price,
                                          sl=existing.get("sl_price"))
                    if existing.get("exit_floor_user"):
                        # update_params set it: honour it, still clamped
                        _fn = clamp_exit_floor(existing["exit_floor_user"], _venue_liq, direction,
                                               entry=entry, tp=tp_price, sl=existing.get("sl_price")) or _fn
                    if _fn and (_floor is None or abs(float(_floor) - _fn) > 0.005):
                        _perp_log_param_change(market, "liq_exit_floor", _floor, _fn,
                                               "auto: venue liq moved (position update)", "auto")
                        existing["liq_price"]        = _venue_liq
                        existing["liq_price_source"] = _liq_src
                        existing["liq_exit_floor"]   = _fn
                        save_position(existing, market=market)
                    _floor = _fn
                if _floor:
                    _floor = float(_floor)
                    _rearm = float(_perp_get_override("LIQ_FLOOR_REARM_PCT", LIQ_FLOOR_REARM_PCT)) / 100.0
                    _breached = ((direction == "long"  and price <= _floor) or
                                 (direction == "short" and price >= _floor))
                    _recovered = ((direction == "long"  and price >= _floor * (1 + _rearm)) or
                                  (direction == "short" and price <= _floor * (1 - _rearm)))
                    if _recovered and st.get("floor_alerted"):
                        st["floor_alerted"] = False
                    if _breached and not st.get("floor_alerted"):
                        st["floor_alerted"] = True
                        _perp_pos_event(market, existing, "floor_breach", price, leveraged_pct)
                        _room = (abs(price - float(_venue_liq)) / price * 100) if _venue_liq else None
                        _room_s = f"Still {_room:.1f}% of room to REAL liquidation.\n" if _room is not None else ""
                        await notify(
                            f"\u26a0\ufe0f <b>EXIT FLOOR REACHED \u2014 {sym} {direction.upper()} {leverage}x</b>\n"
                            f"{sym} @ ${price:.4f} | our floor ${_floor:.4f} | venue liq ${float(_venue_liq):.4f}\n"
                            f"liq source: {_liq_src}\n" + _room_s +
                            f"Unrealized: {leveraged_pct:+.1f}% gross, {net_lev_pct:+.1f}% net of fees (${pnl_usd:+.2f})\n"
                            f"<b>POSITION REMAINS OPEN \u2014 nothing auto-closes.</b> "
                            f"Deposit collateral (widens liq, lowers lev), cut, or ride with a reason."
                        )
                        try:
                            _sz = float(existing.get("size_usd") or 0)
                            _co = float(existing.get("collateral") or 0)
                            _dep = []
                            for _add in (1, 2, 3, 5):
                                if _sz > 0 and (_co + _add) > 0:
                                    _nl = calc_liq_from_account(entry, _sz, _co + _add, direction)
                                    _dep.append({"deposit_usd": _add, "new_collateral": round(_co + _add, 2),
                                                 "new_leverage": round(_sz / (_co + _add), 2),
                                                 "new_venue_liq": _nl,
                                                 "new_exit_floor": calc_exit_floor(_nl, direction, entry=entry, tp=tp_price) if _nl else None})
                            _ctx = None
                            try:
                                _all = await build_perp_packets()
                                if isinstance(_all, dict):
                                    _ctx = _all.get(sym) or _all.get(market)
                            except Exception as _pe:
                                print(f"[perp monitor] floor ctx err: {_pe}")
                            _fp = {"from": "perps_bot", "type": "exit_floor_breach", "market": market,
                                   "direction": direction, "leverage": leverage, "price": price,
                                   "exit_floor": _floor, "venue_liq_price": float(_venue_liq),
                                   "liq_price_source": _liq_src,
                                   "pct_room_to_real_liq": round(_room, 2) if _room is not None else None,
                                   "unrealized_pct": leveraged_pct, "unrealized_usd": pnl_usd,
                                   "cut_now_costs_usd": round(pnl_usd, 2),
                                   "deposit_ladder": _dep,
                                   "chain_context": (_ctx or {}).get("chain_context") if isinstance(_ctx, dict) else None,
                                   "managing_view": _ctx if isinstance(_ctx, dict) else None,
                                   "generated_at": datetime.now(EST).isoformat(),
                                   "doctrine": ("This is OUR floor, not the venue's — you still have room. "
                                                "Nothing auto-closes. deposit_ladder shows what each collateral "
                                                "add does to leverage and liq: size stays, ENTRY STAYS. Adding "
                                                "SIZE instead re-averages entry and moves every % distance "
                                                "(TP, SL, floor) with it. Different trade."),
                                   "ask": "floor reached \u2014 deposit collateral / cut / ride? say which and why"}
                            _fpp = os.path.join(INBOX_DIR, f"perps_bot - exit_floor_{market.replace('-','_')}_{datetime.now(EST).strftime('%H%M%S')}.json")
                            with open(_fpp, "w", encoding="utf-8") as _ff:
                                json.dump(_fp, _ff, indent=2, default=str)
                        except Exception as _fe:
                            print(f"[perp monitor] floor packet err: {_fe}")


                # ── SL LEVEL — alert the decider (position stays) ─────────────
                if sl_price and (
                    (direction == "long"  and price <= float(sl_price)) or
                    (direction == "short" and price >= float(sl_price))
                ):
                    # ── OWNER'S DOCTRINE (2026-07-24, the phantom-close incident):
                    # "nothing auto-closes without the agent firing — the only
                    # auto-close is actual liquidation protection." The old
                    # branch recorded a FICTIONAL realized PnL and cleared the
                    # book with NO on-chain close — the chain position rode on,
                    # unmonitored. A level hitting is a DECISION REQUEST that
                    # travels to the decider. Position stays; book stays.
                    _now_m = time.time()
                    if _now_m - (st.get("sl_alerted_at") or 0) > 1800:
                        st["sl_alerted_at"] = _now_m
                        _perp_pos_event(market, existing, "sl_hit", price, leveraged_pct)
                        await notify(
                            f"🛑 <b>SL LEVEL REACHED — {sym} {direction.upper()} {leverage}x</b>\n"
                            f"{sym} @ ${price:.4f} | your SL @ ${float(sl_price):.4f}\n"
                            f"Unrealized: {leveraged_pct:+.1f}% gross, {net_lev_pct:+.1f}% net of fees (${pnl_usd:+.2f})\n"
                            f"<b>POSITION REMAINS OPEN.</b> Your standing level hit — fire the close "
                            f"(/perpclose or close signal), move the SL, or hold with a reason. "
                            f"Nothing auto-closes except liquidation protection."
                        )
                        try:
                            _lvl = {"from": "perps_bot", "type": "level_hit", "level": "sl",
                                     "market": market, "direction": direction,
                                     "price": price, "sl_price": float(sl_price),
                                     "unrealized_pct": leveraged_pct, "unrealized_usd": pnl_usd,
                                     "generated_at": datetime.now(EST).isoformat(),
                                     "ask": "your SL level hit — close, adjust, or hold (say which)"}
                            _lp = os.path.join(INBOX_DIR, f"perps_bot - level_hit_sl_{market.replace('-','_')}_{datetime.now(EST).strftime('%H%M%S')}.json")
                            with open(_lp, "w", encoding="utf-8") as _lf:
                                json.dump(_lvl, _lf, indent=2)
                        except Exception as _le:
                            print(f"[perp monitor] level packet err: {_le}")
                    continue  # closed; skip further checks for this chain

                # ── deep-underwater liquidation risk ───────────────────────────
                if leveraged_pct <= -80 and not st["liq_alerted"]:
                    st["liq_alerted"] = True
                    _perp_mark_alert_sent(market, existing, "liq")
                    _perp_pos_event(market, existing, "liq_warn", price, leveraged_pct)
                    await notify(
                        f"🚨 <b>LIQUIDATION RISK — {sym} {direction.upper()} {leverage}x</b>\n"
                        f"Leveraged loss: {leveraged_pct:.1f}% (${pnl_usd:.2f})\n"
                        f"Check https://jup.ag/perps NOW"
                    )

                # ─── SPRINT 33: daily_read exit-watch (per open chain) ──────────
                # regime_break / critical exit signals -> ALERT + audit packet.
                # Never an autonomous close; the agent makes the call.
                try:
                    _dr_exit = load_daily_read()  # cached, 10min TTL
                    if _dr_exit and isinstance(_dr_exit, dict):
                        _regime_break_now = bool(_dr_exit.get("regime_break_signal", False))
                        _rec = _dr_exit.get("trade_recommendation") or {}
                        _exit_priority = _rec.get("exit_priority_signals") or []
                        _abort_conditions = _rec.get("abort_conditions") or []

                        _alert_key = (_regime_break_now, len(_exit_priority), len(_abort_conditions))
                        _last_alert_key = existing.get("_last_daily_read_alert_key")
                        _has_signal = bool(_regime_break_now or _exit_priority or _abort_conditions)
                        _should_alert = _has_signal and (_alert_key != tuple(_last_alert_key) if _last_alert_key else True)

                        if _should_alert:
                            _alert_lines = [
                                f"⚠️ <b>daily_read exit signal — open {sym} {direction.upper()} {leverage}x</b>",
                                f"{sym} @ ${price:.4f} | PnL: {leveraged_pct:+.1f}% (${pnl_usd:+.2f})",
                            ]
                            if _regime_break_now:
                                _alert_lines.append("🚨 REGIME BREAK detected — consider closing")
                            if _abort_conditions:
                                _first_abort = _abort_conditions[0] if isinstance(_abort_conditions[0], str) else str(_abort_conditions[0])
                                _alert_lines.append(f"🛑 Abort condition: {_first_abort[:120]}")
                            if _exit_priority:
                                _first_ep = _exit_priority[0] if isinstance(_exit_priority[0], str) else str(_exit_priority[0])
                                _alert_lines.append(f"📌 Exit priority: {_first_ep[:120]}")
                                if len(_exit_priority) > 1:
                                    _alert_lines.append(f"  + {len(_exit_priority)-1} more priority signal(s)")
                            _alert_lines.append("")
                            _alert_lines.append("This is an ALERT, not autonomous close. The agent makes the call.")
                            await notify("\n".join(_alert_lines))

                            _exit_audit = {
                                "_type":              "perps_daily_read_exit_alert",
                                "_source":            "perps_bot",
                                "alerted_at":         datetime.now(EST).isoformat(),
                                "market":             market,
                                "open_position": {
                                    "market":    market,
                                    "direction": direction,
                                    "leverage":  leverage,
                                    "size_usd":  size_usd,
                                    "entry":     entry,
                                    "current":   price,
                                    "pnl_pct":   round(net_lev_pct, 2),
                                    "gross_pnl_pct": round(leveraged_pct, 2),
                                    "pnl_usd":   round(pnl_usd, 2),
                                },
                                "daily_read_triggers": {
                                    "regime_break_signal":  _regime_break_now,
                                    "abort_conditions":     _abort_conditions,
                                    "exit_priority_signals": _exit_priority,
                                },
                                "decision_authority": "The agent — bot only alerts, never autonomous close on daily_read",
                            }
                            _write_audit_to_inbox_results(_exit_audit)

                            # remember what we alerted on so we don't spam every cycle
                            existing["_last_daily_read_alert_key"] = list(_alert_key)
                            save_position(existing, market=market)
                except Exception as _dre:
                    print(f"[daily_read exit-watch] {market} err: {_dre}")
                # ─── END SPRINT 33 daily_read exit-watch ───────────────────────

            await asyncio.sleep(poll)

        except Exception as e:
            await notify_error("perp monitor error", e)
            await asyncio.sleep(10)

# ── telegram commands — relay mode (reads perp_command.json written by the agent) ──
PERP_CMD_FILE = os.path.join(WS_BOT_LOGS, "perp_command.json")

async def telegram_listener(connection: AsyncClient):
    print("Telegram command listener started — relay mode (reading perp_command.json)")
    last_ts = None
    while True:
        try:
            if os.path.exists(PERP_CMD_FILE):
                with open(PERP_CMD_FILE) as f:
                    data = json.load(f)
                ts   = data.get("ts")
                text = data.get("command", "").strip().lower()
                if ts and ts != last_ts and text:
                    last_ts = ts
                    print(f"[relay] received command: {text}")

                    if text == "/perp":
                        _am, existing = active_position()
                        sol_price = await get_sol_price()
                        balance   = await get_sol_balance(connection)
                        if existing and existing.get("direction"):
                            direction = existing["direction"]
                            entry     = float(existing.get("entry_price", 0))
                            leverage  = float(existing.get("leverage", 1))
                            size_usd  = existing.get("size_usd", 0)
                            liq_price = existing.get("liq_price") or calc_liq_price(entry, direction, leverage)
                            breakeven = existing.get("breakeven") or calc_breakeven_price(entry, direction, size_usd, existing.get("base_amount", size_usd/entry if entry else 1))
                            opened_at = existing.get("timestamp")
                            hold_str  = ""
                            if opened_at:
                                try:
                                    opened_dt = datetime.fromisoformat(opened_at)
                                    if opened_dt.tzinfo is None:
                                        opened_dt = opened_dt.replace(tzinfo=EST)
                                    held_mins = int((datetime.now(EST) - opened_dt).total_seconds() / 60)
                                    hold_str = f"{held_mins}m"
                                except Exception:
                                    pass
                            if sol_price and entry:
                                pct        = (sol_price - entry) / entry * 100 if direction == "long" else (entry - sol_price) / entry * 100
                                lev_pct    = pct * leverage
                                gross_pnl  = size_usd * (pct / 100)  # notional already carries leverage
                                net_pnl, fees = calc_net_pnl(gross_pnl, size_usd)
                                liq_dist   = abs(sol_price - liq_price) / sol_price * 100
                                liq_warn   = " ⚠️ NEAR LIQ" if liq_dist < 10 else ""
                            else:
                                lev_pct = gross_pnl = net_pnl = fees = 0
                                liq_dist = 100
                                liq_warn = ""
                            await notify(
                                f"📊 <b>Perp Position (Jupiter)</b>\n"
                                f"{direction.upper()} {leverage}x | ${size_usd:.2f} | held {hold_str}\n"
                                f"Entry: ${entry:.2f} | Now: ${sol_price:.2f}\n"
                                f"Breakeven: ${breakeven:.2f}\n"
                                f"Liq: ${liq_price:.2f} ({liq_dist:.1f}% away){liq_warn}\n"
                                f"Leveraged P&L: {lev_pct:+.1f}%\n"
                                f"Gross: ${gross_pnl:+.2f} | Fees: -${fees:.3f} | <b>Net: ${net_pnl:+.2f}</b>\n"
                                f"TP: ${existing.get('tp_price','none')} | SL: ${existing.get('sl_price','none')}\n"
                                f"Wallet: {balance:.4f} SOL"
                            )
                        else:
                            await notify(
                                f"📊 No open perp position\n"
                                f"Wallet: {balance:.4f} SOL | SOL: ${sol_price:.2f if sol_price else '?'}\n"
                                f"Exchange: Jupiter Perps"
                            )

                    elif text == "/perpstats":
                        if os.path.exists(PNL_FILE):
                            with open(PNL_FILE) as f:
                                pdata = json.load(f)
                            trades     = pdata.get("trades", [])
                            total_net  = pdata.get("total_realized_usd", 0)
                            total_fees = pdata.get("total_fees_usd", 0)
                            total_gross = sum(t.get("gross_pnl_usd", t.get("pnl_usd", 0)) for t in trades)
                            wins       = [t for t in trades if t.get("pnl_usd", 0) > 0]
                            losses     = [t for t in trades if t.get("pnl_usd", 0) < 0]
                            wr         = len(wins) / len(trades) * 100 if trades else 0
                            avg_win    = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
                            avg_loss   = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
                            best       = max(trades, key=lambda t: t.get("pnl_usd", 0)) if trades else None
                            worst      = min(trades, key=lambda t: t.get("pnl_usd", 0)) if trades else None
                            await notify(
                                f"📈 <b>Perp Stats (Jupiter)</b>\n"
                                f"Total trades: {len(trades)} | Win rate: {wr:.1f}%\n"
                                f"Gross PnL: ${total_gross:+.2f}\n"
                                f"Total fees paid: -${total_fees:.3f}\n"
                                f"<b>Net PnL: ${total_net:+.2f}</b>\n"
                                f"Avg win: ${avg_win:+.2f} | Avg loss: ${avg_loss:+.2f}\n"
                                + (f"Best trade: ${best['pnl_usd']:+.2f} ({best['direction']})\n" if best else "")
                                + (f"Worst trade: ${worst['pnl_usd']:+.2f} ({worst['direction']})" if worst else "")
                            )
                        else:
                            await notify("📈 No perp trades recorded yet")

                    elif text == "/perpclose":
                        _am, existing = active_position()
                        if not existing or not existing.get("direction"):
                            await notify("No open perp position to close")
                        else:
                            await notify(f"⏳ Closing {existing['direction'].upper()} position on Jupiter...")
                            await process_signal({
                                "action": "close", "market": _pos_base(_am),
                                "side": existing.get("direction"),
                                "reason": "manual close via Telegram"
                            })

                    elif text.startswith("/perptp "):
                        try:
                            new_tp = float(text.split()[1])
                            _am, existing = active_position()
                            if not existing or not existing.get("direction"):
                                await notify("No open position to update TP")
                            else:
                                existing["tp_price"] = new_tp
                                save_position(existing)
                                await notify(f"✅ TP updated to ${new_tp:.2f}")
                        except (IndexError, ValueError):
                            await notify("Usage: /perptp <price>  e.g. /perptp 95.50")

                    elif text.startswith("/perpsl "):
                        try:
                            new_sl = float(text.split()[1])
                            _am, existing = active_position()
                            if not existing or not existing.get("direction"):
                                await notify("No open position to update SL")
                            else:
                                existing["sl_price"] = new_sl
                                save_position(existing)
                                await notify(f"✅ SL updated to ${new_sl:.2f}")
                        except (IndexError, ValueError):
                            await notify("Usage: /perpsl <price>  e.g. /perpsl 78.00")

                    elif text == "/perptail":
                        _am, existing = active_position()
                        if not existing or not existing.get("direction"):
                            await notify("No open position")
                        else:
                            sol_price = await get_sol_price()
                            direction = existing["direction"]
                            entry     = float(existing.get("entry_price", 0))
                            leverage  = float(existing.get("leverage", 1))
                            size_usd  = existing.get("size_usd", 0)
                            if sol_price and entry:
                                raw_pct       = (sol_price - entry) / entry * 100 if direction == "long" else (entry - sol_price) / entry * 100
                                leveraged_pct = raw_pct * leverage
                                pnl_usd       = size_usd * (raw_pct / 100)   # Sprint 68: leverage once
                            else:
                                leveraged_pct = 0; pnl_usd = 0
                            await notify(
                                f"🔔 <b>Trailing Stop Config</b>\n"
                                f"Activates at: +{trail_levels(leverage)[0]:g}% of margin (base {TRAIL_ACTIVATE_PCT:g} at {PERP_TRAIL_REF_LEVERAGE:g}x, scale {PERP_TRAIL_LEV_SCALE}, {leverage:g}x now)\n"
                                f"Fires if drops: {trail_levels(leverage)[1]:g} pts from peak\n"
                                f"Current P&L (gross): {leveraged_pct:+.1f}% (${pnl_usd:+.2f})\n"
                                f"Status: {'ACTIVE' if leveraged_pct >= trail_levels(leverage)[0] else f'activates at +{trail_levels(leverage)[0]:g}%'}"
                            )

                    elif text == "/perphelp":
                        await notify(
                            f"<b>Perps Bot Commands</b>\n"
                            f"/perp — position status + live PnL\n"
                            f"/perpclose — close position now\n"
                            f"/perptp &lt;price&gt; — update take profit\n"
                            f"/perpsl &lt;price&gt; — update stop loss\n"
                            f"/perptail — trailing stop status\n"
                            f"/perpstats — trade history + win rate"
                        )

        except Exception as e:
            print(f"relay listener error: {e}")
        await asyncio.sleep(2)

# ── main ───────────────────────────────────────────────────────────────────────
def load_json_file(path, default):
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


# ══════════════════════════════════════════════════════════════════════════════
# MISSED-PERP-MOVE AUTOPSY (decline calibration engine)
# ══════════════════════════════════════════════════════════════════════════════
# Every setup intel_scanner surfaces that agent does NOT execute is tracked for
# a rolling 6h window. Each 15-min scan re-simulates the leveraged (50x) outcome
# BOTH directions from the setup's entry price against the current SOL price.
#
# Grading (leveraged-outcome, not raw %):
#   - would have >= 2.0x'd the money at 50x in the flagged direction  -> MISSED
#   - < 1.75x (chop / barely moved)                                   -> CORRECT DECLINE
#   - 1.75x - 2.0x (close, didn't clear the bar)                      -> NEUTRAL (no-action correct)
#   - moved adverse (would have lost)                                 -> GOOD PASS (saved from loss)
#
# A MISS gets an individual autopsy packet (deep lesson). Correct declines /
# good passes are NOT individually packeted (no noise) — they roll into the 6h
# batch calibration summary so the agent sees his decline accuracy without spam.
#
# DOCTRINE: this grades DECISIONS, not just misses. A correct decline is a WIN
# worth reinforcing — it's how the agent learns his discipline is sound, not just
# where he slipped. Calibration, not regret. A miss is a hypothesis to learn
# from ("what did I under-weight?"), never a verdict that he should have chased.
# ══════════════════════════════════════════════════════════════════════════════

MISSED_PERP_AUTOPSY_FILE = os.path.join(WS_BOT_LOGS, "missed_perp_autopsy.json")
MISSED_PERP_WINDOW_HOURS = 6.0
MISSED_PERP_SIM_LEVERAGE = 50.0
MISSED_PERP_MISS_MULT    = 2.0    # >= 2x leveraged = a real miss
MISSED_PERP_NEUTRAL_MULT = 1.75   # < 1.75x = clearly a correct decline


def _missed_perp_load() -> dict:
    """State: {window_started: iso, setups: [ ... ]}."""
    default = {"window_started": _now_iso(), "setups": []}
    data = load_json_file(MISSED_PERP_AUTOPSY_FILE, default)
    if not isinstance(data, dict) or "setups" not in data:
        return default
    return data


def _missed_perp_save(state: dict) -> None:
    try:
        _ps = globals().setdefault("_PERPS_SAVE_SEQ", 0) + 1

        globals()["_PERPS_SAVE_SEQ"] = _ps

        tmp = f"{MISSED_PERP_AUTOPSY_FILE}.{os.getpid()}.{_ps}.tmp"  # Sprint 54: unique tmp (shared name raced)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, MISSED_PERP_AUTOPSY_FILE)
    except Exception as e:
        print(f"[missed_perp] save err: {e}")


def missed_perp_record_setup(direction: str, sol_price: float, confidence, reasons: list) -> None:
    """
    Called when intel_scanner surfaces a setup the agent has not acted on
    (every one — the bot no longer trades on its own, so every surfaced setup is
    a candidate decline until the agent acts). Logs it into the rolling window with
    its entry price so the leveraged outcome can be simulated each scan.
    """
    try:
        if not sol_price or sol_price <= 0:
            return
        state = _missed_perp_load()
        state["setups"].append({
            "id":          f"setup_{direction}_{datetime.now(EST).strftime('%Y%m%d_%H%M%S')}",
            "direction":   direction,
            "entry_price": float(sol_price),
            "confidence":  confidence,
            "reasons":     reasons,
            "fired_at":    _now_iso(),
            "fired_ts":    _s39_now_ts(),
            "resolved":    False,   # set True once it hits a miss verdict (so we don't re-fire)
            "peak_mult":   1.0,     # best leveraged multiple seen in the flagged direction
            "verdict":     None,    # filled at window close if never a miss
        })
        _missed_perp_save(state)
    except Exception as e:
        print(f"[missed_perp] record err: {e}")


def _missed_perp_leveraged_mult(direction: str, entry: float, current: float, lev: float = MISSED_PERP_SIM_LEVERAGE) -> float:
    """
    Simulated multiple-of-money for a `lev`x position in `direction`, entry->current.
    long: gains as price rises. short: gains as price falls.
    Returns money multiple (1.0 = breakeven, 2.0 = doubled, <1 = down).
    """
    if entry <= 0 or current <= 0:
        return 1.0
    raw_pct = (current - entry) / entry           # +ve = price up
    dir_pct = raw_pct if direction == "long" else -raw_pct
    return 1.0 + (dir_pct * lev)


async def missed_perp_evaluate(current_sol_price: float) -> None:
    """
    Called every scan cycle with the fresh SOL price. For each open setup in the
    window: update peak leveraged multiple; if it crossed the 2x miss bar in its
    flagged direction, fire an individual miss autopsy and mark resolved. At the
    6h window boundary, grade everything, fire the batch calibration summary,
    reset the window.
    """
    try:
        if not current_sol_price or current_sol_price <= 0:
            return
        state = _missed_perp_load()
        setups = state.get("setups", [])

        # update peak multiples + fire misses
        for su in setups:
            if su.get("resolved"):
                continue
            mult = _missed_perp_leveraged_mult(su["direction"], su["entry_price"], current_sol_price)
            if mult > su.get("peak_mult", 1.0):
                su["peak_mult"] = round(mult, 3)
            if mult >= MISSED_PERP_MISS_MULT:
                su["resolved"] = True
                su["verdict"]  = "missed"
                await _fire_missed_perp_autopsy(su, current_sol_price, mult)

        # window boundary check
        try:
            started = datetime.fromisoformat(state.get("window_started"))
        except Exception:
            started = datetime.now(EST)
        age_h = (datetime.now(EST) - started).total_seconds() / 3600.0

        if age_h >= MISSED_PERP_WINDOW_HOURS:
            # final-grade anything not already a miss, fire batch summary, reset
            for su in setups:
                if su.get("verdict"):
                    continue
                pk = su.get("peak_mult", 1.0)
                if pk >= MISSED_PERP_MISS_MULT:
                    su["verdict"] = "missed"   # safety net (should've fired already)
                elif pk < MISSED_PERP_NEUTRAL_MULT:
                    su["verdict"] = "correct_decline"
                else:
                    su["verdict"] = "neutral"  # 1.75-2.0x, close but didn't clear the bar
            # adverse check for "good pass": if the flagged direction went underwater
            for su in setups:
                if su.get("verdict") == "correct_decline":
                    mult = _missed_perp_leveraged_mult(su["direction"], su["entry_price"], current_sol_price)
                    if mult < 1.0:
                        su["verdict"] = "good_pass"
            await _fire_missed_perp_batch_summary(setups, current_sol_price)
            _missed_perp_save({"window_started": _now_iso(), "setups": []})
            return

        _missed_perp_save(state)
    except Exception as e:
        print(f"[missed_perp] evaluate err: {e}")


async def _fire_missed_perp_autopsy(setup: dict, current_price: float, mult: float) -> None:
    """Individual deep-lesson packet for a setup the agent passed on that 2x'd at 50x."""
    try:
        os.makedirs(INBOX_DIR, exist_ok=True)
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        direction = setup["direction"]
        entry = setup["entry_price"]
        move_pct = ((current_price - entry) / entry * 100.0) if entry else 0.0
        packet = {
            "id":          f"missed_perp_{direction}_{ts}",
            "type":        "missed_perp_autopsy",
            "from":        "perps_bot.missed_perp_autopsy",
            "to":          "agent",
            "fired_at":    _now_iso(),
            "market":      "SOL-PERP",
            "direction":   direction,
            "entry_price": entry,
            "current_price": round(current_price, 4),
            "raw_move_pct": round(move_pct, 2),
            "leveraged_mult_at_50x": round(mult, 2),
            "confidence_at_fire": setup.get("confidence"),
            "reasons_at_fire": setup.get("reasons", []),
            "fired_at_setup": setup.get("fired_at"),
            "lesson_prompt": (
                f"intel_scanner surfaced a {direction.upper()} setup on SOL-PERP at "
                f"${entry:.2f} (confidence {setup.get('confidence')}). You did not take it. "
                f"SOL has since moved to ${current_price:.2f} ({move_pct:+.1f}%), which at 50x "
                f"would have been {mult:.1f}x the money. This is a HYPOTHESIS to learn from, "
                f"NOT a verdict that you should have chased it. Honest question: was there "
                f"signal in the setup ({', '.join(setup.get('reasons', []))[:200]}) that you "
                f"under-weighted, or was this a coin flip that happened to land? If there was "
                f"a real edge you missed, name it and append the lesson to LEARNINGS.md per "
                f"perps-boss doctrine. If it was genuinely a coin flip, that's also a valid "
                f"finding: record that passing was correct process even though it would have won."
            ),
            "instruction": (
                "Process per perps-boss / forensics. Decide: real missed edge (append lesson) "
                "or correct-process-bad-luck (note and move on). Do NOT let this train you to "
                "chase every move — calibration, not regret."
            ),
        }
        path = os.path.join(INBOX_DIR, f"perps_bot - missed_perp_autopsy_{direction}_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(packet, f, indent=2, default=str)
        print(f"[missed_perp] MISS autopsy fired: {os.path.basename(path)} ({mult:.1f}x at 50x)")
    except Exception as e:
        print(f"[missed_perp] autopsy write err: {e}")


async def _fire_missed_perp_batch_summary(setups: list, current_price: float) -> None:
    """One calibration summary per 6h window. Covers correct declines / neutrals /
    good passes / misses so the agent sees his decline accuracy — no per-decline noise."""
    try:
        if not setups:
            return
        os.makedirs(INBOX_DIR, exist_ok=True)
        ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
        counts = {"missed": 0, "correct_decline": 0, "neutral": 0, "good_pass": 0}
        for su in setups:
            v = su.get("verdict") or "neutral"
            counts[v] = counts.get(v, 0) + 1
        total = len(setups)
        correct = counts["correct_decline"] + counts["good_pass"] + counts["neutral"]
        accuracy = round(100.0 * correct / total, 1) if total else 0.0
        packet = {
            "id":        f"perp_decline_calibration_{ts}",
            "type":      "perp_decline_calibration",
            "from":      "perps_bot.missed_perp_autopsy",
            "to":        "agent",
            "fired_at":  _now_iso(),
            "window_hours": MISSED_PERP_WINDOW_HOURS,
            "setups_surfaced": total,
            "verdicts":  counts,
            "decline_accuracy_pct": accuracy,
            "misses":    [
                {"direction": su["direction"], "entry": su["entry_price"],
                 "peak_mult_50x": su.get("peak_mult"), "reasons": su.get("reasons", [])}
                for su in setups if su.get("verdict") == "missed"
            ],
            "instruction": (
                f"6h decline calibration: intel_scanner surfaced {total} setups, you took none. "
                f"{correct}/{total} were correct to decline ({accuracy}% decline accuracy) — "
                f"{counts['correct_decline']} chopped, {counts['good_pass']} would have lost, "
                f"{counts['neutral']} close-but-under-bar. {counts['missed']} would have 2x'd at 50x "
                f"(individual autopsies already fired for those). This is your discipline scorecard: "
                f"a high decline-accuracy means sitting out is serving you. Only the misses are lessons; "
                f"the correct declines are CONFIRMATION your patience is sound. Append a one-line "
                f"calibration note to LEARNINGS.md if the accuracy shifted meaningfully from prior windows."
            ),
        }
        path = os.path.join(INBOX_DIR, f"perps_bot - perp_decline_calibration_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(packet, f, indent=2, default=str)
        print(f"[missed_perp] batch calibration fired: {total} setups, {accuracy}% decline accuracy")
    except Exception as e:
        print(f"[missed_perp] batch summary write err: {e}")


def save_decision(decision: dict):
    """Log perp decision to perp_decisions.json for the agent to review."""
    try:
        decisions = load_json_file(PERP_DECISION_LOG, [])
        decisions.append(decision)
        decisions = decisions[-200:]
        _ps = globals().setdefault("_PERPS_SAVE_SEQ", 0) + 1

        globals()["_PERPS_SAVE_SEQ"] = _ps

        tmp = f"{PERP_DECISION_LOG}.{os.getpid()}.{_ps}.tmp"  # Sprint 54: unique tmp (shared name raced)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(decisions, f, indent=2, default=str)
        os.replace(tmp, PERP_DECISION_LOG)
    except Exception as e:
        print(f"[perp intel] decision log error: {e}")


async def intelligence_scanner():
    """
    Perp signal scanner (alerts only) — runs every 15 min.
    Reads feed cycle + Polymarket + news catalysts + funding rate.
    Confluence scanner: ALERTS the agent on entry/exit signals. Never auto-executes;
    every trade is the agent writing a signal file. (ENTRY_ALERTS_ENABLED gates
    whether entry alerts fire; it never places a trade.)
    Also acts as position decider — alerts the agent on reversal signals.
    """
    print(f"[perp intel] intelligence scanner started — every {SCAN_INTERVAL_SECONDS}s")
    prev_score = None

    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

            # ── read all data sources ─────────────────────────────────────────
            macro      = _feed("macro", {})
            catalysts  = load_json_file(NEWS_CATALYSTS_FILE, {})
            polymarket = load_json_file(POLYMARKET_FILE, {})

            score         = float(macro.get("blowoff_score", 50) or 50)
            signal        = macro.get("blowoff_signal", "HOLD")
            phase         = macro.get("cycle_phase", "Unknown")
            fg            = float(macro.get("fear_greed", 50) or 50)
            funding       = macro.get("funding_rate")
            btc_momentum  = float(macro.get("btc_momentum", 50) or 50)
            btc_price     = float(macro.get("btc_price") or 0)

            # get current SOL price
            sol_price = await get_sol_price()
            if not sol_price:
                print("[perp intel] could not get SOL price — skipping scan")
                continue

            # Missed-perp-move autopsy: re-simulate every open setup's leveraged
            # outcome against the fresh price; fire miss autopsies / 6h batch summary.
            await missed_perp_evaluate(sol_price)

            # check existing position — FIELD FIX (2026-07-24, the owner: "the log
            # doesn't show the position — it's chained to something not used
            # anymore"): load_position() bare defaults to SOL-PERP (the legacy
            # single-market slot), so the scanner was blind to the live ETH
            # position and evaluated the tape as if flat. Multi-market truth:
            # ALL open positions; `existing` = the largest by size so the
            # downstream correlation math keeps working on the dominant bag.
            _all_pos = load_positions()
            has_position = bool(_all_pos)
            existing = None
            if _all_pos:
                _dom_mkt = max(_all_pos, key=lambda m: float(_all_pos[m].get("size_usd") or 0))
                existing = _all_pos[_dom_mkt]

            now_str = datetime.now(EST).strftime("%Y-%m-%d %H:%M EST")
            _pos_str = ", ".join(f"{m.replace('-PERP','')} {p.get('direction','?')}"
                                   for m, p in _all_pos.items()) if _all_pos else "none"
            print(f"[perp intel] scan | score={score} signal={signal} phase={phase} F&G={fg} funding={funding} | position={_pos_str}")
            # The agent's upgrade #3 (2026-07-23): funding is half the perp edge and
            # market_now.funding_rate_pct was null whenever macro lacked the key —
            # while THIS scan holds it live. Cache it for the packet builder.
            try:
                globals().setdefault("_LIVE_FUNDING_CACHE", {})["last"] = {
                    "funding": funding, "at": datetime.now(EST).isoformat()}
            except Exception:
                pass

            # ── position decider — if in a trade, watch for reversals ─────────
            if has_position:
                direction   = existing.get("direction")
                entry_price = float(existing.get("entry_price", sol_price))
                leverage    = float(existing.get("leverage", _perp_get_override("AUTO_LEVERAGE", AUTO_LEVERAGE)))
                pct_change  = ((sol_price - entry_price) / entry_price) * 100
                lev_pct     = pct_change * leverage

                reversal_signals = []

                # long position reversal checks
                if direction == "long":
                    if score > 65 and signal in ("REDUCE", "SHORT", "CAUTION"):
                        reversal_signals.append(f"feed cycle turned bearish: score={score} signal={signal}")
                    if fg > 70:
                        reversal_signals.append(f"F&G entering greed territory: {fg}")
                    if funding and float(funding) > FUNDING_SHORT_MIN:
                        reversal_signals.append(f"funding rate high: {funding:.4f} — longs overleveraged")

                # short position reversal checks
                elif direction == "short":
                    if score < 35 and signal in ("WATCH-LONG", "LONG", "LONG_AGGRESSIVE"):
                        reversal_signals.append(f"feed cycle turned bullish: score={score} signal={signal}")
                    if fg < 25:
                        reversal_signals.append(f"F&G entering extreme fear: {fg} — capitulation buy signal")
                    if btc_momentum > 60:
                        reversal_signals.append(f"BTC momentum strong: {btc_momentum} — short may be squeeze risk")

                # HOLD TIME — computed ALWAYS (fix 2026-07-26). This was assigned
                # inside `if reversal_signals:` while the scalp take-profit check
                # below reads it unconditionally. On any clean scan (no reversal
                # flags — the normal case) it was never bound, so the scanner died
                # with UnboundLocalError BEFORE the TP suggestion could fire. It
                # only ever surfaced as a caught "scanner error" line, so the
                # take-profit path was silently dead, not visibly broken.
                hold_hours = 0.0
                position_timestamp = existing.get("timestamp", "")
                if position_timestamp:
                    try:
                        from datetime import datetime as _dt
                        opened = _dt.fromisoformat(position_timestamp)
                        hold_hours = (_dt.now() - opened).total_seconds() / 3600
                    except Exception:
                        pass

                if reversal_signals:

                    if len(reversal_signals) >= 2 and hold_hours >= AUTO_CLOSE_MIN_HOLD_HOURS:
                        # DOCTRINE: intel_scanner does NOT auto-close. Reversal signals are
                        # INFORMATION for the agent, not an order. The bot writes a close-suggestion
                        # alert packet to the agent's inbox; the agent decides whether to close. The
                        # autopsy later grades that decision, so this becomes learned exit timing
                        # instead of a hardcoded reflex. (Was: auto-wrote a close to perp_signal.json.
                        # Removed — autonomous closes violate the agent-decides doctrine.)
                        _rc_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        close_alert = {
                            "id":         f"intel_reversal_{direction}_{_rc_ts}",
                            "type":       "close_suggestion",
                            "from":       "perps_bot.intel_scanner",
                            "to":         "agent",
                            "fired_at":   _now_iso(),
                            "market":     "SOL-PERP",
                            "direction":  direction,
                            "sol_price":  sol_price,
                            "entry_price": entry_price,
                            "lev_pct":    round(lev_pct, 2),
                            "hold_hours": round(hold_hours, 1),
                            "reversal_signals": reversal_signals,
                            "instruction": (
                                f"{len(reversal_signals)} reversal flags hit on your {direction.upper()} "
                                "position. This is a heads-up, not a close order. Your call: close it "
                                "(write to perp_signal.json), tighten/adjust, or hold through. Nothing "
                                "fires until you decide. The autopsy will grade whatever you choose."
                            ),
                        }
                        try:
                            os.makedirs(INBOX_DIR, exist_ok=True)
                            _rc_path = os.path.join(INBOX_DIR, f"perps_bot - close_suggestion_{direction}_{_rc_ts}.json")
                            with open(_rc_path, "w", encoding="utf-8") as f:
                                json.dump(close_alert, f, indent=2)
                            print(f"[perp intel] close_suggestion packet written: {os.path.basename(_rc_path)}")
                        except Exception as _rc_e:
                            print(f"[perp intel] close_suggestion write err: {_rc_e}")
                        await notify(
                            f"🔔 <b>Perp Reversal Alert — {direction.upper()}</b>\n"
                            f"SOL: ${sol_price:.2f} | Entry: ${entry_price:.2f} | PnL: {lev_pct:+.1f}% (levered)\n"
                            f"Hold: {hold_hours:.1f}h | {len(reversal_signals)} flags: {' | '.join(reversal_signals)}\n"
                            f"<i>Heads-up only — the agent decides. Nothing auto-closed.</i>"
                        )
                        save_decision({"timestamp": now_str, "type": "reversal_alert", "direction": direction, "sol_price": sol_price, "entry_price": entry_price, "lev_pct": round(lev_pct, 2), "hold_hours": round(hold_hours, 1), "signals": reversal_signals})
                    elif len(reversal_signals) >= 2 and hold_hours < AUTO_CLOSE_MIN_HOLD_HOURS:
                        # strong signals but min hold not reached — alert only
                        await notify(
                            f"⚠️ <b>Perp Reversal Warning — {direction.upper()}</b>\n"
                            f"SOL: ${sol_price:.2f} | PnL: {lev_pct:+.1f}% (levered) | Hold: {hold_hours:.1f}h\n"
                            f"Signals: {' | '.join(reversal_signals)}\n"
                            f"Auto-close blocked — min hold {AUTO_CLOSE_MIN_HOLD_HOURS:.0f}h not reached. Watching."
                        )
                        save_decision({"timestamp": now_str, "type": "reversal_blocked_minhhold", "direction": direction, "hold_hours": round(hold_hours, 1), "signals": reversal_signals})
                    else:
                        # single signal — alert only, the agent decides
                        await notify(
                            f"⚠️ <b>Perp Reversal Alert — {direction.upper()}</b>\n"
                            f"SOL: ${sol_price:.2f} | Entry: ${entry_price:.2f} | PnL: {lev_pct:+.1f}% (levered)\n"
                            f"Signal: {reversal_signals[0]}\n"
                            f"Watching — use /perpclose to exit manually"
                        )
                        save_decision({"timestamp": now_str, "type": "reversal_alert", "direction": direction, "sol_price": sol_price, "entry_price": entry_price, "lev_pct": round(lev_pct, 2), "signals": reversal_signals})

                # scalp exit — in profit + score moving against position.
                # DOCTRINE: no autonomous close. This is a take-profit SUGGESTION to
                # The agent, not an order. (Was: auto-closed to bank gains. Removed — the agent
                # decides every exit; the autopsy grades the timing.)
                scalp_tp = get_perp_thresholds()["scalp_tp_pct"] * 100  # convert to pct
                if lev_pct >= scalp_tp and hold_hours >= AUTO_CLOSE_MIN_HOLD_HOURS:
                    _tp_hit = (direction == "long" and score > 65) or (direction == "short" and score < 35)
                    if _tp_hit:
                        _st_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        tp_alert = {
                            "id":         f"intel_scalp_tp_{direction}_{_st_ts}",
                            "type":       "take_profit_suggestion",
                            "from":       "perps_bot.intel_scanner",
                            "to":         "agent",
                            "fired_at":   _now_iso(),
                            "market":     "SOL-PERP",
                            "direction":  direction,
                            "sol_price":  sol_price,
                            "lev_pct":    round(lev_pct, 2),
                            "score":      score,
                            "hold_hours": round(hold_hours, 1),
                            "reason":     f"scalp TP zone: +{lev_pct:.1f}% levered, score={score} moving against position",
                            "instruction": (
                                f"Your {direction.upper()} scalp is +{lev_pct:.1f}% levered and the score "
                                f"({score}) is turning against it — classic take-profit zone. This is a "
                                "suggestion, not a close order. Your call: bank it (write to perp_signal.json), "
                                "let it ride, or trail. Nothing fires until you decide."
                            ),
                        }
                        try:
                            os.makedirs(INBOX_DIR, exist_ok=True)
                            _st_path = os.path.join(INBOX_DIR, f"perps_bot - take_profit_suggestion_{direction}_{_st_ts}.json")
                            with open(_st_path, "w", encoding="utf-8") as f:
                                json.dump(tp_alert, f, indent=2)
                            print(f"[perp intel] take_profit_suggestion packet written: {os.path.basename(_st_path)}")
                        except Exception as _st_e:
                            print(f"[perp intel] take_profit_suggestion write err: {_st_e}")
                        await notify(
                            f"🔔 <b>Perp Scalp TP Zone — {direction.upper()}</b>\n"
                            f"SOL: ${sol_price:.2f} | +{lev_pct:.1f}% levered | score={score}\n"
                            f"<i>Take-profit suggestion — the agent decides. Nothing auto-closed.</i>"
                        )

                prev_score = score
                continue  # don't open new position while one is open

            # ── no position — evaluate for entry ─────────────────────────────
            if not _perp_get_override("ENTRY_ALERTS_ENABLED", ENTRY_ALERTS_ENABLED):
                prev_score = score
                continue

            # overnight window check — no new positions 10pm-6am unless extreme
            hour = datetime.now(EST).hour
            overnight = 22 <= hour or hour < 6
            if overnight and not (score < 15 or score > 85):
                prev_score = score
                continue

            # ── check Polymarket for BTC/SOL sentiment ────────────────────────
            poly_movers = polymarket.get("movers", []) if isinstance(polymarket, dict) else []
            poly_bearish = any(
                m.get("direction") == "DOWN" and float(m.get("shift", 0)) >= 0.10
                and any(kw in (m.get("question") or "").lower() for kw in ["btc", "bitcoin", "sol", "crypto"])
                for m in poly_movers
            )
            poly_bullish = any(
                m.get("direction") == "UP" and float(m.get("shift", 0)) >= 0.10
                and any(kw in (m.get("question") or "").lower() for kw in ["btc", "bitcoin", "sol", "crypto"])
                for m in poly_movers
            )

            # ── check news catalysts ──────────────────────────────────────────
            active_cats = catalysts.get("active_catalysts", []) if isinstance(catalysts, dict) else []
            geo_catalyst = any(
                any(kw in (c.get("title") or "").lower() for kw in ["war", "iran", "russia", "sanctions", "conflict"])
                for c in active_cats
            )

            # ── score momentum (scalp detection) ─────────────────────────────
            perp_t = get_perp_thresholds()
            score_moved = abs(score - prev_score) if prev_score is not None else 0
            scalp_opportunity = score_moved >= perp_t["scalp_score_move"]

            # ── signal evaluation ─────────────────────────────────────────────
            trade_signal = None
            confidence   = 0
            reasons      = []

            # STRONG LONG — all conditions cluster
            long_conditions = [
                score <= perp_t["long_score_max"],
                signal in ("LONG", "LONG_AGGRESSIVE", "WATCH-LONG"),
                fg <= LONG_FG_MAX,
                phase in ("Depression", "Disbelief", "Anger", "Capitulation"),
                btc_momentum > 45,
            ]
            long_score = sum(long_conditions)

            # STRONG SHORT — all conditions cluster
            funding_val = float(funding) if funding else 0
            short_conditions = [
                score >= perp_t["short_score_min"],
                signal in ("SHORT", "REDUCE"),
                fg >= SHORT_FG_MIN,
                phase in ("Euphoria", "Complacency", "Thrill"),
                funding_val >= FUNDING_SHORT_MIN if funding else False,
            ]
            short_score = sum(short_conditions)

            # SCALP LONG — score dropped fast + momentum
            scalp_long_conditions = [
                scalp_opportunity and score < prev_score,
                score < 40,
                btc_momentum > 50,
                fg < 35,
                not geo_catalyst,
            ]
            scalp_long_score = sum(scalp_long_conditions)

            # SCALP SHORT — score spiked fast
            scalp_short_conditions = [
                scalp_opportunity and score > prev_score,
                score > 60,
                fg > 60,
                poly_bearish or geo_catalyst,
            ]
            scalp_short_score = sum(scalp_short_conditions)

            # determine trade
            # ── dynamic sizing from actual Drift balance ────────────────────────
            # never hardcode — read real collateral available then cap to 80%
            try:
                _avail_col = await get_wallet_balance_usd()
            except Exception:
                _avail_col = 0.0
            if _avail_col > 0:
                _safe_col     = _avail_col * 0.80          # 80% of available
                _full_col     = min(_safe_col, _perp_get_override("AUTO_COLLATERAL", AUTO_COLLATERAL))
                _scalp_col    = min(_safe_col * 0.6, _perp_get_override("AUTO_COLLATERAL", AUTO_COLLATERAL) * 0.6)
                _full_size    = _full_col * _perp_lev_for_collateral_usd(_full_col)
                _scalp_size   = _scalp_col * _perp_lev_for_collateral_usd(_scalp_col)
            else:
                # fall back to env defaults if balance unreadable
                _full_col  = _perp_get_override("AUTO_COLLATERAL", AUTO_COLLATERAL)
                _scalp_col = _perp_get_override("AUTO_COLLATERAL", AUTO_COLLATERAL) * 0.6
                _full_size = AUTO_SIZE_USD
                _scalp_size = AUTO_SIZE_USD * 0.6

            if long_score >= 4:
                trade_signal = "long"
                confidence   = long_score
                reasons      = [f"score={score}", f"signal={signal}", f"phase={phase}", f"F&G={fg}", f"btc_mom={btc_momentum}"]
                size_usd     = _full_size
                leverage     = _perp_lev_for_collateral_usd(_full_col)
                collateral   = _full_col
                tp_mult, sl_mult = 1.15, 0.93
            elif short_score >= 4:
                trade_signal = "short"
                confidence   = short_score
                reasons      = [f"score={score}", f"signal={signal}", f"phase={phase}", f"F&G={fg}", f"funding={funding}"]
                size_usd     = _full_size
                leverage     = _perp_lev_for_collateral_usd(_full_col)
                collateral   = _full_col
                tp_mult, sl_mult = 0.85, 1.07
            elif scalp_long_score >= 3 and poly_bullish:
                trade_signal = "long"
                confidence   = scalp_long_score
                reasons      = [f"SCALP: score dropped {score_moved:.0f}pts", f"score={score}", f"poly_bullish", f"btc_mom={btc_momentum}"]
                size_usd     = _scalp_size
                leverage     = _perp_lev_for_collateral_usd(_scalp_col)
                collateral   = _scalp_col
                tp_mult, sl_mult = 1.06, 0.97
            elif scalp_short_score >= 3 and poly_bearish:
                trade_signal = "short"
                confidence   = scalp_short_score
                reasons      = [f"SCALP: score spiked {score_moved:.0f}pts", f"score={score}", f"poly_bearish"]
                size_usd     = _scalp_size
                leverage     = _perp_lev_for_collateral_usd(_scalp_col)
                collateral   = _scalp_col
                tp_mult, sl_mult = 0.94, 1.03

            if trade_signal:
                tp_price = sol_price * tp_mult
                sl_price = sol_price * sl_mult
                reason   = f"intel_scanner: {' | '.join(reasons)}"

                print(f"[perp intel] {trade_signal.upper()} signal — confidence {confidence}/5 | {reason}")

                # save decision (an ALERT only; the bot never executes a trade on its own)
                save_decision({
                    "timestamp": now_str, "type": "position_alert",
                    "direction": trade_signal, "confidence": confidence,
                    "sol_price": sol_price, "score": score,
                    "reasons": reasons, "size_usd": size_usd
                })

                # DOCTRINE: intel_scanner does NOT auto-open positions. The agent is the
                # sole initiator of entries and sets his own params. intel_scanner only
                # PROPOSES — it writes a position-alert packet to the agent's inbox with its
                # read + SUGGESTED params (a starting point the agent can take, adjust, or
                # ignore). The agent decides whether to fire and with what params.
                # (Was: auto-wrote an execution signal to perp_signal.json. Removed —
                # autonomous leveraged entries violate the agent-decides doctrine.)
                _ia_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                alert_packet = {
                    "id":          f"intel_alert_{trade_signal}_{_ia_ts}",
                    "type":        "position_alert",
                    "from":        "perps_bot.intel_scanner",
                    "to":          "agent",
                    "fired_at":    _now_iso(),
                    "market":      "SOL-PERP",
                    "direction":   trade_signal,
                    "confidence":  f"{confidence}/5",
                    "sol_price":   sol_price,
                    "reason":      reason,
                    "reasons":     reasons,
                    "suggested_params": {
                        "size_usd":   size_usd,
                        "leverage":   leverage,
                        "collateral": collateral,
                        "tp_price":   round(tp_price, 2),
                        "sl_price":   round(sl_price, 2),
                    },
                    "instruction": (
                        "intel_scanner sees this setup. These params are a SUGGESTION, "
                        "not an order. Your call: open it (set your own params, write to "
                        "perp_signal.json), adjust the suggestion, or pass. Nothing fires "
                        "until you decide."
                    ),
                }
                try:
                    os.makedirs(INBOX_DIR, exist_ok=True)
                    _ia_path = os.path.join(INBOX_DIR, f"perps_bot - position_alert_{trade_signal}_{_ia_ts}.json")
                    with open(_ia_path, "w", encoding="utf-8") as f:
                        json.dump(alert_packet, f, indent=2)
                    print(f"[perp intel] position_alert packet written: {os.path.basename(_ia_path)}")
                except Exception as _ia_e:
                    print(f"[perp intel] position_alert write err: {_ia_e}")

                # Track this surfaced-but-not-executed setup for the missed-perp
                # autopsy. The bot never trades on its own, so every surfaced setup
                # is a decline candidate until the agent acts on it.
                missed_perp_record_setup(trade_signal, sol_price, confidence, reasons)

                await notify(
                    f"🔔 <b>Perp Intel Alert — {trade_signal.upper()} setup</b>\n"
                    f"SOL: ${sol_price:.2f} | Confidence: {confidence}/5\n"
                    f"Suggested: ${size_usd:.0f} @ {leverage}x | TP ${tp_price:.2f} / SL ${sl_price:.2f}\n"
                    f"Reason: {' | '.join(reasons)}\n"
                    f"<i>Proposal only — the agent decides. Nothing auto-fired.</i>"
                )
            else:
                # log HOLD decision — useful for calibration
                reasons_str = f"long={long_score}/5 short={short_score}/5 scalp_l={scalp_long_score}/5 scalp_s={scalp_short_score}/5"
                print(f"[perp intel] HOLD | {reasons_str}")
                save_decision({
                    "timestamp": now_str, "type": "hold",
                    "score": score, "fg": fg,
                    "long_score": long_score, "short_score": short_score,
                    "reasons": reasons_str
                })

            prev_score = score

        except Exception as e:
            print(f"[perp intel] scanner error: {e}")
            await asyncio.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
# SPRINT 27 — PACKET BUILDER + QUERY HANDLER + EXIT MONITOR + DAILY BIAS
# ══════════════════════════════════════════════════════════════════════════════
# New architecture: perps_bot.py is a full standalone bot.
#  - Hourly packet builder: reads feed's perp_market_edges, dispatches
#    through packet_analyzers.perps_edge, writes perp_packet.json to the agent's
#    inbox.
#  - Daily bias builder: runs at 9am EST, writes perp_daily_bias.json with
#    strategic bias that hourly packets check alignment against.
#  - Query handler: 14 actions for the agent to ask live questions.
#  - Exit monitor: watches open perp position, writes exit_suggestion.json to
#    The agent's inbox when triggers fire (trailing, time-based, invalidation).
#
# Files written:
#   inbox/perp_packet.json          — hourly opportunity packets (all markets)
#   inbox/perp_daily_bias.json      — daily strategic bias (1x/day)
#   inbox/perp_exit_suggestion.json — exit monitor alerts (continuous)
#   inbox/perp_response.json        — query responses to the agent
#
# Files read:
#   json_relay/perp_query.json      — queries from the agent
#   json_relay/perp_signal.json     — trade signals (existing, unchanged)
# ══════════════════════════════════════════════════════════════════════════════

import sys as _sprint27_sys
_sprint27_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORKSPACE_ROOT = WS_ROOT
BOT_DIR_PATH   = os.path.join(WORKSPACE_ROOT, "bots")
BOT_LOGS_PATH  = WS_BOT_LOGS
INBOX_PATH     = os.path.join(WORKSPACE_ROOT, "inbox")
JSON_RELAY     = WS_JSON_RELAY
INBOX_RESULTS  = os.path.join(WORKSPACE_ROOT, "inbox_results")

# Packet + query files
PERP_PACKET_FILE       = os.path.join(INBOX_PATH, "perp_packet.json")
PERP_DAILY_BIAS_FILE   = os.path.join(INBOX_PATH, "perp_daily_bias.json")
PERP_EXIT_SUGGEST_FILE = os.path.join(INBOX_PATH, "perp_exit_suggestion.json")
PERP_QUERY_FILE        = os.path.join(JSON_RELAY, "perp_query.json")
PERP_RESPONSE_FILE     = os.path.join(INBOX_PATH, "perp_response.json")

# Source files (feed, news, positions)
MACRO_JSON_PATH        = None  # retired: macro comes from the Flow feed hook (_macro())


def _macro() -> dict:
    """Macro snapshot from the Flow feed hook ({} when the feed is down)."""
    v = _feed("macro", {})
    return v if isinstance(v, dict) else {}


def _macro_age_min():
    return _feed_with_age("macro")[1]
NEWS_CATALYSTS_PATH    = os.path.join(BOT_LOGS_PATH, "news_catalysts.json")
AGENT_THESIS_PATH     = os.path.join(BOT_LOGS_PATH, "agent_thesis.json")
AGENT_OVERRIDES_PATH  = os.path.join(BOT_LOGS_PATH, "agent_overrides.json")
PERP_POSITION_PATH     = os.path.join(BOT_LOGS_PATH, "perp_positions.json")  # 2026-07-29: was singular dead path; live book is PLURAL keyed store
PERP_PNL_PATH          = os.path.join(BOT_LOGS_PATH, "perp_pnl.json")
FEED_QUERY_FILE        = os.path.join(JSON_RELAY, "feed_query.json")

# Sprint 27 refactor: perps_bot writes this file from its live driftpy connection.
# feed READS it (no more REST calls to Drift from feed).

# Sprint 27 autonomy upgrade: The owner's quick-fire signal channel.
# The owner writes {market, side, leverage?, rationale?} here, perps_bot converts to
# override + forces a packet rebuild so the agent sees it next scan.
OWNER_SIGNAL_PATH       = os.path.join(INBOX_PATH, "owner_signal.json")

# Overrides auto-expire after this many hours (prevents stale thesis leaking
# into future trades if the owner forgets to clear). 0 = never expire.
OVERRIDE_EXPIRY_HOURS  = 8

# Calibration feedback file (bin-level WR for confidence adjustment)

# Markets covered (must match feed's PERP_MARKETS_TRACKED)
PERP_MARKETS_COVERED = ["SOL-PERP", "BTC-PERP", "ETH-PERP"]

# Packet cadence: 1 hour (Sprint 44: now tunable)
PACKET_INTERVAL_SECONDS = 3600

# Sprint 44: how often the tune-invitation brief fires (the "want to tune?" packet)

# Daily bias cron: 9am EST
DAILY_BIAS_HOUR_EST = 9

# Exit monitor cadence: every 2 minutes
EXIT_MONITOR_INTERVAL = 120

# ── soft_trail_sensor_v1 (2026-08-08) ──────────────────────────────────────
# Throttle identical SOFT sensors. The exit monitor runs every 120s, and while a
# soft giveback condition stays true it was rewriting perp_exit_suggestion.json
# with a FRESH suggestion_id every single tick - The agent inbox spam, HOLD_REJECT
# spam, and eventually a bad discretionary close made out of fatigue rather than
# thesis. The Aug 7 SOL close was exactly that: thesis still true, cut on soft
# giveback, fees printed it net red.
#
# The owner's ruling: soft trail is a SENSOR, not a close command.
EXIT_SOFT_TRAIL_COOLDOWN_S   = 1800   # 30m between identical soft sensors
EXIT_SOFT_GIVEBACK_REFIRE_PP = 5.0    # ...unless the giveback band moved this much
_S27_LAST_EXIT_SUGGEST = {}
# 2026-10-16: perp_exit_suggestion.json is removed when its position closes, the
# market holds a different position now, or it is older than this.
PERP_EXIT_SUGGEST_STALE_S = int(_envv("PERP_EXIT_SUGGEST_STALE_S", "1800"))


def _perp_exit_suggest_prune(store=None):
    try:
        _p = os.path.join(INBOX_PATH, "perp_exit_suggestion.json")
        if not os.path.exists(_p):
            return
        with open(_p, encoding="utf-8-sig") as _f:
            _s = json.load(_f) or {}
        if store is None:
            store = load_positions() or {}
        _mk = str(_s.get("market") or "")
        _pos = store.get(_mk) if isinstance(store, dict) else None
        _why = None
        if not isinstance(_pos, dict) or not _pos.get("direction"):
            _why = "its position is closed"
        elif str(_pos.get("direction")).lower() != str(_s.get("direction") or "").lower():
            _why = "the market now holds the other side"
        elif _s.get("entry_price") and _pos.get("entry_price") and \
                abs(float(_pos["entry_price"]) - float(_s["entry_price"])) > 1e-6 * float(_pos["entry_price"]):
            _why = "the market now holds a different position"
        else:
            try:
                _g = datetime.fromisoformat(str(_s.get("generated_at")))
                if _g.tzinfo is None:
                    _g = _g.replace(tzinfo=EST)
                if (datetime.now(EST) - _g).total_seconds() > PERP_EXIT_SUGGEST_STALE_S:
                    _why = "it is stale"
            except Exception:
                _why = "its timestamp is unreadable"
        if _why:
            os.remove(_p)
            _S27_LAST_EXIT_SUGGEST.pop(_mk, None)
            print(f"[s27 exit] cleared perp_exit_suggestion.json ({_mk}): {_why}")
    except Exception as _e:
        print(f"[s27 exit] suggestion prune err: {_e}")           # market -> {sig, ts, giveback, action, payload}

# SPRINT 56: how often the [perp monitor] status line is PRINTED (seconds).
# This does NOT change how often the position is checked — the monitor still ticks
# on its normal `poll` cadence and TP/SL still fire immediately. This only rate-limits
# the console output.
PERP_MONITOR_LOG_INTERVAL_S = 600   # 10 minutes
_PERP_MONITOR_LAST_LOG   = {}       # sym -> monotonic ts of last printed line
_PERP_MONITOR_LAST_TRAIL = {}       # sym -> last trail_active state (force print on change)

# Query poll cadence: every 5 seconds
QUERY_POLL_INTERVAL = 5


def _s27_load_json(path, default=None):
    try:
        if not os.path.exists(path):
            return default if default is not None else {}
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = f.read().strip()
            if not raw:
                return default if default is not None else {}
            return json.loads(raw)
    except Exception as e:
        print(f"[s27] load err {path}: {e}")
        return default if default is not None else {}


def _s27_save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _ps = globals().setdefault("_PERPS_SAVE_SEQ", 0) + 1

        globals()["_PERPS_SAVE_SEQ"] = _ps

        tmp = f"{path}.{os.getpid()}.{_ps}.tmp"  # Sprint 54: unique tmp (shared name raced)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[s27] save err {path}: {e}")
        return False


def _s27_now_iso():
    return datetime.now(EST).isoformat()


def _s27_read_wallet_balance() -> float:
    """OWNER'S RULING 2026-07-26: "there shouldn't be anything reading a drift
    balance — drift is dead, it's only going to confuse the agent."

    This was _s27_read_drift_balance(), which read bot_logs/wallet_balance.json
    — a file whose WRITER died with Drift and whose last write was 2026-07-15.
    It served an 11-day-old fossil as live ammo to three places at once: the
    packet fallback, the LLM context, and the /live_balance query handler that
    The agent can ASK. That fossil is where the $23.33 phantom ammo came from.

    Now it returns the last GOOD live wallet read (cached by the packet builder
    every sweep). Older than an hour, it reports 0.0 — an honest unknown beats
    a confident corpse."""
    try:
        lg = globals().get("_LAST_GOOD_BALANCE") or {}
        usd, at = lg.get("usd"), (lg.get("at") or 0.0)
        if usd and (time.time() - at) < 3600:
            return float(usd)
    except Exception:
        pass
    return 0.0


# Legacy alias — every old call site keeps working, none of them touch Drift.


def _s27_read_existing_positions() -> list:
    """Read all open perp positions as list of {market, side, size_usd, ...}.

    ── 2026-07-29: THIS BOT WAS READING A FILE IT DOES NOT WRITE ──────────────
    It loaded PERP_POSITION_PATH ("perp_position.json"). The writer is
    PERP_POSITIONS_FILE ("perp_positionS.json"). Two constants in one file,
    one missing letter, and this returned [] forever — so perps concluded it
    held NOTHING while $100 at 10x was open on Jupiter. That empty list fed
    market_mode ("hunting" instead of "managing") and the analyzer.

    The shape was wrong too. It expected a single position dict with a
    top-level `direction`; the real file is {market: position_dict}, so even
    pointed at the right path it would have found nothing.

    The overseer had the identical typo on the identical file. Same missing
    letter, two codebases — which is what happens when a filename is a string
    literal in more than one place instead of one shared constant.
    """
    try:
        pos = _s27_load_json(PERP_POSITIONS_FILE, None)
        if pos is None:                       # legacy fallback, kept deliberately
            pos = _s27_load_json(PERP_POSITION_PATH, {})
        if not pos or not isinstance(pos, dict):
            return []
        # {market: position_dict} — the real shape
        out = []
        for _mkt, _p in pos.items():
            if not isinstance(_p, dict):
                continue
            if not (_p.get("direction") or _p.get("side")):
                continue
            if not (_p.get("size_usd") or _p.get("collateral_usd")):
                continue          # closed/empty rows are not positions
            out.append({
                "market":   _p.get("market", _mkt),
                "side":     str(_p.get("direction") or _p.get("side") or "").upper(),
                "size_usd": _p.get("size_usd"),
                "entry":    _p.get("entry_price"),
                "leverage": _p.get("leverage"),
                "collateral_usd": _p.get("collateral_usd"),
            })
        if out:
            return out
        # single-position legacy shape (top-level direction), still honoured
        if pos.get("direction"):
            return [{
                "market":   pos.get("market", "SOL-PERP"),
                "side":     pos.get("direction", "").upper(),
                "size_usd": pos.get("size_usd"),
                "entry":    pos.get("entry_price"),
                "leverage": pos.get("leverage"),
                "collateral_usd": pos.get("collateral_usd"),
            }]
    except Exception as _e:
        print(f"[s27] existing_positions read failed: {_e!r} — treating as UNKNOWN, not flat")
    return []


# ============================================================================
# SPRINT 27 UPGRADE: Pipeline health + override semantics
# ============================================================================

def _s27_file_age_min(path: str) -> float | None:
    """Age of file in minutes; None if missing."""
    try:
        if not os.path.exists(path):
            return None
        mtime = os.path.getmtime(path)
        import time as _t
        return (_t.time() - mtime) / 60
    except Exception:
        return None


def _s27_assess_pipeline_health(macro: dict) -> dict:
    """Pipeline health for the packet. (Sprint 57: Drift removed.)

    What this used to do: arbitrate Drift-sync freshness vs feed freshness vs a
    driftpy subscription count to decide whether trading was safe. All the Drift
    parts are gone — Jupiter needs no synced market-state file, it reads oracles
    live on every op, so it is always "tradeable" as long as the wallet has ammo.
    The one real health signal left is feed: is macro.json fresh enough that the
    cycle read the agent sizes off isn't stale?
    """
    issues_blocking = []
    issues_warn = []

    FEED_SWEEP_INTERVAL_MIN = 240
    FEED_STUCK_THRESHOLD_MIN = FEED_SWEEP_INTERVAL_MIN + 30   # 270min = actually stuck
    FEED_WARN_THRESHOLD_MIN  = FEED_SWEEP_INTERVAL_MIN + 10   # 250min = overdue

    macro_age = _macro_age_min()
    if macro_age is None:
        issues_warn.append("macro missing - Flow feed not read; trade off live oracle only")
    elif macro_age > FEED_STUCK_THRESHOLD_MIN:
        issues_warn.append(f"macro stale {macro_age:.0f}min - feed read is old, weight it lightly")
    elif macro_age > FEED_WARN_THRESHOLD_MIN:
        issues_warn.append(f"macro aging {macro_age:.0f}min - feed sweep overdue")

    # Kalshi odds feed (optional context, never blocking) — written by feeds/
    kalshi_bet_path = os.path.join(WS_BOT_LOGS, "kalshi_implied_probs.json")
    kalshi_age = _s27_file_age_min(kalshi_bet_path)
    kalshi_live = kalshi_age is not None and kalshi_age < 120

    # Jupiter is the venue and it is always live (per-op oracle reads). The only
    # thing that makes trading impossible is no SOL ammo, which is checked at
    # fund time, not here. So there is no "blocking" pipeline state anymore —
    # stale feed is a "weight it lightly" caution, not a stop.
    overall = "warn" if issues_warn else "healthy"

    _jup_markets = list(JUPITER_ENABLED_MARKETS)
    if issues_warn:
        hint = f"HEALTHY on Jupiter - trade {_jup_markets}; feed read is aging, lean on live oracle + your own call"
    else:
        hint = f"HEALTHY - trade freely on Jupiter: {_jup_markets}"

    return {
        "overall":  overall,
        "components": {
            "venue":            "jupiter (live)",
            "feed_sweep":       "live" if macro_age is not None and macro_age < FEED_WARN_THRESHOLD_MIN else "stale" if macro_age else "dead",
            "kalshi_crossover": "live" if kalshi_live else "offline",
        },
        "file_ages_min": {
            "macro_json": round(macro_age, 1) if macro_age is not None else None,
            "kalshi_feed": round(kalshi_age, 1) if kalshi_age is not None else None,
        },
        "cadence_expectations_min": {"feed_sweep_interval": FEED_SWEEP_INTERVAL_MIN},
        "blocking_issues":    issues_blocking,   # always empty now; kept for schema compat
        "warn_issues":        issues_warn,
        "agent_action_hint": hint,
        "live_markets":       _jup_markets,
    }

def _s27_expire_stale_overrides(overrides: dict) -> tuple:
    """Check override timestamps; remove any older than OVERRIDE_EXPIRY_HOURS.
    Returns (cleaned_overrides, list_of_expired).

    Override format with timestamp:
      { "SOL-PERP": { "force_side": "LONG", "force_leverage": 3,
                      "issued_at": "ISO", "rationale": "..." } }

    If no issued_at present, treated as permanent (user wants it standing).
    """
    if not overrides or OVERRIDE_EXPIRY_HOURS <= 0:
        return overrides, []

    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()
    cleaned = {}
    expired = []

    for market, ov in overrides.items():
        if not isinstance(ov, dict):
            cleaned[market] = ov
            continue
        issued_at = ov.get("issued_at")
        if not issued_at:
            # No timestamp → permanent
            cleaned[market] = ov
            continue
        try:
            issued_dt = _dt.fromisoformat(issued_at.replace("Z", "+00:00"))
            # Remove tz for age compare if needed
            if issued_dt.tzinfo:
                issued_dt = issued_dt.replace(tzinfo=None)
            age = now - issued_dt
            if age > _td(hours=OVERRIDE_EXPIRY_HOURS):
                expired.append({
                    "market":    market,
                    "override":  ov,
                    "age_hours": round(age.total_seconds() / 3600, 1),
                })
            else:
                cleaned[market] = ov
        except Exception:
            # Parse failed — keep it, don't expire
            cleaned[market] = ov

    return cleaned, expired


def _s27_build_agent_decision_tree(
    packets: list,
    pipeline_health: dict,
    existing_positions: list,
    wallet_balance: float,
    expired_overrides: list,
) -> dict:
    """Build a one-shot decision readout for the agent.

    Philosophy: when the agent opens the packet, he should see ONE field that tells
    him what to do. The packet still has all the details, but this field is the
    TL;DR. Format is structured so the agent's reasoning can be templated on it.

    Returns:
      {
        "primary_action":  "FIRE_LONG_SOL" | "FIRE_SHORT_BTC" | "CLOSE_SOL" | "HOLD" | "SKIP_ALL" | "ESCALATE",
        "primary_reason":  "short explanation",
        "tradeable_now":   [{market, action, leverage, tp, sl, size_usd, why}],
        "skip_markets":    [{market, reason}],
        "warnings":        [],
        "escalation":      null | "escalate to the owner: ...",
        "notable":         "anything the owner should know" (for heartbeat)
      }
    """
    tradeable_now = []
    skip_markets = []
    warnings = []
    escalation = None
    notable_bits = []

    # Pipeline blocking → escalate + no trades
    # BUT: if live markets are available AND the only blocking issue is feed
    # cadence (not an actual fault), downgrade to warn not escalate
    if pipeline_health.get("overall") == "degraded" and pipeline_health.get("blocking_issues"):
        blocking = pipeline_health["blocking_issues"]
        live_mkts = pipeline_health.get("live_markets", [])
        # only feed-stale issues + we still have live markets = cautious not escalate
        only_feed_stale = all("macro.json stale" in issue or "macro.json aging" in issue for issue in blocking)
        if only_feed_stale and live_mkts:
            # Feed is overdue but drift data is fresh. We can still trade if
            # override compels or we have strong drift-side signal. Note it
            # but don't escalate - The owner doesn't need a ping for feed cadence.
            warnings.append(f"feed stale ({blocking[0][:80]}) - trading cautious on {live_mkts}")
            # No escalation set; continue to per-packet evaluation
        else:
            escalation = "pipeline degraded: " + "; ".join(blocking[:2])

    # Sprint 57: balance floor DISABLED. This decision_tree is a cross-market
    # aggregate that is no longer written into the per-chain packets (`_ = decision_tree`),
    # and `wallet_balance` here reads a file nothing writes anymore. The real
    # affordability gate is the Jupiter funding path. Left in place, dormant.
    if False and wallet_balance < 15:
        return {
            "primary_action":  "ESCALATE",
            "primary_reason":  f"balance below $15 minimum — cannot trade",
            "tradeable_now":   [],
            "skip_markets":    [{"market": p.get("market"), "reason": "insufficient_balance"} for p in packets],
            "warnings":        [f"balance_floor_hit"],
            "escalation":      f"wallet balance ${wallet_balance:.2f} — needs deposit",
            "notable":         f"no trades possible — balance ${wallet_balance:.2f}",
        }

    # Walk each packet and decide
    for p in packets:
        market = p.get("market", "?")
        fa = p.get("fair_analysis", {}) or {}
        status = fa.get("status", "error")
        mode = fa.get("mode", "unknown")
        side = fa.get("side", "NEUTRAL")
        conviction = fa.get("conviction", 0)
        leverage = fa.get("recommended_leverage", 0)
        liq_math = fa.get("liquidation_math", {}) or {}
        tp_sl = fa.get("tp_sl_suggestion", {}) or {}
        overrides_applied = fa.get("overrides_applied", []) or []
        confidence = fa.get("confidence", "low")
        correlation_risk = fa.get("correlation_risk_score", 0) or 0
        alignment = fa.get("strategic_alignment", "unknown")
        news_warnings = fa.get("news_warnings_count", 0) or 0
        mark = (fa.get("feed_snapshot") or {}).get("mark_price")
        liq_in_vols = liq_math.get("liquidation_in_daily_vols")

        # Skip cases
        if status == "skipped":
            skip_markets.append({
                "market": market,
                "reason": fa.get("skip_reason") or f"mode={mode}",
                "mode":   mode,
            })
            continue

        if status == "error":
            skip_markets.append({
                "market": market,
                "reason": fa.get("skip_reason") or "analyzer_error",
                "mode":   "error",
            })
            warnings.append(f"{market}: analyzer error")
            continue

        # Close directive = immediate close action
        if mode == "override_close":
            tradeable_now.append({
                "market":     market,
                "action":     "CLOSE",
                "leverage":   0,
                "tp":         None,
                "sl":         None,
                "size_usd":   0,
                "why":        "The owner directed close via override",
                "confidence": "high",
            })
            notable_bits.append(f"The owner CLOSE directive for {market}")
            continue

        # Analyzed & wants to trade
        if status == "analyzed" and side in ("LONG", "SHORT") and leverage > 0:
            # Check liquidation safety
            warn_liq = None
            if liq_in_vols is not None and liq_in_vols < 1:
                warn_liq = f"LIQUIDATION <1 daily vol away ({liq_in_vols:.2f}) - gamble not a trade"
            elif liq_in_vols is not None and liq_in_vols < 2:
                warn_liq = f"liquidation tight ({liq_in_vols:.2f} daily vols)"

            # Check correlation
            warn_corr = None
            if correlation_risk > 0.7:
                warn_corr = f"correlation risk {correlation_risk:.2f} with existing position"

            # Check alignment
            warn_align = None
            if alignment == "contradicted" and conviction < 8:
                warn_align = f"hourly {side} contradicts daily - conviction {conviction} insufficient to override, size down or skip"

            # Overrides?
            is_override = mode.startswith("override_")

            # Compute actual tp/sl prices in dollars (ready-to-fire)
            tp_pct = tp_sl.get("tp_pct")
            sl_pct = tp_sl.get("sl_pct")
            tp_price = None
            sl_price = None
            if mark and tp_pct is not None and sl_pct is not None:
                if side == "LONG":
                    tp_price = round(mark * (1 + tp_pct / 100), 4)
                    sl_price = round(mark * (1 - sl_pct / 100), 4)
                elif side == "SHORT":
                    tp_price = round(mark * (1 - tp_pct / 100), 4)
                    sl_price = round(mark * (1 + sl_pct / 100), 4)

            trade_entry = {
                "market":     market,
                "action":     side,
                "leverage":   leverage,
                "tp_pct":     tp_pct,
                "sl_pct":     sl_pct,
                "tp_price":   tp_price,   # dollars - ready to write to signal
                "sl_price":   sl_price,   # dollars - ready to write to signal
                "size_usd":   liq_math.get("position_size_usd"),
                "entry_ref":  mark,
                "why":        fa.get("note", "")[:200],
                "confidence": confidence,
                "conviction": conviction,
                "mode":       mode,
                "is_override": is_override,
                "warnings":   [w for w in [warn_liq, warn_corr, warn_align] if w],
                "signal_payload": {
                    # The agent can write THIS dict directly to perp_signal.json
                    "action":    side.lower(),
                    "market":    market,
                    "size_usd":  liq_math.get("position_size_usd"),
                    "leverage":  leverage,
                    "tp_price":  tp_price,
                    "sl_price":  sl_price,
                },
            }

            # If override is present, the owner is the authority; note it
            if is_override:
                trade_entry["owner_rationale"] = "; ".join(overrides_applied)[:200]
                notable_bits.append(f"The owner override on {market}: {side} {leverage}x")

            tradeable_now.append(trade_entry)
            if warn_liq: warnings.append(f"{market}: {warn_liq}")
            if warn_corr: warnings.append(f"{market}: {warn_corr}")
            if warn_align: warnings.append(f"{market}: {warn_align}")
        elif status == "analyzed" and leverage == 0:
            # edge case: analyzer says analyzed but leverage 0
            skip_markets.append({"market": market, "reason": "zero_leverage_recommended", "mode": mode})

    # Determine primary action
    if tradeable_now:
        first = tradeable_now[0]
        primary_action = f"FIRE_{first['action']}_{first['market'].split('-')[0]}"
        reason_parts = [f"{first['market']} {first['action']} {first['leverage']}x"]
        if first.get("is_override"):
            reason_parts.append("(The owner override)")
        else:
            reason_parts.append(f"conviction {first['conviction']}/10")
        primary_reason = " ".join(reason_parts)
    elif skip_markets and not escalation:
        primary_action = "SKIP_ALL"
        stand_down_count = len([s for s in skip_markets if s.get("mode") == "stand_down"])
        if stand_down_count == len(skip_markets):
            # The owner/the agent 2026-07-27: "stand_down" reads as an instruction. It is
            # an OBSERVATION about the mechanical layer's own visibility — the
            # scan found no edge IT can see. That is not a verdict on the tape,
            # and it is certainly not an order to sit out.
            primary_reason = (f"no mechanical edge found in any of {len(skip_markets)} markets "
                              f"this scan — the scan's own read, not a verdict on the tape")
        else:
            primary_reason = f"{len(skip_markets)} markets skipped (various reasons)"
    elif escalation:
        primary_action = "ESCALATE"
        primary_reason = escalation
    else:
        primary_action = "HOLD"
        primary_reason = "no tradeable packets, no escalation needed"

    # Add position management note
    if existing_positions:
        pos_strs = [f"{p.get('market')} {p.get('side')} ${p.get('size_usd', 0):.0f}" for p in existing_positions]
        notable_bits.append(f"existing positions: {', '.join(pos_strs)}")

    # Expired overrides
    if expired_overrides:
        notable_bits.append(f"{len(expired_overrides)} override(s) auto-expired this scan")

    return {
        "primary_action":  primary_action,
        "primary_reason":  primary_reason,
        "tradeable_now":   tradeable_now,
        "skip_markets":    skip_markets,
        "warnings":        warnings,
        "escalation":      escalation,
        "notable":         "; ".join(notable_bits) if notable_bits else None,
    }


def _s27_check_owner_signal() -> dict | None:
    """Check inbox/owner_signal.json for an owner quick-fire directive.
    The owner writes:
      { "market": "SOL-PERP", "side": "LONG", "leverage": 3, "rationale": "..." }
      OR just { "directive": "long sol" }  (freeform, we'll parse)

    On detection: converts to agent_overrides.json entry with issued_at timestamp,
    archives the signal file, returns the override that was applied.
    """
    if not os.path.exists(OWNER_SIGNAL_PATH):
        return None

    try:
        signal = _s27_load_json(OWNER_SIGNAL_PATH, {})
        if not signal or not isinstance(signal, dict):
            os.remove(OWNER_SIGNAL_PATH)
            return None

        # Parse directive format OR structured format
        market = signal.get("market")
        side = signal.get("side")
        leverage = signal.get("leverage")
        rationale = signal.get("rationale", "The owner directive (no rationale provided)")

        # Freeform directive parsing
        directive = signal.get("directive", "")
        if directive and not (market and side):
            directive_lower = directive.lower()
            # Detect side
            if "long" in directive_lower:
                side = "LONG"
            elif "short" in directive_lower:
                side = "SHORT"
            elif "close" in directive_lower or "flat" in directive_lower:
                side = "CLOSE"
            # Detect market
            for mk in PERP_MARKETS_COVERED:
                base = mk.split("-")[0].lower()
                if base in directive_lower:
                    market = mk
                    break
            # Detect leverage
            import re
            lev_match = re.search(r"(\d+)\s*x", directive_lower)
            if lev_match:
                try:
                    leverage = int(lev_match.group(1))
                except Exception:
                    pass

        # Default market to SOL-PERP if not specified (most common)
        if side and not market:
            market = "SOL-PERP"

        if not market or not side:
            print(f"[s27 owner_signal] couldn't parse: {signal}")
            # Archive with error note
            err_path = OWNER_SIGNAL_PATH + ".error"
            os.rename(OWNER_SIGNAL_PATH, err_path)
            return None

        # Build the override entry
        if side == "CLOSE":
            # Special: close directive triggers position close
            override_entry = {
                "close_now":   True,
                "issued_at":   _s27_now_iso(),
                "rationale":   rationale,
                "source":      "owner_signal",
            }
        else:
            override_entry = {
                "force_side":       side.upper(),
                "issued_at":        _s27_now_iso(),
                "rationale":        rationale,
                "source":           "owner_signal",
            }
            if leverage is not None:
                override_entry["force_leverage"] = float(leverage)

        # Merge into existing overrides file
        existing = _s27_load_json(AGENT_OVERRIDES_PATH, {})
        if not isinstance(existing, dict):
            existing = {}
        existing[market] = override_entry
        _s27_save_json(AGENT_OVERRIDES_PATH, existing)

        # Archive the signal
        archive_name = f"owner_signal_{datetime.now(EST).strftime('%Y%m%d_%H%M%S')}.json"
        archive_dir = os.path.join(INBOX_PATH, "..", "inbox_results", "done")
        try:
            os.makedirs(archive_dir, exist_ok=True)
            os.rename(OWNER_SIGNAL_PATH, os.path.join(archive_dir, archive_name))
        except Exception:
            try:
                os.remove(OWNER_SIGNAL_PATH)
            except Exception:
                pass

        print(f"[s27 owner_signal] CONVERTED: {market} {side} lev={leverage} → override applied (source=owner_signal)")
        return {"market": market, "override": override_entry}
    except Exception as e:
        print(f"[s27 owner_signal] err: {e}")
        return None


# ============================================================================
# PACKET BUILDER — hourly
# ============================================================================

# ════════════════════════════════════════════════════════════════════════════
# Sprint 46: FEED INTEL — feed's cycle read, surfaced into the perp packet
# ════════════════════════════════════════════════════════════════════════════
# feed computes a full cycle read every sweep (200wk MA odometer, Pi Cycle
# top-caller, funding blowout/reset, the confidence-gated goal marker, graded
# per-coin calls, 7d projection) and writes it to the site data dir. perps never
# read it. This folds the pickup in. ONE entry point: _pfi_build_feed_intel().
#
# Sources (all in the SAME dir perps already reads daily_read.json from):
#   daily_read.json          -> cycle_signals, trade_recommendation, phase/score
#   the curated projection -> cycle_position (goal marker + confidence + gate),
#                               per-asset 7d daily path, betas
#   coin_calls.json          -> graded BTC/ETH/SOL calls (entry/tp/sl/horizon)
#
# Discipline: fail-silent (missing/bad file -> None for that section + a freshness
# tag, never raises), no invention (every field maps to a real feed field; the one
# synthesis, leverage_read.size_tilt, is tagged _derived with its rule stated).

# Feed site data dir, derived from the dir perps already reads daily_read from, so
# the two can never disagree. If feed ever moves SITE_DATA, this follows it.
_PFI_STALE_MAX_MIN    = 240  # 4h; older sections get tagged stale (not dropped)




def _pfi_state_for(age_min, data):
    if data is None or age_min is None:
        return "missing"
    return "stale" if age_min > _PFI_STALE_MAX_MIN else "fresh"


def _pfi_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pfi_extract_cycle_signals(daily_read: dict):
    """The cycle odometer + top-timing signals from daily_read.cycle_signals."""
    cs = (daily_read or {}).get("cycle_signals")
    if not isinstance(cs, dict):
        return None
    ma = cs.get("ma_200w") or {}
    pi = cs.get("pi_cycle") or {}
    fr = cs.get("funding_reset") or {}
    rp = cs.get("realized_price") or {}
    return {
        # 200-week MA: the cycle odometer. <1.0 = bottom zone, 3-4x = top zone.
        "ma_200w": {
            "btc_ratio": _pfi_num(ma.get("btc_ratio")),
            "btc_zone":  ma.get("btc_zone"),
            "note":      ma.get("note"),
        } if ma else None,
        # Pi Cycle: fires ~3 days before a top.
        "pi_cycle": {
            "ratio":  _pfi_num(pi.get("ratio")),
            "signal": pi.get("signal"),
            "note":   pi.get("note"),
        } if pi else None,
        # Funding reset: the top-TIMING tell and the most perps-native signal feed
        # gives. RESET = longs flushing (be out/short). NEGATIVE = crowded shorts =
        # squeeze fuel (long setup).
        "funding_reset": {
            "phase":  fr.get("phase"),
            "signal": fr.get("signal"),
            "note":   fr.get("note"),
        } if fr else None,
        "realized_price": {
            "ratio": _pfi_num(rp.get("ratio")),
            "note":  rp.get("note"),
        } if rp else None,
    }


def _pfi_extract_gate(cur_proj: dict):
    """Confidence-gated goal marker. Lives in the curated projection cycle_position,
    NOT daily_read. marker = score-based read; ma_200w_gate = whether the 200wk
    odometer CONFIRMS or CONTRADICTS it. Contradiction is the high-value tell."""
    cp = (cur_proj or {}).get("cycle_position")
    if not isinstance(cp, dict):
        return None
    gate_str = cp.get("ma_200w_gate")
    agreement = "neutral"
    if isinstance(gate_str, str) and gate_str.strip():
        up = gate_str.upper()
        if "CONTRADICT" in up:
            agreement = "contradict"
        elif "CONFIRM" in up:
            agreement = "confirm"
    return {
        "marker":       cp.get("marker"),
        "confidence":   cp.get("confidence"),
        "ma_200w_gate": gate_str,
        "agreement":    agreement,
        "note":         cp.get("note"),
    }


def _pfi_extract_coin_calls(coin_calls: dict):
    """feed's graded per-coin calls. Perps can consume directly as entries."""
    calls = (coin_calls or {}).get("calls")
    if not isinstance(calls, dict):
        return None
    out = {}
    for sym in ("BTC", "ETH", "SOL"):
        c = calls.get(sym)
        if not isinstance(c, dict):
            continue
        out[sym] = {
            "direction":     c.get("direction"),
            "entry":         _pfi_num(c.get("entry")),
            "tp":            _pfi_num(c.get("tp")),
            "sl":            _pfi_num(c.get("sl")),
            "horizon":       c.get("horizon"),
            "horizon_hours": c.get("horizon_hours"),
            "source":        c.get("source"),
            "reason":        c.get("reason"),
            "opened_at":     c.get("opened_at"),
            # Sprint 50: the mechanical confluence read, attached by feed even when
            # The agent overrode this call. Carries so the playbook can surface
            # mechanical-vs-your-call divergence. None on a pure mechanical call.
            "mechanical_confluence": c.get("mechanical_confluence"),
        }
        if PERP_MECHANICAL_CALLS_RETIRED and (c.get("source") or "").lower() == "mechanical":
            out[sym]["retired"] = True
            out[sym]["retired_note"] = ("mechanical calls timed out ~98% (161/164 graded). "
                                        "Flagged retired: reference only, not an entry.")
    return out or None


def _pfi_extract_projection(cur_proj: dict):
    """Compact 7d curated projection per coin: current, day-3/day-7 targets, net move."""
    assets = (cur_proj or {}).get("assets")
    if not isinstance(assets, dict):
        return None
    out = {}
    for sym in ("BTC", "ETH", "SOL"):
        a = assets.get(sym)
        if not isinstance(a, dict):
            continue
        daily = a.get("daily_price") or []
        cur = _pfi_num(a.get("current"))
        d3 = _pfi_num(daily[2]) if len(daily) > 2 else None
        d7 = _pfi_num(daily[6]) if len(daily) > 6 else None
        net_pct = round((d7 - cur) / cur * 100, 2) if (cur and d7) else None
        out[sym] = {
            "current":     cur,
            "day3_target": d3,
            "day7_target": d7,
            "net_7d_pct":  net_pct,
            "daily_path":  [_pfi_num(x) for x in daily] if daily else None,
        }
    betas = (cur_proj or {}).get("betas")
    if isinstance(betas, dict) and out:
        out["_betas_vs_btc"] = {"ETH": _pfi_num(betas.get("ETH")), "SOL": _pfi_num(betas.get("SOL"))}
    return out or None


def _pfi_derive_leverage_read(cycle_signals, gate):
    """One-glance perps synthesis. NOT a feed field. Computed here from the raw
    signals with the rule stated so the agent sees the tilt AND why. He still decides.
    Rule: tilt DOWN when any top-timing signal is hot (pi approaching/fired,
    funding BLOWOUT/EXTREME_BLOWOUT/RESET, or 200wk gate CONTRADICTS). tilt UP when
    the gate CONFIRMS and nothing above is firing. else NEUTRAL."""
    reasons = []
    tilt = "neutral"
    pi_sig = ((cycle_signals or {}).get("pi_cycle") or {}).get("signal") or ""
    fr_phase = ((cycle_signals or {}).get("funding_reset") or {}).get("phase") or ""
    ma_zone = ((cycle_signals or {}).get("ma_200w") or {}).get("btc_zone") or ""
    agreement = (gate or {}).get("agreement") or "neutral"
    pi_hot = pi_sig.upper() in ("APPROACHING_TOP", "PI_CYCLE_TOP_FIRED")
    fr_hot = fr_phase.upper() in ("BLOWOUT", "EXTREME_BLOWOUT", "RESET")
    fr_squeeze = fr_phase.upper() == "NEGATIVE"
    if pi_hot:
        reasons.append(f"pi_cycle {pi_sig}")
    if fr_hot:
        reasons.append(f"funding {fr_phase}")
    if agreement == "contradict":
        reasons.append("200wk gate contradicts marker")
    if pi_hot or fr_hot or agreement == "contradict":
        tilt = "down"
    elif agreement == "confirm":
        tilt = "up"
        reasons.append("200wk gate confirms marker")
        if fr_squeeze:
            reasons.append("funding NEGATIVE (squeeze fuel)")
    return {
        "size_tilt":      tilt,
        "funding_phase":  fr_phase or None,
        "pi_signal":      pi_sig or None,
        "ma_zone":        ma_zone or None,
        "gate_agreement": agreement,
        "reasons":        reasons,
        "_derived":       True,
        "_rule":          "tilt DOWN on any hot top-timing signal (pi/funding/gate-contradict); UP on gate confirm; else neutral. The agent decides.",
    }


def _pfi_build_feed_intel():
    """Build the feed_intel block for the perp packet. Fail-silent; any section
    may be None if its source file is absent/malformed (freshness tells you which)."""
    dr, dr_age = _feed_with_age("daily_read")
    curated, cur_age = _feed_with_age("price_projection")
    cc, cc_age = _feed_with_age("coin_calls")
    cycle_signals = _pfi_extract_cycle_signals(dr)
    gate = _pfi_extract_gate(curated)
    return {
        "as_of": datetime.now(EST).isoformat(),
        "freshness": {
            "daily_read":    {"age_min": dr_age, "state": _pfi_state_for(dr_age, dr)},
            "curated_projection": {"age_min": cur_age, "state": _pfi_state_for(cur_age, curated)},
            "coin_calls":    {"age_min": cc_age, "state": _pfi_state_for(cc_age, cc)},
        },
        "phase":                (dr or {}).get("current_phase"),
        "score":                _pfi_num((dr or {}).get("current_score")),
        "cliff_proximity":      (dr or {}).get("cliff_proximity"),
        "regime_break_signal":  bool((dr or {}).get("regime_break_signal", False)),
        "trade_recommendation": (dr or {}).get("trade_recommendation"),
        "cycle_signals": cycle_signals,
        "gate":          gate,
        "leverage_read": _pfi_derive_leverage_read(cycle_signals, gate),
        "coin_calls":    _pfi_extract_coin_calls(cc),
        "committed_candidates": committed_call_candidates() or None,
        "projection_7d": _pfi_extract_projection(curated),
        "_source": "perp_feed_intel v1.0 (inline)",
        "_doctrine": ("Feed's cycle read, surfaced for perps. cycle_signals + gate + "
                      "coin_calls come straight from feed. leverage_read is a perps-side "
                      "synthesis of those raw signals (tagged _derived). Reference for sizing "
                      "conviction, never an auto-gate. The agent decides."),
    }


def _safe_feed_intel():
    """Fail-silent wrapper for the packet builder. Never kills the packet."""
    try:
        return _pfi_build_feed_intel()
    except Exception as e:
        print(f"[feed_intel] build failed (non-fatal): {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# Sprint 46: PER-CHAIN PACKETS — SOL / BTC / ETH each get their own standalone
# packet so the agent can leverage a chain off its own news + cycle read + ammo.
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
# MACRO PRINTS: released CPI numbers (Flow FRED fields first, BLS public API
# fallback) attached to every chain packet as `macro_prints`. Signal only: the
# agent checks a macro headline against the print. Never gates or fires a trade.
# The fetcher caches to bot_logs/macro_prints.json and never raises; the optional
# one-line `read` uses the SI brain (cached per print, skipped when it is off).
# ════════════════════════════════════════════════════════════════════════════
try:
    from feeds import macro_fetcher as _macro_fetcher
except Exception as _mf_err:
    _macro_fetcher = None
    print(f"[macro_prints] fetcher unavailable: {_mf_err}")


async def _perp_macro_prints() -> dict:
    if _macro_fetcher is None:
        return {"available": False, "reason": "macro_fetcher unavailable"}
    try:
        mp = await asyncio.wait_for(asyncio.to_thread(_macro_fetcher.get_macro_prints), timeout=30)
    except Exception as _me:
        print(f"[macro_prints] fetch err: {_me}")
        try:
            mp = _macro_fetcher.cached_prints()
        except Exception:
            return {"available": False, "reason": f"macro_prints error: {_me}"[:160]}
    try:
        _brain_on = globals().get("_perp_brain_enabled")
        if (_brain_on is None or _brain_on()) and _macro_fetcher.needs_read(mp):
            _macro_fetcher.mark_read_attempt()
            _raw = await asyncio.wait_for(
                _perp_ollama_generate(_macro_fetcher.read_prompt(mp), model=PERPS_OLLAMA_MODEL,
                                      format_json=True, max_tokens=160), timeout=60)
            _line = _macro_fetcher.parse_read(_raw)
            if _line:
                mp = _macro_fetcher.set_read(_line, globals().get("_PERP_LAST_LLM_SOURCE") or "si_brain")
    except Exception as _re:
        print(f"[macro_prints] read skipped: {_re}")
    return mp


# 2026-10-17: macro-first news ranking. The per-bucket cut used to keep the first
# N in file order, so a hot CPI headline could sit behind five older hack stories.
# Rank macro (CPI / inflation / Fed / jobs / yields) first, then newest; cap unchanged.
_PERP_MACRO_NEWS_RE = re.compile(
    r"\b(cpi|inflation|inflationary|disinflation|deflation|fed|fomc|federal reserve|powell|"
    r"rate cuts?|rate hikes?|interest rates?|pce|ppi|payrolls?|nfp|jobs report|unemployment|"
    r"jobless claims|treasury yields?|bond yields?|recession|gdp)\b", re.I)


def _perp_news_is_macro(item: dict) -> bool:
    grp = str(item.get("group") or "").lower()
    if grp.startswith("macro") or "fed" in grp.split("_"):
        return True
    return bool(_PERP_MACRO_NEWS_RE.search(f"{item.get('headline') or ''} {item.get('detail') or ''}"))


def _perp_news_ts(ts) -> float:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=EST)
        return d.timestamp()
    except Exception:
        return 0.0


def _perp_news_rank(items: list) -> list:
    """Macro items first, then newest first. Stable for ties."""
    return sorted(items, key=lambda i: (0 if i.get("macro") else 1, -_perp_news_ts(i.get("ts"))))


def _perp_chain_news(chain: str, max_per_bucket: int = 5) -> dict:
    """Per-chain news bucketed bull / neutral / bear from news_catalysts.json
    (filtered by affects_asset) plus the agent's editorial catalyst bias for the
    chain. Untagged market-wide catalysts show in every chain (flagged)."""
    chain = (chain or "").upper()
    bull, neutral, bear = [], [], []
    try:
        nc = _s27_load_json(NEWS_CATALYSTS_PATH, {}) or {}
        for c in (nc.get("active_catalysts") or []):
            a = (c.get("affects_asset") or "").upper()
            if a and a != chain:
                continue  # tagged to a different chain
            sig = (c.get("signal") or c.get("direction") or "").lower()
            grp = (c.get("group") or "").lower()
            item = {
                "headline":    c.get("title") or c.get("headline") or c.get("catalyst_summary"),
                "detail":      (c.get("description") or "")[:200],
                "group":       c.get("group"),
                "url":         c.get("url"),
                "ts":          c.get("timestamp"),
                "market_wide": (a == ""),
            }
            item["macro"] = _perp_news_is_macro(item)
            bull_sig = ("bull" in sig or "up" in sig or "long" in sig or sig == "positive")
            bear_sig = ("bear" in sig or "down" in sig or "short" in sig or sig == "negative")
            if not bull_sig and not bear_sig:
                # signal came through neutral — fall back to the GROUP tag, which
                # news_catalyst sets by keyword (crypto_bearish / crypto_bullish).
                if "bull" in grp:
                    bull_sig = True
                elif "bear" in grp:
                    bear_sig = True
            if bull_sig:
                bull.append(item)
            elif bear_sig:
                bear.append(item)
            else:
                neutral.append(item)
    except Exception as e:
        print(f"[perp news] {chain} read err: {e}")

    # The agent's editorial catalyst bias for this chain (raw, from feed's file).
    # Displayed as-is (each entry decays on feed's side); we don't re-derive here.
    editorial = []
    try:
        cb = _feed("catalyst_bias", {}) or {}
        for b in (cb.get("biases") or []):
            if (b.get("asset") or "").upper() == chain:
                editorial.append({"pct": b.get("pct"), "reason": b.get("reason"),
                                  "set_at": b.get("set_at"), "decay_days": b.get("decay_days")})
    except Exception:
        pass

    return {
        "bullish":  _perp_news_rank(bull)[:max_per_bucket],
        "neutral":  _perp_news_rank(neutral)[:max_per_bucket],
        "bearish":  _perp_news_rank(bear)[:max_per_bucket],
        "counts":   {"bull": len(bull), "neutral": len(neutral), "bear": len(bear), "net": len(bull) - len(bear),
                     "macro": sum(1 for i in bull + neutral + bear if i.get("macro"))},
        "editorial_bias": editorial or None,
        "summary":  f"{len(bull)} bull / {len(neutral)} neutral / {len(bear)} bear",
        "_doctrine": ("Per-chain news. affects_asset routes each catalyst to its chain; untagged "
                      "market-wide ones appear in every chain (market_wide=true). Split by signal "
                      "direction. Each bucket lists macro items (macro=true: CPI, inflation, Fed, jobs, "
                      "yields) first, then newest; counts are the full totals before the cut. "
                      "Display + context for the leverage call, never an auto-gate."),
    }


POLY_IMPLIED_PROB_FILE = os.path.join(WS_BOT_LOGS, "polymarket_implied_probabilities.json")


def _perp_chain_positioning(chain: str, macro: dict) -> dict:
    """Per-chain positioning + feed, read from feed's macro.json. 2026-08-07.

    NOTHING IS FETCHED HERE. feed pulls these every sweep (OKX long/short
    and taker volume, Deribit options, DeFiLlama exchange balances - all free,
    all keyless) and writes them to bot_logs/macro.json. perps reads what is
    already on disk; a second fetcher hitting the same endpoints would be the
    dual-writer pattern this fleet just spent a pass removing.

    WHY THE PERCENTILE AND NOT THE RATIO: measured 2026-08-07, BTC sat at a
    long/short ratio of 1.30 and SOL at 2.89. On raw numbers SOL just looks
    "higher". Against each asset's OWN 30-day range, BTC was the 30th percentile
    and SOL the 95th - one market pinned at a retail-crowding extreme and the
    other two light. Opposite readings, and only the percentile makes them
    comparable. That matters most right here, because this is the packet a
    leveraged position gets sized from.

    CHAIN-SPECIFIC where the data is per-asset (long/short). BTC-WIDE where it is
    not (cvd, options, exchange reserves), and each says so in its scope field
    rather than implying a SOL-specific read that does not exist.

    Display and context. Never a gate.
    """
    chain = (chain or "").upper()
    macro = macro or {}
    out, missing = {}, []

    ls = (macro.get("long_short_by_asset") or {}).get(chain)
    if isinstance(ls, dict) and ls.get("pct_30d") is not None:
        out["long_short"] = {
            "ratio":     ls.get("ratio"),
            "pct_30d":   ls.get("pct_30d"),
            "chg_24h":   ls.get("chg_24h"),
            "chg_7d":    ls.get("chg_7d"),
            "range_30d": [ls.get("min_30d"), ls.get("max_30d")],
            "read":      ls.get("read"),
            "scope":     f"{chain} accounts on OKX",
        }
    else:
        missing.append("long_short")

    if macro.get("cvd_btc_pct_30d") is not None:
        out["cvd"] = {
            "btc_24h": macro.get("cvd_btc_24h"),
            "pct_30d": macro.get("cvd_btc_pct_30d"),
            "read":    macro.get("cvd_btc_read"),
            "scope":   "BTC spot taker feed (NOT chain-specific)",
        }
    else:
        missing.append("cvd")

    if macro.get("options_skew") is not None:
        out["options"] = {
            "skew_vol_pts": macro.get("options_skew"),
            "read":         macro.get("options_skew_read"),
            "dvol":         macro.get("dvol_btc"),
            "dvol_pct_30d": macro.get("dvol_btc_pct_30d"),
            "expiry":       macro.get("options_skew_expiry"),
            "scope":        "BTC options on Deribit (NOT chain-specific)",
            "_method":      "moneyness proxy (+-10% OTM), not 25-delta",
        }
    else:
        missing.append("options")

    if macro.get("btc_exchange_reserves") is not None:
        out["exchange_reserves"] = {
            "btc":          macro.get("btc_exchange_reserves"),
            "chg_7d_btc":   macro.get("btc_exchange_reserves_chg_7d"),
            "pct_30d":      macro.get("btc_exchange_reserves_pct_30d"),
            "read":         macro.get("btc_exchange_reserves_read"),
            "usd_net_24h": macro.get("exchange_net_24h_usd"),
            "usd_read":     macro.get("exchange_reserves_read"),
            "scope":        "BTC coin count across top CEXs (NOT chain-specific)",
            "_note": ("If usd_read contradicts read, trust the COIN COUNT. The USD "
                      "figure spans all assets and moves with price and stablecoin "
                      "churn; coins moving is coins moving. They disagreed in "
                      "direction on the day this was wired."),
        }
    else:
        missing.append("exchange_reserves")

    if not out:
        return None
    if missing:
        # Absence is STATED, not hidden. A missing block means feed's fetch failed
        # this sweep - it does NOT mean the market is neutral on that dimension.
        out["_unavailable"] = missing
    out["_doctrine"] = (
        "Positioning and feed from feed's sweep (free public endpoints, no keys). "
        "Percentiles rank each series against its OWN recent range, so 50 is "
        "typical-for-now and the extremes are the signal - a raw ratio or CVD number "
        "has no scale. long_short is THIS chain; cvd, options and exchange_reserves are "
        "BTC-wide and say so in scope. Context for the leverage call. NEVER a gate.")
    return out


def _perp_chain_polymarket(chain: str, max_items: int = 6) -> dict:
    """Polymarket implied odds for THIS chain: price-threshold markets (e.g.
    'BTC reach $110k by Q3' -> 62%) + ecosystem markets. Read-only display so
    The agent can cross-check his leverage direction against where real money sits."""
    chain = (chain or "").upper()
    try:
        pm = _s27_load_json(POLY_IMPLIED_PROB_FILE, {}) or {}
        book = (pm.get("implied_probabilities") or {}).get(chain) or {}
        thresholds = book.get("thresholds") or []
        eco = book.get("ecosystem_signals") or []
        def _fmt(e):
            return {
                "question":      e.get("question"),
                "threshold_usd": e.get("threshold_usd"),
                "horizon":       e.get("horizon"),
                "deadline":      e.get("deadline"),
                "implied_prob":  e.get("implied_prob") or e.get("yes_price"),
            }
        if not thresholds and not eco:
            return None
        return {
            "price_markets":     [_fmt(e) for e in thresholds[:max_items]],
            "ecosystem_markets": [_fmt(e) for e in eco[:max_items]],
            "counts":            {"price": len(thresholds), "ecosystem": len(eco)},
            "_updated":          pm.get("last_updated") or pm.get("_updated"),
            "_doctrine": ("What Polymarket money believes about this chain: price-threshold "
                          "markets (implied_prob = YES price = crowd probability) + ecosystem "
                          "events. Cross-check your leverage direction against where real money "
                          "is positioned. Reference, not a gate."),
        }
    except Exception as e:
        print(f"[perp poly] {chain} read err: {e}")
        return None


def _slice_feed_intel_for_chain(feed_intel: dict, chain: str) -> dict:
    """Slice the shared feed_intel down to what THIS chain needs. coin_call and
    projection are chain-specific; cycle_signals + gate are the macro BTC cycle
    read the whole market orients on, carried into every chain."""
    fi = feed_intel or {}
    chain = (chain or "").upper()
    # Sprint 50: this chain's micro-lean (flat-trend front-run), pulled from the
    # trade_recommendation.micro_lean_by_coin feed publishes. Present only when the
    # score is flat AND this coin's confluence leans — otherwise None. Surfacing it
    # here is what lets the leverage playbook SHOW the agent the lean so he can
    # consciously take/size/skip it, instead of it only living in the gate.
    _tr = (fi.get("trade_recommendation") or {})
    _micro = (_tr.get("micro_lean_by_coin") or {}).get(chain)
    return {
        "coin_call":     (fi.get("coin_calls") or {}).get(chain),
        "projection_7d": (fi.get("projection_7d") or {}).get(chain),
        "leverage_read": fi.get("leverage_read"),
        "gate":          fi.get("gate"),
        "cycle_signals": fi.get("cycle_signals"),
        "funding":       (fi.get("cycle_signals") or {}).get("funding_reset"),
        "micro_lean":    _micro,   # this chain's anticipatory flat-trend lean, or None
        "phase":         fi.get("phase"),
        "score":         fi.get("score"),
        "freshness":     fi.get("freshness"),
        "_note": ("cycle_signals + gate are the macro BTC cycle read (applies to all chains). "
                  "coin_call + projection are THIS chain. funding_reset is the top-timing tell. "
                  "micro_lean (if present) = a DIVERGENCE reading for THIS chain: score flat, fast signals disagree. A reading, not a lane."),
    }


def _leverage_playbook(chain: str, chain_feed: dict, venue_and_wallet: dict, position, current_price, sol_price) -> dict:
    """Everything to size this chain in one place: ammo (SOL-first — Drift is down),
    which way feed leans, and where liquidation lands at common leverages off the
    LIVE price. So he can size with eyes open."""
    lr   = chain_feed.get("leverage_read") or {}
    call = chain_feed.get("coin_call") or {}
    micro = chain_feed.get("micro_lean")  # Sprint 50: this chain's flat-trend front-run, or None
    vw   = venue_and_wallet or {}
    wallet_sol = vw.get("wallet_sol")
    sol_usd = round(wallet_sol * sol_price, 2) if (wallet_sol and sol_price) else None
    liq_preview = None
    if current_price:
        liq_preview = {"long": {}, "short": {}}
        for _lev in (5, 10, 20, 40):
            liq_preview["long"][f"{_lev}x"]  = round(current_price * (1 - 1.0 / _lev), 4)
            liq_preview["short"][f"{_lev}x"] = round(current_price * (1 + 1.0 / _lev), 4)
    _ladder = None
    try:
        _ladder = {
            "rule": "less collateral -> higher leverage, capped at PERP_MAX_LEVERAGE; used when a signal omits leverage",
            "cap_x": float(_perp_get_override("PERP_MAX_LEVERAGE", PERP_MAX_LEVERAGE)),
            "steps": [{"collateral_sol": c, "leverage_x": _perp_lev_for_collateral_sol(c),
                       "notional_sol": round(c * _perp_lev_for_collateral_sol(c), 4)} for c, _l in _perp_lev_ladder()],
            "for_wallet": ({"collateral_sol_0.1": _perp_lev_for_collateral_sol(0.1),
                            "collateral_sol_1": _perp_lev_for_collateral_sol(1.0)}),
        }
    except Exception:
        _ladder = None
    return {
        "leverage_ladder": _ladder,
        "in_position": bool(position),
        "current_price": current_price,
        "ammo": {
            "sol_balance":   wallet_sol,
            "sol_usd_value": sol_usd,
            "usdc_floor":    USDC_FLOOR_USD,
            "active_venue":  vw.get("active_venue"),
            "note": "SOL is your firepower — it swaps to USDC (or wraps) to fund a trade. Your ammo is the SOL wallet.",
        },
        "feed_lean": {
            "size_tilt":           lr.get("size_tilt"),
            "tilt_reasons":        lr.get("reasons"),
            "coin_call_direction": call.get("direction"),
            "entry": call.get("entry"), "tp": call.get("tp"), "sl": call.get("sl"),
        },
        # Sprint 50: the flat-trend micro-lean for THIS chain. Present ONLY when the
        # score is flat but this coin's own confluence (its 200wk + funding +
        # projection) leans directional. It's the "score isn't moving but this coin's
        # fast signals say a move is coming" front-run. Anticipatory, 0.15x, tight
        # stop, graded on its own lane. Shown so he acts consciously — his call.
        "micro_lean": ({
            "direction":   micro.get("direction"),
            "conviction":  micro.get("conviction"),
            "score":       micro.get("score"),
            "votes":       micro.get("votes"),
            # ── OWNER/AGENT RULING 2026-07-27: SENSOR, NOT A MINI-ORDER ─────
            # "micro_lean bait — 'front-run the chop' energy makes me want to be
            #  early for the grade, not because the tape is clean."
            #
            # That is the failure mode exactly: a reading that implies a size and
            # a lane starts generating trades for the scoreboard instead of for
            # the setup. `perps_label` (which shipped a literal "long_micro"
            # order string) is DELETED. No size. No lane. No verb.
            "why": (f"score is flat, but {chain}'s own confluence "
                    f"({micro.get('conviction')} conviction) disagrees with the stillness"),
            "note": (f"SENSOR READING: the feed score is flat while {chain}'s fast signals lean "
                     f"{(micro.get('direction') or '').upper()}. That is a disagreement between "
                     f"two layers, and it is reported because you should know it exists — not "
                     f"because it implies a trade. There is no suggested size and no lane. "
                     f"A divergence is a reason to LOOK at the tape, never a reason to be early. "
                     f"If the chart isn't clean, the correct response to this reading is nothing."),
        } if isinstance(micro, dict) else None),
        # Sprint 50: mechanical confluence vs YOUR call — surfaces on ANY trend (not
        # just flat), so when you've overridden the coin call (e.g. to FLAT) but the
        # mechanical read disagrees, you SEE what you're overriding. Mechanical
        # proposes, you dispose — but you can't dispose of what you can't see. This
        # is the same confluence the micro-lean uses, shown regardless of trend.
        "mechanical_vs_your_call": ((lambda _mc, _yc: ({
            "mechanical_direction":  _mc.get("direction"),
            "mechanical_conviction": _mc.get("conviction"),
            "mechanical_score":      _mc.get("score"),
            "your_call_direction":   _yc,
            "diverges":              (_mc.get("direction") not in (None, "FLAT")
                                      and (_yc or "FLAT").upper() != (_mc.get("direction") or "").upper()),
            "votes":                 _mc.get("votes"),
            "note": (f"Mechanical confluence reads {chain} {(_mc.get('direction') or 'FLAT')} "
                     f"({_mc.get('conviction')} conviction, score {_mc.get('score')}); your call is "
                     f"{(_yc or 'FLAT').upper()}. "
                     + ("They DIVERGE — the mechanical read wants a direction you haven't taken. "
                        "Not a gate: your call stands. But look at the votes and decide if you're "
                        "leaving an edge on the table or correctly fading a weak signal."
                        if (_mc.get("direction") not in (None, "FLAT")
                            and (_yc or "FLAT").upper() != (_mc.get("direction") or "").upper())
                        else "They agree — mechanical backs your call.")),
        }) if isinstance(_mc, dict) and _mc.get("direction") else None
        )((chain_feed.get("coin_call") or {}).get("mechanical_confluence"),
          (chain_feed.get("coin_call") or {}).get("direction"))),
        "liq_preview": liq_preview,
        "sizing_rule": ("Leverage sets your liquidation PRICE. liq_preview shows where liq lands at "
                        "5/10/20/40x from the current price, long and short — pick leverage with eyes "
                        "open and keep your exits inside it. size_tilt=down means smaller leverage; "
                        "tilt=up means you can lean in. Size in SOL; it converts to fund."),
        "_doctrine": "Reference to size this chain, not an order. You set the number.",
    }


# ════════════════════════════════════════════════════════════════════════════
# Sprint 47: PERP AUTOPSY ENGINE — the behavioral edge, not a tuner.
# ════════════════════════════════════════════════════════════════════════════
# Leverage has no filter to tune. What it has is the agent's psychology. This engine
# holds up a mirror: patterns in what he did and DIDN'T do that cost him. Four
# lenses, one shape each — a pattern earned by the data, surfaced as ONE compact
# line in the chain's packet, cleared when he ACKNOWLEDGES it. Backsliding after
# an ack re-earns the lesson (acknowledging clears the instance, not the truth).
#
#   decline      : passed a setup that then ran. The contrarian read he keeps
#                  fading (low fear = buy, low poly odds = underpriced tail, etc).
#   correlation  : long/short the SAME direction on multiple chains = hidden
#                  stacked beta, one move liquidates all. (live risk, no gate)
#   leverage_fit : his higher-leverage trades win LESS = oversizing his worst reads.
#   regime       : he gets chopped in specific phases = sit leverage out there.
#
# Nothing surfaces unless the DATA earns it (min sample + the contrarian/negative
# read actually wins at rate). A reminder that's sometimes wrong gets tuned out,
# and then the edge is dead. The statistical gate is what keeps this sharp.

AUTOPSY_LESSONS_FILE   = os.path.join(WS_BOT_LOGS, "perp_autopsy_lessons.json")
AUTOPSY_SNAPSHOTS_FILE = os.path.join(WS_BOT_LOGS, "perp_decision_snapshots.jsonl")

# gates
_AUTOPSY_MIN_DECLINE_MISSES = 3      # min missed setups in a class before it surfaces
_AUTOPSY_MIN_CONTRA_RATE    = 0.60   # contrarian read must win >=60% to be a lesson
_AUTOPSY_MIN_LEV_SAMPLES    = 5      # per leverage bucket
_AUTOPSY_MIN_REGIME_SAMPLES = 4      # per regime
_AUTOPSY_MIN_ASYM_SAMPLES   = 8      # per chain, for the win-rate-vs-dollars read
_AUTOPSY_REGIME_BAD_WR      = 0.40   # regime win rate below this = sit-out lesson
_AUTOPSY_LEV_GAP            = 0.15   # high-lev WR must trail low-lev WR by this

# The soul of the decline lens: signal-he-faded -> the contrarian counter-lesson.
_DECLINE_PSYCHOLOGY = {
    "low_fear":        "Extreme fear IS the entry. The crowd's fear is the setup, not the warning — you keep reading it as a reason to stay out.",
    "low_poly":        "The crowd underprices tail moves. A low Polymarket probability isn't a 'no' — it's where the asymmetric payoff lives.",
    "bearish_news":    "Priced-in bad news that DOESN'T dump is a bullish tell. You keep treating the headline as the trade instead of the reaction to it.",
    "negative_funding":"Negative funding = crowded shorts = squeeze fuel. You keep reading it as danger when it's the long setup.",
}
_DECLINE_LABEL = {
    "low_fear":"low fear & greed","low_poly":"low Polymarket odds",
    "bearish_news":"bearish news","negative_funding":"negative funding",
}


def _build_managing_view(chain, market, pos, current_price, chain_feed, news, poly, sol_price):
    """Sprint 57: the fused per-position MANAGING view. When you're IN a position
    on this chain, this is the single block that fuses position health with
    EVERYTHING moving this specific chain right now — its own news, its own
    Polymarket odds, and feed's current cycle read — so the hold/adjust/close
    call is made against the full chain picture, not just P&L in a vacuum.

    Replaces the old _build_position_management, which read price off the (now
    dead) drift_market_state file and left P&L null. This takes the LIVE per-chain
    price the packet already computes, and folds in the chain context that used to
    live only in sibling blocks. Each chain gets its own — ETH's news is ETH's,
    not blended with BTC or SOL.

    Surfaces, never gates. The verdict is the agent's; this assembles the evidence.
    """
    sym       = market.replace("-PERP", "")
    direction = pos.get("direction")
    entry     = float(pos.get("entry_price") or 0) or None
    leverage  = float(pos.get("leverage") or 0) or None
    size_usd  = pos.get("size_usd")
    tp_price  = pos.get("tp_price")
    sl_price  = pos.get("sl_price")
    liq_price = pos.get("liq_price")
    cur       = current_price

    # ── position health off the LIVE price ──
    unrl_pct = unrl_usd = d_liq = d_tp = d_sl = None
    if cur and entry and leverage:
        raw = (cur - entry) / entry * 100 if direction == "long" else (entry - cur) / entry * 100
        unrl_pct = round(raw * leverage, 2)
        if size_usd:
            unrl_usd = round(size_usd * (unrl_pct / 100), 2)
    if cur:
        if liq_price: d_liq = round(abs(cur - float(liq_price)) / cur * 100, 2)
        if tp_price:  d_tp  = round(abs(float(tp_price) - cur) / cur * 100, 2)
        if sl_price:  d_sl  = round(abs(cur - float(sl_price)) / cur * 100, 2)

    hold_min = None
    ts = pos.get("timestamp")
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=EST)
            hold_min = int((datetime.now(EST) - dt).total_seconds() / 60)
        except Exception:
            pass

    # ── does feed's CURRENT read still support the position? (highest-value tell) ──
    cf    = chain_feed or {}
    tilt  = (cf.get("leverage_read") or {}).get("size_tilt")
    call  = (cf.get("coin_call") or {}).get("direction")
    funding = (cf.get("funding") or {}).get("phase")
    still = None
    cyc_note = "feed read unavailable"
    if direction:
        against = ((direction == "long"  and (tilt == "down" or call == "SHORT")) or
                   (direction == "short" and (tilt == "up"   or call == "LONG")))
        if against:
            still = False
            cyc_note = (f"feed read has TURNED AGAINST this {direction} "
                        f"(tilt={tilt}, coin_call={call}) — weigh hold vs cut")
        else:
            still = True
            cyc_note = f"feed read still supports this {direction} (tilt={tilt}, coin_call={call})"

    # ── news that moved SINCE you opened (chain-specific) ──
    news_net = ((news or {}).get("counts") or {}).get("net")
    news_summary = (news or {}).get("summary")
    # a directional read of this chain's news vs the position
    news_vs_pos = None
    if news_net is not None and direction:
        if direction == "long":
            news_vs_pos = "tailwind" if news_net > 0 else ("headwind" if news_net < 0 else "neutral")
        else:
            news_vs_pos = "tailwind" if news_net < 0 else ("headwind" if news_net > 0 else "neutral")

    # ── polymarket: where the crowd's money sits on this chain ──
    poly_top = None
    if poly and poly.get("price_markets"):
        _pm = poly["price_markets"][0]
        poly_top = {"question": _pm.get("question"), "implied_prob": _pm.get("implied_prob")}

    # ── PRECEDENT: has this exact situation-shape happened before, and how did it end? ──
    # Sprint 58: the "last time this hit" tell. Uses the fingerprint stamped at open
    # (or reconstructs one) and matches it against the episode log. None if no precedent.
    precedent = None
    try:
        fp = pos.get("entry_fingerprint") or build_entry_fingerprint(sym, direction, leverage)
        precedent = match_precedent(fp)
    except Exception:
        precedent = None

    return {
        "in_position": True, "market": market, "direction": direction,
        "leverage": leverage, "size_usd": size_usd, "entry": entry,
        "current_price": cur, "price_is_live": cur is not None,
        "unrealized_pct": unrl_pct, "unrealized_net_usd": unrl_usd,
        "tp_price": tp_price, "sl_price": sl_price, "liq_price": liq_price,
        "distance_to_liq_pct": d_liq, "distance_to_tp_pct": d_tp, "distance_to_sl_pct": d_sl,
        "hold_minutes": hold_min, "venue": "jupiter",
        # the fused chain context — everything moving THIS chain, right now
        "chain_context": {
            "cycle_check": {"position_direction": direction, "feed_size_tilt": tilt,
                            "feed_coin_call": call, "funding_phase": funding,
                            "still_confirms": still, "note": cyc_note},
            "news": {"summary": news_summary, "net": news_net,
                     "vs_position": news_vs_pos,
                     "bullish": (news or {}).get("bullish"),
                     "bearish": (news or {}).get("bearish"),
                     "note": (f"THIS chain's news since it's chain-specific (affects_asset={sym}). "
                              f"vs_position tells you if the news feed is a {direction} tailwind or headwind.")},
            "polymarket_top": poly_top,
            "precedent": precedent,
        },
        "management_doctrine": (
            f"You are IN {sym}. This is position health FUSED with everything moving "
            f"{sym} right now — its own news, its own Polymarket odds, feed's current "
            "cycle read. Entry hunting for this chain is PAUSED until you close. Make "
            "the call: hold / tighten SL / take profit / add / flip / cut. The highest-"
            "value tells: cycle_check (has feed turned against you), news.vs_position "
            "(is the chain's fresh news with you or against you), and precedent (has "
            "THIS exact shape — your direction/leverage/funding/news — burned you before; "
            "an 'X' in recent_form is a past liquidation on this shape). Nothing auto-fires "
            "— write the signal when you decide."),
    }


def _autopsy_load_lessons() -> dict:
    try:
        if os.path.exists(AUTOPSY_LESSONS_FILE):
            with open(AUTOPSY_LESSONS_FILE) as f:
                return json.load(f) or {"lessons": []}
    except Exception:
        pass
    return {"lessons": []}


def _autopsy_save_lessons(store: dict):
    try:
        with open(AUTOPSY_LESSONS_FILE, "w") as f:
            json.dump(store, f, indent=2)
    except Exception as e:
        print(f"[autopsy] save err: {e}")


def _autopsy_upsert(store: dict, chain, lens, key, message, occurrences, hit_rate=None):
    """Add or update a lesson. If it already exists and was acknowledged, a NEW
    occurrence after the ack re-earns it (backslide). Returns the lesson dict."""
    now = datetime.now(EST).isoformat()
    for L in store["lessons"]:
        if L["chain"] == chain and L["lens"] == lens and L["key"] == key:
            grew = occurrences > L.get("occurrences", 0)
            L["message"] = message
            L["occurrences"] = occurrences
            L["hit_rate"] = hit_rate
            L["last_seen"] = now
            if L.get("acknowledged_at") and grew:
                # backslide: re-earn, clear the ack
                L["acknowledged_at"] = None
                L["re_earned_at"] = now
                L["re_earn_count"] = L.get("re_earn_count", 0) + 1
            return L
    L = {"id": f"{chain}_{lens}_{key}_{int(time.time())}", "chain": chain, "lens": lens,
         "key": key, "message": message, "occurrences": occurrences, "hit_rate": hit_rate,
         "earned_at": now, "last_seen": now, "acknowledged_at": None, "re_earn_count": 0}
    store["lessons"].append(L)
    return L


def _autopsy_gate(favorable_n, total_n, min_n, min_rate):
    if total_n < min_n:
        return False, None
    rate = favorable_n / total_n if total_n else 0
    return rate >= min_rate, round(rate, 2)


# ── LENS: correlation concentration (live risk, no historical gate) ──────────
def _lens_correlation(positions: dict) -> list:
    """If >=2 chains are open the SAME direction, that's stacked beta on one
    move, not independent trades. Surfaced on every involved chain."""
    out = []
    longs  = [m for m, p in positions.items() if p.get("direction") == "long"]
    shorts = [m for m, p in positions.items() if p.get("direction") == "short"]
    for side, group in (("long", longs), ("short", shorts)):
        if len(group) >= 2:
            syms = [m.split("-")[0] for m in group]
            for m in group:
                out.append({
                    "chain": m.split("-")[0], "lens": "correlation", "key": f"{side}_{len(group)}x",
                    "occurrences": len(group), "hit_rate": None,
                    "message": (f"You're {side} on {', '.join(syms)} at once — crypto moves together, "
                                f"so that's ~{len(group)}x beta on ONE move, not {len(group)} trades. "
                                f"One reversal liquidates all of them.")})
    return out


# ── LENS: leverage-to-outcome (the sizing mirror) ───────────────────────────
def _lens_leverage_fit(trades: list) -> list:
    """Does he win LESS when he sizes leverage UP? If so he's leaning hardest on
    his worst reads. Buckets: low <10x, high >=20x. Per chain."""
    out = []
    by_chain = {}
    for t in trades:
        c = (t.get("market") or "").split("-")[0]
        if not c:
            continue
        by_chain.setdefault(c, []).append(t)
    for chain, ts in by_chain.items():
        low  = [t for t in ts if (t.get("leverage") or 0) < 10]
        high = [t for t in ts if (t.get("leverage") or 0) >= 20]
        if len(low) < _AUTOPSY_MIN_LEV_SAMPLES or len(high) < _AUTOPSY_MIN_LEV_SAMPLES:
            continue
        wr_low  = sum(1 for t in low  if (t.get("pnl_usd") or 0) > 0) / len(low)
        wr_high = sum(1 for t in high if (t.get("pnl_usd") or 0) > 0) / len(high)
        if wr_low - wr_high >= _AUTOPSY_LEV_GAP:
            out.append({
                "chain": chain, "lens": "leverage_fit", "key": "high_lev_underperforms",
                "occurrences": len(high), "hit_rate": round(wr_high, 2),
                "message": (f"Your high-leverage {chain} trades (>=20x) win {wr_high*100:.0f}% vs "
                            f"{wr_low*100:.0f}% on your low-leverage ones. You're sizing up on your "
                            f"WORST reads — the conviction that makes you crank leverage is the tell to trust less.")})
    return out


# ── LENS: regime sit-out ────────────────────────────────────────────────────
def _lens_regime(trades: list) -> list:
    """Phases where his leverage record is bad = sit out. Needs phase on trades."""
    out = []
    by = {}
    for t in trades:
        ph = t.get("phase")
        c  = (t.get("market") or "").split("-")[0]
        if not ph or not c:
            continue
        by.setdefault((c, ph), []).append(t)
    for (chain, phase), ts in by.items():
        if len(ts) < _AUTOPSY_MIN_REGIME_SAMPLES:
            continue
        wr = sum(1 for t in ts if (t.get("pnl_usd") or 0) > 0) / len(ts)
        if wr <= _AUTOPSY_REGIME_BAD_WR:
            out.append({
                "chain": chain, "lens": "regime", "key": f"bad_in_{phase}",
                "occurrences": len(ts), "hit_rate": round(wr, 2),
                "message": (f"You're {wr*100:.0f}% on {chain} leverage in the {phase} phase "
                            f"({len(ts)} trades). This phase chops your entries — but the market is "
                            f"still moving, so the edge is the OTHER side or smaller size, not sitting "
                            f"out. Stop repeating the losing direction here.")})
    return out


# ── LENS: decline (the behavioral edge — passed setups that ran) ────────────
def _lens_asymmetry(trades: list) -> list:
    """Win rate vs DOLLARS — the honest ledger. A high hit rate that still loses
    money means the losers dwarf the winners (cutting winners early, holding
    losers): the exact opposite of what leverage rewards. A LOW hit rate that is
    profitable means it WORKS; do not let the rate spook a change. Per chain."""
    out = []
    by = {}
    for t in trades:
        c = (t.get("market") or "").split("-")[0]
        if c:
            by.setdefault(c, []).append(t)
    for chain, ts in by.items():
        if len(ts) < _AUTOPSY_MIN_ASYM_SAMPLES:
            continue
        wins   = [t for t in ts if (t.get("pnl_usd") or 0) > 0]
        losses = [t for t in ts if (t.get("pnl_usd") or 0) < 0]
        realized = sum((t.get("pnl_usd") or 0) for t in ts)
        wr = len(wins) / len(ts)
        avg_win  = (sum((t.get("pnl_usd") or 0) for t in wins) / len(wins)) if wins else 0.0
        avg_loss = (sum((t.get("pnl_usd") or 0) for t in losses) / len(losses)) if losses else 0.0
        ratio = (avg_win / abs(avg_loss)) if avg_loss else None
        if wr >= 0.55 and realized < 0 and ratio:
            out.append({"chain": chain, "lens": "asymmetry", "key": "wins_but_red",
                "occurrences": len(ts), "hit_rate": round(wr, 2),
                "message": (f"You win {wr*100:.0f}% on {chain} but you are ${realized:+.0f}. Avg loss "
                            f"${abs(avg_loss):.0f} is {(1/ratio):.1f}x your avg win ${avg_win:.0f} — you are "
                            f"cutting winners early and holding losers, the opposite of what leverage pays. "
                            f"Let winners run (pull-initial, trail) and cut losers faster.")})
        elif wr <= 0.45 and realized > 0 and ratio and ratio >= 1.5:
            out.append({"chain": chain, "lens": "asymmetry", "key": "low_rate_but_green",
                "occurrences": len(ts), "hit_rate": round(wr, 2),
                "message": (f"You only win {wr*100:.0f}% on {chain} but you are ${realized:+.0f} — your winners "
                            f"(${avg_win:.0f}) dwarf your losers (${abs(avg_loss):.0f}). This is WORKING. Do not "
                            f"let the hit rate spook you into changing what makes money.")})
    return out


def _autopsy_decline_reasons(snap: dict) -> list:
    """Infer WHY he passed, from the signal state captured when the packet fired.
    A pass during a contrarian setup gets tagged with each applicable reason."""
    reasons = []
    call_dir = ((snap.get("coin_call") or {}).get("direction") or "").upper()
    fg   = snap.get("fear_greed")
    poly = snap.get("poly_lean")
    news = snap.get("news_net")
    fund = (snap.get("funding_phase") or "").upper()
    if call_dir == "LONG":
        if fg is not None and fg <= 25:            reasons.append("low_fear")
        if poly is not None and poly < 0.35:       reasons.append("low_poly")
        if news is not None and news < 0:          reasons.append("bearish_news")
        if fund == "NEGATIVE":                     reasons.append("negative_funding")
    return reasons


def _lens_decline(snapshots: list) -> list:
    """Cluster RESOLVED snapshots by decline reason; surface a class only when he
    missed it enough times AND the contrarian read actually won at rate."""
    out = []
    # by (chain, reason) -> {miss, correct}
    agg = {}
    for s in snapshots:
        if not s.get("resolved"):
            continue
        verdict = s.get("verdict")  # missed | correct_pass | inconclusive
        if verdict not in ("missed", "correct_pass"):
            continue
        for r in _autopsy_decline_reasons(s):
            k = (s.get("chain"), r)
            d = agg.setdefault(k, {"miss": 0, "correct": 0})
            d["miss" if verdict == "missed" else "correct"] += 1
    for (chain, reason), d in agg.items():
        total = d["miss"] + d["correct"]
        # floor is on actual MISSES (not total passes), then the contrarian read
        # must have won at rate across that class.
        if d["miss"] < _AUTOPSY_MIN_DECLINE_MISSES:
            continue
        rate = round(d["miss"] / total, 2) if total else 0
        if rate >= _AUTOPSY_MIN_CONTRA_RATE:
            out.append({
                "chain": chain, "lens": "decline", "key": reason,
                "occurrences": d["miss"], "hit_rate": rate,
                "message": (f"You've passed {d['miss']} {_DECLINE_LABEL.get(reason, reason)} {chain} setups "
                            f"that then ran ({int(rate*100)}% of that class hit). {_DECLINE_PSYCHOLOGY.get(reason,'')}")})
    return out


async def _autopsy_cloud_discovery_lens() -> list:
    """Sprint 62: the SIXTH lens — cross-pattern discovery the 5 statistical lenses
    structurally can't do. Those lenses each COUNT one pre-defined thing (leverage
    fit, regime, asymmetry, decline, correlation) and can only find patterns they
    were programmed to look for. This one hands the raw episode history to the cloud
    SI brain (optional) and asks: what recurring pattern in how these trades went wrong is
    NOT captured by a simple per-dimension count? — e.g. "losses cluster when an
    override AND a flip-against both preceded the entry", a conjunction no single
    lens tests.

    Doctrine-consistent: it does NOT decide anything. It writes discovered patterns
    into the SAME lesson store, in the SAME format, gated the same way, surfaced the
    same way. The agent reads them as reference — a smarter mirror, not a second
    decision-maker. Runs once/day in the EOD loop (rare → cloud is cheap here).

    Returns a list of candidate lessons [{chain, lens, key, message, occurrences,
    hit_rate}] to upsert, or [] on any failure (fail-silent; the 5 lenses stand alone).
    """
    if not _perp_brain_enabled():
        return []
    try:
        # need enough history for a pattern to mean anything
        episodes = []
        if os.path.exists(PERP_EPISODES_FILE):
            with open(PERP_EPISODES_FILE) as f:
                episodes = [json.loads(l) for l in f if l.strip()]
        if len(episodes) < 8:
            return []   # not enough closed trades to discover anything real

        # compact the episodes so the prompt stays lean — only the fields that
        # carry pattern signal, most recent 40
        compact = []
        for e in episodes[-40:]:
            fp = e.get("fingerprint", {})
            ec = e.get("entry_decision_context", {})
            compact.append({
                "market": e.get("market"), "dir": e.get("direction"),
                "lev": e.get("leverage"), "funding": fp.get("funding_phase"),
                "news": fp.get("news_lean"), "regime": fp.get("regime"),
                "conviction": ec.get("conviction"), "override": ec.get("was_override"),
                "outcome": e.get("category"), "pnl_pct": e.get("pnl_pct"),
                "liquidated": e.get("liquidated"), "hold_min": e.get("hold_minutes"),
            })

        import json as _json
        prompt = (
            "You are a trading-behavior analyst reviewing a bot's CLOSED perp positions. "
            "Each row is one trade: its setup (direction, leverage, funding phase, news lean, "
            "volatility regime), the operator's state at entry (conviction 1-10, whether they "
            "overrode the risk gate), and how it ended (outcome, pnl_pct, whether liquidated).\n\n"
            "Find AT MOST 3 recurring patterns in how the LOSSES happen that a simple per-column "
            "count would MISS — specifically CONJUNCTIONS (two or more conditions together) or "
            "sequences. Ignore anything a single-column average would already show. Only report a "
            "pattern if it appears at least 3 times and the losses meaningfully outnumber wins "
            "within it. Be concrete and quantitative.\n\n"
            "Reply ONLY as JSON: {\"patterns\": [{\"chain\": \"SOL-PERP|BTC-PERP|ETH-PERP|ALL\", "
            "\"pattern_key\": \"short_snake_case_id\", \"finding\": \"one sentence, quantified\", "
            "\"occurrences\": <int>, \"loss_rate\": <0.0-1.0>}]}. Empty list if nothing rises above "
            "single-column noise.\n\n"
            f"TRADES:\n{_json.dumps(compact)}"
        )

        raw = await _perp_ollama_generate(prompt, model=PERPS_OLLAMA_MODEL,
                                          format_json=True, max_tokens=1200, timeout_s=60.0)
        parsed = _perp_parse_llm_json(raw)
        pats = parsed.get("patterns") or []
        out = []
        for p in pats[:3]:
            occ = int(p.get("occurrences", 0) or 0)
            lr  = p.get("loss_rate")
            if occ < 3:
                continue   # same min-occurrence discipline as the statistical lenses
            chain = p.get("chain") or "ALL"
            out.append({
                "chain": chain,
                "lens": "discovery",   # distinct lens tag so it's traceable
                "key": str(p.get("pattern_key") or "pattern")[:40],
                "message": str(p.get("finding") or "")[:240],
                "occurrences": occ,
                "hit_rate": round(float(lr), 2) if lr is not None else None,
            })
        return out
    except Exception as e:
        print(f"[autopsy] cloud discovery lens err: {e}")
        return []


def build_autopsy_lessons() -> dict:
    """Run every lens, update the lesson store (add/grow/re-earn), return it.
    Called by the packet builder and the EOD digest. Fail-silent per lens."""
    store = _autopsy_load_lessons()
    try:
        trades = []
        if os.path.exists(PNL_FILE):
            with open(PNL_FILE) as f:
                trades = (json.load(f) or {}).get("trades", []) or []
    except Exception:
        trades = []
    snaps = []
    try:
        if os.path.exists(AUTOPSY_SNAPSHOTS_FILE):
            with open(AUTOPSY_SNAPSHOTS_FILE) as f:
                snaps = [json.loads(l) for l in f if l.strip()]
    except Exception:
        pass
    try:
        positions = load_positions()
    except Exception:
        positions = {}

    candidates = []
    for lens_fn, arg in ((_lens_correlation, positions), (_lens_leverage_fit, trades),
                         (_lens_regime, trades), (_lens_asymmetry, trades), (_lens_decline, snaps)):
        try:
            candidates += lens_fn(arg)
        except Exception as e:
            print(f"[autopsy] lens {lens_fn.__name__} err: {e}")

    for cand in candidates:
        _autopsy_upsert(store, cand["chain"], cand["lens"], cand["key"],
                        cand["message"], cand["occurrences"], cand.get("hit_rate"))
    _autopsy_save_lessons(store)
    return store


def autopsy_for_chain(chain: str, max_lines: int = 2) -> dict:
    """Compact earned lessons for THIS chain's packet. Acknowledged (and not
    re-earned) lessons are suppressed. Correlation + decline get priority since
    they're the sharpest. Returns None if nothing earned, to avoid bloat."""
    chain = (chain or "").upper()
    store = _autopsy_load_lessons()
    # Sprint 62: include chain-specific lessons AND cross-market "ALL" lessons (the
    # cloud discovery lens can find a pattern that spans chains — it tags those ALL,
    # and they belong on every chain's packet).
    active = [L for L in store["lessons"]
              if L["chain"].upper() in (chain, "ALL") and not L.get("acknowledged_at")]
    if not active:
        return None
    priority = {"correlation": 0, "decline": 1, "leverage_fit": 2, "regime": 3,
                "asymmetry": 4, "discovery": 5}
    active.sort(key=lambda L: (priority.get(L["lens"], 9), -L.get("occurrences", 0)))
    shown = active[:max_lines]
    # polish-2026-10-15 (item 8): missed-trade (decline) lessons are not
    # optional reading. They are listed in full, never truncated, until acked.
    _need = [L for L in active if L.get("lens") == "decline"]
    _ack_block = None
    if _need:
        _ack_block = {
            "count": len(_need),
            "ids": [L["id"] for L in _need],
            "oldest_earned_at": min((L.get("earned_at") or "") for L in _need) or None,
            "instruction": ("Missed-trade lessons need an answer: send action 'ack_lesson' with each id. "
                            "They stay on every packet until acknowledged."),
        }
    return {
        "ack_required": _ack_block,
        "lessons": [{"id": L["id"], "lens": L["lens"], "message": L["message"],
                     "occurrences": L["occurrences"], "hit_rate": L.get("hit_rate"),
                     "re_earned": L.get("re_earn_count", 0) > 0} for L in shown],
        "how_to_clear": "Acknowledge to clear a lesson: send action 'ack_lesson' with its id. If you backslide, it comes back.",
        "_doctrine": ("Behavioral edge, not a tuner. These are patterns the DATA earned — passed setups "
                      "that ran, stacked correlation, oversizing, bad-regime records. Acknowledging clears "
                      "the instance, not the truth: repeat the mistake and it re-earns."),
    }


def acknowledge_lesson(lesson_id: str) -> bool:
    store = _autopsy_load_lessons()
    for L in store["lessons"]:
        if L["id"] == lesson_id:
            L["acknowledged_at"] = datetime.now(EST).isoformat()
            _autopsy_save_lessons(store)
            return True
    return False


def build_autopsy_eod_digest() -> dict:
    """The full accounting for the EOD brief: every earned lesson across chains,
    so the packet stays to 1-2 lines while the digest carries everything."""
    store = build_autopsy_lessons()
    active = [L for L in store["lessons"] if not L.get("acknowledged_at")]
    by_lens = {}
    for L in active:
        by_lens.setdefault(L["lens"], []).append(
            {"chain": L["chain"], "message": L["message"], "occurrences": L["occurrences"], "hit_rate": L.get("hit_rate")})
    return {
        "_type": "perp_autopsy_eod", "built_at": datetime.now(EST).isoformat(),
        "active_lessons": len(active), "by_lens": by_lens,
        "_doctrine": "Everything the autopsy is holding against you right now, grouped by lens. The packets show the top 1-2 per chain; this is the whole board.",
    }


def _autopsy_log_snapshot(snap: dict):
    """Append a decision snapshot (one hunting-mode chain packet) — the substrate
    the decline lens learns from. Fail-silent."""
    try:
        with open(AUTOPSY_SNAPSHOTS_FILE, "a") as f:
            f.write(json.dumps(snap) + "\n")
    except Exception as e:
        print(f"[autopsy] snapshot log err: {e}")


async def resolve_autopsy_snapshots():
    """Counterfactual judge. Each pass, sample price and extend each open
    snapshot's running max/min. Once a snapshot is past its horizon, rule:
      - he ACTED on that chain after the snapshot -> 'acted' (never a miss)
      - the setup EVER hit TP over the horizon    -> 'missed'
      - it EVER hit SL first                        -> 'correct_pass'
      - neither                                     -> 'inconclusive'
    Judging on max/min (not just end price) is why a spike-and-fade still counts
    as a real missed move."""
    if not os.path.exists(AUTOPSY_SNAPSHOTS_FILE):
        return
    try:
        lines = [json.loads(l) for l in open(AUTOPSY_SNAPSHOTS_FILE) if l.strip()]
    except Exception:
        return
    now = datetime.now(EST)
    trades = []
    try:
        if os.path.exists(PNL_FILE):
            trades = (json.load(open(PNL_FILE)) or {}).get("trades", []) or []
    except Exception:
        pass
    price_cache = {}
    changed = False
    for s in lines:
        if s.get("resolved"):
            continue
        mkt = s.get("market")
        # sample + extend running envelope
        if mkt not in price_cache:
            try:
                price_cache[mkt] = await _market_mark_price(mkt)
            except Exception:
                price_cache[mkt] = None
        px = price_cache[mkt]
        if px:
            s["max_price"] = max(s.get("max_price", px), px)
            s["min_price"] = min(s.get("min_price", px), px)
            changed = True
        # due yet?
        try:
            snap_ts = datetime.fromisoformat(s["ts"])
        except Exception:
            continue
        if (now - snap_ts).total_seconds() < (s.get("horizon_hours") or 72) * 3600:
            continue
        # did he act on this chain after the snapshot? then it's not a miss
        if any(t.get("market") == mkt and (t.get("timestamp") or "") >= s["ts"] for t in trades):
            s["resolved"] = True; s["verdict"] = "acted"; changed = True; continue
        call = s.get("coin_call") or {}
        cdir = (call.get("direction") or "").upper()
        tp, sl = call.get("tp"), call.get("sl")
        hi, lo = s.get("max_price"), s.get("min_price")
        if cdir not in ("LONG", "SHORT") or tp is None or sl is None or hi is None:
            s["resolved"] = True; s["verdict"] = "inconclusive"; changed = True; continue
        if cdir == "LONG":
            verdict = "missed" if hi >= tp else ("correct_pass" if lo <= sl else "inconclusive")
        else:
            verdict = "missed" if lo <= tp else ("correct_pass" if hi >= sl else "inconclusive")
        s["resolved"] = True; s["verdict"] = verdict; changed = True
        _perp_decline_log_implicit(s, verdict)
    if changed:
        try:
            with open(AUTOPSY_SNAPSHOTS_FILE, "w") as f:
                for s in lines:
                    f.write(json.dumps(s) + "\n")
        except Exception as e:
            print(f"[autopsy] resolve write err: {e}")


def _flag_stale_fair_analysis(fa, live_price, sol_ammo_usd):
    """Sprint 49: the external perps_edge analyzer can serve STALE numbers (wrong
    price, dead Drift balance, inverted funding) when its Drift/feed inputs lag.
    When its price disagrees with the LIVE macro price, flag the whole block as
    degraded so the agent does NOT defer to its conviction or stand_down verdict."""
    if not fa:
        return fa
    fa = dict(fa)
    reasons = []
    fa_price = (fa.get("feed_snapshot") or {}).get("mark_price")
    if live_price and fa_price:
        gap = abs(fa_price - live_price) / live_price
        if gap > 0.03:
            reasons.append(f"analyzer price ${fa_price:.2f} disagrees with the live ${live_price:.2f} "
                           f"by {gap*100:.0f}% — its price feed is stale")
    fa_bal = (fa.get("liquidation_math") or {}).get("balance")
    if fa_bal is not None and sol_ammo_usd and abs(fa_bal - sol_ammo_usd) > 5:
        reasons.append(f"analyzer sized off ${fa_bal:.2f} (stale analyzer balance) not your "
                       f"${sol_ammo_usd:.2f} SOL ammo")
    if reasons:
        fa["_stale_flag"] = {
            "degraded": True,
            "authoritative_price": live_price,
            "reasons": reasons,
            "guidance": ("perps_edge runs on its own oracle/funding/OI + feed edges; when those lag, its "
                         "price, balance, funding, AND conviction go stale. Do NOT defer to its "
                         "conviction or stand_down verdict while degraded — weight the live feed read "
                         "(market_now price, cycle_read, leverage_read, fear/greed) instead. That is fresh."),
        }
    return fa


_FEED_PULLED: dict = {}


async def _feeds_live_odds() -> dict:
    """Live odds at decision time (never cached). Flow / news come from their last pull."""
    global _FEED_PULLED
    if _feed_router is None:
        _FEED_PULLED = {}
        return {}
    # 2026-10-16 chair fix: Kalshi p_up used Flow's cached macro price as "spot"
    # (read 106.63 while the venue was at 104.27). Use the venue mark price (same Pyth
    # oracle the positions are priced on); fall back to the Flow macro only if missing.
    _spot = {}
    for _a in ("SOL", "BTC", "ETH"):
        try:
            _px = await _market_mark_price(f"{_a}-PERP")
            if _px and _px > 0:
                _spot[_a] = float(_px)
        except Exception:
            pass
    try:
        _FEED_PULLED = await asyncio.to_thread(_feed_router.pull, ("odds",), _spot or None)
    except Exception as e:
        print(f"[feeds] live odds err: {e}")
        _FEED_PULLED = {}
    return _FEED_PULLED


def _feed_block_for(chain: str) -> dict:
    if _feed_router is None:
        return {"flags": ["feeds module unavailable"]}
    try:
        return _feed_router.build_feed_block(chain, _FEED_PULLED or {})
    except Exception as e:
        return {"flags": [f"feed block error: {e}"]}


async def build_perp_packets() -> dict:
    """Main packet builder. Reads feed, dispatches each covered market through
    packet_analyzers.perps_edge, aggregates into one packet file.

    Sprint 27 autonomy upgrade: now writes pipeline_health + data_freshness +
    override_trace + calibration_context + agent_action_hint so the agent can
    act intelligently even when data is partial or the owner fires a directive.
    """
    await _feeds_live_odds()
    from packet_analyzers import analyze_packet

    # Step 1: check for the owner signal and convert to override if present
    owner_signal_result = _s27_check_owner_signal()

    # Load all context files
    macro = _macro()
    news_catalysts = _s27_load_json(NEWS_CATALYSTS_PATH, {})
    agent_thesis = _s27_load_json(AGENT_THESIS_PATH, {})
    agent_overrides_raw = _s27_load_json(AGENT_OVERRIDES_PATH, {})

    # Auto-expire stale overrides
    agent_overrides, expired_overrides = _s27_expire_stale_overrides(agent_overrides_raw)
    if expired_overrides:
        # Write back the cleaned version
        _s27_save_json(AGENT_OVERRIDES_PATH, agent_overrides)
        print(f"[s27] expired {len(expired_overrides)} override(s) older than {OVERRIDE_EXPIRY_HOURS}h")

    existing_positions = _s27_read_existing_positions()

    # The agent's upgrade #2 (2026-07-23): risk_state.json + market_mode.json were
    # april "dead mode" fossils polluting secondary reads. Refresh both from the
    # LIVE tape every packet build — preserve unknown keys, stamp provenance.
    try:
        _mm_path = os.path.join(os.path.dirname(AGENT_OVERRIDES_PATH), "market_mode.json")
        _rs_path = os.path.join(os.path.dirname(AGENT_OVERRIDES_PATH), "risk_state.json")
        _live_solpx = _pfi_num(macro.get("sol_price")) if macro else None
        # ── 2026-07-29: THE "FOSSILS RETIRED" NOTE WAS FALSE ────────────────
        # The old refresh overwrote FOUR keys in risk_state and FIVE in
        # market_mode, then stamped a fresh updated_at and a note claiming the
        # april fossils were retired. Twenty-one fields in risk_state and
        # thirteen in market_mode were carried forward untouched — including
        # `mode: "dead"` and `reason: "exposure 0% (dead mode)"` sitting three
        # lines above the note that said fossils were gone.
        #
        # The proof was two SOL prices written 2ms apart in the same loop:
        # market_mode had 73.43 (live, in its refresh dict) and risk_state had
        # 86.44 (fossil, not in its refresh dict) — 17% apart.
        #
        # This is the most dangerous shape we have hit: a fresh timestamp, a
        # `source: live_tape_refresh` field, and a note asserting the problem
        # was already fixed, over values months old. Every staleness check
        # passes. Nothing errors.
        #
        # So: compute the money fields for real, and for anything we CANNOT
        # compute, write null rather than preserving a stale number. A null
        # reads as "unknown" and gets questioned. A stale float reads as fact.
        _exposure_usd = 0.0
        try:
            _exposure_usd = round(sum(float(p.get("size_usd") or 0)
                                      for p in (existing_positions or [])), 2)
        except Exception:
            _exposure_usd = 0.0
        # NOTE: `wallet_balance` is assigned BELOW this block (the live read runs
        # after the refresh), so referencing it here raises UnboundLocalError.
        # Predeploy caught it. Use the last-good stored value instead and mark
        # exposure_pct as approximate rather than pretending to a live figure.
        _wallet_usd = None
        try:
            _wb = _s27_read_wallet_balance()
            _wallet_usd = round(float(_wb), 2) if _wb else None
        except Exception:
            _wallet_usd = None
        _expo_pct = (round(_exposure_usd / _wallet_usd * 100, 2)
                     if (_wallet_usd and _wallet_usd > 0) else None)
        _n_pos = len(existing_positions or [])
        _mode = "hunting" if not existing_positions else "managing"

        _mm_fresh = {
            "mode": _mode, "source": "live_tape_refresh", "updated_by": "perps_bot",
            "exposure_usd": _exposure_usd, "exposure_pct": _expo_pct,
            "open_position_count": _n_pos,
            "reason": (f"{_n_pos} open position(s), ${_exposure_usd:.2f} notional"
                       if _n_pos else "no open perp positions"),
            # Values this refresh cannot compute are BLANKED, never carried.
            "risk": None, "risk_reason": None, "meta_note": None,
            "blowoff_score": None, "signal": None, "scanner_heat": None,
            "tokens_passed_hr": None, "tokens_500k_hr": None, "is_euphoria": None,
            **({"sol_price": _live_solpx} if _live_solpx else {"sol_price": None}),
        }
        _rs_fresh = {
            "block_new_perp": False, "source": "live_tape_refresh",
            "updated_by": "perps_bot",
            "perp_usd": _exposure_usd, "exposure_pct": _expo_pct,
            "wallet_usd": _wallet_usd,
            "wallet_usd_basis": ("last-good stored value — the live wallet read happens "
                                 "after this refresh, so exposure_pct is approximate"),
            **({"sol_price": _live_solpx} if _live_solpx else {"sol_price": None}),
            "mode": "managing" if existing_positions else "flat",
            "reason": (f"perp exposure ${_exposure_usd:.2f}"
                       + (f" ({_expo_pct:.1f}% of wallet)" if _expo_pct is not None else "")),
            "total_usd": None, "risk_level": None,
        }
        for _gp, _fresh in ((_mm_path, _mm_fresh), (_rs_path, _rs_fresh)):
            try:
                _cur = _s27_load_json(_gp, {}) or {}
                _cur.update(_fresh)
                _cur["updated_at"] = datetime.now(EST).isoformat()
                _cur["ghost_note"] = (
                    "perps_bot writes ONLY the perp-side fields. null = this writer cannot "
                    "compute it, NOT zero — do not read a null as a flat book. ")
                _s27_save_json(_gp, _cur)
            except Exception as _ge:
                print(f"[s46] ghost refresh err ({_gp}): {_ge}")
    except Exception:
        pass
    # The owner's ruling (2026-07-23): "it's supposed to be the SOL wallet balance."
    # _s27_read_drift_balance read the DEAD wallet_balance.json (frozen $23.33
    # since Drift died) — the agent's packets understated his real ammo ~3.5x,
    # feeding the thin-ammo psychology. Live SOL wallet USD, every packet.
    try:
        wallet_balance = await get_wallet_balance_usd()   # despite the legacy name: live SOL wallet USD
    except Exception as _be:
        print(f"[s46] live wallet read failed ({_be}) — serving last-good live value (the drift file is retired)")
        wallet_balance = _s27_read_wallet_balance()
    # 2026-07-23 (field: a degraded-RPC minute wrote "balance $0.00" into the
    # packet — the thin-ammo lie's evil twin; $0 tells the agent he has NOTHING at
    # the exact moment he's writing a fire thesis): a zero/None read when the
    # last known balance was real is a degraded READ, not a drained WALLET.
    # Serve last-good with a staleness tag; write true zero only if it repeats.
    try:
        _lg = globals().setdefault("_LAST_GOOD_BALANCE", {"usd": None, "at": 0.0, "zero_streak": 0})
        if wallet_balance and wallet_balance > 0.5:
            _lg["usd"] = wallet_balance; _lg["at"] = time.time(); _lg["zero_streak"] = 0
        else:
            _lg["zero_streak"] = (_lg.get("zero_streak") or 0) + 1
            if _lg.get("usd") and _lg["zero_streak"] < 3:
                print(f"[s46] wallet read ~zero (streak {_lg['zero_streak']}) — serving last-good "
                      f"${_lg['usd']:.2f} from {int(time.time()-_lg['at'])}s ago (degraded read ≠ drained wallet)")
                wallet_balance = _lg["usd"]
    except Exception:
        pass
    perp_daily_bias = _s27_load_json(PERP_DAILY_BIAS_FILE, {})
    calibration_bins = {}  # no writer in the drop

    # Pipeline health assessment
    pipeline_health = _s27_assess_pipeline_health(macro)

    # Build feed_context
    feed_context = {
        "available":     bool(macro),
        "current_state": macro,
        "macro":         macro,
    }

    analyzer_context = {
        "feed_context":             feed_context,
        "wallet_balance_usd":       wallet_balance,
        "drift_balance_usd":        wallet_balance,  # LEGACY ALIAS — perps_edge.py + the agent
                                                    # still read this key. Dual-emitted so nothing
                                                    # breaks; drop once both read wallet_balance_usd.
        "news_catalysts":           news_catalysts,
        "agent_thesis":            agent_thesis,
        "owner_overrides":           agent_overrides,
        "existing_perp_positions":  existing_positions,
        "perp_daily_bias":          perp_daily_bias,
        "calibration_bins":         calibration_bins,
    }

    _feed_intel = _safe_feed_intel()  # computed once; reused for managing blocks + envelope
    _macro_prints = await _perp_macro_prints()  # once per build, shared by every chain
    packets = []
    for market in PERP_MARKETS_COVERED:
        base = market.split("-")[0].lower()
        packet_in = {
            "vertical":  f"perps_{base}",
            "market":    market,
        }
        try:
            result = await analyze_packet(packet_in, analyzer_context)


            # --- SPRINT 27 AUTONOMY UPGRADE: enrich packet with trace fields ---
            # 1. override_trace (even when nothing applied)
            market_ov_raw = (agent_overrides or {}).get(market) or {}
            override_trace = {
                "file_exists":          os.path.exists(AGENT_OVERRIDES_PATH),
                "file_age_min":         _s27_file_age_min(AGENT_OVERRIDES_PATH),
                "market_key_in_file":   bool(market_ov_raw),
                "override_content":     market_ov_raw if market_ov_raw else None,
                "applied":              result.get("overrides_applied", []) if isinstance(result, dict) else [],
                "issued_by":            market_ov_raw.get("source") if market_ov_raw else None,
                "issued_at":            market_ov_raw.get("issued_at") if market_ov_raw else None,
                "rationale":            market_ov_raw.get("rationale") if market_ov_raw else None,
                "expired_in_this_scan": any(e.get("market") == market for e in expired_overrides),
            }

            # 2. data_freshness per packet (how fresh was the source data?)
            # The agent confirmed 2026-07-26 he reads none of the market-state fields
            # ("leftover plumbing") — he sizes off macro/feed, position, venue_and_wallet
            # ammo, and edge/conviction. The whole drift_market_state path is deleted.
            data_freshness = {
                "macro_json_age_min":          _macro_age_min(),
                "feed_synthesized_at":         macro.get("perp_market_edges", {}).get(market, {}).get("synthesized_at") if isinstance(macro.get("perp_market_edges"), dict) else None,
            }

            # 3. calibration_adjusted — always present, even on stand_down
            if isinstance(result, dict):
                existing_cal = result.get("calibration_adjusted")
                if not existing_cal:
                    # Build a placeholder trace so the agent can see what WOULD
                    # have been looked up, and why it wasn't applied
                    side = result.get("side", "NEUTRAL")
                    conv = result.get("conviction", 0)
                    mode = result.get("mode", "stand_down")
                    bin_key = f"{mode}_{side.lower()}_conv{conv}"
                    bin_data = calibration_bins.get(bin_key) or {}
                    result["calibration_adjusted"] = {
                        "applied":           False,
                        "reason":            "no_trade" if mode == "stand_down" else "calibration_bin_insufficient_samples",
                        "bin_if_traded":     bin_key,
                        "historical_bin_wr": bin_data.get("win_rate"),
                        "sample_count":      bin_data.get("sample_count", 0),
                        "min_samples_required": 10,
                    }

            # 4. confidence formula documentation
            if isinstance(result, dict):
                conf_reasoning = []
                data_sources_count = len(result.get("data_sources", []) or [])
                cal_samples = result.get("calibration_adjusted", {}).get("sample_count", 0)
                conviction = result.get("conviction", 0)
                if data_sources_count >= 4: conf_reasoning.append(f"4+ data sources ({data_sources_count})")
                elif data_sources_count >= 2: conf_reasoning.append(f"2-3 data sources ({data_sources_count})")
                else: conf_reasoning.append(f"only {data_sources_count} data source(s)")
                if cal_samples >= 10: conf_reasoning.append(f"calibration bin has {cal_samples} samples")
                else: conf_reasoning.append(f"calibration bin undersampled ({cal_samples}/10)")
                if conviction >= 7: conf_reasoning.append(f"conviction strong ({conviction})")
                elif conviction >= 5: conf_reasoning.append(f"conviction moderate ({conviction})")
                else: conf_reasoning.append(f"conviction low ({conviction})")
                result["confidence_reasoning"] = conf_reasoning

            # 5. correlation_risk_note — graceful null handling
            if isinstance(result, dict):
                corr_ctx = result.get("correlation_context", [])
                if not corr_ctx:
                    result["correlation_risk_note"] = "only 1 market with live data — correlation analysis not meaningful"

            _pos = load_position(market)
            packet_out = {
                "market":           market,
                "vertical":         f"perps_{base}",
                "analyzed_at":      _s27_now_iso(),
                "mode":             "managing" if _pos else "hunting",
                # Sprint 57: position_management is now built in the per-chain loop
                # (_build_managing_view) where the LIVE per-chain price + this chain's
                # news/poly/cycle are in scope. The old _build_position_management read
                # the dead venue_state and left P&L null; it's retired. Only `mode`
                # matters from here.
                "position_management": None,
                "fair_analysis":    result,
                "override_trace":   override_trace,
                "data_freshness":   data_freshness,
            }
            packets.append(packet_out)
        except Exception as e:
            print(f"[s27] packet build err {market}: {e}")
            packets.append({
                "market":    market,
                "vertical":  f"perps_{base}",
                "analyzed_at": _s27_now_iso(),
                "fair_analysis": {
                    "status":       "error",
                    "skip_reason":  "analyzer_exception",
                    "note":         str(e)[:200],
                },
            })

    scan_id = f"perp_scan_{datetime.now(EST).strftime('%Y%m%d_%H%M')}_EST"

    # Sprint 27 autonomy upgrade: one-shot decision tree so the agent can read
    # a single field and know what action is warranted RIGHT NOW.
    decision_tree = _s27_build_agent_decision_tree(
        packets=packets,
        pipeline_health=pipeline_health,
        existing_positions=existing_positions,
        wallet_balance=wallet_balance,
        expired_overrides=expired_overrides,
    )

    # Sprint 57: wallet pools — Jupiter is the only venue. No Drift account, no
    # venue-health arbitration; the only "collateral" is the SOL wallet.
    venue_and_wallet = {
        "active_venue":    "jupiter",
        "wallet_sol":      None,
        "wallet_wsol":     None,
        "wallet_usdc":     None,
        "note":            ("Jupiter is the venue. Your ammo is your SOL: opens swap SOL->USDC "
                            "(or wrap SOL) to fund, and every close / pull-initial sweeps proceeds "
                            "back to SOL, keeping a $5 USDC floor. SOL is the universal fuel."),
    }
    _vw_last = globals().get("_PERP_VW_LAST")
    if _vw_last and perp_helius_restricted():
        # Helius daily cap tripped: background packet reuses the last balances.
        venue_and_wallet.update(_vw_last)
        venue_and_wallet["note"] += " | balances reused (Helius daily cap)"
    else:
      try:
        _vw_conn = AsyncClient(RPC_URL, commitment=Confirmed)
        try:
            _owner = _kp.pubkey()
            venue_and_wallet["wallet_sol"]  = round(await get_sol_balance(_vw_conn), 4)
            venue_and_wallet["wallet_wsol"] = round(await _get_token_ui_balance(
                _vw_conn, _owner, Pubkey.from_string(JUPITER_TOKEN_MINTS["SOL"])), 4)
            venue_and_wallet["wallet_usdc"] = round(await _get_token_ui_balance(
                _vw_conn, _owner, Pubkey.from_string(JUPITER_TOKEN_MINTS["USDC"])), 2)
            globals()["_PERP_VW_LAST"] = {k: venue_and_wallet[k] for k in ("wallet_sol", "wallet_wsol", "wallet_usdc")}
        finally:
            try:
                await _vw_conn.close()
            except Exception:
                pass
      except Exception as e:
        print(f"[s43] wallet/venue read for packet failed (non-fatal): {e}")
        venue_and_wallet["note"] += f" | read_error: {str(e)[:80]}"

    # Sprint 44+: surface the agent's OWN published editorial call so the trade is a
    # conscious validate-or-contradict of his public thesis (reference, not a gate).
    _published = load_published_call()
    your_published_call = ({
        "calls": _published,
        "_doctrine": ("This is YOUR published feed-site editorial call (leverage_bias / "
                      "phase / the_read). Your perp trade validates or contradicts it. "
                      "Trading against your own leverage_bias is your sharpest signal , "
                      "do it consciously and tag why: conviction / hedge / test."),
    } if _published else None)

    # Sprint 46: THREE separate per-chain packets (SOL / BTC / ETH). Each carries
    # everything to leverage that chain standalone: mode, this chain's news split
    # bull/neutral/bear, the cycle read sliced to the chain, ammo, and the leverage
    # playbook. Replaces the old single bundled envelope. decision_tree stays
    # computed above for logging but is cross-market, so it is not written per-chain.
    _ = decision_tree  # cross-market aggregate; retained for future use
    _pp_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    build_autopsy_lessons()  # Sprint 47: refresh earned lessons before surfacing per chain
    written = []
    for pk in packets:
        market = pk.get("market")
        chain = (market or "").split("-")[0].upper()
        chain_feed = _slice_feed_intel_for_chain(_feed_intel, chain)
        _cpos = load_position(market)
        _news = _perp_chain_news(chain)
        _poly = _perp_chain_polymarket(chain)
        try:
            _posn = _perp_chain_positioning(chain, macro)
        except Exception as _pce:
            print(f"[perp positioning] {chain}: {_pce}")
            _posn = None
        _clow = chain.lower()
        _price   = _pfi_num(macro.get(f"{_clow}_price")) if macro else None
        _funding = macro.get(f"{_clow}_funding") if macro else None
        if _funding is None:   # The agent's #3: fall back to the intel scan's live read
            _funding = (globals().get("_LIVE_FUNDING_CACHE", {}).get("last") or {}).get("funding")
        _sol_px  = _pfi_num(macro.get("sol_price")) if macro else None
        _fg      = _pfi_num(macro.get("fear_greed")) if macro else None
        _wsol    = venue_and_wallet.get("wallet_sol")
        _sol_ammo_usd = round(_wsol * _sol_px, 2) if (_wsol and _sol_px) else None
        # Surface a conservative "usable for perps" figure alongside raw ammo.
        # PERP_SOL_SHARE = fraction of the wallet it's reasonable to commit to a perp.
        _perp_usable_usd = round(_sol_ammo_usd * _PERP_SOL_SHARE, 2) if _sol_ammo_usd else None
        # ── the agent's upgrades #4/#5 (2026-07-23, RELOCATED same-day: v1 sat above
        # the _funding/_fg assignments — UnboundLocalError every build. The
        # block lives BELOW its inputs now. Order is a dependency.) when the cycle is literally
        # AT the floor with fear in the tape, a 0/10 STAND_DOWN should not be
        # the packet's ONLY voice — it should also hand him the small-long
        # TEMPLATE so firing is one override-thesis away, not a blank page.
        # Templates are OFFERS, never verdicts: fair_analysis stays untouched
        # and the template says what would make it live. Same for the ETH
        # crowded-short squeeze lane.
        _playbooks = []
        try:
            _gate_info = _pfi_extract_gate(_feed("price_projection", {}))
        except Exception:
            _gate_info = {}
        try:
            _f_calm = (_funding is not None and abs(float(_funding)) < 0.006)
            _fear = (_fg is not None and float(_fg) < 40)
            _at_floor = False
            _g = (_gate_info or {}).get("ma_200w_gate") or ""
            if isinstance(_g, str) and ("FLOOR" in _g.upper() or "floor" in _g):
                _at_floor = True
            if _at_floor and _fear and _f_calm:
                _playbooks.append({
                    "name": "panic_floor_long",
                    "side": "LONG",
                    "size_frac_of_usable": 0.25,
                    "leverage_band": "3-8x",
                    "thesis": (f"cycle AT the 200wk floor + F&G {_fg} fear + calm funding "
                                f"({_funding}) — the classic floor-long. Small size, tight stop "
                                f"under the floor; the floor breaking IS the invalidation."),
                    "invalidated_if": "clean daily close below the 200wk floor, or funding spikes hot",
                    "note": ("OFFER, not a verdict — fair_analysis stands. This is the template "
                              "so 0/10 is never the only thing on the page when the cycle is "
                              "literally at the floor. Fire it only WITH an override thesis."),
                })
            if chain == "ETH" and _funding is not None and float(_funding) < -0.003:
                _playbooks.append({
                    "name": "eth_squeeze_long",
                    "side": "LONG",
                    "size_frac_of_usable": 0.2,
                    "leverage_band": "3-6x",
                    "thesis": (f"ETH funding {_funding} — shorts PAYING to stay in a crowded "
                                f"trade. Squeeze fuel. Long the crowd's exit."),
                    "invalidated_if": "funding normalizes above 0 without price moving, or fresh bearish catalyst",
                    "note": "OFFER, not a verdict — surfaced hard per doctrine because crowded shorts are a real setup.",
                })
        except Exception as _pbe:
            print(f"[s46] playbook build err: {_pbe}")
        # Sprint 57/59: build the managing view, then (if in a position) attach the
        # flip alert — conditions that TURNED against the position since entry / last
        # cycle. Async because the flip digest calls the local ollama; graceful if down.
        _mgmt_view = (_build_managing_view(chain, market, _cpos, _price, chain_feed, _news, _poly, _sol_px)
                      if _cpos else None)
        if _mgmt_view is not None:
            # Per-check adjuster: the live exit params and how to tune them (update_params).
            try:
                _mgmt_view["params"] = {k: _cpos.get(k) for k in (
                    "tp_price", "sl_price", "trail_activate_pct", "trail_drop_pct",
                    "liq_exit_floor", "params_rev")}
                _mgmt_view["params_source"] = _cpos.get("params_source") or {}
                _mgmt_view["tune_with"] = {
                    "action": "update_params", "market": market,
                    "updatable": sorted(_PERP_UPDATABLE_PARAMS),
                    "note": "TP / SL / trail / exit floor only; size and leverage go through add / adjust."}
            except Exception as _pe:
                print(f"[params] attach err: {_pe}")
            try:
                _flip = await build_flip_alert(chain, market, _cpos, macro)
                _mgmt_view["flip_alert"] = _flip
            except Exception as _fe:
                print(f"[flip] attach err: {_fe}")
                _mgmt_view["flip_alert"] = None
            # Sprint 63: fuse the lenses into one brief (local model, light work).
            # Runs after flip so the synthesis can see it. Graceful — None if the
            # LLM is unavailable or there's <2 signals to fuse; the raw lenses stand.
            try:
                _mgmt_view["synthesis"] = await build_position_synthesis(_mgmt_view)
            except Exception as _sye:
                print(f"[synthesis] attach err: {_sye}")
                _mgmt_view["synthesis"] = None
        chain_packet = {
            "_type":               "perp_chain_packet",
            "chain":               chain,
            "market":              market,
            "signal_file":         f"perp_{chain.lower()}_signal.json",  # write here to fire this chain
            "scan_id":             f"perp_{chain}_{datetime.now(EST).strftime('%Y%m%d_%H%M')}_EST",
            "built_at":            _s27_now_iso(),
            "mode":                pk.get("mode"),
            # Sprint 57: fused live managing view (built here where the live per-chain
            # price + this chain's news/poly/cycle are all in scope). Falls back to
            # the pre-computed block only if somehow not in a position here.
            "position_management": _mgmt_view,
            "news":                _news,
            "macro_prints":        _macro_prints,   # released CPI numbers behind the headlines (signal only)
            "polymarket":          _poly,
            "positioning":         _posn,   # 2026-08-07: long/short, CVD, skew, reserves
            "autopsy":             autopsy_for_chain(chain),
            "playbook_templates":  _playbooks,   # The agent's #4/#5: offers, never verdicts
            "perp_budget_usd":     PERP_BUDGET_USD,   # The agent's #7: soft perp budget
            "market_now":          {"price": _price, "funding_rate_pct": _funding, "fear_greed": _fg,
                                    "price_is_authoritative": True,
                                    "note": "Live price — the SINGLE source of price truth in this packet. If "
                                            "fair_analysis shows a different price, THIS one is correct. Plus this "
                                            "chain's funding rate and fear/greed. Low fear on a long is the "
                                            "contrarian setup the autopsy grades you on."},
            "cycle_read":          chain_feed,
            "fair_analysis":       _flag_stale_fair_analysis(pk.get("fair_analysis"), _price, _sol_ammo_usd),
            "leverage_playbook":   _leverage_playbook(chain, chain_feed, venue_and_wallet, _cpos, _price, _sol_px),
            "ammo_usd":            _sol_ammo_usd,  # SOL firepower in USD (Drift is down)
            "ammo_shared_warning": {
                "raw_ammo_usd":       _sol_ammo_usd,
                "usable_for_perp_usd": _perp_usable_usd,
                "note": (f"usable_for_perp_usd "
                         f"(~{int(_PERP_SOL_SHARE*100)}% of the shared pool) is the working "
                         "capital for perps; the rest of the pool belongs to the other bots on this "
                         "wallet. Size inside it per rules/rules.md and the leverage ladder. Small "
                         "collateral is still a valid position (leverage is what makes it "
                         "meaningful; a tighter liquidation is the trade-off, not a reason to pass). "
                         "Three chains is a ceiling, not a quota. Edge discipline is unchanged: a "
                         "no-edge stand-down is a correct call. Below the venue minimum, nothing fires."),
            },
            "venue_and_wallet":    venue_and_wallet,
            "your_published_call": your_published_call,
            "track_record":        perp_close_report(market=market),
            "rotation": (lambda _r: ({
                "stage": _r.get("stage"),
                "read": _r.get("read"),
                "sol_btc_divergence_pct": _r.get("sol_btc_divergence_pct"),
                "eth_btc_divergence_pct": _r.get("eth_btc_divergence_pct"),
                "note": (f"Capital rotation stage: {_r.get('stage')}. {_r.get('read','')} "
                         "This is where the market sits in the small-cap->alt->blowoff sequence — "
                         "context for how aggressive to be. SOL leads, ETH follows; both hard = late.")
            } if isinstance(_r, dict) and _r.get("stage") else None)
            )(_feed("rotation_state", {})),
            "firing_hypotheses": (lambda _h: ({
                "firing": _h.get("firing_now", []),
                "note": ("Named theses currently firing (seed-and-track — proposed reads graded "
                         "going forward, since the euphoria approach can't be backtested). "
                         + ("; ".join(x.get("name","") for x in _h.get("firing_now", [])[:4]) if _h.get("firing_now") else "none firing")),
            } if isinstance(_h, dict) else None)
            )(_feed("hypothesis_log", {})),
            "firing_patterns": (lambda _pm: ({
                "firing": _pm.get("firing", []),
                "watching_count": _pm.get("watching_count", 0),
                "note": (lambda fr: (
                    f"{len(fr)} PROVEN historical pattern(s) firing on today's macro bins: "
                    + "; ".join(f"{p.get('description')}" for p in fr[:3])
                    + ". These caught >=5 separate past turns — real confirmation signals to weigh "
                      "(bottoms are more trustworthy than tops; history has more of them). Not a gate."
                    if fr else
                    f"No proven patterns firing right now. ({_pm.get('watching_count',0)} unproven/thin "
                    "patterns are being watched but haven't earned a live voice.)"
                ))(_pm.get("firing", [])),
            } if isinstance(_pm, dict) else None)
            )(_feed("pattern_matches", {})),
            "mechanical_scoreboard": (lambda _sb: ({
                "chain": chain,
                "mechanical_wr": _sb.get("mechanical_wr"),
                "agent_wr":     _sb.get("editorial_wr"),
                "mechanical_record": _sb.get("mechanical_record"),
                "agent_record":     _sb.get("editorial_record"),
                "when_diverged": _sb.get("when_diverged"),
                "note": (lambda wd: (
                    f"Mechanical confluence vs your calls on {chain}, graded 24h-forward. "
                    f"Mechanical {_sb.get('mechanical_record')} ({_sb.get('mechanical_wr')}% WR), "
                    f"you {_sb.get('agent_record')} ({_sb.get('agent_wr')}% WR). "
                    + (f"When you DIVERGED ({wd.get('total')} times): mechanical won {wd.get('mechanical_won')}, "
                       f"you won {wd.get('agent_won')}. "
                       + ("Mechanical is beating you when you disagree — worth asking if you're fading real edges."
                          if (wd.get('mechanical_edge_pct') or 0) > 15
                          else "You're holding your own against the machine." if (wd.get('mechanical_edge_pct') or 0) <= 0
                          else "Roughly even when you diverge.")
                       if wd and wd.get('total') else "No divergences graded yet.")
                ))(_sb.get("when_diverged") or {}),
            } if isinstance(_sb, dict) else None)
            )(((_feed("scoreboard", {}) or {}).get("summary") or {}).get(chain)),
            "override_trace":      pk.get("override_trace"),
            "data_freshness":      pk.get("data_freshness"),
            "pipeline_health":     pipeline_health,
            "active_overrides":    {k: v for k, v in (agent_overrides or {}).items() if isinstance(v, dict) and k == market},
            "feed_freshness":      macro.get("_perp_edges_updated") or macro.get("updated"),
            # Flow + live odds + news, each with source / age / stale (bots/feeds). Read-only context.
            **_feed_block_for(chain),
            "_doctrine": (f"Standalone {chain} leverage packet. Everything to make the {chain} call is "
                          "here: mode (hunt vs manage), this chain's news (bull/neutral/bear), the "
                          "cycle read, your ammo, and the leverage playbook. The other chains get "
                          "their own packets. When in a position on this chain, entry hunting is "
                          "paused and position_management carries the health."),
        }
        _cp_path = os.path.join(INBOX_PATH, f"perps_bot - perp_{chain}_{_pp_ts}.json")
        _s27_save_json(_cp_path, chain_packet)
        written.append(os.path.basename(_cp_path))
        _PERP_LAST_PACKET[str(chain).upper()] = os.path.basename(_cp_path)
        # Sprint 47: snapshot the decision surface when hunting, so a pass can be judged later
        if pk.get("mode") == "hunting":
            try:
                _poly_lean = (_poly.get("price_markets") or [{}])[0].get("implied_prob") if _poly else None
                _autopsy_log_snapshot({
                    "id": f"{chain}_{_pp_ts}", "chain": chain, "market": market,
                    "ts": datetime.now(EST).isoformat(),
                    "horizon_hours": ((chain_feed.get("coin_call") or {}).get("horizon_hours") or 72),
                    "fear_greed": (_pfi_num(macro.get("fear_greed")) if macro else None),
                    "news_net": ((_news.get("counts") or {}).get("net") if _news else None),
                    "poly_lean": _poly_lean,
                    "funding_phase": ((chain_feed.get("funding") or {}).get("phase")),
                    "phase": chain_feed.get("phase"),
                    "coin_call": chain_feed.get("coin_call"),
                    "resolved": False, "verdict": None,
                })
            except Exception as _se:
                print(f"[autopsy] snapshot build err ({chain}): {_se}")
    # Sprint 59: persist this cycle's per-chain feed state so NEXT cycle's flip
    # detector has a "last cycle" baseline to diff against. Runs every cycle,
    # in position or not, so the baseline is always one cycle fresh.
    try:
        save_feed_snapshot(macro)
    except Exception as _fce:
        print(f"[flip] snapshot save err: {_fce}")
    print(f"[s46] wrote {len(written)} per-chain perp packets ({[p.get('market') for p in packets]}), "
          f"balance ${wallet_balance:.2f}, pipeline={pipeline_health['overall']}")
    return {"written": written, "chains": [p.get("market") for p in packets], "wallet_balance": wallet_balance}


# ============================================================================
# DAILY BIAS BUILDER — runs 1x/day at 9am EST
# ============================================================================

async def build_daily_bias() -> dict:
    """Compute strategic daily bias from feed's perp_market_edges + broader
    macro. This gives the hourly packets a "regime" to check alignment against.
    """
    macro = _macro()
    perp_edges = macro.get("perp_market_edges", {}) or {}
    blowoff = macro.get("blowoff", {}) or {}
    cycle = macro.get("cycle", {}) or {}

    # Aggregate directional bias across major markets
    long_votes = short_votes = 0
    total_conviction = 0
    market_biases = {}

    for symbol, edge in perp_edges.items():
        if not isinstance(edge, dict):
            continue
        if edge.get("tier") != "major":
            continue
        directional = edge.get("directional_edge", {}) or {}
        side = directional.get("side", "NEUTRAL")
        conv = directional.get("conviction", 0)
        market_biases[symbol] = {"side": side, "conviction": conv}
        if side == "LONG":
            long_votes += 1
            total_conviction += conv
        elif side == "SHORT":
            short_votes += 1
            total_conviction -= conv

    # Macro overlay
    blowoff_signal = blowoff.get("signal", "HOLD")
    blowoff_score = blowoff.get("score", 50)
    cycle_phase = cycle.get("phase", "unknown")

    blowoff_contribution = 0
    if blowoff_signal in ("LONG", "LONG_AGGRESSIVE"):
        blowoff_contribution = 2
    elif blowoff_signal == "SHORT":
        blowoff_contribution = -2
    elif blowoff_signal == "REDUCE":
        blowoff_contribution = -1

    total_conviction += blowoff_contribution

    # ── Field fix (2026-07-19 packet: headline conviction 10/10 from three
    # markets each at 5/10): min(10, SUM of convictions) conflated BREADTH with
    # STRENGTH — unanimous-but-lukewarm read as maximum conviction, and
    # fair_analysis consumed the inflated 10 as daily_bias_snapshot in the money
    # path. Honest aggregation: conviction = mean strength of the signed net
    # across contributing voters (markets + blowoff when it votes). Breadth is
    # its own signal and now says so in the rationale.
    #   field case: 5+5+5 net 15, 3 voters → conviction 5 (was 10)
    #   contested:  LONG5 LONG5 SHORT5 → net 5, 3 voters → conviction 2 (weak, correct)
    voters = long_votes + short_votes + (1 if blowoff_contribution != 0 else 0)
    if abs(total_conviction) < 3 or voters == 0:
        daily_side = "NEUTRAL"
        daily_conv = 0
    else:
        daily_side = "LONG" if total_conviction > 0 else "SHORT"
        daily_conv = min(10, round(abs(total_conviction) / voters))
    _unanimous = (long_votes > 0 and short_votes == 0) or (short_votes > 0 and long_votes == 0)

    bias = {
        "side":             daily_side,
        "conviction":       daily_conv,
        "aggregation_note": ("conviction = mean strength across voters (breadth reported "
                             "separately); pre-fix this was a capped SUM and read 10/10 "
                             "off three 5s"),
        "unanimous_direction": _unanimous,
        "rationale":        f"{long_votes} market(s) LONG, {short_votes} SHORT "
                            f"({'unanimous' if _unanimous else 'contested'} breadth, "
                            f"mean strength {daily_conv}/10), "
                            f"blowoff={blowoff_signal}({blowoff_score}), phase={cycle_phase}",
        "market_biases":    market_biases,
        "blowoff_signal":   blowoff_signal,
        "blowoff_score":    blowoff_score,
        "cycle_phase":      cycle_phase,
        "total_conviction": total_conviction,
        "generated_at":     _s27_now_iso(),
        "valid_until":      (datetime.now(EST) + timedelta(hours=24)).isoformat(),
    }

    _s27_save_json(PERP_DAILY_BIAS_FILE, bias)
    print(f"[s27] daily bias: {daily_side} conv={daily_conv} ({bias['rationale'][:100]})")
    return bias


# ============================================================================
# EXIT MONITOR — continuous, every 2 min
# ============================================================================

async def evaluate_exit_conditions() -> dict | None:
    """Check open perp position(s) for exit triggers. Writes a suggestion
    to inbox if yes. Does NOT auto-close - The agent sees suggestion and decides.
    Returns the first suggestion dict or None if no trigger fired.

    2026-07-29: reads the PLURAL keyed store via load_positions()/save_position().
    Old path loaded dead singular perp_position.json and false-flatted live books.
    """
    store = load_positions()
    _perp_exit_suggest_prune(store)
    if not store:
        return None

    macro = _macro()
    perp_edges = macro.get("perp_market_edges", {}) or {}
    first_suggestion = None

    for market, pos in list(store.items()):
        if not isinstance(pos, dict) or not pos.get("direction"):
            continue

        direction = (pos.get("direction") or "").lower()
        entry = _s27_float(pos.get("entry_price"))
        size_usd = _s27_float(pos.get("size_usd"))
        leverage = _s27_float(pos.get("leverage")) or 1.0
        market = (pos.get("market") or market or "SOL-PERP").upper()
        opened_at = pos.get("opened_at") or pos.get("timestamp")
        # live writer uses peak_pct; older rows may still carry peak_pnl_pct
        peak_pnl_pct = _s27_float(pos.get("peak_pct"))
        if peak_pnl_pct is None:
            peak_pnl_pct = _s27_float(pos.get("peak_pnl_pct")) or 0.0

        if not entry or not direction:
            continue
        if (pos.get("owner_position") or {}).get("state") in ("pending", "leave"):
            continue

        edge = perp_edges.get(market) or {}
        # 2026-10-16: price off the venue oracle like the monitor. The macro
        # sol_price fallback was stale/rounded ($106.0) and minted fake peaks.
        try:
            current_price = await _market_mark_price(market)
        except Exception:
            current_price = None
        _live_px = bool(current_price)
        if not current_price:
            current_price = edge.get("mark_price")
        if not current_price:
            continue

        if direction == "long":
            pnl_pct = (current_price - entry) / entry * 100 * leverage
        else:
            pnl_pct = (entry - current_price) / entry * 100 * leverage
        # v1.0.3: compare on the same basis as the monitor's peak (net of fees, % of collateral)
        try:
            _hh = 0.0
            if opened_at:
                _hh = max(0.0, (datetime.now(EST) - datetime.fromisoformat(opened_at)).total_seconds() / 3600.0)
            _col_x = _s27_float(pos.get("collateral") or pos.get("collateral_usd")) or ((size_usd / leverage) if (size_usd and leverage) else 0)
            if size_usd and _col_x:
                _net_x, _ = calc_net_pnl(size_usd * (pnl_pct / leverage / 100), size_usd, _hh)
                pnl_pct = _net_x / _col_x * 100
        except Exception:
            pass

        triggers_fired = []
        suggested_action = None

        # Trigger 1: Trailing giveback = SENSOR only (soft_trail_sensor_v1, 2026-08-08)
        #
        # WAS: hardcoded `peak >= 5` with a 3pp floor, and it set suggested_action
        # = "close". Two faults. First, those numbers were never the standing
        # knobs - TRAIL_ACTIVATE_PCT is 20 and TRAIL_DROP_PCT is 15, so this fired
        # four times too early and on a band five times too tight. Second, and
        # worse, a soft giveback was being dressed as a CLOSE COMMAND.
        #
        # Soft trail is a sensor. It reports that green was handed back; it does
        # not know whether the thesis died. Close levers stay: hard SL, thesis
        # death, regime break / invalidation, an owner order, or a planned TP.
        trail_activate, trail_drop = trail_levels(
            leverage, peak_pnl_pct, pos=pos,
            base_activate=float(_perp_get_override("TRAIL_ACTIVATE_PCT", TRAIL_ACTIVATE_PCT)),
            base_drop=float(_perp_get_override("TRAIL_DROP_PCT", TRAIL_DROP_PCT)))
        trail_active_flag = bool(pos.get("trail_active")) or (peak_pnl_pct >= trail_activate)
        giveback = max(0.0, float(peak_pnl_pct) - float(pnl_pct))
        if trail_active_flag and giveback >= trail_drop:
            triggers_fired.append(
                f"soft_trail_sensor: peak {peak_pnl_pct:+.2f}% -> now {pnl_pct:+.2f}% "
                f"(gave {giveback:.1f}pp >= trail_drop {trail_drop:g}; activate {trail_activate:g})"
            )
            # review, never close - and only if nothing harder already spoke
            if suggested_action is None:
                suggested_action = "review"

        # Update peak if current is higher (write back into keyed store)
        if _live_px and pnl_pct > peak_pnl_pct:
            pos["peak_pct"] = pnl_pct
            pos["peak_pnl_pct"] = pnl_pct  # keep legacy alias in sync
            try:
                # 2026-10-16: write the peak onto a FRESH row so this pass never
                # clobbers an update_params that landed while it was pricing.
                _fresh = (load_positions() or {}).get(market)
                if isinstance(_fresh, dict) and _fresh.get("entry_price") == pos.get("entry_price"):
                    _fresh["peak_pct"] = pnl_pct
                    _fresh["peak_pnl_pct"] = pnl_pct
                    save_position(_fresh, market)
            except Exception as _pe:
                print(f"[s27 exit] peak save failed {market}: {_pe}")
            peak_pnl_pct = pnl_pct

        # Trigger 2: Time-based exit - if position is >8hr old and still flat.
        # 2026-08-08: skipped when the thesis is an explicitly NAMED floor /
        # accumulation hold. Sitting flat is the POINT of that trade; calling it
        # dead time would suggest closing the position precisely for doing what it
        # was opened to do.
        thesis_mode = (pos.get("thesis_mode") or pos.get("mode") or "").lower()
        floorish = any(k in thesis_mode for k in ("floor", "panic_floor", "accumulation"))
        if opened_at and abs(pnl_pct) < 0.5 and not floorish:
            try:
                opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                hold_hrs = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 3600
                if hold_hrs > 8:
                    triggers_fired.append(f"time_based: held {hold_hrs:.1f}h flat")
                    suggested_action = "close"
            except Exception:
                pass

        # Trigger 3: Invalidation - edge flipped against us
        directional = edge.get("directional_edge", {}) or {}
        feed_side = directional.get("side", "NEUTRAL")
        feed_conv = directional.get("conviction", 0)
        if feed_conv >= 5:
            opposing = (direction == "long" and feed_side == "SHORT") or \
                       (direction == "short" and feed_side == "LONG")
            if opposing:
                triggers_fired.append(
                    f"invalidation: feed now {feed_side}@{feed_conv} vs our {direction.upper()}"
                )
                suggested_action = "close"

        # Trigger 4: Large drawdown warning (not auto-close)
        # 2026-10-16: entry/exit fees and a stale feed mark at open are not a drawdown.
        # Skip the first 3 min and allow the round-trip fee (on collateral) on top of -4%.
        try:
            _held_s = time.time() - datetime.fromisoformat(str(pos.get("timestamp"))).timestamp()
        except Exception:
            _held_s = 1e9
        _fee_allow = 2 * float(JUPITER_TRADE_FEE_PCT) * float(leverage or 1)
        if pnl_pct < -(4 + _fee_allow) and _held_s > 180:
            triggers_fired.append(f"drawdown_warning: {pnl_pct:+.2f}%")

        if not triggers_fired:
            continue

        # ── THROTTLE (soft_trail_sensor_v1) ────────────────────────────────
        # A soft condition that is STILL TRUE is not new information. Rewriting
        # the suggestion file with a fresh suggestion_id every 120s turned one
        # observation into dozens of identical pages, and the pressure of that
        # repetition is what produced a close the thesis never asked for.
        # Re-fire only when the sig changes, the giveback band actually moves, or
        # the cooldown lapses. HARD levers are never throttled.
        hard = any(t.startswith("invalidation:") or t.startswith("time_based:")
                   for t in triggers_fired)
        soft_only = (not hard) and any(t.startswith("soft_trail_sensor:")
                                       for t in triggers_fired)
        now_ts = time.time()
        prev = _S27_LAST_EXIT_SUGGEST.get(market) or {}
        # SIGNATURE = the CONDITION, not the numbers. Joining the raw trigger
        # strings would embed live values ("peak +21.00% -> now +5.20%"), which
        # change on every 120s tick - so prev["sig"] == sig would never be true
        # and the cooldown below could never fire. The throttle would have been
        # dead on arrival and the spam it exists to stop would have shipped.
        # Materiality is judged by the giveback band, which is the right question:
        # has the situation MOVED, not has the price ticked.
        sig = "|".join(sorted(t.split(":", 1)[0] for t in triggers_fired)) + f"|{suggested_action}"

        if soft_only:
            same_band = abs(giveback - float(prev.get("giveback") if prev.get("giveback")
                                             is not None else -999)) < EXIT_SOFT_GIVEBACK_REFIRE_PP
            cool = (now_ts - float(prev.get("ts") or 0)) < EXIT_SOFT_TRAIL_COOLDOWN_S
            if prev.get("sig") == sig and same_band and cool:
                # still true, already said so - do not rewrite the inbox
                if first_suggestion is None:
                    first_suggestion = prev.get("payload")
                continue
        elif (not hard) and triggers_fired and all(t.startswith("drawdown_warning:")
                                                   for t in triggers_fired):
            # a standing drawdown warning is the same class of repetition
            if (now_ts - float(prev.get("ts") or 0)) < EXIT_SOFT_TRAIL_COOLDOWN_S:
                continue

        if hard:
            note = ("HARD exit lever candidate (invalidation/time). Still no auto-close. "
                    "The agent confirms thesis death before close.")
        elif soft_only:
            note = ("SOFT TRAIL SENSOR only. Not a close command. Thesis and SL are still "
                    "The agent's call. Close levers: hard SL, thesis death, regime break / "
                    "invalidation, the owner order, planned TP.")
        else:
            note = "Exit monitor triggered. The agent reviews and decides - no auto-close."

        suggestion = {
            "suggestion_id":      f"exit_{market}_{int(datetime.now(EST).timestamp())}",
            "market":             market,
            "direction":          direction,
            "entry_price":        entry,
            "current_price":      current_price,
            "current_pnl_pct":    round(pnl_pct, 2),
            "peak_pnl_pct":       round(peak_pnl_pct, 2),
            "giveback_pp":        round(giveback, 2),
            "trail_activate_pct": trail_activate,
            "trail_drop_pct":     trail_drop,
            "size_usd":           size_usd,
            "leverage":           leverage,
            "triggers_fired":     triggers_fired,
            "suggested_action":   suggested_action or "review",
            "severity":           "sensor" if soft_only else ("hard_candidate" if hard else "review"),
            "generated_at":       _s27_now_iso(),
            "note":               note,
        }

        _s27_save_json(PERP_EXIT_SUGGEST_FILE, suggestion)
        _S27_LAST_EXIT_SUGGEST[market] = {
            "sig": sig, "ts": now_ts, "giveback": giveback,
            "action": suggested_action, "payload": suggestion,
        }
        print(f"[s27 exit] {market} {direction} [{suggestion['severity']}] triggers: {triggers_fired}")
        if first_suggestion is None:
            first_suggestion = suggestion

    return first_suggestion


def _s27_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ============================================================================
# QUERY HANDLER — 14 actions, kalshi-pattern
# ============================================================================

_S27_LAST_FORCE_PACKET_TS = 0
_S27_LAST_RESIM_PER_MARKET = {}


async def _pqh_live_balance(params):
    # 2026-07-26: when the agent ASKS for his balance he gets a fresh on-chain
    # read, not a cached anything and certainly not the retired drift file.
    try:
        bal = await get_wallet_balance_usd()
    except Exception:
        bal = 0.0
    if not bal:
        bal = _s27_read_wallet_balance()
    return {
        "wallet_collateral_usd": round(bal, 2),
        "drift_collateral_usd": round(bal, 2),  # LEGACY ALIAS — drop once readers move
        "safe_collateral_30pct": round(bal * 0.30, 2),
        "min_balance_required": 15.0,
        "ready_to_trade": bal >= 15.0,
        "fetched_at": _s27_now_iso(),
    }, "ok"


async def _pqh_current_position(params):
    # 2026-07-29: keyed plural store. optional market param; else active_position().
    req_mkt = (params or {}).get("market")
    if req_mkt:
        market = str(req_mkt).upper()
        pos = load_position(market)
        if not pos:
            return {"has_position": False, "market": market}, "ok"
    else:
        market, pos = active_position()
        if not pos:
            return {"has_position": False}, "ok"
        market = market or pos.get("market") or "SOL-PERP"
    macro = _macro()
    edge = macro.get("perp_market_edges", {}).get(market, {})
    current = edge.get("mark_price")
    if not current and market == "SOL-PERP":
        current = macro.get("sol_price")
    pnl_pct = None
    if current and pos.get("entry_price"):
        lev = pos.get("leverage") or 1
        entry = pos["entry_price"]
        if (pos.get("direction") or "").lower() == "long":
            pnl_pct = (current - entry) / entry * 100 * lev
        else:
            pnl_pct = (entry - current) / entry * 100 * lev
    peak = pos.get("peak_pct")
    if peak is None:
        peak = pos.get("peak_pnl_pct", 0)
    return {
        "has_position":  True,
        "market":        market,
        "direction":     pos.get("direction"),
        "entry":         pos.get("entry_price"),
        "current_price": current,
        "pnl_pct":       round(pnl_pct, 2) if pnl_pct is not None else None,
        "size_usd":      pos.get("size_usd"),
        "leverage":      pos.get("leverage"),
        "tp_price":      pos.get("tp_price"),
        "sl_price":      pos.get("sl_price"),
        "liq_price":     pos.get("liq_price"),
        "opened_at":     pos.get("opened_at") or pos.get("timestamp"),
        "peak_pnl_pct":  peak,
        "peak_pct":      peak,
        "venue":         pos.get("venue"),
    }, "ok"


async def _pqh_position_pnl(params):
    pnl_data = _s27_load_json(PERP_PNL_PATH, {})
    return pnl_data, "ok"


async def _pqh_recent_perp_trades(params):
    limit = min(int(params.get("limit", 10)), 50)
    pnl_data = _s27_load_json(PERP_PNL_PATH, {})
    trades = pnl_data.get("trades", []) if isinstance(pnl_data, dict) else []
    return {
        "count":  len(trades[-limit:]),
        "trades": trades[-limit:],
    }, "ok"


async def _pqh_perp_bot_status(params):
    heartbeat_path = os.path.join(BOT_LOGS_PATH, "perps_bot_heartbeat.json")
    hb = _s27_load_json(heartbeat_path, {})
    packet = _s27_load_json(PERP_PACKET_FILE, {})
    return {
        "heartbeat":         hb,
        "last_packet_scan":  packet.get("scan_id"),
        "last_packet_at":    packet.get("generated_at"),
        "last_packet_count": packet.get("packet_count", 0),
    }, "ok"


async def _pqh_force_rebuild_packet(params):
    global _S27_LAST_FORCE_PACKET_TS
    now = time.time()
    if now - _S27_LAST_FORCE_PACKET_TS < 300:
        remaining = int(300 - (now - _S27_LAST_FORCE_PACKET_TS))
        return {"error": f"rate_limited_{remaining}s"}, "rate_limited"
    _S27_LAST_FORCE_PACKET_TS = now
    envelope = await build_perp_packets()
    return {
        "triggered":    True,
        "scan_id":      envelope.get("scan_id"),
        "packet_count": envelope.get("packet_count"),
    }, "ok"


async def _pqh_force_rebuild_daily_bias(params):
    bias = await build_daily_bias()
    return bias, "ok"


async def _pqh_live_sol_price(params):
    macro = _macro()
    edge = macro.get("perp_market_edges", {}).get("SOL-PERP", {})
    return {
        "mark_price":   edge.get("mark_price"),
        "oracle_price": edge.get("oracle_price"),
        "spread_pct":   edge.get("mark_spread_pct"),
        "macro_sol_price": macro.get("sol_price"),
        "fetched_at":   _s27_now_iso(),
    }, "ok"


async def _pqh_live_market_snapshot(params):
    market = (params.get("market") or "SOL-PERP").upper()
    macro = _macro()
    edge = macro.get("perp_market_edges", {}).get(market, {})
    if not edge:
        return {"error": f"no_edge_data_for_{market}"}, "error"
    return {
        "market":                market,
        "mark_price":            edge.get("mark_price"),
        "funding_rate_now":      edge.get("funding_rate_now"),
        "open_interest_usd":     edge.get("open_interest_usd"),
        "leaderboard_bias_pct":  (edge.get("leaderboard") or {}).get("bias_pct"),
        "vol_regime":            edge.get("vol_regime"),
        "directional_edge":      edge.get("directional_edge"),
        "funding_arb_score":     edge.get("funding_arb_score"),
        "squeeze_score":         edge.get("squeeze_score"),
        "kalshi_implied_prob":   edge.get("kalshi_implied_prob"),
        "synthesized_at":        edge.get("synthesized_at"),
    }, "ok"


async def _pqh_recent_news_for_perps(params):
    news = _s27_load_json(NEWS_CATALYSTS_PATH, {})
    catalysts = news.get("catalysts") or news.get("items") or []
    perp_relevant = []
    for c in catalysts[:30]:
        if not isinstance(c, dict):
            continue
        text = f"{c.get('headline','')} {c.get('body','')} {c.get('affected','')}".lower()
        if any(t in text for t in ("btc", "bitcoin", "sol", "solana", "eth", "ethereum", "jupiter", "perp", "funding", "liquidation")):
            perp_relevant.append(c)
    return {"count": len(perp_relevant), "catalysts": perp_relevant[:10]}, "ok"


async def _pqh_override_apply(params):
    """Apply an override to agent_overrides.json for a specific market."""
    market = params.get("market")
    override_data = params.get("override", {})
    if not market or not isinstance(override_data, dict):
        return {"error": "need market + override"}, "error"
    current = _s27_load_json(AGENT_OVERRIDES_PATH, {})
    current[market] = {
        **override_data,
        "override_at": _s27_now_iso(),
    }
    _s27_save_json(AGENT_OVERRIDES_PATH, current)
    return {"applied": True, "market": market, "override": current[market]}, "ok"


async def _pqh_sim_close(params):
    """Simulate closing current position — returns realized PnL + fees without trading."""
    # 2026-07-29: plural keyed store via active_position/load_position
    req_mkt = (params or {}).get("market")
    if req_mkt:
        pos = load_position(str(req_mkt).upper())
    else:
        _, pos = active_position()
    if not pos or not pos.get("direction"):
        return {"error": "no_open_position"}, "error"
    current_data, _ = await _pqh_current_position(params)
    if not current_data.get("has_position"):
        return {"error": "position_lost_during_sim"}, "error"

    size_usd = current_data.get("size_usd") or 0
    pnl_pct = current_data.get("pnl_pct") or 0      # collateral-relative (leveraged)
    _lev = float(pos.get("leverage") or 1)
    # Sprint 68: dollars from the PRICE move (divide leverage back out); Jupiter
    # trade fees + live borrow accrual for the hold so far.
    gross_pnl_usd = size_usd * (pnl_pct / max(_lev, 1)) / 100
    _hold_h = 0.0
    try:
        _ts = pos.get("timestamp")
        if _ts:
            _hold_h = max(0.0, (datetime.now(EST) - datetime.fromisoformat(_ts)).total_seconds() / 3600.0)
    except Exception:
        _hold_h = 0.0
    net_pnl_usd, fee_usd = calc_net_pnl(gross_pnl_usd, size_usd, _hold_h)

    return {
        "gross_pnl_pct":  round(pnl_pct, 2),
        "gross_pnl_usd":  round(gross_pnl_usd, 2),
        "fee_usd":        round(fee_usd, 2),
        "net_pnl_usd":    round(net_pnl_usd, 2),
        "note":           "simulation - no trade executed",
    }, "ok"


async def _pqh_last_packet_age(params):
    pkt = _s27_load_json(PERP_PACKET_FILE, {})
    if not pkt.get("generated_at"):
        return {"age_seconds": None, "no_packet": True}, "ok"
    try:
        gen = datetime.fromisoformat(pkt["generated_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc) if gen.tzinfo else datetime.now()
        age = (now - gen).total_seconds()
        return {
            "age_seconds":   int(age),
            "age_minutes":   round(age / 60, 1),
            "generated_at":  pkt["generated_at"],
            "scan_id":       pkt.get("scan_id"),
            "stale":         age > 3600,
        }, "ok"
    except Exception as e:
        return {"error": f"age_compute_err: {e}"}, "error"


async def _pqh_resim_market(params):
    """Re-run analyzer for a single market with fresh data."""
    market = (params.get("market") or "SOL-PERP").upper()
    global _S27_LAST_RESIM_PER_MARKET
    now = time.time()
    last = _S27_LAST_RESIM_PER_MARKET.get(market, 0)
    if now - last < 60:
        return {"error": f"rate_limited_{int(60 - (now - last))}s"}, "rate_limited"
    _S27_LAST_RESIM_PER_MARKET[market] = now

    from packet_analyzers import analyze_packet

    # Pull fresh feed data (may already have be up to date if feed swept recently)
    macro = _macro()
    base = market.split("-")[0].lower()
    context = {
        "feed_context":            {"available": True, "current_state": macro, "macro": macro},
        "wallet_balance_usd":      _s27_read_wallet_balance(),
        "wallet_balance_usd":      _s27_read_wallet_balance(),
        "drift_balance_usd":       _s27_read_wallet_balance(),  # LEGACY ALIAS — see above
        "news_catalysts":          _s27_load_json(NEWS_CATALYSTS_PATH, {}),
        "agent_thesis":           _s27_load_json(AGENT_THESIS_PATH, {}),
        "owner_overrides":          _s27_load_json(AGENT_OVERRIDES_PATH, {}),
        "existing_perp_positions": _s27_read_existing_positions(),
        "perp_daily_bias":         _s27_load_json(PERP_DAILY_BIAS_FILE, {}),
    }
    result = await analyze_packet({"vertical": f"perps_{base}", "market": market}, context)
    return {"market": market, "resim": result}, "ok"


# ═══════════════════════════════════════════════════════════════════════════
# SPRINT 40 — MID-LIFE ORDER CONTROLS (drift cancel limit, list orders)
# ═══════════════════════════════════════════════════════════════════════════
# Sprint 57: these order-management handlers are Jupiter stubs — Jupiter has no
# resting on-chain orders, so list/cancel resolve cleanly instead of erroring.
# Adds list_open_orders so the agent can see all resting orders before deciding
# which to cancel.
#
# Patterns:
#   - cancel_limit_order takes either order_id (preferred) or market+direction
#     (resolves to the most recent matching reduce_only=false order).
#   - cancel_all_orders_for_market: nuclear option, cancels every resting
#     order on a market (TP, SL, limit entries — everything reduce_only or not).

async def _pqh_list_open_orders(params):
    """Sprint 57: Jupiter has no resting on-chain orders through our path, so there
    is nothing to enumerate. (This was a Drift feature — Drift kept resting limit /
    TP / SL orders on-chain; Jupiter market-executes via keeper and the bot manages
    TP/SL itself.) Returns an empty list, not an error, so an agent call resolves
    cleanly. The one 'pending' thing that CAN exist is a parked perp_limit_order.json
    (poll-and-fire) — surfaced separately, not here."""
    return {
        "orders": [], "count": 0,
        "note": ("Jupiter has no resting on-chain orders. TP/SL is bot-side monitoring, "
                 "not resting triggers. A limit entry lives in perp_limit_order.json "
                 "(poll-and-fire), not as an on-chain order."),
    }, "ok"


async def _pqh_cancel_limit_order(params):
    """Sprint 57: no resting on-chain orders on Jupiter, so there's nothing to
    cancel here. To cancel a PARKED limit entry, delete perp_limit_order.json.
    To exit an OPEN position, use the close action. Returns a clear note, not a
    crash."""
    return {
        "cancelled": False,
        "note": ("Jupiter has no resting on-chain orders to cancel. To cancel a parked "
                 "limit entry, remove perp_limit_order.json; to exit an open position, "
                 "fire a close signal."),
    }, "ok"


async def _pqh_cancel_all_orders_for_market(params):
    """Sprint 57: no resting on-chain orders on Jupiter — nothing to sweep. TP/SL
    is bot-side, not on-chain triggers. To flatten a market, fire a close signal
    for the open position; to drop a parked limit, remove perp_limit_order.json."""
    return {
        "cancelled_count": 0,
        "note": ("Jupiter has no resting on-chain orders to sweep. Close the open "
                 "position via a close signal; remove perp_limit_order.json for a "
                 "parked limit entry."),
    }, "ok"


async def _pqh_feed_packet(params):
    """Pull now (default all lanes; odds always live) and return the agent packet for a chain."""
    if _feed_router is None:
        return {"error": "feeds module unavailable"}, "error"
    chain = str((params or {}).get("chain") or "SOL").upper().replace("-PERP", "")
    lanes = tuple((params or {}).get("lanes") or _feed_router.LANES)
    pk = await asyncio.to_thread(_feed_router.build_agent_packet, chain, None, lanes)
    return pk, "ok"


async def _pqh_pull_feeds(params):
    if _feed_router is None:
        return {"error": "feeds module unavailable"}, "error"
    lanes = tuple((params or {}).get("lanes") or ("flow", "news"))
    await asyncio.to_thread(_feed_router.pull, lanes)
    return _feed_router.status(), "ok"


_PERP_QUERY_ACTIONS = {
    "feed_packet":              _pqh_feed_packet,
    "pull_feeds":               _pqh_pull_feeds,
    "live_balance":             _pqh_live_balance,
    "current_position":         _pqh_current_position,
    "position_pnl":             _pqh_position_pnl,
    "recent_perp_trades":       _pqh_recent_perp_trades,
    "perp_bot_status":          _pqh_perp_bot_status,
    "force_rebuild_packet":     _pqh_force_rebuild_packet,
    "force_rebuild_daily_bias": _pqh_force_rebuild_daily_bias,
    "live_sol_price":           _pqh_live_sol_price,
    "live_market_snapshot":     _pqh_live_market_snapshot,
    "recent_news_for_perps":    _pqh_recent_news_for_perps,
    "override_apply":           _pqh_override_apply,
    "sim_close":                _pqh_sim_close,
    "last_packet_age":          _pqh_last_packet_age,
    "resim_market":             _pqh_resim_market,
    # Sprint 40: mid-life order controls
    "list_open_orders":         _pqh_list_open_orders,
    "cancel_limit_order":       _pqh_cancel_limit_order,
    "cancel_all_orders_for_market": _pqh_cancel_all_orders_for_market,
}


async def process_perp_query(query: dict) -> dict:
    """Process a query from the agent, return response dict ready to write."""
    query_id = query.get("query_id", "no_id")
    action = query.get("action", "")
    params = query.get("params", {}) or {}

    resp = {
        "query_id":     query_id,
        "requested_at": query.get("requested_at", ""),
        "answered_at":  _s27_now_iso(),
        "action":       action,
        "bot":          "perps_bot",
        "bot_version":  "v5.7",
    }

    if action not in _PERP_QUERY_ACTIONS:
        resp["status"] = "error"
        resp["error"]  = f"unknown_action_{action}"
        resp["data"]   = {"available_actions": sorted(_PERP_QUERY_ACTIONS.keys())}
        return resp

    handler = _PERP_QUERY_ACTIONS[action]
    try:
        data, status = await handler(params)
        resp["status"] = status
        resp["data"]   = data
        if status == "error":
            resp["error"] = data.get("error", "unknown_error") if isinstance(data, dict) else "unknown_error"
        else:
            resp["error"] = None
    except Exception as e:
        resp["status"] = "error"
        resp["error"]  = f"handler_exception: {str(e)[:200]}"
        resp["data"]   = None

    return resp


async def check_perp_query():
    """Poll perp_query.json, process, write response. Called in query loop."""
    if not os.path.exists(PERP_QUERY_FILE):
        return
    try:
        query = _s27_load_json(PERP_QUERY_FILE, None)
        if not query or not isinstance(query, dict):
            return
        resp = await process_perp_query(query)
        _s27_save_json(PERP_RESPONSE_FILE, resp)
        # Remove the query file now that we've responded
        try:
            os.remove(PERP_QUERY_FILE)
        except Exception:
            pass
        print(f"[s27 query] {query.get('action')} -> {resp.get('status')}")
    except Exception as e:
        print(f"[s27 query] err: {e}")


# ============================================================================
# BACKGROUND LOOPS
# ============================================================================

async def packet_builder_loop():
    """Hourly packet builder loop."""
    print("[s27] packet builder loop started")
    # Initial packet on startup
    try:
        await build_perp_packets()
    except Exception as e:
        print(f"[s27] initial packet build err: {e}")

    while True:
        await asyncio.sleep(_perp_get_override("PACKET_INTERVAL_SECONDS", PACKET_INTERVAL_SECONDS))
        try:
            await build_perp_packets()
        except Exception as e:
            print(f"[s27] packet build err: {e}")


async def daily_bias_loop():
    """Checks every 5 min if it's 9am EST and daily bias hasn't run today."""
    print("[s27] daily bias loop started")
    last_run_date = None
    while True:
        await asyncio.sleep(300)
        now = datetime.now(EST)
        if now.hour == DAILY_BIAS_HOUR_EST and now.date() != last_run_date:
            try:
                await build_daily_bias()
                last_run_date = now.date()
            except Exception as e:
                print(f"[s27] daily bias err: {e}")


async def exit_monitor_loop():
    """Continuous exit monitor - every 2 min checks for trigger fires."""
    print("[s27] exit monitor loop started")
    while True:
        await asyncio.sleep(EXIT_MONITOR_INTERVAL)
        try:
            await evaluate_exit_conditions()
        except Exception as e:
            print(f"[s27] exit monitor err: {e}")


async def query_handler_loop():
    """Poll perp_query.json every 5s, respond via perp_response.json."""
    print("[s27] query handler loop started")
    while True:
        await asyncio.sleep(QUERY_POLL_INTERVAL)
        try:
            await check_perp_query()
        except Exception as e:
            print(f"[s27] query handler err: {e}")


# End of Sprint 27 module


# ══════════════════════════════════════════════════════════════════════════════
# SPRINT 32 v2.7: MORNING PACKET (6:05AM EST DAILY)
# ──────────────────────────────────────────────────────────────────────────────
# Sequenced after feed's 6:00am thesis. Reads feed's morning packet for
# regime context and perp_signal.json's horse_race_state, composes a brief, and
# writes it to the agent's inbox.
#
# DOCTRINE LOCKS:
#  1. perps_bot WRITES the brief to inbox. It does NOT create any meeting file.
#     The agent is the only initiator of meeting files. If the agent wants to act on
#     the brief, the agent creates perp_meeting.json — perps_bot only reads it.
#  2. horse_race leverage_recommendation is INFO ONLY in this packet. The agent
#     reviews and decides. Auto-wiring leverage to trading decisions is a
#     future-session deliverable because real positions are running.
# ══════════════════════════════════════════════════════════════════════════════

def _is_perps_morning_packet_due(state_file_path: str) -> bool:
    """6:05am EST cron, once per day. Returns True if not yet fired today and time has passed."""
    state = {}
    try:
        if os.path.exists(state_file_path):
            with open(state_file_path, "r", encoding="utf-8") as f:
                state = json.load(f)
    except Exception:
        state = {}

    now = datetime.now(EST)
    today_str = now.strftime("%Y-%m-%d")
    today_605am = now.replace(hour=6, minute=5, second=0, microsecond=0)

    if now < today_605am:
        return False

    if state.get("last_fired_date") == today_str:
        return False

    return True


def _record_perps_morning_packet_fired(state_file_path: str) -> None:
    now = datetime.now(EST)
    state = {
        "last_fired_at":   now.isoformat(),
        "last_fired_date": now.strftime("%Y-%m-%d"),
        "next_fire_eligible_after": (now + timedelta(days=1)).replace(
            hour=6, minute=5, second=0, microsecond=0
        ).isoformat(),
    }
    try:
        os.makedirs(os.path.dirname(state_file_path), exist_ok=True)
        with open(state_file_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[perps morning] state write err: {e}")


def _read_feed_morning_thesis_perps(feed_packet_path: str) -> dict:
    """Read feed's 6:00am morning thesis for regime context."""
    return _feed("morning_thesis", {}) or {}


def _read_perp_signal_horse_race(perp_signal_path: str) -> dict:
    """
    Read perp_signal.json's horse_race_state field (added by feed v2.7).
    Returns horse_race_state subdict or empty if not present.
    """
    if not os.path.exists(perp_signal_path):
        return {}
    try:
        with open(perp_signal_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data.get("horse_race_state", {}) or {}
    except Exception:
        return {}


def _read_recent_perp_pnl_24h() -> float:
    """Sum realized pnl from PNL_FILE for trades closed in last 24h."""
    try:
        if not os.path.exists(PNL_FILE):
            return 0.0
        with open(PNL_FILE) as f:
            data = json.load(f)
        trades = data.get("trades", []) or []
        cutoff = datetime.now(EST) - timedelta(hours=24)
        total = 0.0
        for t in trades:
            ts_raw = t.get("timestamp")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=EST)
                if ts >= cutoff:
                    total += float(t.get("pnl_usd", 0) or 0)
            except Exception:
                continue
        return round(total, 4)
    except Exception:
        return 0.0


def _perps_morning_regime_fallback() -> dict:
    """2026-10-17: regime + horse race for the morning brief when the morning
    thesis packet is missing. Built from the macro snapshot and the daily read
    (same data the packets use) so the brief never shows '?/unknown' while
    that data exists. Read-only; returns empty parts when a source is absent."""
    m = _macro() or {}
    dr = _feed("daily_read", {}) or {}
    src = "feed:macro+daily_read"
    if not isinstance(m, dict):
        m = {}
    if not isinstance(dr, dict):
        dr = {}
    bc = m.get("brain_consensus") if isinstance(m.get("brain_consensus"), dict) else {}
    tr = dr.get("trade_recommendation") if isinstance(dr.get("trade_recommendation"), dict) else {}
    hr = dr.get("horse_race") if isinstance(dr.get("horse_race"), dict) else {}
    macro = {}
    if m or dr:
        macro = {
            "blowoff_signal":   m.get("blowoff_signal") or bc.get("final_signal") or "?",
            "phase":            m.get("cycle_phase") or m.get("phase") or dr.get("current_phase") or "?",
            "phase_source":     m.get("phase_source") or dr.get("phase_source") or "?",
            "phase_suggestion": m.get("cycle_phase_suggestion") or dr.get("phase_suggestion"),
            "btc_price":        m.get("btc_price"),
            "blowoff_score":    m.get("blowoff_score") if m.get("blowoff_score") is not None else dr.get("current_score"),
        }
    brain = {"signal": bc.get("final_signal")} if bc.get("final_signal") else {}
    inv = tr.get("invariants") if isinstance(tr.get("invariants"), dict) else {}
    invariants = {"top_signal_strength_pct": inv.get("top_strength_pct")} if inv else {}
    horse = {}
    if hr or tr.get("perps"):
        _read = next((v for k, v in hr.items() if str(k).endswith("horse_race_read") and v), None)
        horse = {
            "state":                   hr.get("state") or "?",
            "state_explanation":       _read or hr.get("explanation"),
            "leverage_recommendation": tr.get("perps") or "unknown",
            "leverage_rec_reasoning":  tr.get("reasoning"),
            "current_actual":          dr.get("current_score"),
            "regime":                  dr.get("regime"),
            "regime_break":            hr.get("regime_break", dr.get("regime_break_signal")),
        }
    return {"macro": macro, "brain": brain, "invariants": invariants, "horse": horse, "source": src}


async def fire_perps_morning_brief():
    """
    Compose and write perps_bot's morning brief to the agent's inbox.

    NOTE: This packet INFORMS the agent but does NOT auto-execute leverage changes.
    Per the owner's session lock: horse race input is shown to the agent, the agent decides
    if leverage strategy needs to change. If the agent wants to enact changes, AGENT
    creates a meeting file — perps_bot does NOT create meeting files itself.
    """
    feed_packet = _read_feed_morning_thesis_perps(FEED_MORNING_PACKET_FILE)
    feed_macro = feed_packet.get("macro_snapshot", {})
    feed_brain = feed_packet.get("brain_consensus", {})
    feed_horse = feed_packet.get("horse_race_standings", {})
    feed_invariants = feed_packet.get("invariants_firing", {})

    # Also pull horse race directly from perp_signal.json (most fresh)
    direct_horse = _read_perp_signal_horse_race(PERP_SIGNAL_FILE_FOR_HORSE)
    horse_state = direct_horse if direct_horse else feed_horse

    # 2026-10-17: fall back to the macro snapshot + daily read when the morning
    # thesis packet is missing, instead of reporting '?' / 'unknown'.
    regime_source = "feed_morning_packet" if feed_macro else None
    horse_source = "perp_signal" if direct_horse else ("feed_morning_packet" if feed_horse else None)
    if not feed_macro or not horse_state:
        try:
            _fb = _perps_morning_regime_fallback()
        except Exception as _fbe:
            print(f"[perps morning] regime fallback err: {_fbe}")
            _fb = {}
        if not feed_macro and _fb.get("macro"):
            feed_macro = _fb["macro"]
            feed_brain = feed_brain or _fb.get("brain") or {}
            feed_invariants = feed_invariants or _fb.get("invariants") or {}
            regime_source = _fb.get("source")
        if not horse_state and _fb.get("horse"):
            horse_state = _fb["horse"]
            horse_source = _fb.get("source")

    # Get perps_bot internal state via existing accessors
    open_positions = []
    recent_pnl = 0.0
    account_balance = 0.0

    try:
        open_positions = _s27_read_existing_positions() or []
    except Exception as e:
        print(f"[perps morning] positions snapshot err: {e}")

    try:
        recent_pnl = _read_recent_perp_pnl_24h()
    except Exception as e:
        print(f"[perps morning] pnl snapshot err: {e}")

    sol_balance = 0.0
    sol_ammo_usd = None
    try:
        _mconn = AsyncClient(RPC_URL, commitment=Confirmed)
        sol_balance = await get_sol_balance(_mconn)
        try:
            await _mconn.close()
        except Exception:
            pass
        _sp = _pfi_num((_macro() or {}).get("sol_price"))
        sol_ammo_usd = round(sol_balance * _sp, 2) if (sol_balance and _sp) else None
        try:
            _maybe_stamp_wallet_balance_fallback(sol_balance, sol_ammo_usd)
        except Exception:
            pass
    except Exception as e:
        print(f"[perps morning] SOL balance snapshot err: {e}")

    leverage_rec = horse_state.get("leverage_recommendation", "unknown")

    # Map leverage_recommendation to a sizing multiplier (info-only display, NOT applied)
    sizing_multiplier_hint = {
        "zero":                              0.0,
        "small":                             0.3,
        "moderate_with_forecast":            0.6,
        "moderate_against_forecast":         0.6,
        "high":                              1.0,
        "high_directional_with_forecast":    1.0,
        "high_directional_against_forecast": 1.0,
        "neutral":                           0.5,
    }.get(leverage_rec, 0.5)

    packet = {
        "id":       f"perps_morning_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "from":     "perps_bot",
        "to":       "agent",
        "type":     "morning_perps_brief",
        "fired_at": datetime.now(EST).isoformat(),
        "regime_context_from_feed": {
            "feed_packet_present":       bool(feed_packet),
            "regime_source":             regime_source or "none",
            "blowoff_signal":            feed_macro.get("blowoff_signal", feed_brain.get("signal", "?")),
            "phase":                     feed_macro.get("phase", "?"),
            "phase_source":              feed_macro.get("phase_source", "?"),
            "phase_suggestion":          feed_macro.get("phase_suggestion"),
            "btc_price":                 feed_macro.get("btc_price"),
            "blowoff_score":             feed_macro.get("blowoff_score"),
            "top_invariants_firing_pct": feed_invariants.get("top_signal_strength_pct"),
        },
        "horse_race_standings": {
            "state":                   horse_state.get("state", "?"),
            "explanation":             horse_state.get("state_explanation") or horse_state.get("explanation"),
            "leverage_recommendation": leverage_rec,
            "sizing_multiplier_hint":  sizing_multiplier_hint,
            "convergence_zone":        horse_state.get("convergence_zone"),
            "current_actual":          horse_state.get("current_actual"),
            "forecast_today":          horse_state.get("forecast_today"),
            "devils_today":            horse_state.get("devils_today"),
            "regime":                  horse_state.get("regime"),
            "leverage_rec_reasoning":  horse_state.get("leverage_rec_reasoning"),
            "source":                  horse_source or "none",
            "info_only_note": (
                "horse_race recommendation is INFO ONLY. perps_bot does NOT auto-apply this. "
                "The agent reviews and decides whether to issue trading directives based on it. "
                "Auto-wiring is a future-session change."
            ),
        },
        "perps_state": {
            "open_positions_count":    len(open_positions),
            "open_positions":          open_positions[:5] if isinstance(open_positions, list) else [],
            "recent_realized_pnl_24h": recent_pnl,
            # 2026-07-27: the only PnL view was /perpstats, a Telegram command
            # The owner types. The bot placing the trades could not see its own record.
            # Same rules as the scoreboard so the two can never disagree.
            "realized_pnl": perps_realized_summary(),
            "ammo_sol":                round(sol_balance, 4),
            "ammo_usd":                sol_ammo_usd,  # SOL firepower (Drift is down)
        },
        "agent_prompt_question": (
            "Given feed's regime read and the horse race standings above, "
            "what's the perps strategy for today? Increase/decrease leverage? Close positions? "
            "Open new ones? Wait? "
            "If you want to enact changes, YOU create perp_meeting.json with directives — "
            "perps_bot will read it. perps_bot does not create meeting files itself. "
            "For an immediate halt, write to agent_signals.json, or restart perps with PERPS_DRY_RUN=true "
            "(paper mode: fills are simulated and nothing is signed or sent)."
        ),
        "doctrine": (
            "Daily 6:05am ritual. Perps brief delivered with feed's regime + horse race "
            "context. Sequenced architecture (feed first at 6:00am, perps at 6:05am). "
            "Horse race output is shown to the agent but NOT auto-applied to trading decisions yet. "
            "Auto-wiring is a separate session because real money is running through perps. "
            "MEETING DOCTRINE: perps_bot only writes inbox briefs. The agent is the sole initiator "
            "of meeting files. perps_bot can READ a meeting file the agent creates, never CREATE one."
        ),
    }

    try:
        os.makedirs(INBOX_DIR, exist_ok=True)
        # Sprint 45 inbox doctrine: conforming, timestamped filename so each
        # morning brief is unique. A fixed name ("perps_morning_brief.json")
        # collided in inbox_results/done/, overwriting prior snapshots.
        _pm_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _pm_path = os.path.join(INBOX_DIR, f"perps_bot - morning_brief_{_pm_ts}.json")
        with open(_pm_path, "w", encoding="utf-8") as f:
            json.dump(packet, f, indent=2)
        _record_perps_morning_packet_fired(PERPS_MORNING_STATE_FILE)
        print(
            f"[perps morning] 6:05AM RITUAL: morning brief fired "
            f"(regime={horse_state.get('state', '?')}, leverage_rec={leverage_rec}, "
            f"open_positions={len(open_positions)}, pnl_24h=${recent_pnl:.2f})"
        )
    except Exception as e:
        print(f"[perps morning] write err: {e}")


async def feed_loop():
    """Feed requests + optional cadence. FEED_CADENCE=manual (default): never pulls on its
    own; only a json_relay/feed_request.json from the agent triggers a pull.
    FEED_CADENCE=<minutes>: also pulls flow + news when the last pull is that old.
    Request: {"lanes": ["flow","news","odds"], "chains": ["SOL","BTC","ETH"]} -> one
    "perps_bot - feed_<chain>_<ts>.json" per chain in inbox/, request file deleted."""
    if _feed_router is None:
        return
    req = _feed_router.request_path()
    last = 0.0
    while True:
        try:
            if os.path.exists(req):
                try:
                    with open(req, encoding="utf-8") as fh:
                        r = json.load(fh) or {}
                except Exception:
                    r = {}
                try:
                    os.remove(req)
                except Exception:
                    pass
                lanes = tuple(r.get("lanes") or _feed_router.LANES)
                pulled = await asyncio.to_thread(_feed_router.pull, lanes)
                ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
                for ch in (r.get("chains") or ["SOL", "BTC", "ETH"]):
                    ch = str(ch).upper().replace("-PERP", "")
                    pk = {"chain": ch, "built_at": _s27_now_iso(), "request": r,
                          **_feed_router.build_feed_block(ch, pulled)}
                    _s27_save_json(os.path.join(INBOX_PATH, f"perps_bot - feed_{ch}_{ts}.json"), pk)
                last = time.time()
                print(f"[feeds] request served: lanes={lanes}")
            cad = _feed_router.cadence()
            if cad and time.time() - last >= cad * 60:
                pulled = await asyncio.to_thread(_feed_router.pull, _feed_router.LANES)
                last = time.time()
                print(f"[feeds] cadence pull ({cad:g} min): " + ", ".join(
                    f"{k}={(v or {}).get('lane_source') or (v or {}).get('source') or '?'}" for k, v in (pulled or {}).items()))
        except Exception as e:
            print(f"[feeds] loop err: {e}")
        await asyncio.sleep(PERPS_MEETING_POLL_S)


async def perps_morning_packet_loop():
    """Polls every 60s, fires the morning brief once per day at 6:05am EST."""
    print("[perps morning] morning packet loop starting (will fire at 6:05am EST daily)")
    while True:
        try:
            if _is_perps_morning_packet_due(PERPS_MORNING_STATE_FILE):
                await fire_perps_morning_brief()
        except Exception as e:
            print(f"[perps morning] cycle err: {e}")
        await asyncio.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
# SPRINT 32+: MEETING FILES (TRANSIENT, AGENT-INITIATED, LLM-ON-DEMAND)
# ──────────────────────────────────────────────────────────────────────────────
# DOCTRINE LOCKS (per the owner):
#  1. perps_bot does NOT auto-create perp_meeting.json. The file appears only
#     when the agent writes one. Watcher uses os.path.exists, NOT mtime polling.
#  2. The SI brain (optional) stays asleep when no meeting file is present.
#     Wakes only on file detection, tears down after processing. VRAM is free.
#  3. After processing, bot writes reply to inbox\perps_bot - meeting_response_*.md
#     and DELETES the meeting file. json_relay\ folder returns to empty.
#  4. Overrides live in runtime memory (_PERP_ACTIVE_OVERRIDES). Hybrid persistence:
#     a snapshot is written to PERP_OVERRIDES_SNAPSHOT on every change for audit/manual
#     reload, but bot does NOT auto-reload it on boot. Restart = clean slate; the agent
#     re-asserts overrides if needed (matches transient meeting doctrine).
#  5. Hard-locked params (credentials, safety floors, fees, market indexes) NEVER
#     tunable via meeting. Schema validation rejects any directive targeting them.
# ══════════════════════════════════════════════════════════════════════════════

# Size, leverage, and capital are NOT meeting-tunable. Code defaults below stay
# as written. A meeting cannot change them.
_PERP_CAPITAL_LOCKED = {
    "MAX_LEVERAGE", "AUTO_SIZE_USD", "JUPITER_MAX_LEVERAGE",
    "JUPITER_MAX_POSITION_USD", "JUPITER_REGIME_SCALE", "AUTO_LEVERAGE",
    "AUTO_COLLATERAL", "DEFAULT_SIZE_USD", "MIN_COLLATERAL_USD",
    "BASE_LEVERAGE_FOR_REGIME", "REGIME_SCALE_BTC", "REGIME_SCALE_SOL",
    "REGIME_SCALE_ETH", "REGIME_SCALE_JUP",
}

# Tunable parameter schema. ONLY these can be changed via meeting note.
# Anything not in this dict is HARD-LOCKED and meeting interpreter will reject.
TUNABLE_PARAMS = {
    # ── EMERGENCY TOGGLE ───────────────────────────────────────────────
    "NEW_POSITIONS_ENABLED": {
        "type": "bool", "default": NEW_POSITIONS_ENABLED,
        "description": "Allow new positions from agent signals. False = long/short/add signals are declined "
                       "(reason new_positions_off); closes, adjust, update_params, close_pct and pull_initial "
                       "still work. The bot never opens a trade on its own. Use to halt new exposure without stopping the bot.",
    },
    "ENTRY_ALERTS_ENABLED": {
        "type": "bool", "default": ENTRY_ALERTS_ENABLED,
        "description": "Intel scanner entry alerts. False = no entry alerts; packets, exit alerts and agent signals are unaffected.",
    },

    # ── LEVERAGE & SIZING ──────────────────────────────────────────────

    # ── SIGNAL THRESHOLDS ──────────────────────────────────────────────
    "LONG_SCORE_MAX": {
        "type": "float", "min": 0.0, "max": 100.0, "default": 30.0,
        "unit": "score",
        "description": "Long entry trigger: score must be BELOW this (oversold). Default 30. Lower = stricter long entry.",
    },
    "SHORT_SCORE_MIN": {
        "type": "float", "min": 0.0, "max": 100.0, "default": 72.0,
        "unit": "score",
        "description": "Short entry trigger: score must be ABOVE this (overbought). Default 72. Higher = stricter short entry.",
    },
    "LONG_FG_MAX": {
        "type": "float", "min": 0.0, "max": 100.0, "default": 28.0,
        "unit": "fear-greed index",
        "description": "Long entry: Fear & Greed must be BELOW this (extreme fear). Default 28.",
    },
    "SHORT_FG_MIN": {
        "type": "float", "min": 0.0, "max": 100.0, "default": 72.0,
        "unit": "fear-greed index",
        "description": "Short entry: Fear & Greed must be ABOVE this (extreme greed). Default 72.",
    },
    "FUNDING_SHORT_MIN": {
        "type": "float", "min": 0.0, "max": 1.0, "default": 0.04,
        "unit": "rate",
        "description": "Funding rate threshold for short entries. Higher funding = stronger short bias. Default 0.04.",
    },

    # ── EXIT MANAGEMENT ────────────────────────────────────────────────
    "TRAIL_ACTIVATE_PCT": {
        "type": "float", "min": 5.0, "max": 200.0, "default": 20.0,
        "unit": "percent",
        "description": "Trail arms at this % of collateral (net of fees) at 5x; scaled up with leverage by PERP_TRAIL_LEV_SCALE. Default 20%.",
    },
    "TRAIL_DROP_PCT": {
        "type": "float", "min": 1.0, "max": 50.0, "default": 15.0,
        "unit": "percent",
        "description": "Trail giveback from peak, in % of collateral points at 5x; scaled with leverage (cap 50). Default 15.",
    },
    "AUTO_CLOSE_MIN_HOLD_HOURS": {
        "type": "float", "min": 0.0, "max": 168.0, "default": 4.0,
        "unit": "hours",
        "description": "Minimum hold time before auto-close logic activates. Default 4h. Set 0 to disable hold-floor.",
    },

    # ── CADENCE ────────────────────────────────────────────────────────
    "SCAN_INTERVAL_SECONDS": {
        "type": "int", "min": 60, "max": 7200, "default": 900,
        "unit": "seconds",
        "description": "Intelligence scanner cadence. Default 15min. Min 1min, max 2h.",
    },

    # ── META: MEETING SYSTEM CONTROLS ──────────────────────────────────
    "MEETING_LOW_CONFIDENCE_THRESHOLD": {
        "type": "float", "min": 0.30, "max": 0.95, "default": 0.50,
        "unit": "0-1",
        "description": "Confidence floor for auto-applying a directive. Below this, status downgrades to clarification_needed. Default 0.50.",
    },

    # ── SPRINT 39 / 57: JUPITER VENUE KNOBS (failover machinery removed) ──
    "JUPITER_PERPS_MAX_SLIPPAGE_BPS": {
        "type": "int", "min": 10, "max": 500, "default": 50,
        "unit": "bps",
        "description": "Max slippage tolerance for Jupiter Perps orders. Default 50 bps. Higher = orders fill more often but at worse prices.",
    },
    "JUPITER_ENABLED_MARKETS": {
        "type": "list_str", "default": ["SOL-PERP", "BTC-PERP", "ETH-PERP"],
        "description": "Which markets the bot will open on Jupiter. A signal for a market not in this list is refused.",
    },
    # ── SPRINT 44: RISK FLOORS & EXIT BUFFERS ──────────────────────────
    "TP_ALERT_BUFFER_PCT": {
        "type": "float", "min": 0.0, "max": 20.0, "default": 2.0, "unit": "%",
        "description": "How close (%) price must get to TP before the fast-poll alert kicks in.",
        "hint": "Wider = react earlier near take-profit.",
    },
    "SL_ALERT_BUFFER_PCT": {
        "type": "float", "min": 0.0, "max": 20.0, "default": 2.0, "unit": "%",
        "description": "How close (%) price must get to SL before the fast-poll alert kicks in.",
        "hint": "Wider = react earlier near stop-loss.",
    },

    # ── SPRINT 44: REGIME LEVERAGE SCALING ─────────────────────────────

    # ── SPRINT 44: AUTO SIZING ─────────────────────────────────────────
    "SCALP_SCORE_MOVE": {
        "type": "float", "min": 0.0, "max": 50.0, "default": 8.0, "unit": "pts",
        "description": "Score move threshold that flips entries into scalp mode.",
        "hint": "Lower = enter scalp mode on smaller moves.",
    },
    "PROPOSAL_EXPIRY_HOURS": {
        "type": "float", "min": 0.25, "max": 48.0, "default": 3.0, "unit": "hours",
        "description": "How long you have to act on a proposal before it expires.",
        "hint": "Shorter = proposals stay fresh; longer = more time to decide.",
    },

    # ── SPRINT 44: JUPITER FUNDING (SOL->USDC collateral swap) ─────────
    "JUPITER_SWAP_SLIPPAGE_BPS": {
        "type": "int", "min": 10, "max": 1000, "default": 100, "unit": "bps",
        "description": "Slippage tolerance on the SOL->USDC funding swap for a Jupiter open. 100 = 1%.",
        "hint": "Raise if funding swaps fail in volatile conditions; costs more on the swap.",
    },
    "JUPITER_FUND_BUFFER_BPS": {
        "type": "int", "min": 0, "max": 1000, "default": 150, "unit": "bps",
        "description": "Overshoot the funding amount by this (bps) so fees/rounding don't leave you short.",
        "hint": "Raise if funding swaps land just short; small efficiency cost.",
    },
    "JUPITER_SOL_FEE_RESERVE": {
        "type": "float", "min": 0.005, "max": 0.5, "default": 0.02, "unit": "SOL",
        "description": "Native SOL held back for tx fees/rent during funding (physically required to submit).",
        "hint": "Don't set too low or transactions fail to submit.",
    },

    # ── SPRINT 44: CADENCES (how often things fire) ────────────────────
    "PACKET_INTERVAL_SECONDS": {
        "type": "int", "min": 600, "max": 21600, "default": 3600, "unit": "seconds",
        "description": "How often perps builds + sends you a market packet (the read you trade off).",
        "hint": "Raise for fewer/calmer packets; lower to react faster to regime shifts. 3600=hourly.",
    },
    "PRICE_POLL_SECONDS": {
        "type": "int", "min": 5, "max": 120, "default": 30, "unit": "seconds",
        "description": "Position/price monitor poll interval.",
        "hint": "Lower = tighter TP/SL reaction but more RPC load; raise if you're getting rate-limited.",
    },
    "FAST_POLL_SECONDS": {
        "type": "int", "min": 2, "max": 60, "default": 8, "unit": "seconds",
        "description": "Faster poll used when price is near TP or SL.",
        "hint": "Lower = catch the exact touch near exits; costs more RPC calls.",
    },
    "SIGNAL_POLL": {
        "type": "int", "min": 2, "max": 60, "default": 10, "unit": "seconds",
        "description": "How often the OG signal relay (perp_signal.json) is polled.",
        "hint": "This is the legacy fast-execution path; keep low for snappy fills.",
    },
    "PERPS_MEETING_POLL_S": {
        "type": "int", "min": 10, "max": 300, "default": 30, "unit": "seconds",
        "description": "How often I check for your tune/meeting directives.",
        "hint": "Keep low so your tunes apply almost immediately; raising it delays your own changes.",
    },

    # ── SPRINT 39: PER-MARKET AUTO-DISABLE ─────────────────────────────
    "PER_MARKET_AUTO_DISABLE_ENABLED": {
        "type": "bool", "default": True,
        "description": "Master switch for per-market auto-disable. When True and a single market accumulates filter-related skips above threshold, bot writes a meeting directive to add that market to MARKET_BLOCKLIST. Default True.",
    },
    "MARKET_BLOCKLIST": {
        "type": "list_str", "default": [],
        "description": "Markets EXCLUDED from packet building even if they would otherwise have signals. Use to skip markets that aren't performing. Valid markets: SOL-PERP, BTC-PERP, ETH-PERP. Example: ['ETH-PERP'].",
    },
}

# ════════════════════════════════════════════════════════════════════════
#  ACTIVE OVERRIDES + EXPIRY TRACKING (sprint 32+ — direct-orders doctrine)
# ════════════════════════════════════════════════════════════════════════
# Storage shape: {param_name: {"value": v, "expires_at": iso_or_None, "applied_at": iso, "reason": str}}
# Public API _perp_get_override(name, fallback) unwraps ["value"] for callers.
# Background expiry watcher clears expired entries every 60s and writes a P2
# "override expired" packet to the agent's inbox so he sees the auto-rollback.

_PERP_ACTIVE_OVERRIDES: dict = {}


def _perp_get_override(param_name: str, fallback):
    """Resolution order (Sprint 44): active TTL override > saved default (tuner) > code default.
    Lock-free read (dict reads are atomic in CPython). Expiry of active overrides is handled
    by the background expiry watcher; when it clears an expired entry, this naturally falls
    through to the saved default (if any), else the code default."""
    entry = _PERP_ACTIVE_OVERRIDES.get(param_name)
    if entry is not None:
        # Sprint 32+: entries are dict-shaped {value, expires_at, applied_at, reason}.
        # Backward compat: if a raw value got stored, return it as-is.
        if isinstance(entry, dict) and "value" in entry:
            return entry["value"]
        return entry
    # No active override — fall to the agent's saved default (the SAVE layer)
    if param_name in _PERP_SAVED_DEFAULTS:
        return _PERP_SAVED_DEFAULTS[param_name]
    return fallback


def _perp_validate_expires_at(expires_at_iso) -> tuple:
    """Sanity-check the LLM's expires_at output.
    Returns (is_valid, parsed_dt_or_None, error_message).
    Rules: must be parseable ISO datetime, must be in the future (>30s buffer)."""
    if not expires_at_iso:
        return (True, None, "")  # null is valid (means permanent)
    if not isinstance(expires_at_iso, str):
        return (False, None, f"expires_at must be ISO string or null, got {type(expires_at_iso).__name__}")
    try:
        dt = datetime.fromisoformat(expires_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST)
        now = datetime.now(EST)
        if dt <= now + timedelta(seconds=30):
            return (False, None, f"expires_at {expires_at_iso} is in the past or too soon (must be >30s in the future)")
        return (True, dt, "")
    except (ValueError, TypeError) as e:
        return (False, None, f"expires_at not parseable as ISO datetime: {e}")


def _perp_now_iso() -> str:
    return datetime.now(EST).isoformat()


def _perp_meeting_audit(record: dict) -> None:
    """Append-only audit log for every meeting interaction (perp_meeting_audit.jsonl)."""
    record["timestamp"] = _perp_now_iso()
    try:
        os.makedirs(os.path.dirname(PERP_MEETING_AUDIT_LOG), exist_ok=True)
        with open(PERP_MEETING_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[perps meeting] audit log err: {e}")


def _perp_save_overrides_snapshot() -> None:
    """Hybrid persistence: write current overrides to disk for audit + manual reload.
    Bot does NOT auto-load this on boot — the agent can manually replay if needed."""
    snapshot = {
        "saved_at": _perp_now_iso(),
        "overrides": dict(_PERP_ACTIVE_OVERRIDES),
        "note": "audit snapshot only. perps_bot does not auto-load on boot. To restore, write a meeting note re-asserting these.",
    }
    try:
        os.makedirs(os.path.dirname(PERP_OVERRIDES_SNAPSHOT), exist_ok=True)
        with open(PERP_OVERRIDES_SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        print(f"[perps meeting] snapshot write err: {e}")


# ════════════════════════════════════════════════════════════════════════
#  SAVE LAYER — the agent's tuned defaults (Sprint 44 tuning system)
#  bots\tuner is the truth. Saved defaults sit BENEATH active TTL overrides and
#  ABOVE code defaults; they survive note-expiry AND restart (re-loaded at boot).
#  Verbs: set (temp, optional ttl) | save_default (permanent) | reset (-> code default)
# ════════════════════════════════════════════════════════════════════════
_PERP_SAVED_DEFAULTS: dict = {}   # {param: value} — loaded from perps_tuned_defaults.json at boot


def _perp_code_default(param):
    """The hardcoded code default for a param (from TUNABLE_PARAMS)."""
    return TUNABLE_PARAMS.get(param, {}).get("default")


def _perp_coerce_and_validate(param, value) -> tuple:
    """Coerce + range-check a value against TUNABLE_PARAMS[param].
    Returns (ok, coerced_or_None, err). Same rules the meeting interpreter enforces,
    so a saved default can never be something a live tune couldn't also set."""
    if param not in TUNABLE_PARAMS:
        return (False, None, f"param '{param}' not in tunable schema (hard-locked)")
    spec = TUNABLE_PARAMS[param]
    ptype = spec.get("type")
    try:
        if ptype == "bool":
            value = bool(value)
        elif ptype == "int":
            value = int(value)
        elif ptype in ("float", "usd"):
            value = float(value)
        # list_str / str pass through unchanged
    except (ValueError, TypeError):
        return (False, None, f"value '{value}' not coercible to {ptype}")
    if ptype in ("int", "float", "usd"):
        pmin, pmax = spec.get("min"), spec.get("max")
        if pmin is not None and value < pmin:
            return (False, None, f"{value} below min {pmin}")
        if pmax is not None and value > pmax:
            return (False, None, f"{value} above max {pmax}")
    return (True, value, "")


_LEGACY_SWITCH = "AUTO_TRADE_ENABLED"
_SPLIT_SWITCHES = ("NEW_POSITIONS_ENABLED", "ENTRY_ALERTS_ENABLED")


def _perp_expand_legacy_switch(d: dict, where: str) -> dict:
    """Deprecated AUTO_TRADE_ENABLED -> both new switches (only where they are not set)."""
    if _LEGACY_SWITCH in d:
        v = d.pop(_LEGACY_SWITCH)
        for k in _SPLIT_SWITCHES:
            d.setdefault(k, v)
        print(f"[perps tuner] deprecated: AUTO_TRADE_ENABLED in {where} read as "
              f"NEW_POSITIONS_ENABLED + ENTRY_ALERTS_ENABLED")
    return d


def _perp_load_saved_defaults() -> None:
    """Boot: load the agent's saved defaults from the tuner folder. Each is re-validated
    against the CURRENT schema, so anything now unknown or out-of-range is dropped
    (e.g., a range tightened in a later sprint) rather than silently mis-applied."""
    global _PERP_SAVED_DEFAULTS
    _PERP_SAVED_DEFAULTS = {}
    if not os.path.exists(PERPS_TUNED_DEFAULTS_FILE):
        print("[perps tuner] no saved defaults yet (fresh) — using code defaults")
        return
    try:
        with open(PERPS_TUNED_DEFAULTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[perps tuner] saved-defaults load err: {e} — using code defaults")
        return
    saved = raw.get("defaults", {}) if isinstance(raw, dict) else {}
    saved = _perp_expand_legacy_switch(dict(saved), "saved defaults") if isinstance(saved, dict) else {}
    kept, dropped = {}, []
    for param, value in saved.items():
        ok, coerced, err = _perp_coerce_and_validate(param, value)
        if ok:
            kept[param] = coerced
        else:
            dropped.append(f"{param} ({err})")
    _PERP_SAVED_DEFAULTS = kept
    if kept:
        print(f"[perps tuner] loaded {len(kept)} saved default(s): {list(kept.keys())}")
    if dropped:
        print(f"[perps tuner] dropped {len(dropped)} invalid saved default(s): {dropped}")


def _perp_persist_saved_defaults() -> None:
    """Write the in-memory saved defaults back to the tuner folder (atomic-ish)."""
    payload = {
        "_updated": _perp_now_iso(),
        "_note": "The agent's saved tuning defaults. Loaded at boot as the baseline beneath "
                 "active TTL overrides and above code defaults. bots\\tuner is the truth.",
        "defaults": dict(_PERP_SAVED_DEFAULTS),
    }
    try:
        os.makedirs(os.path.dirname(PERPS_TUNED_DEFAULTS_FILE), exist_ok=True)
        with open(PERPS_TUNED_DEFAULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"[perps tuner] saved-defaults persist err: {e}")


def _perp_tune_history_append(record: dict) -> None:
    """Append one change to the tune-history ledger — the feedback spine. Every
    set / save_default / reset lands here so the 6h tune packet can show what
    changed and the agent can correlate tunes with outcomes (find his edge)."""
    record["timestamp"] = _perp_now_iso()
    try:
        os.makedirs(os.path.dirname(PERPS_TUNE_HISTORY_FILE), exist_ok=True)
        with open(PERPS_TUNE_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[perps tuner] tune-history append err: {e}")


def _perp_save_default(param, value, reason="") -> tuple:
    """save_default verb: persist a new permanent baseline (survives expiry + restart).
    Validated against schema. Returns (ok, coerced_or_None, err)."""
    ok, coerced, err = _perp_coerce_and_validate(param, value)
    if not ok:
        return (False, None, err)
    old = _PERP_SAVED_DEFAULTS.get(param, _perp_code_default(param))
    _PERP_SAVED_DEFAULTS[param] = coerced
    _perp_persist_saved_defaults()
    _perp_tune_history_append({
        "action": "save_default", "param": param, "old": old, "new": coerced, "reason": reason,
    })
    print(f"[perps tuner] save_default {param}: {old} -> {coerced}")
    return (True, coerced, "")


def _perp_reset_default(param, reason="") -> tuple:
    """reset verb: drop saved default AND clear any active override -> back to code default.
    Returns (ok, code_default, err)."""
    if param not in TUNABLE_PARAMS:
        return (False, None, f"param '{param}' not in tunable schema")
    had_saved  = _PERP_SAVED_DEFAULTS.pop(param, None)
    had_active = _PERP_ACTIVE_OVERRIDES.pop(param, None)
    if had_saved is not None:
        _perp_persist_saved_defaults()
    cd = _perp_code_default(param)
    _perp_tune_history_append({
        "action": "reset", "param": param,
        "from_saved":  had_saved,
        "from_active": (had_active.get("value") if isinstance(had_active, dict) else had_active),
        "new": cd, "reason": reason,
    })
    print(f"[perps tuner] reset {param} -> code default {cd}")
    return (True, cd, "")


# ════════════════════════════════════════════════════════════════════════
#  TUNE-INVITATION SYSTEM (Sprint 44 — Part 3)
#  Every TUNE_PACKET_INTERVAL_HOURS, write a BRIEF to the agent's inbox: full
#  tunable surface + current values + ranges + recent perf + what changed.
#  The agent reads it and (per Meeting Doctrine) decides whether to initiate a
#  meeting (perp_meeting.json). Bots write briefs; the agent initiates meetings.
#  Also mirrors the live surface to perps_params.json (the "truth" file).
# ════════════════════════════════════════════════════════════════════════
PERPS_TUNE_PACKET_STATE = os.path.join(TUNER_DIR, "perps_tune_packet_state.json")


def _perp_build_surface_snapshot() -> dict:
    """Full tunable surface with current resolution per knob. Reused by the
    perps_params.json mirror and the tune-invitation brief."""
    out = {}
    for name, spec in TUNABLE_PARAMS.items():
        code_def = spec.get("default")
        saved    = _PERP_SAVED_DEFAULTS.get(name)
        active   = _PERP_ACTIVE_OVERRIDES.get(name)
        active_val = active.get("value") if isinstance(active, dict) else active
        active_exp = active.get("expires_at") if isinstance(active, dict) else None
        out[name] = {
            "type": spec.get("type"), "min": spec.get("min"), "max": spec.get("max"),
            "unit": spec.get("unit"),
            "code_default": code_def,
            "saved_default": saved,
            "active_override": ({"value": active_val, "expires_at": active_exp} if active is not None else None),
            "effective": _perp_get_override(name, code_def),
            "description": spec.get("description", ""), "hint": spec.get("hint", ""),
        }
    return out


def _perp_write_params_mirror() -> None:
    """Mirror the live tunable surface to perps_params.json so the tuner folder
    always reflects what the bot actually exposes (the readable 'truth')."""
    payload = {
        "_updated": _perp_now_iso(),
        "_note": "Full tunable surface for perps_bot. bots\\tuner is the truth. Read this to "
                 "know what you can tune and the valid ranges. Change values by writing a "
                 "meeting directive to bots\\json_relay\\perp_meeting.json.",
        "resolution_order": "active TTL override > saved default (perps_tuned_defaults.json) > code default",
        "verbs": {
            "set": "temporary; optional ttl_hours; reverts to saved/code default on expiry",
            "save_default": "permanent new baseline; survives expiry and restart",
            "reset": "drop saved default + active override -> back to code default",
        },
        "params": _perp_build_surface_snapshot(),
    }
    try:
        os.makedirs(TUNER_DIR, exist_ok=True)
        with open(PERPS_PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"[perps tuner] mirrored {len(payload['params'])} params -> perps_params.json")
    except Exception as e:
        print(f"[perps tuner] params mirror err: {e}")


def _perp_recent_perf_since(since_iso: str) -> dict:
    """Realized perf since a timestamp (the feedback loop). Reads perp_pnl.json."""
    res = {"trades": 0, "realized_usd": 0.0, "wins": 0, "win_rate": None}
    try:
        if not os.path.exists(PNL_FILE):
            return res
        with open(PNL_FILE) as f:
            data = json.load(f)
        since_dt = None
        if since_iso:
            try: since_dt = datetime.fromisoformat(since_iso)
            except Exception: since_dt = None
        for t in data.get("trades", []):
            ts = t.get("timestamp")
            if since_dt and ts:
                try:
                    if datetime.fromisoformat(ts) < since_dt:
                        continue
                except Exception:
                    pass
            res["trades"] += 1
            pnl = t.get("pnl_usd", 0) or 0
            res["realized_usd"] += pnl
            if pnl > 0:
                res["wins"] += 1
        res["realized_usd"] = round(res["realized_usd"], 2)
        if res["trades"]:
            res["win_rate"] = round(100 * res["wins"] / res["trades"], 1)
    except Exception as e:
        print(f"[perps tuner] perf read err: {e}")
    return res


def _perp_recent_tune_changes(n: int = 8) -> list:
    """Last n entries from the tune-history ledger."""
    try:
        if not os.path.exists(PERPS_TUNE_HISTORY_FILE):
            return []
        with open(PERPS_TUNE_HISTORY_FILE) as f:
            lines = [l for l in f if l.strip()]
        out = []
        for l in lines[-n:]:
            try: out.append(json.loads(l))
            except Exception: pass
        return out
    except Exception:
        return []


def _perp_tune_state_read() -> dict:
    try:
        if os.path.exists(PERPS_TUNE_PACKET_STATE):
            with open(PERPS_TUNE_PACKET_STATE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _perp_tune_state_write(d: dict) -> None:
    try:
        os.makedirs(TUNER_DIR, exist_ok=True)
        with open(PERPS_TUNE_PACKET_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        print(f"[perps tuner] tune-state write err: {e}")


def _perp_fmt(v):
    return "—" if v is None else f"{v}"


def _perp_write_expiry_packet_to_inbox(expired_entries: list) -> str:
    """Write a packet to the agent's inbox notifying that one or more overrides
    auto-expired and reverted to default. Returns inbox file path."""
    if not expired_entries:
        return ""
    ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
    fname = f"perps_bot - override_expired_{ts}.md"
    inbox_path = os.path.join(INBOX_DIR, fname)

    body = f"""# perps_bot override expired

**from:** perps_bot
**to:** agent
**fired_at:** {_perp_now_iso()}
**type:** auto_expiry_notification

The following override(s) reached their `expires_at` time and have been cleared from runtime memory. Behavior has reverted to the default (or any older still-active override on the same param).

## expired overrides

"""
    for entry in expired_entries:
        body += f"- **{entry['param']}**: was `{entry['value']}`, expired at `{entry['expires_at']}`\n"
        body += f"  - applied at: {entry.get('applied_at', 'unknown')}\n"
        if entry.get("reason"):
            body += f"  - original reason: {entry['reason']}\n"
        body += "\n"

    body += """---

*This packet was generated automatically by perps_bot's expiry watcher. No action needed unless you want to re-apply the override — write a fresh meeting file if so.*
"""

    try:
        os.makedirs(INBOX_DIR, exist_ok=True)
        with open(inbox_path, "w", encoding="utf-8") as f:
            f.write(body)
        return inbox_path
    except Exception as e:
        print(f"[perps expiry] inbox write err: {e}")
        return ""


async def perp_expiry_watcher_loop():
    """Sprint 32+: scan _PERP_ACTIVE_OVERRIDES every 60s, clear entries past expires_at,
    write a packet to the agent's inbox so he sees the auto-rollback.

    Doctrine: 'for 6 hours' means 6 hours, not until-The agent-says-otherwise.
    Direct orders honor their own time bounds.
    """
    print("[perps expiry] watcher starting (60s cadence, honors expires_at)")
    while True:
        try:
            now = datetime.now(EST)
            expired_entries = []
            params_to_clear = []
            for param, entry in list(_PERP_ACTIVE_OVERRIDES.items()):
                if not isinstance(entry, dict):
                    continue  # legacy shape, skip
                expires_at_iso = entry.get("expires_at")
                if not expires_at_iso:
                    continue  # permanent override, skip
                try:
                    exp_dt = datetime.fromisoformat(expires_at_iso)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=EST)
                    if now >= exp_dt:
                        expired_entries.append({
                            "param": param,
                            "value": entry.get("value"),
                            "expires_at": expires_at_iso,
                            "applied_at": entry.get("applied_at"),
                            "reason": entry.get("reason"),
                        })
                        params_to_clear.append(param)
                except Exception as e:
                    print(f"[perps expiry] bad expires_at on {param}: {e}, clearing it")
                    params_to_clear.append(param)
                    expired_entries.append({
                        "param": param,
                        "value": entry.get("value"),
                        "expires_at": expires_at_iso,
                        "applied_at": entry.get("applied_at"),
                        "reason": "invalid expires_at format, force-cleared",
                    })

            if params_to_clear:
                for param in params_to_clear:
                    _PERP_ACTIVE_OVERRIDES.pop(param, None)
                    print(f"[perps expiry] CLEARED {param}, expired at {next((e['expires_at'] for e in expired_entries if e['param'] == param), '?')}")
                _perp_save_overrides_snapshot()
                inbox_path = _perp_write_expiry_packet_to_inbox(expired_entries)
                if inbox_path:
                    print(f"[perps expiry] expiry notification → {inbox_path}")
                _perp_meeting_audit({
                    "event": "overrides_auto_expired",
                    "expired": expired_entries,
                    "active_overrides_after": dict(_PERP_ACTIVE_OVERRIDES),
                })
        except Exception as e:
            print(f"[perps expiry] watcher cycle err: {e}")
        await asyncio.sleep(60)


# Sprint 61b: the helper records which model/endpoint ACTUALLY served the last
# call, so callers (e.g. the meeting response's interpreted_by) can label the true
# source instead of hardcoding the local model name. "cloud:<model>" or "local:<model>".
_PERP_LAST_LLM_SOURCE = "none"

def _perp_brain_order() -> list:
    """Provider order from LLM_MAIN / LLM_FALLBACK (bots/.env; the console's Brain Setup
    writes them). Defaults: grok main, local ollama fallback. "off" skips that slot.
    Read at call time, so a changed .env applies on the next start (load_dotenv runs at boot)."""
    ok = ("grok", "ollama", "off")
    m = (os.getenv("LLM_MAIN") or "grok").strip().lower()
    f = (os.getenv("LLM_FALLBACK") or "ollama").strip().lower()
    m = m if m in ok else "grok"
    f = f if f in ok else "ollama"
    out = []
    for prov in (m, f):
        if prov != "off" and prov not in out:
            out.append(prov)
    return out


def _perp_brain_enabled() -> bool:
    """False when LLM_MAIN and LLM_FALLBACK are both off. Brain jobs (flip digest,
    fused brief, loss patterns, gap classifier) then skip; trading is unaffected."""
    return bool(_perp_brain_order())


_STRICT_FORM_HELP = ("Use exact form: 'set PARAM VALUE', 'default PARAM VALUE' or 'reset PARAM' "
                     "(several separated by ';'), or JSON {\"translated_to\": [{\"param\": ..., "
                     "\"action\": \"set\", \"new\": ...}]}.")


def _perp_strict_directive(message: str, current_overrides: dict) -> dict:
    """No-brain directive parser. Accepts only the exact forms in _STRICT_FORM_HELP.
    Anything else -> clarification_needed asking for exact form. Pure, no network."""
    cur = current_overrides or {}
    names = {n.lower(): n for n in TUNABLE_PARAMS}
    msg = str(message or "").strip()
    items, bad = [], []

    def _val(v):
        if isinstance(v, str):
            t = v.strip()
            if t.lower() in ("true", "on", "yes"):
                return True
            if t.lower() in ("false", "off", "no"):
                return False
            try:
                return json.loads(t)
            except Exception:
                return t
        return v

    def _add(param, action, new):
        if str(param or "").strip().upper() == _LEGACY_SWITCH:   # deprecated: sets both switches
            for _k in _SPLIT_SWITCHES:
                _add(_k, action, new)
            return
        n = names.get(str(param or "").strip().lower())
        if not n or action not in ("set", "save_default", "reset"):
            bad.append(str(param)); return
        items.append({"param": n, "action": action,
                      "old": cur.get(n, TUNABLE_PARAMS[n].get("default")),
                      "new": None if action == "reset" else _val(new), "reason": "exact-form directive"})

    data = None
    if msg[:1] in ("{", "["):
        try:
            data = json.loads(msg)
        except Exception:
            data = None
    if isinstance(data, dict) and isinstance(data.get("translated_to"), list):
        for c in data["translated_to"]:
            if isinstance(c, dict):
                _add(c.get("param"), (c.get("action") or "set").lower(), c.get("new", c.get("value")))
    elif isinstance(data, dict) and data.get("param"):
        _add(data.get("param"), (data.get("action") or "set").lower(), data.get("new", data.get("value")))
    elif data is None:
        for part in re.split(r"[;\n]+", msg):
            w = part.strip().split(None, 2)
            if not w:
                continue
            verb = w[0].lower()
            if verb == "reset" and len(w) >= 2:
                _add(w[1], "reset", None)
            elif verb in ("set", "default") and len(w) == 3:
                _add(w[1], "set" if verb == "set" else "save_default", w[2])
            else:
                bad.append(part.strip()[:40])
    if items and not bad:
        return {"status": "applied", "interpretation": "exact-form directive: " +
                ", ".join(f"{i['action']} {i['param']}" for i in items),
                "confidence": 1.0, "translated_to": items, "expires_at": None,
                "clarification_question": "", "error": "", "interpreted_by": "strict_parser"}
    return {"status": "clarification_needed", "interpretation": "(SI brain off or unavailable)",
            "confidence": 0.0, "translated_to": [], "expires_at": None,
            "clarification_question": ("Could not read: " + ", ".join(bad) + ". " if bad else "") + _STRICT_FORM_HELP,
            "error": "", "interpreted_by": "strict_parser"}


async def _perp_ollama_generate(prompt: str, *, model: str, format_json: bool = True,
                                 max_tokens: int = 600, timeout_s: float = None) -> str:
    """Generate text with the configured brain: LLM_MAIN first, then LLM_FALLBACK.
    grok = xAI chat/completions (GROK_API_KEY); ollama = the LOCAL daemon at OLLAMA_URL
    with `model`. Returns the text, or "" when every configured provider fails."""
    global _PERP_LAST_LLM_SOURCE
    timeout_s = timeout_s if timeout_s is not None else PERPS_OLLAMA_TIMEOUT_S
    for prov in _perp_brain_order():
        if prov == "grok":
            _gt, _gm = await _grokc_text(prompt, max_tokens=max_tokens, timeout=PERPS_GROK_TIMEOUT_S,
                                         json_mode=bool(format_json), temperature=0.3)
            if _gt:
                _PERP_LAST_LLM_SOURCE = f"grok:{_gm}"
                return _gt
            print(f"[llm] grok unavailable ({_gm}) - trying next brain")
        elif prov == "ollama":
            payload = {"model": model, "prompt": prompt, "stream": False,
                       "options": {"num_predict": max_tokens, "temperature": 0.3}}
            if format_json:
                payload["format"] = "json"
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    r = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
                    r.raise_for_status()
                    _resp = r.json().get("response", "") or ""
                    if _resp:
                        _PERP_LAST_LLM_SOURCE = f"local:{model}"
                        return _resp
            except Exception as e:
                print(f"[llm] local ollama err (model={model}): {e}")
    return ""


# ── GROK CLOUD STEP (2026-10-14, the owner: Ollama cloud retired) ─────────────────
# Key: GROK_API_KEY / XAI_API_KEY. Model: GROK_TEXT_MODEL pins one, else the
# built-in ladder. Local Ollama stays the fallback.
_GROKC_KEY = os.getenv("GROK_API_KEY", "") or os.getenv("XAI_API_KEY", "")
_GROKC_PIN = os.getenv("GROK_TEXT_MODEL", "auto").strip()
_GROKC_BENCH = {"until": 0.0}


def _grokc_models() -> list:
    if _GROKC_PIN and _GROKC_PIN.lower() != "auto":
        return [_GROKC_PIN]
    return ["grok-4.7", "grok-4.6", "grok-4.5"]


async def _grokc_text(prompt: str, max_tokens: int = 800, timeout: float = 60,
                      json_mode: bool = False, temperature: float = 0.3):
    """Returns (text, model) or ("", reason). Never raises."""
    if not _GROKC_KEY:
        return "", "no GROK_API_KEY"
    if time.time() < _GROKC_BENCH["until"]:
        return "", "grok benched (recent 401/403/429)"
    last = "?"
    try:
        async with httpx.AsyncClient(timeout=timeout) as _c:
            for _m in _grokc_models():
                _body = {"model": _m, "max_tokens": max(64, int(max_tokens)), "temperature": temperature,
                         "messages": [{"role": "user", "content": prompt}]}
                if json_mode:
                    _body["response_format"] = {"type": "json_object"}
                _r = await _c.post("https://api.x.ai/v1/chat/completions",
                                   headers={"Authorization": f"Bearer {_GROKC_KEY}"}, json=_body)
                if _r.status_code == 200:
                    _t = ((((_r.json().get("choices") or [{}])[0]).get("message") or {}).get("content") or "").strip()
                    if _t:
                        return _t, _m
                    last = f"{_m} empty"
                    continue
                last = f"{_m} HTTP {_r.status_code}"
                if _r.status_code in (401, 403, 429):
                    _GROKC_BENCH["until"] = time.time() + 300
                    break
    except Exception as _e:
        last = f"{type(_e).__name__}: {_e!r}"[:160]
    return "", last


def _perp_parse_llm_json(raw: str) -> dict:
    """Robustly parse JSON from LLM output. Handles <think> blocks, ```json fences,
    trailing commas, truncated JSON."""
    if not raw:
        return {}
    text = str(raw).strip()
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    text = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    if start < 0:
        return {}
    match = re.search(r"\{[\s\S]*\}", text)
    candidate = match.group(0) if match else text[start:]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass
    fixed = re.sub(r",(\s*[}\]])", r"\1", candidate)
    try:
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        pass
    open_b, close_b = fixed.count("{"), fixed.count("}")
    if open_b > close_b:
        balanced = fixed.rstrip().rstrip(",") + ("}" * (open_b - close_b))
        try:
            return json.loads(balanced)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _perp_build_interpreter_prompt(message: str, current_overrides: dict) -> str:
    """Build the Ollama prompt for interpreting a meeting directive."""
    schema_lines = []
    for name, spec in TUNABLE_PARAMS.items():
        ptype = spec.get("type", "")
        bounds = ""
        if "min" in spec and "max" in spec:
            bounds = f" range[{spec['min']}-{spec['max']}]"
        unit = f" ({spec.get('unit', '')})" if spec.get("unit") else ""
        default = spec.get("default")
        cur = current_overrides.get(name, default)
        cur_marker = " [OVERRIDDEN]" if name in current_overrides else ""
        schema_lines.append(
            f"  {name}: {ptype}{bounds}{unit}, default={default}, current={cur}{cur_marker}\n"
            f"    {spec.get('description', '')}"
        )
    schema_text = "\n".join(schema_lines)

    return f"""/no_think

You are interpreting an operator directive to perps_bot, the Jupiter perpetuals trading bot.

The bot has these TUNABLE parameters the operator can adjust through plain-language messages:

{schema_text}

OPERATOR DIRECTIVE:
"{message}"

Your job: translate the operator's directive into structured parameter changes.

RULES:
1. Only change parameters in the schema above. If the directive targets something not in the schema (credentials, safety floors, file paths, fees), it's HARD-LOCKED. Return status="failed" with error explaining the param is locked.
2. If the directive is ambiguous, return status="clarification_needed" with a short question.
3. If the directive maps cleanly, return status="applied" with the parameter changes.
4. Bound checks are MANDATORY. If a directive asks to set a value outside the range, return status="failed".
5. **TIME BOUNDS ARE DIRECT ORDERS** — when the directive says "for 6 hours", "until 3pm", "for the rest of today", "this week", you MUST set `expires_at` to that exact future ISO datetime. The bot will auto-revert at that time. If no time bound is mentioned, set `expires_at` to null.
6. **VERBS** — each change has an "action":
   - "set" (DEFAULT): a temporary/session change. Use with expires_at for time-bounded ("for 6h"). With null expires_at it holds until restart or the next change.
   - "save_default": a PERMANENT new baseline that survives restart. Use when the directive says "save", "make it the default", "from now on", "permanently", "always", "keep it at".
   - "reset": revert a param to its original code default (drops saved default AND active override). Use when the directive says "reset", "back to default", "undo", "revert". For reset, "new" can be null.
7. Today's date in EST: {_perp_now_iso()[:19]}
8. Confidence: 0.0 to 1.0, how certain you are about your interpretation.

Output ONLY valid JSON, no commentary, no markdown, no <think> blocks:
{{
  "status": "applied" | "clarification_needed" | "failed",
  "interpretation": "one-sentence plain-language summary of what you understood",
  "confidence": 0.0-1.0,
  "translated_to": [
    {{"param": "PARAM_NAME", "action": "set" | "save_default" | "reset", "old": <current_value>, "new": <new_value>, "reason": "..."}}
  ],
  "expires_at": "YYYY-MM-DDTHH:MM:SS-04:00" or null,
  "clarification_question": "" or "what to ask the operator if status=clarification_needed",
  "error": "" or "why if status=failed"
}}"""


def _perp_load_self_description() -> dict:
    """Sprint 41: load the bot's own _self_description.json for gap-detection
    fallback in the meeting watcher. Returns {} if unavailable."""
    try:
        bot_logs = WS_BOT_LOGS
        path = os.path.join(bot_logs, "perps_bot_self_description.json")
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


async def _perp_classify_gap_via_llm(param: str, directive_message: str, self_desc: dict) -> dict:
    """Sprint 41 classifier — RESTORED 2026-07-26. It was CALLED at the gap-routing
    site but never defined anywhere in this file, so the try-block raised NameError
    on its first line every single time and the except swallowed it. Net effect:
    capability-gap routing has never once worked and nothing has ever reached
    code_requests.json through this path. Found by pyflakes, not by reading.

    Decides why a directive named a param that doesn't exist:
      "hard_locked"    — the param is deliberately absent; the agent may not have it
      "capability_gap" — a real missing capability worth a code_request
      "ambiguous"      — unclear; surface to the owner, do NOT auto-route

    Returns {"classification": str, "reasoning": str}. Never raises — on any
    failure it returns "ambiguous", which is the path that asks a human.
    """
    if not _perp_brain_enabled():
        return {"classification": "ambiguous", "reasoning": "SI brain off - needs manual review"}
    try:
        known = sorted(list((self_desc or {}).get("tunable_params", {}).keys()))[:60]
        locked = sorted(list((self_desc or {}).get("hard_locked_params", []) or []))[:60]
        prompt = (
            "You classify a trading bot's rejected parameter request. Reply ONLY with JSON.\n\n"
            f"REQUESTED PARAM: {param}\n"
            f"DIRECTIVE TEXT: {str(directive_message)[:600]}\n\n"
            f"TUNABLE PARAMS THAT EXIST: {known}\n"
            f"DELIBERATELY HARD-LOCKED: {locked}\n\n"
            "Classify as exactly one of:\n"
            "  hard_locked    - it matches or is equivalent to a hard-locked param\n"
            "  capability_gap - a real capability the bot lacks and plausibly should have\n"
            "  ambiguous      - you cannot tell confidently\n\n"
            'Reply: {"classification": "...", "reasoning": "one sentence"}'
        )
        raw = await _perp_ollama_generate(prompt, model=PERPS_OLLAMA_MODEL,
                                          format_json=True, max_tokens=250)
        if not raw:
            raw = await _perp_ollama_generate(prompt, model=PERPS_OLLAMA_MODEL,
                                              format_json=True, max_tokens=250)
        parsed = _perp_parse_llm_json(raw) or {}
        cls = str(parsed.get("classification", "")).strip().lower()
        if cls not in ("hard_locked", "capability_gap", "ambiguous"):
            return {"classification": "ambiguous",
                    "reasoning": f"classifier returned unusable value {cls!r} — needs manual review"}
        return {"classification": cls,
                "reasoning": str(parsed.get("reasoning", ""))[:400] or "no reasoning given"}
    except Exception as e:
        return {"classification": "ambiguous", "reasoning": f"classifier error: {e!r}"}


def _perp_write_code_request_for_gap(param: str, directive_message: str,
                                      interp: dict, gap_classification: dict) -> str:
    """A real capability gap stays in the agent's inbox as his own unfinished patch.
    The agent applies it himself. Nothing routes to owner_inbox.
    Returns the request id."""
    req_id = f"CTR-perps-meeting-{datetime.now(EST).strftime('%Y%m%d%H%M%S')}"

    # Description of the gap for the agent. He applies his own patch.
    description = (
        f"perps_bot meeting watcher detected a CAPABILITY GAP, not a HARD-LOCKED rejection.\n\n"
        f"## original directive\n"
        f'"{directive_message}"\n\n'
        f"## LLM interpretation\n"
        f"The bot LLM interpreted the directive and proposed param '{param}' but "
        f"this param does NOT exist in TUNABLE_PARAMS. Classification result: "
        f"{gap_classification.get('classification', 'unclear')}\n\n"
        f"## Classification reasoning\n"
        f"{gap_classification.get('reasoning', '(no reasoning)')}\n\n"
        f"## Proposed change\n"
        f"{gap_classification.get('proposed_change', '(no proposal)')}\n\n"
        f"## Suggested tunable shape\n"
        f"```python\n{gap_classification.get('suggested_tunable_def', '(none)')}\n```\n\n"
        f"## Risk assessment\n"
        f"{gap_classification.get('risk_notes', '(none)')}\n"
    )

    # Capability gaps are PACKETS in the agent's inbox. He applies his own patch.
    # code_requests / code_tuner / a 9am cron / the owner-applies are not this path.
    ts_file = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
    packet = {
        "id":              req_id,
        "type":            "capability_gap",
        "received_at":     datetime.now(EST).isoformat(),
        "priority":        "normal",
        "from_user": {"username": "@perps_bot", "friendship_tier": "system",
                       "user_id": "perps_bot"},
        "requester":       "perps_bot_meeting_watcher",
        "bot":             "perps_bot.py",
        "originating_directive":  directive_message[:500],
        "proposed_param_name":    param,
        "gap_classification":     gap_classification.get("classification"),
        "description":     description,
        "_doctrine": ("A capability gap, not a HARD-LOCKED rejection. This packet "
                       "stays in your inbox as your own unfinished patch. You apply "
                       "it yourself. The bot cannot do this itself."),
        "actions_taken":   [],
    }
    try:
        _gap_path = os.path.join(INBOX_PATH, f"perps_bot - capability_gap_{ts_file}.json")
        with open(_gap_path + ".tmp", "w", encoding="utf-8") as _f:
            json.dump(packet, _f, indent=2, default=str)
        os.replace(_gap_path + ".tmp", _gap_path)
    except Exception as e:
        try:
            log(f"capability_gap packet write failed ({req_id}): {e}")
        except Exception:
            pass
    return req_id

def _perp_validate_interpretation(interp: dict) -> dict:
    """Validate LLM interpretation against schema. Catches: param not in schema,
    value outside bounds, type mismatch."""
    status = interp.get("status", "failed")
    if status not in ("applied", "clarification_needed", "failed"):
        interp["status"] = "failed"
        interp["error"] = f"invalid status from LLM: {status}"
        return interp

    if status == "applied":
        translated = interp.get("translated_to", [])
        if not isinstance(translated, list) or not translated:
            interp["status"] = "failed"
            interp["error"] = "status=applied but no translated_to changes"
            return interp

        _tx = []
        for change in translated:
            if isinstance(change, dict) and str(change.get("param") or "").upper() == _LEGACY_SWITCH:
                print("[perps meeting] deprecated: AUTO_TRADE_ENABLED applied to "
                      "NEW_POSITIONS_ENABLED + ENTRY_ALERTS_ENABLED")
                _tx += [dict(change, param=k) for k in _SPLIT_SWITCHES]
            else:
                _tx.append(change)
        translated = interp["translated_to"] = _tx
        validated_changes = []
        for change in translated:
            param = change.get("param")
            new_val = change.get("new")
            action = change.get("action", "set")
            if action not in ("set", "save_default", "reset"):
                interp["status"] = "failed"
                interp["error"] = f"invalid action '{action}' (must be set/save_default/reset)"
                return interp
            change["action"] = action

            if param not in TUNABLE_PARAMS:
                interp["status"] = "failed"
                if param in _PERP_CAPITAL_LOCKED:
                    interp["error"] = (
                        f"param '{param}' is HARD-LOCKED (size, leverage, or capital). "
                        f"A meeting cannot change it."
                    )
                    return interp
                interp["error"] = f"param '{param}' is HARD-LOCKED (not in tunable schema). Locked params: credentials, safety floors, file paths, fees, market indexes."
                # Sprint 41: mark for async gap-detection downstream. The async
                # caller (_perp_interpret_meeting_message) sees this flag and
                # classifies whether it's truly HARD-LOCKED or a capability gap
                # that should fire a capability_gap inbox packet to the agent (Cert 007 ruling).
                interp["_sprint41_gap_param"] = param
                return interp

            # reset needs no value — param-in-schema is the only check
            if action == "reset":
                validated_changes.append(change)
                continue

            spec = TUNABLE_PARAMS[param]
            ptype = spec.get("type")

            try:
                if ptype == "bool":
                    new_val = bool(new_val)
                elif ptype == "int":
                    new_val = int(new_val)
                elif ptype in ("float", "usd"):
                    new_val = float(new_val)
            except (ValueError, TypeError):
                interp["status"] = "failed"
                interp["error"] = f"param '{param}' value '{new_val}' could not be coerced to {ptype}"
                return interp

            if ptype in ("int", "float", "usd"):
                pmin = spec.get("min")
                pmax = spec.get("max")
                if pmin is not None and new_val < pmin:
                    interp["status"] = "failed"
                    interp["error"] = f"param '{param}' value {new_val} below min {pmin}"
                    return interp
                if pmax is not None and new_val > pmax:
                    interp["status"] = "failed"
                    interp["error"] = f"param '{param}' value {new_val} above max {pmax}"
                    return interp

            change["new"] = new_val
            validated_changes.append(change)

        interp["translated_to"] = validated_changes

    # Confidence floor: low confidence on "applied" downgrades to clarification_needed
    conf = float(interp.get("confidence", 0.0))
    threshold = float(_perp_get_override("MEETING_LOW_CONFIDENCE_THRESHOLD", 0.50))
    if status == "applied" and conf < threshold:
        interp["status"] = "clarification_needed"
        interp["clarification_question"] = (
            interp.get("clarification_question")
            or f"interpretation confidence too low ({conf:.2f} below threshold {threshold:.2f}). can you rephrase?"
        )

    return interp


async def _perp_interpret_meeting_message(message: str, current_overrides: dict) -> dict:
    """Interpret a directive with the SI brain (LLM_MAIN then LLM_FALLBACK).
    Brain off or unavailable: strict exact-form / JSON parse (_perp_strict_directive).
    """
    if not _perp_brain_enabled():
        return _perp_validate_interpretation(_perp_strict_directive(message, current_overrides))
    _strict = _perp_strict_directive(message, current_overrides)
    if _strict.get("status") == "applied":
        return _perp_validate_interpretation(_strict)   # exact form never needs the brain
    prompt = _perp_build_interpreter_prompt(message, current_overrides)
    
    # Try primary
    raw = await _perp_ollama_generate(prompt, model=PERPS_OLLAMA_MODEL, format_json=True, max_tokens=2000)
    used_model = _PERP_LAST_LLM_SOURCE if raw else PERPS_OLLAMA_MODEL   # true source (cloud:/local:)
    parsed = _perp_parse_llm_json(raw) if raw else {}
    
    # Fallback if: primary empty, parser failed, or parsed dict missing 'status' (e.g. just '{}')
    if not raw or not parsed or not parsed.get("status"):
        reason = "empty response" if not raw else ("parse failed" if not parsed else "missing status field (likely truncated)")
        print(f"[perps meeting] primary ({PERPS_OLLAMA_MODEL}) {reason}, retrying the brain once")
        raw = await _perp_ollama_generate(prompt, model=PERPS_OLLAMA_MODEL, format_json=True, max_tokens=2000)
        used_model = _PERP_LAST_LLM_SOURCE if raw else PERPS_OLLAMA_FALLBACK   # true source
        parsed = _perp_parse_llm_json(raw) if raw else {}
    
    if not raw:
        return _perp_validate_interpretation(_strict)
    if False:
        return {
            "status": "failed",
            "interpretation": "(brain unavailable)",
            "confidence": 0.0,
            "error": "both primary and fallback ollama models returned empty",
            "interpreted_by": used_model,
        }
    if not parsed or not parsed.get("status"):
        return {
            "status": "failed",
            "interpretation": "(could not parse LLM output)",
            "confidence": 0.0,
            "error": f"LLM returned non-JSON, malformed JSON, or missing status field. Both primary and fallback failed. Raw len={len(raw)}.",
            "interpreted_by": used_model,
            "raw_output": raw[:500],
        }
    parsed["interpreted_by"] = f"ollama/{used_model}"
    validated = _perp_validate_interpretation(parsed)

    # Sprint 41: capability gap detection. If validator marked a missing param,
    # ask the LLM to classify (hard_locked vs capability_gap vs ambiguous).
    # capability_gap stays in the agent's inbox as his unfinished patch.
    gap_param = validated.pop("_sprint41_gap_param", None)
    if gap_param:
        try:
            self_desc = _perp_load_self_description()
            classification = await _perp_classify_gap_via_llm(gap_param, message, self_desc)
            validated["_sprint41_gap_classification"] = classification
            cls = classification.get("classification", "ambiguous")
            if cls == "capability_gap":
                req_id = _perp_write_code_request_for_gap(
                    gap_param, message, validated, classification,
                )
                if req_id:
                    validated["status"] = "gap_agent_unfinished_patch"
                    validated["error"] = (
                        f"Param '{gap_param}' does not exist yet but is a real capability gap "
                        f"(not HARD-LOCKED). It is in your inbox as unfinished patch {req_id}. "
                        f"You apply that patch yourself."
                    )
                    validated["code_request_id"] = req_id
            elif cls == "ambiguous":
                # Don't auto-route. Leave as failed; the inbox response packet
                # will surface the ambiguity to the owner for manual decision.
                validated["error"] = (
                    f"Param '{gap_param}' could not be cleanly classified as HARD-LOCKED "
                    f"or capability gap. Reasoning: {classification.get('reasoning', '?')}. "
                    f"Manual review needed."
                )
            # else: hard_locked — keep the original error message
        except Exception as e:
            print(f"[perps meeting] gap classification err: {e}")
            traceback.print_exc()
            # Fall through to original failed status

    return validated


def _perp_write_meeting_response_to_inbox(note: dict, interp: dict) -> str:
    """Write the meeting reply to the agent's inbox per Sprint 43 naming convention.
    Returns inbox file path."""
    ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
    fname = f"perps_bot - meeting_response_{ts}.md"
    inbox_path = os.path.join(INBOX_DIR, fname)

    status = interp.get("status", "failed")
    interp_summary = interp.get("interpretation", "(no interpretation)")
    confidence = interp.get("confidence", 0.0)
    interpreted_by = interp.get("interpreted_by", "unknown")
    note_id = note.get("id", "no-id")
    original_msg = note.get("message", "(empty)")[:500]
    note_from = note.get("from", "unknown")

    body = f"""# perps_bot meeting response

**from:** perps_bot
**to:** agent
**status:** {status}
**confidence:** {confidence:.2f}
**interpreted_by:** {interpreted_by}
**fired_at:** {_perp_now_iso()}
**responding_to_note:** {note_id}
**note_from:** {note_from}

## original directive
> {original_msg}

## interpretation
{interp_summary}

"""

    if status == "applied":
        body += "## changes applied (now live in runtime memory)\n\n"
        expires_at_iso = interp.get("expires_at")
        for change in interp.get("translated_to", []):
            body += f"- **{change.get('param')}**: `{change.get('old')}` → `{change.get('new')}`\n"
            if change.get("reason"):
                body += f"  - reason: {change['reason']}\n"
        if expires_at_iso:
            body += f"\n**Duration:** override expires at `{expires_at_iso}`. The expiry watcher will auto-clear it at that time and write an expiry packet to your inbox. No follow-up needed.\n"
        else:
            body += "\n**Duration:** permanent (no expires_at set). Override stays active until explicitly cleared via a new meeting note, or until perps_bot restarts.\n"
        body += "\nOverrides are runtime-only. Snapshot saved to `bot_logs\\perp_overrides_snapshot.json` for audit. If perps_bot restarts, overrides reset and the agent re-asserts as needed.\n"

    elif status == "clarification_needed":
        body += f"## clarification needed\n\n{interp.get('clarification_question', '(no question provided)')}\n\nWrite a new perp_meeting.json with a clearer directive.\n"

    elif status == "failed":
        body += f"## failed\n\n**error:** {interp.get('error', 'unknown failure')}\n\n"
        if "raw_output" in interp:
            body += f"**raw LLM output (first 500 chars):**\n```\n{interp['raw_output']}\n```\n"

    elif status == "gap_agent_unfinished_patch":
        # Capability gap stays with the agent as his own unfinished patch.
        gap_cls = interp.get("_sprint41_gap_classification", {})
        body += f"## capability gap — your unfinished patch\n\n"
        body += f"**code_request_id:** `{interp.get('code_request_id', '?')}`\n\n"
        body += f"**reasoning:** {gap_cls.get('reasoning', '?')}\n\n"
        body += f"**proposed change:**\n{gap_cls.get('proposed_change', '?')}\n\n"
        if gap_cls.get('suggested_tunable_def'):
            body += f"**suggested tunable shape:**\n```python\n{gap_cls['suggested_tunable_def']}\n```\n\n"
        if gap_cls.get('risk_notes'):
            body += f"**risk notes:** {gap_cls['risk_notes']}\n\n"
        body += (
            "### What happens next\n\n"
            "This stays in your inbox until you apply the patch yourself. "
            "Nothing is picked up by a cron and nothing is routed out for someone "
            "else to apply. After you land it, re-fire the original directive.\n"
        )

    body += "\n---\n\n*This packet was generated by perps_bot in response to a meeting note. The meeting file at `bots\\json_relay\\perp_meeting.json` has been deleted. To send a follow-up, write a fresh meeting file.*\n"

    try:
        os.makedirs(INBOX_DIR, exist_ok=True)
        with open(inbox_path, "w", encoding="utf-8") as f:
            f.write(body)
        return inbox_path
    except Exception as e:
        print(f"[perps meeting] inbox write err: {e}")
        return ""


async def _perp_process_meeting_file() -> None:
    """File appeared. Wake LLM, interpret, apply, reply, delete."""
    # 1. Read the meeting file
    try:
        # utf-8-sig handles both regular UTF-8 and UTF-8-with-BOM (e.g. PowerShell Out-File default).
        with open(PERP_MEETING_FILE, "r", encoding="utf-8-sig") as f:
            note = json.load(f)
    except Exception as e:
        print(f"[perps meeting] read err: {e}")
        # Move corrupted file out of the way so we don't loop on it
        try:
            broken_path = PERP_MEETING_FILE + f".broken.{int(time.time())}"
            os.rename(PERP_MEETING_FILE, broken_path)
            print(f"[perps meeting] corrupted file moved to {broken_path}")
        except Exception:
            pass
        return

    # Validate basic structure — accept either a plain-English 'message' or a 'structured' list
    if not isinstance(note, dict) or (not note.get("message") and not note.get("structured")):
        print(f"[perps meeting] file present but missing 'message' field, treating as failed")
        interp = {
            "status": "failed",
            "interpretation": "(invalid meeting note structure)",
            "confidence": 0.0,
            "error": "meeting file must be a JSON object with at least a 'message' field",
        }
    elif isinstance(note.get("structured"), list) and note["structured"]:
        # OG STRUCTURED PATH — explicit changes, no LLM (kept "just in case").
        # Shape: {"structured":[{"action":"set|save_default|reset","param":"X","value":N}], "expires_at": iso|null}
        print(f"[perps meeting] structured directive detected ({len(note['structured'])} change(s)) — bypassing LLM")
        changes = []
        for sc in note["structured"]:
            changes.append({
                "param":  sc.get("param"),
                "action": sc.get("action", "set"),
                "new":    sc.get("value", sc.get("new")),
                "reason": sc.get("reason", "structured directive"),
            })
        interp = {
            "status": "applied", "confidence": 1.0,
            "interpretation": "(structured directive — applied without LLM)",
            "translated_to": changes, "expires_at": note.get("expires_at"),
            "interpreted_by": "structured",
        }
        interp = _perp_validate_interpretation(interp)
    else:
        # 2. Wake LLM and interpret (plain-English path)
        print(f"[perps meeting] file detected, waking LLM to interpret note id={note.get('id', 'no-id')}")
        message = note["message"].strip()
        current_overrides = dict(_PERP_ACTIVE_OVERRIDES)
        try:
            interp = await _perp_interpret_meeting_message(message, current_overrides)
        except Exception as e:
            print(f"[perps meeting] interpretation exception: {e}")
            interp = {
                "status": "failed",
                "interpretation": "(interpreter exception)",
                "confidence": 0.0,
                "error": f"interpreter exception: {e}",
                "interpreted_by": "exception",
            }

    # 3. If applied, mutate runtime overrides + snapshot
    if interp.get("status") == "applied":
        # Sprint 32+: validate expires_at sanity-check before applying
        expires_at_iso = interp.get("expires_at")
        is_valid, parsed_dt, expiry_err = _perp_validate_expires_at(expires_at_iso)
        if not is_valid:
            # Reject — direct orders honor their time bounds. Bad time bound = bad order.
            print(f"[perps meeting] REJECTED applied directive: {expiry_err}")
            interp["status"] = "failed"
            interp["error"] = f"expires_at validation failed: {expiry_err}"
            interp["interpretation"] = (interp.get("interpretation", "") +
                                         f" [REJECTED: {expiry_err}]").strip()
        else:
            now_iso = _perp_now_iso()
            for change in interp.get("translated_to", []):
                param  = change.get("param")
                action = change.get("action", "set")
                new_val = change.get("new")
                reason = change.get("reason", "")
                if param not in TUNABLE_PARAMS:
                    continue
                if action == "reset":
                    ok, cd, err = _perp_reset_default(param, reason or "meeting: reset")
                    print(f"[perps meeting] RESET {param} -> {cd}" if ok else f"[perps meeting] reset failed {param}: {err}")
                elif action == "save_default":
                    ok, cv, err = _perp_save_default(param, new_val, reason or "meeting: save_default")
                    print(f"[perps meeting] SAVE_DEFAULT {param}={cv}" if ok else f"[perps meeting] save_default failed {param}: {err}")
                elif new_val is not None:  # set (temporary/session)
                    old_eff = _perp_get_override(param, _perp_code_default(param))
                    _PERP_ACTIVE_OVERRIDES[param] = {
                        "value": new_val,
                        "expires_at": expires_at_iso,  # iso string or None (session)
                        "applied_at": now_iso,
                        "reason": reason,
                    }
                    _perp_tune_history_append({"action": "set", "param": param, "old": old_eff,
                                               "new": new_val, "reason": reason, "expires_at": expires_at_iso})
                    expiry_note = f" (expires {expires_at_iso})" if expires_at_iso else " (session — until restart/change)"
                    print(f"[perps meeting] APPLIED {param}={new_val}{expiry_note}")
            _perp_save_overrides_snapshot()
            _perp_write_params_mirror()  # keep perps_params.json fresh after a change

    # 4. Audit log (always, regardless of status)
    _perp_meeting_audit({
        "event": "note_processed",
        "note_id": note.get("id") if isinstance(note, dict) else None,
        "from": note.get("from") if isinstance(note, dict) else None,
        "message": (note.get("message", "")[:200] if isinstance(note, dict) else ""),
        "status": interp.get("status"),
        "confidence": interp.get("confidence"),
        "translated_to": interp.get("translated_to"),
        "interpreted_by": interp.get("interpreted_by"),
        "active_overrides_after": dict(_PERP_ACTIVE_OVERRIDES),
    })

    # 5. Write response to the agent's inbox
    inbox_path = _perp_write_meeting_response_to_inbox(note if isinstance(note, dict) else {}, interp)
    if inbox_path:
        print(f"[perps meeting] response written to {inbox_path}")

    # 6. DELETE the meeting file (folder returns to empty)
    try:
        os.remove(PERP_MEETING_FILE)
        print(f"[perps meeting] deleted {PERP_MEETING_FILE}, cycle complete")
    except Exception as e:
        print(f"[perps meeting] delete err: {e}")


async def perp_meeting_watcher_loop():
    """
    Sprint 32+: file-presence watcher (cheap, no LLM until file appears).

    Doctrine:
      - perp_meeting.json does NOT exist by default. Bot does NOT auto-create it.
      - File appears only when the agent writes one.
      - When file is detected: wake Ollama, interpret, apply override to runtime
        memory, write response packet to the agent's inbox, DELETE the meeting file.
      - LLM stays asleep when no file is present (VRAM is free).
    """
    print(f"[perps meeting] watcher starting (file-presence poll, {PERPS_MEETING_POLL_S}s cadence, LLM dormant)")
    while True:
        try:
            if os.path.exists(PERP_MEETING_FILE):
                await _perp_process_meeting_file()
        except Exception as e:
            print(f"[perps meeting] watcher cycle err: {e}")
        await asyncio.sleep(_perp_get_override("PERPS_MEETING_POLL_S", PERPS_MEETING_POLL_S))


# ── TRADE AUTOPSY: "what went wrong", one review per closed trade (2026-10-17) ──
# record_episode() queues every close in bot_logs/perp_autopsy_queue.jsonl (never
# raises). A main-loop tick starts a background worker (never blocks the loop, never
# touches the close path) that, for each queued close (losses only by default):
#   builds a compact bundle: the position (entry/exit, size, TP/SL/trail, the events
#   list of TP/SL/trail/liq/floor/flip alerts), the entry situation + the agent's stated
#   reason, the chain packet in effect at entry (entry_packet, stamped at open), param
#   changes during the hold, macro_prints, news lean at exit, fees;
#   asks the SI brain (existing chain) for {verdict, root_cause, evidence, lesson,
#   proposal}; no brain / over the daily cap / bad JSON -> rule-based verdict;
#   writes bot_logs/perp_trade_autopsies.jsonl (one line per position: idempotent),
#   an inbox packet "perps_bot - trade_autopsy_<CHAIN>_<ts>.json" and one learnings entry.
# It never changes params: anything that implies a param change is a proposal for the owner.
# Settings (bots/.env): AUTOPSY_ON=loss|all|off (loss), AUTOPSY_SI_ENABLED=true,
# AUTOPSY_SI_MAX_PER_DAY=6.
_TA_DIR = WS_BOT_LOGS
_TA_QUEUE = os.path.join(_TA_DIR, "perp_autopsy_queue.jsonl")
_TA_OUT = os.path.join(_TA_DIR, "perp_trade_autopsies.jsonl")
_TA_STATE = os.path.join(_TA_DIR, "perp_trade_autopsy_state.json")
_TA_POLL_S = 60
_TA_MAX_PER_PASS = 3
_TA_LAST = 0.0
_TA_TASK = None
_PERP_LAST_PACKET = {}   # chain -> newest chain packet filename (stamped on opens)
_TA_CATS = {
    "thesis_wrong": "The read was wrong: name the signal that disagreed at entry and require it to agree next time.",
    "entered_early": "Entry came before confirmation: wait for the trigger you named, or use a limit entry.",
    "entered_late": "Entry chased a move already made: skip setups after the bulk of the move, or wait for a pullback.",
    "oversized": "Size was too big for the setup: a normal move took most of the collateral. Size down on this shape.",
    "stop_too_tight": "The stop sat inside normal noise: give the setup room or size down so a wider stop fits.",
    "stop_too_wide": "The stop let a small loss become a big one: set the stop where the thesis is wrong, not further.",
    "ignored_flip": "Conditions turned against the position (flip alert) and it stayed open: act on flips (tighten, trim or close).",
    "news_shock": "News turned against the position during the hold: check the news lean before holding through it.",
    "macro_shock": "A macro print landed during the hold: flatten or size down into scheduled releases.",
    "gave_back_profit": "It was green past the trail level and closed red: take partials or tighten once the trail arms.",
    "fees": "Fees ate the move: hold for moves that clear round-trip fees, or skip small targets.",
    "noise_within_plan": "A normal loss inside the plan: no change needed if the setup still fits the rules.",
    "worked_as_planned": "The plan worked: note what lined up so the same shape gets the same treatment.",
}
_TA_PARAM_RX = re.compile(r"\b(set|raise|lower|increase|decrease|change|widen|tighten)\b[^.]{0,60}\b(param|parameter|PERP_[A-Z_]+|TRAIL_[A-Z_]+|trail|leverage cap|max leverage|default)", re.I)


def _ta_env(name, default):
    v = os.environ.get(name)
    return default if v is None or not str(v).strip() else str(v).strip()


def _ta_mode():
    v = _ta_env("AUTOPSY_ON", "loss").lower()
    return v if v in ("loss", "all", "off") else "loss"


def _ta_si_enabled():
    return _ta_env("AUTOPSY_SI_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def _ta_max_per_day():
    try:
        return max(0, int(float(_ta_env("AUTOPSY_SI_MAX_PER_DAY", "6"))))
    except Exception:
        return 6


def _ta_dirs():
    done = globals().get("INBOX_RESULTS_DONE")
    out = [globals().get("INBOX_PATH"), done, os.path.join(os.path.dirname(done), "failed") if done else None]
    return [d for d in out if d]


def _ta_find_packet(chain, before_iso=None):
    """Newest chain packet at or before a time (filenames carry the timestamp)."""
    import glob as _glob
    chain = str(chain or "").upper().replace("-PERP", "")
    try:
        cut = datetime.fromisoformat(str(before_iso)).astimezone(EST).strftime("%Y%m%d_%H%M%S") if before_iso else "99999999_999999"
    except Exception:
        cut = "99999999_999999"
    best = None
    for d in _ta_dirs():
        for p in _glob.glob(os.path.join(d, f"perps_bot - perp_{chain}_*.json")):
            m = re.search(r"_(\d{8}_\d{6})\.json$", p)
            if m and m.group(1) <= cut and (best is None or m.group(1) > best[0]):
                best = (m.group(1), os.path.basename(p))
    return best[1] if best else None


def _perp_entry_packet_ref(market):
    """Filename of the chain packet the agent was looking at when this open fired."""
    try:
        chain = _pos_base(market).replace("-PERP", "").upper()
        return _PERP_LAST_PACKET.get(chain) or _ta_find_packet(chain)
    except Exception:
        return None


def _ta_load_packet(name):
    if not name:
        return None
    for d in _ta_dirs():
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
    return None


def _perp_pos_event(market_key, pos, kind, price=None, pnl_pct=None, detail=None):
    """Append a TP/SL/trail/liq/floor/flip event to the position (kept for the autopsy).
    Mutates pos so a later save of the same dict keeps it. Never raises."""
    try:
        if not isinstance(pos, dict):
            return
        ev = {"t": datetime.now(EST).isoformat(timespec="seconds"), "kind": kind}
        if price:
            ev["price"] = round(float(price), 6)
        if pnl_pct is not None:
            ev["pnl_pct"] = round(float(pnl_pct), 2)
        if detail:
            ev["detail"] = str(detail)[:160]
        evs = pos.setdefault("events", [])
        if evs and evs[-1].get("kind") == kind and evs[-1].get("detail") == ev.get("detail") and kind == "flip":
            return
        evs.append(ev)
        del evs[:-40]
        save_position(pos, market=market_key)
    except Exception as e:
        print(f"[autopsy] event save err: {e!r}")


def _ta_key(pos, market):
    return f"{market}|{(pos or {}).get('timestamp') or ''}|{(pos or {}).get('tx_sig') or ''}"


def perp_autopsy_enqueue(pos, episode):
    """Queue a closed trade for review. Called from record_episode; never raises."""
    try:
        if _ta_mode() == "off":
            return
        market = (episode or {}).get("market") or (pos or {}).get("market") or "SOL-PERP"
        job = {"key": _ta_key(pos, market), "queued_at": datetime.now(EST).isoformat(),
               "episode": episode, "position": pos}
        os.makedirs(_TA_DIR, exist_ok=True)
        with open(_TA_QUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(job, default=str) + "\n")
    except Exception as e:
        print(f"[autopsy] enqueue err: {e!r}")


def _ta_read_jsonl(path):
    out = []
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
    except Exception:
        pass
    return out


def _ta_done_keys():
    return {r.get("key") for r in _ta_read_jsonl(_TA_OUT) if r.get("key")}


def _ta_is_loss(ep):
    ep = ep or {}
    try:
        return bool(ep.get("liquidated")) or float(ep.get("pnl_pct") or 0) < 0 or float(ep.get("pnl_usd") or 0) < 0
    except Exception:
        return False


def _ta_state():
    try:
        with open(_TA_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    today = datetime.now(EST).strftime("%Y-%m-%d")
    if st.get("date") != today:
        st = {"date": today, "si_calls": 0}
    return st


def _ta_si_allowed():
    """(allowed, why_not). Cost guard: AUTOPSY_SI_ENABLED, brain configured, daily cap."""
    if not _ta_si_enabled():
        return False, "AUTOPSY_SI_ENABLED is off"
    _be = globals().get("_perp_brain_enabled")
    if callable(_be) and not _be():
        return False, "SI brain off (LLM_MAIN and LLM_FALLBACK)"
    if _ta_state().get("si_calls", 0) >= _ta_max_per_day():
        return False, f"daily SI cap reached ({_ta_max_per_day()})"
    return True, ""


def _ta_count_si_call():
    st = _ta_state()
    st["si_calls"] = int(st.get("si_calls", 0)) + 1
    try:
        tmp = _TA_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, _TA_STATE)
    except Exception as e:
        print(f"[autopsy] state save err: {e!r}")


def _ta_dt(v):
    try:
        t = datetime.fromisoformat(str(v))
        return t if t.tzinfo else t.replace(tzinfo=EST)
    except Exception:
        return None


def _ta_compact(v, n):
    try:
        t = json.dumps(v, default=str, separators=(",", ":"))
    except Exception:
        t = str(v)
    return t if len(t) <= n else t[:n] + "...(cut)"


def _ta_bundle(job):
    ep = job.get("episode") or {}
    pos = job.get("position") or {}
    market = ep.get("market") or pos.get("market") or "SOL-PERP"
    chain = _pos_base(market).replace("-PERP", "").upper()
    t_open, t_close = _ta_dt(pos.get("timestamp")), _ta_dt(ep.get("ts"))
    trade = {
        "market": market, "side": ep.get("direction") or pos.get("direction"),
        "leverage": ep.get("leverage") or pos.get("leverage"),
        "collateral_usd": pos.get("collateral") or pos.get("collateral_usd"), "size_usd": ep.get("size_usd"),
        "entry": ep.get("entry_price"), "exit": ep.get("exit_price"), "pnl_pct": ep.get("pnl_pct"),
        "pnl_usd": ep.get("pnl_usd"), "peak_pct": ep.get("peak_pct"), "hold_min": ep.get("hold_minutes"),
        "category": ep.get("category"), "exit_reason": str(ep.get("reason") or "")[:200],
        "tp": pos.get("tp_price"), "sl": pos.get("sl_price"), "exit_floor": pos.get("liq_exit_floor"),
        "liq": pos.get("liq_price"), "trail_activate_pct": pos.get("trail_activate_pct") or TRAIL_ACTIVATE_PCT,
        "params_rev": pos.get("params_rev"), "opened_at": pos.get("timestamp"), "closed_at": ep.get("ts"),
    }
    fees = None
    try:
        with open(PNL_FILE, encoding="utf-8") as f:
            trades = (json.load(f) or {}).get("trades") or []
        best = None
        for tr in trades[-50:]:
            if str(tr.get("market") or "").upper() != str(market).upper():
                continue
            tt = _ta_dt(tr.get("timestamp"))
            if tt and t_close and abs((tt - t_close).total_seconds()) <= 900:
                d = abs((tt - t_close).total_seconds())
                if best is None or d < best[0]:
                    best = (d, tr)
        if best:
            fees = {"fees_usd": best[1].get("fees_usd"), "gross_pnl_usd": best[1].get("gross_pnl_usd")}
    except Exception:
        fees = None
    changes = []
    for r in _ta_read_jsonl(globals().get("PERP_PARAM_CHANGE_LOG") or ""):
        rt = _ta_dt(r.get("ts"))
        if (str(r.get("market") or "").upper().split("#")[0] == str(market).upper().split("#")[0]
                and rt and t_open and rt >= t_open and (t_close is None or rt <= t_close)):
            changes.append({k: r.get(k) for k in ("ts", "param", "old", "new", "reason", "source")})
    pkt_name = pos.get("entry_packet") or _ta_find_packet(chain, pos.get("timestamp"))
    pkt = _ta_load_packet(pkt_name)
    pkt_c = None
    if isinstance(pkt, dict):
        pkt_c = {"file": pkt_name, "built_at": pkt.get("built_at"), "mode": pkt.get("mode")}
        for k, n in (("market_now", 500), ("cycle_read", 700), ("news", 900), ("macro_prints", 500),
                     ("positioning", 400), ("polymarket", 300), ("fair_analysis", 400)):
            if pkt.get(k) is not None:
                pkt_c[k] = _ta_compact(pkt.get(k), n)
    macro = None
    try:
        with open(os.path.join(_TA_DIR, "macro_prints.json"), encoding="utf-8") as f:
            mp = json.load(f) or {}
        cpi = mp.get("cpi") or {}
        macro = {"period": mp.get("period"), "print_first_seen_at": mp.get("print_first_seen_at"),
                 "headline_yoy_pct": cpi.get("headline_yoy_pct"), "core_yoy_pct": cpi.get("core_yoy_pct"),
                 "read": mp.get("read")}
    except Exception:
        macro = None
    lean_exit = None
    try:
        lean_exit = _news_lean_from_bucketed(_perp_chain_news(chain))
    except Exception:
        lean_exit = None
    return {"trade": trade, "entry": {"fingerprint": ep.get("fingerprint") or pos.get("entry_fingerprint") or {},
                                      "decision": ep.get("entry_decision_context") or pos.get("entry_decision_context") or {}},
            "events": (pos.get("events") or [])[-15:], "param_changes": changes[-10:],
            "entry_packet": pkt_c, "entry_packet_file": pkt_name, "macro_now": macro,
            "news_lean_exit": lean_exit, "fees": fees}


def _ta_rules(b):
    """Offline verdict: (root_cause, evidence list). Ordered checks, first match wins."""
    t = b.get("trade") or {}
    fp = (b.get("entry") or {}).get("fingerprint") or {}
    kinds = [e.get("kind") for e in (b.get("events") or [])]
    f = lambda v: float(v) if v not in (None, "") else None
    pnl, peak, lev, hold = f(t.get("pnl_pct")) or 0.0, f(t.get("peak_pct")), f(t.get("leverage")) or 0.0, f(t.get("hold_min"))
    side = str(t.get("side") or "").lower()
    reason = str(t.get("exit_reason") or "").lower()
    cat = str(t.get("category") or "").lower()
    if pnl >= 0 and cat != "liquidated":
        return "worked_as_planned", [f"closed {pnl:+.1f}% on collateral"]
    if cat == "liquidated" or "liquidat" in reason:
        return "oversized", [f"liquidated at {lev:g}x"]
    act = f(t.get("trail_activate_pct")) or 20.0
    if peak is not None and peak >= act:
        return "gave_back_profit", [f"peak {peak:+.1f}% passed the trail level {act:g}%", f"closed {pnl:+.1f}%"]
    if hold is not None and hold <= 15 and ("sl_hit" in kinds or "sl_near" in kinds or " sl" in f" {reason}" or "stop" in reason):
        return "stop_too_tight", [f"stopped out after {hold:g} min", f"closed {pnl:+.1f}%"]
    if "flip" in kinds:
        fl = [e for e in b.get("events") or [] if e.get("kind") == "flip"]
        return "ignored_flip", [f"{len(fl)} flip alert(s) against the position before exit",
                                (fl[-1].get("detail") or "")[:120]]
    m = b.get("macro_now") or {}
    ts, to, tc = _ta_dt(m.get("print_first_seen_at")), _ta_dt(t.get("opened_at")), _ta_dt(t.get("closed_at"))
    if ts and to and tc and to <= ts <= tc:
        return "macro_shock", [f"CPI print {m.get('period')} landed during the hold ({str(m.get('print_first_seen_at'))[:16]})"]
    lean = str(fp.get("news_lean") or "")
    if (side == "long" and lean == "bearish") or (side == "short" and lean == "bullish"):
        return "thesis_wrong", [f"entered {side} into {lean} news"]
    try:
        cap = float(_perp_get_override("PERP_MAX_LEVERAGE", PERP_MAX_LEVERAGE))
    except Exception:
        cap = 20.0
    if lev and lev >= 0.6 * cap:
        return "oversized", [f"{lev:g}x is {lev / cap:.0%} of the {cap:g}x cap", f"closed {pnl:+.1f}%"]
    fz = b.get("fees") or {}
    try:
        if fz.get("gross_pnl_usd") is not None and float(fz["gross_pnl_usd"]) >= 0 and float(fz.get("fees_usd") or 0) > 0:
            return "fees", [f"gross ${float(fz['gross_pnl_usd']):+.2f}, fees ${float(fz['fees_usd']):.2f}"]
    except Exception:
        pass
    le = str(b.get("news_lean_exit") or "")
    if (side == "long" and le == "bearish" and lean != "bearish") or (side == "short" and le == "bullish" and lean != "bullish"):
        return "news_shock", [f"news lean went {lean or '?'} -> {le} during the hold"]
    return "noise_within_plan", [f"closed {pnl:+.1f}% with no rule-level cause"]


def _ta_guard_lesson(text):
    """Lessons never change params: anything that reads like a param change is a proposal."""
    t = str(text or "").strip()[:240]
    if t and _TA_PARAM_RX.search(t) and not t.lower().startswith("proposal"):
        t = "Proposal for the owner (not applied): " + t
    return t


def _ta_parse(raw):
    """Strict-ish JSON parse of the SI answer. None on anything unusable."""
    try:
        txt = str(raw or "").strip()
        if not txt:
            return None
        if "```" in txt:
            txt = re.sub(r"```(?:json)?", "", txt)
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0 or j <= i:
            return None
        d = json.loads(txt[i:j + 1])
        rc = str(d.get("root_cause") or "").strip().lower()
        verdict = " ".join(str(d.get("verdict") or "").split()[:25])
        lesson = str(d.get("lesson") or "").strip()
        if rc not in _TA_CATS or not verdict or not lesson:
            return None
        ev = d.get("evidence") or []
        ev = [str(x)[:160] for x in (ev if isinstance(ev, list) else [ev])][:3]
        try:
            conf = max(0.0, min(1.0, float(d.get("confidence"))))
        except Exception:
            conf = None
        prop = str(d.get("proposal") or "").strip()[:240] or None
        return {"root_cause": rc, "verdict": verdict, "lesson": lesson[:240], "evidence": ev,
                "confidence": conf, "proposal": prop}
    except Exception:
        return None


def _ta_prompt(b, rule_rc):
    return ("You review ONE closed crypto perp trade and say what went wrong (or right). "
            "Use only the data below. Be specific and short.\n"
            f"root_cause must be one of: {', '.join(_TA_CATS)}.\n"
            f"A rule-based check suggests: {rule_rc} (override it if the data says otherwise).\n"
            "The bot never changes its own settings: if your lesson implies changing a parameter "
            "(leverage cap, trail, TP/SL defaults), put that in `proposal` for the owner, not in `lesson`.\n"
            "Reply ONLY as JSON: {\"verdict\": \"<=25 words\", \"root_cause\": \"...\", "
            "\"evidence\": [\"<=3 short facts citing the data\"], \"lesson\": \"one sentence, next time ...\", "
            "\"proposal\": \"optional param change for the owner, or empty\", \"confidence\": 0.0-1.0}\n\n"
            f"DATA:\n{_ta_compact(b, 7000)}")


async def _ta_review(job):
    b = _ta_bundle(job)
    rule_rc, rule_ev = _ta_rules(b)
    res, source, si_note = None, "rules", ""
    ok, why = _ta_si_allowed()
    if ok:
        _ta_count_si_call()
        try:
            raw = await asyncio.wait_for(_perp_ollama_generate(_ta_prompt(b, rule_rc), model=PERPS_OLLAMA_MODEL,
                                                               format_json=True, max_tokens=400, timeout_s=60.0), 150)
            res = _ta_parse(raw)
            if res:
                source = str(globals().get("_PERP_LAST_LLM_SOURCE") or "si")
            else:
                si_note = "SI answer empty or not valid JSON; rules used"
        except Exception as e:
            si_note = f"SI error ({type(e).__name__}); rules used"
    else:
        si_note = why
    if not res:
        res = {"root_cause": rule_rc, "verdict": None, "lesson": _TA_CATS[rule_rc], "evidence": rule_ev,
               "confidence": None, "proposal": None}
    t = b["trade"]
    if not res.get("verdict"):
        res["verdict"] = (f"{t.get('side')} {t.get('market')} {float(t.get('pnl_pct') or 0):+.1f}%: "
                          f"{rule_rc.replace('_', ' ')} ({'; '.join(rule_ev)[:120]})")
    prop = res.get("proposal")
    rec = {
        "key": job.get("key"), "market": t.get("market"), "side": t.get("side"), "leverage": t.get("leverage"),
        "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"), "pnl_pct": t.get("pnl_pct"),
        "pnl_usd": t.get("pnl_usd"), "exit_category": t.get("category"),
        "verdict": res["verdict"], "root_cause": res["root_cause"], "evidence": res.get("evidence") or [],
        "lesson": _ta_guard_lesson(res["lesson"]),
        "proposal": (("Proposal for the owner (not applied): " + prop) if prop and not prop.lower().startswith("proposal") else prop),
        "confidence": res.get("confidence"), "source": source, "rules_root_cause": rule_rc,
        "si_note": si_note or None, "entry_packet": b.get("entry_packet_file"),
        "events_seen": len(b.get("events") or []), "param_changes_seen": len(b.get("param_changes") or []),
        "reviewed_at": datetime.now(EST).isoformat(timespec="seconds"),
    }
    os.makedirs(_TA_DIR, exist_ok=True)
    with open(_TA_OUT, "a", encoding="utf-8") as f:          # written first: marks the position reviewed
        f.write(json.dumps(rec, default=str) + "\n")
    try:
        chain = _pos_base(rec["market"] or "SOL-PERP").replace("-PERP", "").upper()
        pkt = {"_type": "perp_trade_autopsy", **rec,
               "_doctrine": ("Review of ONE closed trade. Read it, acknowledge it in your notes, and judge the "
                             "lesson against the rules before using it. The bot changed nothing: a proposal is "
                             "for the owner to approve, never something to apply silently.")}
        _s27_save_json(os.path.join(INBOX_PATH, f"perps_bot - trade_autopsy_{chain}_"
                                                f"{datetime.now(EST).strftime('%Y%m%d_%H%M%S')}.json"), pkt)
    except Exception as e:
        print(f"[autopsy] packet err: {e!r}")
    try:
        txt = (f"{rec['market']} {str(rec['side']).upper()} {rec['leverage']}x {float(rec['pnl_pct'] or 0):+.1f}% | "
               f"{rec['root_cause']} | {rec['verdict']}\nLesson: {rec['lesson']}"
               + (f"\n{rec['proposal']}" if rec.get("proposal") else "") + f"\n(source: {source})")
        _ta_learnings_append(txt)
    except Exception as e:
        print(f"[autopsy] learnings err: {e!r}")
    print(f"[autopsy] {rec['market']} {rec['side']} {rec['root_cause']} via {source}")
    return rec


def _ta_learnings_append(text):
    """One entry via learnings_writer (Section 8 append window). False on any failure."""
    try:
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        for d in (here, os.path.join(here, "sub-bots")):
            if os.path.exists(os.path.join(d, "learnings_writer.py")) and d not in sys.path:
                sys.path.insert(0, d)
        import learnings_writer as _lw
        return bool(_lw.append_to_learnings(text, "trade_lesson", "perps_autopsy"))
    except Exception as e:
        print(f"[autopsy] learnings_writer unavailable: {e!r}")
        return False


async def _ta_worker_once():
    if _ta_mode() == "off" or not os.path.exists(_TA_QUEUE):
        return
    jobs = _ta_read_jsonl(_TA_QUEUE)
    done = _ta_done_keys()
    mode = _ta_mode()
    drop, retry, n = set(), {}, 0
    for job in jobs:
        k = job.get("key")
        if not k or k in done or k in drop:
            drop.add(k); continue
        if mode == "loss" and not _ta_is_loss(job.get("episode")):
            drop.add(k); continue
        if n >= _TA_MAX_PER_PASS:
            continue
        n += 1
        try:
            await _ta_review(job)
            drop.add(k); done.add(k)
        except Exception as e:
            print(f"[autopsy] review err {k}: {e!r}")
            a = int(job.get("attempts", 0)) + 1
            if a >= 3:
                drop.add(k)
            else:
                retry[k] = a
    # rewrite (sync, no await between read and replace: a close cannot interleave)
    keep = []
    for job in _ta_read_jsonl(_TA_QUEUE):
        k = job.get("key")
        if k in drop:
            continue
        if k in retry:
            job["attempts"] = retry[k]
        keep.append(job)
    tmp = _TA_QUEUE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for job in keep:
            f.write(json.dumps(job, default=str) + "\n")
    os.replace(tmp, _TA_QUEUE)
    if not keep:
        try:
            os.remove(_TA_QUEUE)
        except Exception:
            pass


async def _ta_worker_guarded():
    try:
        await _ta_worker_once()
    except Exception as e:
        print(f"[autopsy] worker err (isolated): {e!r}")


async def perp_trade_autopsy_tick():
    """Main-loop hook: starts the worker in the background; never blocks or raises."""
    global _TA_LAST, _TA_TASK
    try:
        if time.time() - _TA_LAST < _TA_POLL_S:
            return
        _TA_LAST = time.time()
        if _TA_TASK is not None and not _TA_TASK.done():
            return
        if _ta_mode() == "off" or not os.path.exists(_TA_QUEUE):
            return
        _TA_TASK = asyncio.create_task(_ta_worker_guarded())
    except Exception as e:
        print(f"[autopsy] tick err: {e!r}")


async def perp_counterfactual_loop():
    """Sprint 47: judge passed setups (miss vs correct-pass) on a cadence."""
    print("[autopsy] counterfactual resolver started")
    while True:
        await asyncio.sleep(_perp_get_override("AUTOPSY_RESOLVE_SECONDS", 900))
        try:
            await resolve_autopsy_snapshots()
        except Exception as e:
            print(f"[autopsy] resolve loop err: {e}")


async def perp_autopsy_eod_loop():
    """Sprint 47: once/day, write the full autopsy digest to the agent's inbox."""
    print("[autopsy] EOD digest loop started")
    _last = None
    while True:
        await asyncio.sleep(300)
        try:
            now = datetime.now(EST)
            if now.hour == 22 and _last != now.strftime("%Y%m%d"):
                # Sprint 62: run the cloud discovery lens FIRST so any patterns it
                # finds are in the lesson store before the digest reads it. Cloud
                # (the optional SI brain) reasons over the full episode history for cross-lens
                # conjunctions the 5 statistical lenses can't count. Fail-silent —
                # if it finds nothing or errors, the digest is exactly as before.
                try:
                    _disc = await _autopsy_cloud_discovery_lens()
                    if _disc:
                        _store = _autopsy_load_lessons()
                        for _c in _disc:
                            _autopsy_upsert(_store, _c["chain"], _c["lens"], _c["key"],
                                            _c["message"], _c["occurrences"], _c.get("hit_rate"))
                        _autopsy_save_lessons(_store)
                        print(f"[autopsy] cloud discovery added/updated {len(_disc)} pattern(s)")
                except Exception as _de:
                    print(f"[autopsy] discovery err: {_de}")
                dig = build_autopsy_eod_digest()
                _s27_save_json(os.path.join(INBOX_PATH, f"perps_bot - autopsy_eod_{now.strftime('%Y%m%d')}.json"), dig)
                _last = now.strftime("%Y%m%d")
                print(f"[autopsy] EOD digest written: {dig['active_lessons']} lessons")
        except Exception as e:
            print(f"[autopsy] eod loop err: {e}")


async def main_async():
    print("=" * 70)
    print("perps_bot v5.7 (Jupiter Perps: SOL / BTC / ETH, agent-signal execution, per-chain packets)")
    print("=" * 70)
    print("RPC: " + re.sub(r"(?i)(api[-_]?key=)[^&\s]+", r"\1***", str(RPC_URL)))
    print(f"[perps] trail_activate={TRAIL_ACTIVATE_PCT}% trail_drop={TRAIL_DROP_PCT}% "
          f"| polls {PRICE_POLL_SECONDS}/{FAST_POLL_SECONDS}/{SIGNAL_POLL}s "
          f"| auto_size=${AUTO_SIZE_USD} lev_ladder={','.join(f'{c:g}SOL:{l:g}x' for c, l in _perp_lev_ladder())} (cap {_perp_get_override('PERP_MAX_LEVERAGE', PERP_MAX_LEVERAGE):g}x)")
    _lu_on_boot()  # live-update: report a completed reload (if any) + snapshot last-good

    # ─── Sprint 41: self-awareness boot hook ─────────────────────────
    # Generate (or regenerate) _self_description.json based on current source.
    # Detect if source was patched since last boot — if so, fire ack packet
    # to the agent's inbox and notify the owner via Telegram.
    if _SELF_DESC_AVAILABLE:
        try:
            patch_status = detect_source_patched(__file__)
            desc = generate_self_description(__file__)
            if patch_status.get("patched"):
                print(f"[boot] source hash mismatch detected — bot was patched.")
                print("[boot] perps_bot v5.7 | exit_floor NOTIFICATION active "
                      f"(floor {LIQ_EXIT_FLOOR_PCT}% above venue liq, re-arm {LIQ_FLOOR_REARM_PCT}%) "
                      "| ZERO auto-closes | venue liq read: on-chain position account")
                print(f"  old hash: {(patch_status.get('cached_hash') or 'none')[:30]}...")
                print(f"  new hash: {patch_status.get('current_hash', '')[:30]}...")
                # Write a patch_landed_ack packet to the agent's inbox (Sprint 43 naming)
                try:
                    # 2026-10-16: one boot item. The full ack goes to bot_logs;
                    # the single "started" alert points at it.
                    _ack_dir = os.path.join(WS_BOT_LOGS, "patch_acks")
                    os.makedirs(_ack_dir, exist_ok=True)
                    ts = datetime.now(EST).strftime("%Y%m%d_%H%M%S")
                    ack_fname = f"perps_bot - patch_landed_ack_{ts}.md"
                    ack_path = os.path.join(_ack_dir, ack_fname)
                    globals()["_BOOT_ACK_PATH"] = ack_path
                    body = (
                        f"# perps_bot patch landed — ack\n\n"
                        f"**from:** perps_bot\n"
                        f"**to:** agent\n"
                        f"**fired_at:** {datetime.now(EST).isoformat()}\n"
                        f"**event:** bot booted with new source\n\n"
                        f"## Detection\n"
                        f"- old hash: `{patch_status.get('cached_hash')}`\n"
                        f"- new hash: `{patch_status.get('current_hash')}`\n"
                        f"- cached version: `{patch_status.get('cached_version', '?')}`\n"
                        f"- new version: `{desc.get('version', '?')}`\n\n"
                        f"## New capability surface\n"
                        f"- tunable params: **{len(desc.get('tunable_params', {}))}**\n"
                        f"- signal actions: **{len(desc.get('actions_supported', {}).get('signal_actions', []))}**\n"
                        f"- query actions: **{len(desc.get('actions_supported', {}).get('query_actions', []))}**\n"
                        f"- loops running: **{len(desc.get('loops_running', []))}**\n\n"
                        f"## Updated self-description\n"
                        f"`{desc.get('written_to', '?')}`\n\n"
                        f"Full surface available at the path above. If a capability gap is still "
                        f"in your inbox, that patch is yours to apply. Then re-fire the directive.\n"
                    )
                    with open(ack_path, "w", encoding="utf-8") as f:
                        f.write(body)
                    print(f"[boot] patch_landed_ack written to {ack_fname}")
                except Exception as e:
                    print(f"[boot] patch_landed_ack write err: {e}")
                # Folded into the single "started" alert below.
                globals()["_BOOT_NEW_SOURCE"] = (
                    f"New source: {patch_status.get('cached_version', '?')} -> {desc.get('version', '?')} | "
                    f"tunables {len(desc.get('tunable_params', {}))}, "
                    f"actions {len(desc.get('actions_supported', {}).get('signal_actions', []))}, "
                    f"queries {len(desc.get('actions_supported', {}).get('query_actions', []))}")
            elif patch_status.get("first_boot"):
                print(f"[boot] first-boot self-description generated ({desc.get('version', '?')})")
            else:
                print(f"[boot] self-description refreshed (no source change, hash unchanged)")
        except Exception as e:
            print(f"[boot] self-description hook err (non-fatal): {e}")
    else:
        print(f"[boot] self-description module not available — skipping self-awareness hook")
    # ────────────────────────────────────────────────────────────────

    connection = AsyncClient(RPC_URL)
    balance    = await get_sol_balance(connection)
    sol_price  = await get_sol_price()
    try:
        _boot_usd = round(balance * sol_price, 2) if (balance and sol_price) else None
        _maybe_stamp_wallet_balance_fallback(balance, _boot_usd)
    except Exception:
        pass
    sol_str    = f"${sol_price:.2f}" if sol_price else "unavailable"
    print(f"wallet: {balance:.4f} SOL | SOL: {sol_str}")

    # Jupiter is the only venue and needs no persistent client — each operation
    # opens its own AsyncClient. (The vestigial drift_client parameter that was
    # threaded through every task-loop signature was removed 2026-07-26.)

    _all_pos = load_positions()
    if _all_pos:
        pos_str = " | ".join(f"{mkt} {p['direction'].upper()} ${p.get('size_usd','?')} {p.get('leverage','?')}x" for mkt, p in _all_pos.items())
    else:
        pos_str = "none"
    print(f"open positions: {pos_str}")
    print(f"leverage: {PERP_MIN_LEVERAGE:g}x-{PERP_MAX_LEVERAGE:g}x band | "
          f"min opening ${PERP_MIN_COLLATERAL_USD:.0f} collateral / ${PERP_MIN_NOTIONAL_USD:.0f} notional "
          f"| out-of-band DECLINES, never clamps")
    print(f"max leverage: {PERP_MAX_LEVERAGE:g}x (your cap, PERP_MAX_LEVERAGE) | {JUPITER_VENUE_MAX_LEVERAGE:g}x (Jupiter venue max)")
    print(f"one position per market: {'ON' if _perp_one_per_market() else 'OFF (long + short allowed as separate positions)'} (PERP_ONE_PER_MARKET)")
    print("commands: /perp /perpstats /perpclose")

    await notify(
        f"⚡ <b>Perps Bot v5.7 started</b>\n"
        f"💼 {balance:.4f} SOL | SOL @ {sol_str}\n"
        f"Open position: {pos_str}\n"
        f"Max leverage: {PERP_MAX_LEVERAGE:g}x (PERP_MAX_LEVERAGE) | one per market: {'on' if _perp_one_per_market() else 'off'}\n"
        + (f"{globals().get('_BOOT_NEW_SOURCE')}\n" if globals().get('_BOOT_NEW_SOURCE') else "")
        + (f"Patch ack: {globals().get('_BOOT_ACK_PATH')}\n" if globals().get('_BOOT_ACK_PATH') else "")
        + (f"{globals().get('_BOOT_RELOAD_NOTE')}\n" if globals().get('_BOOT_RELOAD_NOTE') else "")
        + ""
        f"🧠 Intel scanner: alerts only, the agent fires (every {SCAN_INTERVAL_SECONDS//60}min)\n"
        f"Thresholds: long<{LONG_SCORE_MAX} short>{SHORT_SCORE_MIN}\n"
        f"Venue: Jupiter\n"
        f"/perp /perpstats /perpclose"
    )

    # Sprint 44: load the agent's saved tuning defaults (the SAVE layer) before loops start
    _perp_load_saved_defaults()
    _perp_write_params_mirror()   # mirror the live surface -> perps_params.json

    asyncio.create_task(monitor_position())
    asyncio.create_task(telegram_listener(connection))
    asyncio.create_task(intelligence_scanner())

    # Sprint 43 / Build B proposal pipeline: retired, not started. Every trade is an
    # agent signal file handled by process_signal.

    # Sprint 27: new background loops
    # Sprint 57: drift_sync_loop + drift_balance_refresh_loop REMOVED (Drift gone).
    # feed no longer gets drift_market_state.json from us; it reads Jupiter
    # oracle prices directly / from the per-chain packets.
    asyncio.create_task(packet_builder_loop())
    asyncio.create_task(daily_bias_loop())
    asyncio.create_task(exit_monitor_loop())
    asyncio.create_task(query_handler_loop())
    asyncio.create_task(perp_counterfactual_loop())   # Sprint 47: autopsy resolver
    asyncio.create_task(perp_autopsy_eod_loop())                  # Sprint 47: autopsy EOD digest

    # Sprint 32 v2.7: morning packet at 6:05am EST (sequenced after feed's 6:00am)
    asyncio.create_task(perps_morning_packet_loop())
    asyncio.create_task(feed_loop())   # feeds: agent requests + optional FEED_CADENCE

    # Sprint 32+: meeting watcher (file-presence poll, LLM dormant until the agent writes a file)
    asyncio.create_task(perp_meeting_watcher_loop())
    
    # Sprint 32+: override expiry watcher (60s cadence, honors expires_at on overrides,
    # auto-clears + writes expiry packet to the agent inbox so direct orders honor their time bounds)
    asyncio.create_task(perp_expiry_watcher_loop())

    # Sprint 39: decline patterns + auto-tighten + auto-disable
    # Sprint 57: venue_health_probe_loop REMOVED — it probed Drift health for the
    # failover decision, and there is no failover anymore.
    asyncio.create_task(perps_decline_patterns_writer_loop())
    asyncio.create_task(perps_auto_tighten_loop())
    asyncio.create_task(owner_position_perps_loop())

    _PERP_HB   = os.path.join(WS_BOT_LOGS, "perps_bot_heartbeat.json")
    _last_hb   = 0.0

    try:
        while True:
            _lu_maybe_reload()  # live-update: safe-point reload (only when no op in flight)
            await perp_limit_watch_tick()   # parked limit entries (perp_limit_order.json)
            await perp_trade_autopsy_tick()   # trade autopsy worker (background, isolated)
            # heartbeat every 60s
            import time as _t
            if _t.time() - _last_hb > 60:
                try:
                    with open(_PERP_HB, "w") as _f:
                        json.dump({"timestamp": datetime.now(EST).isoformat(), "status": "alive", "interval_s": 60, "stale_after_s": 180}, _f)
                    _last_hb = _t.time()
                except Exception:
                    pass

            # Sprint 46: process EVERY relay file each poll, so SOL/BTC/ETH signals
            # fired together all execute in one cycle. Each is its own file, read +
            # processed + deleted independently — no overwrite, no lost-during-fire.
            for _relay_path, _forced_market in _PERP_RELAYS:
                if not os.path.exists(_relay_path):
                    continue
                _rname = os.path.basename(_relay_path)
                print(f"FOUND {_rname} — the agent signal")
                try:
                    with open(_relay_path, encoding="utf-8-sig") as f:
                        raw = f.read().strip()
                    if not raw:
                        # Was silent forever. An empty signal file means his writer
                        # produced nothing — he should know, once.
                        relay_ack_once(_rname, "<empty>", "perp_signal", False,
                                       "signal file is EMPTY — nothing to execute; "
                                       "left in place for retry",
                                       {"hint": "writer produced an empty file"})
                        print(f"[perp relay] {_rname} empty — leaving for retry")
                        continue
                    data = json.loads(raw)
                    # SHAPE CHECK (2026-07-27). perps expects a single signal OBJECT.
                    # A relay once took a top-level array from the agent and
                    # silently skipped it for weeks; check explicitly here so the
                    # same drift reports itself instead of surfacing as a cryptic
                    # AttributeError from somewhere inside process_signal.
                    if isinstance(data, list):
                        if len(data) == 1 and isinstance(data[0], dict):
                            relay_ack_once(_rname, raw, "perp_signal_shape", True,
                                           "signal arrived as a 1-item ARRAY; perps expects a "
                                           "single object. Unwrapped and executed — please emit "
                                           "the object directly.",
                                           {"observed": "list[1]", "expected": "object"})
                            data = data[0]
                        else:
                            relay_ack_once(_rname, raw, "perp_signal_shape", False,
                                           f"signal is an ARRAY of {len(data)} — perps takes ONE "
                                           f"signal per file, one file per chain "
                                           f"(perp_sol_signal.json / _btc_ / _eth_). Not executed.",
                                           {"observed": f"list[{len(data)}]", "expected": "object"})
                            os.remove(_relay_path)
                            continue
                    elif not isinstance(data, dict):
                        relay_ack_once(_rname, raw, "perp_signal_shape", False,
                                       f"signal is a {type(data).__name__}, not an object. Not executed.",
                                       {"observed": type(data).__name__, "expected": "object"})
                        os.remove(_relay_path)
                        continue
                    if _forced_market:
                        data["market"] = _forced_market  # filename is source of truth for the chain
                    _raw_act = str(
                        data.get("action") or data.get("direction")
                        or data.get("signal") or data.get("side") or ""
                    ).lower().strip()
                    # 2026-10-16: schema check with clear decline reasons (covers
                    # perps_is_order on opens). A bad shape is declined, never guessed.
                    _opening = (_raw_act in _PERP_OPEN_ACTIONS
                                and _raw_act not in _PERP_EXPLICIT_CLOSE_ACTIONS)
                    _errs = _perp_signal_schema_errors(data, _opening)
                    # An add is new exposure: it needs perps_is_order like an open.
                    # Closes, adjusts, partial closes and update_params do not.
                    if _raw_act in _PERP_ADD_ACTIONS and not _perps_order_flag_true(data):
                        _errs.append("perps_is_order must be true on an add (new exposure); without it nothing is sent")
                    if _errs:
                        _why = "; ".join(_errs)
                        relay_ack(_rname, "perp_signal", False,
                                  f"declined, not sent: {_why}. No transaction was sent.",
                                  detail={"action": _raw_act, "errors": _errs})
                        _perp_write_signal_ack(_rname, data, {"status": "declined", "reason": _why,
                                                              "at": datetime.now(EST).isoformat()})
                        try:
                            os.remove(_relay_path)
                            print(f"deleted {_rname} (declined: {_why})")
                        except FileNotFoundError:
                            pass
                        continue
                    # Consume BEFORE executing: a slow open (retries) must never be
                    # picked up and run a second time from the same file.
                    try:
                        os.remove(_relay_path)
                        print(f"consumed {_rname}")
                    except FileNotFoundError:
                        pass
                    print(f"perp signal ({_rname}): {data}")
                    globals()["_PERP_SIG_OUTCOME"] = None
                    globals()["_PERP_SIG_NOTES"] = []
                    try:
                        await process_signal(data)
                    finally:
                        _notes = globals().get("_PERP_SIG_NOTES") or []
                        globals()["_PERP_SIG_NOTES"] = None
                    _out = globals().get("_PERP_SIG_OUTCOME") or {}
                    if not _out:
                        _isref, _nr = _perp_note_reason(_notes)
                        if _opening or _isref:
                            _out = {"status": "declined",
                                    "reason": _nr or "the bot returned without a result or a message (silent path - report it)"}
                            print(f"[perp] DECLINED {data.get('action','?')} {data.get('market','SOL-PERP')}: {_out['reason']}")
                        else:
                            _out = {"status": "processed"}
                            if _nr:
                                _out["note"] = _nr
                        _out["at"] = datetime.now(EST).isoformat()
                    _ok = _out.get("status") in ("filled", "processed", "parked")
                    _summ = (f"{data.get('action','?')} {data.get('market','SOL-PERP')} {_out.get('status')}"
                             + (f" tx {str(_out.get('tx'))[:20]}..." if _out.get("tx") else "")
                             + (f" - {_out.get('reason')}" if _out.get("reason") else ""))
                    relay_ack(_rname, "perp_signal", _ok, _summ,
                              detail={"action": data.get("action"), "market": data.get("market"), **_out},
                              alerted=bool(_notes))
                    _perp_write_signal_ack(_rname, data, _out)
                except json.JSONDecodeError as e:
                    # Malformed JSON is the agent's to fix — tell him instead of
                    # retrying a broken file forever in silence.
                    relay_ack_once(_rname, raw if "raw" in dir() else "<unread>",
                                   "perp_signal", False,
                                   f"bad JSON, not parseable: {str(e)[:200]}",
                                   {"hint": "re-emit valid JSON; file left in place for retry"})
                    print(f"[perp relay] {_rname} bad JSON: {e} — leaving for retry")
                    continue
                except Exception as e:
                    try:
                        _tb_path = os.path.join(WS_BOT_LOGS, "perp_signal_traceback.txt")
                        with open(_tb_path, "w", encoding="utf-8") as _tb:
                            _tb.write(traceback.format_exc())
                    except Exception:
                        pass
                    # Ack the FAILURE to the agent — he wrote this signal and needs to
                    # know it died, otherwise he waits on a fill that never comes.
                    # notify_error goes to the owner, which is the wrong audience for a
                    # malformed signal the agent can just fix and resend.
                    relay_ack(_rname, "perp_signal", False,
                              f"signal raised {type(e).__name__} during processing: {str(e)[:200]}",
                              detail={"error_type": type(e).__name__, "error": str(e)[:500]},
                              alerted=True)  # notify_error below mirrors the alert
                    await notify_error(f"processing {_rname}", e)
                    traceback.print_exc()
                    try:
                        os.remove(_relay_path)
                    except FileNotFoundError:
                        pass  # already gone — fine
            await asyncio.sleep(_perp_get_override("SIGNAL_POLL", SIGNAL_POLL))

    except Exception as e:
        await notify_error("PERPS BOT CRASHED", e)
        traceback.print_exc()
    finally:
        await connection.close()


# ── sub-bot supervisor ──────────────────────────────────────────────────────
# perps.py boots its sub-bots itself as child processes, each logging to
# bot_logs/<name>.console.log, restarted with backoff if one crashes, and all
# stopped together on Ctrl+C, "Stop Perps" in the console, or any exit.
# PERPS_COMPONENTS (comma list) limits which ones start; unset = all.
# owner_commands starts only when OWNER_COMMANDS != off.
import signal as _sup_signal
import subprocess as _sup_subprocess
import threading as _sup_threading
import sys

_SUP_BOTS = os.path.dirname(os.path.abspath(__file__))
_SUP_SUB = os.path.join(_SUP_BOTS, "sub-bots")
COMPONENTS = [  # (name, script path)
    ("security_warden", os.path.join(_SUP_SUB, "security_warden.py")),
    ("agent_autoagent", os.path.join(_SUP_SUB, "agent_autoagent.py")),
    ("code_tuner", os.path.join(_SUP_SUB, "code_tuner.py")),
    ("learnings_writer", os.path.join(_SUP_SUB, "learnings_writer.py")),
]
OWNER_COMMANDS_COMPONENT = ("owner_commands", os.path.join(_SUP_SUB, "owner_commands.py"))
OWNER_COMMANDS_CHOICES = ("off", "telegram", "file")
LAUNCHER_LOG = os.path.join(WS_BOT_LOGS, "perps_launcher.log")
LAUNCHER_HB = os.path.join(WS_BOT_LOGS, "perps_launcher_heartbeat.json")
ENGINE_HB = os.path.join(WS_BOT_LOGS, "perps_bot_heartbeat.json")


def _sup_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sup_log(msg: str) -> None:
    try:
        os.makedirs(WS_BOT_LOGS, exist_ok=True)
        with open(LAUNCHER_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _sup_read_json(p, default=None):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _sup_write_json(p, obj) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, p)


def owner_commands_mode() -> str:
    m = str(os.getenv("OWNER_COMMANDS") or "off").strip().lower()
    return m if m in OWNER_COMMANDS_CHOICES else "off"


class Child:
    def __init__(self, name: str, script: str):
        self.name, self.script = name, script
        self.proc = None
        self.restarts = 0
        self.started = 0.0
        self.next_start = 0.0
        self.state = "pending"
        self.log = None

    def start(self) -> None:
        if not os.path.exists(self.script):
            self.state = "missing"; return
        os.makedirs(WS_BOT_LOGS, exist_ok=True)
        self.log = open(os.path.join(WS_BOT_LOGS, f"{self.name}.console.log"), "a", encoding="utf-8")
        self.log.write(f"\n==== start {_sup_now_iso()} (restart #{self.restarts}) ====\n"); self.log.flush()
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", WORKSPACE_DIR=WS_ROOT)
        self.proc = _sup_subprocess.Popen([sys.executable, self.script], cwd=os.path.dirname(self.script),
                                          stdout=self.log, stderr=_sup_subprocess.STDOUT,
                                          stdin=_sup_subprocess.DEVNULL, env=env)
        self.started = time.time(); self.state = "running"
        _sup_log(f"started {self.name} pid={self.proc.pid}")

    def poll(self) -> None:
        if self.state in ("missing", "stopped"):
            return
        if self.proc and self.proc.poll() is None:
            return
        if self.proc is not None:
            rc = self.proc.returncode
            _sup_log(f"{self.name} exited rc={rc}")
            self.proc = None
            if self.log:
                self.log.close(); self.log = None
            if time.time() - self.started > 300:
                self.restarts = 0       # it ran a while; reset the backoff
            self.restarts += 1
            self.next_start = time.time() + min(60, 5 * self.restarts)
            self.state = f"crashed rc={rc}"
        if time.time() >= self.next_start:
            self.start()

    def stop(self, timeout: float = 20.0) -> None:
        p = self.proc
        if not p or p.poll() is not None:
            self.state = "stopped"
            return
        try:
            if os.name != "nt":
                p.send_signal(_sup_signal.SIGINT)
        except Exception:
            pass
        try:
            p.wait(timeout=timeout)
        except Exception:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        if self.log:
            self.log.close(); self.log = None
        self.state = "stopped"


def write_stop_reason(reason: str = "user_stopped") -> None:
    hb = _sup_read_json(LAUNCHER_HB, {}) or {}
    hb.update({"stop_reason": reason, "stopped_at": _sup_now_iso()})
    _sup_write_json(LAUNCHER_HB, hb)
    eng = _sup_read_json(ENGINE_HB, None)
    if isinstance(eng, dict) and not eng.get("stop_reason"):
        eng.update({"stop_reason": reason, "stopped_at": _sup_now_iso()})
        _sup_write_json(ENGINE_HB, eng)
    _sup_log(f"stop_reason {reason}")


def start_all():
    """Start the sub-bots and a supervisor thread. Returns stop_all(reason)."""
    only = [s.strip() for s in (os.getenv("PERPS_COMPONENTS") or "").split(",") if s.strip()]
    comps = list(COMPONENTS)
    if owner_commands_mode() != "off":
        comps.append(OWNER_COMMANDS_COMPONENT)      # optional; off by default
    children = [Child(n, p) for n, p in comps if not only or n in only]
    _sup_write_json(LAUNCHER_HB, {"started_at": _sup_now_iso(), "pid": os.getpid(),
                                  "components": [c.name for c in children]})
    _sup_log("start: " + (", ".join(c.name for c in children) or "(none)"))
    print("[perps] sub-bots: " + (", ".join(c.name for c in children) or "none")
          + " (logs: bot_logs/<name>.console.log)", flush=True)
    halt = _sup_threading.Event()
    stopped = _sup_threading.Event()

    def _supervise():
        while not halt.is_set():
            for c in children:
                c.poll()
            hb = _sup_read_json(LAUNCHER_HB, {}) or {}
            hb["last_update"] = _sup_now_iso()
            hb["states"] = {c.name: c.state for c in children}
            try:
                _sup_write_json(LAUNCHER_HB, hb)
            except Exception:
                pass
            halt.wait(2)

    def _stop_all(reason: str = "user_stopped"):
        if stopped.is_set():
            return
        stopped.set(); halt.set()
        print("[perps] stopping all components...", flush=True)
        threads = [_sup_threading.Thread(target=c.stop) for c in children]
        for th in threads:
            th.start()
        for th in threads:
            while th.is_alive():
                try:
                    th.join()
                except KeyboardInterrupt:   # a second Ctrl+C must not orphan a child
                    continue
        try:
            hb = _sup_read_json(LAUNCHER_HB, {}) or {}
            hb["states"] = {c.name: c.state for c in children}
            _sup_write_json(LAUNCHER_HB, hb)
        except Exception:
            pass
        write_stop_reason(reason)
        print("[perps] all stopped", flush=True)

    _sup_threading.Thread(target=_supervise, daemon=True, name="supervisor").start()
    return _stop_all


def _perps_rotate_console_log():
    """2026-10-16: one clean console log per run. The previous run moves to
    bot_logs/console_runs/perps.console_<ts>.log (last 10 kept)."""
    try:
        cur = os.path.join(WS_BOT_LOGS, "perps.console.log")
        runs = os.path.join(WS_BOT_LOGS, "console_runs")
        if os.path.exists(cur) and os.path.getsize(cur) > 0:
            os.makedirs(runs, exist_ok=True)
            ts = datetime.fromtimestamp(os.path.getmtime(cur), EST).strftime("%Y%m%d_%H%M%S")
            os.replace(cur, os.path.join(runs, f"perps.console_{ts}.log"))
            old = sorted(f for f in os.listdir(runs) if f.startswith("perps.console_"))
            for f in old[:-10]:
                try:
                    os.remove(os.path.join(runs, f))
                except Exception:
                    pass
        with open(cur, "w", encoding="utf-8") as f:
            f.write(f"==== perps run {datetime.now(EST).isoformat()} pid {os.getpid()} ====\n")
    except Exception as e:
        print(f"[perps] console log rotate err: {e}")


class _PerpsTee:
    """No-UI runs: print to the terminal AND bot_logs/perps.console.log."""
    def __init__(self, stream, path):
        self._s, self._p = stream, path

    def write(self, x):
        try:
            self._s.write(x)
        except Exception:
            pass
        try:
            with open(self._p, "a", encoding="utf-8") as f:
                f.write(x)
        except Exception:
            pass
        return len(x) if isinstance(x, str) else 0

    def flush(self):
        try:
            self._s.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._s.isatty()
        except Exception:
            return False


if __name__ == "__main__":
    _perps_rotate_console_log()
    # Console UI (perps_ui.py): logo splash + menu in this window while the engine trades.
    # Fault isolated: no TTY, PERPS_UI=0 or --no-ui -> no UI, engine runs exactly as before.
    _PERPS_UI_MOD, _PERPS_UI_ON = None, False
    try:
        import perps_ui as _PERPS_UI_MOD
        _PERPS_UI_ON = _PERPS_UI_MOD.start_hosted()
    except Exception:
        _PERPS_UI_MOD, _PERPS_UI_ON = None, False
    if not _PERPS_UI_ON:
        import sys as _tee_sys
        _tee_p = os.path.join(WS_BOT_LOGS, "perps.console.log")
        _tee_sys.stdout = _PerpsTee(_tee_sys.stdout, _tee_p)
        _tee_sys.stderr = _PerpsTee(_tee_sys.stderr, _tee_p)
    _STOP_ALL, _STOP_REASON = None, "user_stopped"
    try:
        _STOP_ALL = start_all()
    except Exception as e:
        _sup_log(f"supervisor start failed: {e}")
        print(f"[perps] sub-bots not started ({type(e).__name__}); engine runs on")
    try:
        asyncio.run(main_async())
        _STOP_REASON = "engine_exited"
    except KeyboardInterrupt:
        print("stopped by user")
    except Exception as e:
        _STOP_REASON = f"exception:{type(e).__name__}"
        print(f"FATAL: {e}")
        traceback.print_exc()
    finally:
        if _STOP_ALL is not None:
            _STOP_ALL(_STOP_REASON)
        if _PERPS_UI_ON and _PERPS_UI_MOD is not None:
            _PERPS_UI_MOD.shutdown_hosted("exit")