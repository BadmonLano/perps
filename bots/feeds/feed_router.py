"""feed_router.py - Flow-first feed router with failover, for any bot.

Lanes:
  flow  Flow public data (bots/feed_hook.py). Fallback: last good Flow cache, flagged stale.
  news  Flow news.json if Flow publishes one, else direct Google News RSS (news_fetcher).
  odds  ALWAYS live at decision time: Kalshi + Polymarket direct (odds_fetcher). Never cached.
        If both venues fail, Flow odds.json (if Flow publishes one) is used, flagged not live.

Failover / flap rule (per lane): a source is DOWN after FEED_FAIL_LIMIT (2) failures in a
row. While on the fallback, the primary is retried after FEED_FAILBACK_COOLDOWN_MIN (10)
minutes; one good primary read switches back. Every lane result carries source, age_min,
stale and fetched_at. State: bot_logs/feed_router_state.json.

Cadence (no hardcoded intervals): FEED_CADENCE=manual (default) = pull only when asked
(json_relay/feed_request.json, a query action, or this CLI). FEED_CADENCE=<minutes> lets
the host bot's loop pull flow + news on that cadence. Odds are never on a cadence.

CLI: python bots/feeds/feed_router.py pull [flow news odds]
     python bots/feeds/feed_router.py packet SOL
     python bots/feeds/feed_router.py status
"""
import json, os, re, sys, time
from datetime import datetime

BOTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOTS)
from feeds._common import env, root, bot_logs, now_iso, age_min, http_get, write_json, read_json  # noqa: E402
from feeds import odds_fetcher, news_fetcher  # noqa: E402
try:
    from feeds import macro_fetcher  # noqa: E402
except Exception:  # optional lane; packets build without it
    macro_fetcher = None
import feed_hook  # noqa: E402

STATE = "feed_router_state.json"
REQUEST_FILE_NAME = "feed_request.json"
LANES = ("flow", "news", "odds")


def cadence():
    """None = manual (on demand). Otherwise minutes (float > 0)."""
    v = str(env("FEED_CADENCE", "manual")).strip().lower()
    if v in ("", "manual", "off", "on_demand", "on-demand"):
        return None
    try:
        m = float(v)
        return m if m > 0 else None
    except ValueError:
        return None


def request_path():
    return os.path.join(root(), "json_relay", REQUEST_FILE_NAME)


def _limits():
    return int(env("FEED_FAIL_LIMIT", "2")), float(env("FEED_FAILBACK_COOLDOWN_MIN", "10"))


def _state():
    return read_json(bot_logs(STATE), {}) or {}


def _save(st):
    write_json(bot_logs(STATE), st)


def _lane(st, lane):
    return st.setdefault(lane, {"active": "primary", "fails": 0, "switched_at": None})


def _use_primary(ls):
    if ls["active"] == "primary":
        return True
    _, cool = _limits()
    return (time.time() - (ls.get("switched_ts") or 0)) / 60.0 >= cool


def _record(ls, primary_ok, tried_primary):
    limit, _ = _limits()
    if not tried_primary:
        return
    if primary_ok:
        if ls["active"] != "primary":
            ls.update(active="primary", switched_at=now_iso(), switched_ts=time.time())
        ls["fails"] = 0
    else:
        ls["fails"] = ls.get("fails", 0) + 1
        if ls["fails"] >= limit and ls["active"] == "primary":
            ls.update(active="fallback", switched_at=now_iso(), switched_ts=time.time())
        elif ls["active"] == "fallback":
            ls["switched_ts"] = time.time()   # failed retry: restart the cooldown


def _flow_extra(name):
    try:
        return http_get(f"{feed_hook.base_url()}/{name}.json", timeout=10)
    except Exception:
        return None


# ── lanes ───────────────────────────────────────────────────────────────────
def pull_flow(st):
    ls = _lane(st, "flow")
    p = feed_hook.pull_flow()
    _record(ls, bool(p.get("ok")), True)
    if p.get("ok"):
        out = p
        out["lane_source"] = "flow"
    else:
        out = feed_hook.cached() or {"files": {}, "stale": True, "age_min": None, "fetched_at": None,
                                     "flow_updated": None, "ok": False}
        out["stale"] = True
        out["lane_source"] = "flow_cache"
        out["errors"] = p.get("errors")
    ls.update(last_source=out["lane_source"], last_fetched_at=now_iso(), last_age_min=out.get("age_min"),
              last_stale=out.get("stale"), last_error=None if p.get("ok") else "flow unreachable")
    return out


