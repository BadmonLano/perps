"""agent_ack.py - the ecosystem's acknowledgment channel (agent_ack_v1).

(Public build: history trimmed; see TIMELINE.md.)
"""

import os

# ── Workspace root (public build) ─────────────────────────────────────────
# WORKSPACE_DIR in the environment wins; otherwise the repo root is the
# folder that contains bots/ (works from bots/ and bots/sub-bots/).
def _resolve_workspace_root():
    _env = (os.environ.get("WORKSPACE_DIR") or "").strip()
    if _env:
        return os.path.abspath(os.path.expanduser(_env))
    _p = os.path.dirname(os.path.abspath(__file__))
    _root = None
    for _ in range(6):
        if os.path.basename(_p).lower() == "bots":
            _root = os.path.dirname(_p); break
        if os.path.isdir(os.path.join(_p, "bots")) and os.path.isdir(os.path.join(_p, "skills")):
            _root = _p; break
        _p = os.path.dirname(_p)
    _root = _root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # .env may not be loaded yet at import time: honour WORKSPACE_DIR set there too.
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
for _d in (os.path.join(WS_ROOT, "inbox"), os.path.join(WS_ROOT, "inbox_results", "done"),
           os.path.join(WS_ROOT, "inbox_results", "failed"), os.path.join(WS_ROOT, "owner_inbox"),
           os.path.join(WS_ROOT, "learnings"), os.path.join(WS_ROOT, "rules"), WS_BOT_LOGS, WS_JSON_RELAY):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass
# ──────────────────────────────────────────────────────────────────────────

import json
import time
import hashlib
import contextlib
from datetime import datetime, timezone

SCHEMA = "agent_ack_v1"

# Bus lives under bot_logs/ next to this module (bots/). A caller may override `bus`.
_HERE = WS_BOTS
ACK_BUS = os.path.join(WS_BOT_LOGS, "agent_acks.jsonl")

VALID_STAGES = {"received", "applied", "resulting_state", "rejected"}
VALID_RESULTS = {"ok", "error", "partial", "rejected", "noop", "timeout_internal"}

# For VBR ordering: how "far" a stage is. `rejected` is a terminal OBSERVED reaction,
# ranked alongside `applied` so it satisfies a "did the consumer handle it?" check.
_STAGE_RANK = {"received": 1, "applied": 2, "rejected": 2, "resulting_state": 3}


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


def make_fire_id(signal_type, path="", extra=""):
    """Derive a stable fire_id from material fields when the agent didn't stamp one.
    Deterministic within a fire (no wall-clock in the hash) so producer and reader
    agree. Callers that already have a real id should pass that instead."""
    material = f"{signal_type}|{path}|{extra}"
    return f"{signal_type}_{hashlib.sha256(material.encode()).hexdigest()[:10]}"


def _default_dedupe_key(ack_of, stage):
    sid = ack_of.get("signal_id") or ack_of.get("path") or "?"
    stype = ack_of.get("signal_type", "?")
    return f"ack|{stype}|{sid}|{stage}"


def _iter_bus(bus):
    """Yield parsed ack dicts from the bus, tolerating partial/corrupt lines."""
    if not os.path.exists(bus):
        return
    try:
        with open(bus, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


def _has_dedupe(bus, dedupe_key):
    for a in _iter_bus(bus):
        if a.get("dedupe_key") == dedupe_key:
            return True
    return False


@contextlib.contextmanager
def _bus_lock(bus, timeout=5.0):
    """Cross-platform exclusive lock on a per-bus lockfile so concurrent multi-writer
    appends (patch acks + signal acks from N bots) cannot interleave or both pass the
    dedupe check. Windows: msvcrt.locking; POSIX: fcntl.flock. Spin-retries up to
    timeout then proceeds best-effort - a lock hiccup must never drop a real ack."""
    lockpath = bus + ".lock"
    os.makedirs(os.path.dirname(bus), exist_ok=True)
    f = open(lockpath, "a+")
    acquired = False
    deadline = time.time() + timeout
    try:
        try:
            import msvcrt
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.05)
        except ImportError:
            import fcntl
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.05)
        yield acquired
    finally:
        try:
            if acquired:
                try:
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except ImportError:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass


