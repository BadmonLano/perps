"""Offline tests for the trade autopsy ("what went wrong" per closed trade).
Extracts the TRADE AUTOPSY block from perps.py (or PERPS_FILE) and runs it on stubs:
no network, no wallet, no Telegram, temp folders only.
Run: python -m unittest bots/tests/test_trade_autopsy.py  (or via unittest discover)."""
import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PATH = os.environ.get("PERPS_FILE") or os.path.join(HERE, "..", "perps.py")
EST = ZoneInfo("America/New_York")


def _block():
    src = open(SRC_PATH, encoding="utf-8").read()
    a = src.index("# ── TRADE AUTOPSY")
    b = src.index("async def perp_counterfactual_loop():")
    return src[a:b]


BLOCK = _block()


class Env:
    """A fresh namespace + temp folders per test."""
    def __init__(self, env=None, llm=None, brain=True):
        self.tmp = tempfile.mkdtemp(prefix="ta_test_")
        self.logs = os.path.join(self.tmp, "bot_logs"); os.makedirs(self.logs)
        self.inbox = os.path.join(self.tmp, "inbox"); os.makedirs(self.inbox)
        self.done = os.path.join(self.tmp, "inbox_results", "done"); os.makedirs(self.done)
        self.learn, self.llm_calls, self.saved = [], [], []
        self.llm = llm
        self.old_env = {k: os.environ.get(k) for k in ("AUTOPSY_ON", "AUTOPSY_SI_ENABLED", "AUTOPSY_SI_MAX_PER_DAY")}
        for k in self.old_env:
            os.environ.pop(k, None)
        for k, v in (env or {}).items():
            os.environ[k] = v

        async def gen(prompt, **kw):
            self.llm_calls.append(prompt)
            if isinstance(self.llm, Exception):
                raise self.llm
            return self.llm if isinstance(self.llm, str) else ""

        def save_json(p, d):
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, default=str)

        self.ns = {"os": os, "json": json, "re": re, "time": time, "asyncio": asyncio, "datetime": datetime,
                   "timedelta": timedelta, "EST": EST,
                   "_pos_base": lambda k: (str(k).upper().split("#")[0] if str(k).upper().endswith("-PERP") or "#" in str(k)
                                           else str(k).upper() + "-PERP"),
                   "save_position": lambda d, market=None: self.saved.append((market, json.loads(json.dumps(d, default=str)))),
                   "INBOX_PATH": self.inbox, "INBOX_RESULTS_DONE": self.done,
                   "PNL_FILE": os.path.join(self.logs, "perp_pnl.json"),
                   "PERP_PARAM_CHANGE_LOG": os.path.join(self.logs, "perps_param_changes.jsonl"),
                   "TRAIL_ACTIVATE_PCT": 20, "PERP_MAX_LEVERAGE": 20.0, "_perp_get_override": lambda k, d: d,
                   "_news_lean_from_bucketed": lambda n: n.get("lean", "mixed"), "_perp_chain_news": lambda c: {"lean": "mixed"},
                   "_perp_ollama_generate": gen, "PERPS_OLLAMA_MODEL": "test-model", "_s27_save_json": save_json,
                   "_perp_brain_enabled": lambda: brain, "_PERP_LAST_LLM_SOURCE": "grok:mock",
                   "WS_BOT_LOGS": self.logs, "_BOT_HOME": self.tmp}
        exec(BLOCK, self.ns)
        self.ns["_ta_learnings_append"] = lambda t: self.learn.append(t) or True

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def trade(self, n=1, pnl=-30.0, lev=10, peak=None, hold=90, cat="closed", reason="agent close",
              events=None, lean="mixed", side="long", liquidated=False):
        o = datetime(2026, 10, 17, 9, 0, tzinfo=EST) + timedelta(hours=n)
        c = o + timedelta(minutes=hold)
        pos = {"market": "SOL-PERP", "direction": side, "leverage": lev, "collateral": 10, "timestamp": o.isoformat(),
               "tx_sig": f"TX{n}", "tp_price": 160, "sl_price": 145, "events": events or [],
               "entry_packet": None}
        ep = {"ts": c.isoformat(), "market": "SOL-PERP", "direction": side, "leverage": lev, "entry_price": 150,
              "exit_price": 148, "size_usd": 10 * lev, "pnl_pct": pnl, "pnl_usd": pnl / 10, "peak_pct": peak,
              "hold_minutes": hold, "category": cat, "reason": reason, "liquidated": liquidated,
              "fingerprint": {"news_lean": lean, "lev_bucket": "extreme"},
              "entry_decision_context": {"reason": "dip buy", "conviction": 6}}
        return pos, ep

    def run(self, coro):
        return asyncio.run(coro)

    def records(self):
        return self.ns["_ta_read_jsonl"](self.ns["_TA_OUT"])

    def packets(self):
        return [f for f in os.listdir(self.inbox) if f.startswith("perps_bot - trade_autopsy_")]


