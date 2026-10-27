# Example signals

A signal is one small JSON file the agent writes to `json_relay/perp_<chain>_signal.json`
(`perp_sol_signal.json`, `perp_btc_signal.json` or `perp_eth_signal.json`). The bot reads it, checks
the hard gates, then fills it or declines it with a reason in `bot_logs/perp_signal_acks/acks.jsonl`.

| File | What it does |
|---|---|
| `signal_open_long.json` | Opens a SOL long with $11 collateral, 2% take-profit, 1% stop. Leverage is left out, so the ladder picks it. Opens need `"perps_is_order": true`. |
| `signal_update_params.json` | Moves the take-profit and stop on the open position (the ratchet). Never changes size or leverage. Ack status `processed`, no tx. |
| `signal_close.json` | Closes the whole position. Use `close_pct` for a partial close (a peel). |

The easiest way to write one is `bots/tools/send_signal.py`. It writes the file atomically, waits for
the ack and prints one JSON line:

```
python bots/tools/send_signal.py --market SOL --action long --collateral-usd 11 --tp-pct 2 --sl-pct 1 --reason "why" --wait
python bots/tools/send_signal.py --market SOL --action update_params --tp-pct 3 --sl-pct 0.8 --reason "ratchet" --wait
python bots/tools/send_signal.py --market SOL --action close --reason "thesis invalidated" --wait
python bots/tools/send_signal.py --market SOL --action long --collateral-usd 11 --reason "test" --dry-run   # print only, write nothing
```

Exit codes: 0 filled/processed/parked, 2 declined/failed/expired, 3 no ack before the timeout,
4 the previous signal for that chain has not been consumed yet. In paper mode every fill is simulated.
