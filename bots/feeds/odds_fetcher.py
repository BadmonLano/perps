"""odds_fetcher.py - live prediction-market odds, no keys.

Kalshi (api.elections.kalshi.com, market data needs no auth): BTC/ETH/SOL "above $X"
ladders, both the nearest hourly event and the end-of-day (5pm ET) event, (KXBTCD/KXETHD/KXSOLD) -> implied_median and p_up vs spot, plus
macro series (Fed, CPI, unemployment). Polymarket: Gamma events (tag crypto) for
price-threshold markets and 24h movers, CLOB midpoint for a live YES price.

fetch_odds() is called live at decision time and is never served from cache. It also
writes the shared schemas other bots read:
  bot_logs/kalshi_implied_probs.json, bot_logs/polymarket_markets.json,
  bot_logs/polymarket_implied_probabilities.json
CLI: python bots/feeds/odds_fetcher.py   (one fetch, prints a summary)
KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH are optional slots, unused (reads need no auth).
"""
import json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from feeds._common import env, bot_logs, now_iso, http_get, write_json  # noqa: E402

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
KALSHI_CRYPTO = {"KXBTCD": "BTC", "KXETHD": "ETH", "KXSOLD": "SOL"}
KALSHI_MACRO = {"KXFED": "fed_funds_next_meeting", "KXCPI": "cpi_mom", "KXCPIYOY": "cpi_yoy",
                "KXU3": "unemployment_u3"}
ASSET_WORDS = {"BTC": ("bitcoin", "btc"), "ETH": ("ethereum", "eth ", "ether "), "SOL": ("solana", "sol ")}


