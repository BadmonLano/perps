# Contributing

Thanks for wanting to help. This code moves real money with leverage, so the bar is "safe and tested" before "clever".

## Ground rules

- **Never commit secrets.** No private keys, API keys, `.env` files, `setup.json` or wallet addresses you own. `.gitignore` covers the usual files; check `git diff --staged` anyway.
- **No personal data** in code, skills or docs (names, handles, home paths).
- **Keep it self-contained.** Don't reference bots, files or services that aren't in this repo. Every path resolves inside the repo root (`WORKSPACE_DIR`, else the folder that holds `bots/`). If a feature needs an outside service, put it behind an env switch that defaults to off or to manual (see `FEED_CADENCE`).
- **The hard safety gates are not up for loosening** in a PR: the leverage band (10x up to `PERP_MAX_LEVERAGE`), collateral checks, live-price checks, exit floor and liquidation guards.
- **UI code must never be able to stop trading.** Console faults are caught and logged.

## Before you open a PR

Run everything in the README "Tests" section (all offline), plus:

```
python -m py_compile bots/perps.py bots/perps_ui.py bots/feed_hook.py bots/perps_heartbeat_watch.py bots/perps_setup.py
```

Describe what changed, why, and how you tested it. If you change a skill, say which behaviour of the agent it changes.

## Issues

Use GitHub issues for bugs and ideas. Include your OS, Python version, the menu option or command you ran, and the relevant lines from `bot_logs/` with any keys or addresses removed. For security problems, see [SECURITY.md](SECURITY.md) instead.
