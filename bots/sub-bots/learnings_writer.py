"""
learnings_writer.py - the one safe way to add an entry to learnings/LEARNINGS.md.

Two jobs:
  1. Library. Any bot or helper calls append_to_learnings(); the entry lands
     INSIDE Section 8 (between the append-window markers) so the nightly
     self-eval sees it. A file lock stops two writers from losing each other's
     entries, and the write is atomic (.tmp + replace).
  2. Sub-bot. Started by the launcher like the other sub-bots. Every
     LEARNINGS_WRITER_INTERVAL_S seconds it:
       - creates learnings/LEARNINGS.md from LEARNINGS.template.md if missing,
       - restores a missing append-window marker (never deletes text),
       - drains requests the agent drops in json_relay/learnings/*.json
         ({"entry": "...", "type": "trade_lesson", "source": "agent"}),
         moving each to json_relay/learnings/done/ or failed/,
       - writes bot_logs/learnings_writer_heartbeat.json.
     Every append is logged to bot_logs/learnings_writer.jsonl (type, source,
     size; never the text of a secret, never a key).

Use:
    from learnings_writer import append_to_learnings
    append_to_learnings("lesson text", "trade_lesson", "perps_bot")

CLI (run from the drop folder):
    python bots/sub-bots/learnings_writer.py            run as the sub-bot (loop)
    python bots/sub-bots/learnings_writer.py --once     one pass, then exit
    python bots/sub-bots/learnings_writer.py --check    file, markers, pending entries
    python bots/sub-bots/learnings_writer.py --append "text" --type bot_finding --source agent
    python bots/sub-bots/learnings_writer.py --selftest scratch-folder test, touches nothing real

Closed-world: reads and writes only inside this drop. No network.
"""
from __future__ import annotations

import argparse
import json
import os

def _envv(key, default=None):
    """os.getenv, but a blank value (KEY= in .env) counts as unset and returns the default."""
    v = os.environ.get(key)
    return default if v is None or not str(v).strip() else v

import shutil
import sys
import tempfile
import time
from datetime import datetime


# ── paths (relative to the drop) ──────────────────────────────────────────────
def _find_ws() -> str:
    env = (os.environ.get("WORKSPACE_DIR") or "").strip()
    if env and os.path.isdir(os.path.join(env, "bots")):
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(4):
        if os.path.isdir(os.path.join(cur, "bots")) and os.path.isdir(os.path.join(cur, "learnings")):
            return cur
        cur = os.path.dirname(cur)
    return os.path.dirname(os.path.dirname(here))   # bots/sub-bots -> drop


def _set_paths(ws: str) -> None:
    global WS, LEARNINGS_DIR, LEARNINGS_PATH, TEMPLATE_PATH, LOCK_PATH, BOT_LOGS
    global WRITE_LOG, HEARTBEAT, REQUEST_DIR
    WS = ws
    LEARNINGS_DIR = os.path.join(WS, "learnings")
    LEARNINGS_PATH = os.path.join(LEARNINGS_DIR, "LEARNINGS.md")
    TEMPLATE_PATH = os.path.join(LEARNINGS_DIR, "LEARNINGS.template.md")
    LOCK_PATH = os.path.join(LEARNINGS_DIR, ".write_lock")
    BOT_LOGS = os.path.join(WS, "bot_logs")
    WRITE_LOG = os.path.join(BOT_LOGS, "learnings_writer.jsonl")
    HEARTBEAT = os.path.join(BOT_LOGS, "learnings_writer_heartbeat.json")
    REQUEST_DIR = os.path.join(WS, "json_relay", "learnings")


_set_paths(_find_ws())

LOCK_TIMEOUT_S = 10
LOCK_STALE_S = 30
INTERVAL_S = max(10, int(float(_envv("LEARNINGS_WRITER_INTERVAL_S") or 60)))
MAX_ENTRY_CHARS = 2000

