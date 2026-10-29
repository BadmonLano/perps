# SETUP

**Who this is for:** the agent setting Perps up for its owner. "You" is the agent; "the owner" is the human whose wallet and rules these are. Read this once, top to bottom, before the first start. The skills ship as tested defaults; change them only with the owner (section 8).

> **It ships in paper mode** (`PERPS_DRY_RUN=true`): fills are simulated and nothing is signed. With `PERPS_DRY_RUN=false`, every signal you write is a real, leveraged Jupiter Perps transaction. Make sure the owner uses a burner wallet and understands the warning in the README before you start anything.

> **The owner runs setup once by hand.** The first `python bots/perps.py` opens the setup wizard when `bots/.env` is missing or incomplete (section 1.1). Or copy `bots/.env.example` to `bots/.env` and fill the must-haves (section 0), or use the console's Settings and Brain Setup screens (`python bots/perps_ui.py`). After that, you (the agent) can change the rest through Settings or directives.

## How Perps works, in short

The key ideas before you start:

- **Venue and markets:** Jupiter Perps only (SOL-PERP, BTC-PERP, ETH-PERP), leveraged.
- **The bot never opens a trade on its own.** It writes one packet per chain; every open, add, tune or close is a signal the agent (or the owner) writes (`json_relay/perp_<chain>_signal.json`), and the bot gates it or declines with a reason. Packets and suggestions (such as `perp_exit_suggestion.json`) are for the agent to decide; the bot never acts on them.
- **Leverage gates and ladder:** `PERP_MAX_LEVERAGE` and `PERP_LEV_LADDER` (section 4) set how much leverage a position may use.
- **Feeds:** three outside lanes (Flow public data, Kalshi/Polymarket odds, news), each labelled with source, age and stale (section 9).
- **Lanes:** `perps_lane` is the main agent loop; its drain cadence is derived from the feed and packet cadences (section 11).

## 0. Prerequisites

Have these ready before the first start. Every key goes in `bots/.env` (copy `bots/.env.example`, the single template; each key there says where to sign up).