def pull_news(st):
    ls = _lane(st, "news")
    tried = _use_primary(ls)
    data = _flow_extra("news") if tried else None
    ok = isinstance(data, dict) and bool(data.get("active_catalysts"))
    _record(ls, ok, tried)
    if ok:
        data["source"] = "flow"
        write_json(bot_logs("news_catalysts.json"), data)
    else:
        try:
            data = news_fetcher.fetch_news(write=True)
            data["source"] = "direct:google_news_rss"
            err = None
        except Exception as e:
            data, err = {}, str(e)
        if not (data.get("active_catalysts") or []):
            # zero items (every query failed or returned nothing): never label that fresh.
            # Serve the last good cache with its real age, or report the lane unavailable.
            if not err:
                errs = data.get("errors") or {}
                err = ("all news queries failed: " + "; ".join(f"{k}: {v}" for k, v in list(errs.items())[:3])
                       if errs else "news fetch returned no items")
            cache = read_json(bot_logs("news_catalysts.json"), {}) or {}
            if cache.get("active_catalysts"):
                data = cache
                data["source"] = "cache"
            else:
                data = {"source": "none", "active_catalysts": [], "last_updated": None, "_updated": None}
            data["error"] = err[:300]
    items = data.get("active_catalysts") or []
    a = age_min(data.get("last_updated") or data.get("_updated")) if items else None
    lim = float(env("NEWS_STALE_MIN", "180"))
    data["age_min"], data["stale"] = a, (a is None or a > lim)
    data["available"] = bool(items)
    ls.update(last_source=data["source"], last_fetched_at=now_iso(), last_age_min=a, last_stale=data["stale"])
    return data


def pull_odds(st, spot=None):
    """Live, every call. Never served from cache."""
    ls = _lane(st, "odds")
    o = odds_fetcher.fetch_odds(spot=spot, write=True)
    o["lane_source"] = "direct"
    if not o.get("ok"):
        fo = _flow_extra("odds")
        if isinstance(fo, dict) and fo:
            o = {"fetched_at": now_iso(), "lane_source": "flow_odds_not_live", "ok": True, "flow": fo,
                 "kalshi": fo.get("kalshi"), "polymarket": fo.get("polymarket"), "errors": o.get("errors")}
    ls.update(last_source=o["lane_source"], last_fetched_at=o.get("fetched_at"), last_age_min=0.0,
              last_stale=not o.get("ok"), active="primary" if o["lane_source"] == "direct" else "fallback")
    return o


def pull(lanes=LANES, spot=None):
    st = _state()
    out = {}
    if "flow" in lanes:
        out["flow"] = pull_flow(st)
    if "news" in lanes:
        out["news"] = pull_news(st)
    if "odds" in lanes:
        spot = dict(spot or {})
        if not all(spot.get(a) for a in ("BTC", "ETH", "SOL")):
            # fallback only for assets the caller could not price live (Flow macro can lag)
            m = ((out.get("flow") or feed_hook.cached() or {}).get("files") or {}).get("macro") or {}
            for a, k in (("BTC", "btc_price"), ("ETH", "eth_price"), ("SOL", "sol_price")):
                if not spot.get(a) and m.get(k):
                    spot[a] = m.get(k)
        out["odds"] = pull_odds(st, spot)
    _save(st)
    return out


