#!/usr/bin/env python3
"""perps_ui.py: Perps console (logo splash, menu, dashboard, logs, settings, skills, brain, owner commands).

    python perps_ui.py            open the console on its own (reads state files; never trades)
    python perps_ui.py --smoke    headless render test of every screen (no keys, no trades)

The engine can also host it in its own window (start_hosted()); any UI error is logged to
bot_logs/perps_ui_errors.log and the UI turns itself off while trading carries on.
No TTY or PERPS_UI=0 -> no UI.
Keys are not case-sensitive. Q / Esc / B = Back everywhere.
"""
from __future__ import annotations
import sys as _pyver_sys
if _pyver_sys.version_info < (3, 12):
    _pyver_sys.exit("Perps needs Python 3.12 or newer (this is %d.%d). Install 3.12+ from python.org, recreate the venv, then run again." % _pyver_sys.version_info[:2])

import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# ── paths (everything relative to this file; nothing outside the bot folder) ──
BOTS = Path(__file__).resolve().parent
ROOT = Path(os.environ["WORKSPACE_DIR"]).expanduser() if os.environ.get("WORKSPACE_DIR") else BOTS.parent
ENGINE_NAMES = ("perps_bot.py", "perps.py")
OWNER_INBOX_NAME = "owner_inbox"      # folder under ROOT the bot writes owner-facing notes to
AGENT_INBOX_NAME = "inbox"            # folder under ROOT the bot writes agent packets to


def _first_dir(*cands: Path) -> Path:
    for c in cands:
        if c.is_dir():
            return c
    return cands[0]


P = {
    "root": ROOT,
    "logs": _first_dir(ROOT / "bot_logs", BOTS / "bot_logs"),
    "relay": _first_dir(ROOT / "json_relay", BOTS / "json_relay"),
    "tuner": BOTS / "tuner",
    "env": BOTS / ".env",
    "skills": _first_dir(ROOT / "skills", BOTS / "skills"),
    "agent_inbox": ROOT / AGENT_INBOX_NAME,
    "owner_inbox": ROOT / OWNER_INBOX_NAME,
    "owner_cmds": ROOT / "owner_commands",
    "logo": next((p for p in (ROOT / "assets" / "perps_logo.png", BOTS / "assets" / "perps_logo.png")
                  if p.exists()), ROOT / "assets" / "perps_logo.png"),
}
P["backups"] = P["logs"] / "ui_backups"
P["changes"] = P["logs"] / "perps_ui_changes.jsonl"
P["skill_changes"] = P["logs"] / "perps_ui_skill_changes.jsonl"
P["errors"] = P["logs"] / "perps_ui_errors.log"
P["console"] = P["logs"] / "perps.console.log"

GREEN, PURPLE, BG = "#39FF14", "#8A2BE2", "#000000"
VERSION = "v1.0"
TAGLINE = "Long it, short it, let the agent manage it."
WORDMARK = [
    "\u2588\u2580\u2588 \u2588\u2580\u2580 \u2588\u2580\u2588 \u2588\u2580\u2588 \u2588\u2580\u2580",
    "\u2588\u2580\u2580 \u2588\u2580\u2580 \u2588\u2580\u2584 \u2588\u2580\u2580 \u2580\u2580\u2588",
    "\u2580   \u2580\u2580\u2580 \u2580 \u2580 \u2580   \u2580\u2580\u2580",
]
MENU = [("1", "Live Dashboard"), ("2", "Logs"), ("3", "Settings"), ("4", "Skills"),
        ("5", "Owner Commands"), ("6", "Brain Setup"),
        ("7", "Quit UI (Engine Keeps Trading)"), ("8", "Stop Perps")]
SKILL_DIRS = ("perps-boss", "pnl-tracker", "forensics", "sub-skills")
LLM_CHOICES = ("grok", "ollama", "off")
LLM_LABEL = {"grok": "Grok (Cloud)", "ollama": "Ollama (Local)", "off": "Off"}
# The *.console.log files are written by bots/perps.py (it starts the sub-bots); the rest are each sub-bot's own logs.
# Sub-bot log tabs (letter keys, same scheme as the Sniper UI): (key, label, file or (files...)) under bot_logs/.
SUBBOT_LOG_SOURCES = [
    ("w", "Warden", "security_warden.console.log"),
    ("a", "Autoagent", "agent_autoagent.console.log"),
    ("t", "Tuner", "code_tuner.console.log"),
    ("e", "Learnings", ("learnings_writer.console.log", "learnings_writer.jsonl")),
    ("c", "Owner Commands", ("owner_commands.console.log", "owner_commands.log")),
    ("k", "Acks", "agent_acks.jsonl"),
    ("l", "Launcher", "perps_launcher.log")]

STATE = {"active": False, "hosted": False, "errors": 0, "console": None}
RING = collections.deque(maxlen=3000)
LOCK = threading.Lock()
SCROLL: dict = {}


# ── small helpers ─────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _err(where: str) -> None:
    try:
        P["logs"].mkdir(parents=True, exist_ok=True)
        with open(P["errors"], "a", encoding="utf-8") as f:
            f.write(f"{_now_iso()} [{where}] {_redact(traceback.format_exc())}\n")
    except Exception:
        pass


_SECRET_RX = re.compile(r"(api[-_]?key=|token=|secret=)[^&\s]+|[1-9A-HJ-NP-Za-km-z]{60,}", re.I)


def _redact(s) -> str:
    return _SECRET_RX.sub(lambda m: (m.group(1) or "") + "<hidden>", str(s))


def _json(p: Path, default=None):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _tail(p: Path, n: int, max_bytes: int = 262144) -> list:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        return [_redact(x) for x in data.splitlines()[-n:]]
    except Exception:
        return []


def _jsonl(p: Path, n: int) -> list:
    out = []
    for ln in _tail(p, n, 1_048_576):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def _num(v, nd=2, pre="", suf="") -> str:
    try:
        return f"{pre}{float(v):,.{nd}f}{suf}"
    except Exception:
        return "-"


def _epoch(v) -> float:
    try:
        if isinstance(v, (int, float)):
            return float(v)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _age_txt(age: float) -> str:
    if age < 0 or age > 10 ** 8:
        return "never"
    if age < 120:
        return f"{int(age)}s ago"
    if age < 7200:
        return f"{int(age // 60)}m ago"
    return f"{age / 3600:.1f}h ago"


def _on(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def keys(*pairs) -> str:
    """Consistent key hint line: [Key] Action."""
    return "  ".join(f"[{k}] {v}" for k, v in pairs)


def _back(k: str) -> bool:
    return k in ("q", "esc", "b")


# ── env (values never printed) ───────────────────────────────────────────────
def read_env() -> dict:
    out = {}
    try:
        for ln in P["env"].read_text(encoding="utf-8-sig", errors="replace").splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = re.sub(r"\s+#.*$", "", v).strip().strip('"').strip("'")
    except Exception:
        pass
    return out


_SECRET_KEY_RX = re.compile(r"(PRIVATE|SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|_KEY$|^KEY$|SEED|MNEMONIC|COOKIE|JWT|RPC_URL)")


def is_secret(key: str) -> bool:
    return bool(_SECRET_KEY_RX.search(key.upper()))


def show_val(key: str, val) -> str:
    if is_secret(key):
        return "(Set)" if str(val or "").strip() else "(Not Set)"
    return str(val) if str(val or "").strip() != "" else "(Blank)"


ENV_ROWS = [  # (key, label) shown in Settings > Env; perps engine keys only
    ("PERPS_DRY_RUN", "Paper Mode / Dry Run (true/false; Restart Perps To Apply)"),
    ("PRIVATE_KEY", "Wallet Private Key"), ("RPC_URL", "Solana RPC URL"),
    ("GROK_API_KEY", "Grok API Key"), ("GROK_TEXT_MODEL", "Grok Text Model"),
    ("OLLAMA_MODEL", "Ollama Local Model"), ("OLLAMA_URL", "Ollama URL"),
    ("LLM_MAIN", "Main Brain"), ("LLM_FALLBACK", "Fallback Brain"),
    ("TELEGRAM_TOKEN", "Telegram Alert Bot Token"), ("TELEGRAM_CHAT_ID", "Telegram Chat ID"),
    ("PERP_RESIDUAL_DUST_USD", "Residual Dust (USD)"),
    ("PERP_MAX_LEVERAGE", "Max Leverage Cap (X)"), ("PERP_LEV_LADDER", "Leverage Ladder (SOL:X, Less Collateral = More Leverage)"),
    ("PERP_ONE_PER_MARKET", "One Position Per Market (true/false)"),
    ("PERP_LIQ_WARN_PCT", "Liq Warning Distance (%)"), ("PERP_SL_WARN_PCT", "SL Warning Distance (%)"),
    ("OWNER_POS_ENABLED", "Owner Positions Enabled"), ("OWNER_POS_TIMEOUT_MIN", "Owner Position Timeout (Min)"),
    ("OWNER_POS_LIQ_EARLY_FRAC", "Owner Liq Step-In Fraction"), ("OWNER_POS_SCAN_S", "Owner Scan Interval (S)"),
    ("OWNER_COMMANDS", "Owner Commands Mode"), ("OWNER_COMMANDS_TG_TOKEN", "Owner Commands Bot Token"),
    ("OWNER_COMMANDS_TG_CHAT_ID", "Owner Commands Chat ID"),
]


# ── backups, change log, stale-write guard ───────────────────────────────────
def sig(p: Path):
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size, hashlib.sha256(p.read_bytes()).hexdigest()[:16])
    except Exception:
        return None


