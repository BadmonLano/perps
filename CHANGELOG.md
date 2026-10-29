# Changelog

Full history: [TIMELINE.md](TIMELINE.md).

## v1.0.6 (2026-10-29): always-on loop, active hours, tunable lane cadences
- **The bot is a loop meant to run continuously.** New "Always on: loop, cadence and active hours" section at the top of README.md and SETUP.md, plus notes in `skills/perps-boss/SKILL.md`, `automations/README.md` and each lane file: keep it on an always-on hosted agent or auto-start on boot, and keep the lanes as saved recurring automations.
- **`ACTIVE_HOURS` (default `00:00-23:59` = always on), `ACTIVE_TZ` (`America/New_York`), `ACTIVE_DAYS` (blank = every day).** Outside the window long/short/add/limit entries are declined (`outside_active_hours`) before any order is built, so nothing is cut off mid-order; closes, adjust, update_params, close_pct, TP/SL/trail and monitoring keep running, and it resumes on its own. `bots/active_hours.py` (+ `python bots/active_hours.py` status, no secrets).
- **Lane cadences (tunable):** `LANE_CADENCE_PERPS_LANE_MIN` (blank = wizard suggestion, 5-30), `LANE_CADENCE_MISC_LANE_MIN` (15), `LANE_CADENCE_WARDEN_LANE_MIN` (20), `LANE_MORNING_BRIEF_AT` (08:00), `LANE_NIGHTLY_EVAL_AT` (23:30). `PERP_POLL_SECONDS`, `FEED_CADENCE` unchanged, not duplicated.
- **Setup wizard:** active hours + timezone asked right after `AGENT_MODE` (setup table rows 2-3); end-of-wizard reminder says it is an always-on loop and honours `LANE_CADENCE_PERPS_LANE_MIN`.
- Defaults keep the old always-on behaviour. Tests: `bots/tests/test_active_hours.py`, `bots/tests/test_setup_wizard.py` updated.

## v1.0.5 (2026-10-29): autonomy is the first setup question
- `AGENT_MODE` (`suggest` default / `auto` with a warning and typed `AUTO` confirm) is the first wizard question and the first row of the setup table; `bots/.env.example` ships `AGENT_MODE=suggest`. The perps-boss skill tells the agent to propose and wait for the owner's OK in `suggest`. Paper stays the default (`PERPS_DRY_RUN=true`). No trading code or other defaults changed. Test: `bots/tests/test_setup_wizard.py`.

## v1.0.4 (2026-10-28): cursor.directory safety rule
- `rules/perps-safety.mdc: cursor.directory Rule component (paper default, no secrets, respect caps, confirm live orders). Docs/packaging only; no trading code or defaults changed.`

## v1.0.3 (2026-10-27): profit % across leverage, numbers-first opens
- TP/SL shown as price % and margin % in fill alerts, acks and the dashboard.
- Trail arm/giveback scale with leverage from the 5x values (`PERP_TRAIL_LEV_SCALE=sqrt`, `PERP_TRAIL_REF_LEVERAGE=5`), tiers too; giveback capped at 50.
- Trail and its peak run on after-fee P/L; the monitor line shows gross and net.
- Open warns (never blocks) when the SL sits past 60% of entry-to-liquidation (`PERP_SL_LIQ_WARN_FRAC`).
- perps-boss SKILL.md: unit lines, 20x-cap example, "Deciding Whether To Open" (raw numbers over narrative). README examples labelled price %.
- Tests: `test_profit_units.py`; 40/40 pass, `--smoke` passes. Paper default, 20x cap and other defaults unchanged.
Notes: [docs/RELEASE_NOTES_v1.0.3.md](docs/RELEASE_NOTES_v1.0.3.md).

## v1.0.2 (2026-10-27): repo parity with Meme Sniper
Docs and packaging only. No trading logic or defaults changed; paper stays the default.
- Plugin manifests `.cursor-plugin/plugin.json` and `.grok-plugin/plugin.json` (9 skills).
- README: Works with, Contents, Install as plugin, Windows/Mac/Linux, Contributing, Versions and releases, Updating, License sections.
- `CHANGELOG.md`; `assets/social-preview.png` (1280x640).

## v1.0.1 (2026-10-27): docs and safer defaults
Paper default stated plainly, paper quick start, `--help`/`--version`, example signals, glossary, default leverage cap 20x. Notes: [docs/RELEASE_NOTES_v1.0.1.md](docs/RELEASE_NOTES_v1.0.1.md).

## v1.0.0 (2026-10-27): first public release
Paper mode, oracle halt, open/add guards. Notes: [docs/RELEASE_NOTES_v1.0.0.md](docs/RELEASE_NOTES_v1.0.0.md).
