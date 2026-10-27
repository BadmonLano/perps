"""Shared path layout for the perps agent tools (send_signal, lane_run).

Works in both setups without config:
  drop (repo):  <root>/bots/perps.py      -> relay <root>/json_relay, logs <root>/bot_logs,
                                             inbox <root>/inbox (WORKSPACE_DIR overrides <root>)
  live:         <ws>/bots/perps_bot.py    -> relay <ws>/bots/json_relay, logs <ws>/bots/bot_logs,
                                             inbox <ws>/inbox
"""
import os

TOOLS = os.path.dirname(os.path.abspath(__file__))
BOTS = os.path.dirname(TOOLS)


def layout():
    if os.path.exists(os.path.join(BOTS, "perps.py")):
        root = (os.environ.get("WORKSPACE_DIR") or "").strip() or os.path.dirname(BOTS)
        root = os.path.abspath(os.path.expanduser(root))
        return {"kind": "drop", "root": root, "relay": os.path.join(root, "json_relay"),
                "logs": os.path.join(root, "bot_logs"), "inbox": os.path.join(root, "inbox"),
                "results": os.path.join(root, "inbox_results"),
                "audit": os.path.join(root, "bot_logs", "inbox_cron_audit.log")}
    ws = os.path.dirname(BOTS)
    return {"kind": "live", "root": ws, "relay": os.path.join(BOTS, "json_relay"),
            "logs": os.path.join(BOTS, "bot_logs"), "inbox": os.path.join(ws, "inbox"),
            "results": os.path.join(ws, "inbox_results"),
            "audit": os.path.join(BOTS, "bot_logs", "inbox_cron_audit.log")}