class Stale(Exception):
    """File changed on disk since it was shown; nothing written."""


def guard(p: Path, expect) -> None:
    if expect is not None and sig(p) != expect:
        raise Stale(p.name)


def backup(p: Path) -> Path:
    P["backups"].mkdir(parents=True, exist_ok=True)
    dst = P["backups"] / f"{p.name}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
    if p.exists():
        shutil.copy2(p, dst)
    return dst


def change_log(rec: dict, path: Path | None = None) -> None:
    try:
        P["logs"].mkdir(parents=True, exist_ok=True)
        with open(path or P["changes"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _now_iso(), **rec}) + "\n")
    except Exception:
        _err("change log")


def atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def env_apply(key: str, new: str, expect) -> str:
    p = P["env"]
    guard(p, expect)
    lines = p.read_text(encoding="utf-8-sig", errors="replace").splitlines() if p.exists() else []
    done = False
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=", ln)
        if m and m.group(1) == key:
            lines[i] = f"{key}={new}"
            done = True
    if not done:
        lines.append(f"{key}={new}")
    b = backup(p)
    atomic_write(p, "\n".join(lines) + "\n")
    change_log({"file": ".env", "key": key, "secret": is_secret(key),
                "new": None if is_secret(key) else new, "backup": b.name})
    return f"Saved {key}. Backup: {b.name}"


# ── tuner (two-way: engine writes perps_params.json, UI writes saved defaults) ──
def tuner_params() -> dict:
    d = _json(P["tuner"] / "perps_params.json", {}) or {}
    return d.get("params") if isinstance(d.get("params"), dict) else {}


def tuner_saved() -> dict:
    d = _json(P["tuner"] / "perps_tuned_defaults.json", {}) or {}
    return d.get("defaults") if isinstance(d.get("defaults"), dict) else {}


