#!/usr/bin/env python3
"""send_signal - write a perps signal to the right per-chain file and wait for its ack.

  python bots/tools/send_signal.py --market SOL --action long --collateral-usd 11 \
      --tp-pct 2 --sl-pct 1 --reason "why" --wait
  python bots/tools/send_signal.py --market BTC --action update_params --sl-pct 0.8 --reason "tighten" --wait
  python bots/tools/send_signal.py --market ETH --action close --reason "done" --wait

Writes json_relay/perp_<chain>_signal.json (never the legacy perp_signal.json), adds
perps_is_order:true on opens, leaves leverage out unless you pass --leverage (the
ladder sets it). With --wait it watches bot_logs/perp_signal_acks/acks.jsonl for the
ack of THIS signal and prints one JSON line: status, latency_s, tx, ack.
Exit code: 0 filled/processed/parked (a limit entry waiting for its price), 2 declined/failed/expired, 3 no ack before the timeout,
4 a previous signal for this chain is still unconsumed.
Opens/closes fill with a tx; update_params returns status "processed" with no tx.
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _layout import layout
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
except Exception:
    pass

OPEN = {"long", "short", "add", "open_long", "open_short", "buy", "sell"}


def _acks_lines(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", required=True, help="SOL | BTC | ETH (or SOL-PERP ...)")
    ap.add_argument("--action", required=True, help="long|short|close|update_params|adjust|add|close_pct|...")
    ap.add_argument("--reason", required=True, help="one-line reason (always required)")
    ap.add_argument("--collateral-usd", type=float)
    ap.add_argument("--collateral-sol", type=float)
    ap.add_argument("--size-usd", type=float, help="notional instead of collateral")
    ap.add_argument("--leverage", type=float, help="omit to let the ladder pick")
    ap.add_argument("--tp-pct", type=float); ap.add_argument("--sl-pct", type=float)
    ap.add_argument("--tp-price", type=float); ap.add_argument("--sl-price", type=float)
    ap.add_argument("--extra", help='extra JSON keys, e.g. \'{"trail_activate_pct":20}\'')
    ap.add_argument("--wait", nargs="?", const=90, type=float, default=None, help="wait for the ack (default 90s)")
    ap.add_argument("--dry-run", action="store_true", help="print the file and payload, write nothing")
    a = ap.parse_args()

    chain = a.market.upper().replace("-PERP", "")
    if chain not in ("SOL", "BTC", "ETH"):
        sys.exit(f"unknown market {a.market}")
    act = a.action.lower().strip()
    sig = {"action": act, "market": f"{chain}-PERP", "reason": a.reason}
    if act in OPEN:
        sig["perps_is_order"] = True
    for k in ("collateral_usd", "collateral_sol", "size_usd", "leverage", "tp_pct", "sl_pct", "tp_price", "sl_price"):
        v = getattr(a, k)
        if v is not None:
            sig[k] = v
    if a.extra:
        sig.update(json.loads(a.extra))

    L = layout()
    path = os.path.join(L["relay"], f"perp_{chain.lower()}_signal.json")
    acks = os.path.join(L["logs"], "perp_signal_acks", "acks.jsonl")
    if a.dry_run:
        print(json.dumps({"file": path, "acks": acks, "signal": sig}))
        return 0
    for _ in range(30):  # a previous signal for this chain must be consumed first
        if not os.path.exists(path):
            break
        time.sleep(1)
    else:
        print(json.dumps({"status": "busy", "error": f"{path} still unconsumed after 30s"}))
        return 4
    before = len(_acks_lines(acks))
    os.makedirs(L["relay"], exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        json.dump(sig, f)
    t0 = time.time()
    os.replace(tmp, path)  # atomic: the bot never reads half a file
    if a.wait is None:
        print(json.dumps({"status": "written", "file": path, "signal": sig}))
        return 0
    while time.time() - t0 < a.wait:
        time.sleep(0.5)
        lines = _acks_lines(acks)
        for ln in lines[before:]:
            try:
                ack = json.loads(ln)
            except Exception:
                continue
            if ack.get("signal_file") == os.path.basename(path) and ack.get("signal") == sig:
                st = ack.get("status")
                out = {"status": st, "latency_s": round(time.time() - t0, 1), "tx": ack.get("tx"), "ack": ack}
                print(json.dumps(out, default=str))
                return 0 if st in ("filled", "processed", "parked") else 2
    print(json.dumps({"status": "timeout", "latency_s": round(time.time() - t0, 1),
                      "error": f"no ack in {a.wait:g}s - check the engine console and heartbeat", "file": path}))
    return 3


if __name__ == "__main__":
    sys.exit(main())
