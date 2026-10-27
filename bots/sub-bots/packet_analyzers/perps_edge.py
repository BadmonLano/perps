"""
packet_analyzers/perps_edge.py

Perps analyzer. Pure: takes a packet + context (the caller supplies a per-market
edge block, balance, existing position, news_catalysts from bots/feeds) and
returns a recommendation. Reads no files.

What this analyzer does NOT do:
  - Fetch venue APIs directly (the caller supplies the context)
  - Estimate probability from scratch (feed's synthesis IS the estimate)
  - Execute trades (perps_bot does that)

What it DOES do:
  - Determine mode: directional | funding_arb | squeeze_setup | stand_down
  - Compute recommended_leverage via Kelly + vol regime, capped by the venue max
  - Emit full liquidation math in packet (transparency, no silent capping)
  - Apply correlation-aware sizing (reduce if existing position is correlated)
  - Apply calibration feedback loop (bin-level adjustment per market)
  - Respect the owner overrides and thesis

Output fields (AnalysisResult):
  fair_prob_yes:       conviction / 10  (so calibration bin-adjustment works)
  confidence:          "high" | "medium" | "low"
  model:               "perps_edge_v1"
  data_sources:        [sources used: feed_edge, funding, oi, leaderboard, ...]
  note:                human-readable summary
  warnings:            list of concerns
  status:              "analyzed" | "skipped" | "error"
  metadata:
    market:                    "SOL-PERP"
    mode:                      "directional" | "funding_arb" | "squeeze_setup" | "stand_down"
    side:                      "LONG" | "SHORT" | "NEUTRAL"
    conviction:                0-10
    recommended_leverage:      computed float
    max_leverage_available:    the effective cap (operator ceiling, <= venue max)
    size_rationale:            string explaining leverage choice
    liquidation_math: {
      balance: $X, safe_collateral: $Y (30%),
      position_size_usd: $Z, liquidation_distance_pct: W%,
      daily_vol: V%, liquidation_in_daily_vols: N (std devs before liq),
      variance_warning: str or None,
    }
    strategic_alignment:       "aligned" | "contradicted" | "neutral" | "no_daily_bias"
    correlation_risk_score:    0.0 - 1.0 (0=no risk, 1=full stacking)
    correlation_context:       list of correlated open positions
    feed_snapshot:             subset of perp_market_edges (not full, just summary)
    position_snapshot:         { has_position, pnl, size_usd, direction }
    overrides_applied:         list of the owner overrides that influenced the call
    tp_sl_suggestion: { tp_pct, sl_pct }
"""
from __future__ import annotations
import os
import json
import math
from typing import Optional
from datetime import datetime, timezone

from .base import PacketAnalyzer, AnalysisResult


# ============================================================================
# ============================================================================
# CONFIG - Jupiter Perps venue specifics
# ============================================================================

# Jupiter's REAL max leverage per market (250x across majors as of 2026).
# IMPORTANT — two-tier model:
#   * VENUE_MAX_LEVERAGE = what Jupiter physically allows (a fact about the venue).
#     Shown in every packet as `venue_max_leverage` so it's always visible what the
#     real headroom is.
#   * The actual RECOMMENDATION is capped by the operator-owned MAX_LEVERAGE bound
#     (passed in via context; the meeting safeguard keeps that <=20). The user owns
#     the recommendation ceiling; the venue owns the physical max. Never conflate them.
VENUE_MAX_LEVERAGE = {
    "SOL-PERP": 250,
    "BTC-PERP": 250,
    "ETH-PERP": 250,
    "JUP-PERP": 100,
    # Default for unknown markets
    "_default": 100,
}

# Maintenance margin per market (% where liquidation triggers).
# Source: Jupiter Perps docs (liquidation at ~0.2% of position for the pool model;
# these per-market buffers are the analyzer's conservative estimates for liq math).
MAINTENANCE_MARGIN = {
    "SOL-PERP": 0.04,
    "BTC-PERP": 0.03,
    "ETH-PERP": 0.03,
    "JUP-PERP": 0.06,
    "_default": 0.05,
}

# Hard floor: safe collateral = balance * SAFE_COLLATERAL_PCT
# The owner's rule: 30% max of account to any single perp position.
SAFE_COLLATERAL_PCT = 0.30

# Minimum balance to even consider perps
MIN_BALANCE_USD = 15.0


# ============================================================================
# LEVERAGE MATH (the transparency layer)
# ============================================================================

