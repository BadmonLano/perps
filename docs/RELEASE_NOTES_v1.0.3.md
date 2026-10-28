# Perps v1.0.3

Profit % made honest across leverage, plus numbers-first open guidance for the SI. Paper stays the default; the 20x leverage cap and every other default are unchanged except the trail scaling below.

## Units, shown everywhere
- `tp_pct` / `sl_pct` are **% of price move from entry**. P/L and the trail are **% of margin (collateral)**.
- Fill alerts, acks and the dashboard now show both: `TP: $182.50 (+2.0% price, +40% margin at 20x)`.
- New ack fields on a fill: `tp_price_pct`, `tp_margin_pct`, `sl_price_pct`, `sl_margin_pct`, `sl_liq_fraction`, `warning`.
- Dashboard SL / TP cells read `price (+x% px, +y% mgn)`.

## Trail scales with leverage
A fixed 20 / 15 margin trail was a 4% / 3% price move at 5x but 0.4% / 0.3% at 50x, so it fired on noise. The base values are now the values **at 5x** and scale with leverage (`PERP_TRAIL_LEV_SCALE`, default `sqrt`, relative to `PERP_TRAIL_REF_LEVERAGE=5`; never below the base, giveback capped at 50 points). The trail tiers scale the same way.

| Leverage | Trail arm (margin) | Giveback (pts) | Arm / giveback in price |
|---|---|---|---|
| 5x | +20 | 15 | 4.0% / 3.0% |
| 20x | +40 | 30 | 2.0% / 1.5% |
| 50x | +63 | 47 | 1.3% / 0.95% |

`linear` keeps the same price move at every leverage; `off` restores the old fixed behaviour. A per-position `trail_activate_pct` / `trail_drop_pct` from `update_params` is used as given.

## Trail on after-fee P/L
- The trail and its peak now run on **net** % of collateral (round-trip fees plus borrow accrued so far), the same basis as the closed-trade ledger. The exit-suggestion sensor uses the same basis.
- The monitor line shows both: `gross +41.2% net +38.6% ($+7.72)`. TP, SL-warning and floor alerts show gross and net.

## SL near liquidation warning
At open the bot warns (never blocks) when the SL sits past 60% of the way from entry to liquidation (`PERP_SL_LIQ_WARN_FRAC`, `0` = off). Example: 50x with `sl_pct 1.5` sits at 75% and warns; 20x with `sl_pct 1` sits at 20%.

## Docs and skills
- perps-boss SKILL.md: unit lines and a 5x / 20x / 50x table, the signal example now fits the 20x cap (0.5 SOL at 20x, `tp_pct 2`, `sl_pct 1`), trail units and scaling.
- New perps-boss section "Deciding Whether To Open": the SI may use outside resources to finalize an open, leaning on raw numbers (price, funding, OI, volume, liquidation levels, flows) over headlines or narrative.
- README examples labelled as price %; `.env.example` and the console Settings screen document the new knobs.

## Tests
New `bots/tests/test_profit_units.py` (7 tests). Full suite 40/40, console `--smoke` passes.

## Upgrading
Pull and restart when convenient. `bots/.env` is untouched; add `PERP_TRAIL_LEV_SCALE=off` to keep the old fixed trail. Open positions keep their stored peak; it moves to the net basis on the next new high.
