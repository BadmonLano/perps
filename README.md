<p align="center"><img src="assets/perps_logo.png" alt="Perps" width="320"></p>

# Perps v1.0

*Long it, short it, let the agent manage it.*

Perps is a Solana perpetuals trading system for Jupiter Perps (SOL, BTC, ETH) that an **SI agent runs on behalf of a human owner**. The bot reads prices, funding, open interest, the wallet and three outside feed lanes, and writes one "packet" per chain to an inbox. The agent reads those packets, decides with the help of the skills in `skills/`, and answers with a signal file. The bot checks the hard gates and executes, or declines with a reason. It never opens a trade on its own. The owner sets the wallet, sizes and personal rules once and gets notified about what happens.

**Venue:** Jupiter Perps on Solana is the only venue. Markets: SOL-PERP, BTC-PERP, ETH-PERP.

**Who it's for:** people who already run an SI agent that can read files, run Python and keep a loop going (an always-on hosted agent, or a local agent on your PC), and who understand leveraged perpetuals and their risks.

> [!WARNING]
> **This bot trades real money with leverage. Read this before you run anything.**
> - **Live by default.** Every signal the agent (or the owner) writes is signed and sent as a real Jupiter Perps transaction: opens, adds, adjusts and closes. The bot never trades on its own: it only executes those signals, and the packets and suggestions it writes are for the agent to decide.
> - **Leverage liquidates.** Positions run from 10x up to your cap, `PERP_MAX_LEVERAGE` (70x by default). When a signal leaves leverage out, the ladder `PERP_LEV_LADDER` picks it by collateral: 40x from 0.1 SOL, 25x from 1 SOL, 15x from 10 SOL, 10x from 50 SOL. A small move against you can liquidate the collateral completely.
> - **Use a burner hot wallet** that holds only what you can afford to lose completely. Never use your main wallet or a wallet with other assets.
> - **The gates reduce risk; they do not remove it.** Bugs, RPC failures, stale feeds and bad model output can lose money.
> - **Not financial advice.** This is experimental software provided as-is under the MIT license, with no warranty. You are responsible for every transaction it sends.

## Features

- **Per-chain packets:** SOL, BTC and ETH, each with price, funding, open interest, the open position and the feed lanes.
- **Hard gates** on every signal: leverage between 10x and your cap (`PERP_MAX_LEVERAGE`, 70x by default), collateral (optional `PERP_MAX_COLLATERAL_SOL` cap, which also caps size at collateral x leverage cap), live price. Out-of-band requests are declined, never clamped.
- **Per-check tuning:** on every position check the agent can move TP, SL, trail and exit floor with `update_params`; every change is logged.
- **Exit floor and liquidation watch:** alerts before liquidation; the bot never auto-closes.
- **Feeds:** Flow public data, live Kalshi and Polymarket odds, and news, each labelled with source, age and stale flag.
- **Autopsies:** closed and missed trades, with lessons written to `bot_logs/` and `learnings/`. Each losing close also gets its own "what went wrong" review (SI brain, or rules offline): verdict, root cause, one lesson, delivered to the agent's inbox. Param changes are only ever proposed (`AUTOPSY_ON`, `AUTOPSY_SI_ENABLED`, `AUTOPSY_SI_MAX_PER_DAY`).
- **Owner Commands (optional, off by default):** `/roast`, `/recap`, `/autopsy [market|last]`, `/manage`, `/leave` by Telegram through a separate command bot or by dropping a file in `owner_commands/` (SETUP.md section 6.5).
- **Owner positions:** a position the bot did not open is asked about before it is managed.
- **First-Run Setup Wizard:** `python bots/perps.py` opens it when `bots/.env` is missing or incomplete: wallet key (hidden input), RPC, brain main/fallback, Grok and Ollama, Telegram, owner commands, feed cadence and Flow URL, and the Allow New Positions / Entry Alerts switches. It shows Set/Missing, never values, backs up any existing `.env` and keeps keys it does not know. `python bots/perps.py --setup` re-runs it.
- **Console:** live dashboard, logs, settings, skills, owner commands and brain setup.
- **Optional SI brain:** Grok or local Ollama for summaries; trading works the same with it off.

## Paper mode (start here)

