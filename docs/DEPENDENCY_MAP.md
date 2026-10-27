# Perps: Dependency Map
Overview of what `bots/perps.py` depends on (one large file, run with `asyncio` `main_async()`).

## 1. Entry point and launch
- Launch: `python bots/perps.py`. It opens the console in the same window (`bots/perps_ui.py`, hosted) and starts the sub-bots as child processes (see SETUP.md section 1). `python bots/perps_ui.py` is the console alone. Before `.env` loads, the engine calls `bots/perps_setup.py` (first-run wizard; `--setup` re-runs it).
- Engine entry point: `bots/perps.py`, which runs `asyncio` `main_async()` (near the end of the file) under `if __name__ == "__main__"`, after `start_all()` starts the supervisor. The `finally` block stops every child.
- Python 3.12+ (Windows, macOS or Linux; tested on Windows). `perps.py` and `perps_ui.py` exit with a clear message on older Pythons.

## 2. Imports
**Stdlib:** asyncio, base64, collections, datetime, hashlib, json, os, re, secrets, shutil, signal, struct, subprocess, sys, threading, time, traceback, zoneinfo, ctypes (Windows console setup). agent_ack uses `msvcrt` (Windows) / `fcntl` (POSIX); the console uses `msvcrt` on Windows and `termios`, `tty`, `select` on macOS/Linux. No curses.

**Network (no SDK):** Jupiter Perps program on-chain via `RPC_URL` (anchorpy, IDL fetched from chain); `api.jup.ag/swap/v1` for funding swaps (optional `JUPITER_API_KEY`); Telegram, xAI, local Ollama, Flow, Kalshi, Polymarket, Google News over httpx.

**Third-party:** see `requirements.txt` (rich, tzdata, httpx, python-dotenv, solders, solana, anchorpy, base58, Pillow).

**Local modules:**
| Module | Used for | Its deps |
|---|---|---|
| `feed_hook.py` (bots/) | Flow reader: fixed file list, neutral field names, last-good cache | httpx; env FLOW_* |
| `feeds/` (bots/) | `feed_router` (Flow-first router + failover), `odds_fetcher` (Kalshi, Polymarket), `news_fetcher` (Google News RSS), `_brain` (optional SI tagging), `_common` | httpx / stdlib; env FEED_*, NEWS_*, POLY_*, KALSHI_* |
| `perps_setup.py` (bots/) | first-run setup wizard; writes `bots/.env` (backup, keeps unknown keys), `bot_logs/setup_wizard.json`, the new-positions and entry-alerts switches in tuner saved defaults | rich |
| `perps_ui.py` (bots/) | console, hosted by the engine; fault-isolated | rich, Pillow (logo) |
| `agent_ack.py` (sub-bots/) | ack protocol for the agent's relay files | stdlib (msvcrt/fcntl locks) |
| `_self_description.py` (sub-bots/) | self-description JSON, patch detection | stdlib |
| `learnings_writer.py` (sub-bots/) | the one safe LEARNINGS.md writer (Section 8, file lock, atomic); also a sub-bot that keeps the file healthy and drains `json_relay/learnings/` | stdlib |
| `_si_gate.py` (sub-bots/) | `LLM_MAIN` / `LLM_FALLBACK` order for the sub-bots' SI calls | stdlib |
| `packet_analyzers/` (sub-bots/) | per-packet edge analyzer (pure; reads no files) | stdlib |

**Sub-bots started by `perps.py`** (child processes, `bot_logs/<name>.console.log`):
| Sub-bot | Reads | Writes |
|---|---|---|
| security_warden | heartbeats, wallet, perp logs, feed outputs in bot_logs/ | inbox/, owner_inbox/, bot_logs/ |
| agent_autoagent | perp logs in bot_logs/, inbox_results/, learnings/ | overseer reports in inbox/ |
| code_tuner | bots/ source, bots/tuner/, bot_logs/ | bot_logs/code_tuner.log, inbox/ |
| learnings_writer | learnings/, json_relay/learnings/ | learnings/LEARNINGS.md (Section 8), bot_logs/learnings_writer.jsonl + heartbeat |
| owner_commands (only when `OWNER_COMMANDS` != off) | bot_logs/perp_episodes.jsonl, owner_commands/ | bot_logs/owner_commands.log, inbox_results/owner_replies/ |

## 3. Files read and written
Workspace root (resolved via `WORKSPACE_DIR` or the folder containing `bots/`): `bots/`, `bots/sub-bots/`, `bot_logs/`, `json_relay/`, `inbox/`, `inbox_results/{done,failed}/`, `owner_inbox/`, `owner_commands/`, `learnings/`, `rules/`, `skills/`.

**Config/tunables (read, hot-reloaded):** `bots/tuner/perps_params.json`; tuned defaults in `bots/tuner/perps_tuned_defaults.json`; history in `bots/tuner/tune history/`. Changed by directive (`json_relay/perp_meeting.json`) or the console.

**Engine reads:** `bots/.env`, `json_relay/perp_signal.json` and `perp_<chain>_signal.json`, `json_relay/perp_meeting.json`, `json_relay/feed_request.json`, `rules/rules.md`, agent-written `bot_logs/agent_thesis.json` / `agent_overrides.json`, the feed outputs below.