# Must match LEARNINGS.template.md exactly. The em-dash variant is accepted too.
BEGIN_MARKER = "<!-- APPEND BELOW THIS LINE - DO NOT EDIT ANYTHING ABOVE -->"
END_MARKER = "<!-- end of append window -->"
_BEGIN_ALT = "<!-- APPEND BELOW THIS LINE \u2014 DO NOT EDIT ANYTHING ABOVE -->"

# Documented types (LEARNINGS.template.md). Left: what callers may pass. Right: type written.
CATEGORY_NORMALIZE = {
    "trade_lesson": "trade_lesson",
    "pattern_finding": "pattern_finding",
    "pattern_lesson": "pattern_finding",
    "bot_finding": "bot_finding",
    "code_change": "bot_finding",
    "rule_update": "rule_update",
    "autopsy": "autopsy",
    "perp_lesson": "trade_lesson",
    "overseer_followup": "overseer_followup",
    "sub_agent_insight": "sub_agent_insight",
    "agent_finding": "sub_agent_insight",
    "error_pattern": "error_pattern",
    "error": "error_pattern",
    "alignment": "alignment",
    "self_eval": "self_eval",
    "nightly_summary": "self_eval",
    "market_observation": "self_eval",
}


def normalize_category(category: str) -> tuple[str, bool]:
    """(written_type, remapped). Unknown types fall back to bot_finding."""
    t = CATEGORY_NORMALIZE.get((category or "").strip())
    if t is None:
        return "bot_finding", True
    return t, t != category


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [learnings_writer] {msg}", flush=True)