def compute_leverage_recommendation(
    conviction: int,
    vol_regime: str,
    mode: str,
    market: str,
    balance: float,
    operator_max_leverage: float = 20.0,
) -> dict:
    """Compute recommended leverage via:
      - Base from conviction (1-10)
      - Multiplier from vol regime (crushed = more, extreme = less)
      - Adjustment from mode (funding_arb is lower-risk, directional at normal)
      - Capped by the OPERATOR-owned ceiling (never above venue physical max)

    Two-tier leverage model (Sprint 61c):
      * venue_max_leverage    = what Jupiter physically allows (250x on majors) — a
        FACT, surfaced so the real headroom is always visible.
      * operator_max_leverage = the owned recommendation ceiling (default 70, kept
        <=20 by the meeting safeguard). The recommendation never exceeds this.
    The user owns the recommendation ceiling; the venue owns the physical max.

    Returns full reasoning dict for packet transparency.
    """
    # Base leverage from conviction
    # conviction 5 -> 2x, 6 -> 2.5x, 7 -> 3x, 8 -> 4x, 9 -> 5x, 10 -> 6x
    if conviction < 5:
        base = 0
    elif conviction == 5:
        base = 2.0
    elif conviction == 6:
        base = 2.5
    elif conviction == 7:
        base = 3.0
    elif conviction == 8:
        base = 4.0
    elif conviction == 9:
        base = 5.0
    else:  # 10
        base = 6.0

    # Vol regime multiplier
    vol_mult = {
        "crushed":  1.5,   # safer to lever up - low variance
        "normal":   1.0,   # baseline
        "elevated": 0.65,  # smaller size, noise is bigger
        "extreme":  0.40,  # tiny size if trading at all
        "unknown":  0.75,  # conservative if we don't know
    }.get(vol_regime, 1.0)

    # Mode adjustment
    mode_mult = {
        "directional":      1.0,
        "funding_arb":      1.2,    # mechanical mean-reversion edge, can size up
        "squeeze_setup":    1.1,    # high conviction when triggered
        "stand_down":       0.0,    # no trade
    }.get(mode, 1.0)

    raw_leverage = base * vol_mult * mode_mult

    # Round to 0.5 increments
    rec = round(raw_leverage * 2) / 2

    # Two-tier cap: venue physical max (fact) vs operator-owned ceiling (the real bound).
    venue_max = VENUE_MAX_LEVERAGE.get(market, VENUE_MAX_LEVERAGE["_default"])
    op_max    = float(operator_max_leverage or 20.0)
    effective_cap = min(op_max, venue_max)   # recommendation never exceeds the owned ceiling

    # Floor at 0 if conviction too low
    rec = max(0, min(rec, effective_cap))

    rationale_parts = []
    if conviction < 5:
        rationale_parts.append(f"conviction {conviction}/10 below min threshold 5 - no trade")
    else:
        rationale_parts.append(f"base {base}x from conviction {conviction}/10")
        rationale_parts.append(f"vol regime '{vol_regime}' x{vol_mult}")
        if mode != "directional":
            rationale_parts.append(f"mode '{mode}' x{mode_mult}")
        # show the owned cap that actually bounds the rec, and the venue max for context
        rationale_parts.append(f"= recommended {rec}x (your cap {op_max:g}x; Jupiter allows {venue_max}x)")

    return {
        "recommended_leverage":  rec,
        "max_leverage_available": effective_cap,   # the bound that actually applies
        "operator_max_leverage":  op_max,          # owned ceiling (meeting-controlled, <=20)
        "venue_max_leverage":     venue_max,        # Jupiter physical max (informational)
        "size_rationale":         "; ".join(rationale_parts),
        "base_from_conviction":   base,
        "vol_multiplier":         vol_mult,
        "mode_multiplier":        mode_mult,
    }