def status():
    """Per-lane source/age/stale from disk, no network (for UIs)."""
    st = _state()
    fc = feed_hook.cached()
    nc = read_json(bot_logs("news_catalysts.json"), {}) or {}
    kc = read_json(bot_logs("kalshi_implied_probs.json"), {}) or {}
    rows = {
        "flow": {"source": (st.get("flow") or {}).get("last_source") or ("flow_cache" if fc else "none"),
                 "age_min": (fc or {}).get("age_min"), "stale": (fc or {}).get("stale", True),
                 "fetched_at": (fc or {}).get("fetched_at"), "active": (st.get("flow") or {}).get("active", "primary")},
        "news": {"source": (st.get("news") or {}).get("last_source") or nc.get("source") or "none",
                 "available": bool(nc.get("active_catalysts")),
                 "age_min": age_min(nc.get("last_updated") or nc.get("_updated")) if nc.get("active_catalysts") else None,
                 "stale": None, "fetched_at": nc.get("last_updated"), "active": (st.get("news") or {}).get("active", "primary")},
        "odds": {"source": (st.get("odds") or {}).get("last_source") or "live on decision",
                 "age_min": age_min(kc.get("last_updated") or kc.get("_updated")), "stale": None,
                 "fetched_at": kc.get("last_updated"), "active": "live (never cached)"},
    }
    a = rows["news"]["age_min"]
    rows["news"]["stale"] = a is None or a > float(env("NEWS_STALE_MIN", "180"))
    rows["cadence"] = cadence() or "manual"
    return rows


# ── packet ──────────────────────────────────────────────────────────────────
# Macro headlines (CPI, inflation, Fed, jobs, yields) rank first, then newest, so
# the per-chain cut never hides a print behind older stories. Cap unchanged.
_MACRO_NEWS_RE = re.compile(
    r"\b(cpi|inflation|inflationary|disinflation|deflation|fed|fomc|federal reserve|powell|"
    r"rate cuts?|rate hikes?|interest rates?|pce|ppi|payrolls?|nfp|jobs report|unemployment|"
    r"jobless claims|treasury yields?|bond yields?|recession|gdp)\b", re.I)


def _is_macro_news(i):
    grp = str(i.get("group") or "").lower()
    if grp.startswith("macro") or "fed" in grp.split("_"):
        return True
    return bool(_MACRO_NEWS_RE.search(f"{i.get('title') or i.get('headline') or ''} {i.get('description') or ''}"))