def _jsonl(path: str, rec: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for attempt in range(5):   # Windows: a reader can hold the file for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.5)


# ── lock ──────────────────────────────────────────────────────────────────────
def _acquire_lock(timeout: float = LOCK_TIMEOUT_S) -> bool:
    os.makedirs(LEARNINGS_DIR, exist_ok=True)
    start = time.time()
    while time.time() - start < timeout:
        try:
            if os.path.exists(LOCK_PATH) and time.time() - os.path.getmtime(LOCK_PATH) > LOCK_STALE_S:
                try:
                    os.remove(LOCK_PATH)
                except OSError:
                    pass
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # atomic create
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            time.sleep(0.25)
        except OSError:
            time.sleep(0.25)
    return False


def _release_lock() -> None:
    try:   # only remove a lock we own (close it first: Windows can't delete an open file)
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            owner = f.read().strip()
        if owner == str(os.getpid()):
            os.remove(LOCK_PATH)
    except Exception:
        pass


# ── names for the template placeholders ───────────────────────────────────────
def _name_setting(key: str) -> str:
    """AGENT_NAME / OWNER_NAME from the environment, else from bots/.env (blank = unset)."""
    v = (os.environ.get(key) or "").strip()
    if v:
        return v[:60]
    try:
        with open(os.path.join(WS, "bots", ".env"), "r", encoding="utf-8-sig", errors="replace") as f:
            for ln in f:
                k, sep, val = ln.strip().partition("=")
                if sep and k.strip() == key:
                    val = val.split(" #", 1)[0].strip().strip('"').strip("'")
                    if val:
                        return val[:60]
    except OSError:
        pass
    return ""


# ── file health ───────────────────────────────────────────────────────────────
def ensure_learnings_file() -> str:
    """Create LEARNINGS.md from the template if missing. Returns 'exists' | 'created' | 'no_template'."""
    if os.path.exists(LEARNINGS_PATH):
        return "exists"
    os.makedirs(LEARNINGS_DIR, exist_ok=True)
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        filled = []
        for ph, key in (("{AGENT_NAME}", "AGENT_NAME"), ("{USER}", "OWNER_NAME")):
            val = _name_setting(key)
            if val and ph in text:
                text = text.replace(ph, val)
                filled.append(key)
        _atomic_write(LEARNINGS_PATH, text)
        _log("created learnings/LEARNINGS.md from LEARNINGS.template.md"
             + (f" (filled {', '.join(filled)})" if filled else
                " (set AGENT_NAME / OWNER_NAME in bots/.env to fill {AGENT_NAME} / {USER})"))
        return "created"
    _atomic_write(LEARNINGS_PATH, "# LEARNINGS.md\n\n## 8. TODAY'S APPEND WINDOW\n\n"
                  f"{BEGIN_MARKER}\n{END_MARKER}\n")
    _log("created a minimal learnings/LEARNINGS.md (template missing)")
    return "no_template"


def _insert(existing: str, formatted: str) -> tuple[str, str]:
    """Return (new_text, how). Never removes existing text."""
    if END_MARKER in existing:
        return existing.replace(END_MARKER, f"{formatted}\n{END_MARKER}", 1), "between_markers"
    for bm in (BEGIN_MARKER, _BEGIN_ALT):
        if bm in existing:
            return existing.replace(bm, f"{bm}\n{formatted}\n{END_MARKER}", 1), "end_marker_restored"
    return (existing.rstrip() + f"\n\n{BEGIN_MARKER}\n{formatted}\n{END_MARKER}\n"), "markers_restored"


def pending_entries(text: str | None = None) -> int:
    """Entries waiting in Section 8 (between the markers)."""
    if text is None:
        try:
            with open(LEARNINGS_PATH, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return 0
    b = max(text.find(BEGIN_MARKER), text.find(_BEGIN_ALT))
    e = text.find(END_MARKER)
    if b < 0 or e < 0 or e < b:
        return 0
    return sum(1 for ln in text[b:e].splitlines() if ln.startswith("### "))


# ── the one write path ────────────────────────────────────────────────────────
def append_to_learnings(entry: str, category: str = "bot_finding", source: str = "unknown") -> bool:
    """Append one entry to Section 8 of learnings/LEARNINGS.md. True on success."""
    if not entry or not str(entry).strip():
        return False
    body = str(entry).strip()[:MAX_ENTRY_CHARS]
    source = (str(source or "unknown").strip() or "unknown")[:60]
    if not _acquire_lock():
        _log(f"could not get the write lock, skipped entry from {source}")
        _jsonl(WRITE_LOG, {"ts": _now_iso(), "ok": False, "source": source, "why": "lock_timeout"})
        return False
    try:
        ensure_learnings_file()
        written_type, remapped = normalize_category(category)
        if remapped:
            body = f"[orig_type: {category}]\n{body}"
        formatted = f"\n### {_now_iso()} | {written_type} | {source}\n{body}\n"
        with open(LEARNINGS_PATH, "r", encoding="utf-8", errors="replace") as f:
            existing = f.read()
        new_text, how = _insert(existing, formatted)
        if how != "between_markers":
            _log(f"append-window marker missing; {how.replace('_', ' ')} (source {source})")
        _atomic_write(LEARNINGS_PATH, new_text)
        _jsonl(WRITE_LOG, {"ts": _now_iso(), "ok": True, "type": written_type, "source": source,
                           "chars": len(body), "how": how})
        _log(f"appended {written_type} from {source} ({len(body)} chars)")
        return True
    except Exception as e:
        _log(f"write failed from {source}: {type(e).__name__}: {e}")
        _jsonl(WRITE_LOG, {"ts": _now_iso(), "ok": False, "source": source, "why": type(e).__name__})
        return False
    finally:
        _release_lock()


# ── convenience wrappers (this drop's lanes) ──────────────────────────────────
def log_perp_lesson(market: str, direction: str, result: str, pnl_usd: float, context: str,
                    source: str = "perps_bot") -> bool:
    return append_to_learnings(f"Market: {market} | Direction: {direction} | Result: {result} | PnL: ${pnl_usd:+.2f}\n"
                               f"Context: {context}", "trade_lesson", source)


def log_pattern_finding(pattern: str, result: str, conditions: str, source: str = "perps_bot") -> bool:
    return append_to_learnings(f"Pattern: {pattern} | Result: {result}\nConditions: {conditions}",
                               "pattern_finding", source)


def log_param_change(market: str, change: str, why: str, source: str = "agent") -> bool:
    return append_to_learnings(f"Market: {market}\nChange: {change}\nReason: {why}", "rule_update", source)


def log_code_change(bot_file: str, what_changed: str, why: str, source: str = "code_tuner") -> bool:
    return append_to_learnings(f"File: {bot_file}\nChange: {what_changed}\nReason: {why}", "code_change", source)


def log_error(bot: str, error: str, impact: str, fix: str = "", source: str = "security_warden") -> bool:
    entry = f"Bot: {bot}\nError: {error}\nImpact: {impact}" + (f"\nFix: {fix}" if fix else "")
    return append_to_learnings(entry, "error_pattern", source)


def log_overseer_followup(lines: list[str], source: str = "overseer") -> bool:
    lines = [str(l).strip() for l in (lines or []) if str(l).strip()]
    return bool(lines) and append_to_learnings("\n".join(f"- {l}" for l in lines), "overseer_followup", source)


# ── request drain (agent -> json_relay/learnings/*.json) ──────────────────────
def drain_requests() -> dict:
    out = {"ok": 0, "failed": 0}
    if not os.path.isdir(REQUEST_DIR):
        return out
    for name in sorted(os.listdir(REQUEST_DIR)):
        if not name.endswith(".json"):
            continue
        src = os.path.join(REQUEST_DIR, name)
        ok = False
        try:
            with open(src, "r", encoding="utf-8") as f:
                req = json.load(f)
            items = req if isinstance(req, list) else [req]
            ok = all(isinstance(it, dict) and append_to_learnings(
                it.get("entry", ""), it.get("type") or it.get("category") or "bot_finding",
                it.get("source") or "agent") for it in items) and bool(items)
        except Exception as e:
            _log(f"bad request {name}: {type(e).__name__}")
        dest = os.path.join(REQUEST_DIR, "done" if ok else "failed")
        os.makedirs(dest, exist_ok=True)
        try:
            os.replace(src, os.path.join(dest, name))
        except OSError:
            pass
        out["ok" if ok else "failed"] += 1
    return out


def _heartbeat(status: str, extra: dict | None = None) -> None:
    try:
        os.makedirs(BOT_LOGS, exist_ok=True)
        rec = {"bot": "learnings_writer", "ts": _now_iso(), "status": status, "pid": os.getpid()}
        rec.update(extra or {})
        _atomic_write(HEARTBEAT, json.dumps(rec, indent=2))
    except Exception:
        pass


def run_once() -> dict:
    created = ensure_learnings_file()
    fixed = False
    try:
        with open(LEARNINGS_PATH, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if END_MARKER not in text or (BEGIN_MARKER not in text and _BEGIN_ALT not in text):
            if _acquire_lock():
                try:
                    with open(LEARNINGS_PATH, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    if END_MARKER not in text or (BEGIN_MARKER not in text and _BEGIN_ALT not in text):
                        if END_MARKER in text:   # begin missing: put it right above the end marker
                            text = text.replace(END_MARKER, f"{BEGIN_MARKER}\n{END_MARKER}", 1)
                        else:
                            text, _ = _insert(text, "")
                        _atomic_write(LEARNINGS_PATH, text)
                        fixed = True
                        _log("restored the append-window markers")
                finally:
                    _release_lock()
    except OSError:
        pass
    drained = drain_requests()
    res = {"file": created, "markers_fixed": fixed, "requests": drained, "pending": pending_entries()}
    _heartbeat("ok", res)
    return res


def check() -> dict:
    exists = os.path.exists(LEARNINGS_PATH)
    text = ""
    if exists:
        with open(LEARNINGS_PATH, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    queued = len([n for n in os.listdir(REQUEST_DIR) if n.endswith(".json")]) if os.path.isdir(REQUEST_DIR) else 0
    return {"learnings_file": os.path.relpath(LEARNINGS_PATH, WS), "exists": exists,
            "template": os.path.exists(TEMPLATE_PATH),
            "begin_marker": (BEGIN_MARKER in text) or (_BEGIN_ALT in text), "end_marker": END_MARKER in text,
            "pending_entries": pending_entries(text) if exists else 0, "queued_requests": queued}


def selftest() -> bool:
    real = WS
    tmp = tempfile.mkdtemp(prefix="lw_selftest_")
    ok = True
    try:
        for d in ("bots", "learnings", "bot_logs", os.path.join("json_relay", "learnings")):
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
        if os.path.exists(TEMPLATE_PATH):
            shutil.copyfile(TEMPLATE_PATH, os.path.join(tmp, "learnings", "LEARNINGS.template.md"))
        _set_paths(tmp)

        def t(name, cond):
            nonlocal ok
            ok &= bool(cond)
            print(("PASS " if cond else "FAIL ") + name)

        with open(os.path.join(tmp, "bots", ".env"), "w", encoding="utf-8") as f:
            f.write("AGENT_NAME=TestAgent\nOWNER_NAME=\n")
        t("file created from template", run_once()["file"] in ("created", "no_template"))
        if os.path.exists(TEMPLATE_PATH):
            _txt = open(LEARNINGS_PATH, encoding="utf-8").read()
            t("AGENT_NAME filled, blank OWNER_NAME leaves {USER}", "TestAgent" in _txt and "{AGENT_NAME}" not in _txt)
        t("append lands between markers", append_to_learnings("first lesson", "trade_lesson", "test") and pending_entries() == 1)
        txt = open(LEARNINGS_PATH, encoding="utf-8").read()
        t("entry sits before end marker", txt.find("first lesson") < txt.find(END_MARKER))
        t("unknown type falls back + keeps original", append_to_learnings("x", "weird", "test")
          and "[orig_type: weird]" in open(LEARNINGS_PATH, encoding="utf-8").read())
        t("empty entry refused", append_to_learnings("   ", "trade_lesson", "test") is False)
        with open(os.path.join(REQUEST_DIR, "req1.json"), "w", encoding="utf-8") as f:
            json.dump({"entry": "from the agent", "type": "self_eval", "source": "agent"}, f)
        with open(os.path.join(REQUEST_DIR, "bad.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        r = run_once()
        t("request drained to done", r["requests"] == {"ok": 1, "failed": 1}
          and os.path.exists(os.path.join(REQUEST_DIR, "done", "req1.json"))
          and os.path.exists(os.path.join(REQUEST_DIR, "failed", "bad.json")))
        txt = open(LEARNINGS_PATH, encoding="utf-8").read().replace(END_MARKER, "")
        _atomic_write(LEARNINGS_PATH, txt)
        t("missing end marker restored, text kept", run_once()["markers_fixed"]
          and END_MARKER in open(LEARNINGS_PATH, encoding="utf-8").read()
          and "first lesson" in open(LEARNINGS_PATH, encoding="utf-8").read())
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write("999999")
        t("stale lock is taken over", True
          and (os.utime(LOCK_PATH, (time.time() - 120, time.time() - 120)) or True)
          and append_to_learnings("after stale lock", "bot_finding", "test"))
        t("heartbeat written", os.path.exists(HEARTBEAT))
        t("write log has no entry text", "first lesson" not in open(WRITE_LOG, encoding="utf-8").read())
    finally:
        _set_paths(real)
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASSED" if ok else "SELFTEST FAILED")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Safe LEARNINGS.md appends + Section 8 upkeep")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--append")
    ap.add_argument("--type", default="bot_finding")
    ap.add_argument("--source", default="agent")
    a = ap.parse_args()
    if a.selftest:
        return 0 if selftest() else 1
    if a.check:
        print(json.dumps(check(), indent=2))
        return 0
    if a.append is not None:
        return 0 if append_to_learnings(a.append, a.type, a.source) else 1
    if a.once:
        print(json.dumps(run_once(), indent=2))
        return 0
    _log(f"started, learnings file {os.path.relpath(LEARNINGS_PATH, WS)}, every {INTERVAL_S}s")
    while True:
        try:
            r = run_once()
            if r["requests"]["ok"] or r["requests"]["failed"] or r["markers_fixed"] or r["file"] != "exists":
                _log(f"pass: {r}")
        except Exception as e:
            _log(f"pass failed: {type(e).__name__}: {e}")
            _heartbeat("error", {"error": type(e).__name__})
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
