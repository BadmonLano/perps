<p align="center"><img src="assets/perps_logo.png" alt="Perps" width="320"></p>

# Perps

*Long it, short it, let the agent manage it.*

Perps is a Solana perpetuals trading system for Jupiter Perps (SOL, BTC, ETH) that an **SI agent runs on behalf of a human owner**. The bot reads prices, funding, open interest, the wallet and three outside feed lanes, and writes one "packet" per chain to an inbox. The agent reads those packets, decides with the help of the skills in `skills/`, and answers with a signal file. The bot checks the hard gates and executes, or declines with a reason. It never opens a trade on its own. The owner sets the wallet, sizes and personal rules once and gets notified about what happens.

**Venue:** Jupiter Perps on Solana is the only venue. Markets: SOL-PERP, BTC-PERP, ETH-PERP.

**Who it's for:** people who already run an SI agent that can read files, run Python and keep a loop going (an always-on hosted agent, or a local agent on your PC), and who understand leveraged perpetuals and their risks.

> [!WARNING]
> **Paper by default; real money with leverage once you switch it live. Read this before you go live.**
> - **Paper by default.** Out of the box (`PERPS_DRY_RUN=true`) every fill is simulated at the live oracle price and nothing is signed or sent. Only when you set `PERPS_DRY_RUN=false` does every signal the agent (or the owner) write become a real Jupiter Perps transaction: opens, adds, adjusts and closes. Either way the bot never trades on its own: it only executes those signals.
> - **Leverage liquidates.** Positions run from 10x up to your cap, `PERP_MAX_LEVERAGE` (20x by default). When a signal leaves leverage out, the ladder `PERP_LEV_LADDER` picks it by collateral: 20x from 0.1 SOL, 15x from 1 SOL, 12x from 10 SOL, 10x from 50 SOL. You can raise the cap (up to the 250x venue max), but start low. A small move against you can liquidate the collateral completely.
> - **Use a burner hot wallet** that holds only what you can afford to lose completely. Never use your main wallet or a wallet with other assets.
> - **The gates reduce risk; they do not remove it.** Bugs, RPC failures, stale feeds and bad model output can lose money.
> - **Not financial advice.** This is experimental software provided as-is under the MIT license, with no warranty. You are responsible for every transaction it sends.

## Works with

Perps can be run by Grok Bot, Muse, or an agent from any of the larger SI companies: any SI agent that can read and write files and run Python works. No official partnership or endorsement is implied.

## Contents