def compute_liquidation_math(
    market: str,
    balance: float,
    leverage: float,
    vol_24h_realized: Optional[float],
) -> dict:
    """Produce the full liquidation math block for packet transparency.
    This is what makes agent aware of the variance he's taking on.
    """
    maintenance = MAINTENANCE_MARGIN.get(market, MAINTENANCE_MARGIN["_default"])
    safe_collateral = round(balance * SAFE_COLLATERAL_PCT, 2)

    if leverage <= 0:
        return {
            "balance":                       round(balance, 2),
            "safe_collateral_30pct":         safe_collateral,
            "leverage_used":                 0,
            "position_size_usd":             0,
            "liquidation_distance_pct":      None,
            "daily_vol":                     vol_24h_realized,
            "liquidation_in_daily_vols":     None,
            "variance_warning":              "no trade (leverage = 0)",
        }

    position_size = round(safe_collateral * leverage, 2)
    # Liquidation distance: roughly 1/leverage - maintenance
    # Formula: price_move_to_liq = (1 - maintenance) / leverage
    liq_distance = round((1.0 - maintenance) / leverage * 100, 2)

    # If we have vol data, compute how many standard deviations before liq
    liq_in_vols = None
    variance_warning = None
    if vol_24h_realized and vol_24h_realized > 0:
        # ── v1.1 FIELD FIX (2026-07-19 SOL packet: "LIQUIDATION IN LESS THAN 1
        # DAILY STDEV (0.0)" on a 2x position — impossible; 2x liq sits ~47% away).
        # Root cause: a triple units mismatch. Feed's CONTRACT write site feeds
        # vol_24h_realized from _daily_realized_vol = ANNUALIZED PERCENT
        # (stdev × √365 × 100, e.g. 32.59). This code assumed HOURLY DECIMAL and
        # did × √24 × 100 → a "daily stdev" of ~16,000% → liq_in_vols 0.0029 →
        # rounded to 0.0 → the scariest warning in the vocabulary fired on the
        # safest position on the book. Cry-wolf class: teach the agent to ignore
        # this warning and a real one on a 20x scalp gets ignored too.
        # Feed's venue write site feeds the SAME field as hourly decimal, so the
        # consumer sniffs units:
        #   > 1.0   → annualized percent  → daily% = v / √365
        #   ≤ 1.0   → hourly decimal      → daily% = v × √24 × 100  (original contract)
        # Post-conversion sanity: a computed daily stdev > 25% on majors is a
        # data/unit problem, not a risk reading — say so instead of crying wolf.
        if vol_24h_realized > 1.0:
            daily_stdev_pct = vol_24h_realized / math.sqrt(365)
        else:
            daily_stdev_pct = vol_24h_realized * math.sqrt(24) * 100
        if daily_stdev_pct > 25.0:
            daily_stdev_pct = 0.0
            variance_warning = (
                f"vol input implausible ({vol_24h_realized}) — computed daily stdev "
                f"exceeds 25%; treating as unit/data error, liq-vs-stdev UNAVAILABLE "
                f"(not a risk reading)")
        if daily_stdev_pct > 0:
            liq_in_vols = round(liq_distance / daily_stdev_pct, 2)

            if liq_in_vols < 1.0:
                variance_warning = (
                    f"LIQUIDATION IN LESS THAN 1 DAILY STDEV ({liq_in_vols}). "
                    f"A single normal-range daily move can wipe this position."
                )
            elif liq_in_vols < 2.0:
                variance_warning = (
                    f"Liquidation within 2 daily stdevs ({liq_in_vols}). "
                    f"~5% chance of being liquidated by noise alone over 24h."
                )
            elif liq_in_vols < 4.0:
                variance_warning = (
                    f"Liquidation within {liq_in_vols} daily stdevs. "
                    f"Safe against normal moves; large adverse news could hit."
                )
            # >= 4 stdev: no warning, safe zone
    else:
        variance_warning = "no vol data - cannot compute liquidation variance"

    return {
        "balance":                   round(balance, 2),
        "safe_collateral_30pct":     safe_collateral,
        "leverage_used":             leverage,
        "position_size_usd":         position_size,
        "liquidation_distance_pct":  liq_distance,
        "daily_vol_pct":             (round(vol_24h_realized / math.sqrt(365), 2) if vol_24h_realized > 1.0
                                       else round(vol_24h_realized * math.sqrt(24) * 100, 2)) if vol_24h_realized else None,
        "liquidation_in_daily_vols": liq_in_vols,
        "variance_warning":          variance_warning,
        "maintenance_margin_pct":    round(maintenance * 100, 2),
    }


# ============================================================================
# MODE DETECTION
# ============================================================================

def detect_mode(feed_edge: dict) -> str:
    """Classify this opportunity into one of:
      - stand_down:       no trade, market is chop / unclear
      - funding_arb:      extreme funding, mean-reversion play
      - squeeze_setup:    cascade building, front-run or momentum play
      - directional:      regular trend-follow / fade
    """
    funding_arb_score = feed_edge.get("funding_arb_score", 0)
    squeeze_score = feed_edge.get("squeeze_score", 0)
    directional = feed_edge.get("directional_edge", {}) or {}
    directional_conviction = directional.get("conviction", 0)

    # Stand-down conditions
    if directional.get("side", "NEUTRAL") == "NEUTRAL" and funding_arb_score < 5 and squeeze_score < 6:
        return "stand_down"

    # Funding arb is highest priority if triggered (most mechanical edge)
    if funding_arb_score >= 7:
        return "funding_arb"

    # Squeeze setup next
    if squeeze_score >= 7:
        return "squeeze_setup"

    # Strong directional
    if directional_conviction >= 5:
        return "directional"

    # Weak funding_arb as fallback
    if funding_arb_score >= 5:
        return "funding_arb"

    return "stand_down"


