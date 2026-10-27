# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems (anything that could leak keys, move funds, bypass a safety gate or let an outsider control the bot).

Report it privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** (a private security advisory). Include what you found, how to reproduce it, and what an attacker could do with it. We'll reply in the advisory.

For non-sensitive bugs, a normal GitHub issue is fine.

## Keeping your setup safe

- Run with a **burner hot wallet** that holds only what you can afford to lose.
- Keep `bots/.env` and `setup.json` private; they are in `.gitignore` for a reason. Never paste them into an issue, a chat or a log.
- Use your own RPC and model keys, and rotate them if they were ever exposed.
- The bot trades on any signal file it finds: protect `json_relay/` and `inbox/` from other users and processes on the machine.
- The bot is live by default and trades with leverage. Read the warning in the README before the first start.
