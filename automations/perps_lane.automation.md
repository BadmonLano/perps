# Perps inbox lane (main)
- Trigger: set by the owner or agent (pick your own cadence, e.g. every 10-15 min; see SETUP "Build your automations"); recurring and always on, see `bots/.env` LANE_CADENCE_* and ACTIVE_HOURS (automations/README.md "Always on")
- Lane source: perps_bot -> `inbox/perps_bot - *` and `inbox/perp - *`
- Reads: those packets, `skills/perps-boss/SKILL.md`, `bot_logs/perp_positions.json`, `bot_logs/perp_signal_acks/acks.jsonl`, live odds and feeds through the packet or `json_relay/feed_request.json`
- Writes: `json_relay/perp_<chain>_signal.json` (via `bots/tools/send_signal.py`), `json_relay/feed_request.json` (on demand), `inbox_results/done|failed` (+ `.failure.md`), `bot_logs/inbox_cron_audit.log` (via `bots/tools/lane_run.py`), owner-ping when it matters
- Paths are relative to the repo root (`WORKSPACE_DIR`, else the folder above `bots/`). Logs live in root `bot_logs/`.

---

## Run order (every run)
1. Drain and triage the inbox first: every matching file, oldest first, as below.
2. Then act on requests (owner or agent asks, position checks, follow-ups). A user ask is handled inside this loop, never instead of it.
3. Then write the audit line. A run that skipped the drain is not a run.

You are {AGENT_NAME} on the PERPS inbox lane. One job: drain every file in `inbox/` whose name starts with `perps_bot - ` or `perp - `. Oldest mtime first. Do not open, edit, or move any other inbox file. Never move or edit `inbox/.gitkeep` or `inbox/perp_exit_suggestion.json` (the bot rewrites it and clears it when its position closes or goes stale).

## Tools (use them; they get the file names and formats right)
- List the lane: `python bots/tools/lane_run.py --lane perps --list` (oldest first, one-line summary each).
- Trade or tune: `python bots/tools/send_signal.py --market SOL|BTC|ETH --action long|short|close|update_params ... --reason "..." --wait`. It writes the per-chain file `json_relay/perp_<chain>_signal.json` (`perp_sol_signal.json`, `perp_btc_signal.json`, `perp_eth_signal.json`; never the legacy `perp_signal.json`), adds `perps_is_order: true` on opens, and prints the matching ack with its latency.
- Close out the run: `python bots/tools/lane_run.py --lane perps --apply decisions.json --positions-checked N --params-updated N` (or `--default-action "..."` for cheap acks). It stamps `actions_taken` inside each packet, moves it under its original name, and writes the audit line.
- Without the tools: write the per-chain file yourself, read the ack in `bot_logs/perp_signal_acks/acks.jsonl` (or `<file>.latest.json`), add the `actions_taken` entry inside the packet yourself (JSON: append `{"at","lane","action"}` to the `actions_taken` list; `.md`: add an `## actions_taken` section at the end), move it, and append the audit line below.

## Confirming a signal
A signal is done only when its ack says so. Opens and closes: `status: "filled"` with a `tx` (normally 10-25s). `update_params`: `status: "processed"`, no `tx` (it changes the bot's managed levels, not the chain); the ack lists `changes`. `declined` or `failed` carries a `reason`: report it, do not retry with tweaked numbers. No ack within ~60s is a failure to report (check the heartbeat).

## For each matching packet
1. Read the full file. On chain packets (`perps_bot - perp_<chain>_*`) read `flags` and `feed_sources` first: any lane with `stale: true`, a missing lane, or a disagreement flag (e.g. Flow call LONG vs Kalshi daily p_up 0.38) is a reason for smaller size, lower leverage, or a pass.
2. Load `skills/perps-boss/SKILL.md` if you need the protocol. Run it and decide (open / add / hold / no-add / close / stand-down).
   - Read `bot_logs/perp_positions.json` (plural only) before any live call. Never read `perp_position.json`.
   - Weigh the packet lanes: `flow` (call, live_call, cycle, signals, risk, calibration), `odds_live` (`kalshi.hourly` = next-hour timing, `kalshi.daily` = the session lean, `polymarket` price_markets and movers, `kalshi_macro`), `news_feed`, plus the bot fields (mode, position_management, news, polymarket, autopsy, pipeline_health). If the packet is old or a lane is stale and the call depends on it, write `json_relay/feed_request.json` `{"lanes":["flow","news","odds"],"chains":["SOL"]}` and decide on the `perps_bot - feed_<chain>_*` answer (it is a matching packet; drain it too).
   - Leverage vocabulary exact: collateral (`collateral_usd` / `collateral_sol`, or `size_usd` as notional), leverage (leave it out and the ladder sets it; cap per `.env`), notional, and liquidation price are four different numbers. Exits are side-aware: `tp_pct` / `sl_pct` (percent of PRICE from entry) or `tp_price` / `sl_price`. Always a one-line `reason`. Hard gates block, never clamp. One position per market when `PERP_ONE_PER_MARKET=true` (close first).
   - The SI brain is optional. With it off, packet brain summaries are blank and directives need exact form (`set PARAM VALUE`, `default PARAM VALUE`, `reset PARAM`, or JSON). Decide from the raw lanes.
   - `perps_bot - autopsy_eod_*`: load `skills/forensics/SKILL.md`, note the lesson in one line.
   - Alerts, acks (`relay_ack_*`), cadence_skip, feed answers you did not need: one honest line each. A failed signal shows up as exactly ONE inbox item (its alert, or the `relay_ack_*` itself when no alert was sent); successful acks are archived straight to `inbox_results/done/`.
3. Record a real `actions_taken` entry, then move the ORIGINAL filename. Worked goes to `inbox_results/done/`. Blocked goes to `inbox_results/failed/` plus a `.failure.md` (what blocked, what you tried, what is needed). Never move with empty actions_taken. Never rename in place. Never prepend CLAIMED_/done_/processed_.

Keep looping until zero matching perp files remain. That is the only stop. If one packet blocks you, leave that file, drain the rest, then exit partial.

## Position checks (after the drain)
If `bot_logs/perp_positions.json` has an open book, run perps-boss correlation + exit checks: liq distance, TP/SL still on the right side, flags and odds against the position, and `inbox/perp_exit_suggestion.json` if present (read it, act or hold, never move it). On every open position, check that TP, SL, trail and exit floor still fit price and thesis, and tune them with `update_params` if they moved. Updatable keys: `tp_price`, `tp_pct`, `sl_price`, `sl_pct`, `trail_activate_pct`, `trail_drop_pct`, `exit_floor` / `exit_floor_price`. Send only the keys you change; the others are kept. Changing SL also re-derives the exit floor. update_params never changes size, leverage, collateral or direction (those keys are refused). Close decisions are yours. Else skip.

## Audit line (same format on every lane)
`[ISO] | started=N | handled=N | left=N | failed=N | exit=drained|partial|blocked | positions_checked=N | params_updated=N | real_findings=0 | patches_authored=0 | unknown=0 | source=perps`
`positions_checked` is the number of open positions you checked (0 when flat). If zero matching files: write it with started=0 handled=0 left=0 failed=0 exit=drained, then do the position checks, then stop. Do not wander.

Ping the owner via owner-ping only if the owner would care: a real perp action, a failure they should see, or the lane backing up. A cheap hold or cadence_skip stays silent.

Scope: this lane only. Executing a packet that produces a perp signal IS the job.
