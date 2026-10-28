# Changelog

Full history: [TIMELINE.md](TIMELINE.md).

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