`PERPS_DRY_RUN=true` is the default in `bots/.env.example` and the recommended first run. In paper mode:

- Opens and closes fill at the live oracle price (the same Pyth feed Jupiter uses). Positions go to `bot_logs/paper_perp_positions.json`, P/L to `bot_logs/paper_perp_pnl.json`, every fill to `bot_logs/paper_fills.jsonl`.
- Stop, take-profit, trail and liquidation watch run exactly as live, so you see the alerts your agent will get.
- Nothing is ever signed or sent. No wallet key is needed (the wizard skips it). Every alert is tagged `[PAPER]`, and the console shows PAPER MODE.
- The live book (`perp_positions.json`, `perp_pnl.json`) and any real wallet positions are never touched. Owner-position scanning is off.
- Partial reduces via `adjust` are not simulated; use `close_pct`.

Run at least one full trade with your SI in paper mode. To go live, set `PERPS_DRY_RUN=false` (console: Settings, or edit `bots/.env`), add your burner wallet key, and restart Perps.

## Stop and take-profit protection

Jupiter Perps has no on-chain stop or take-profit this bot can arm without an API partnership, so protection is bot-side:

- Every signal carries a stop, take-profit and trail (or the bot fills them from your defaults). The engine checks open positions every `PERP_POLL_SECONDS`.
- When price crosses a stop, take-profit, trail or exit floor, the bot alerts you and writes a packet to the agent's `inbox/`. The agent calls the shots within your rules and sends the close; the bot never closes on its own.
- On each position update the agent may move the stop, take-profit, trail and exit floor. It never changes size or leverage.
- Liquidation watch warns before liquidation (`PERP_LIQ_WARN_PCT`, `PERP_SL_WARN_PCT`).
- Opens and adds are refused while the monitor is not ticking (`PERP_OPEN_REQUIRE_MONITOR`, `PERP_MONITOR_STALE_S`), so a position is never opened unwatched.
- An add is refused if its estimated liquidation would sit within `PERP_ADD_MIN_SL_LIQ_GAP_PCT` (3%) of the stop.
- **Oracle halt:** prices and accounts come live from Jupiter's Pool and Custody accounts. If that read fails, new orders are refused and you get an alert (and a `trading_halted` packet); the bot never falls back to the old hardcoded oracle map. Existing positions stay watched, and trading resumes when the read recovers.
- Because protection lives in the loop, it only works while the bot is running and the RPC is reachable. Stop the bot and nothing is watching.

## Config

All settings live in `bots/.env` (copy `bots/.env.example`, or run the setup wizard). Your rules go in `rules/rules.md` (start from `rules/rules.example.md`). Never commit `bots/.env`; it is in `.gitignore`. Full reference: [SETUP.md](SETUP.md).

## Requirements

**Software**
- **Python 3.12+**. `pip install -r requirements.txt` installs rich, tzdata, httpx, python-dotenv, solders, solana, anchorpy, base58 and Pillow. Windows needs `tzdata` or every bot exits on start.
- **Ollama (optional):** local fallback brain. `ollama pull qwen3:14b-q4_K_M` (text, about 10 GB RAM/VRAM).
- **No Node.js** or other runtime. Everything is Python.
- **OS:** tested on Windows 10/11; macOS and Linux should work but are untested. On Windows use **Windows Terminal** for the colours and layout of the console.
- **Git** to clone (`git clone <repo-url>`); a computer that stays on (trading pauses when it sleeps).
- **Text editor (optional):** the console opens files in `EDITOR` if set, else Notepad (Windows) or nano.

**Accounts and keys** (all go in `bots/.env`, template `bots/.env.example`)

| Needed | What for |
|---|---|
| A new **burner Solana wallet** (`PRIVATE_KEY`) | signs every open, add, adjust and close |
| A **Solana RPC**, Helius recommended (`RPC_URL`; `HELIUS_API_KEY` optional, used by the warden). The setup wizard asks your Helius plan and sets a daily credit cap (Free = 30k/day; SETUP.md 0.2) | chain reads and sends |

