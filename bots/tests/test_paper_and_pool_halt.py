"""Offline tests: paper mode (PERPS_DRY_RUN), the pool-read trading halt, and the open/add guards.
No network, no wallet: the engine is imported in a temp workspace with price and pool reads stubbed.
Run: python bots/tests/test_paper_and_pool_halt.py   (or via unittest discover)."""

import os as _os, subprocess as _sp, sys as _sys, unittest as _ut

HERE = _os.path.dirname(_os.path.abspath(__file__))
BOTS = _os.path.dirname(HERE)


class PaperAndPoolHalt(_ut.TestCase):
    def test_script(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        env = {**_os.environ, "PYTHONIOENCODING": "utf-8", "WORKSPACE_DIR": tmp, "PERPS_DRY_RUN": "true",
               "PRIVATE_KEY": "", "LLM_MAIN": "off", "LLM_FALLBACK": "off", "TELEGRAM_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
        p = _sp.run([_sys.executable, _os.path.abspath(__file__), "--run"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=300, env=env, cwd=tmp)
        self.assertEqual(p.returncode, 0, p.stdout[-3000:] + p.stderr[-3000:])
        self.assertIn("ALL PASS", p.stdout)


def _run():
    import asyncio, importlib.util, json, re, time
    tmp = _os.environ["WORKSPACE_DIR"]
    for d in ("bots", "bot_logs", "inbox", "json_relay"):
        _os.makedirs(_os.path.join(tmp, d), exist_ok=True)
    src = open(_os.path.join(BOTS, "perps.py"), encoding="utf-8").read()

    # source-level: every sign/send site is guarded, and the old Doves map is never a price fallback
    lines = src.split("\n")
    for i, l in enumerate(lines):
        if re.match(r"^\s*(tx\.sign\(|signed = VersionedTransaction\()", l):
            assert "_dry_run_refuse(" in lines[i - 1], f"unguarded sign site at line {i+1}"
    assert "JUPITER_ORACLES[asset]" not in src, "Doves oracle map still used as a fallback"
    assert "set perps to dry-run." not in src

    spec = importlib.util.spec_from_file_location("perps", _os.path.join(BOTS, "perps.py"))
    m = importlib.util.module_from_spec(spec)
    _sys.argv = ["perps.py"]
    spec.loader.exec_module(m)
    assert m.PERPS_DRY_RUN is True
    assert _os.path.basename(m.PNL_FILE) == "paper_perp_pnl.json"
    assert _os.path.basename(m.PERP_POSITIONS_FILE) == "paper_perp_positions.json"

    SENDS = []
    import solana.rpc.async_api as A
    async def _boom(*a, **k):
        SENDS.append(1); raise RuntimeError("send attempted")
    A.AsyncClient.send_transaction = _boom
    A.AsyncClient.send_raw_transaction = _boom

    PRICE = {"v": 150.0}
    async def mark(mk, *a, **k): return PRICE["v"]
    m._market_mark_price = mark
    POOL = {"cfg": {"SOL": {"custody": "x", "oracle": "y"}}}
    async def raw(rpc_client=None, force=False): return dict(POOL["cfg"])
    m._load_jupiter_pool_config_raw = raw
    NOTES = []
    async def notify(msg): NOTES.append(str(msg))
    m.notify = notify

    async def main():
        # dry-run backstop
        try:
            m._dry_run_refuse("x"); raise AssertionError("no refusal")
        except RuntimeError as e:
            assert "PERPS_DRY_RUN" in str(e)
        # monitor guard: no tick -> declined
        m._PERP_MONITOR_LAST_TICK = 0.0
        sig, v = await m.route_open_position("long", 220, 20, 11, None, None, 150.0, "SOL-PERP")
        assert (sig, v) == (None, "declined"), (sig, v)
        assert any("monitor is not running" in n for n in NOTES)
        m._PERP_MONITOR_LAST_TICK = time.time()
        # paper open fills at the oracle mark, never sends
        sig, v = await m.route_open_position("long", 220, 20, 11, None, None, 150.0, "SOL-PERP")
        assert sig and sig.startswith("PAPER-open-") and v == "jupiter", (sig, v)
        assert m._LAST_OPEN_MARK["mark"] == 150.0 and m._LAST_JUP_FILL["size_usd"] == 220
        sig2, _ = await m.route_close_position({"market": "SOL-PERP", "direction": "long", "entry_price": 150.0})
        assert sig2.startswith("PAPER-close-")
        ev = [json.loads(l)["event"] for l in open(m.PAPER_FILLS_FILE)]
        assert ev == ["open", "close"], ev
        # paper never reads/clears from the chain and never swaps
        assert await m.reconcile_jupiter_positions() == {}
        assert await m.fetch_venue_liq_price("SOL-PERP", "long") is None
        assert await m._swap_sol_to_usdc(None, None, 10) == 0.0
        # paper alerts are labelled
        NOTES.clear(); await m.notify("x")  # stubbed; check the real prefix in source instead
        assert 'msg = "[PAPER] " + str(msg)' in src
        # pool halt: read fails -> refuse, alert once, packet, no Doves fallback
        POOL["cfg"] = {}
        m._JUP_POOL_CACHE = None
        NOTES.clear()
        sig, v = await m.route_open_position("long", 220, 20, 11, None, None, 150.0, "SOL-PERP")
        assert (sig, v) == (None, "declined")
        assert m.perps_trading_halted()
        assert any("Trading halted" in n for n in NOTES)
        pk = [f for f in _os.listdir(m.INBOX_DIR) if "trading_halted" in f]
        assert pk, "no trading_halted packet"
        n0 = len(NOTES)
        await m.route_open_position("long", 220, 20, 11, None, None, 150.0, "SOL-PERP")
        assert len([n for n in NOTES[n0:] if "Trading halted" in n]) == 0, "halt alert not rate-limited"
        a = m._resolve_position_accounts("SOL-PERP", "long")
        assert a["oracle"] is None and a["_custody_source"] == "hardcoded_fallback"
        m._JUP_POOL_CACHE = {"SOL": {"custody": a["custody"], "mint": a["mint"], "oracle": None}}
        a = m._resolve_position_accounts("SOL-PERP", "long")
        assert a["_custody_source"] == "no_live_oracle", a["_custody_source"]
        # recovery clears the halt
        POOL["cfg"] = {"SOL": {"custody": "x", "oracle": "y"}}
        await m._load_jupiter_pool_config()
        assert m.perps_trading_halted() is None
        # add guard math (ported 2026-10-24 fix)
        liq = m._perp_est_liq("long", 100.0, 2000.0, 100.0)
        assert abs(liq - 95.5) < 1e-9
        assert abs(m._perp_sl_liq_gap_pct("long", 97.0, liq) - (97.0 - 95.5) / 97.0 * 100) < 1e-9
        assert len(SENDS) == 0, "a transaction send was attempted"
    asyncio.run(main())

    # UI + wizard: paper mode reads the paper book and needs no wallet key
    _sys.path.insert(0, BOTS)
    import perps_setup
    assert "PRIVATE_KEY" not in perps_setup.required_keys({"PERPS_DRY_RUN": "true"})
    assert "PRIVATE_KEY" in perps_setup.required_keys({"PERPS_DRY_RUN": "false"})
    print("ALL PASS")


if __name__ == "__main__":
    if "--run" in _sys.argv:
        _run()
    else:
        _ut.main()
