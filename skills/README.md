# Skills

The skills are the agent's playbook. The bots produce data and packets; the skills tell the agent how to read them, decide and act. Each skill is a `SKILL.md` with a short front-matter header (name, version, description) and the full instructions below it.

## Load order

1. **`perps-boss`** (core, always loaded): trading doctrine for the perps bot (venue, leverage vocabulary, signal format, position modification with `update_params`, feed lanes, hard gates, directives).
2. **`forensics`**: after closes and on weekly reviews. Autopsies of closed and missed perp trades; writes proposals, never edits live params.
3. **`pnl-tracker`**: realized and unrealized PnL, fees and funding.
4. **Sub-skills**, loaded when their packets or tasks come up:

| skill | load when |
|---|---|
| `sub-skills/warden` | security_warden packets: wallet anomalies, integrity, crashes, perps-down pings |
| `sub-skills/code-monitor` | code_tuner packets about the bots' code and config |
| `sub-skills/autoagent` | overseer reports, ecosystem audits, owner commands (`/roast`, `/recap`, `/autopsy`) |
| `sub-skills/owner-ping` | any time you notify the owner |
| `sub-skills/morning-brief` | the daily brief to the owner |
| `sub-skills/learnings-summarizer` | the nightly learnings distill |

Before acting on any trade, also read `rules/rules.md` (the owner's rules; they override skill defaults but never the hard safety gates).

`automations/` has one prompt per inbox lane and says which skill each lane uses.

## Loading perps-boss

`skills/perps-boss/SKILL.md` is short enough to load whole. Find sections by their `##` headings:

1. **Always:** the front matter, "Venue", "Leverage Vocabulary" and "Hard Gates".
2. **For a trade decision:** "Signal Format", "Each Cycle", "Feed Lanes In The Packet".
3. **For open positions:** "Position Modification" (`update_params` on every position check).
4. **For tuning the bot:** "Directives".

## Leverage, sizes and per-check tuning

SETUP.md section 4. Leverage runs from 10x up to `PERP_MAX_LEVERAGE` (70x default) and sizing is explicit (`collateral_sol`, `collateral_usd` or `size_usd`). On every position check you may move TP, SL, trail and exit floor with `update_params` (perps-boss "Position Modification"); every change is logged to `bot_logs/perps_param_changes.jsonl` and the owner is told. Bot-wide knobs in `bots/tuner/perps_params.json` change only by directive (`json_relay/perp_meeting.json`); review `perps_param_changes.jsonl` weekly with the forensics skill.

## Changing skills

The skills ship tuned for a cautious, newer owner. Change them only with the owner, never loosen the hard gates, and record every change in `learnings/LEARNINGS.md` (SETUP.md section 8).
