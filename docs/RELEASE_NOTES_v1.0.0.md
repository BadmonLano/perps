# Perps v1.0.0

First public release.

- Jupiter Perps on SOL, BTC and ETH. Your SI agent calls the shots within your rules; the bot executes and guards.
- **Paper mode first** (`PERPS_DRY_RUN=true`, the default): simulated fills at the oracle price, a separate paper book and P/L, full stop/TP protection, and nothing ever signed or sent. No wallet needed. Toggle in the console (Settings) or `bots/.env`.
- Bring your own wallet, RPC and agent. Setup wizard writes `bots/.env` (values never shown).
- Bot-side stop, take-profit, trail and exit-floor alerts, plus liquidation watch. The bot never auto-closes.
- Opens and adds are refused while the position monitor is not running, and adds are refused when the estimated liquidation would sit too close to the stop.
- Prices from the same Pyth oracle Jupiter validates against, resolved live from the pool's custody accounts. If that read fails, trading halts with an alert instead of falling back to the old oracle map.
- Owner positions: positions you open by hand get tagged, and you choose manage or leave.
- Optional brain (Grok, Ollama, or off), optional Telegram alerts and owner commands.
- Sub-bots: security_warden, agent_autoagent, code_tuner, learnings_writer.
- Console with live dashboard, logs and archives.

Leverage liquidates. Read the risk notes in README before going live.