- [Works with](#works-with)
- [Glossary](#glossary)
- [Features](#features)
- [Paper quick start (no wallet, no keys)](#paper-quick-start-no-wallet-no-keys)
- [Signals (what the agent writes)](#signals-what-the-agent-writes)
- [Stop and take-profit protection](#stop-and-take-profit-protection)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Install as plugin](#install-as-plugin)
- [Windows, Mac and Linux](#windows-mac-and-linux)
- [Agent setup (required)](#agent-setup-required)
- [How it fits together](#how-it-fits-together)
- [Tests](#tests)
- [Contributing](#contributing)
- [Versions and releases](#versions-and-releases)
- [Updating](#updating)
- [License](#license)

## Glossary

- **SI agent:** your AI agent (hosted or local) that reads packets, decides and writes signals. The bot never decides.
- **Packet:** a JSON snapshot the bot writes to `inbox/` per chain (price, funding, open interest, your position, feeds) for the agent to read.
- **Signal:** the agent's answer, a JSON file in `json_relay/` (open, add, update_params, close). See [Signals](#signals-what-the-agent-writes).
- **json_relay/:** the hand-off folder for signals, feed requests and bot-to-bot files.
- **Lane:** one recurring agent job (cron/routine) that drains one kind of packet, from `automations/`; `perps_lane` is the main one.
- **Gates:** hard checks on every signal (leverage band, collateral cap, live price, monitor running). A signal that fails one is declined, never resized.
- **Ratchet:** moving the stop and take-profit up (down on a short) as price makes new structure, so gains get locked in.
- **Peel:** a partial close (`close_pct`), usually 30-40% at the first target; the rest rides as the runner.
- **Exit floor:** a price level that triggers an alert so the agent can protect profit. The bot alerts; it never auto-closes.
- **Flow:** a free public crypto data feed ([theflowcycle.com](https://theflowcycle.com)): per-coin calls, cycle, risk and news. One of three feed lanes.
- **Paper mode:** `PERPS_DRY_RUN=true`, simulated fills, nothing signed. The default.

## Features

- **Per-chain packets:** SOL, BTC and ETH, each with price, funding, open interest, the open position and the feed lanes.
- **Hard gates** on every signal: leverage between 10x and your cap (`PERP_MAX_LEVERAGE`, 20x by default), collateral (optional `PERP_MAX_COLLATERAL_SOL` cap, which also caps size at collateral x leverage cap), live price. Out-of-band requests are declined, never clamped.
- **Per-check tuning:** on every position check the agent can move TP, SL, trail and exit floor with `update_params`; every change is logged.
- **Exit floor and liquidation watch:** alerts before liquidation; the bot never auto-closes.
- **Feeds:** Flow public data, live Kalshi and Polymarket odds, and news, each labelled with source, age and stale flag.
- **Autopsies:** closed and missed trades, with lessons written to `bot_logs/` and `learnings/`. Each losing close also gets its own "what went wrong" review (SI brain, or rules offline): verdict, root cause, one lesson, delivered to the agent's inbox. Param changes are only ever proposed (`AUTOPSY_ON`, `AUTOPSY_SI_ENABLED`, `AUTOPSY_SI_MAX_PER_DAY`).
- **Owner Commands (optional, off by default):** `/roast`, `/recap`, `/autopsy [market|last]`, `/manage`, `/leave` by Telegram through a separate command bot or by dropping a file in `owner_commands/` (SETUP.md section 6.5).
- **Owner positions:** a position the bot did not open is asked about before it is managed.
- **First-Run Setup Wizard:** `python bots/perps.py` opens it when `bots/.env` is missing or incomplete: wallet key (hidden input), RPC, brain main/fallback, Grok and Ollama, Telegram, owner commands, feed cadence and Flow URL, and the Allow New Positions / Entry Alerts switches. It shows Set/Missing, never values, backs up any existing `.env` and keeps keys it does not know. `python bots/perps.py --setup` re-runs it.
- **Console:** live dashboard, logs, settings, skills, owner commands and brain setup.
- **Optional SI brain:** Grok or local Ollama for summaries; trading works the same with it off.

## Paper quick start (no wallet, no keys)

```
git clone https://github.com/BadmonLano/perps.git
cd perps
python3 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp bots/.env.example bots/.env            # Windows: copy bots\.env.example bots\.env
python bots/perps.py
```

`bots/.env.example` ships with `PERPS_DRY_RUN=true`, so this starts in paper mode and the console shows PAPER MODE. The setup wizard then opens: press Enter to skip the wallet key and every optional key; paper mode needs none of them. It does need an RPC for live prices: paste the free public `https://api.mainnet-beta.solana.com` (or a free Helius URL) when it asks for `RPC_URL`, and pick Free or None for the Helius plan. Then write a test signal (see [Signals](#signals-what-the-agent-writes)) and watch it fill on the paper book. `python bots/perps.py --help` lists the flags without starting anything.

## Paper mode

`PERPS_DRY_RUN=true` is the default in `bots/.env.example` and the recommended first run. In paper mode:

- Opens and closes fill at the live oracle price (the same Pyth feed Jupiter uses). Positions go to `bot_logs/paper_perp_positions.json`, P/L to `bot_logs/paper_perp_pnl.json`, every fill to `bot_logs/paper_fills.jsonl`.
- Stop, take-profit, trail and liquidation watch run exactly as live, so you see the alerts your agent will get.
- Nothing is ever signed or sent. No wallet key is needed (the wizard skips it). Every alert is tagged `[PAPER]`, and the console shows PAPER MODE.
- The live book (`perp_positions.json`, `perp_pnl.json`) and any real wallet positions are never touched. Owner-position scanning is off.
- Partial reduces via `adjust` are not simulated; use `close_pct`.

Run at least one full trade with your SI in paper mode. To go live, set `PERPS_DRY_RUN=false` (console: Settings, or edit `bots/.env`), add your burner wallet key and RPC, and restart Perps.

## Signals (what the agent writes)

The agent answers a packet with one small JSON file, `json_relay/perp_<chain>_signal.json`. The bot gates it, then fills it or declines it with a reason in `bot_logs/perp_signal_acks/acks.jsonl`. Three examples (also in [`bots/examples/`](bots/examples/)). `tp_pct` and `sl_pct` are **% of price move from entry**, not % of your margin: at the 20x default cap, `tp_pct: 2.0` is about +40% on margin and `sl_pct: 1.0` about -20%.

```json
{"action": "long", "market": "SOL-PERP", "reason": "SOL holding the 4h higher low", "perps_is_order": true, "collateral_usd": 11.0, "tp_pct": 2.0, "sl_pct": 1.0}
{"action": "update_params", "market": "SOL-PERP", "reason": "ratchet: stop under the new higher low", "tp_pct": 3.0, "sl_pct": 0.8}
{"action": "close", "market": "SOL-PERP", "reason": "thesis invalidated"}
```

Easiest is the helper, which writes the file safely and waits for the ack:

```
python bots/tools/send_signal.py --market SOL --action long --collateral-usd 11 --tp-pct 2 --sl-pct 1 --reason "why" --wait
python bots/tools/send_signal.py --market SOL --action update_params --tp-pct 3 --sl-pct 0.8 --reason "ratchet" --wait
python bots/tools/send_signal.py --market SOL --action close --reason "done" --wait
```

How the agent manages a trade (the ratchet: walk the stop and take-profit up on structure, peel 30-40% at the first trim, let the runner ride, one add at most, close everything if the thesis dies) is written out in [`skills/perps-boss/SKILL.md`](skills/perps-boss/SKILL.md).

## Stop and take-profit protection

Jupiter Perps has no on-chain stop or take-profit this bot can arm without an API partnership, so protection is bot-side:

- Every signal carries a stop, take-profit and trail (or the bot fills them from your defaults). The engine checks open positions every `PERP_POLL_SECONDS`.
- When price crosses a stop, take-profit, trail or exit floor, the bot alerts you and writes a packet to the agent's `inbox/`. The agent calls the shots within your rules and sends the close; the bot never closes on its own.
- On each position update the agent may move the stop, take-profit, trail and exit floor. It never changes size or leverage.
- Liquidation watch warns before liquidation (`PERP_LIQ_WARN_PCT`, `PERP_SL_WARN_PCT`).
- Units: TP and SL are % of price; P/L and the trail are % of margin (collateral) after fees and borrow. Alerts, acks and the dashboard show both, e.g. `TP: $182.50 (+2.0% price, +40% margin at 20x)`.
- The trail alert (default arm 20 / giveback 15 margin points, the values at 5x) scales with leverage (`PERP_TRAIL_LEV_SCALE=sqrt`): 20x arms at +40 / 30, 50x at +63 / 47, so it is not set off by a 0.3% wiggle at high leverage. `linear` keeps the same price move at every leverage; `off` keeps 20 / 15 fixed.
- At open the bot warns (never blocks) when the SL sits past 60% of the way from entry to liquidation (`PERP_SL_LIQ_WARN_FRAC`).
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
- **OS:** Windows 10/11 and Linux (the tests and console smoke check pass on both); macOS should work. On Windows use **Windows Terminal** for the colours and layout of the console.
- **Git** to clone (`git clone https://github.com/BadmonLano/perps.git`); a computer that stays on (trading pauses when it sleeps).
- **Text editor (optional):** the console opens files in `EDITOR` if set, else Notepad (Windows) or nano.

**Accounts and keys** (all go in `bots/.env`, template `bots/.env.example`)

| Needed | What for |
|---|---|
| A new **burner Solana wallet** (`PRIVATE_KEY`) | live mode only: signs every open, add, adjust and close (paper mode needs none) |
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
git clone https://github.com/BadmonLano/perps.git
cd perps
python3 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bots/perps.py
```

Check `python --version` says 3.12 or newer first; the bot exits with a clear message on older Pythons. Windows PowerShell blocks `activate` by default: run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use Command Prompt.

On the first start the **setup wizard** asks for the wallet key, RPC and the rest (in paper mode press Enter to skip the key), writes `bots/.env` and starts the bot. A complete `.env` skips it. Re-run it any time with `python bots/perps.py --setup` (runs the wizard and exits); `--help` lists the flags. Prefer to edit by hand? `copy bots\.env.example bots\.env` (macOS/Linux: `cp`) and fill it in.

`bots/perps.py` opens the console in this window and starts the sub-bots (security_warden, agent_autoagent, code_tuner, learnings_writer, and owner_commands when `OWNER_COMMANDS` is not `off`) as child processes. One Ctrl+C, or **8 Stop Perps** in the console, stops everything. `python bots/perps_ui.py` opens the console only, without starting anything.

Full walkthrough: **[SETUP.md](SETUP.md)**. Skills and how an agent should load them: **[skills/README.md](skills/README.md)**.

## Install as plugin

Perps ships as a plugin: 9 agent skills (perps-boss, pnl-tracker, forensics and the sub-skills) plus this repo's bots. The manifest is in `.cursor-plugin/plugin.json` (Cursor) and `.grok-plugin/plugin.json` (Grok Bot); both list the same skills.

- **Cursor:** once listed, open **Customize** in the sidebar, find **perps**, then **Install**. To try it before then, clone this repo into `~/.cursor/plugins/local/perps`, run **Developer: Reload Window**, and check under **Customize** that the skills show up.
- **Grok Bot or any other SI agent:** clone the repo and point your agent at `SETUP.md`. It loads the skills from `skills/` and sets up the loops in `automations/`.

The skills tell your agent how to run the bot. The bot itself still runs on your machine: follow [Quick start](#quick-start), stay in paper mode (`PERPS_DRY_RUN=true`) until the acks look right, then go live with the smallest collateral you're OK losing.

## Windows, Mac and Linux

The same Python files run on all three. Tests and the console smoke check pass on Windows 10/11 and Linux; macOS should work but is less tested.

| Thing | Windows | Mac / Linux |
|---|---|---|
| Python command | `python` | usually `python3` (inside a venv, `python`) |
| Paths in docs | `bots\.env` | `bots/.env` |
| Terminal | Windows Terminal recommended | Terminal / iTerm |
| File locks | `msvcrt` | `fcntl` (picked automatically) |

Windows-only bits in the code are guarded so they're skipped elsewhere: `bots/perps_ui.py` uses `msvcrt` for single-key input and `ctypes.windll` for the console (typed input on Mac/Linux), and `bots/sub-bots/agent_ack.py` picks `msvcrt` or `fcntl` locks. Keep the computer awake: trading pauses when it sleeps (Mac: `caffeinate -i python3 bots/perps.py`).

## Agent setup (required)

The bot never decides anything on its own: it writes packets to `inbox/` and waits. **The agent MUST create recurring jobs (cron, scheduled routine, heartbeat) from `automations/`**: `perps_lane` (the main loop: drains packets, sends signals, tunes TP/SL) plus `warden_lane`, `misc_lane`, `morning_brief` and the nightly self-eval. Schedules, how often to drain and an example staggered crontab: [SETUP.md section 11](SETUP.md#11-build-your-automations).

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md), not public issues.

## Versions and releases

- Versions look like `v1.0.3`. Each one is a **Release** with notes on what changed (history in [`CHANGELOG.md`](CHANGELOG.md) and [`TIMELINE.md`](TIMELINE.md)).
- **Forks and downloads don't auto-update.** ZIP: download the new release and copy your `bots/.env`, `rules/` and `learnings/` across. Git clone: `git pull`. Fork: click **Sync fork**.
- To hear about new versions: **Watch → Custom → Releases**.

## Updating

- Git: `git pull` in the folder. ZIP: unzip the new release over the old folder or next to it.
- Your `bots/.env` and `bot_logs/` are kept either way (git ignores them). Check `bots/.env.example` and the release notes for new settings.

## License

[MIT](LICENSE) © 2026 Bentayga Empire LLC. Use it, change it, share it; keep the license notice. No warranty.

---

More from Bentayga: [JOS (@JOSYSTEMS)](https://x.com/JOSYSTEMS) · [theflowcycle.com](https://theflowcycle.com)