class Base(unittest.TestCase):
    def mk(self, **kw):
        e = Env(**kw); self.addCleanup(e.close); return e


class Rules(Base):
    def rc(self, e, **kw):
        pos, ep = e.trade(**kw)
        b = e.ns["_ta_bundle"]({"episode": ep, "position": pos, "key": "k"})
        return e.ns["_ta_rules"](b)[0], b

    def test_categories(self):
        e = self.mk()
        self.assertEqual(self.rc(e, pnl=12)[0], "worked_as_planned")
        self.assertEqual(self.rc(e, cat="liquidated", liquidated=True, pnl=-100)[0], "oversized")
        self.assertEqual(self.rc(e, peak=25)[0], "gave_back_profit")
        self.assertEqual(self.rc(e, hold=8, events=[{"kind": "sl_hit"}])[0], "stop_too_tight")
        self.assertEqual(self.rc(e, events=[{"kind": "flip", "detail": "funding turned against"}])[0], "ignored_flip")
        self.assertEqual(self.rc(e, lean="bearish")[0], "thesis_wrong")
        self.assertEqual(self.rc(e, lev=15)[0], "oversized")
        self.assertEqual(self.rc(e)[0], "noise_within_plan")

    def test_macro_fees_news(self):
        e = self.mk()
        with open(os.path.join(e.logs, "macro_prints.json"), "w") as f:
            json.dump({"period": "2026-09", "print_first_seen_at": datetime(2026, 10, 17, 10, 30, tzinfo=EST).isoformat()}, f)
        self.assertEqual(self.rc(e, n=1)[0], "macro_shock")
        os.remove(os.path.join(e.logs, "macro_prints.json"))
        pos, ep = e.trade(n=2)
        with open(e.ns["PNL_FILE"], "w") as f:
            json.dump({"trades": [{"market": "SOL-PERP", "timestamp": ep["ts"], "gross_pnl_usd": 0.4, "fees_usd": 0.9}]}, f)
        self.assertEqual(self.rc(e, n=2)[0], "fees")
        os.remove(e.ns["PNL_FILE"])
        e.ns["_perp_chain_news"] = lambda c: {"lean": "bearish"}
        self.assertEqual(self.rc(e, n=3)[0], "news_shock")

    def test_bundle_inputs(self):
        e = self.mk()
        pos, ep = e.trade(n=1)
        with open(e.ns["PERP_PARAM_CHANGE_LOG"], "w") as f:
            for ts in (pos["timestamp"], ep["ts"], "2026-10-16T09:00:00-04:00"):
                f.write(json.dumps({"ts": ts, "market": "SOL-PERP", "param": "sl", "old": 1, "new": 2}) + "\n")
        for stamp in ("20261017_093000", "20261017_095900", "20261017_120000"):
            with open(os.path.join(e.done, f"perps_bot - perp_SOL_{stamp}.json"), "w") as f:
                json.dump({"built_at": stamp, "mode": "hunt", "news": {"counts": {"net": 1}}}, f)
        b = e.ns["_ta_bundle"]({"episode": ep, "position": pos, "key": "k"})
        self.assertEqual(len(b["param_changes"]), 2)            # only inside the hold window
        self.assertEqual(b["entry_packet_file"], "perps_bot - perp_SOL_20261017_095900.json")  # newest at/before open
        self.assertEqual(b["entry_packet"]["mode"], "hunt")