**Engine writes (bot_logs/):** `perp_positions.json` (plural, the store of record), `perp_pnl.json`, `perp_decisions.json`, `perp_episodes.jsonl`, `perp_autopsy_lessons.json`, `perp_trade_autopsies.jsonl` (+ `perp_autopsy_queue.jsonl`, `perp_trade_autopsy_state.json`), `missed_perp_autopsy.json`, `perp_decision_snapshots.jsonl`, `perps_param_changes.jsonl`, `perps_decline_log.jsonl`, `owner_positions.jsonl`, `owner_positions_perps.json`, `wallet_balance.json`, `market_mode.json`, `risk_state.json`, `perps.log`, `perps_bot_heartbeat.json`; supervisor: `perps_launcher.log`, `perps_launcher_heartbeat.json`, `<name>.console.log`. Packets to `inbox/`, pings to `owner_inbox/`.

**Feeds write (bot_logs/):** `flow_cache.json`, `feed_router_state.json`, `kalshi_implied_probs.json`, `polymarket_markets.json`, `polymarket_implied_probabilities.json`, `news_catalysts.json`.

## 4. Env vars and secrets (names only; loaded via `load_dotenv()` from `bots/.env`; see `bots/.env.example`)
- **Wallet / keys (critical):** `PRIVATE_KEY` (base58), `WALLET_ADDRESS` (warden, optional)
- **RPC:** `RPC_URL`, `HELIUS_API_KEY` (warden), `RPC_PUBLIC_FALLBACK` (warden)
- **LLM:** `LLM_MAIN`, `LLM_FALLBACK` (only these two run), `GROK_API_KEY` / `XAI_API_KEY`, `GROK_TEXT_MODEL`, `OLLAMA_URL`, `OLLAMA_MODEL`, `OVERSEER_GROK_MODEL`, `OVERSEER_FALLBACK_MODEL`
- **Feeds:** `FLOW_BASE_URL`, `FEED_CADENCE`, `FLOW_STALE_MIN`, `NEWS_STALE_MIN`, `FEED_FAIL_LIMIT`, `FEED_FAILBACK_COOLDOWN_MIN`, `NEWS_QUERIES`, `NEWS_MAX_AGE_H`, `POLY_MIN_LIQUIDITY_USD`, `POLY_MOVER_MIN_SHIFT`, `KALSHI_DAILY_CLOSE_HOUR_ET`
- **Alerts:** `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `WARDEN_TELEGRAM_TOKEN`, `PERPS_WATCH_TELEGRAM_TOKEN`
- **Owner commands / positions:** `OWNER_COMMANDS`, `OWNER_COMMANDS_TG_TOKEN`, `OWNER_COMMANDS_TG_CHAT_ID`, `OWNER_ROAST_COOLDOWN_S`, `OWNER_POS_*`
- **Supervisor / console:** `PERPS_COMPONENTS`, `PERPS_UI`, `WORKSPACE_DIR`
- **Misc tunables:** `PERP_RESIDUAL_DUST_USD`, `WARDEN_FEED_MAX_STALE_HOURS`, `CODE_TUNER_*`, `OVERSEER_SCHEDULED_*`, `PERPS_WATCH_*`, `INBOX_CRON_LOCK_MAX_AGE_S`

## 5. External services
Solana RPC (your `RPC_URL`; public mainnet as warden fallback), Jupiter (Perps program, api.jup.ag / quote-api.jup.ag), price cross-checks (CoinGecko, Coinbase, Kraken, Binance, DexScreener), Flow public data (theflowcycle.com/data, fixed list), Kalshi (api.elections.kalshi.com, no auth), Polymarket (gamma-api / clob.polymarket.com, no key), Google News RSS, xAI Grok (api.x.ai), Ollama (local), Telegram Bot API.

## 6. Other bots and pipelines
| Dependency | Direction | If it's off | Flag? |
|---|---|---|---|
| **the agent** (packets in, `perp_signal.json` / `perp_meeting.json` / `feed_request.json` out) | both | no trades are placed; packets pile up in inbox | none (the bot never trades on its own) |
| **sub-bots** (warden, overseer, code tuner, owner commands) | both | the engine trades the same; no alerts/reports from that sub-bot | `PERPS_COMPONENTS`, `OWNER_COMMANDS` |
| **Flow feed** | reads | packets carry the last good pull flagged stale, or no flow lane | `FEED_CADENCE` (manual by default) |
| **Kalshi / Polymarket / news** | reads | that lane is marked missing | none |
| **Grok / Ollama SI brain** | calls | summaries blank, directives need exact form | `LLM_MAIN`, `LLM_FALLBACK` |
| **Telegram** | writes | silent if token is empty | effectively env-gated |

## 7. Money logic
- **Execution:** only on an agent signal. Leverage from 10x up to `PERP_MAX_LEVERAGE` (70x default), collateral and a live price are hard gates; out-of-band requests are declined, never clamped.
- **Exits:** TP, SL, trail and the exit floor are set on open and tuned per check with `update_params` (size, leverage and direction refused there). The exit floor warns before liquidation; the bot never auto-closes.
- **Wallet:** it signs and sends real transactions with `PRIVATE_KEY` (solders + Jupiter Perps). Leave the key blank until you have read SETUP.md.