def coerce(spec: dict, raw: str):
    t = spec.get("type")
    r = raw.strip()
    if t == "bool":
        if r.lower() in ("1", "true", "yes", "on", "y"):
            return True
        if r.lower() in ("0", "false", "no", "off", "n"):
            return False
        raise ValueError("Enter True Or False")
    if t == "int":
        v = int(float(r))
    elif t in ("float", "usd"):
        v = float(r)
    elif t == "list_str":
        return [x.strip() for x in r.split(",") if x.strip()]
    else:
        return r
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < lo:
        raise ValueError(f"Below Min {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"Above Max {hi}")
    return v


def tuner_apply(name: str, value, expect) -> str:
    p = P["tuner"] / "perps_tuned_defaults.json"
    guard(p, expect)
    raw = _json(p, {}) or {}
    raw = raw if isinstance(raw, dict) else {}
    raw.setdefault("defaults", {})
    old = raw["defaults"].get(name)
    raw["defaults"][name] = value
    raw["_updated"] = _now_iso()
    b = backup(p)
    atomic_write(p, json.dumps(raw, indent=2))
    try:
        with open(P["tuner"] / "tune history" / "perps_tune_history.json", "a", encoding="utf-8") as f:
            f.write(json.dumps({"action": "save_default", "param": name, "old": old, "new": value,
                                "reason": "perps_ui settings", "timestamp": _now_iso()}) + "\n")
    except Exception:
        pass
    change_log({"file": "perps_tuned_defaults.json", "key": name, "old": old, "new": value, "backup": b.name})
    return f"Saved {name} = {value}. Applies When The Engine Next Reloads. Backup: {b.name}"


# ── data for the dashboard ───────────────────────────────────────────────────
def engine_hb() -> dict:
    hb = _json(P["logs"] / "perps_bot_heartbeat.json", {}) or {}
    age = time.time() - _epoch(hb.get("timestamp")) if hb.get("timestamp") else 10 ** 9
    return {"status": hb.get("status", "missing"), "age": age, "alive": age < 180}


def wallet() -> dict:
    d = _json(P["logs"] / "wallet_balance.json", {}) or {}
    d = d if isinstance(d, dict) else {}
    ts = d.get("updated_at") or d.get("timestamp")
    d["age"] = time.time() - _epoch(ts) if ts else None
    return d


def paper_mode() -> bool:
    """PERPS_DRY_RUN from bots/.env (or the process env)."""
    v = os.environ.get("PERPS_DRY_RUN") or read_env().get("PERPS_DRY_RUN") or ""
    return str(v).strip().lower() in ("1", "true", "yes", "on", "paper")


def _book(name: str) -> str:
    """Paper mode reads the paper_* book, never the live one."""
    return ("paper_" + name) if paper_mode() else name


def positions() -> dict:
    d = _json(P["logs"] / _book("perp_positions.json"), {}) or {}
    return d if isinstance(d, dict) else {}


def owner_positions() -> dict:
    d = _json(P["logs"] / "owner_positions_perps.json", {}) or {}
    return d if isinstance(d, dict) else {}


def pnl() -> dict:
    d = _json(P["logs"] / _book("perp_pnl.json"), {}) or {}
    trades = [t for t in (d.get("trades") or []) if isinstance(t, dict)]
    now = time.time()

    def per(sec):
        rows = [t for t in trades if now - _epoch(t.get("timestamp")) <= sec]
        return sum(float(t.get("pnl_usd") or 0) for t in rows), len(rows)
    wins = sum(1 for t in trades if float(t.get("pnl_usd") or 0) > 0)
    return {"total": d.get("total_realized_usd"), "fees": d.get("total_fees_usd"),
            "today": per(86400), "week": per(7 * 86400), "n": len(trades),
            "win": (100.0 * wins / len(trades)) if trades else None, "last": trades[-6:]}


def liq_room(pos: dict):
    """Distance from entry to liquidation in % of entry (no network: entry-based)."""
    try:
        e, lq = float(pos.get("entry_price")), float(pos.get("liq_price"))
        return abs(e - lq) / e * 100.0
    except Exception:
        return None


def _collat(pos: dict):
    """Collateral (money put up) in USD: stored value, else position / leverage."""
    try:
        c = pos.get("collateral") or pos.get("collateral_usd")
        if c:
            return float(c)
        return float(pos.get("size_usd")) / float(pos.get("leverage"))
    except Exception:
        return None


# ── rendering ────────────────────────────────────────────────────────────────
def _rich():
    from rich import box
    from rich.align import Align
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    return box, Align, Group, Panel, Table, Text


_LOGO_CACHE: dict = {}


def logo_art(con, width: int = 0):
    if (con.color_system in (None, "standard", "windows")) or "utf" not in (con.encoding or "").lower():
        return None
    if not width:
        width = 40 if (con.size.width >= 90 and con.size.height >= 40) else 28
    if width in _LOGO_CACHE:
        return _LOGO_CACHE[width]
    try:
        from PIL import Image
        from rich.text import Text
        src = Image.open(P["logo"])
        if src.mode in ("RGBA", "LA", "P"):
            src = src.convert("RGBA")
            bg = Image.new("RGBA", src.size, (0, 0, 0, 255))
            bg.alpha_composite(src)
            src = bg
        im = src.convert("RGB")
        h = max(2, int(im.height * width / im.width / 2) * 2)
        im = im.resize((width, h), Image.LANCZOS)
        t = Text()
        for y in range(0, h, 2):
            for x in range(width):
                a, b = im.getpixel((x, y)), im.getpixel((x, y + 1))
                t.append("\u2580", style=f"rgb({a[0]},{a[1]},{a[2]}) on rgb({b[0]},{b[1]},{b[2]})")
            t.append("\n")
    except Exception:
        return None
    _LOGO_CACHE[width] = t
    return t


def banner(con=None, show_logo: bool = False):
    box, Align, Group, Panel, Table, Text = _rich()
    t = Text(justify="center")
    for i, ln in enumerate(WORDMARK):
        t.append(ln + "\n", style=f"bold {GREEN if i < 2 else PURPLE} on {BG}")
    mode = "Engine Running In This Window" if STATE["hosted"] else "Console (Reads State Files, Never Trades)"
    t.append(f"{VERSION}  {mode}\n", style=f"{PURPLE} on {BG}")
    t.append(TAGLINE, style=f"italic {GREEN} on {BG}")
    art = logo_art(con) if (show_logo and con is not None) else None
    body = Group(Align.center(art), Align.center(t)) if art is not None else Align.center(t)
    return Panel(body, style=f"on {BG}", border_style=PURPLE, box=box.HEAVY)


def menu_panel():
    box, Align, Group, Panel, Table, Text = _rich()
    t = Table.grid(padding=(0, 2))
    hb = engine_hb()
    for k, v in MENU:
        row = Text(v, style="white")
        if v == "Brain Setup":
            m, f = brain_choice(read_env())
            row.append(f"   Main: {LLM_LABEL.get(m, m)}  Fallback: {LLM_LABEL.get(f, f)}", style="dim")
        t.add_row(Text(k, style=f"bold {GREEN}"), row)
    eng = Text(f"Engine: {'Alive' if hb['alive'] else 'Not Running'} ({_age_txt(hb['age'])})",
               style=GREEN if hb["alive"] else "red")
    if paper_mode():
        eng.append("   PAPER MODE (no real orders)", style="bold yellow")
    last = RING[-1][:100] if RING else ""
    sub = Text.assemble(eng, ("   " + last, "dim"))
    return Panel(t, title="Menu   " + keys(("1-8", "Open"), SCROLL_HINT), subtitle=sub,
                 border_style=GREEN, style=f"on {BG}", box=box.ROUNDED)


def dashboard_view():
    box, Align, Group, Panel, Table, Text = _rich()
    hb = engine_hb()
    head = Text.assemble(("Engine ", "bold"), ("Alive" if hb["alive"] else "Not Running", GREEN if hb["alive"] else "red"),
                         (f"  Heartbeat {_age_txt(hb['age'])}   ", "dim"),
                         ("New Positions ", "bold"),
                         (str((tuner_params().get("NEW_POSITIONS_ENABLED") or {}).get("effective", "-")), PURPLE),
                         ("   Entry Alerts ", "bold"),
                         (str((tuner_params().get("ENTRY_ALERTS_ENABLED") or {}).get("effective", "-")), PURPLE),
                         ("   Wallet ", "bold"),
                         (_num(wallet().get("balance_sol"), 4, suf=" SOL") + "  " + _num(wallet().get("balance_usd"), 2, "$"), GREEN),
                         (f"  ({_age_txt(wallet()['age'])})" if wallet().get("age") is not None else "  (No Stamp Yet)",
                          "yellow" if (wallet().get("age") or 0) > 3600 else "dim"))
    pt = Table(title="Open Positions", box=box.SIMPLE_HEAVY, expand=True, header_style=f"bold {GREEN}")
    for c in ("Market", "Side", "Entry", "Position (USD)", "Collateral", "Leverage", "Liq Price", "Liq Dist (From Entry)", "SL", "TP", "Opened"):
        pt.add_column(c, no_wrap=True)
    pos = positions()
    for mk, p in pos.items():
        if not isinstance(p, dict):
            continue
        room = liq_room(p)
        rs = "red" if (room is not None and room < 5) else ("yellow" if (room is not None and room < 10) else GREEN)
        pt.add_row(str(p.get("market") or mk), str(p.get("direction", "-")).title(), _num(p.get("entry_price"), 4),
                   _num(p.get("size_usd"), 2, "$"), _num(_collat(p), 2, "$"), _num(p.get("leverage"), 1, suf="x"), _num(p.get("liq_price"), 4),
                   Text(_num(room, 2, suf="%"), style=rs), _num(p.get("sl_price"), 4), _num(p.get("tp_price"), 4),
                   _age_txt(time.time() - _epoch(p.get("timestamp"))))
    if not pos:
        pt.add_row("None", *[""] * 10)
    s = pnl()
    pn = Table.grid(padding=(0, 3))

    def c(v):
        try:
            return GREEN if float(v) >= 0 else "red"
        except Exception:
            return "white"
    pn.add_row(Text("Realized Total", style="bold"), Text(_num(s["total"], 2, "$"), style=c(s["total"])),
               Text("Fees", style="bold"), Text(_num(s["fees"], 2, "$")),
               Text("Win Rate", style="bold"), Text(_num(s["win"], 1, suf="%") + f" of {s['n']}"))
    pn.add_row(Text("Today", style="bold"), Text(_num(s["today"][0], 2, "$") + f" ({s['today'][1]})", style=c(s["today"][0])),
               Text("7 Days", style="bold"), Text(_num(s["week"][0], 2, "$") + f" ({s['week'][1]})", style=c(s["week"][0])),
               Text(""), Text(""))
    lt = Table(title="Recent Closes", box=box.SIMPLE, expand=True, header_style=f"bold {PURPLE}")
    for col in ("When", "Market", "Side", "Lev", "PnL USD", "PnL % (Net, Of Collateral)"):
        lt.add_column(col, no_wrap=True)
    lt.add_column("Reason", no_wrap=True, overflow="ellipsis", ratio=1)
    for t in reversed(s["last"]):
        lt.add_row(str(t.get("timestamp", ""))[5:16].replace("T", " "), str(t.get("market", "")),
                   str(t.get("direction", "")).title(), _num(t.get("leverage"), 1, suf="x"),
                   Text(_num(t.get("pnl_usd"), 2, "$"), style=c(t.get("pnl_usd"))), _num(t.get("pnl_pct"), 2, suf="%"),
                   str(t.get("reason") or t.get("category") or "")[:40])
    ot = Table(title="Owner Positions (Opened Outside The Bot)", box=box.SIMPLE, expand=True,
               header_style=f"bold {PURPLE}")
    for col in ("Market", "Status", "Detail"):
        ot.add_column(col)
    op = owner_positions()
    rows = op.get("positions") if isinstance(op.get("positions"), dict) else op
    for mk, v in (rows or {}).items():
        if isinstance(v, dict):
            ot.add_row(str(mk), str(v.get("status") or v.get("decision") or "pending").title(),
                       str(v.get("note") or v.get("reason") or "")[:60])
    if not ot.row_count:
        ot.add_row("None", "", "")
    tp = tuner_params()
    tt = Table(title="Tuner (Changed From Code Default)", box=box.SIMPLE, expand=True, header_style=f"bold {PURPLE}")
    for col in ("Param", "Effective", "Code Default"):
        tt.add_column(col)
    for name, spec in tp.items():
        if isinstance(spec, dict) and spec.get("effective") != spec.get("code_default"):
            tt.add_row(name, str(spec.get("effective")), str(spec.get("code_default")))
    if not tt.row_count:
        tt.add_row("All At Code Default", "", "")
    return Panel(Group(head, pt, Panel(pn, title="PnL", border_style=PURPLE), lt, ot, tt),
                 title="Live Dashboard   " + keys(("A", "Archives"), ("R", "Reset PnL"), SCROLL_HINT, ("Q", "Back")),
                 border_style=GREEN, style=f"on {BG}")


# ── logs ─────────────────────────────────────────────────────────────────────
def log_sources() -> list:
    """Tabs mirror the Sniper UI: digits for core logs, letters for sub-bots, shown as [K] Name."""
    L = P["logs"]
    src = [("1", "Perps Console", "ring"),
           ("2", "Trades", L / _book("perp_pnl.json")),
           ("3", "Declines", L / "perps_decline_log.jsonl"),
           ("4", "Owner Positions", L / "owner_positions.jsonl"),
           ("5", "Warden Alerts", (L / "security_warden.json", L / "medic_log.json", L / "integrity_log.json")),
           ("6", "Owner Inbox", P["owner_inbox"]),
           ("i", "Agent Inbox", P["agent_inbox"]),
           ("r", "JSON Relay", ("relay", P["relay"])),
           ("f", "Feeds", ("feeds", None)),
           ]
    for k, lbl, name in SUBBOT_LOG_SOURCES:
        src.append((k, lbl, tuple(L / x for x in name) if isinstance(name, tuple) else L / name))
    return src


LOG_TAB_HELP = "1-6 I R F W A T E C K L"
LOG_HISTORY = 5000          # lines kept per tab (same as the Sniper UI)
LOGV: dict = {}             # tab -> {"off": lines up from the bottom, "q": search, "hit": line index}
LOGCACHE: dict = {}         # tab -> (time, lines), refreshed every ~2s
LOG_EMPTY = {
    "perp_pnl.json": "no trades yet, waiting for first trade",
    "paper_perp_pnl.json": "no paper trades yet",
    "perps_decline_log.jsonl": "no declines yet",
    "owner_positions.jsonl": "no owner positions logged yet",
    "security_warden.json": "no warden findings yet",
    "medic_log.json": "no medic actions yet",
    "integrity_log.json": "no integrity events yet (nothing changed or deleted)",
    "perps_launcher.log": "launcher has not logged anything yet",
}


def log_empty_msg(src: Path) -> str:
    n = src.name
    if n in LOG_EMPTY:
        return f"({LOG_EMPTY[n]})"
    if n.endswith(".console.log"):
        return f"({n[:-12]} has not written output yet; it may not be running)"
    return f"({n}: nothing written yet)"


def _dir_lines(d: Path) -> list:
    if not d.is_dir():
        return [f"(Folder Not Found: {d.name})"]
    files = sorted((f for f in d.rglob("*") if f.is_file() and f.name != ".gitkeep"),
                   key=lambda f: f.stat().st_mtime)
    out = []
    for f in files[-200:]:
        out.append(f"{datetime.fromtimestamp(f.stat().st_mtime):%m-%d %H:%M}  {f.relative_to(d)}  ({f.stat().st_size} B)")
        if f.suffix.lower() in (".json", ".md", ".txt"):
            for ln in _tail(f, 3, 4096):
                out.append("    " + ln[:160])
    return out or ["(Empty)"]


# JSON Relay filter: only files that belong to Perps. A relay file counts when
#   (a) its name starts with "perp" (perp_signal*.json, perp_query, perp_meeting, ...) or is
#       market_mode.json (the market mode file the engine reads), or
#   (b) its JSON names Perps in a routing field: from / to / bot / source / target / lane /
#       type contains "perp", or a market field ends in "-PERP".
_RELAY_NAMES = ("market_mode.json",)
_RELAY_FIELDS = ("from", "to", "bot", "source", "target", "lane", "type")


def is_perps_relay(f: Path) -> bool:
    n = f.name.lower()
    if n.startswith("perp") or n in _RELAY_NAMES:
        return True
    d = _json(f, None)
    items = d if isinstance(d, list) else [d]
    for it in items[:50]:
        if not isinstance(it, dict):
            continue
        if any("perp" in str(it.get(k) or "").lower() for k in _RELAY_FIELDS):
            return True
        if str(it.get("market") or "").upper().endswith("-PERP"):
            return True
    return False


def relay_lines(d: Path) -> list:
    if not d.is_dir():
        return [f"(Folder Not Found: {d.name})"]
    allf = [f for f in d.rglob("*.json") if f.is_file()]
    files = sorted((f for f in allf if is_perps_relay(f)), key=lambda f: f.stat().st_mtime, reverse=True)
    out = [f"{len(files)} Perps Relay File(s), Newest First  (Filtered From {len(allf)} In json_relay/: "
           "Name Starts With perp, market_mode.json, Or A from/to/bot/source/target/lane/type Field Says Perps)",
           "-" * 40]
    if not files:
        return out + ["(Nothing For Perps In Flight)"]
    for f in files[:200]:
        out.append(f"{datetime.fromtimestamp(f.stat().st_mtime):%m-%d %H:%M}  {f.relative_to(d)}  ({f.stat().st_size} B)")
    out += ["-" * 40, f"== {files[0].name} (Newest) =="]
    try:
        out += [_redact(x)[:220] for x in json.dumps(_json(files[0], {}), indent=1, default=str).splitlines()]
    except Exception:
        pass
    return out


def feed_lines() -> list:
    """Feeds status per lane (source / age / stale), from disk only, no network."""
    try:
        import sys as _sys
        if str(BOTS) not in _sys.path:
            _sys.path.insert(0, str(BOTS))
        os.environ.setdefault("WORKSPACE_DIR", str(ROOT))
        from feeds import feed_router as _fr
        st = _fr.status()
    except Exception as e:
        return [f"(Feeds Unavailable: {_redact(e)})"]
    out = [f"Cadence: {st.get('cadence')}   (manual = on demand; odds are fetched live at every packet)", ""]
    out.append(f"{'Lane':<6} {'Source':<26} {'Age (min)':>10}  {'Stale':<6} {'Mode':<20} Fetched")
    for lane in ("flow", "news", "odds"):
        r = st.get(lane) or {}
        age = r.get("age_min")
        out.append(f"{lane:<6} {str(r.get('source') or '-'):<26} {('-' if age is None else f'{age:.1f}'):>10}  "
                   f"{('-' if r.get('stale') is None else ('YES' if r.get('stale') else 'no')):<6} "
                   f"{str(r.get('active') or '-'):<20} {str(r.get('fetched_at') or '-')[:19]}")
    out += ["", "Pull on demand: write json_relay/feed_request.json, or query action pull_feeds / feed_packet."]
    return out


def log_lines(src, n: int = LOG_HISTORY) -> list:
    if isinstance(src, tuple) and src and src[0] == "relay":
        return relay_lines(Path(src[1]))[:n]
    if isinstance(src, tuple) and src and src[0] == "feeds":
        return feed_lines()[:n]
    if src == "ring":
        with LOCK:
            r = list(RING)
        return r[-n:] if r else (_tail(P["console"], n, 4_194_304) or _tail(P["logs"] / "perps_stdout.log", n, 4_194_304)
                                  or ["(no engine output yet)"])
    if isinstance(src, (tuple, list)):          # several files under one tab, newest entries last
        out = []
        for p in src:
            out += [f"== {Path(p).name} =="] + log_lines(p, max(5, n // len(src)))
        return out
    src = Path(src)
    if src.is_dir():
        return _dir_lines(src)
    if not src.exists():
        return [log_empty_msg(src)]
    if src.name in ("perp_pnl.json", "paper_perp_pnl.json"):
        out = []
        for t in (_json(src, {}) or {}).get("trades", [])[-n:]:
            out.append(f"{str(t.get('timestamp',''))[:16]}  {t.get('market','')} {str(t.get('direction','')).upper()} "
                       f"{_num(t.get('leverage'),1,suf='x')}  {_num(t.get('pnl_usd'),2,'$')}  {t.get('reason','')}")
        return out or [log_empty_msg(src)]
    if src.suffix == ".json":
        d = _json(src, None)
        if isinstance(d, dict):
            for k in ("alerts", "items", "history", "entries", "findings", "events"):
                if isinstance(d.get(k), list):
                    d = d[k]; break
        items = d[-n:] if isinstance(d, list) else (list(d.items())[-n:] if isinstance(d, dict) else [])
        return [_redact(json.dumps(i, ensure_ascii=False, default=str)) for i in items] or [log_empty_msg(src)]
    return _tail(src, n, 4_194_304) or [log_empty_msg(src)]


def log_cached(sel: str, src) -> list:
    """Last LOG_HISTORY lines of the CURRENT tab only (lazy), refreshed every ~2s."""
    e = LOGCACHE.get(sel)
    if not e or time.time() - e[0] > 2:
        try:
            lines = log_lines(src, LOG_HISTORY)
        except Exception:
            lines = ["(unreadable)"]
        e = LOGCACHE[sel] = (time.time(), lines)
    return e[1]


def log_find(sel: str, forward: bool) -> None:
    v = LOGV.setdefault(sel, {"off": 0, "q": "", "hit": None})
    if not v["q"]:
        return
    lines = LOGCACHE.get(sel, (0, []))[1]
    total = len(lines)
    q = v["q"].lower()
    cur = v["hit"] if v["hit"] is not None else (total - v["off"])
    rng = range(cur + 1, total) if forward else range(cur - 1, -1, -1)
    for i in rng:
        if q in lines[i].lower():
            v["hit"] = i
            v["off"] = max(0, total - i - 5)      # bring it into view
            return


def logs_view(sel: str, height: int, width: int = 160):
    """Same layout as the Sniper UI: tab panel (help in subtitle), body, status line with LIVE/PAUSED."""
    box, Align, Group, Panel, Table, Text = _rich()
    srcs = log_sources()
    tabs = Text()
    for k, name, _ in srcs:
        tabs.append(f" [{k.upper()}] {name} ", style=f"bold black on {GREEN}" if k == sel else GREEN)
    src = next(s for k, _, s in srcs if k == sel)
    lines = log_cached(sel, src)
    v = LOGV.setdefault(sel, {"off": 0, "q": "", "hit": None})
    n = max(5, height - 8)
    total = len(lines)
    v["off"] = max(0, min(v["off"], max(0, total - n)))
    end = total - v["off"]
    start = max(0, end - n)
    body = Text()
    w = max(20, width - 6)
    q = v["q"].lower()
    for i in range(start, end):
        ln = lines[i][:w]
        style = "black on yellow" if v["hit"] == i else ("bold" if q and q in ln.lower() else "")
        body.append(ln + "\n", style=style)
    status = Text()
    status.append(f" lines {start + 1 if total else 0}-{end} of {total} ", style="dim")
    status.append(" PAUSED " if v["off"] else " LIVE ", style="bold black on yellow" if v["off"] else f"bold black on {GREEN}")
    if v["q"]:
        status.append(f"  /{v['q']}", style="yellow")
    help_ = keys((LOG_TAB_HELP, "Tab"), ("\u2191\u2193", "Line"), ("PgUp/PgDn", "Page"), ("Home/End", "Top / Follow"),
                 ("/", "Search"), ("N", "Next Hit"), ("P", "Prev Hit"), ("Q", "Back"))
    return Group(Panel(tabs, title="Logs", subtitle=Text(help_, style="dim"), border_style=GREEN, style=f"on {BG}"), body, status)


def log_scroll(sel: str, k: str, page: int = 10) -> bool:
    """Sniper key semantics: Up/PgUp go back in time, Home = top (oldest), End = follow (LIVE)."""
    v = LOGV.setdefault(sel, {"off": 0, "q": "", "hit": None})
    if k == "up":
        v["off"] += 1
    elif k == "down":
        v["off"] = max(0, v["off"] - 1)
    elif k == "pgup":
        v["off"] += page
    elif k == "pgdn":
        v["off"] = max(0, v["off"] - page)
    elif k == "home":
        v["off"] = 10 ** 9
    elif k == "end":
        v["off"] = 0; v["hit"] = None
    elif k == "n":
        log_find(sel, forward=True)
    elif k == "p":
        log_find(sel, forward=False)
    else:
        return False
    return True


# ── keys ─────────────────────────────────────────────────────────────────────
def getkey(timeout: float) -> str:
    """One key, lower-cased, or '' after timeout. Arrows -> up/down/pgup/pgdn/home/end; Esc -> esc."""
    if os.name == "nt":
        import msvcrt
        end = time.time() + timeout
        while time.time() < end:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    return {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn", "G": "home", "O": "end"}.get(msvcrt.getwch(), "")
                if ch == "\x03":
                    raise KeyboardInterrupt
                return "esc" if ch == "\x1b" else ("enter" if ch in ("\r", "\n") else ch.lower())
            time.sleep(0.05)
        return ""
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if not select.select([sys.stdin], [], [], timeout)[0]:
            return ""
        ch = os.read(fd, 1).decode("utf-8", "replace")
        if ch == "\x1b":
            seq = ""
            while select.select([sys.stdin], [], [], 0.03)[0] and len(seq) < 6:
                seq += os.read(fd, 1).decode("utf-8", "replace")
            return {"[A": "up", "[B": "down", "[5~": "pgup", "[6~": "pgdn", "[H": "home", "[1~": "home",
                    "[F": "end", "[4~": "end"}.get(seq, "esc" if not seq else "")
        return "enter" if ch in ("\r", "\n") else ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def ask(con, prompt: str, default: str = "") -> str | None:
    """Line input. Q / Esc / Back = cancel (None)."""
    from rich.prompt import Prompt
    try:
        v = Prompt.ask(f"{prompt} [dim](Q = Back)[/]", default=default, show_default=bool(default), console=con)
    except (EOFError, KeyboardInterrupt):
        return None
    return None if str(v).strip().lower() in ("q", "back", "esc", "\x1b") else v


def pause(con, msg: str = "") -> None:
    if msg:
        con.print(msg)
    con.print(f"[dim]{keys(('Any Key', 'Continue'))}[/]")
    getkey(60.0)


# ── shared scrolling (every screen) ──────────────────────────────────────────
SCROLL_KEYS = ("up", "down", "pgup", "pgdn", "home", "end")
SCROLL_HINT = ("\u2191\u2193 PgUp/PgDn Home/End", "Scroll")
VIEW: dict = {}   # view key -> {"off": top row, "n": rows, "page": visible rows}


class Scroll:
    """Render `inner` at full height, show the window that fits the CURRENT terminal
    (size read every frame, so a resize just works) plus a one-line position hint.
    Top-anchored: a refreshing view keeps its place. Render faults are logged, never raised."""

    def __init__(self, key: str, inner):
        self.key, self.inner = key, inner

    def __rich_console__(self, console, options):
        from rich.segment import Segment
        from rich.style import Style
        st = VIEW.setdefault(self.key, {"off": 0, "n": 0, "page": 10})
        H = max(3, options.height or console.size.height)
        try:
            inner = self.inner() if callable(self.inner) else self.inner
            lines = console.render_lines(inner, options.update(height=None), pad=False)
        except Exception:
            _err(f"scroll render {self.key}")
            lines = [[Segment("(Render Error, Logged; Trading Unaffected)")]]
        n = len(lines)
        if n <= H:
            st.update(off=0, n=n, page=H)
            for i, ln in enumerate(lines):
                yield from ln
                if i < n - 1:
                    yield Segment.line()
            return
        page = H - 1
        st["off"] = max(0, min(int(st.get("off") or 0), n - page))
        st["n"], st["page"] = n, page
        a, b = st["off"], min(n, st["off"] + page)
        for ln in lines[a:b]:
            yield from ln
            yield Segment.line()
        txt = (f" Rows {a + 1}-{b} Of {n}   " + keys(SCROLL_HINT)
               + ("   \u25bc More" if b < n else "") + ("   \u25b2 More" if a > 0 else ""))
        yield Segment(txt[: max(1, options.max_width - 1)], Style(color="black", bgcolor=GREEN))


def scroll_key(key: str, k: str) -> bool:
    """Up/Down = line, PgUp/PgDn = page, Home/End = top/bottom. True if consumed."""
    if k not in SCROLL_KEYS:
        return False
    st = VIEW.setdefault(key, {"off": 0, "n": 0, "page": 10})
    pg = max(1, int(st.get("page") or 10) - 1)
    off = int(st.get("off") or 0)
    off = {"up": off - 1, "down": off + 1, "pgup": off - pg, "pgdn": off + pg, "home": 0, "end": 10 ** 9}[k]
    st["off"] = max(0, min(off, max(0, int(st.get("n") or 0) - int(st.get("page") or 10))))
    return True


def live_loop(con, render, on_key=None, vkey: str | None = None, own=()):
    """vkey: wrap the view in Scroll and give it Up/Down/PgUp/PgDn/Home/End (keys listed in
    `own` go to on_key instead, for screens that move a selection or scroll themselves)."""
    from rich.live import Live
    key = vkey or "view"
    VIEW[key] = {"off": 0, "n": 0, "page": 10}
    frame = lambda: Scroll(key, render)
    with Live(frame(), console=con, refresh_per_second=4, screen=True,
              redirect_stdout=False, redirect_stderr=False) as live:
        while STATE["active"]:
            k = getkey(0.5)
            if _back(k):
                return k
            if k in SCROLL_KEYS and k not in own:
                scroll_key(key, k)
            elif k and on_key and on_key(k) == "exit":
                return k
            live.update(frame())


# ── screens ──────────────────────────────────────────────────────────────────
def screen_dashboard(con) -> None:
    while STATE["active"]:
        k = live_loop(con, dashboard_view, lambda k: "exit" if k in ("a", "r") else None, vkey="dash")
        if k == "a":
            screen_pnl_archives(con)
        elif k == "r":
            screen_pnl_reset(con)
        else:
            return


# ── PnL reset ────────────────────────────────────────────────────────────────
# The old ledger is COPIED to perp_pnl.json.archive_<stamp>.json beside the original,
# also backed up to ui_backups/, then the board starts fresh (trades [], totals
# recomputed to 0). Every reset is appended to scoreboard_resets.json with lane
# "perps". Nothing is ever deleted.
def _pnl_path() -> Path:
    return P["logs"] / _book("perp_pnl.json")


def pnl_reset(note: str = "", expect=None) -> dict:
    p = _pnl_path()
    if not p.exists():
        raise FileNotFoundError("No perp_pnl.json Yet; Nothing To Reset")
    guard(p, expect)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = p.with_name(f"{p.name}.archive_{stamp}.json")
    shutil.copy2(p, archive)
    b = backup(p)
    d = _json(p, {}) or {}
    d = d if isinstance(d, dict) else {}
    old = [t for t in (d.get("trades") or []) if isinstance(t, dict)]
    payload = {k: v for k, v in d.items() if k != "trades"}
    payload.update({"trades": [], "total_realized_usd": 0.0, "total_fees_usd": 0.0,
                    "_reset_at": datetime.now().isoformat(), "_archived_to": str(archive)})
    guard(p, sig(p) if expect is None else expect)
    atomic_write(p, json.dumps(payload, indent=2, default=str))
    rec = {"lane": "perps", "archived_to": str(archive), "at": datetime.now().isoformat(),
           "note": note or "perps dashboard reset", "scrubbed_before": None, "via": "perps_ui",
           "trades_archived": len(old), "net_usd_archived": round(sum(float(t.get("pnl_usd") or 0) for t in old), 4)}
    rl = P["logs"] / "scoreboard_resets.json"
    log = _json(rl, {"resets": []}) or {"resets": []}
    log = log if isinstance(log, dict) else {"resets": []}
    log.setdefault("resets", []).append(rec)
    atomic_write(rl, json.dumps(log, indent=2, default=str))
    change_log({"file": p.name, "key": "pnl_reset", "archive": archive.name, "backup": b.name,
                "trades_archived": len(old)})
    return rec


def pnl_archive_rows() -> list:
    log = _json(P["logs"] / "scoreboard_resets.json", {}) or {}
    rows = [r for r in (log.get("resets") or []) if isinstance(r, dict) and r.get("lane") == "perps"]
    return rows[::-1]


def archives_view():
    box, Align, Group, Panel, Table, Text = _rich()
    t = Table(box=box.SIMPLE, expand=True, header_style=f"bold {GREEN}")
    for c in ("Reset At", "Trades", "Net USD", "Archive File", "Note"):
        t.add_column(c)
    for r in pnl_archive_rows():
        n, net = r.get("trades_archived"), r.get("net_usd_archived")
        if n is None:
            a = _json(Path(str(r.get("archived_to") or "")), {}) or {}
            tr = [x for x in (a.get("trades") or []) if isinstance(x, dict)] if isinstance(a, dict) else []
            n, net = len(tr), sum(float(x.get("pnl_usd") or 0) for x in tr)
        t.add_row(str(r.get("at", ""))[:16].replace("T", " "), str(n), _num(net, 2, "$"),
                  Path(str(r.get("archived_to") or "")).name, str(r.get("note") or "")[:40])
    if not t.row_count:
        t.add_row("No Perps Resets Yet", "", "", "", "")
    return Panel(t, title="PnL Archives (Past Resets)   " + keys(SCROLL_HINT, ("Q", "Back")),
                 border_style=GREEN, style=f"on {BG}")


def screen_pnl_archives(con) -> None:
    live_loop(con, archives_view, vkey="archives")


def screen_pnl_reset(con) -> None:
    p = _pnl_path()
    expect = sig(p)
    s = pnl()
    con.clear()
    con.print(f"[bold]Reset Perps PnL[/]   Board Now: {_num(s['total'], 2, '$')} Realized Over {s['n']} Closed Trades")
    con.print("[dim]The Ledger Is Archived Beside The Original (perp_pnl.json.archive_<time>.json) And Backed Up; "
              "Nothing Is Deleted. The Board Starts Fresh. Logged In scoreboard_resets.json.[/]")
    con.print("[yellow]Archive And Reset The Perps Board?  [Y] Yes  [N] No[/]")
    if getkey(60.0) != "y":
        pause(con, "Nothing Changed.")
        return
    note = ask(con, "Note (Optional)", "") or ""
    try:
        r = pnl_reset(note, expect)
        pause(con, f"[{GREEN}]Reset Done. Archived {r['trades_archived']} Trades To {Path(r['archived_to']).name}.[/]")
    except Stale:
        pause(con, "[yellow]The Ledger Changed While You Were Deciding (A Trade Closed?). Nothing Changed; Try Again.[/]")
    except Exception as e:
        pause(con, f"[red]Not Reset: {_redact(e)}[/]")


def screen_logs(con) -> None:
    from rich.prompt import Prompt
    cur = {"sel": "1"}

    def on_key(k):
        if k in [s[0] for s in log_sources()]:
            cur["sel"] = k
        elif k == "/":
            return "exit"
        else:
            log_scroll(cur["sel"], k, max(5, con.size.height - 9))
    while STATE["active"]:
        k = live_loop(con, lambda: logs_view(cur["sel"], con.size.height, con.size.width), on_key,
                      vkey="logs", own=SCROLL_KEYS)
        if k == "/":
            v = LOGV.setdefault(cur["sel"], {"off": 0, "q": "", "hit": None})
            try:
                v["q"] = Prompt.ask("search (blank clears)", default="", console=con).strip()
            except (EOFError, KeyboardInterrupt):
                v["q"] = ""
            v["hit"] = None
            if v["q"]:
                log_find(cur["sel"], forward=False)
        else:
            return


def settings_rows() -> list:
    """[(id, section, key, label, shown value, meta)] fresh from disk every call (two-way sync)."""
    rows = []
    for name, spec in tuner_params().items():
        if isinstance(spec, dict):
            rng = f"{spec.get('min')}..{spec.get('max')}" if spec.get("min") is not None else spec.get("type", "")
            rows.append(("tuner", name, name.replace("_", " ").title(), str(spec.get("effective")), spec, rng))
    env = read_env()
    for k, lbl in ENV_ROWS:
        rows.append(("env", k, lbl, show_val(k, env.get(k, "")), None, "Secret" if is_secret(k) else ""))
    return rows


def settings_view(sel: int, height: int):
    box, Align, Group, Panel, Table, Text = _rich()
    rows = settings_rows()
    t = Table(box=box.SIMPLE, expand=True, header_style=f"bold {GREEN}")
    for c in ("#", "Section", "Setting", "Value", "Range / Note"):
        t.add_column(c)
    page = max(5, height - 9)
    top = max(0, min(sel - page // 2, max(0, len(rows) - page)))
    for i, (sec, key, lbl, val, meta, note) in enumerate(rows[top:top + page], start=top):
        st = f"bold black on {GREEN}" if i == sel else ""
        t.add_row(str(i + 1), "Tuner" if sec == "tuner" else "Env", lbl, val, str(note), style=st)
    tail = Text(f"Tuner Edits Save A Default (perps_tuned_defaults.json); Engine Values Re-Read Every Screen. "
                f"Every Write Is Backed Up And Logged; A File Changed Elsewhere Is Never Overwritten.", style="dim")
    return Panel(Group(t, tail), title="Settings   " + keys(("\u2191\u2193 PgUp/PgDn Home/End", "Move"), ("Enter / E", "Edit"),
                                                             ("Q", "Back")), border_style=GREEN, style=f"on {BG}")


def screen_settings(con) -> None:
    cur = {"sel": 0}
    while STATE["active"]:
        def on_key(k):
            n = len(settings_rows())
            if k in ("up", "down", "pgup", "pgdn", "home", "end"):
                cur["sel"] = max(0, min(n - 1, cur["sel"] + {"up": -1, "down": 1, "pgup": -10, "pgdn": 10,
                                                              "home": -10 ** 6, "end": 10 ** 6}[k]))
            elif k in ("enter", "e"):
                return "exit"
        k = live_loop(con, lambda: settings_view(cur["sel"], con.size.height), on_key, vkey="settings", own=SCROLL_KEYS)
        if k not in ("enter", "e"):
            return
        rows = settings_rows()
        sec, key, lbl, val, meta, note = rows[cur["sel"]]
        con.clear()
        if sec == "tuner":
            p = P["tuner"] / "perps_tuned_defaults.json"
            expect = sig(p)
            con.print(f"[bold]{lbl}[/]  ({key})\n{(meta or {}).get('description', '')}\n"
                      f"Now: {val}   Code Default: {(meta or {}).get('code_default')}   Range: {note}")
            raw = ask(con, "New Value")
            if raw is None:
                continue
            try:
                pause(con, f"[{GREEN}]{tuner_apply(key, coerce(meta, raw), expect)}[/]")
            except Stale:
                pause(con, "[yellow]That File Changed On Disk Since You Opened It. Nothing Saved; Reloaded.[/]")
            except ValueError as e:
                pause(con, f"[red]Not Saved: {e}[/]")
        else:
            expect = sig(P["env"])
            con.print(f"[bold]{lbl}[/]  ({key})   Now: {val}")
            if is_secret(key):
                from rich.prompt import Prompt
                try:
                    raw = Prompt.ask("New Value (Hidden; Q = Back)", password=True, console=con)
                except (EOFError, KeyboardInterrupt):
                    raw = None
                raw = None if raw is None or raw.strip().lower() in ("q", "back") else raw
            else:
                raw = ask(con, "New Value")
            if raw is None:
                continue
            try:
                pause(con, f"[{GREEN}]{env_apply(key, raw.strip(), expect)}[/] [dim]Env Changes Apply On Next Engine Start.[/]")
            except Stale:
                pause(con, "[yellow].env Changed On Disk Since You Opened It. Nothing Saved; Reloaded.[/]")


def skill_files() -> list:
    out = []
    for d in SKILL_DIRS:
        base = P["skills"] / d
        if base.is_dir():
            out += sorted(base.rglob("SKILL.md"))
    return out


def skill_save(f: Path, text: str, how: str, expect) -> str:
    guard(f, expect)
    b = backup(f)
    atomic_write(f, text)
    change_log({"skill": str(f.relative_to(P["skills"])), "how": how, "backup": b.name}, P["skill_changes"])
    return f"Saved. Backup: {b.name}"


def skills_view(sel: int, height: int = 50):
    box, Align, Group, Panel, Table, Text = _rich()
    t = Table(box=box.SIMPLE, expand=True, header_style=f"bold {GREEN}")
    for c in ("#", "Skill", "Size", "Modified"):
        t.add_column(c)
    files = skill_files()
    page = max(3, height - 7)
    top = max(0, min(sel - page // 2, max(0, len(files) - page)))
    for i, f in enumerate(files[top:top + page], start=top):
        st = f.stat()
        t.add_row(str(i + 1), str(f.parent.relative_to(P["skills"])), f"{st.st_size:,} B",
                  f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}",
                  style=f"bold black on {GREEN}" if i == sel else "")
    if not t.row_count:
        t.add_row("", "(No Skills Found)", "", "")
    return Panel(t, title="Skills   " + keys(("\u2191\u2193 PgUp/PgDn Home/End", "Move"), ("Enter / V", "View"), ("E", "Edit In Editor"),
                                             ("Q", "Back")), border_style=GREEN, style=f"on {BG}")


def editor_cmd() -> list:
    e = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
    return e.split()


def screen_skills(con) -> None:
    cur = {"sel": 0}
    while STATE["active"]:
        files = skill_files()

        def on_key(k):
            if k in SCROLL_KEYS:
                cur["sel"] = max(0, min(len(files) - 1, cur["sel"] + {"up": -1, "down": 1, "pgup": -10, "pgdn": 10,
                                                                      "home": -10 ** 6, "end": 10 ** 6}[k]))
            elif k in ("enter", "v", "e"):
                return "exit"
        k = live_loop(con, lambda: skills_view(cur["sel"], con.size.height), on_key, vkey="skills", own=SCROLL_KEYS)
        if k not in ("enter", "v", "e") or not files:
            return
        f = files[cur["sel"]]
        if k == "e":
            expect = sig(f)
            before = f.read_text(encoding="utf-8", errors="replace")
            tmp = P["backups"] / f"edit_{f.parent.name}_SKILL.md"
            P["backups"].mkdir(parents=True, exist_ok=True)
            tmp.write_text(before, encoding="utf-8")
            con.print(f"Opening In Editor: {f.parent.name}. Save And Close The Editor To Apply.")
            try:
                subprocess.call(editor_cmd() + [str(tmp)])
                after = tmp.read_text(encoding="utf-8", errors="replace")
                if after == before:
                    pause(con, "No Changes.")
                else:
                    pause(con, f"[{GREEN}]{skill_save(f, after, 'editor', expect)}[/]")
            except Stale:
                pause(con, f"[yellow]The Skill Changed On Disk While You Edited. Not Saved; Your Edit Is In {tmp.name}.[/]")
            except Exception as e:
                pause(con, f"[red]Editor Failed: {e}[/]")
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        key = "skill" + str(cur["sel"])
        SCROLL[key] = {"off": 0}

        def view():
            box, Align, Group, Panel, Table, Text = _rich()
            page = max(5, con.size.height - 4)
            off = SCROLL[key]["off"]
            return Panel(Text("\n".join(lines[off:off + page]), no_wrap=True, overflow="ellipsis"), title=f"{f.parent.name}   " +
                         keys(SCROLL_HINT, ("Q", "Back")) + f"   Line {off + 1}/{len(lines)}",
                         border_style=GREEN)

        def vkey(k):
            pg = max(1, con.size.height - 5)
            d = {"up": -1, "down": 1, "pgup": -pg, "pgdn": pg, "home": -10 ** 6, "end": 10 ** 6}.get(k)
            if d is not None:
                SCROLL[key]["off"] = max(0, min(max(0, len(lines) - max(5, con.size.height - 4)), SCROLL[key]["off"] + d))
        live_loop(con, view, vkey, vkey="skillview", own=SCROLL_KEYS)


def brain_choice(env: dict) -> tuple:
    m = str(env.get("LLM_MAIN") or "").strip().lower() or "grok"
    f = str(env.get("LLM_FALLBACK") or "").strip().lower() or "ollama"
    return (m if m in LLM_CHOICES else "grok"), (f if f in LLM_CHOICES else "ollama")


def brain_ready(p: str, env: dict) -> str:
    if p == "grok":
        return "Key Set" if (env.get("GROK_API_KEY") or env.get("XAI_API_KEY")) else "Key Not Set"
    if p == "ollama":
        return "Local, No Key Needed" + (f" ({env['OLLAMA_MODEL']})" if env.get("OLLAMA_MODEL") else "")
    return "-"


def brain_view():
    box, Align, Group, Panel, Table, Text = _rich()
    env = read_env()
    m, f = brain_choice(env)
    t = Table(box=box.SIMPLE, expand=True, header_style=f"bold {GREEN}")
    for c in ("#", "Brain", "Role", "Status"):
        t.add_column(c)
    for i, p in enumerate(LLM_CHOICES, 1):
        role = "Main" if p == m else ("Fallback" if p == f else "Off")
        t.add_row(str(i), LLM_LABEL[p], Text(role, style=GREEN if role != "Off" else "dim"), brain_ready(p, env))
    note = Text("Optional. Recommended: Grok Main, Local Ollama Fallback. Both Off = No SI Brain: Summaries Stay "
                "Blank, Directives Need Exact Form, Trading Is Unaffected. Saved To .env (LLM_MAIN / LLM_FALLBACK) "
                "With A Backup.", style="dim")
    return Panel(Group(t, note), title="Brain Setup   " + keys(("M", "Pick Main"), ("F", "Pick Fallback"), SCROLL_HINT, ("Q", "Back")),
                 border_style=GREEN, style=f"on {BG}")


def screen_brain(con) -> None:
    while STATE["active"]:
        k = live_loop(con, brain_view, lambda k: "exit" if k in ("m", "f") else None, vkey="brain")
        if k not in ("m", "f"):
            return
        con.clear()
        con.print(brain_view())
        con.print(f"Pick {'Main' if k == 'm' else 'Fallback'}: " +
                  "  ".join(f"[{i}] {LLM_LABEL[p]}" for i, p in enumerate(LLM_CHOICES, 1)) + "  [Q] Back")
        c = getkey(60.0)
        if _back(c) or c not in ("1", "2", "3"):
            continue
        choice = LLM_CHOICES[int(c) - 1]
        key = "LLM_MAIN" if k == "m" else "LLM_FALLBACK"
        try:
            pause(con, f"[{GREEN}]{env_apply(key, choice, sig(P['env']))}[/]")
        except Stale:
            pause(con, "[yellow].env Changed On Disk. Nothing Saved.[/]")


OWNER_CMDS = [("1", "/recap", "Recap"), ("2", "/roast", "Roast"), ("3", "/autopsy", "Autopsy"),
              ("4", "/manage", "Manage An Owner Position"), ("5", "/leave", "Leave An Owner Position")]


def owner_view():
    box, Align, Group, Panel, Table, Text = _rich()
    env = read_env()
    mode = str(env.get("OWNER_COMMANDS") or "off").lower()
    t = Table.grid(padding=(0, 2))
    for k, cmd, lbl in OWNER_CMDS:
        t.add_row(Text(k, style=f"bold {GREEN}"), Text(lbl), Text(cmd, style="dim"))
    info = Text(f"Mode: {mode.title()}   Command Folder: {P['owner_cmds'].name}/   "
                + ("Commands Are Dropped As Files And Answered By The Owner Commands Bot."
                   if mode == "file" else "Switch OWNER_COMMANDS To File In Settings To Send From Here."),
                style=GREEN if mode == "file" else "yellow")
    recent = Text("\n".join(_tail(P["logs"] / "owner_commands.log", 8)) or "(No Owner Command Activity Yet)", style="dim")
    return Panel(Group(info, t, Text("Recent", style="bold"), recent),
                 title="Owner Commands   " + keys(("1-5", "Send"), SCROLL_HINT, ("Q", "Back")), border_style=GREEN, style=f"on {BG}")


def send_owner_command(text: str) -> Path:
    P["owner_cmds"].mkdir(parents=True, exist_ok=True)
    f = P["owner_cmds"] / f"ui_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    atomic_write(f, text + "\n")
    change_log({"owner_command": text.split()[0], "file": f.name})
    return f


def screen_owner(con) -> None:
    valid = {k for k, _, _ in OWNER_CMDS}
    while STATE["active"]:
        k = live_loop(con, owner_view, lambda k: "exit" if k in valid else None, vkey="owner")
        if k not in valid:
            return
        cmd = next(c for kk, c, _ in OWNER_CMDS if kk == k)
        if str(read_env().get("OWNER_COMMANDS") or "off").lower() != "file":
            pause(con, "[yellow]Owner Commands Are Not In File Mode; Nothing Sent.[/]")
            continue
        con.clear()
        if cmd in ("/manage", "/leave"):
            tk = ask(con, "Ticker (E.g. SOL)")
            if not tk:
                continue
            cmd = f"{cmd} {tk.strip().upper()}"
        f = send_owner_command(cmd)
        pause(con, f"[{GREEN}]Sent {cmd} ({f.name}).[/]")


SCREENS = {"1": screen_dashboard, "2": screen_logs, "3": screen_settings, "4": screen_skills,
           "5": screen_owner, "6": screen_brain}


def boot_animation(con) -> None:
    from rich.align import Align
    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    try:
        art = logo_art(con)
        with Live(console=con, refresh_per_second=30, screen=True, redirect_stdout=False, redirect_stderr=False) as live:
            w = len(WORDMARK[0])
            for step in range(0, w + 6, 2):
                t = Text(justify="center")
                for i, ln in enumerate(WORDMARK):
                    t.append(ln[:step].ljust(w) + "\n", style=f"bold {GREEN if i < 2 else PURPLE} on {BG}")
                bar = int(30 * min(1.0, step / max(1, w)))
                t.append("[" + "#" * bar + "." * (30 - bar) + "]  Booting Perps\n", style=PURPLE)
                body = Group(Align.center(art), Align.center(t)) if art is not None else Align.center(t)
                live.update(Panel(body, border_style=PURPLE, style=f"on {BG}"))
                time.sleep(0.05)
            time.sleep(0.6)
    except Exception:
        _err("boot animation")


def ui_main() -> None:
    from rich.console import Console
    con = Console(file=sys.__stdout__, highlight=False)
    STATE["console"] = con
    boot_animation(con)
    while STATE["active"]:
        try:
            from rich.console import Group
            mk = [m[0] for m in MENU]
            k = live_loop(con, lambda: Group(banner(con, show_logo=con.size.height >= 36), menu_panel()),
                          lambda k: "exit" if k in mk else None, vkey="menu")
            if k not in mk:
                continue
            if k in SCREENS:
                SCREENS[k](con)
            elif k == "7":
                deactivate()
                con.print(f"[{PURPLE}]UI Closed. " + ("The Engine Keeps Trading; Its Output Prints Here Again.[/]"
                                                    if STATE["hosted"] else "Nothing Was Stopped.[/]"))
                return
            elif k == "8":
                if not STATE["hosted"]:
                    pause(con, "[yellow]This Console Did Not Start The Engine, So It Will Not Stop It. "
                               "Stop Perps In Its Own Window (Ctrl+C).[/]")
                    continue
                con.print("[yellow]Stop Perps? This Stops Trading (Same As Ctrl+C).  [Y] Yes  [N] No[/]")
                if getkey(30.0) == "y":
                    con.print("[yellow]Stopping Perps...[/]")
                    import _thread
                    _thread.interrupt_main()
                    return
        except KeyboardInterrupt:
            if not STATE["hosted"]:
                deactivate()
                return
        except Exception:
            _err("ui view")
            STATE["errors"] += 1
            try:
                con.print("[red]UI Error (Logged To perps_ui_errors.log). Back To Menu; Trading Unaffected.[/]")
                time.sleep(2)
            except Exception:
                pass
            if STATE["errors"] >= 5:
                deactivate()
                print("[perps] UI disabled after repeated errors; engine keeps running.", flush=True)
                return


# ── hosting inside the engine window ─────────────────────────────────────────
class _Stream:
    """While the UI owns the screen, engine prints go to the ring + perps.console.log."""
    def __init__(self):
        self._buf = ""

    def write(self, s):
        try:
            self._buf += str(s)
            while "\n" in self._buf:
                ln, self._buf = self._buf.split("\n", 1)
                with LOCK:
                    RING.append(ln)
                with open(P["console"], "a", encoding="utf-8") as f:
                    f.write(ln + "\n")
        except Exception:
            pass
        return len(s) if isinstance(s, str) else 0

    def flush(self):
        pass

    def isatty(self):
        return False


def should_start() -> bool:
    if "--no-ui" in sys.argv or not _on(os.environ.get("PERPS_UI", "1")):
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.__stdout__ and sys.__stdout__.isatty())
    except Exception:
        return False


def activate(hosted: bool) -> None:
    STATE["active"], STATE["hosted"] = True, hosted
    if hosted:
        P["logs"].mkdir(parents=True, exist_ok=True)
        sys.stdout, sys.stderr = _Stream(), _Stream()


def deactivate() -> None:
    STATE["active"] = False
    if STATE["hosted"]:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


def start_hosted() -> bool:
    """Called by the engine at boot. Never raises; False = no UI (engine runs as before)."""
    try:
        if not should_start():
            return False
        import rich  # noqa: F401
        activate(hosted=True)

        def _entry():
            try:
                ui_main()
            except Exception:
                _err("ui thread")
                deactivate()
        threading.Thread(target=_entry, daemon=True, name="perps-ui").start()
        return True
    except Exception:
        _err("ui start")
        deactivate()
        return False


def shutdown_hosted(reason: str = "exit") -> None:
    try:
        if STATE.get("active") and STATE.get("hosted"):
            deactivate()
            for ln in list(RING)[-15:]:
                print(ln)
            print(f"[perps] engine exited ({reason}); full output in {P['console'].name}", flush=True)
    except Exception:
        pass


def _win_utf8() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleOutputCP(65001); k.SetConsoleCP(65001)
        for std in (-11, -12):
            h = k.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ── smoke ────────────────────────────────────────────────────────────────────
def smoke() -> int:
    """Render every screen to a throwaway console. No keys, no writes, no network, no trades."""
    import io
    from rich.console import Console
    con = Console(file=io.StringIO(), width=140, height=50, color_system="truecolor", force_terminal=True)
    STATE["active"] = True
    ok = 0
    checks = [("banner", lambda: banner(con, True)), ("menu", menu_panel), ("dashboard", dashboard_view),
              ("settings", lambda: settings_view(0, 50)), ("skills", lambda: skills_view(0)),
              ("brain", brain_view), ("owner", owner_view)]
    checks += [(f"logs:{lbl}", (lambda k=k: logs_view(k, 50))) for k, lbl, _ in log_sources()]
    for name, fn in checks:
        try:
            con.print(fn())
            ok += 1
            print(f"PASS {name}")
        except Exception as e:
            print(f"FAIL {name}: {_redact(e)}")
    art = logo_art(con, 28)
    print(f"{'PASS' if art is not None else 'WARN'} logo ({P['logo'].name}{'' if art is not None else ' not rendered'})")
    out = con.file.getvalue()
    leaked = [k for k, v in read_env().items() if is_secret(k) and len(v) >= 8 and v in out]
    print("PASS no secrets in render" if not leaked else f"FAIL secrets visible: {len(leaked)} keys")
    small_ok = smoke_small(checks)
    STATE["active"] = False
    return 0 if ok == len(checks) and not leaked and small_ok else 1


def smoke_small(checks, w: int = 80, h: int = 24) -> bool:
    """Every screen at a small terminal: output must fit h rows, overflow must show the scroll
    hint, and scroll keys must move the window. Plus relay filter and reset on temp copies."""
    import io
    import tempfile
    from rich.console import Console
    good = True
    for name, fn in checks + [("archives", archives_view)]:
        con = Console(file=io.StringIO(), width=w, height=h, color_system="truecolor", force_terminal=True)
        key = "smoke:" + name
        VIEW[key] = {"off": 0}
        try:
            con.print(Scroll(key, fn), end="")
            rows = con.file.getvalue().count("\n") + 1
            st = VIEW[key]
            moved = True
            if st["n"] > st["page"]:
                scroll_key(key, "pgdn"); a = VIEW[key]["off"]
                scroll_key(key, "end"); b = VIEW[key]["off"]
                scroll_key(key, "home"); c = VIEW[key]["off"]
                moved = a > 0 and b == st["n"] - st["page"] and c == 0
            fit = rows <= h and moved and (st["n"] <= h or "Scroll" in con.file.getvalue())
            good &= fit
            print(f"{'PASS' if fit else 'FAIL'} {w}x{h} {name}: {rows} rows shown, content {st['n']} rows"
                  + (" (scrolls)" if st["n"] > h else ""))
        except Exception as e:
            good = False
            print(f"FAIL {w}x{h} {name}: {_redact(e)}")
    saved = dict(P)
    try:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            rd = tdp / "json_relay"; rd.mkdir()
            (rd / "perp_sol_signal.json").write_text('{"action": "long"}', encoding="utf-8")
            (rd / "x.json").write_text('{"from": "perps_bot", "type": "ack"}', encoding="utf-8")
            (rd / "y.json").write_text('{"market": "BTC-PERP"}', encoding="utf-8")
            (rd / "other_request.json").write_text('{"from": "other_bot"}', encoding="utf-8")
            (rd / "pre_sweep_ack_x.json").write_text('{"source": "feed"}', encoding="utf-8")
            got = sorted(f.name for f in rd.glob("*.json") if is_perps_relay(f))
            rok = got == ["perp_sol_signal.json", "x.json", "y.json"]
            good &= rok
            print(f"{'PASS' if rok else 'FAIL'} relay filter keeps {got}")
            P["logs"] = tdp / "bot_logs"; P["logs"].mkdir()
            P["backups"] = P["logs"] / "ui_backups"; P["changes"] = P["logs"] / "c.jsonl"
            led = {"trades": [{"pnl_usd": 2.5, "fees_usd": 0.1}, {"pnl_usd": -1.0, "fees_usd": 0.1}],
                   "total_realized_usd": 1.5, "total_fees_usd": 0.2}
            _pnl_path().write_text(json.dumps(led), encoding="utf-8")
            r = pnl_reset("smoke", sig(_pnl_path()))
            after = _json(_pnl_path(), {})
            arch = _json(Path(r["archived_to"]), {})
            resets = pnl_archive_rows()
            sok = (after.get("trades") == [] and after.get("total_realized_usd") == 0.0
                   and len(arch.get("trades", [])) == 2 and r["trades_archived"] == 2
                   and abs(r["net_usd_archived"] - 1.5) < 1e-9 and len(resets) == 1
                   and any(P["backups"].glob(_pnl_path().name + ".*.bak")))
            try:
                pnl_reset("stale", ("x", 0, "y")); stale_ok = False
            except Stale:
                stale_ok = True
            good &= sok and stale_ok
            print(f"{'PASS' if sok else 'FAIL'} reset archives + clears + logs lane perps (temp copy)")
            print(f"{'PASS' if stale_ok else 'FAIL'} reset refuses when the ledger changed")
    finally:
        P.clear(); P.update(saved)
    return good


def main() -> int:
    _win_utf8()
    if "--smoke" in sys.argv:
        return smoke()
    try:
        import rich  # noqa: F401
    except ImportError:
        print("Perps console needs `rich`. Run:  pip install rich pillow")
        return 1
    activate(hosted=False)
    try:
        ui_main()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
