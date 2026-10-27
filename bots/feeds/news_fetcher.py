"""news_fetcher.py - market news from Google News RSS (free, no key).

Tags each item with group / signal / affects_asset. Keyword rules always; the optional
SI brain (LLM_MAIN / LLM_FALLBACK) refines tags only when enabled and reachable.
Writes bot_logs/news_catalysts.json in the shared schema:
  {last_updated, _updated, source, active_catalysts:[{group, signal, affects_asset, emoji,
   title, description, url, published, keywords, timestamp, query, catalyst_summary}],
   history, signal_summary}
Settings: NEWS_QUERIES (';'-separated), NEWS_MAX_AGE_H (default 48), NEWS_MAX_ITEMS (default 40),
NEWS_RSS_URL (default https://news.google.com/rss/search).
CLI: python bots/feeds/news_fetcher.py
"""
import json, os, re, sys, xml.etree.ElementTree as ET_XML
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from feeds._common import env, bot_logs, now_iso, http_get, write_json, read_json  # noqa: E402
from feeds import _brain  # noqa: E402

DEFAULT_QUERIES = ("bitcoin price", "ethereum price", "solana price", "crypto market",
                   "federal reserve rates", "CPI inflation", "crypto regulation SEC", "bitcoin ETF flows")
ASSETS = {"BTC": ("bitcoin", "btc"), "ETH": ("ethereum", "ether", " eth "), "SOL": ("solana", " sol ")}
BULL = ("surge", "rally", "soar", "jump", "record high", "all-time high", "approve", "approval", "inflow",
        "rate cut", "cuts rates", "bullish", "breakout", "adopt", "gains", "climbs", "rebound")
BEAR = ("crash", "plunge", "tumble", "slump", "sell-off", "selloff", "hack", "exploit", "lawsuit", "ban",
        "outflow", "rate hike", "hikes rates", "bearish", "liquidat", "falls", "drops", "war", "sanction",
        "conflict", "default", "fraud", "probe", "delist")
MACRO = ("fed", "federal reserve", "inflation", "cpi", "jobs report", "treasury", "yields", "tariff", "recession")
REG = ("sec", "regulat", "lawsuit", "court", "congress", "bill", "etf")
GEO = ("war", "iran", "russia", "sanction", "conflict", "missile", "china", "taiwan")
# Inflation headlines read inverted: "inflation jumps" is hot (risk-off), "inflation cools" is risk-on.
INFLATION = ("inflation", "cpi", "consumer prices", "consumer price index", "pce", "price index")
INFL_UP = ("jump", "jumps", "jumped", "surge", "soar", "climb", "rises", "rise", "rose", "accelerat", "hotter",
           "hot", "spike", "higher than expected", "more than expected", "gains", "heats up")
INFL_DOWN = ("cools", "cooling", "eases", "easing", "slows", "slowing", "falls", "drops", "declines", "softer",
             "lower than expected", "less than expected", "decelerat")


def _rss(query):
    url = env("NEWS_RSS_URL", "https://news.google.com/rss/search")
    xml = http_get(url, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}, as_json=False)
    out = []
    for it in ET_XML.fromstring(xml).iter("item"):
        g = lambda t: (it.findtext(t) or "").strip()
        out.append({"title": g("title"), "url": g("link"), "published": g("pubDate"),
                    "description": re.sub(r"<[^>]+>", " ", g("description"))[:300].strip(),
                    "source_name": (it.find("source").text if it.find("source") is not None else ""), "query": query})
    return out


def _has(t, w):
    w = w.strip()
    return re.search(r"(?<![a-z])" + re.escape(w) + (r"(?![a-z])" if len(w) <= 4 else ""), t) is not None


