# Timeline

Version history, newest first.

## v1.0.6 (2026-10-29): always-on loop, active hours, tunable lane cadences
- Always-on loop docs, `ACTIVE_HOURS`/`ACTIVE_TZ`/`ACTIVE_DAYS` (default always on), tunable `LANE_CADENCE_*`, asked right after `AGENT_MODE`. Details: CHANGELOG.md.

## v1.0.5 (2026-10-29): autonomy is the first setup question
- `AGENT_MODE` (suggest default, auto with warning) asked first in the wizard; perps-boss skill honours it. No trading code changed.

## v1.0.4 (2026-10-28)
- Safety rule `rules/perps-safety.mdc` for cursor.directory. Docs only.

## v1.0.3 (2026-10-27)
- Profit units: TP/SL price % plus margin % everywhere, trail scaled by leverage and run on after-fee P/L, SL-near-liquidation warning at open, numbers-first open guidance in perps-boss.

## v1.0.2 (2026-10-27)
- Repo parity with Meme Sniper: plugin manifests, CHANGELOG.md, social preview, README sections (Works with, Contents, Install as plugin, Windows/Mac/Linux, Versions, Updating, License). Docs only.

## v1.0.1 (2026-10-27)
- README: paper mode is the stated default (live only with `PERPS_DRY_RUN=false`), a paper quick start with no wallet, a glossary, example signals and `send_signal.py` usage, the real clone URL, Linux noted as working. Cron detail moved to SETUP.md section 11.
- `python bots/perps.py --help` / `--version` print and exit without starting the engine.
- Example signals in `bots/examples/` (open, update_params, close).
- Default leverage cap lowered from 70x to 20x (`PERP_MAX_LEVERAGE`), ladder `0.1:20,1:15,10:12,50:10`. Existing `bots/.env` values are unchanged.
- `FLOW_EDITOR_PREFIX` now blank by default (skips Flow's two editorial files) and documented.
- Standalone docs: no references to other, unreleased projects.

## v1.0.0 (2026-10-27): first public GitHub release

Includes paper mode, the oracle halt and the open/add guards, plus everything below from 2026-10-17.

- `macro_prints` in every chain packet: released US CPI (headline / core y/y, m/m, gasoline) from Flow + the public BLS API, FRED mirror fallback, cached in `bot_logs/macro_prints.json`, with an optional one-line SI read. Optional `BLS_API_KEY` / `FRED_API_KEY`.
- News: macro headlines (CPI, inflation, Fed, jobs, yields) rank first, then newest, before the per-chain cut; `macro` flag and count per item. Inflation headlines read hot = bearish for risk.
- Morning brief falls back to the macro snapshot + daily read for regime and leverage recommendation when the morning thesis is missing.
- Adds follow the same caps as opens (`PERP_MAX_LEVERAGE`, optional `PERP_MAX_COLLATERAL_SOL`) and need `perps_is_order: true`.
- `FLOW_EDITOR_PREFIX` setting; feed flags no longer crash on a missing news source.
- `AUTO_TRADE_ENABLED` split into `NEW_POSITIONS_ENABLED` (declines agent long/short/add, reason `new_positions_off`; exits and tuning unaffected) and `ENTRY_ALERTS_ENABLED` (scanner entry alerts only). Both default true; the old key still sets both, with a deprecation note. Setup wizard asks both. Retired proposal-pipeline comments corrected (the bot never trades on its own).
- Parked limit entries now fire: `limit_long` / `limit_short` (or `order_type: limit`) more than 1% away park in `bot_logs/perp_limit_order.json` (ack `parked`); the main loop polls that market's mark price and sends a normal market open through every gate when the target is reached. One-shot (no double fill), expiry (`expires_in_min`, default 24h `PERP_LIMIT_TTL_HOURS`), ack + Telegram for parked / filled / declined / expired. Previously parked orders were never fired.
- Trade autopsy: every losing close (`AUTOPSY_ON=loss|all|off`) gets one review: verdict, root cause (fixed list), evidence, one lesson, optional proposal. SI brain first (cost guard `AUTOPSY_SI_MAX_PER_DAY`, default 6), rule-based offline fallback. Output: `bot_logs/perp_trade_autopsies.jsonl`, an inbox packet and one learnings entry. Queue + background worker, isolated from the close path; one review per position. Positions now carry `entry_packet` and an `events` list (TP/SL/trail/liq/floor/flip alerts). Never changes params.

## v1.0-rc (2026-10-16)
- Self-contained repo: no references to anything outside it; every path resolves inside the repo root.
- `bots/perps.py` starts and supervises its sub-bots itself (security_warden, agent_autoagent, code_tuner, owner_commands), each logging to `bot_logs/<name>.console.log`; one Ctrl+C stops everything.
- Sub-bots scoped to the Perps ecosystem; their SI calls follow `LLM_MAIN` / `LLM_FALLBACK` (off = no SI calls).
- Per-check `update_params` tuning surfaced in every packet with an open position and documented in perps-boss.
- Docs, automations, `.env.example` and console follow one shared layout.

## v0.9 (2026-10-16)
- Feeds: Flow reader with neutral field names, live Kalshi hourly and end-of-day ladders, Polymarket price markets and movers, news; Flow-first router with failover.
- Optional SI brain switch (`LLM_MAIN` / `LLM_FALLBACK`); exact-form directives when it is off.
- Owner commands and owner positions; skills rewritten.

## v0.8 (2026-10-15)
- Repo scaffolded: repo-local paths, console (`bots/perps_ui.py`) with scrollable screens, PnL reset with archives, relay filter.