- **A computer that stays on:** Windows, macOS or Linux. If it sleeps, trading pauses.
- **Python 3.12+** and **git**.
- **An SI agent** that can read files, run Python and keep a loop going (hosted or local; section 2).
- **A new burner Solana wallet** (`PRIVATE_KEY`, base58) funded only with SOL you can lose. Make it fresh in any Solana wallet app and export the private key. Never use a main wallet.
- **A Solana RPC** (`RPC_URL`): sign up at helius.dev, or use any Solana RPC provider. `HELIUS_API_KEY` is optional (the warden's enhanced APIs).
- **Optional brain:** a Grok key from console.x.ai, and/or Ollama (ollama.com) running locally for free. With neither, trading works the same (section 5).
- **Optional:** a Telegram bot for alerts (section 6), a separate Telegram command bot for owner commands (section 6.5).
- **Optional:** a Jupiter API key (`JUPITER_API_KEY`, developers.jup.ag, free tier) for a higher rate limit on the swap API that funds shorts. Without it the bot uses keyless `api.jup.ag` (low rate limit). Opening, adjusting and closing perps is on-chain through your RPC and needs no Jupiter key.
- **No key needed:** Flow public data, Kalshi and Polymarket market data, Google News RSS.

## 0.1 What it costs

The bot itself is free (MIT). **Prices change: check each provider before you sign up.**

| Item | Used for | Price | Source |
|---|---|---|---|
| Solana RPC (Helius or other) | `RPC_URL` | Helius (2026-10-17): Free $0/mo 1M credits, Developer $49/mo 10M, Business $499/mo 100M; see 0.2 | [helius.dev/pricing](https://www.helius.dev/pricing) |
| xAI Grok API (optional brain) | summaries, directives, overseer | per-token, see the provider | [docs.x.ai/docs/models](https://docs.x.ai/docs/models) |
| Ollama local (optional) | free fallback brain | no API bill; your own hardware (about 10 GB RAM/VRAM for `qwen3:14b-q4_K_M`) | check provider |
| Jupiter Perps | every open, add, adjust, close | open/close fees, borrow fees and price impact, paid from collateral | check provider |
| Solana network | every transaction | base fee plus priority fee | check provider |
| Jupiter API (optional key) | SOL to USDC/BTC/ETH funding swaps | keyless free at a low rate; free and paid key tiers | [developers.jup.ag](https://developers.jup.ag/docs/api-setup) |
| Flow, Kalshi, Polymarket, Google News | feeds | free, no key | - |
| Telegram (optional) | alerts, owner commands | free | - |

How spend grows: LLM cost scales with how often your automations run (section 11) and how many packets the bot writes; start with sparse cadences. Trading costs come out of every trade.

### 0.2 Helius credits and the daily cap

Helius bills in credits (checked 2026-10-17 at [helius.dev/docs/billing/credits](https://www.helius.dev/docs/billing/credits)): most RPC calls cost 1, getProgramAccounts and DAS 10, getTransactionsForAddress 10 per 100 transactions, Enhanced Transactions 100. The bot meters its own Helius calls per day (`bot_logs/perps_helius_meter.json`) and, near a daily cap, slows background reads; trades, closes and the position monitor never wait on the cap.

The setup wizard asks which plan you are on and sets `HELIUS_DAILY_CREDIT_CAP` to about 1/30 of the monthly credits, so one bad day cannot burn the month:

| Plan | Monthly credits | Daily cap set |
|---|---|---|
| Free ($0) | 1M | 30,000 |
| Developer ($49) | 10M | 300,000 |
| Business ($499) | 100M | 3,000,000 |
| Custom | yours | any number |
| None | | 0 = off (warned: nothing stops a bug or a leaked key from burning the month) |

Unset (for example a non-interactive first run) = the Free cap, 30,000. `HELIUS_CAP_MODE` = `soft` (default; restrict at `HELIUS_SOFT_CAP_PCT`, 80%), `hard` (at 100%) or `log` (count only). Change it any time with the wizard (RPC area) or in `bots/.env`.

Typical use, from our own metering: Perps about 10k-300k credits/month, so the Free plan fits. If the same RPC key also serves other bots, size the plan for all of them.

## 1. Install and launch

Python 3.12+. `requirements.txt` includes `tzdata`, which Windows needs for time zones; without it every bot exits on start.

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

**`python bots/perps.py`** starts everything:

- the engine (packets, gates, execution, positions);
- the **console** in the same window (section 10) when it runs in a terminal (`PERPS_UI=0` or `--no-ui` = no console);
- the **sub-bots** (security_warden, agent_autoagent, code_tuner, learnings_writer, and owner_commands when `OWNER_COMMANDS` is not `off`), each as a separate child process. A child that crashes restarts after a short backoff (5s, growing to 60s). Each child's output goes to `bot_logs/<name>.console.log`; the supervisor logs to `bot_logs/perps_launcher.log` and writes its states to `bot_logs/perps_launcher_heartbeat.json`. One Ctrl+C (or console **8 Stop Perps**) stops everything cleanly and writes `stop_reason: user_stopped`. To run only some sub-bots, set `PERPS_COMPONENTS=security_warden,code_tuner` (a name that matches none, e.g. `none`, starts no sub-bots).

`python bots/perps_ui.py` opens the console only, without starting anything. `python bots/perps_ui.py --smoke` renders every screen headless (no keys, no trades).

### 1.1 First-Run Setup Wizard (`bots/perps_setup.py`)

`python bots/perps.py` checks `bots/.env` before anything loads:

1. **`setup.json`** at the root or in `bots/` (`{"env": {"KEY": "value", ...}}`, plus `"NEW_POSITIONS_ENABLED"` / `"ENTRY_ALERTS_ENABLED": true|false` if wanted) is applied without questions and logged as "configured by agent". This is the agent path. The file holds secrets, so the bot **deletes it right after applying it** (it is also in `.gitignore`); write it again for the next change.
2. A **complete** `.env` (`PRIVATE_KEY` and `RPC_URL` set) skips the wizard and the bot starts.
3. Missing or incomplete, in a terminal: the **wizard** opens. Without a terminal it prints what is missing and stops at the usual `.env` check.

The wizard shows a table (Area / # / Setting / Status / Key) with Set or Missing, never values. Pick **[1] Fill Missing**, **[2] Edit Items** or **[3] Everything**. It asks for: wallet private key (hidden input), RPC URL, your **Helius plan** (Free / Developer / Business / Custom / None; sets the daily credit cap, section 0.2), brain main and fallback (Grok / Ollama / Off, shown as Main, Fallback, Off or Off (Key Set)), Grok key and model (optional), Ollama URL and model, Telegram token and chat ID (optional), owner commands (Off / Telegram / File, plus the command bot token and chat when Telegram), feed cadence (Manual default, or minutes) and Flow URL, and two switches, Allow New Positions and Entry Alerts, On/Off (saved to `bots/tuner/perps_tuned_defaults.json`). Answers are case-insensitive. Enter keeps the current value, `clear` blanks it, `q` / `back` / `esc` goes back, Ctrl+C stops. Nothing is written until **Save?** is answered Y.

On save, any existing `bots/.env` is backed up to `bot_logs/backups/env/`, changed lines are replaced in place, unknown keys are kept, and the write is atomic. The first-run marker `bot_logs/setup_wizard.json` records when and how setup ran (key names only, no values).

`python bots/perps.py --setup` (or `python bots/perps_setup.py`) re-runs the wizard and exits without starting the bot.

**Optional outside watchdog:** `python bots/perps_heartbeat_watch.py` runs as its own process and pings the owner if the engine's heartbeat goes stale while a position is open (`--once` checks one time). It is separate on purpose: a watchdog cannot live inside the process it watches.

## 2. Who's running it

Ask the owner which of these fits:

1. **Hosted agent on its own computer.** The repo lives on the agent's machine. It's always on and hands-off, and keys are stored as the agent's saved secrets.
2. **Hosted agent working on the owner's PC.** The repo lives on the owner's PC and keys stay local. You act through approved computer access. If the PC sleeps, everything pauses.
3. **Local agent.** You download the repo to the PC and run everything yourself.

## 3. Root folder

Everything lives under one root folder: **the repo folder (the one that contains `bots/`)**, unless `WORKSPACE_DIR` is set in the environment or in `bots/.env`, in which case that folder is the root.

```
<root>/
  bots/            perps.py, perps_ui.py, feed_hook.py, feeds/ + sub-bots/ + tuner/
  skills/          default skills + sub-skills/
  inbox/           packets from bots, waiting for you
  inbox_results/
    done/          handled packets
    failed/        unroutable or broken packets (+ .failure.md)
  owner_inbox/     notify-only pings for the owner
  owner_commands/  file-mode owner commands (section 6.5)
  learnings/       LEARNINGS.md + nightly history
  rules/           the owner's rules (rules.md)
  json_relay/      signals (perp_sol|btc|eth_signal.json; use bots/tools/send_signal.py), feed requests, bot-to-bot handoffs
  bot_logs/        logs, state, audit
```

Every bot resolves paths from this one root and creates any missing folders on first start. Nobody has to make them by hand.

## 4. Wallet, sizes and going live

**First question: agent autonomy (`AGENT_MODE`).** The wizard asks it before anything else. `suggest` (default, preselected): the agent proposes each open, add and close and waits for the owner's OK before sending it. `auto`: **warning** - the agent opens and closes leveraged positions on its own inside the caps; the wizard makes the owner type `AUTO`. Never pick auto for the owner. `NEW_POSITIONS_ENABLED=false` stays the hard switch in both modes.

Start in paper mode: `PERPS_DRY_RUN=true` (the template default) simulates fills at the oracle price, keeps a separate paper book, runs all protection, and never signs or sends anything; no wallet key needed. Go live with `PERPS_DRY_RUN=false` plus the wallet key, then restart Perps. See README, Paper mode.

After the autonomy question, walk the owner through `bots/.env.example`: wallet, RPC, brain, Telegram, owner commands. Live knobs (the new-positions and entry-alerts switches, trail, leverage defaults and the rest) live in `bots/tuner/perps_params.json` and are changed by directive (`json_relay/perp_meeting.json`, see perps-boss "Directives") or the console's Settings screen.

- `NEW_POSITIONS_ENABLED` (tuner, default true): false = every long, short and add the agent sends is declined (reason `new_positions_off`); closes, adjust, `update_params`, `close_pct` and `pull_initial` still work, and packets keep coming. The bot never opens a trade on its own either way.
- `ENTRY_ALERTS_ENABLED` (tuner, default true): false = the intel scanner sends no entry alerts. Nothing else changes.
- `AUTO_TRADE_ENABLED` is deprecated: if it is still in `bots/.env` or the saved tuner defaults and the two new keys are not, it sets both (a one-line deprecation note is logged).
- Leverage runs from 10x up to `PERP_MAX_LEVERAGE` (20x by default, also when unset; never above the 250x venue max); anything outside is declined, never clamped. Optional `PERP_MAX_COLLATERAL_SOL` caps collateral per position and size at that collateral x the leverage cap; blank or 0 = no extra cap beyond the wallet and risk rules. These are the only leverage/size gates, there is no separate hard-coded envelope. A signal without leverage gets it from `PERP_LEV_LADDER` (default `0.1:20,1:15,10:12,50:10`: less collateral, more leverage).
- `rules/rules.md` (section 7) sets the owner's own limits: max size, max leverage, no-trade hours, loss limits.

## 5. Models

The SI brain is optional. It writes summaries (flip digest, fused position brief, loss patterns), reads plain-language directives, and runs the overseer and the warden's checks. The packets and trading never depend on it.

**Pick one main brain and one fallback.** `LLM_MAIN` = `grok` | `ollama` | `off` (default `grok`), `LLM_FALLBACK` = the same list (default `ollama`). The engine and every sub-bot follow this pair. With both `off`, or with no Grok key and no Ollama running, no SI call is made: summaries stay blank and directives need exact form (`set PARAM VALUE`, `default PARAM VALUE`, `reset PARAM`, several joined with `;`, or JSON `{"translated_to": [{"param": "...", "action": "set", "new": ...}]}`).

- **Grok:** set `GROK_API_KEY` (or `XAI_API_KEY`) and `GROK_TEXT_MODEL` (`auto` = the built-in newest-first list; or pin one, e.g. `grok-4.7`). The overseer can be pinned separately with `OVERSEER_GROK_MODEL`.
- **Local Ollama:** install Ollama, `ollama pull qwen3:14b-q4_K_M`, then set `OLLAMA_MODEL` (default `qwen3:14b-q4_K_M`) and `OLLAMA_URL` (default `http://localhost:11434`). The code tuner reads `OLLAMA_HOST` (default `http://127.0.0.1:11434`); set it too if Ollama runs elsewhere.
- The console's **6 Brain Setup** screen picks main and fallback.

## 6. Alerts (optional)

Owner alerts (fills, exits, declines, liquidation warnings, warden alerts, overseer) go to Telegram when `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are set in `bots/.env`. Leave both blank to run without alerts.

### 6.1 Telegram

1. In Telegram message @BotFather, send `/newbot`, copy the token into `TELEGRAM_TOKEN`.
2. Open the new bot and press Start. Get your chat ID (for example from @userinfobot) and put it in `TELEGRAM_CHAT_ID`.
3. Restart `python bots/perps.py`.

### 6.5 Owner commands (optional, off by default)

| Command | What you get |
|---|---|
| `/roast` | An honest roast of the last 24h, from the same Overseer prompt and judges as the nightly run |
| `/recap` | The same judges, framed as a recap (strong, weak, missed, next) |
| `/autopsy` or `/autopsy last` | The latest closed perp trade: PnL, exit reason, hold time |
| `/autopsy sol` or `/autopsy SOL-PERP` | The latest closed trade on that market |
| `/manage <market>` or `/leave <market>` | Answer an owner-position question (section 6.6) |
| Plain text | "roast me", "recap please", "autopsy sol": commands are case-insensitive |

`/roast` and `/recap` make one SI call each and share a cooldown (`OWNER_ROAST_COOLDOWN_S`, default 900 s). `/autopsy` reads `bot_logs/perp_episodes.jsonl` and makes no SI call. Every reply is also saved in `inbox_results/owner_replies/`.

Pick a source with `OWNER_COMMANDS` (`.env` or console Settings):

- **`off`** (default): nothing runs and `perps.py` does not start `owner_commands`.
- **`file`** (any OS, no Telegram): drop a text file into `owner_commands/` in the root folder, e.g. `roast.txt` containing `/roast`, or a `.json` like `{"text": "/autopsy last"}`. Answered through Telegram (if set) and moved to `inbox_results/owner_commands/done/`.
- **`telegram`**: chat with a **separate** command bot (`OWNER_COMMANDS_TG_TOKEN`; never reuse `TELEGRAM_TOKEN`). Replies go to `OWNER_COMMANDS_TG_CHAT_ID` (blank = `TELEGRAM_CHAT_ID`); other chats are ignored. Anyone who can post in that chat can send commands, so use a private chat or one you trust.

Check it without starting anything: `python bots/sub-bots/owner_commands.py --check` (never prints a token). Try one locally: `python bots/sub-bots/owner_commands.py --once "/autopsy last" --no-send`. Restart `perps.py` after changing `OWNER_COMMANDS`. Log: `bot_logs/owner_commands.log`. Test: `python bots/tests/test_owner_commands.py` (offline).

### 6.6 Owner positions

If the wallet holds a Jupiter perps position (or an add) that the bot did not place, the bot books it as an owner position, writes an `owner_position` packet to `inbox/` and alerts: "I see a $500 SOL long position I didn't open. Manage it or leave it to you?" Until you answer it only sends the liquidation warning.

| Answer | What happens |
|---|---|
| `/manage sol` or the SI relays `{"action": "owner_position", "id": "...", "decision": "manage"}` | normal TP, SL, trail and exit floor. Exits are alerts; a signal closes. Size and leverage are never changed. |
| `/leave sol` or `"decision": "leave"` | liquidation warning only |
| no answer within `OWNER_POS_TIMEOUT_MIN` (default 60), or price inside `OWNER_POS_LIQ_EARLY_FRAC` (default 15%) of the entry-to-liquidation distance from liquidation | the bot manages it and alerts |

Logged to `bot_logs/owner_positions.jsonl`. Off: `OWNER_POS_ENABLED=false`. Test: `python bots/tests/test_owner_positions_perps.py bots/perps.py`.

## 7. Owner rules (`rules/rules.md`)

Copy `rules/rules.example.md` to `rules/rules.md` and fill it with the owner: max size, max leverage, no-trade hours, loss limits. The rules override skill defaults but never the hard gates.

## 8. Tune the skills to the owner (before the first trade)

Read `skills/README.md` and every skill once. Walk the owner through anything that does not fit them (leverage bias, how often to tune with `update_params`, when to stand down). Change skills only with the owner, never loosen the hard gates, and record every change in `learnings/LEARNINGS.md`.

## 9. Feeds

Nothing to sign up for. In `bots/.env`:

- `FLOW_BASE_URL` (default Flow's public data; the bot reads only a fixed list of files under it).
- `FEED_CADENCE=manual` (default: pull Flow and news only when the agent writes `json_relay/feed_request.json` or uses query actions `pull_feeds` / `feed_packet`) or a number of minutes. Odds are never on a cadence: they are fetched live at every packet.
- `FLOW_STALE_MIN` (120), `NEWS_STALE_MIN` (180): older data is flagged stale, not dropped.
- `FEED_FAIL_LIMIT` (2), `FEED_FAILBACK_COOLDOWN_MIN` (10): failover to the next source and retry of the primary.
- `KALSHI_DAILY_CLOSE_HOUR_ET` (17): the end-of-day ladder close.
- `FLOW_EDITOR_PREFIX` (blank by default): the file prefix your Flow feed uses for its two editorial files (`<prefix>_thesis`, `mechanical_vs_<prefix>`). Blank skips those two; every other Flow lane works without them. The bot renames them to neutral fields (`editorial_thesis`, `editorial_vs_mechanical`).
- Macro prints (released CPI numbers in every packet as `macro_prints`): no key needed. Optional `BLS_API_KEY` (free, https://data.bls.gov/registrationEngine/) raises the BLS limit; optional `FRED_API_KEY` is the fallback when BLS refuses. `MACRO_BLS_DAILY_MAX` (12) caps fetches per day. Test: `python bots/feeds/macro_fetcher.py`.
- Test once: `python bots/feeds/feed_router.py pull` then `python bots/feeds/feed_router.py packet SOL`.

## 10. Console (`bots/perps_ui.py`)

`python bots/perps.py` opens it in the same window; `python bots/perps_ui.py` opens it alone. It reads state files and never trades (Settings edits are range-checked, confirmed and backed up).

- **1 Live Dashboard**: engine heartbeat, open positions (room to liquidation), PnL, recent closes, owner positions, tuner values changed from default. `A` archives, `R` reset PnL (the old ledger is archived, never deleted).
- **2 Logs**: one tab per source. `1` engine console, `2` trades, `3` declines, `4` owner positions, `5` agent inbox, `6` owner inbox, `7` JSON Relay (perps files only), `F` feeds; sub-bot consoles: `W` security_warden, `A` agent_autoagent, `T` code_tuner, `E` learnings_writer (console + `learnings_writer.jsonl`), `C` owner_commands, `X` acks, `L` launcher log. Every sub-bot tab reads the `bot_logs/<name>.console.log` that `perps.py` writes.
- **3 Settings**: every knob in `bots/tuner/perps_params.json` and the keys in `bots/.env` (secrets masked). Env changes apply on the next engine start.
- **4 Skills**: view and edit the skills.
- **5 Owner Commands**: send `/recap`, `/roast`, `/autopsy`, `/manage`, `/leave`.
- **6 Brain Setup**: pick `LLM_MAIN` and `LLM_FALLBACK`.
- **7 Quit UI**: closes the console; the engine keeps trading.
- **8 Stop Perps**: stops the engine and every sub-bot.

## 11. Build your automations

The bot only writes packets; nothing gets decided until you run your own loops. **Agent setup (required): you MUST build them yourself, from the templates in `automations/`, before the first live start.** Without a recurring perps_lane job the inbox fills up and no signal, TP/SL tune or exit decision ever happens.

1. Read `automations/README.md`, then each lane file: `perps_lane`, `warden_lane`, `morning_brief`, `misc_lane`.
2. For each lane, create one recurring job in your own host (cron, scheduled routine, heartbeat, whatever it supports). The job's prompt is the lane file's text with `{AGENT_NAME}` replaced by your name and `{USER}` by the owner's name (also set `AGENT_NAME` and `OWNER_NAME` in `bots/.env`; `learnings_writer` fills them into `learnings/LEARNINGS.md`).
3. Agree the cadence with the owner. The files carry no schedule on purpose. A starting point: perps_lane every 5 minutes (it handles signals and the per-check `update_params` TP/SL tuning), warden_lane every 15 minutes, misc_lane every 30 minutes, morning_brief once a day. Faster runs cost more model calls (section 0.1).
4. Derive the drain cadence from the cadences you or the owner chose, and re-gauge it whenever one changes. perps_lane should drain at least as often as the fastest packet producer: `FEED_CADENCE` (minutes, when not `manual`), the packet cadence (`PACKET_INTERVAL_SECONDS`, default 60 min) and the intel scan (15 min). Use that fastest interval, floored at 5 minutes and capped at 30. Examples: `FEED_CADENCE=5` -> perps_lane every 5 min; `FEED_CADENCE=manual` with default packets -> every 15 min; `FEED_CADENCE=60` -> every 15 min (the scan is faster). The setup wizard prints this suggestion from `bots/.env` when it finishes. warden_lane and misc_lane run on their own, slower schedules.
5. Run each job once by hand. Check it appended a line to `bot_logs/inbox_cron_audit.log` and moved its packets to `inbox_results/done/`.
6. Tell the owner which jobs exist and how often they run, and record it in `learnings/LEARNINGS.md`. Change cadences later the same way. The nightly learnings distill prompt is `learnings/nightly_self_eval.automation.md`.

Example staggered schedule (owner's real setup, cron syntax, local time). Offset the start minutes so no two lanes ever fire in the same minute; two lanes starting together race for the same packets and the same model budget:

```
3-53/10 * * * *        perps_lane      (every 10 min, at :03 :13 :23 :33 :43 :53)
7,27,47 * * * *        warden_lane     (every 20 min)
9,24,39,54 * * * *     misc_lane       (every 15 min)
15 7 * * *             morning_brief   (once a day)
30 23 * * *            nightly self-eval (learnings/nightly_self_eval.automation.md, once a night)
```

Check a new schedule by listing every start minute 0-59: each minute may appear at most once.

Don't run two copies of the same lane at once; the warden reaps stale lane locks after 15 minutes (`INBOX_CRON_LOCK_MAX_AGE_S`).

## 12. Optional pipes (off by default)

None. Every outside source the bot reads (Flow, Kalshi, Polymarket, Google News) is public, needs no key, and is pulled manually by default (`FEED_CADENCE=manual`).

## 13. Pre-start check

Before anything goes live, check:

- `bots/.env` loads (the wizard or `--setup` filled it, section 1.1), and no secrets are printed
- `pip install -r requirements.txt` ran in this Python (on Windows, `tzdata` must be installed or every bot crashes on start)
- your automations from section 11 exist and each has run once
- every folder in section 3 exists
- wallet (a burner), RPC and `rules/rules.md` are set; everything is live from the first start
- `NEW_POSITIONS_ENABLED`, `ENTRY_ALERTS_ENABLED` and the tuner knobs are what the owner wants (section 4)
- `python bots/perps.py` starts, the console shows the sub-bots running (Logs `L`), and one packet lands in `inbox/`
- you pick that packet up and move it to `inbox_results/done/`
- the owner gets one test ping (notify-only)

## 14. Tests

```
python bots/perps_ui.py --smoke
python bots/tests/test_owner_commands.py
python bots/tests/test_setup_wizard.py
python bots/tests/test_owner_positions_perps.py bots/perps.py
```

All offline; none touch the wallet.

## Tips

- **Do at least one trade with your SI start to finish.** Open, manage, close, then read the lesson `learnings_writer` logs in `learnings/LEARNINGS.md`. Trading alongside your SI is the fastest way it learns, it helps you both find new strategies, and it keeps the system evolving with a market that never stops changing.
- **It doesn't have to run 24/7.** Trading sessions are fine: turn it on when you want to trade, off when you're done.
- **Start small, on a burner.** Small collateral and a lower leverage cap (`PERP_MAX_LEVERAGE`) until you trust the setup, from a fresh burner wallet that holds only what you can lose.
- **Keep the Telegram chat private.** Alerts and owner commands go to `TELEGRAM_CHAT_ID`; use a private chat only you (and your SI) can read.
- **Read the acks and declines.** Every signal is answered in `bot_logs/perp_signal_acks/acks.jsonl` (`filled` with a tx, `processed`, or `declined`/`failed` with a reason). Fix the reason, don't resend with tweaked numbers.
- **Don't be afraid to build something better.** If you figure out how to make it twice as good, build it simple. That's the fastest way to keep up with an ever-changing market. Contributions welcome.
