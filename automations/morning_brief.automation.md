# Morning brief
- Trigger: set by the owner or agent; also on demand
- Lane source: none (reads logs and `inbox_results/done/` only, never drains the inbox)
- Reads: `skills/sub-skills/morning-brief/SKILL.md`, `bot_logs/inbox_cron_audit.log`, `inbox_results/done/` (last 24h), `bot_logs/perp_positions.json`, `bot_logs/perps_bot_heartbeat.json`, `bot_logs/perps_launcher_heartbeat.json` (sub-bot states), latest `perps_bot - autopsy_eod_*`
- Writes: owner-ping (the brief), `bot_logs/inbox_cron_audit.log`

---

## Run order (every run)
1. This lane never drains; read the done packets and logs first, as below.
2. Then act on requests (owner or agent asks, position checks, follow-ups). A user ask is handled inside this loop, never instead of it.
3. Then write the audit line. A run that skipped the drain is not a run.

You are {AGENT_NAME}. Load `skills/sub-skills/morning-brief/SKILL.md` and send ONE brief through owner-ping: open book (collateral, leverage, notional, liq, PnL % on collateral), last 24h perp actions and passes with reasons, failed packets, stale lanes or dead bots, and anything waiting on the owner. Read only; no trades, no restarts. Then load `skills/sub-skills/learnings-summarizer/SKILL.md` only if the nightly summary is missing.

Append: `[ISO] | started=0 | handled=1 | left=0 | failed=0 | exit=drained | positions_checked=N | params_updated=0 | real_findings=0 | patches_authored=0 | unknown=0 | source=brief` (same format on every lane)