def _tag(item):
    t = " " + (item["title"] + " " + item.get("description", "")).lower() + " "
    asset = next((a for a, w in ASSETS.items() if any(_has(t, x) for x in w)), "")
    bull = sum(_has(t, w) for w in BULL)
    bear = sum(_has(t, w) for w in BEAR)
    if any(_has(t, w) for w in INFLATION):
        up = sum(_has(t, w) for w in INFL_UP)
        down = sum(_has(t, w) for w in INFL_DOWN)
        if up or down:
            bull, bear = down, up  # hot print = bearish for risk, cooling print = bullish
    signal = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    if any(_has(t, w) for w in GEO):
        group = "geopolitical"
    elif any(_has(t, w) for w in MACRO):
        group = "macro"
    elif any(_has(t, w) for w in REG):
        group = "regulation"
    else:
        group = f"crypto_{signal}" if signal != "neutral" else "crypto"
    kws = [w for w in BULL + BEAR + MACRO + GEO if _has(t, w)][:6]
    return {"group": group, "signal": signal, "affects_asset": asset,
            "emoji": {"bullish": "🟢", "bearish": "🔴"}.get(signal, "⚪"), "keywords": kws,
            "catalyst_summary": item["title"][:160], "tagged_by": "rules"}


def _brain_tag(items):
    if not items or not _brain.enabled():
        return
    lines = "\n".join(f"{i}. {it['title']}" for i, it in enumerate(items))
    prompt = ("Tag each crypto/macro headline. Reply ONLY JSON {\"tags\":[{\"i\":0,\"signal\":\"bullish|bearish|neutral\","
              "\"affects_asset\":\"BTC|ETH|SOL|\",\"group\":\"macro|regulation|geopolitical|crypto\"}]}.\n" + lines)
    raw = _brain.generate(prompt, max_tokens=1500)
    try:
        tags = json.loads(raw[raw.index("{"): raw.rindex("}") + 1]).get("tags") or []
    except Exception:
        return
    for t in tags:
        try:
            it = items[int(t["i"])]
        except Exception:
            continue
        if t.get("signal") in ("bullish", "bearish", "neutral"):
            it["signal"] = t["signal"]
        if t.get("affects_asset") in ("BTC", "ETH", "SOL", ""):
            it["affects_asset"] = t["affects_asset"]
        if t.get("group"):
            it["group"] = str(t["group"])[:30]
        it["tagged_by"] = "si_brain"


def fetch_news(write=True, queries=None):
    qs = queries or [q.strip() for q in env("NEWS_QUERIES", ";".join(DEFAULT_QUERIES)).split(";") if q.strip()]
    max_age_h = float(env("NEWS_MAX_AGE_H", "48"))
    max_items = int(env("NEWS_MAX_ITEMS", "40"))
    now = datetime.now(timezone.utc)
    seen, items, errors = set(), [], {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for q, res in zip(qs, ex.map(lambda q: _safe(_rss, q), qs)):
            rows, err = res
            if err:
                errors[q] = err
            for r in rows:
                key = r["url"] or r["title"]
                if key in seen:
                    continue
                try:
                    pt = parsedate_to_datetime(r["published"])
                    if (now - pt).total_seconds() > max_age_h * 3600:
                        continue
                    r["timestamp"] = pt.isoformat()
                except Exception:
                    r["timestamp"] = None
                seen.add(key)
                items.append(r)
    items.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    items = items[:max_items]
    for it in items:
        it.update(_tag(it))
    _brain_tag(items)
    ts = now_iso()
    prev = read_json(bot_logs("news_catalysts.json"), {}) or {}
    hist = ((prev.get("history") or []) + [{"ts": ts, "count": len(items)}])[-50:]
    summ = {s: sum(1 for i in items if i["signal"] == s) for s in ("bullish", "bearish", "neutral")}
    data = {"last_updated": ts, "_updated": ts, "source": "direct:google_news_rss", "active_catalysts": items,
            "history": hist, "signal_summary": summ, "errors": errors,
            "tagging": "si_brain" if any(i.get("tagged_by") == "si_brain" for i in items) else "rules"}
    if write and items:
        write_json(bot_logs("news_catalysts.json"), data)
    return data


def _safe(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:
        return [], str(e)


if __name__ == "__main__":
    d = fetch_news()
    print(len(d["active_catalysts"]), "items |", d["signal_summary"], "| tagging", d["tagging"], "| errors", d["errors"])
    for i in d["active_catalysts"][:5]:
        print(" ", i["signal"], i["affects_asset"] or "-", i["group"], "|", i["title"][:90])
