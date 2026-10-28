"""v1.0.3: profit units. TP/SL are price %, trail is margin % net of fees and scales
with leverage, SL-near-liq warning fraction. Offline: the helpers are pulled from
bots/perps.py by AST, so nothing heavy is imported."""
import ast, math, os, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "perps.py")
NAMES = {"trail_lev_factor", "trail_levels", "margin_pct", "tp_sl_label", "tp_sl_pcts", "sl_liq_fraction"}


def _load(**over):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
    ns = {"math": math, "TRAIL_ACTIVATE_PCT": 20.0, "TRAIL_DROP_PCT": 15.0,
          "PERP_TRAIL_TIERS": [(50.0, 12.0), (100.0, 10.0), (200.0, 8.0)],
          "PERP_TRAIL_REF_LEVERAGE": 5.0, "PERP_TRAIL_LEV_SCALE": "sqrt", "PERP_TRAIL_DROP_MAX": 50.0}
    ns.update(over)
    exec(compile(ast.Module(body=fns, type_ignores=[]), SRC, "exec"), ns)
    assert NAMES <= set(ns), NAMES - set(ns)
    return ns


class ProfitUnits(unittest.TestCase):
    def test_trail_keeps_5x_values(self):
        m = _load()
        self.assertEqual(m["trail_levels"](5), (20.0, 15.0))
        self.assertEqual(m["trail_levels"](2), (20.0, 15.0))  # never below base

    def test_trail_scales_sqrt(self):
        m = _load()
        self.assertEqual(m["trail_levels"](20), (40.0, 30.0))
        a, d = m["trail_levels"](50)
        self.assertAlmostEqual(a, 63.25, places=1)
        self.assertAlmostEqual(d, 47.43, places=1)
        self.assertLessEqual(m["trail_levels"](250)[1], 50.0)  # giveback cap

    def test_trail_modes(self):
        self.assertEqual(_load(PERP_TRAIL_LEV_SCALE="off")["trail_levels"](50), (20.0, 15.0))
        lin = _load(PERP_TRAIL_LEV_SCALE="linear")["trail_levels"](20)
        self.assertEqual(lin, (80.0, 50.0))  # 4% price arm; giveback capped

    def test_trail_tiers_scale_and_override(self):
        m = _load()
        self.assertEqual(m["trail_levels"](20, peak_pct=100)[1], 24.0)  # tier 50 x2
        self.assertEqual(m["trail_levels"](20, pos={"trail_activate_pct": 30, "trail_drop_pct": 12}), (30.0, 12.0))

    def test_tp_sl_label_shows_margin(self):
        m = _load()
        self.assertEqual(m["tp_sl_label"]("tp", 102, 100, "long", 20), "TP: $102.00 (+2.0% price, +40% margin at 20x)")
        self.assertEqual(m["tp_sl_label"]("sl", 101, 100, "short", 50), "SL: $101.00 (-1.0% price, -50% margin at 50x)")
        self.assertEqual(m["tp_sl_label"]("tp", None, 100, "long", 5), "TP: none")
        self.assertEqual(m["tp_sl_pcts"](99, 100, "long", 5), (-1.0, -5.0))

    def test_sl_liq_fraction(self):
        f = _load()["sl_liq_fraction"]
        self.assertAlmostEqual(f(100, 98.5, 98.0, "long"), 0.75)   # 50x, sl 1.5% -> warn
        self.assertAlmostEqual(f(100, 99.0, 95.0, "long"), 0.2)    # 20x, sl 1% -> fine
        self.assertAlmostEqual(f(100, 101.5, 102.0, "short"), 0.75)
        self.assertIsNone(f(100, 99, 101, "long"))


class WiredIn(unittest.TestCase):
    def test_monitor_uses_net_and_scaled_trail(self):
        s = open(SRC, encoding="utf-8").read()
        self.assertIn('_t_act, _t_drop = trail_levels(leverage, st["peak_pct"], pos=existing)', s)
        self.assertIn('(st["peak_pct"] - net_lev_pct) >= _t_drop', s)
        self.assertIn('tp_str = tp_sl_label("tp"', s)
        self.assertIn("sl_margin_pct=_slm", s)
        self.assertIn('PERP_SL_LIQ_WARN_FRAC   = _env_float("PERP_SL_LIQ_WARN_FRAC", "SL_LIQ_WARN_FRAC", 0.6)', s)


if __name__ == "__main__":
    unittest.main()
