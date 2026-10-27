#!/usr/bin/env python3
"""lane_run - one-step inbox drain for the perps / warden / misc lanes.

Step 1, list what the lane owns (oldest first) with a one-line summary each:
  python bots/tools/lane_run.py --lane perps --list
Step 2, decide, then apply. Every file gets an actions_taken entry stamped INSIDE it
(JSON: appended to the "actions_taken" list; .md: an "## actions_taken" section at the
end), is moved under its ORIGINAL name to inbox_results/done (or failed + .failure.md),
and ONE audit line is appended to bot_logs/inbox_cron_audit.log:
  python bots/tools/lane_run.py --lane perps --apply decisions.json --positions-checked 3 --params-updated 0
decisions.json = {"<filename>": {"action": "what you decided/did", "result": "done"|"failed"|"leave",
                                 "failure": "what blocked, what you tried, what is needed"}}
  --default-action "text"  stamps every listed file NOT in decisions.json with that text (cheap acks).
Files you mark "leave" stay in inbox and count as left (exit=partial).
Never touched: .gitkeep, perp_exit_suggestion.json (the bot rewrites/clears it), any other lane's files.
Audit line (same keys on every lane):
  [ISO] | started=N | handled=N | left=N | failed=N | exit=drained|partial|blocked | positions_checked=N |
  params_updated=N | real_findings=N | patches_authored=N | unknown=N | source=<lane>
"""
import argparse, json, os, shutil, sys
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _layout import layout
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
except Exception:
    pass

NEVER = {".gitkeep", "perp_exit_suggestion.json"}
PERPS = ("perps_bot - ", "perp - ")
WARDEN = ("security_warden - ",)


def owns(lane, name):
    if name in NEVER or name.startswith(".") or name.endswith(".tmp"):
        return False
    if lane == "perps":
        return name.startswith(PERPS)
    if lane == "warden":
        return name.startswith(WARDEN)
    return not name.startswith(PERPS + WARDEN)


def files(lane, inbox):
    out = []
    for n in os.listdir(inbox):
        p = os.path.join(inbox, n)
        if os.path.isfile(p) and owns(lane, n):
            out.append((os.path.getmtime(p), n))
    return [n for _, n in sorted(out)]


def summary(p):
    try:
        if p.endswith(".json"):
            with open(p, encoding="utf-8-sig") as f:
                j = json.load(f)
            if isinstance(j, dict):
                if j.get("chain"):
                    fl = "; ".join(map(str, j.get("flags") or []))
                    call = ((j.get("flow") or {}).get("call") or {}).get("direction")
                    return f"chain packet {j['chain']} | flow {call} | flags: {fl}"
                for k in ("message", "summary", "note", "kind", "type", "_type"):
                    if j.get(k):
                        return " ".join(str(j[k]).split())[:200]
                return "keys: " + ",".join(list(j)[:8])
        with open(p, encoding="utf-8", errors="replace") as f:
            return " ".join(f.read(400).split())[:200]
    except Exception as e:
        return f"UNREADABLE: {e}"


def stamp(p, entry):
    if p.endswith(".json"):
        with open(p, encoding="utf-8-sig") as f:
            j = json.load(f)
        if not isinstance(j, dict):
            j = {"_original": j}
        at = j.get("actions_taken")
        j["actions_taken"] = (at if isinstance(at, list) else ([at] if at else [])) + [entry]
        with open(p, "w", encoding="utf-8") as f:
            json.dump(j, f, indent=2, ensure_ascii=False, default=str)
    else:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n\n## actions_taken\n- {entry['at']} [{entry['lane']}] {entry['action']}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lane", required=True, choices=["perps", "warden", "misc"])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply")
    ap.add_argument("--default-action")
    ap.add_argument("--agent", default=os.environ.get("AGENT_NAME", "agent"))
    for k in ("positions_checked", "params_updated", "real_findings", "patches_authored", "unknown"):
        ap.add_argument("--" + k.replace("_", "-"), type=int, default=0)
    a = ap.parse_args()
    L = layout()
    fs = files(a.lane, L["inbox"])
    if a.list or not (a.apply or a.default_action):
        print(json.dumps({"lane": a.lane, "inbox": L["inbox"], "count": len(fs),
                          "files": [{"file": n, "summary": summary(os.path.join(L["inbox"], n))} for n in fs]},
                         indent=1, ensure_ascii=False))
        return 0
    dec = {}
    if a.apply:
        with open(a.apply, encoding="utf-8-sig") as f:
            dec = json.load(f)
    done_d, fail_d = os.path.join(L["results"], "done"), os.path.join(L["results"], "failed")
    os.makedirs(done_d, exist_ok=True); os.makedirs(fail_d, exist_ok=True)
    handled = failed = left = 0
    now = lambda: datetime.now().astimezone().isoformat(timespec="seconds")
    for n in fs:
        d = dec.get(n) or ({"action": a.default_action, "result": "done"} if a.default_action else None)
        if not d or d.get("result") == "leave":
            left += 1
            continue
        act = (d.get("action") or "").strip()
        if not act:
            print(f"refusing to move {n}: empty action", file=sys.stderr)
            left += 1
            continue
        src = os.path.join(L["inbox"], n)
        try:
            stamp(src, {"at": now(), "lane": a.lane, "agent": a.agent, "action": act})
            if d.get("result") == "failed":
                shutil.move(src, os.path.join(fail_d, n))
                with open(os.path.join(fail_d, n + ".failure.md"), "w", encoding="utf-8") as f:
                    f.write(f"# {n}\n\n{d.get('failure') or act}\n")
                failed += 1
            else:
                shutil.move(src, os.path.join(done_d, n))
                handled += 1
        except Exception as e:
            print(f"error on {n}: {e}", file=sys.stderr)
            left += 1
    left_now = len(files(a.lane, L["inbox"]))
    ex = "drained" if left_now == 0 else ("blocked" if handled + failed == 0 and fs else "partial")
    line = (f"[{now()}] | started={len(fs)} | handled={handled} | left={left_now} | failed={failed} | exit={ex} | "
            f"positions_checked={a.positions_checked} | params_updated={a.params_updated} | "
            f"real_findings={a.real_findings} | patches_authored={a.patches_authored} | unknown={a.unknown} | "
            f"source={a.lane}")
    os.makedirs(os.path.dirname(L["audit"]), exist_ok=True)
    with open(L["audit"], "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