class Guards(Base):
    def test_param_lesson_becomes_proposal(self):
        e = self.mk()
        g = e.ns["_ta_guard_lesson"]
        self.assertTrue(g("Raise the PERP_MAX_LEVERAGE default to 80x").startswith("Proposal for the owner"))
        self.assertTrue(g("Lower the trail activation to 12%").startswith("Proposal for the owner"))
        self.assertEqual(g("Wait for the retest before entering."), "Wait for the retest before entering.")

    def test_parse(self):
        e = self.mk()
        p = e.ns["_ta_parse"]
        good = {"verdict": "Long into bearish news, stopped fast", "root_cause": "thesis_wrong",
                "evidence": ["news bearish"], "lesson": "Next time require news to agree.", "confidence": 0.7}
        self.assertEqual(p(json.dumps(good))["root_cause"], "thesis_wrong")
        self.assertEqual(p("```json\n" + json.dumps(good) + "\n```")["confidence"], 0.7)
        self.assertIsNone(p("not json"))
        self.assertIsNone(p(""))
        self.assertIsNone(p(json.dumps({**good, "root_cause": "bad_luck"})))
        self.assertIsNone(p(json.dumps({**good, "lesson": ""})))

    def test_events(self):
        e = self.mk()
        pos = {}
        for i in range(45):
            e.ns["_perp_pos_event"]("SOL-PERP", pos, "tp_reached", 150 + i, 10)
        self.assertEqual(len(pos["events"]), 40)
        e.ns["_perp_pos_event"]("SOL-PERP", pos, "flip", detail="x")
        e.ns["_perp_pos_event"]("SOL-PERP", pos, "flip", detail="x")
        self.assertEqual(sum(1 for v in pos["events"] if v["kind"] == "flip"), 1)
        e.ns["save_position"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk"))
        e.ns["_perp_pos_event"]("SOL-PERP", pos, "sl_hit")   # never raises


class Worker(Base):
    def enqueue(self, e, **kw):
        pos, ep = e.trade(**kw)
        e.ns["perp_autopsy_enqueue"](pos, ep)
        return pos, ep

    def test_llm_off_rules_and_idempotent(self):
        e = self.mk(env={"AUTOPSY_SI_ENABLED": "false"}, llm="{}")
        pos, ep = self.enqueue(e, lean="bearish")
        e.run(e.ns["_ta_worker_once"]())
        r = e.records()
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["source"], r[0]["root_cause"]), ("rules", "thesis_wrong"))
        self.assertEqual(e.llm_calls, [])
        self.assertEqual(len(e.packets()), 1)
        self.assertEqual(len(e.learn), 1)
        self.assertIn("thesis_wrong", e.learn[0])
        self.assertFalse(os.path.exists(e.ns["_TA_QUEUE"]))
        e.ns["perp_autopsy_enqueue"](pos, ep)                 # same position queued again
        e.run(e.ns["_ta_worker_once"]())
        self.assertEqual((len(e.records()), len(e.packets()), len(e.learn)), (1, 1, 1))

    def test_llm_mocked_json(self):
        ans = {"verdict": "Held through a funding flip and gave it back", "root_cause": "ignored_flip",
               "evidence": ["flip alert at 10:12", "closed -30%"], "lesson": "Next time trim on the first flip alert.",
               "proposal": "lower the default trail activation to 12%", "confidence": 0.8}
        e = self.mk(llm=json.dumps(ans))
        self.enqueue(e, events=[{"kind": "flip", "detail": "funding against"}])
        e.run(e.ns["_ta_worker_once"]())
        r = e.records()[0]
        self.assertEqual((r["source"], r["root_cause"], r["rules_root_cause"]), ("grok:mock", "ignored_flip", "ignored_flip"))
        self.assertTrue(r["proposal"].startswith("Proposal for the owner (not applied)"))
        self.assertEqual(len(e.llm_calls), 1)
        pkt = json.load(open(os.path.join(e.inbox, e.packets()[0])))
        self.assertEqual(pkt["_type"], "perp_trade_autopsy")

    def test_bad_json_and_error_fall_back(self):
        e = self.mk(llm="garbage {not json")
        self.enqueue(e, n=1)
        e.run(e.ns["_ta_worker_once"]())
        r = e.records()[0]
        self.assertEqual(r["source"], "rules"); self.assertIn("not valid JSON", r["si_note"])
        e.llm = RuntimeError("timeout")
        self.enqueue(e, n=2)
        e.run(e.ns["_ta_worker_once"]())
        self.assertEqual(e.records()[1]["source"], "rules")
        self.assertIn("SI error", e.records()[1]["si_note"])

    def test_cost_cap(self):
        good = json.dumps({"verdict": "v", "root_cause": "oversized", "lesson": "Size down.", "evidence": []})
        e = self.mk(env={"AUTOPSY_SI_MAX_PER_DAY": "1"}, llm=good)
        self.enqueue(e, n=1); self.enqueue(e, n=2)
        e.run(e.ns["_ta_worker_once"]())
        r = e.records()
        self.assertEqual(len(e.llm_calls), 1)
        self.assertEqual([x["source"] for x in r], ["grok:mock", "rules"])
        self.assertIn("daily SI cap", r[1]["si_note"])

    def test_brain_off(self):
        e = self.mk(llm="{}", brain=False)
        self.enqueue(e)
        e.run(e.ns["_ta_worker_once"]())
        self.assertEqual(e.llm_calls, [])
        self.assertIn("SI brain off", e.records()[0]["si_note"])

    def test_modes(self):
        e = self.mk(env={"AUTOPSY_SI_ENABLED": "false"})
        self.enqueue(e, n=1, pnl=15)                          # win, mode=loss -> dropped
        e.run(e.ns["_ta_worker_once"]())
        self.assertEqual(e.records(), [])
        self.assertFalse(os.path.exists(e.ns["_TA_QUEUE"]))
        os.environ["AUTOPSY_ON"] = "all"
        self.enqueue(e, n=2, pnl=15)
        e.run(e.ns["_ta_worker_once"]())
        self.assertEqual(e.records()[0]["root_cause"], "worked_as_planned")
        os.environ["AUTOPSY_ON"] = "off"
        self.enqueue(e, n=3)
        self.assertFalse(os.path.exists(e.ns["_TA_QUEUE"]))

    def test_cap_per_pass_and_failure_isolated(self):
        e = self.mk(env={"AUTOPSY_SI_ENABLED": "false"})
        for i in range(5):
            self.enqueue(e, n=i + 1)
        e.run(e.ns["_ta_worker_once"]())
        self.assertEqual(len(e.records()), 3)                 # 3 per pass, rest stay queued
        e.run(e.ns["_ta_worker_once"]())
        self.assertEqual(len(e.records()), 5)
        e.ns["_ta_learnings_append"] = lambda t: (_ for _ in ()).throw(RuntimeError("locked"))
        self.enqueue(e, n=9)
        e.run(e.ns["_ta_worker_once"]())                      # learnings failure does not break the review
        self.assertEqual(len(e.records()), 6)
        e.ns["_ta_bundle"] = lambda job: (_ for _ in ()).throw(ValueError("bad job"))
        self.enqueue(e, n=10)
        for _ in range(3):
            e.run(e.ns["_ta_worker_guarded"]())               # retried, then dropped after 3 attempts
        self.assertFalse(os.path.exists(e.ns["_TA_QUEUE"]))

    def test_enqueue_never_raises(self):
        e = self.mk()
        e.ns["_TA_QUEUE"] = os.path.join(e.tmp, "queue_is_a_folder")
        os.makedirs(e.ns["_TA_QUEUE"])                        # appending to a folder fails on every OS
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):                 # the expected error is logged, not shown
            e.ns["perp_autopsy_enqueue"]({"timestamp": object()}, {"market": "SOL-PERP"})   # no exception
        self.assertIn("enqueue err", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