def derive_side_conviction(mode: str, feed_edge: dict) -> tuple:
    """From mode + feed_edge, derive the (side, conviction) for this trade.
    Returns ('LONG'|'SHORT'|'NEUTRAL', int 0-10).
    """
    if mode == "stand_down":
        return ("NEUTRAL", 0)

    directional = feed_edge.get("directional_edge", {}) or {}

    if mode == "directional":
        return (directional.get("side", "NEUTRAL"),
                directional.get("conviction", 0))

    if mode == "funding_arb":
        # Trade AGAINST the funding direction (mean reversion)
        funding = feed_edge.get("funding_rate_now")
        arb_score = feed_edge.get("funding_arb_score", 0)
        if funding is None:
            return ("NEUTRAL", 0)
        # Positive funding = crowd is long, paying shorts => SHORT is the arb
        if funding > 0:
            return ("SHORT", arb_score)
        else:
            return ("LONG", arb_score)

    if mode == "squeeze_setup":
        # Squeeze: crowd is extreme one side, funding agrees. Fade the crowd.
        leaderboard = feed_edge.get("leaderboard") or {}
        bias = leaderboard.get("bias_pct")
        squeeze_score = feed_edge.get("squeeze_score", 0)
        if bias is None:
            return ("NEUTRAL", 0)
        # Crowd is long (bias > 0) => short the squeeze
        if bias > 0:
            return ("SHORT", squeeze_score)
        else:
            return ("LONG", squeeze_score)

    return ("NEUTRAL", 0)


def suggest_tp_sl(mode: str, side: str, conviction: int, vol_regime: str) -> dict:
    """Suggest TP/SL percentages based on mode + vol regime.
    Returns {tp_pct, sl_pct} as positive floats (sign applied by perps_bot).
    """
    if side == "NEUTRAL" or side == "CLOSE" or conviction == 0:
        return {"tp_pct": None, "sl_pct": None}

    # Base by mode
    if mode == "funding_arb":
        # Mean reversion: smaller targets, tight stops
        base_tp, base_sl = 1.5, 1.0
    elif mode == "squeeze_setup":
        # Cascade: larger targets, wider stops (ride the move)
        base_tp, base_sl = 5.0, 2.5
    elif mode == "override_directional":
        # The owner thesis trade: balanced with slightly tighter stop
        # (we have less confidence than model-detected edge)
        base_tp, base_sl = 3.0, 2.0
    else:  # directional
        # Balance
        base_tp, base_sl = 3.5, 2.0

    # Scale by vol regime
    vol_scalar = {
        "crushed":  0.6,
        "normal":   1.0,
        "elevated": 1.4,
        "extreme":  1.8,
        "unknown":  1.0,   # when history empty, use normal scalar
    }.get(vol_regime, 1.0)

    # Scale by conviction (higher conviction = wider TP, still tight SL)
    conv_tp_scalar = 1 + (conviction - 5) * 0.15 if conviction >= 5 else 1.0

    return {
        "tp_pct": round(base_tp * vol_scalar * conv_tp_scalar, 2),
        "sl_pct": round(base_sl * vol_scalar, 2),
    }


# ============================================================================
# STRATEGIC ALIGNMENT (daily vs hourly)
# ============================================================================

def check_strategic_alignment(side: str, daily_bias: dict) -> str:
    """Compare hourly side to daily bias file. Returns alignment classification."""
    if not daily_bias or not isinstance(daily_bias, dict):
        return "no_daily_bias"
    daily_side = daily_bias.get("side", "NEUTRAL")
    daily_conv = daily_bias.get("conviction", 0)

    if daily_conv < 4:
        return "no_daily_bias"
    if daily_side == side:
        return "aligned"
    if daily_side == "NEUTRAL":
        return "neutral"
    return "contradicted"


# ============================================================================
# CORRELATION CHECK
# ============================================================================

def compute_correlation_risk(
    new_market: str,
    new_side: str,
    existing_positions: list,
    feed_edge: dict,
) -> tuple:
    """Check if new trade stacks correlated exposure with existing positions.
    Returns (risk_score 0-1, context list of strings)."""
    if not existing_positions:
        return (0.0, [])

    correlations = feed_edge.get("correlations", {}) or {}
    max_corr_risk = 0.0
    context = []

    for pos in existing_positions:
        if not isinstance(pos, dict):
            continue
        pos_market = pos.get("market") or pos.get("symbol") or ""
        pos_side = pos.get("side") or pos.get("direction") or ""
        if not pos_market or not pos_side:
            continue

        # Same market - this is scaling, not correlation
        if pos_market == new_market:
            if pos_side == new_side:
                context.append(f"{pos_market} ALREADY {pos_side} - this would scale existing")
                max_corr_risk = max(max_corr_risk, 0.95)
            else:
                context.append(f"{pos_market} is {pos_side}, new trade {new_side} - would reduce position not stack")
            continue

        # Different market, check correlation
        corr = correlations.get(pos_market)
        if corr is None:
            continue

        # Stacking: same side + high positive correlation = dangerous
        if pos_side == new_side and corr > 0.6:
            risk = abs(corr)
            max_corr_risk = max(max_corr_risk, risk)
            context.append(
                f"{pos_market} {pos_side} correlation {corr:+.2f} - adds correlated {new_side} exposure"
            )
        # Hedging: opposite side + high positive correlation = natural hedge (good)
        elif pos_side != new_side and corr > 0.6:
            context.append(
                f"{pos_market} {pos_side} correlation {corr:+.2f} - new {new_side} is natural hedge"
            )

    return (round(max_corr_risk, 3), context)


