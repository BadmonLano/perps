"""Setup wizard test: runs in a fresh temp folder with scripted input. Never touches the real bots/.env."""

import os as _os, subprocess as _sp, sys as _sys, unittest as _ut


class SetupWizard(_ut.TestCase):
    """unittest discover entry: runs this file as a script in a child process (offline)."""
    def test_script(self):
        here = _os.path.dirname(_os.path.abspath(__file__))
        p = _sp.run([_sys.executable, _os.path.abspath(__file__)] + [],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                    env={**_os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(p.returncode, 0, (p.stdout[-3000:] + p.stderr[-3000:]))


if __name__ == "__main__":  # script mode (also what the test above runs)
    import io, json, os, shutil, subprocess, sys, tempfile
    
    HERE = os.path.dirname(os.path.abspath(__file__))
    BOTS = os.path.dirname(HERE)
    TMP = tempfile.mkdtemp(prefix="perps_wizard_test_")
    os.makedirs(os.path.join(TMP, "bots", "tuner"))
    shutil.copy(os.path.join(BOTS, ".env.example"), os.path.join(TMP, "bots", ".env.example"))
    os.environ["WORKSPACE_DIR"] = TMP
    sys.path.insert(0, BOTS)
    import base58  # noqa: E402
    import perps_setup as ps  # noqa: E402
    from rich.console import Console  # noqa: E402
    
    assert os.path.realpath(ps.ENV_FILE).startswith(os.path.realpath(TMP)), "not in temp folder"
    # tuner defaults live next to the code (bots/tuner), not under WORKSPACE_DIR: point them at a temp copy
    _tuned = os.path.join(TMP, "bots", "tuner", "perps_tuned_defaults.json")
    if ps.TUNED_DEFAULTS.exists():
        shutil.copy(ps.TUNED_DEFAULTS, _tuned)
    ps.TUNED_DEFAULTS = type(ps.TUNED_DEFAULTS)(_tuned)
    KEY = base58.b58encode(os.urandom(64)).decode()
    fails = []
    
    def check(name, ok):
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)
    
    def run(lines):
        buf = io.StringIO()
        r = ps.wizard(io=ps.IO(Console(file=buf, width=140, force_terminal=False), lines))
        return r, buf.getvalue()
    
    try:
        # 1. Fresh: Fill Missing
        r, out = run(["1", "", "notakey", KEY, "https://rpc.example/x", "", "GROK", "ollama", "", "", "", "",
                      "", "", "FILE", "", "", "", "", "", "n", "y", "y"])   # blanks: cadence, flow URL, lev cap, ladder, one-per-market; then new positions n, entry alerts y, save y
        env = ps.read_env()
        check("fresh save", r is not None and ps.env_complete(env))
        check("bad key rejected", "Not A Base58 Private Key" in out)
        check("autonomy asked first, suggest default", env.get("AGENT_MODE") == "suggest"
              and "how much can the agent do" in out
              and out.find("how much can the agent do") < out.find("Not A Base58 Private Key"))
        check("brain main/fallback case-insensitive", (env.get("LLM_MAIN"), env.get("LLM_FALLBACK")) == ("grok", "ollama"))
        check("owner commands file", env.get("OWNER_COMMANDS") == "file")
        check("feed cadence 5 min default", env.get("FEED_CADENCE") == "5")
        check("helius plan Enter = Free cap 30000", env.get("HELIUS_DAILY_CREDIT_CAP") == "30000")
        check("new positions off in tuner defaults", ps.switch_now("NEW_POSITIONS_ENABLED") is False)
        check("entry alerts on in tuner defaults", ps.switch_now("ENTRY_ALERTS_ENABLED") is True)
        check("no legacy key written", "AUTO_TRADE_ENABLED" not in json.load(open(ps.TUNED_DEFAULTS)).get("defaults", {}))
        check("marker written", json.load(open(ps.MARKER)).get("by") == "wizard")
        check("marker holds names only", KEY not in open(ps.MARKER).read())
        check("key never printed", KEY not in out)
    
        # 2. Edit one item; unknown key kept; backup made
        with open(ps.ENV_FILE, "a") as f:
            f.write("MY_CUSTOM_KEY=keepme\n")
        r, out = run(["2", "11", "123:abc", "y"])   # item numbers +2: Autonomy row is #1, Helius plan row is #4
        env = ps.read_env()
        check("unknown key kept", env.get("MY_CUSTOM_KEY") == "keepme")
        check("item edited", env.get("TELEGRAM_TOKEN") == "123:abc")
        check("existing .env backed up", len(os.listdir(ps.ENV_BACKUPS)) >= 1)
        check("token never printed", "123:abc" not in out)
    
        # 3. Back writes nothing
        before = open(ps.ENV_FILE).read()
        r, _ = run(["3", "Q"])
        check("back saves nothing", r is None and open(ps.ENV_FILE).read() == before)
    
        # 4. Complete .env skips the wizard (no prompts, nothing changes)
        buf = io.StringIO()
        skipped = ps.first_run_check(io=ps.IO(Console(file=buf), []))
        check("complete .env skips wizard", skipped is True and open(ps.ENV_FILE).read() == before)
    
        # 5. setup.json (agent path)
        os.remove(ps.ENV_FILE)
        with open(os.path.join(TMP, "setup.json"), "w") as f:
            json.dump({"env": {"PRIVATE_KEY": KEY, "RPC_URL": "https://rpc.example/y"}, "NEW_POSITIONS_ENABLED": True}, f)
        buf = io.StringIO()
        ps.first_run_check(io=ps.IO(Console(file=buf), []))
        env = ps.read_env()
        check("setup.json applied", env.get("RPC_URL") == "https://rpc.example/y" and ps.switch_now("NEW_POSITIONS_ENABLED") is True)
        check("setup.json left entry alerts as they were", ps.switch_now("ENTRY_ALERTS_ENABLED") is True)
        check("setup.json logged as agent", json.load(open(ps.MARKER)).get("by") == "agent")
        check("setup.json deleted after applying", not os.path.exists(os.path.join(TMP, "setup.json")))
        check("setup.json without a plan -> Free cap 30000", env.get("HELIUS_DAILY_CREDIT_CAP") == "30000")
    
        # 5b. legacy AUTO_TRADE_ENABLED=false (setup.json) sets both switches off
        with open(os.path.join(TMP, "setup.json"), "w") as f:
            json.dump({"env": {}, "AUTO_TRADE_ENABLED": False}, f)
        ps.first_run_check(io=ps.IO(Console(file=io.StringIO()), []))
        check("legacy AUTO_TRADE_ENABLED=false sets both off",
              ps.switch_now("NEW_POSITIONS_ENABLED") is False and ps.switch_now("ENTRY_ALERTS_ENABLED") is False)
        # 5c. a saved legacy key alone reads as both (old tuned-defaults file)
        json.dump({"defaults": {"AUTO_TRADE_ENABLED": False}}, open(ps.TUNED_DEFAULTS, "w"))
        check("legacy saved key read as both", ps.switch_now("NEW_POSITIONS_ENABLED") is False and ps.switch_now("ENTRY_ALERTS_ENABLED") is False)
        ps.set_switch("ENTRY_ALERTS_ENABLED", True)
        d = json.load(open(ps.TUNED_DEFAULTS))["defaults"]
        check("legacy key migrated on save", d == {"NEW_POSITIONS_ENABLED": False, "ENTRY_ALERTS_ENABLED": True})

        # 6. perps.py --setup with piped input exits without starting the bot
        p = subprocess.run([sys.executable, os.path.join(BOTS, "perps.py"), "--setup"], input="2\n12\n555\ny\n",
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           env={**os.environ, "WORKSPACE_DIR": TMP, "PYTHONIOENCODING": "utf-8"})
        check("--setup exits 0", p.returncode == 0)
        check("--setup wrote value", ps.read_env().get("TELEGRAM_CHAT_ID") == "555")
        check("--setup printed no key", KEY not in p.stdout + p.stderr)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    
    print("ALL PASS" if not fails else f"{len(fails)} FAIL")
    sys.exit(1 if fails else 0)
