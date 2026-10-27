#!/usr/bin/env python3
"""perps_setup.py: the Perps setup wizard (first run, or `python bots/perps.py --setup`).

    python bots/perps.py --setup     re-run the wizard, then exit
    python bots/perps_setup.py       same, on its own

perps.py calls first_run_check() before it loads bots/.env:
  1. setup.json in the repo root (or bots/) with {"env": {...}} -> written into bots/.env,
     no questions asked ("configured by agent").
  2. bots/.env complete (PRIVATE_KEY and RPC_URL set) -> nothing to do.
  3. Otherwise, in a terminal, the wizard opens. No terminal -> a note, and the engine
     carries on (it stops on its own if the wallet key is missing).

The wizard shows a status table first (Set / Missing, never a value), then:
1 Fill Only What Is Missing, 2 Edit Items, 3 Go Through Everything. Secrets are typed
hidden. Enter keeps a value, `clear` blanks one, Q / Back / Esc goes back without saving,
Ctrl+C stops. Nothing is written until the final confirm. On save, an existing bots/.env is
backed up to bot_logs/backups/env/, every key the wizard does not know is kept as it was,
and the first-run marker bot_logs/setup_wizard.json records when and which key NAMES were set.
Keys and choices are not case-sensitive.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

BOTS = Path(__file__).resolve().parent


def _root() -> Path:
    v = (os.environ.get("WORKSPACE_DIR") or "").strip()
    env = BOTS / ".env"
    if not v and env.exists():
        for ln in env.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if ln.strip().startswith("WORKSPACE_DIR="):
                v = ln.split("=", 1)[1].strip().strip('"').strip("'")
    return Path(v).expanduser().resolve() if v else BOTS.parent


ROOT = _root()
ENV_FILE = ROOT / "bots" / ".env"            # the same file perps.py loads
ENV_EXAMPLE = BOTS / ".env.example"
LOGS = ROOT / "bot_logs"
MARKER = LOGS / "setup_wizard.json"
ENV_BACKUPS = LOGS / "backups" / "env"
SETUP_JSON_CANDIDATES = (ROOT / "setup.json", BOTS / "setup.json")
TUNED_DEFAULTS = BOTS / "tuner" / "perps_tuned_defaults.json"
GREEN, PURPLE = "#39FF14", "#8A2BE2"

REQUIRED = ("PRIVATE_KEY", "RPC_URL")
LLM_CHOICES = ("grok", "ollama", "off")
OWNER_COMMANDS_CHOICES = ("off", "telegram", "file")
BACK_WORDS = ("q", "back", "esc", "b", "\x1b")

# (key, label, secret, default, kind)  kind: text | url | choice | bool | tuner
FIELDS = {
    "PRIVATE_KEY": ("Wallet Private Key (Base58, Burner Wallet)", True, "", "key"),
    "RPC_URL": ("Solana RPC URL", True, "", "url"),
    "HELIUS_DAILY_CREDIT_CAP": ("Helius Plan (Sets The Daily Credit Cap)", False, "30000", "plan"),
    "LLM_MAIN": ("Main Brain", False, "grok", "choice"),
    "LLM_FALLBACK": ("Fallback Brain", False, "ollama", "choice"),
    "GROK_API_KEY": ("Grok / xAI API Key (Optional)", True, "", "text"),
    "GROK_TEXT_MODEL": ("Grok Text Model (Auto = Newest First)", False, "auto", "text"),
    "OLLAMA_URL": ("Ollama URL", False, "http://localhost:11434", "url"),
    "OLLAMA_MODEL": ("Ollama Model", False, "qwen3:14b-q4_K_M", "text"),
    "TELEGRAM_TOKEN": ("Telegram Alert Bot Token (Optional)", True, "", "text"),
    "TELEGRAM_CHAT_ID": ("Telegram Chat ID (Optional)", False, "", "chat"),
    "OWNER_COMMANDS": ("Owner Commands Mode", False, "off", "choice"),
    "OWNER_COMMANDS_TG_TOKEN": ("Owner Commands Bot Token (A Separate Bot)", True, "", "text"),
    "OWNER_COMMANDS_TG_CHAT_ID": ("Owner Commands Chat ID (Blank = Telegram Chat ID)", False, "", "chat"),
    "FEED_CADENCE": ("Feed Cadence (Manual Or Minutes)", False, "5", "cadence"),
    "FLOW_BASE_URL": ("Flow Data URL", False, "https://theflowcycle.com/data", "url"),
    "NEW_POSITIONS_ENABLED": ("Allow New Positions (Opens And Adds From Agent Signals)", False, "on", "tuner"),
    "ENTRY_ALERTS_ENABLED": ("Entry Alerts (Intel Scanner Setups)", False, "on", "tuner"),
    "PERP_MAX_LEVERAGE": ("Max Leverage Cap (10-250x)", False, "20", "lev"),
    "PERP_LEV_LADDER": ("Leverage Ladder (Collateral SOL:Leverage, Less Collateral = More Leverage)", False, "0.1:20,1:15,10:12,50:10", "ladder"),
    "PERP_LIQ_WARN_PCT": ("Liquidation Warning Distance (% Of Price, Once Per Position)", False, "5", "text"),
    "PERP_SL_WARN_PCT": ("SL Warning Distance (% Of Price, Once Per Position)", False, "0.5", "text"),
    "PERP_ONE_PER_MARKET": ("One Position Per Market (true = a long blocks a short; false = long + short as separate positions, what Jupiter allows)", False, "false", "onoff"),
}


class Back(Exception):
    """Q / Back / Esc: leave the wizard, nothing saved."""


# ── small helpers ─────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _on(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on", "y")


def read_env(path: Path = ENV_FILE) -> dict:
    out = {}
    if path.exists():
        for ln in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = re.sub(r"\s+#.*$", "", v).strip().strip('"').strip("'")
    return out


def required_keys(env: dict) -> tuple:
    """Paper mode (PERPS_DRY_RUN=true) needs no wallet key."""
    paper = str(env.get("PERPS_DRY_RUN") or "").strip().lower() in ("1", "true", "yes", "on", "paper")
    return tuple(k for k in REQUIRED if not (paper and k == "PRIVATE_KEY"))


def env_complete(env: dict) -> bool:
    return all(str(env.get(k) or "").strip() for k in required_keys(env))


def setup_missing(env: dict) -> list:
    return [FIELDS[k][0].split(" (")[0] for k in required_keys(env) if not str(env.get(k) or "").strip()]


def write_env(values: dict, path: Path = ENV_FILE, template: Path = ENV_EXAMPLE) -> Path:
    """Set keys: replace their lines in place, append the rest. Unknown keys and comments stay.
    An existing file is backed up first. Values are never printed."""
    if path.exists():
        ENV_BACKUPS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, ENV_BACKUPS / f".env.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak")
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    elif template.exists():
        lines = template.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    else:
        lines = []
    done = set()
    # pass 1: active KEY= lines (every copy of the key gets the new value)
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", ln)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            done.add(m.group(1))
    # pass 2: a template's optional "# KEY=default" knob, only when no active line exists
    for i, ln in enumerate(lines):
        m = re.match(r"^#\s*([A-Z0-9_]+)=", ln.strip())
        if m and m.group(1) in values and m.group(1) not in done:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            done.add(m.group(1))
    extra = [f"{k}={v}" for k, v in values.items() if k not in done]
    if extra:
        lines += ["", "# --- set by the perps setup wizard ---"] + extra
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _read_json(p: Path, default=None):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# Helius plan -> daily credit cap (about 1/30 of the monthly credits, so one bad day
# cannot burn the month). 0 = no cap. Unset = Free.
HELIUS_PLANS = (("free", "Free", 30000, "$0, 1M Credits/Month"),
                ("developer", "Developer", 300000, "$49, 10M Credits/Month"),
                ("business", "Business", 3000000, "$499, 100M Credits/Month"))
HELIUS_FREE_CAP = 30000
CAP_KEY = "HELIUS_DAILY_CREDIT_CAP"


def cap_label(env: dict) -> str:
    v = str(env.get(CAP_KEY) or "").strip()
    if not v:
        return f"Free Plan Cap {HELIUS_FREE_CAP:,}/Day (Default)"
    try:
        n = int(float(v))
    except ValueError:
        return "Invalid (Bot Uses Free Cap)"
    if n <= 0:
        return "Off (No Cap)"
    for _k, name, cap, _d in HELIUS_PLANS:
        if n == cap:
            return f"{name} ({cap:,}/Day)"
    return f"Custom ({n:,}/Day)"


def ask_helius_plan(io, cur: dict):
    """Helius plan menu. Returns the cap as a string, or None to keep the current value."""
    now = str(cur.get(CAP_KEY) or "").strip()
    io.con.print("[dim]Helius bills in credits. The bot meters its own Helius calls and slows background "
                 "reads near a daily cap so one bad day cannot burn the month. Perps alone uses about "
                 "10k-300k credits/month. Not on Helius? Pick Free.[/]")
    opts = []
    for i, (k, name, cap, desc) in enumerate(HELIUS_PLANS, 1):
        io.con.print(f"  {i}. {name} [dim]({desc}) -> {cap:,}/Day[/]")
        opts.append(k)
    io.con.print(f"  {len(opts) + 1}. Custom [dim](Your Own Daily Number)[/]")
    io.con.print(f"  {len(opts) + 2}. None [dim](0 = No Cap, Not Recommended)[/]")
    opts += ["custom", "none"]
    try:
        n_now = int(float(now)) if now else HELIUS_FREE_CAP
    except ValueError:
        n_now = HELIUS_FREE_CAP
    dflt = "none" if n_now <= 0 else next((k for k, _n, c, _d in HELIUS_PLANS if c == n_now), "custom")
    while True:
        pick = io.choose("Which Helius Plan Are You On?", opts, dflt)
        if pick == "custom":
            while True:
                v = io.ask("Daily Credit Cap (Whole Number Above 0)").replace(",", "").replace("_", "")
                if v.isdigit() and int(v) > 0:
                    break
                io.con.print("[yellow]Enter A Whole Number Above 0 (Q = Back; Pick None For No Cap).[/]")
            val = str(int(v))
        elif pick == "none":
            io.con.print("[bold red]Warning: No Cap Means Nothing Stops A Bug, A Busy Market Or A Leaked Key "
                         "From Using Your Whole Monthly Helius Credits (Or Running Up Overage).[/]")
            if not io.confirm("Really Run With No Daily Cap?", False):
                continue
            val = "0"
        else:
            val = str(next(c for k, _n, c, _d in HELIUS_PLANS if k == pick))
        return None if val == now else val


# Two tuner switches (saved defaults in bots/tuner, the layer the engine reads at boot).
SWITCHES = ("NEW_POSITIONS_ENABLED", "ENTRY_ALERTS_ENABLED")
LEGACY_SWITCH = "AUTO_TRADE_ENABLED"   # deprecated single switch: sets both when they are unset
SWITCH_HELP = {
    "NEW_POSITIONS_ENABLED": ("Off = the bot declines every long, short and add the agent sends (reason "
                              "new_positions_off). Closes, adjust, update_params, close_pct and pull_initial "
                              "still work. The bot never opens a trade without an agent signal."),
    "ENTRY_ALERTS_ENABLED": ("Off = the intel scanner stops sending entry alerts. Packets, exit alerts "
                             "and agent signals are unaffected."),
}


def switch_now(key: str) -> bool:
    d = (_read_json(TUNED_DEFAULTS, {}) or {}).get("defaults") or {}
    if key in d:
        return bool(d[key])
    if LEGACY_SWITCH in d:
        return bool(d[LEGACY_SWITCH])
    return True


def set_switch(key: str, on: bool) -> None:
    """Save one switch to bots/tuner (backed up first). A legacy AUTO_TRADE_ENABLED is
    migrated: its value fills the other switch if that one is unset, then it is removed."""
    raw = _read_json(TUNED_DEFAULTS, {}) or {}
    raw = raw if isinstance(raw, dict) else {}
    if TUNED_DEFAULTS.exists():
        ENV_BACKUPS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TUNED_DEFAULTS, ENV_BACKUPS / f"{TUNED_DEFAULTS.name}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak")
    d = raw.setdefault("defaults", {})
    if LEGACY_SWITCH in d:
        legacy = bool(d.pop(LEGACY_SWITCH))
        for k in SWITCHES:
            d.setdefault(k, legacy)
        log("deprecated: AUTO_TRADE_ENABLED migrated to NEW_POSITIONS_ENABLED + ENTRY_ALERTS_ENABLED")
    d[key] = bool(on)
    raw["_updated"] = _now_iso()
    _write_json(TUNED_DEFAULTS, raw)


def _switch_values(src: dict) -> dict:
    """Switch values from answers / setup.json. Legacy AUTO_TRADE_ENABLED sets both (if unset)."""
    out = {k: src[k] for k in SWITCHES if k in src}
    if LEGACY_SWITCH in src:
        log("deprecated: AUTO_TRADE_ENABLED read as NEW_POSITIONS_ENABLED + ENTRY_ALERTS_ENABLED")
        for k in SWITCHES:
            out.setdefault(k, src[LEGACY_SWITCH])
    return out


def write_marker(by: str, keys) -> None:
    m = _read_json(MARKER, {}) or {}
    m.update({"completed_at": _now_iso(), "by": by, "keys_set": sorted(keys),
              "runs": int(m.get("runs") or 0) + 1})
    m.setdefault("first_run_at", m["completed_at"])
    _write_json(MARKER, m)


def log(msg: str) -> None:
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "perps_launcher.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} setup: {msg}\n")
    except Exception:
        pass


# ── brain ─────────────────────────────────────────────────────────────────────
def brain_pair(env: dict) -> tuple:
    m = str(env.get("LLM_MAIN") or "grok").strip().lower()
    f = str(env.get("LLM_FALLBACK") or "ollama").strip().lower()
    m = m if m in LLM_CHOICES else "grok"
    f = f if f in LLM_CHOICES and (f != m or f == "off") else "off"
    return m, f


def brain_label(p: str, env: dict) -> str:
    """Main / Fallback / Off / Off (Key Set) for one provider."""
    m, f = brain_pair(env)
    if p == m:
        return "Main"
    if p == f:
        return "Fallback"
    keyset = bool(str(env.get("GROK_API_KEY") or env.get("XAI_API_KEY") or "").strip()) if p == "grok" else \
        bool(str(env.get("OLLAMA_MODEL") or "").strip())
    return "Off (Key Set)" if keyset else "Off"


# ── status table ──────────────────────────────────────────────────────────────
def rows(env: dict) -> list:
    """[(area, key or None, label, required)] in display order; key None = info row."""
    m, f = brain_pair(env)
    out = [("Wallet", "PRIVATE_KEY", FIELDS["PRIVATE_KEY"][0], True),
           ("RPC", "RPC_URL", FIELDS["RPC_URL"][0], True),
           ("RPC", CAP_KEY, FIELDS[CAP_KEY][0], False),
           ("Brain", "LLM_MAIN", FIELDS["LLM_MAIN"][0], False),
           ("Brain", "LLM_FALLBACK", FIELDS["LLM_FALLBACK"][0], False)]
    for p, keys in (("grok", ("GROK_API_KEY", "GROK_TEXT_MODEL")), ("ollama", ("OLLAMA_URL", "OLLAMA_MODEL"))):
        if p in (m, f):
            out += [("Brain", k, FIELDS[k][0], False) for k in keys]
        else:
            out.append(("Brain", None, p.title(), False))
    out += [("Alerts", "TELEGRAM_TOKEN", FIELDS["TELEGRAM_TOKEN"][0], False),
            ("Alerts", "TELEGRAM_CHAT_ID", FIELDS["TELEGRAM_CHAT_ID"][0], False),
            ("Owner Commands", "OWNER_COMMANDS", FIELDS["OWNER_COMMANDS"][0], False)]
    if str(env.get("OWNER_COMMANDS") or "off").strip().lower() == "telegram":
        out += [("Owner Commands", k, FIELDS[k][0], False) for k in ("OWNER_COMMANDS_TG_TOKEN", "OWNER_COMMANDS_TG_CHAT_ID")]
    out += [("Feeds", "FEED_CADENCE", FIELDS["FEED_CADENCE"][0], False),
            ("Feeds", "FLOW_BASE_URL", FIELDS["FLOW_BASE_URL"][0], False),
            ("Trading", "NEW_POSITIONS_ENABLED", FIELDS["NEW_POSITIONS_ENABLED"][0], False),
            ("Trading", "ENTRY_ALERTS_ENABLED", FIELDS["ENTRY_ALERTS_ENABLED"][0], False),
            ("Trading", "PERP_MAX_LEVERAGE", FIELDS["PERP_MAX_LEVERAGE"][0], False),
            ("Trading", "PERP_LEV_LADDER", FIELDS["PERP_LEV_LADDER"][0], False),
            ("Trading", "PERP_ONE_PER_MARKET", FIELDS["PERP_ONE_PER_MARKET"][0], False)]
    return out


def status_of(key, env: dict, required: bool, label: str = "") -> str:
    if key is None:
        return brain_label(label.lower(), env)
    if key in SWITCHES:
        return "On" if switch_now(key) else "Off"
    if key == CAP_KEY:
        return cap_label(env)
    if key in ("LLM_MAIN", "LLM_FALLBACK"):
        m, f = brain_pair(env)
        name = m if key == "LLM_MAIN" else f
        return name.title() + ("" if str(env.get(key) or "").strip() else " (Default)")
    if key == "OWNER_COMMANDS":
        v = str(env.get(key) or "").strip().lower()
        return (v or "off").title() + ("" if v else " (Default)")
    if str(env.get(key) or "").strip():
        return "Set"
    if required:
        return "Missing"
    return "Default" if FIELDS[key][2] else "Not Set (Optional)"


def status_table(env: dict):
    from rich import box
    from rich.table import Table
    from rich.text import Text
    t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {PURPLE}", expand=False)
    for c in ("Area", "#", "Setting", "Status", "Key"):
        t.add_column(c, style="dim" if c == "Key" else None)
    n, last = 0, None
    for area, key, label, req in rows(env):
        a = Text(area if area != last else "", style=f"bold {GREEN}")
        last = area
        st = status_of(key, env, req, label)
        style = "bold red" if st == "Missing" else (f"bold {GREEN}" if st in ("Set", "Main", "Fallback", "On") else "dim")
        if key is None:
            t.add_row(a, "", Text(label, style="dim"), Text(st, style=style), "")
            continue
        n += 1
        t.add_row(a, str(n), label, Text(st, style=style), key)
    return t


def editable_keys(env: dict) -> list:
    return [k for _a, k, _l, _r in rows(env) if k]


def missing_keys(env: dict) -> list:
    return [k for k in editable_keys(env)
            if k not in SWITCHES and not str(env.get(k) or "").strip()]


# ── input (scriptable) ───────────────────────────────────────────────────────
class IO:
    """Terminal input. Secrets hidden. A piped stdin (scripted run) reads plain lines."""

    def __init__(self, console=None, lines=None):
        from rich.console import Console
        self.con = console or Console(highlight=False)
        self.lines = list(lines) if lines is not None else None

    def raw(self, prompt: str, secret: bool = False) -> str:
        if self.lines is not None:
            self.con.print(prompt, end="")
            v = self.lines.pop(0) if self.lines else ""
            self.con.print("" if secret else v)
            return v
        if secret and sys.stdin.isatty():
            return self.con.input(prompt, password=True)
        self.con.print(prompt, end="")
        ln = sys.stdin.readline()
        if ln == "":
            raise Back()
        return ln.rstrip("\r\n")

    def ask(self, prompt: str, secret: bool = False) -> str:
        v = self.raw(f"{prompt} [dim][Q] Back[/] ", secret)
        if v.strip().lower() in BACK_WORDS or "\x1b" in v:
            raise Back()
        return v.strip()

    def choose(self, prompt: str, choices, default: str) -> str:
        opts = [str(c).lower() for c in choices]
        while True:
            v = self.ask(f"{prompt} [dim]({' / '.join(o.title() for o in opts)}; Enter = {default.title()})[/]").lower()
            if not v:
                return default
            if v.isdigit() and 1 <= int(v) <= len(opts):
                return opts[int(v) - 1]
            if v in opts:
                return v
            self.con.print("[yellow]Pick One Of The Listed Options.[/]")

    def confirm(self, prompt: str, default: bool) -> bool:
        while True:
            v = self.ask(f"{prompt} [dim][Y] Yes  [N] No (Enter = {'Y' if default else 'N'})[/]").lower()
            if not v:
                return default
            if v in ("y", "yes", "1", "true", "on"):
                return True
            if v in ("n", "no", "0", "false", "off"):
                return False


def _valid(key: str, kind: str, v: str) -> str | None:
    """Error text, or None if fine. Never echoes the value."""
    if kind == "key":
        if v.startswith("["):
            try:
                arr = json.loads(v)
                return None if isinstance(arr, list) and len(arr) == 64 else "Expected A 64-Byte Array"
            except Exception:
                return "Not A Valid Byte Array"
        return None if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{80,90}", v) else "Not A Base58 Private Key"
    if kind == "url":
        return None if re.match(r"^https?://\S+$", v, re.I) else "Must Start With http:// Or https://"
    if kind == "chat":
        return None if re.fullmatch(r"-?\d+|@\w+", v) else "Expected A Numeric Chat ID"
    if kind == "lev":
        try:
            return None if 10 <= float(v) <= 250 else "Enter 10 To 250"
        except ValueError:
            return "Enter A Number (10 To 250)"
    if kind == "ladder":
        try:
            pts = [tuple(float(x) for x in part.split(":")) for part in v.replace(";", ",").split(",") if part.strip()]
            ok = pts and all(len(t) == 2 and t[0] > 0 and 10 <= t[1] <= 250 for t in pts)
            return None if ok else "Use collateral_sol:leverage pairs, e.g. 0.1:20,1:15,10:12,50:10"
        except ValueError:
            return "Use collateral_sol:leverage pairs, e.g. 0.1:20,1:15,10:12,50:10"
    if kind == "onoff":
        return None if v.strip().lower() in ("true", "false", "on", "off", "1", "0", "yes", "no") else "Enter true Or false"
    if kind == "cadence":
        return None if v.lower() == "manual" or (v.isdigit() and int(v) > 0) else "Enter Manual Or A Number Of Minutes"
    return None


def ask_field(io: IO, key: str, cur: dict):
    """One field. Returns the new value, or None to keep the current one."""
    label, secret, default, kind = FIELDS[key]
    if key in SWITCHES:
        now = switch_now(key)
        io.con.print(f"[dim]{SWITCH_HELP[key]}[/]")
        v = io.confirm(label, now)
        return None if v == now else ("on" if v else "off")
    if key in ("LLM_MAIN", "LLM_FALLBACK"):
        m, f = brain_pair(cur)
        io.con.print("  " + "   ".join(f"{i}. {p.title()} [dim]({brain_label(p, cur) if p != 'off' else 'No SI'})[/]"
                                       for i, p in enumerate(LLM_CHOICES, 1)))
        now = m if key == "LLM_MAIN" else f
        opts = list(LLM_CHOICES) if key == "LLM_MAIN" else [p for p in LLM_CHOICES if p != m or p == "off"]
        v = io.choose(f"{label} [dim](The Other Provider Is Off Even If Its Key Is Set)[/]", opts, now)
        return None if (v == now and str(cur.get(key) or "").strip()) else v
    if key == CAP_KEY:
        return ask_helius_plan(io, cur)
    if key == "OWNER_COMMANDS":
        io.con.print("[dim]Off: nothing runs.  Telegram: a SEPARATE command bot reads only your chat.  "
                     "File: drop a .txt into owner_commands/ (any OS).[/]")
        now = str(cur.get(key) or "off").strip().lower()
        now = now if now in OWNER_COMMANDS_CHOICES else "off"
        v = io.choose(label, OWNER_COMMANDS_CHOICES, now)
        return None if (v == now and str(cur.get(key) or "").strip()) else v
    has = bool(str(cur.get(key) or "").strip())
    while True:
        if has:
            hint = "[dim](Set; Enter Keeps, Clear Blanks)[/]"
        elif default and not secret:
            hint = f"[dim](Enter = {default})[/]"
        else:
            hint = "[dim](Missing; Enter Skips)[/]"
        v = io.ask(f"{label} {hint}", secret=secret)
        if v.lower() == "clear":
            return "" if has and io.confirm(f"Really Clear {label}?", False) else None
        if not v:
            return None if has or not default else default
        if not secret and v == str(cur.get(key) or ""):
            return None
        err = _valid(key, kind, v)
        if err is None:
            return v
        io.con.print(f"[yellow]{err}. Try Again (Q = Back).[/]")


# ── the wizard ────────────────────────────────────────────────────────────────

def suggest_drain_minutes(env: dict) -> tuple[int, str]:
    """perps_lane drain interval from the .env cadences: the fastest packet
    producer (FEED_CADENCE minutes, PACKET_INTERVAL_SECONDS, 15-min scan),
    floored at 5 and capped at 30."""
    src = {"intel scan": 15.0}
    fc = str(env.get("FEED_CADENCE") or "manual").strip().lower()
    if fc.isdigit() and int(fc) > 0:
        src["FEED_CADENCE"] = float(fc)
    try:
        src["packets"] = float(env.get("PACKET_INTERVAL_SECONDS") or 3600) / 60.0
    except ValueError:
        src["packets"] = 60.0
    who = min(src, key=src.get)
    return int(min(30, max(5, round(src[who])))), who


def print_agent_setup_reminder(io, env: dict | None = None):
    """End-of-wizard reminder: the agent MUST build the lane automations."""
    try:
        env = env if env is not None else read_env()
        m, who = suggest_drain_minutes(env)
        io.con.print("")
        io.con.print("[bold yellow]Agent Setup (Required)[/]")
        io.con.print("The bot only writes packets. The agent MUST create recurring jobs from automations/:")
        io.con.print(f"  perps_lane     every {m} min (fastest producer: {who}; floor 5, cap 30)")
        io.con.print("  warden_lane    every 15-20 min   misc_lane every 15-30 min   morning_brief once a day")
        io.con.print("  Stagger start minutes, e.g. perps 3-53/10, warden 7,27,47, misc 9,24,39,54.")
        io.con.print("  Re-gauge when a cadence changes. Details: SETUP.md section 11.")
    except Exception:
        pass


def wizard(out_path: Path = ENV_FILE, io: IO | None = None, answers: dict | None = None):
    """Interactive (io) or non-interactive (answers dict: key -> value, for tests and agents).
    Returns the saved path, or None when nothing was saved."""
    from rich.panel import Panel
    from rich.text import Text
    io = io or IO()
    cur = read_env(out_path)
    if answers is not None:
        vals = {k: str(v) for k, v in answers.items() if k in FIELDS and k not in SWITCHES}
        if CAP_KEY not in vals and not str(cur.get(CAP_KEY) or "").strip():
            vals[CAP_KEY] = str(HELIUS_FREE_CAP)   # no plan given: Free-plan cap
        if vals:
            write_env(vals, out_path)
        sw = _switch_values(answers)
        for k, v in sw.items():
            set_switch(k, _on(v))
        write_marker("answers", list(vals) + list(sw))
        log(f"wizard (answers) wrote {len(vals)} keys")
        return out_path
    try:
        io.con.print(Panel("Setup Wizard. Secrets Are Typed Hidden And Never Shown. Enter Keeps A Value, "
                           "Clear Blanks One, Q / Back / Esc Goes Back Without Saving. Ctrl+C Stops.",
                           border_style=PURPLE, title="Perps Setup"))
        io.con.print(status_table(cur))
        miss = setup_missing(cur)
        io.con.print(Text("Setup: Complete" if not miss else f"Setup: {len(miss)} Missing ({', '.join(miss)})",
                          style=GREEN if not miss else "bold red"))
        io.con.print("[1] Fill Only What Is Missing   [2] Edit Items   [3] Go Through Everything   [Q] Back")
        mode = io.choose("Choose", ["1", "2", "3"], "1")
        keys = editable_keys(cur)
        if mode == "1":
            todo = missing_keys(cur) + (list(SWITCHES) if not MARKER.exists() else [])
            if not todo:
                io.con.print(f"[{GREEN}]Nothing Missing.[/]")
                write_marker("wizard", [])
                print_agent_setup_reminder(io)
                return out_path
        elif mode == "2":
            n = io.ask("Item Number(s), Comma Separated")
            idx = [int(x) for x in n.replace(" ", "").split(",") if x.isdigit()]
            todo = [keys[i - 1] for i in idx if 1 <= i <= len(keys)]
        else:
            todo = list(keys)
        vals: dict = {}
        done: set = set()
        while todo:
            k = todo.pop(0)
            if k in done:
                continue
            done.add(k)
            v = ask_field(io, k, {**cur, **vals})
            if v is not None:
                vals[k] = v
            if k in ("LLM_MAIN", "LLM_FALLBACK", "OWNER_COMMANDS"):
                merged = {**cur, **vals}
                if k == "LLM_MAIN" and "LLM_FALLBACK" not in done and "LLM_FALLBACK" not in todo:
                    todo.insert(0, "LLM_FALLBACK")
                new = editable_keys(merged) if mode == "3" else missing_keys(merged)
                area = "Brain" if k != "OWNER_COMMANDS" else "Owner Commands"
                todo += [x for a, x, _l, _r in rows(merged) if x and a == area and x in new
                         and x not in done and x not in todo]
        if not vals:
            io.con.print("[dim]No Changes.[/]")
            write_marker("wizard", [])
            print_agent_setup_reminder(io)
            return out_path
        io.con.print("Changes: " + ", ".join(f"{FIELDS[k][0].split(' (')[0]} ({'Cleared' if v == '' else 'Set'})"
                                             for k, v in vals.items()))
        if not io.confirm(f"Save {len(vals)} Change(s)?", True):
            io.con.print("[yellow]Not Saved.[/]")
            return None
        sw = {k: vals.pop(k) for k in SWITCHES if k in vals}
        if vals:
            write_env(vals, out_path)
        for k, v in sw.items():
            set_switch(k, v == "on")
        write_marker("wizard", list(vals) + list(sw))
        log(f"wizard wrote {len(vals)} keys to {out_path.name}" + (f" + {', '.join(sw)}" if sw else ""))
        io.con.print(f"[{GREEN}]Saved[/] {len(vals) + len(sw)} Setting(s) (Values Hidden). "
                     f"Old .env Backed Up To bot_logs/backups/env/.")
        print_agent_setup_reminder(io)
        return out_path
    except Back:
        io.con.print("[yellow]Wizard Closed, Nothing Saved.[/]")
        return None


# ── agent path + first-run check (called by perps.py) ────────────────────────
def setup_json_path():
    for p in SETUP_JSON_CANDIDATES:
        if p.exists():
            return p
    return None


def apply_setup_json(con=None) -> bool:
    """Agent override: setup.json {"env": {...}} is written into bots/.env, no questions asked."""
    p = setup_json_path()
    if not p:
        return False
    data = _read_json(p, {}) or {}
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    src = {k: data[k] for k in SWITCHES + (LEGACY_SWITCH,) if k in data}
    src.update({k: env.pop(k) for k in SWITCHES + (LEGACY_SWITCH,) if k in env})
    sw = _switch_values(src)
    if CAP_KEY not in env and not str(read_env().get(CAP_KEY) or "").strip():
        env[CAP_KEY] = str(HELIUS_FREE_CAP)   # no plan given: Free-plan cap
    if env or not ENV_FILE.exists():
        write_env({k: str(v) for k, v in env.items()})
    for k, v in sw.items():
        set_switch(k, _on(v))
    write_marker("agent", list(env) + list(sw))
    log(f"configured by agent ({p.name}, {len(env)} keys)")
    try:   # setup.json holds secrets: remove it once applied (values now live in bots/.env)
        p.unlink()
        log(f"deleted {p.name} after applying (secrets now only in bots/.env)")
    except OSError as e:
        log(f"could not delete {p.name} ({type(e).__name__}); delete it by hand")
    if con:
        con.print(f"[{GREEN}]Configured By Agent[/] ({p.name}, File Deleted After Applying)")
    return True


def first_run_check(force: bool = False, io=None) -> bool:
    """perps.py calls this before loading bots/.env. force = --setup.
    Returns True when the wizard was skipped (setup.json applied or .env already complete)."""
    if io is None:
        from rich.console import Console
        io = IO(Console(highlight=False))
    con = io.con
    if force:
        wizard(io=io)
        return False
    if apply_setup_json(con):
        return True
    if env_complete(read_env()):
        if not MARKER.exists():
            write_marker("complete .env", [])
        return True
    if not sys.stdin or not sys.stdin.isatty():
        con.print("[yellow]bots/.env Is Incomplete And There Is No Terminal For The Wizard. "
                  "Add setup.json Or Fill bots/.env. Helius Daily Cap: "
                  f"{cap_label(read_env())} (Set HELIUS_DAILY_CREDIT_CAP For Your Plan).[/]")
        log("first run: incomplete .env, no tty, wizard skipped")
        return False
    con.print("[yellow]No Complete .env Yet, Opening The Setup Wizard.[/]")
    wizard(io=io)
    return False
    first_run_check(force=True)