# ============================================================================
# NEWS CATALYST CHECK
# ============================================================================

def detect_news_warnings(news_catalysts: dict, market: str, base: str) -> list:
    """Look for high-impact catalysts affecting this market's base asset."""
    warnings = []
    if not news_catalysts or not isinstance(news_catalysts, dict):
        return warnings
    catalysts = news_catalysts.get("catalysts") or news_catalysts.get("items") or []
    for cat in catalysts[:10]:  # check recent top 10
        if not isinstance(cat, dict):
            continue
        impact = (cat.get("impact") or cat.get("severity") or "").lower()
        mentions = (cat.get("affected") or cat.get("tickers") or "")
        headline = cat.get("headline") or cat.get("title") or ""
        if isinstance(mentions, str):
            mentions_str = mentions.upper()
        elif isinstance(mentions, list):
            mentions_str = " ".join(str(m).upper() for m in mentions)
        else:
            mentions_str = ""
        if base.upper() not in mentions_str and base.upper() not in headline.upper():
            continue
        if impact in ("high", "critical"):
            warnings.append(f"HIGH-IMPACT catalyst: {headline[:100]}")
        elif impact == "medium":
            warnings.append(f"medium catalyst: {headline[:100]}")
    return warnings


# ============================================================================
# OVERRIDE HANDLING
# ============================================================================

def apply_overrides(
    feed_edge: dict,
    recommendation: dict,
    owner_overrides: dict,
    agent_thesis: dict,
) -> tuple:
    """Apply any the owner overrides or the agent thesis contradictions.
    Returns (modified_recommendation, list_of_overrides_applied).

    Sprint 27 autonomy doctrine: The owner's overrides can force a trade even when
    the analyzer's native recommendation is stand_down. This is intentional —
    The owner has authority over the agent, and when he fires a directive, it means
    "I have information/conviction the model doesn't have." The agent should
    trade it with the override's specs.

    Evaluation trace is returned so the agent (and audit) can see exactly
    which fields were considered, matched, and applied or skipped.
    """
    applied = []
    evaluation_trace = {
        "market_key_in_file":   False,
        "fields_evaluated":     [],
        "fields_applied":       [],
        "fields_skipped":       [],
    }
    rec = dict(recommendation)  # copy

    market = rec.get("market", "")

    # The owner overrides by market
    market_ov = (owner_overrides or {}).get(market) or {}
    evaluation_trace["market_key_in_file"] = bool(market_ov)

    if market_ov.get("disable", False):
        evaluation_trace["fields_evaluated"].append("disable")
        rec["mode"] = "stand_down"
        rec["side"] = "NEUTRAL"
        rec["conviction"] = 0
        rec["recommended_leverage"] = 0
        applied.append(f"The owner disabled {market}")
        evaluation_trace["fields_applied"].append("disable")
        # Early return — disable supersedes everything
        rec["override_evaluation_trace"] = evaluation_trace
        return (rec, applied)

    forced_side = market_ov.get("force_side")
    if forced_side:
        evaluation_trace["fields_evaluated"].append("force_side")
        prev_side = rec.get("side", "NEUTRAL")
        prev_mode = rec.get("mode", "stand_down")
        rec["side"] = forced_side.upper()
        applied.append(f"The owner forced side={forced_side}")
        evaluation_trace["fields_applied"].append("force_side")

        # Sprint 27 doctrine: if override specifies a side AND the native
        # mode is stand_down, promote to "override_directional" so the agent
        # knows this is an owner-directed trade, not a model-spotted edge
        if prev_mode == "stand_down":
            rec["mode"] = "override_directional"
            rec["_mode_promoted_by_override"] = True
            applied.append(f"mode promoted stand_down→override_directional (The owner override provides the edge)")
            # Grant conviction = 7 by default for the owner overrides (his word stands)
            # unless a specific conviction was provided
            if rec.get("conviction", 0) < 5:
                rec["conviction"] = 7
                applied.append("conviction set to 7 (The owner directive default)")

    forced_lev = market_ov.get("force_leverage")
    if forced_lev is not None:
        evaluation_trace["fields_evaluated"].append("force_leverage")
        try:
            rec["recommended_leverage"] = float(forced_lev)
            applied.append(f"The owner forced leverage={forced_lev}x")
            evaluation_trace["fields_applied"].append("force_leverage")
        except Exception:
            evaluation_trace["fields_skipped"].append(f"force_leverage (parse_failed: {forced_lev})")

    # Close directive (from owner_signal "close" directive)
    close_now = market_ov.get("close_now", False)
    if close_now:
        evaluation_trace["fields_evaluated"].append("close_now")
        rec["mode"] = "override_close"
        rec["side"] = "CLOSE"
        rec["recommended_leverage"] = 0
        applied.append(f"operator directed CLOSE of {market}")
        evaluation_trace["fields_applied"].append("close_now")

    # Remove SL directive (lock position open against auto-close)
    if market_ov.get("remove_sl", False):
        evaluation_trace["fields_evaluated"].append("remove_sl")
        rec["_remove_sl"] = True
        applied.append(f"The owner locked position (no SL)")
        evaluation_trace["fields_applied"].append("remove_sl")

    # The agent thesis contradiction check
    thesis = agent_thesis or {}
    thesis_side = thesis.get("side") or thesis.get("direction")
    thesis_strength = thesis.get("conviction", 0)
    if thesis_side and thesis_strength >= 7:
        if thesis_side.upper() != rec.get("side") and rec.get("side") not in ("NEUTRAL", "CLOSE"):
            applied.append(
                f"The agent thesis {thesis_side}@{thesis_strength} contradicts recommendation — reducing size 50%"
            )
            rec["recommended_leverage"] = rec.get("recommended_leverage", 0) * 0.5
            evaluation_trace["fields_applied"].append("agent_thesis_contradiction_reduce")

    rec["override_evaluation_trace"] = evaluation_trace
    return (rec, applied)