def _f(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _kalshi_prob(m):
    """YES mid in 0-1. Kalshi returns *_dollars fields as strings in dollars (0-1)."""
    b, a = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    if b is None and a is None:
        return None
    return (b + a) / 2.0 if (b is not None and a is not None) else (b if b is not None else a)


def _kalshi_pull(series):
    r = http_get(f"{KALSHI_BASE}/markets", params={"series_ticker": series, "status": "open", "limit": 500})
    return (r or {}).get("markets") or []


def _events(rows):
    now = datetime.now(timezone.utc)
    ev = {}
    for m in rows:
        try:
            ct = datetime.fromisoformat(str(m.get("close_time")).replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now:
            continue
        ev.setdefault(m.get("event_ticker"), {"close": ct, "rows": []})["rows"].append(m)
    return ev


def _nearest_event(rows):
    ev = _events(rows)
    if not ev:
        return None, None
    k = min(ev, key=lambda e: ev[e]["close"])
    return k, ev[k]


def _hourly_and_daily(rows):
    """(hourly, daily): hourly = nearest future event; daily = nearest future event that
    closes at the end-of-day hour (KALSHI_DAILY_CLOSE_HOUR_ET, default 17 = 5pm ET)."""
    ev = _events(rows)
    if not ev:
        return (None, None), (None, None)
    order = sorted(ev, key=lambda e: ev[e]["close"])
    h = int(env("KALSHI_DAILY_CLOSE_HOUR_ET", "17"))
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone.utc
    daily = next((e for e in order if ev[e]["close"].astimezone(et).hour == h), None)
    return (order[0], ev[order[0]]), ((daily, ev[daily]) if daily else (None, None))


def _ladder(rows):
    """[(strike, p_above, oi)] sorted by strike, for strike_type 'greater' rows."""
    out = []
    for m in rows:
        if (m.get("strike_type") or "greater") != "greater":
            continue
        s, p = _f(m.get("floor_strike")), _kalshi_prob(m)
        if s is None or p is None:
            continue
        out.append((s, p, _f(m.get("open_interest_fp")) or 0.0))
    return sorted(out)


def _median(ladder):
    """Strike where P(above) crosses 0.5, linear between neighbours."""
    for (s1, p1, _), (s2, p2, _) in zip(ladder, ladder[1:]):
        if p1 >= 0.5 >= p2 and p1 != p2:
            return round(s1 + (p1 - 0.5) / (p1 - p2) * (s2 - s1), 4)
    return None


def _p_above(ladder, x):
    if not ladder or x is None:
        return None
    for (s1, p1, _), (s2, p2, _) in zip(ladder, ladder[1:]):
        if s1 <= x <= s2:
            return round(p1 + (x - s1) / (s2 - s1) * (p2 - p1), 4) if s2 != s1 else round(p1, 4)
    return None


def fetch_kalshi(spot=None):
    """spot: {asset: price} for p_up. Missing spot -> p_up None (median still given)."""
    spot = spot or {}
    out = {"_updated": now_iso(), "last_updated": None, "source": "kalshi", "crypto": {}, "macro": {}, "errors": {}}
    series = list(KALSHI_CRYPTO) + list(KALSHI_MACRO)
    with ThreadPoolExecutor(max_workers=4) as ex:
        res = dict(zip(series, ex.map(lambda s: _safe(_kalshi_pull, s), series)))
    for s, asset in KALSHI_CRYPTO.items():
        rows, err = res[s]
        if err:
            out["errors"][s] = err
            continue
        px = _f(spot.get(asset))
        (hk, hb), (dk, db) = _hourly_and_daily(rows)

        def _one(ev, blob):
            if not blob:
                return None
            lad = _ladder(blob["rows"])
            if len(lad) < 3:
                return None
            return {"series": s, "event": ev, "expiry": blob["close"].isoformat(), "strikes": len(lad),
                    "implied_median": _median(lad), "spot": px, "p_up": _p_above(lad, px),
                    "ladder": [{"strike": a, "p_above": round(b, 4), "oi": c} for a, b, c in lad if c > 0][-40:]}
        hr, dy = _one(hk, hb), (_one(dk, db) if dk != hk else None)
        if dy is None and dk == hk:
            dy = hr                      # the nearest event IS the end-of-day one
        if not hr and not dy:
            continue
        # top level mirrors hourly (older readers); hourly/daily carry both ladders
        out["crypto"][asset] = {**(hr or dy), "hourly": hr, "daily": dy}
    for s, label in KALSHI_MACRO.items():
        rows, err = res[s]
        if err:
            out["errors"][s] = err
            continue
        ev, blob = _nearest_event(rows)
        if not blob:
            continue
        top = max(blob["rows"], key=lambda m: _f(m.get("volume_24h_fp")) or 0.0)
        p = _kalshi_prob(top)
        out["macro"][label] = {"series": s, "event": ev, "expiry": blob["close"].isoformat(),
                               "crossing_50pct": _median(_ladder(blob["rows"])),
                               "most_traded": {"title": (top.get("title") or "")[:160],
                                               "yes_sub_title": top.get("yes_sub_title"),
                                               "implied_prob": round(p, 4) if p is not None else None,
                                               "volume_24h": _f(top.get("volume_24h_fp"))}}
    out["last_updated"] = out["_updated"]
    return out


def _safe(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:
        return [], str(e)


# ── Polymarket ──────────────────────────────────────────────────────────────
_BELOW_RX = re.compile(r"\b(dip|below|less than|under|fall|drop)\b", re.I)
_THRESH_RX = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM])?")


def _threshold(q):
    m = _THRESH_RX.search(q or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    mult = {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)
    return v * mult


def _asset_of(q):
    ql = " " + (q or "").lower() + " "
    for a, words in ASSET_WORDS.items():
        if any(w in ql for w in words):
            return a
    return None


def _yes_price(m):
    try:
        prices = json.loads(m.get("outcomePrices") or "[]")
        outs = json.loads(m.get("outcomes") or "[]")
        i = [o.lower() for o in outs].index("yes") if "yes" in [o.lower() for o in outs] else 0
        return float(prices[i])
    except Exception:
        return _f(m.get("lastTradePrice"))


def _yes_token(m):
    try:
        return (json.loads(m.get("clobTokenIds") or "[]") or [None])[0]
    except Exception:
        return None


def _clob_mid(token):
    if not token:
        return None
    try:
        return _f((http_get(f"{CLOB_BASE}/midpoint", params={"token_id": token}, timeout=10) or {}).get("mid"))
    except Exception:
        return None


def fetch_polymarket(min_liquidity=None, max_events=None):
    min_liq = float(min_liquidity if min_liquidity is not None else env("POLY_MIN_LIQUIDITY_USD", "5000"))
    limit = int(max_events or env("POLY_MAX_EVENTS", "100"))
    events = http_get(f"{GAMMA_BASE}/events", params={"tag_slug": "crypto", "active": "true", "closed": "false",
                                                        "order": "volume24hr", "ascending": "false",
                                                        "limit": limit}) or []
    now = datetime.now(timezone.utc)
    markets, movers, book = [], [], {"BTC": [], "ETH": [], "SOL": []}
    for e in events:
        for m in e.get("markets") or []:
            if m.get("closed") or not m.get("active", True):
                continue
            q = m.get("question") or ""
            liq = _f(m.get("liquidityNum")) or _f(m.get("liquidity")) or 0.0
            yes = _yes_price(m)
            chg = _f(m.get("oneDayPriceChange"))
            url = f"https://polymarket.com/event/{e.get('slug') or m.get('slug')}"
            row = {"id": m.get("id"), "question": q, "yes_price": yes, "liquidity_usd": liq,
                   "volume_24h_usd": _f(m.get("volume24hr")), "end_date": m.get("endDate"), "url": url,
                   "one_day_change": chg, "event": e.get("title")}
            markets.append(row)
            if chg is not None and abs(chg) >= float(env("POLY_MOVER_MIN_SHIFT", "0.10")) and liq >= min_liq:
                movers.append({"question": q, "outcome": "Yes", "prev_price": round((yes or 0) - chg, 4),
                               "curr_price": yes, "shift": round(abs(chg), 4), "direction": "UP" if chg > 0 else "DOWN",
                               "url": url, "timestamp": now_iso()})
            a, t = _asset_of(q), _threshold(q)
            if a and t and liq >= min_liq and yes is not None:
                try:
                    end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
                    days = round((end - now).total_seconds() / 86400, 2)
                except Exception:
                    end, days = None, None
                book[a].append({"market_id": m.get("id"), "question": q, "threshold_usd": t,
                                "deadline": m.get("endDate"), "days_to_resolution": days,
                                "horizon": None if days is None else ("short" if days <= 2 else "medium" if days <= 30 else "long"),
                                "yes_price": yes, "implied_prob": yes, "liquidity_usd": liq,
                                "volume_24h_usd": row["volume_24h_usd"], "url": url, "_token": _yes_token(m),
                                "side": "below" if _BELOW_RX.search(q) else "above",
                                "parser_used": "regex_threshold", "signal_type": "price_threshold"})
    # live CLOB midpoint for the most liquid few per asset
    picks = [r for a in book for r in sorted(book[a], key=lambda r: -(r["liquidity_usd"] or 0))[:6]]
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r, mid in zip(picks, ex.map(lambda r: _clob_mid(r.get("_token")), picks)):
            if mid is not None:
                r["implied_prob"], r["price_source"] = mid, "clob_midpoint"
    for a in book:
        for r in book[a]:
            r.pop("_token", None)
            r.setdefault("price_source", "gamma")
        book[a].sort(key=lambda r: (r["days_to_resolution"] if r["days_to_resolution"] is not None else 9e9,
                                    -(r["liquidity_usd"] or 0)))
    ts = now_iso()
    pm_markets = {"last_updated": ts, "_updated": ts, "source": "polymarket",
                  "markets": sorted(markets, key=lambda r: -(r["volume_24h_usd"] or 0))[:200],
                  "movers": movers, "signal_context": {"events_scanned": len(events)}}
    pm_probs = {"last_updated": ts, "_updated": ts, "source": "polymarket",
                "implied_probabilities": {a: {"thresholds": v[:30], "ecosystem_signals": []} for a, v in book.items() if v},
                "extracted_from_market_count": len(markets)}
    return pm_markets, pm_probs


def fetch_odds(spot=None, write=True):
    """Live fetch of both venues. Never cached. Returns the odds_live packet block."""
    out = {"fetched_at": now_iso(), "source": "kalshi+polymarket direct", "ok": True, "errors": {}}
    with ThreadPoolExecutor(max_workers=2) as ex:
        fk = ex.submit(fetch_kalshi, spot)
        fp = ex.submit(fetch_polymarket)
        try:
            k = fk.result()
        except Exception as e:
            k, out["errors"]["kalshi"] = None, str(e)
        try:
            pm_markets, pm_probs = fp.result()
        except Exception as e:
            pm_markets = pm_probs = None
            out["errors"]["polymarket"] = str(e)
    if write:
        if k and k.get("crypto"):
            write_json(bot_logs("kalshi_implied_probs.json"), k)
        if pm_markets:
            write_json(bot_logs("polymarket_markets.json"), pm_markets)
            write_json(bot_logs("polymarket_implied_probabilities.json"), pm_probs)
    out["kalshi"] = k
    out["polymarket"] = {"movers": (pm_markets or {}).get("movers"),
                         "price_markets": (pm_probs or {}).get("implied_probabilities")} if pm_markets else None
    out["ok"] = bool((k and k.get("crypto")) or pm_markets)
    return out


def _slim(x):
    if not x:
        return None
    return {**{key: x.get(key) for key in ("series", "event", "expiry", "implied_median", "spot", "p_up", "strikes")},
            "ladder": (x.get("ladder") or [])[-12:]}


def odds_for_chain(odds, chain, max_items=6):
    C = (chain or "").upper().replace("-PERP", "")
    k = ((odds or {}).get("kalshi") or {}).get("crypto", {}).get(C)
    pm = (odds or {}).get("polymarket") or {}
    words = ASSET_WORDS.get(C, ())
    movers = [m for m in (pm.get("movers") or []) if any(w in " " + m["question"].lower() + " " for w in words)]
    return {"fetched_at": (odds or {}).get("fetched_at"), "source": (odds or {}).get("source"),
            "ok": (odds or {}).get("ok"), "errors": (odds or {}).get("errors"),
            "kalshi": ({"hourly": _slim(k.get("hourly")), "daily": _slim(k.get("daily"))} if k else None),
            "kalshi_macro": ((odds or {}).get("kalshi") or {}).get("macro"),
            "polymarket": {"price_markets": ((pm.get("price_markets") or {}).get(C) or {}).get("thresholds", [])[:max_items],
                           "movers": movers[:max_items]}}


if __name__ == "__main__":
    o = fetch_odds()
    for C in ("BTC", "ETH", "SOL"):
        c = odds_for_chain(o, C)
        print(C, "kalshi hourly/daily median", ((c["kalshi"] or {}).get("hourly") or {}).get("implied_median"),
              ((c["kalshi"] or {}).get("daily") or {}).get("implied_median"),
              "| poly thresholds", len(c["polymarket"]["price_markets"]), "| movers", len(c["polymarket"]["movers"]))
    print("errors:", o["errors"])
