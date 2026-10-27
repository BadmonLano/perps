# Owner trading rules (example)

Copy this file to `rules/rules.md` and fill it in with the owner.

The agent reads this file before every trade decision. These rules override skill defaults.
The bot's hard gates (leverage from 10x up to `PERP_MAX_LEVERAGE`, 20x by default; explicit collateral; live price) still apply.
Leave a section empty if it does not apply.

## Max size
<!-- Largest collateral per trade in SOL or USD (e.g. 0.25 SOL / $50) -->


## Max leverage
<!-- Your own cap between 10x and PERP_MAX_LEVERAGE (20x default), e.g. 15x -->


## No-trade hours
<!-- Local hours or days you never open (e.g. 00:00-07:00, weekends) -->


## Loss limits
<!-- Daily / weekly loss that stops trading (e.g. -$50 per day) -->


## Exits
<!-- Your own SL/TP or trail rules (e.g. SL always set, never past 1.5%) -->


## Other
<!-- Anything else the agent must always or never do -->
