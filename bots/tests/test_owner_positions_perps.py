"""Offline tests for the perps owner-position feed. No network, no wallet.
Extracts the OWNER POSITIONS block from perps.py / perps_bot.py and runs it on stubs.
Run: python test_owner_positions_perps.py [path to perps file]  (default ../perps.py), or via unittest discover."""

import os as _os, subprocess as _sp, sys as _sys, unittest as _ut


class OwnerPositionsPerps(_ut.TestCase):
    """unittest discover entry: runs this file as a script in a child process (offline)."""
    def test_script(self):
        here = _os.path.dirname(_os.path.abspath(__file__))
        p = _sp.run([_sys.executable, _os.path.abspath(__file__)] + [_os.path.join(here, "..", "perps.py")],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                    env={**_os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(p.returncode, 0, (p.stdout[-3000:] + p.stderr[-3000:]))


if __name__ == "__main__":  # script mode (also what the test above runs)
    import asyncio, json, os, re, sys, tempfile, time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    src = open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "perps.py"), encoding="utf-8").read()
    a = src.index("OWNER_POS_ENABLED        =")
    a = src.rfind("\n# ════", 0, a); a = src.rfind("\n# ════", 0, a - 1)
    b = src.index("async def process_signal(data: dict):")
    block = src[a:b]
    tmp = tempfile.mkdtemp(); os.makedirs(os.path.join(tmp, "bot_logs")); inbox = os.path.join(tmp, "inbox"); os.makedirs(inbox)
    STORE, NOTES = {}, []
    async def notify(m): NOTES.append(m)
    async def fetch_venue_liq_price(m, d):
        return {"liq": 100.0, "source": "onchain_position_account", "entry": 110.0, "size_usd": 500.0,
                "collateral_usd": 25.0, "leverage": 20.0} if (m, d) == ("SOL-PERP", "long") else None
    PRICE = {"v": 109.0}
    async def _market_mark_price(m, *a): return PRICE["v"]
    async def _market_mark_price_cached(m, max_age=None): return await _market_mark_price(m)   # shared cache bypassed here
    ns = {"os": os, "json": json, "time": time, "asyncio": asyncio, "datetime": datetime,
          "EST": ZoneInfo("America/New_York"), "_BOT_HOME": tmp, "INBOX_DIR": inbox,
          "WS_ROOT": tmp, "WS_BOT_LOGS": os.path.join(tmp, "bot_logs"), "WS_JSON_RELAY": os.path.join(tmp, "json_relay"),
          "load_positions": lambda: STORE, "load_position": lambda m="SOL-PERP": STORE.get(m),
          "save_position": lambda d, market=None: STORE.__setitem__(market, d),
          "notify": notify, "fetch_venue_liq_price": fetch_venue_liq_price, "_market_mark_price": _market_mark_price,
          "_market_mark_price_cached": _market_mark_price_cached,
          "calc_breakeven_price": lambda e, d, s, b: e, "calc_exit_floor": lambda l, d, entry=None, tp=None, **k: l + 1,
          "calc_auto_tp_sl": lambda e, d, l: (e * 1.04, e * 0.98), "_perp_log_param_change": lambda *a, **k: None,
          "PERP_MARKETS_COVERED": ["SOL-PERP", "BTC-PERP", "ETH-PERP"], "PERPS_DRY_RUN": False}
    import os as _os; ns.setdefault("os", _os); _h = src.index("def _envv("); exec(src[_h:src.index("\n\n", _h)], ns)  # blank-as-unset helper used by the extracted code
    exec(block, ns)
    log = os.path.join(tmp, "bot_logs", "owner_positions.jsonl")
    def events(): return [json.loads(l)["event"] for l in open(log)] if os.path.exists(log) else []
    async def main():
        # detect -> pending, packet, ping
        assert await ns["_owner_pos_perps_adopt"]("SOL-PERP", "long")
        p = STORE["SOL-PERP"]; o = p["owner_position"]
        assert o["state"] == "pending" and o["source"] == "unknown" and p["size_usd"] == 500.0 and p["leverage"] == 20.0
        assert p["tp_price"] is None and any("didn't open" in n for n in NOTES)
        assert any(f.startswith("perps_bot - owner_position_SOL") for f in os.listdir(inbox))
        # leave via owner command (case-insensitive target)
        with open(ns["OWNER_POS_DECISIONS_FILE"], "a") as f:
            f.write(json.dumps({"ts": time.time(), "target": "SOL", "decision": "leave"}) + "\n")
        decs = ns["_owner_pos_decisions"]()
        assert ns["_owner_pos_matches"]("SOL-PERP", o, decs[-1]["target"])
        assert await ns["_owner_pos_decide"]("SOL-PERP", p, "leave", "owner_command")
        assert p["owner_position"]["state"] == "leave" and p["size_usd"] == 500.0
        # second: timeout -> manage with auto TP/SL, size/lev untouched
        os.remove(ns["OWNER_POS_DECISIONS_FILE"])
        STORE.clear(); await ns["_owner_pos_perps_adopt"]("SOL-PERP", "long")
        p = STORE["SOL-PERP"]; p["owner_position"]["detected_ts"] -= 3601
        PRICE["v"] = 109.0
        ns["OWNER_POS_SCAN_S"] = 1e9
        t = asyncio.create_task(ns["owner_position_perps_loop"]()); await asyncio.sleep(0.2); t.cancel()
        assert p["owner_position"]["state"] == "managed" and p["tp_price"] and p["sl_price"]
        assert p["size_usd"] == 500.0 and p["leverage"] == 20.0
        # third: near liquidation -> early take-over before the timeout
        STORE.clear(); await ns["_owner_pos_perps_adopt"]("SOL-PERP", "long"); p = STORE["SOL-PERP"]
        PRICE["v"] = 101.0   # (101-100)/(110-100) = 0.10 <= 0.15
        t = asyncio.create_task(ns["owner_position_perps_loop"]()); await asyncio.sleep(0.2); t.cancel()
        assert p["owner_position"]["state"] == "managed"
        ev = events()
        for e in ("detected", "decision", "timeout", "take_over", "early_take_over"):
            assert e in ev, (e, ev)
        print("owner positions perps: all tests passed")
    asyncio.run(main())