| Optional | What for |
|---|---|
| A Grok key from console.x.ai (`GROK_API_KEY`) or local Ollama | the SI brain (summaries, plain-language directives) |
| **Telegram** bot (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) | alerts |
| A separate Telegram command bot (`OWNER_COMMANDS_TG_TOKEN`) | owner commands in telegram mode |
| A Jupiter API key from developers.jup.ag (`JUPITER_API_KEY`) | higher rate limit on the Jupiter swap API used to fund shorts; keyless works at a low rate |
| A free BLS API key from data.bls.gov/registrationEngine (`BLS_API_KEY`) | higher limit for the CPI numbers in `macro_prints`; keyless works at 25 requests/day per IP |
| A free FRED API key (`FRED_API_KEY`) | fallback source for the same CPI series when BLS refuses |

No key is needed for Flow, Kalshi, Polymarket or Google News RSS. Trades go on-chain to the Jupiter Perps program through your RPC, so trading itself needs no Jupiter key.

## Quick start

Requires **Python 3.12+**.

Use a virtual environment (newer Linux/macOS Pythons refuse a system-wide `pip install`). On Windows the command is `python` (or `py -3.12`); on macOS/Linux it is `python3`. Inside an activated venv, `python` works everywhere.

```
git clone <repo-url> Perps
cd Perps
python3 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bots/perps.py
```

Check `python --version` says 3.12 or newer first; the bot exits with a clear message on older Pythons. Windows PowerShell blocks `activate` by default: run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use Command Prompt.

On the first start the **setup wizard** asks for the wallet key, RPC and the rest, writes `bots/.env` and starts the bot. A complete `.env` skips it. Re-run it any time with `python bots/perps.py --setup` (runs the wizard and exits). Prefer to edit by hand? `copy bots\.env.example bots\.env` (macOS/Linux: `cp`) and fill it in.

`bots/perps.py` opens the console in this window and starts the sub-bots (security_warden, agent_autoagent, code_tuner, learnings_writer, and owner_commands when `OWNER_COMMANDS` is not `off`) as child processes. One Ctrl+C, or **8 Stop Perps** in the console, stops everything. `python bots/perps_ui.py` opens the console only, without starting anything.

Full walkthrough: **[SETUP.md](SETUP.md)**. Skills and how an agent should load them: **[skills/README.md](skills/README.md)**.

## Agent setup (required)

