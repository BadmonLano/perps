"""Bot-agnostic market feeds: Flow reader (bots/feed_hook.py), live odds (Kalshi +
Polymarket), news (Google News RSS) and a Flow-first router. No hardcoded fetch
intervals: FEED_CADENCE=manual|<minutes>. Odds are fetched live on every call."""
