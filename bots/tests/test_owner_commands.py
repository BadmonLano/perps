"""Offline tests for owner_commands.py + roast_now(). No network, no wallet, no SI calls.
Run: python test_owner_commands.py   (from bots/ or bots/tests/)"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest

_D = os.path.dirname(os.path.abspath(__file__))
for _c in (_D, os.path.join(_D, "..", "sub-bots"), os.path.join(_D, "sub-bots")):
    if os.path.isfile(os.path.join(_c, "owner_commands.py")):
        SRC = os.path.abspath(_c)
        break
else:
    raise SystemExit("owner_commands.py not found")
sys.path.insert(0, SRC)
spec = importlib.util.spec_from_file_location("owner_commands_t", os.path.join(SRC, "owner_commands.py"))
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)
run = asyncio.run


class Parse(unittest.TestCase):
    def test_slash_commands_any_case(self):
        self.assertEqual(oc.parse_command("/roast"), ("roast", None))
        self.assertEqual(oc.parse_command("/ROAST"), ("roast", None))
        self.assertEqual(oc.parse_command("/Recap@SomeBot"), ("recap", None))
        self.assertEqual(oc.parse_command("/autopsy"), ("autopsy", "last"))
        self.assertEqual(oc.parse_command("/AUTOPSY $SOL"), ("autopsy", "SOL"))
        self.assertEqual(oc.parse_command("/autopsy last"), ("autopsy", "last"))

    def test_plain_text(self):
        self.assertEqual(oc.parse_command("Roast. Be honest."), ("roast", None))
        self.assertEqual(oc.parse_command("give me a recap"), ("recap", None))
        self.assertEqual(oc.parse_command("autopsy on $eth please"), ("autopsy", "eth"))
        self.assertEqual(oc.parse_command("Autopsy please"), ("autopsy", "last"))
        self.assertEqual(oc.parse_command("hello"), ("help", None))
        self.assertEqual(oc.parse_command("/start"), ("help", None))
        self.assertIsNone(oc.parse_command("   "))


class Config(unittest.TestCase):
    def test_default_off(self):
        self.assertEqual(oc.mode({}), "off")
        self.assertEqual(oc.mode({"OWNER_COMMANDS": "Telegram"}), "telegram")
        self.assertEqual(oc.mode({"OWNER_COMMANDS": "bogus"}), "off")

    def test_refuses_shared_token(self):
        env = {"OWNER_COMMANDS_TG_TOKEN": "A", "TELEGRAM_TOKEN": "A", "TELEGRAM_CHAT_ID": "1"}
        self.assertFalse(oc.tg_config(env)["ok"])
        env = {"OWNER_COMMANDS_TG_TOKEN": "B", "WARDEN_TELEGRAM_TOKEN": "B", "TELEGRAM_CHAT_ID": "1"}
        self.assertFalse(oc.tg_config(env)["ok"])
        self.assertFalse(oc.tg_config({"TELEGRAM_CHAT_ID": "1"})["ok"])
        c = oc.tg_config({"OWNER_COMMANDS_TG_TOKEN": "C", "TELEGRAM_TOKEN": "A", "TELEGRAM_CHAT_ID": "1"})
        self.assertTrue(c["ok"]) ; self.assertEqual(c["chat_id"], "1")

    def test_status_never_prints_token(self):
        s = json.dumps(oc.status({"OWNER_COMMANDS": "telegram", "OWNER_COMMANDS_TG_TOKEN": "SECRET123",
                                  "TELEGRAM_CHAT_ID": "1"}))
        self.assertNotIn("SECRET123", s)


class WithOverseer(unittest.TestCase):
    """Real roast_now() from the *_autoagent.py next to owner_commands.py, judges stubbed."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.o = oc.overseer()
        self._saved = {k: getattr(self.o, k) for k in
                       ("LOGS", "INBOX_RESULTS", "collect_inputs", "run_judgment", "notify", "OVERSEER_LOG")}
        self.o.LOGS = os.path.join(self.tmp, "bot_logs"); os.makedirs(self.o.LOGS)
        self.o.OVERSEER_LOG = os.path.join(self.tmp, "overseer_test.log")   # never the real log
        self.o.INBOX_RESULTS = os.path.join(self.tmp, "inbox_results")
        self.sent, self.judged = [], []
        self.o.collect_inputs = lambda: {"perps": {}}

        async def judge(inputs, top):
            self.judged.append(1)
            return {"verdict": {"headline": "Held <too> long", "score_0_to_10": 6,
                                "what_was_weak": ["chased a top"], "what_was_strong": ["sat out rugs"],
                                "improvements_for_tomorrow": ["size down"]},
                    "meta": {"judge": "stub"}}

        async def notify(msg):
            self.sent.append(msg)
        self.o.run_judgment, self.o.notify = judge, notify
        os.environ["OWNER_COMMANDS_DIR"] = os.path.join(self.tmp, "owner_commands")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.o, k, v)
        os.environ.pop("OWNER_COMMANDS_DIR", None)

    def test_overseer_file_untouched(self):
        with open(self.o.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("roast_now", src)              # the Overseer is reused, never edited

    def test_roast_then_cooldown(self):
        r = run(oc.roast_now("roast"))
        self.assertTrue(r["ok"]); self.assertIn("Roast", r["text"]); self.assertIn("&lt;too&gt;", r["text"])
        self.assertTrue(os.path.isfile(r["reply_path"])); self.assertEqual(len(self.sent), 1)
        r2 = run(oc.roast_now("recap"))
        self.assertFalse(r2["ok"]); self.assertEqual(r2["reason"], "cooldown")
        self.assertEqual(len(self.judged), 1)          # no second SI call inside the cooldown

    def test_nightly_files_untouched(self):
        run(oc.roast_now("recap", send=False))
        self.assertEqual(self.sent, [])
        names = os.listdir(self.o.LOGS)
        self.assertEqual(sorted(n for n in names if n != "owner_commands.log"), ["owner_roast_last.json"])   # no summary / history / packet

    def test_autopsy_last_and_ticker(self):
        with open(os.path.join(self.o.LOGS, "perp_episodes.jsonl"), "w") as f:
            f.write(json.dumps({"market": "ETH-PERP", "direction": "long", "pnl_pct": 24.0, "reason": "tp",
                                "leverage": 20, "size_usd": 200}) + "\n")
            f.write(json.dumps({"market": "SOL-PERP", "direction": "short", "pnl_pct": -8.5}) + "\n")
        r = run(oc.roast_now("autopsy", "last", send=False))
        self.assertIn("SOL-PERP", r["text"])
        r = run(oc.roast_now("autopsy", "ETH", send=False))
        self.assertIn("ETH-PERP", r["text"]); self.assertIn("Leverage: 20", r["text"])
        r = run(oc.roast_now("autopsy", "nope", send=False))
        self.assertIn("No closed trade", r["text"])
        self.assertEqual(self.judged, [])             # autopsy never calls the judges

    def test_file_source(self):
        d = os.environ["OWNER_COMMANDS_DIR"]; os.makedirs(d)
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("ROAST me\n")
        with open(os.path.join(d, "b.json"), "w") as f:
            json.dump({"text": "hello"}, f)
        self.assertEqual(run(oc.file_once()), 2)
        self.assertEqual(os.listdir(d), [])
        self.assertEqual(len(os.listdir(os.path.join(self.o.INBOX_RESULTS, "owner_commands", "done"))), 2)
        self.assertEqual(len(self.sent), 2)            # roast + help, via alert channels

    def test_telegram_owner_chat_only_and_409(self):
        calls = []
        updates = [{"update_id": 10, "message": {"chat": {"id": 999}, "text": "/roast", "message_id": 1}},
                   {"update_id": 11, "message": {"chat": {"id": 1}, "text": "/Autopsy", "message_id": 2}}]

        async def fake(tok, method, params, timeout=0):
            calls.append(method)
            if method == "getWebhookInfo":
                return {"ok": True, "result": {"url": ""}}
            if method == "getUpdates" and params.get("offset") == -1:
                return {"ok": True, "result": []}
            if method == "getUpdates":
                if calls.count("getUpdates") == 2:
                    return {"ok": True, "result": updates}
                raise oc.TgConflict("getUpdates")
            if method == "sendMessage":
                self.assertEqual(params["chat_id"], "1")
                return {"ok": True}
        why = run(oc.telegram_loop({"token": "T", "chat_id": "1"}, call=fake, max_polls=5))
        self.assertEqual(why, "conflict")              # stops on 409, never fights
        self.assertEqual(calls.count("sendMessage"), 1)  # stranger's chat ignored
        self.assertEqual(self.judged, [])              # the stranger's /roast never ran

    def test_webhook_means_no_polling(self):
        async def fake(tok, method, params, timeout=0):
            if method == "getWebhookInfo":
                return {"ok": True, "result": {"url": "https://example.invalid/hook"}}
            raise AssertionError("must not poll")
        self.assertEqual(run(oc.telegram_loop({"token": "T", "chat_id": "1"}, call=fake)), "webhook")


class OwnerPositions(unittest.TestCase):
    """/manage and /leave write a decision the bots pick up. No overseer needed."""
    def setUp(self):
        self.logs = tempfile.mkdtemp()
        os.environ.pop("OWNER_POS_DECISIONS_FILE", None)

    def _pending(self):
        with open(os.path.join(self.logs, "owner_positions_perps.json"), "w") as f:
            json.dump({"records": {"SOL-PERP": {"id": "op_sol_1", "state": "pending", "market": "SOL-PERP"},
                                   "ETH-PERP": {"id": "op_eth_1", "state": "pending", "market": "ETH-PERP"}}}, f)

    def _rows(self):
        p = os.path.join(self.logs, "owner_position_decisions.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def test_parse_any_case(self):
        self.assertEqual(oc.parse_command("/MANAGE $Pepe"), ("manage", "Pepe"))
        self.assertEqual(oc.parse_command("/leave sol"), ("leave", "sol"))
        self.assertEqual(oc.parse_command("/Leave@Bot SOL"), ("leave", "SOL"))
        self.assertEqual(oc.parse_command("/manage"), ("manage", None))

    def test_manage_and_leave_write_decisions(self):
        self._pending()
        r = oc.owner_position_decision("manage", "eth", logs=self.logs)
        self.assertTrue(r["ok"]); self.assertIn("ETH", r["text"])
        r = oc.owner_position_decision("leave", "SOL", logs=self.logs)
        self.assertTrue(r["ok"])
        rows = self._rows()
        self.assertEqual([(x["target"], x["decision"]) for x in rows], [("eth", "manage"), ("sol", "leave")])
        self.assertTrue(all(isinstance(x["ts"], float) for x in rows))

    def test_no_match_writes_nothing(self):
        self._pending()
        r = oc.owner_position_decision("manage", "btc", logs=self.logs)
        self.assertFalse(r["ok"]); self.assertIn("ETH", r["text"])
        self.assertEqual(self._rows(), [])

    def test_missing_target_lists_pending(self):
        self._pending()
        r = oc.owner_position_decision("leave", None, logs=self.logs)
        self.assertFalse(r["ok"]); self.assertIn("Usage", r["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