def _news_ts(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def news_for_chain(news, chain, max_items=8):
    C = (chain or "").upper().replace("-PERP", "")
    items = [i for i in (news or {}).get("active_catalysts") or [] if (i.get("affects_asset") or "") in (C, "")]
    items = sorted(items, key=lambda i: (0 if _is_macro_news(i) else 1, -_news_ts(i.get("timestamp"))))
    keep = ("title", "signal", "group", "affects_asset", "url", "timestamp", "tagged_by")
    has = bool((news or {}).get("active_catalysts"))
    return {"source": (news or {}).get("source") or "none", "available": has,
            "age_min": (news or {}).get("age_min") if has else None,
            "stale": (news or {}).get("stale") if has else True,
            "error": (news or {}).get("error"),
            "items": [dict({k: i.get(k) for k in keep}, macro=_is_macro_news(i)) for i in items[:max_items]],
            "counts": dict({s: sum(1 for i in items if i.get("signal") == s) for s in ("bullish", "bearish", "neutral")},
                           macro=sum(1 for i in items if _is_macro_news(i)))}


def flags(flow, odds, news):
    f = []
    if not flow.get("available"):
        f.append("flow unavailable")
    elif flow.get("stale"):
        f.append(f"flow stale (age {flow.get('age_min')} min)")
    if flow.get("lane_source") == "flow_cache" and flow.get("available"):
        f.append("flow down: serving last good cache")
    cal, risk, ctx = flow.get("calibration") or {}, flow.get("risk") or {}, flow.get("context") or {}
    if cal.get("projection_decision_grade") is False:
        f.append("projection not decision-grade")
    if (cal.get("pattern_match") or {}).get("cluster_label") == "ambiguous":
        f.append("pattern match ambiguous")
    if (risk.get("blowoff") or {}).get("strict_fire") or (risk.get("blowoff") or {}).get("alert") not in (None, "quiet"):
        f.append(f"blowoff alert: {(risk.get('blowoff') or {}).get('alert')}")
    if risk.get("abort_conditions"):
        f.append("flow abort conditions present")
    if risk.get("do_not_size_when"):
        f.append("flow do-not-size rules apply")
    if (flow.get("call") or {}).get("levels_stale"):
        f.append("flow call levels stale")
    ea = (flow.get("editorial") or {}).get("age_min")
    if ea is not None and ea > 24 * 60:
        f.append("editorial thesis older than 24h")
    if not (odds or {}).get("ok"):
        f.append("live odds unavailable")
    elif (odds or {}).get("lane_source") != "direct":
        f.append("odds from Flow, not live")
    kk = (odds or {}).get("kalshi") or {}
    k = kk.get("daily") or kk.get("hourly") or {}
    d = str((flow.get("call") or {}).get("direction") or "").upper()
    if k.get("p_up") is not None and d in ("LONG", "SHORT"):
        if (d == "LONG" and k["p_up"] < 0.45) or (d == "SHORT" and k["p_up"] > 0.55):
            f.append(f"flow call {d} vs Kalshi end-of-day p_up {k['p_up']}")
    if not (news or {}).get("available", True):
        f.append("news unavailable")
    elif str((news or {}).get("source") or "").startswith("direct"):
        f.append("news from direct RSS (Flow publishes no news)")
    if (news or {}).get("stale") and (news or {}).get("available", True):
        f.append("news stale")
    return f


def build_feed_block(chain, pulled, flow_pull=None):
    """{flow, news_feed, odds_live, flags, feed_sources} for one chain from a pull() result."""
    fp = pulled.get("flow") or flow_pull or feed_hook.cached()
    flow = feed_hook.flow_section(fp, chain)
    if fp:
        flow["lane_source"] = fp.get("lane_source") or ("flow" if fp.get("ok") else "flow_cache")
    odds = pulled.get("odds")
    oc = odds_fetcher.odds_for_chain(odds, chain) if odds else None
    if oc is not None:
        oc["lane_source"] = (odds or {}).get("lane_source")
    news = pulled.get("news") or read_json(bot_logs("news_catalysts.json"), {})
    if news and "age_min" not in news and news.get("active_catalysts"):
        news["age_min"] = age_min(news.get("last_updated") or news.get("_updated"))
        news["stale"] = news["age_min"] is None or news["age_min"] > float(env("NEWS_STALE_MIN", "180"))
    nf = news_for_chain(news, chain)
    return {"flow": flow, "news_feed": nf, "odds_live": oc, "flags": flags(flow, oc, nf),
            "feed_sources": {"flow": {"source": flow.get("lane_source"), "age_min": flow.get("age_min"),
                                      "stale": flow.get("stale"), "fetched_at": flow.get("fetched_at")},
                             "news": {"source": nf.get("source"), "age_min": nf.get("age_min"), "stale": nf.get("stale")},
                             "odds": {"source": (oc or {}).get("lane_source"), "fetched_at": (oc or {}).get("fetched_at"),
                                      "live": (oc or {}).get("lane_source") == "direct"},
                             "cadence": cadence() or "manual"}}


def build_agent_packet(chain, bot=None, lanes=LANES, spot=None):
    """Full standalone packet: {chain, built_at, bot, flow, news_feed, odds_live, flags, feed_sources}."""
    pulled = pull(lanes, spot)
    blk = {"chain": chain, "built_at": now_iso(), "bot": bot or {}, **build_feed_block(chain, pulled)}
    if macro_fetcher is not None:
        try:
            blk["macro_prints"] = macro_fetcher.get_macro_prints()
        except Exception as e:
            blk["macro_prints"] = {"available": False, "reason": str(e)[:160]}
    return blk


if __name__ == "__main__":
    a = sys.argv[1:] or ["status"]
    if a[0] == "pull":
        r = pull(tuple(a[1:]) or LANES)
        print(json.dumps({k: {"source": v.get("lane_source") or v.get("source"), "age_min": v.get("age_min"),
                              "stale": v.get("stale"), "ok": v.get("ok")} for k, v in r.items()}, indent=1))
    elif a[0] == "packet":
        print(json.dumps(build_agent_packet(a[1] if len(a) > 1 else "SOL"), indent=1, default=str)[:6000])
    else:
        print(json.dumps(status(), indent=1))