# ============================================================================
# THE ANALYZER
# ============================================================================

class PerpsEdgeAnalyzer(PacketAnalyzer):
    """Per-market perps analyzer. Reads feed's perp_market_edges for a
    given market and builds the full decision packet.

    Vertical format expected: "perps_<market_base_lower>" e.g.
      "perps_sol" -> SOL-PERP
      "perps_btc" -> BTC-PERP
      "perps_eth" -> ETH-PERP
      "perps_jup" -> JUP-PERP
    """
    name = "perps_edge_v1"
    vertical_prefixes = ("perps_",)

    def handles(self, vertical: str) -> bool:
        return vertical.startswith("perps_")

    async def analyze(self, packet: dict, context: dict) -> AnalysisResult:
        vertical = packet.get("vertical", "")
        market = packet.get("market") or self._vertical_to_market(vertical)

        if not market:
            return AnalysisResult(
                status="error",
                skip_reason="could_not_resolve_market",
                model=self.name,
                note=f"cannot map vertical '{vertical}' to a perp market",
            )

        feed_context = context.get("feed_context", {}) or {}
        macro = feed_context.get("current_state") or feed_context.get("macro") or {}
        perp_edges = macro.get("perp_market_edges", {}) or {}
        feed_edge = perp_edges.get(market) or {}

        # Venue-independent: the edge block (regime, funding, confluence, momentum) is
        # analyzable even when venue data is missing; only venue-specific parts degrade.
        _venue_degraded = False
        _venue_warnings = []

        _feed_edge_status = (feed_edge or {}).get("status")
        if _feed_edge_status in ("venue_data_unavailable",):
            _venue_degraded = True
            _venue_warnings.append("venue_data_unavailable")

        # ONLY skip if feed gave us genuinely NOTHING to work with — no edge block at
        # all, or the legacy dead stub. A venue-degraded edge still carries the real
        # regime/confluence read and IS analyzable.
        _edge_is_dead = (
            not feed_edge
            or _feed_edge_status == "synthesis_failed"        # feed synth threw
        )
        # a venue-degraded edge is only usable if it carries the analyzer's real contract
        _has_native_edge = bool((feed_edge or {}).get("directional_edge"))
        if _edge_is_dead and not _has_native_edge:
            return AnalysisResult(
                status="skipped",
                skip_reason="no_feed_edge_data",
                fair_prob_yes=None,
                confidence="low",
                model=self.name,
                note=f"feed has no usable perp_market_edges for {market} "
                     f"(status={_feed_edge_status or 'absent'}). The agent can fire feed query "
                     f"'force_perp_sweep' to force rebuild.",
                warnings=["feed_edge_missing"],
                metadata={"market": market, "feed_edge_status": _feed_edge_status},
            )

        # Balance (required)
        balance = float(context.get("balance_usd") or context.get("wallet_balance_usd") or 0)
        if balance < MIN_BALANCE_USD:
            return AnalysisResult(
                status="skipped",
                skip_reason=f"balance_below_min_${balance:.2f}_<_${MIN_BALANCE_USD}",
                fair_prob_yes=None,
                model=self.name,
                note=f"Balance ${balance:.2f} below ${MIN_BALANCE_USD} minimum",
                warnings=["insufficient_balance"],
                metadata={"market": market, "balance": balance},
            )

        # --- Mode detection ---
        mode = detect_mode(feed_edge)
        side, conviction = derive_side_conviction(mode, feed_edge)

        # --- Leverage recommendation ---
        vol_regime = feed_edge.get("vol_regime", "unknown")
        # Sprint 61c: pass the operator-owned MAX_LEVERAGE ceiling (from overrides if
        # tuned via meeting, else the 70 default). This bounds the recommendation; the
        # venue physical max (250x) is surfaced separately as context, not as the cap.
        _op_overrides = context.get("owner_overrides", {}) or context.get("operator_overrides", {}) or {}
        _op_max_lev = float(_op_overrides.get("MAX_LEVERAGE") or 20.0)
        lev_info = compute_leverage_recommendation(
            conviction=conviction,
            vol_regime=vol_regime,
            mode=mode,
            market=market,
            balance=balance,
            operator_max_leverage=_op_max_lev,
        )

        # --- Apply overrides ---
        recommendation = {
            "market":               market,
            "mode":                 mode,
            "side":                 side,
            "conviction":           conviction,
            "recommended_leverage": lev_info["recommended_leverage"],
        }
        owner_overrides = context.get("owner_overrides", {}) or {}
        agent_thesis = context.get("agent_thesis", {}) or {}
        recommendation, overrides_applied = apply_overrides(
            feed_edge, recommendation, owner_overrides, agent_thesis
        )
        # If overrides changed leverage, recompute math
        final_leverage = recommendation["recommended_leverage"]
        final_side = recommendation["side"]
        final_mode = recommendation["mode"]
        final_conviction = recommendation["conviction"]

        # Rebuild size_rationale if override modified leverage
        original_leverage = lev_info["recommended_leverage"]
        if final_leverage != original_leverage:
            if final_mode == "override_directional":
                final_size_rationale = (
                    f"The owner override: leverage {final_leverage}x (bypassing model's {original_leverage}x on stand_down). "
                    f"Model had no edge; the owner is providing the thesis."
                )
            elif final_mode == "override_close":
                final_size_rationale = "operator directed CLOSE — no new position"
            else:
                final_size_rationale = (
                    f"The owner override: leverage adjusted {original_leverage}x → {final_leverage}x. "
                    f"Original reasoning: {lev_info['size_rationale']}"
                )
        else:
            final_size_rationale = lev_info["size_rationale"]

        # --- Liquidation math ---
        vol_24h = feed_edge.get("vol_24h_realized")
        liq_math = compute_liquidation_math(
            market=market,
            balance=balance,
            leverage=final_leverage,
            vol_24h_realized=vol_24h,
        )

        # --- Strategic alignment ---
        daily_bias = context.get("perp_daily_bias", {}) or {}
        alignment = check_strategic_alignment(final_side, daily_bias)

        # --- Correlation risk ---
        existing_positions = context.get("existing_perp_positions", []) or []
        corr_risk, corr_context = compute_correlation_risk(
            market, final_side, existing_positions, feed_edge
        )

        # --- News catalyst check ---
        news_catalysts = context.get("news_catalysts", {}) or {}
        base = feed_edge.get("base", market.split("-")[0])
        news_warnings = detect_news_warnings(news_catalysts, market, base)

        # --- TP/SL suggestion ---
        tp_sl = suggest_tp_sl(final_mode, final_side, final_conviction, vol_regime)

        # --- Build confidence ---
        # Sprint 27 doctrine: override modes get "medium" confidence by default
        # (The owner's word stands but model didn't validate — user is taking
        # responsibility for the trade)
        if final_mode == "stand_down":
            confidence = "low"
        elif final_mode == "override_close":
            confidence = "high"  # close directive is always certain
        elif final_mode == "override_directional":
            confidence = "medium"  # The owner says trade, model didn't flag edge
        elif final_conviction >= 8:
            confidence = "high"
        elif final_conviction >= 6:
            confidence = "medium"
        else:
            confidence = "low"

        # --- Contradiction warnings ---
        warnings = list(news_warnings)
        if alignment == "contradicted":
            warnings.append(
                f"hourly {final_side} CONTRADICTS daily bias {daily_bias.get('side')} - smaller size"
            )
        if corr_risk > 0.7:
            warnings.append(
                f"high correlation risk {corr_risk:.2f} with existing positions - consider reducing"
            )
        if liq_math.get("variance_warning"):
            warnings.append(liq_math["variance_warning"])

        # --- Data sources ---
        sources = ["feed_perp_market_edges"]
        if feed_edge.get("funding_rate_now") is not None:
            sources.append("feed_funding")
        if feed_edge.get("open_interest_usd") is not None:
            sources.append("feed_oi")
        if feed_edge.get("leaderboard"):
            sources.append("feed_leaderboard")
        if feed_edge.get("kalshi_implied_prob") is not None:
            sources.append("kalshi_crypto_implied")
        if news_warnings:
            sources.append("news_catalysts")

        # --- Human-readable note ---
        rationale = feed_edge.get("directional_edge", {}).get("rationale", "")
        note = (
            f"{market} {final_mode.upper()} {final_side} "
            f"conviction {final_conviction}/10 @ {final_leverage}x leverage. "
            f"{rationale[:200]}"
        )

        # --- Calibration-style conviction-to-prob mapping ---
        # Map conviction 0-10 to fair_prob_yes 0.0-1.0 for calibration bin adjustment
        # Convention: conviction 5 -> 0.50, scale linearly either side
        fair_prob_yes = None
        if final_side == "LONG":
            fair_prob_yes = 0.50 + (final_conviction - 5) * 0.05  # 5->.50, 10->.75
        elif final_side == "SHORT":
            fair_prob_yes = 0.50 - (final_conviction - 5) * 0.05  # 5->.50, 10->.25

        # --- Build metadata ---
        metadata = {
            "market":                market,
            "mode":                  final_mode,
            "side":                  final_side,
            "conviction":            final_conviction,
            "recommended_leverage":  final_leverage,
            "max_leverage_available": lev_info["max_leverage_available"],
            "size_rationale":        final_size_rationale,
            "liquidation_math":      liq_math,
            "tp_sl_suggestion":      tp_sl,
            "strategic_alignment":   alignment,
            "correlation_risk_score": corr_risk,
            "correlation_context":   corr_context,
            "overrides_applied":     overrides_applied,
            "feed_snapshot": {
                "mark_price":             feed_edge.get("mark_price"),
                "funding_rate_now":       feed_edge.get("funding_rate_now"),           # hourly %
                "funding_rate_hourly_pct": feed_edge.get("funding_rate_hourly_pct"),    # explicit alias
                "funding_rate_daily_pct":  feed_edge.get("funding_rate_daily_pct"),     # daily %
                "funding_rate_apr_pct":    feed_edge.get("funding_rate_apr_pct"),       # annualized %
                "funding_extreme":        feed_edge.get("funding_extreme_flag"),
                "open_interest_usd":      feed_edge.get("open_interest_usd"),
                "oi_velocity":            feed_edge.get("oi_velocity_regime"),
                "leaderboard_bias":       (feed_edge.get("leaderboard") or {}).get("bias_pct"),
                "vol_regime":             vol_regime,
                "narrative_alignment":    feed_edge.get("narrative_alignment"),
                "kalshi_implied":         feed_edge.get("kalshi_implied_prob"),
                "funding_arb_score":      feed_edge.get("funding_arb_score"),
                "squeeze_score":          feed_edge.get("squeeze_score"),
            },
            "daily_bias_snapshot": {
                "side":       daily_bias.get("side"),
                "conviction": daily_bias.get("conviction"),
            } if daily_bias else None,
            "news_warnings_count":   len(news_warnings),
            "synthesized_at":        datetime.now(timezone.utc).isoformat(),
        }

        # --- Determine final status ---
        if final_mode == "stand_down":
            final_status = "skipped"
            final_skip_reason = "stand_down_no_edge"
        elif final_mode == "override_close":
            final_status = "analyzed"
            final_skip_reason = None
        elif final_mode == "override_directional":
            final_status = "analyzed"
            final_skip_reason = None
        else:
            final_status = "analyzed"
            final_skip_reason = None

        return AnalysisResult(
            fair_prob_yes=round(fair_prob_yes, 4) if fair_prob_yes is not None else None,
            confidence=confidence,
            model="perps_edge_v1",
            data_sources=sources,
            note=note,
            warnings=warnings,
            status=final_status,
            skip_reason=final_skip_reason,
            metadata=metadata,
        )

    def _vertical_to_market(self, vertical: str) -> Optional[str]:
        """perps_sol -> SOL-PERP, perps_btc -> BTC-PERP, etc."""
        if not vertical.startswith("perps_"):
            return None
        base = vertical[6:].upper()
        if not base:
            return None
        return f"{base}-PERP"