def write_ack(*, ack_of, stage, source, result="ok", summary="",
              resulting_state=None, error=None, dedupe_key=None, bus=ACK_BUS):
    """Emit ONE ack to the bus.

    ack_of: dict identifying the fire - {signal_id, path, signal_type, fired_ts,
            fired_by}. At least one of signal_id / path should be present.
    stage:  received | applied | resulting_state | rejected
    source: the consumer emitting the ack (e.g. "perps_bot", "signal_executor")
    result: ok | error | partial | rejected | noop | timeout_internal

    Returns the ack dict (written or already-present via dedupe), or None on failure.
    RMW-safe append. A consumer calls this ONLY for what it actually observed - it
    must never be used to self-satisfy VBR on a fire nothing consumed.
    """
    if stage not in VALID_STAGES:
        raise ValueError(f"invalid stage {stage!r} (want one of {sorted(VALID_STAGES)})")
    if result not in VALID_RESULTS:
        raise ValueError(f"invalid result {result!r} (want one of {sorted(VALID_RESULTS)})")
    ack_of = dict(ack_of or {})
    # 2026-10-14: every ack echoes the originating id as request_id (signal_id / id / request_id)
    _rid = ack_of.get("request_id") or ack_of.get("signal_id") or ack_of.get("id")
    if _rid:
        ack_of["request_id"] = _rid
    ded = dedupe_key or _default_dedupe_key(ack_of, stage)
    ack = {
        "schema": SCHEMA,
        "ts": _now_iso(),
        "request_id": _rid,
        "ack_of": ack_of,
        "stage": stage,
        "source": source,
        "result": result,
        "summary": (summary or "")[:200],
        "resulting_state": resulting_state or {},
        "error": error,
        "dedupe_key": ded,
    }
    try:
        os.makedirs(os.path.dirname(bus), exist_ok=True)
        with _bus_lock(bus):  # atomic read-dedupe-append across concurrent writers
            if _has_dedupe(bus, ded):
                return ack  # collapse a repeated stage, do not append a duplicate line
            with open(bus, "a", encoding="utf-8") as f:
                f.write(json.dumps(ack, default=str) + "\n")
        return ack
    except Exception:
        return None


def read_acks(*, signal_id=None, path=None, source=None, since_ts=None, bus=ACK_BUS):
    """Return acks matching a fire (by signal_id and/or path), oldest-first."""
    out = []
    for a in _iter_bus(bus):
        ao = a.get("ack_of", {})
        if signal_id is not None and ao.get("signal_id") != signal_id:
            continue
        if path is not None and ao.get("path") != path:
            continue
        if source is not None and a.get("source") != source:
            continue
        if since_ts is not None and a.get("ts", "") < since_ts:
            continue
        out.append(a)
    return out


def is_verified(*, signal_id=None, path=None, require_stage="received", bus=ACK_BUS):
    """VBR helper. True iff at least one ack at >= require_stage exists for the fire.

    - No ack at all  -> False (UNVERIFIED). Absence is signal; elapsed time is not.
    - A `rejected` ack -> counts as observed handling (verified-rejected).
    """
    need = _STAGE_RANK.get(require_stage, 1)
    for a in read_acks(signal_id=signal_id, path=path, bus=bus):
        if _STAGE_RANK.get(a.get("stage"), 0) >= need:
            return True
    return False


def ack_patch_landed(*, bot_name, from_hash, to_hash, bus=ACK_BUS):
    """P0 conforming producer: the lived `patch_landed_ack`, emitted as agent_ack_v1.

    Call from a bot's boot-detect-patch path ALONGSIDE the existing patch_landed_ack
    packet (do not break existing readers - this is the parallel conforming record).
    One line at the call site:

        import agent_ack
        agent_ack.ack_patch_landed(bot_name="perps_bot",
                                    from_hash=old_hash, to_hash=new_hash)
    """
    return write_ack(
        ack_of={"signal_type": "code_patch", "signal_id": to_hash,
                "path": f"bots/{bot_name}.py", "fired_by": "owner"},
        stage="applied", source=bot_name, result="ok",
        summary=f"patch landed {from_hash} -> {to_hash}; bot restarted",
        resulting_state={"bot": bot_name, "patch_hash": to_hash, "prev_hash": from_hash},
        dedupe_key=f"ack|code_patch|{to_hash}|applied", bus=bus)