The bot never decides anything on its own: it writes packets to `inbox/` and waits. **The agent MUST create recurring jobs (cron, scheduled routine, heartbeat) from `automations/`**: `perps_lane` (main, drains signals and tunes TP/SL), plus `warden_lane`, `misc_lane`, `morning_brief` and the nightly self-eval (`learnings/nightly_self_eval.automation.md`) on their own schedules. Drain perps_lane at least as often as the fastest producer (`FEED_CADENCE`, the packet cadence, the 15-min scan), floored at 5 min and capped at 30, and re-gauge when a cadence changes. Stagger start minutes so no two lanes fire together, e.g. perps_lane `3-53/10 * * * *`, warden `7,27,47 * * * *`, misc `9,24,39,54 * * * *`, morning_brief `15 7 * * *`, nightly self-eval `30 23 * * *`. The wizard prints a suggested interval when it finishes. Details and the run-once check: [SETUP.md section 11](SETUP.md#11-build-your-automations).

## How it fits together

```
bots/perps.py             the engine: packets, gates, execution, positions; starts and supervises the sub-bots
bots/perps_ui.py          console: dashboard, logs, settings, skills, owner commands, brain setup (never trades)
bots/sub-bots/            helpers run as child processes (security_warden, agent_autoagent, code_tuner,
                          learnings_writer, owner_commands) + shared modules (agent_ack, _self_description, _si_gate, packet_analyzers)
bots/feed_hook.py         Flow reader (fixed file list, neutral field names)
bots/feeds/               live odds (Kalshi, Polymarket), news and the Flow-first router
bots/perps_heartbeat_watch.py  optional outside watchdog: pings the owner if the engine dies with a position open
bots/tuner/               perps_params.json, the live tunable knobs (written at runtime)
skills/                   instructions the agent follows (start with skills/README.md)
automations/              paste-ready loop prompts for the agent, one per inbox lane
inbox/ -> inbox_results/  packets from the bots, then handled packets (done/ or failed/)
owner_inbox/              notify-only pings for the owner
owner_commands/           optional: drop /roast, /recap or /autopsy here (file mode); replies in inbox_results/owner_replies/
rules/                    the owner's personal trading rules (see rules/rules.example.md)
learnings/                what the agent learned and changed, with dates
bot_logs/, json_relay/    state, logs, audit files, signals and bot-to-bot handoffs
```

The engine is one large file by design (everything that touches money is in one place). `docs/DEPENDENCY_MAP.md` lists what it imports, reads, writes and calls.

## Feeds

Every packet carries three outside lanes (plus `macro_prints`, the released CPI numbers), each labelled with `source`, `age_min` and `stale`: **flow** ([Flow](https://theflowcycle.com) public data: per-coin calls, the live 12h call, cycle, signals, risk, macro, editorial thesis, calibration), **odds_live** (Kalshi hourly and end-of-day price ladders, Polymarket price-threshold markets and movers, fetched live at every packet) and **news_feed** (Flow news, else Google News RSS). `FEED_CADENCE=manual` (default) pulls Flow and news only when the agent asks (`json_relay/feed_request.json`). Headlines list macro items (CPI, inflation, Fed, jobs, yields) first, then newest. Tuning knobs (`PERP_POLL_SECONDS`, `PERP_TRAIL_ACTIVATE` / `PERP_TRAIL_DROP`, `PERP_AUTO_SIZE_USD` / `PERP_AUTO_LEVERAGE`, `NEWS_RSS_URL`, `NEWS_MAX_ITEMS`, `XAI_MODEL`) are listed with defaults in `bots/.env.example`. See SETUP.md section 9.

## Models

You pick one main brain and one fallback (`LLM_MAIN`, `LLM_FALLBACK`: `grok` | `ollama` | `off`; default Grok, then local Ollama). The engine and every sub-bot follow the same pair; `off` on both means no SI calls at all, and trading works the same. See [SETUP.md, section 5](SETUP.md#5-models).

## Tests

```
python bots/perps_ui.py --smoke                                   # renders every screen; set WORKSPACE_DIR to a fresh temp copy first
python bots/tests/test_owner_commands.py                          # owner commands, offline
python bots/tests/test_owner_positions_perps.py bots/perps.py     # owner positions, offline
python bots/tests/test_setup_wizard.py                            # setup wizard in a temp folder, scripted input
python bots/tests/test_paper_and_pool_halt.py --run               # paper mode, oracle halt, open/add guards (set WORKSPACE_DIR + PERPS_DRY_RUN=true; unittest does this for you)
python -m unittest discover -s bots/tests                         # all of the above except --smoke, in one run
```

None of these touch the wallet; all run offline.

## Tips

- **Do at least one trade with your SI start to finish.** Open, manage, close, then read the lesson `learnings_writer` logs in `learnings/LEARNINGS.md`. Trading alongside your SI is the fastest way it learns, it helps you both find new strategies, and it keeps the system evolving with a market that never stops changing.
- **It doesn't have to run 24/7.** Trading sessions are fine: turn it on when you want to trade, off when you're done.
- **Start small, on a burner.** Small collateral and a lower leverage cap (`PERP_MAX_LEVERAGE`) until you trust the setup, from a fresh burner wallet that holds only what you can lose.
- **Keep the Telegram chat private.** Alerts and owner commands go to `TELEGRAM_CHAT_ID`; use a private chat only you (and your SI) can read.
- **Read the acks and declines.** Every signal is answered in `bot_logs/perp_signal_acks/acks.jsonl` (`filled` with a tx, `processed`, or `declined`/`failed` with a reason). Fix the reason, don't resend with tweaked numbers.
- **Don't be afraid to build something better.** If you figure out how to make it twice as good, build it simple. That's the fastest way to keep up with an ever-changing market. Contributions welcome.

## More

- [SETUP.md](SETUP.md): full setup, going live, Telegram, models, rules
- [skills/README.md](skills/README.md): skills index and load order
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [TIMELINE.md](TIMELINE.md)
- License: [MIT](LICENSE)

## Credits

This system was built with the help of Jordan, the SI agent that worked on it alongside its owner. If the name Jordan turns up (for example in the Flow feed reference), that is who it means.
