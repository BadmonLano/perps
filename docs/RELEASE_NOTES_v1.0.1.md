# Perps v1.0.1

Docs and safer defaults. No trading logic changed.

- **Paper by default, said plainly.** The README warning now matches the code: `PERPS_DRY_RUN=true` out of the box; real transactions only with `PERPS_DRY_RUN=false`.
- **Paper quick start:** clone, install, copy `.env.example`, run. No wallet needed (press Enter to skip the key).
- **`python bots/perps.py --help`** (and `--version`) prints and exits instead of starting the engine.
- **Example signals** (open, update_params, close) in the README and `bots/examples/`, with `send_signal.py` usage.
- **Glossary** (packet, signal, lane, gates, ratchet, peel, exit floor, Flow, paper mode) and a pointer to `skills/perps-boss` for the trade-management style.
- **Default leverage cap 70x -> 20x**, ladder `0.1:20,1:15,10:12,50:10`. An existing `bots/.env` keeps its values.
- `FLOW_EDITOR_PREFIX` blank by default and documented; cron detail moved to SETUP.md section 11; docs stand alone; real clone URL; Linux noted as working.

Leverage liquidates. Use a burner wallet and run paper first.
