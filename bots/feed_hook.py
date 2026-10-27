"""feed_hook.py - Flow reader (theflowcycle.com public /data/*.json).

perps.py reads every outside-feed section through _feed(name) -> read_feed(name).
read_feed NEVER touches the network: it serves the last pull from bot_logs/flow_cache.json.
A pull happens only when asked: pull_flow() (agent request, query action, or the
FEED_CADENCE=<minutes> loop). FEED_CADENCE=manual (default) = on demand only.

Settings (bots/.env): FLOW_BASE_URL (default https://theflowcycle.com/data),
FLOW_STALE_MIN (default 120: data older than this is flagged stale, not dropped).

Only the locked file list below is pulled. Internal-identity fields are renamed
(editorial / curated / editorial_vs_mechanical) and doctrine/note prose is dropped.
"""
import os, re, sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feeds._common import env, bot_logs, now_iso, age_min, http_get, write_json, read_json  # noqa: E402

TIER1 = ("coin_calls", "call_cycle", "daily_read", "current_state", "macro", "blowoff_tracker",
         "rotation_state", "ma_200w", "pi_cycle", "price_projection", "invariants", "pattern_matches_live")
TIER2 = ("track_record", "brain_forward_record", "price_accuracy", "brain_consensus", "pattern_similarity",
         "forecast_curve", "horse_race_scoreboard", "euphoria_state", "hypothesis_log")
# The public Flow feed names two editorial files after its author (<prefix>_thesis,
# mechanical_vs_<prefix>). The prefix is upstream config, not code: FLOW_EDITOR_PREFIX in
# bots/.env (see .env.example / Credits). Everything is renamed to neutral local names on
# the way in. Blank = those two files are skipped and no name rewriting runs; every other
# file still pulls.
_UPSTREAM_EDITOR = re.sub(r"[^a-z0-9]", "", (env("FLOW_EDITOR_PREFIX", "") or "").strip().lower()) or None
# remote file -> neutral local name
IDENTITY = {"editorial_calls": "editorial_calls", "j6_price_projection": "curated_projection"}
if _UPSTREAM_EDITOR:
    IDENTITY.update({f"{_UPSTREAM_EDITOR}_thesis": "editorial_thesis",
                     f"mechanical_vs_{_UPSTREAM_EDITOR}": "editorial_vs_mechanical"})
SKIPPED = ("chart_history", "chart_markers", "indicator_history", "called_dates", "coin_call_history",
           "daily_summary", "chart_annotations", "forecast_v2", "data_gaps", "weight_fit_suggestion", "j6_curve")

# read_feed(name) -> cached file (names perps.py asks for)
READ_MAP = {"macro": "macro", "daily_read": "daily_read", "editorial_calls": "editorial_calls",
            "price_projection": "curated_projection", "coin_calls": "coin_calls",
            "rotation_state": "rotation_state", "hypothesis_log": "hypothesis_log",
            "pattern_matches": "pattern_matches_live", "scoreboard": "editorial_vs_mechanical",
            "catalyst_bias": None, "morning_thesis": None}

CACHE = "flow_cache.json"
_DROP_KEYS = {"_doctrine", "doctrine", "_note", "raw_response"}
_STR_SUBS = ((re.compile(r"\b[Jj]6\b"), "curated"),
             (re.compile(r"\bEmpire's\b|\bempire's\b"), "the"), (re.compile(r"\b[Ee]mpire\b"), "system"))
if _UPSTREAM_EDITOR:
    _ED_T, _ED_U = _UPSTREAM_EDITOR.title(), _UPSTREAM_EDITOR.upper()
    _STR_SUBS = ((re.compile(rf"\b{_ED_T}'s\b"), "the editor's"), (re.compile(rf"\b{_ED_U}'S\b"), "THE EDITOR'S"),
                 (re.compile(rf"\b{_ED_T}\b"), "the editor"), (re.compile(rf"\b{_ED_U}\b"), "EDITOR"),
                 (re.compile(rf"\b{_UPSTREAM_EDITOR}\b"), "editorial")) + _STR_SUBS


def base_url():
    return env("FLOW_BASE_URL", "https://theflowcycle.com/data").rstrip("/")


def _nkey(k):
    k2 = str(k)
    if _UPSTREAM_EDITOR:
        k2 = k2.replace(f"mechanical_vs_{_UPSTREAM_EDITOR}", "editorial_vs_mechanical")
        k2 = re.sub(re.escape(_UPSTREAM_EDITOR), "editorial", k2, flags=re.I)
    return re.sub(r"(?<![a-z])j6(?![0-9])", "curated", k2, flags=re.I)


def _nstr(s):
    for rx, rep in _STR_SUBS:
        s = rx.sub(rep, s)
    return s


def neutralize(o):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            ks = str(k)
            if ks in _DROP_KEYS or (ks.endswith("_note") and isinstance(v, str)):
                continue
            out[_nkey(ks)] = neutralize(v)
        return out
    if isinstance(o, list):
        return [neutralize(v) for v in o]
    if isinstance(o, str):
        return _nstr(o)
    return o


def _stamp(d):
    if isinstance(d, dict):
        for k in ("_updated", "updated_at", "timestamp", "computed_at"):
            if d.get(k):
                return d[k]
    return None


def pull_flow(timeout=20.0):
    """Fetch the locked list now. Returns the pull dict and writes the cache. Never raises."""
    remote = list(TIER1) + list(TIER2) + list(IDENTITY)
    files, errors, ages = {}, {}, {}
    url = base_url()

    def one(name):
        try:
            return name, http_get(f"{url}/{name}.json", timeout=timeout), None
        except Exception as e:
            return name, None, str(e)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for name, data, err in ex.map(one, remote):
            local = IDENTITY.get(name, name)
            if err:
                errors[local] = err
                continue
            files[local] = neutralize(data)
            ages[local] = age_min(_stamp(files[local]))
    stamps = [s for s in (_stamp(v) for v in files.values()) if s]
    flow_updated = max(stamps, key=lambda s: -(age_min(s) or 1e9)) if stamps else None
    pull = {"source": "flow", "base_url": url, "fetched_at": now_iso(), "flow_updated": flow_updated,
            "ok": bool(files) and len(errors) < len(remote) / 2, "files": files, "errors": errors,
            "file_age_min": ages}
    _annotate(pull)
    if files:
        write_json(bot_logs(CACHE), pull)
    return pull


def _annotate(pull):
    pull["age_min"] = age_min(pull.get("flow_updated"))
    lim = float(env("FLOW_STALE_MIN", "120") or 120)
    pull["stale"] = (not pull.get("ok")) or pull["age_min"] is None or pull["age_min"] > lim
    return pull


def cached():
    """Last pull from disk (no network), freshness recomputed now. None if never pulled."""
    p = read_json(bot_logs(CACHE))
    return _annotate(p) if isinstance(p, dict) and p.get("files") else None


def read_feed(name):
    local = READ_MAP.get(name, name)
    if not local:
        return None
    p = cached()
    return (p or {}).get("files", {}).get(local)


# ── packet section ─────────────────────────────────────────────────────────
def _pick(d, *keys):
    d = d or {}
    return {k: d.get(k) for k in keys if k in d}


def flow_section(pull, chain):
    """The `flow` block of the agent packet for one chain (BTC/ETH/SOL)."""
    if not pull or not (pull.get("files") or {}):
        # no Flow data at all (never pulled, or the pull failed with no cache): say so
        return {"source": "flow", "available": False, "stale": True, "fetched_at": (pull or {}).get("fetched_at"),
                "flow_updated": None, "age_min": None, "errors": (pull or {}).get("errors") or {}}
    C = (chain or "").upper().replace("-PERP", "")
    f = pull.get("files") or {}
    g = lambda n: f.get(n) or {}
    cc, cyc, dr, cs = g("coin_calls"), g("call_cycle"), g("daily_read"), g("current_state")
    call = dict((cc.get("calls") or {}).get(C) or {})
    call = _pick(call, "direction", "entry", "tp", "sl", "horizon", "horizon_hours", "hours_left", "locked",
                 "levels_stale", "source", "note", "mechanical_confluence")
    asi = dr.get("active_signal_inputs") or {}
    tr = dr.get("trade_recommendation") or {}
    lc = cyc.get("live_call") or {}
    ed_calls = (g("editorial_calls").get("calls") or {})
    ed_age = g("editorial_calls").get("content_age_hours") or {}
    evm = g("editorial_vs_mechanical")
    th = g("editorial_thesis")
    cp = g("curated_projection")
    pa = g("price_accuracy")
    return {
        "source": "flow", "available": True, "base_url": pull.get("base_url"),
        "fetched_at": pull.get("fetched_at"), "flow_updated": pull.get("flow_updated"),
        "age_min": pull.get("age_min"), "stale": pull.get("stale"), "errors": pull.get("errors") or {},
        "call": call,
        "live_call": {**_pick(lc, "direction", "confidence", "invalidate_if", "invalidate_checkable",
                              "window_hours", "next_boundary_iso", "live_bullet_drift_vs_call_pct", "stamped_at"),
                      "minutes_left": (cyc.get("countdown") or {}).get("minutes_left"),
                      "state": (cyc.get("call_state") or {}).get("state")},
        "cycle": {**_pick(cs, "score", "trend", "phase", "phase_source", "phase_suggestion", "next_move"),
                  **_pick(dr, "cliff_proximity", "regime", "regime_break_signal", "phase_leg"),
                  "score_7d_target": (dr.get("forecast") or {}).get("score_7d_target")},
        "signals": {**_pick(asi, "net_signal", "weighted_bullish_score", "weighted_bearish_score",
                            "by_direction", "confluence_clusters"),
                    "top": (asi.get("top_envelopes_by_weight") or [])[:5],
                    "market_drivers": dr.get("market_drivers"),
                    "historical_analogues": dr.get("historical_analogues")},
        "risk": {"blowoff": {**_pick(g("blowoff_tracker").get("composite"), "composite", "alert", "strict_fire",
                                     "signatures_above_70"),
                             "delta_7": (g("blowoff_tracker").get("trend") or {}).get("delta_7")},
                 "invariants": {**(g("invariants").get("currently_firing") or {}),
                                "interpretation": g("invariants").get("interpretation")},
                 "patterns": _pick(g("pattern_matches_live"), "firing", "watching", "nearest_miss", "why_quiet"),
                 "perps_read": tr.get("perps"), "abort_conditions": tr.get("abort_conditions"),
                 "abort_policy": tr.get("abort_policy"), "exit_priority_signals": tr.get("exit_priority_signals"),
                 "micro_lean": (tr.get("micro_lean_by_coin") or {}).get(C),
                 "do_not_size_when": ((cc.get("perps_contract") or {}).get("do_not_size_when") or {}).get(C)},
        "context": {
            "macro": _pick(g("macro"), "btc_price", "eth_price", "sol_price", "btc_chg", "realized_vol_7d",
                           "realized_vol_30d", "realized_vol_ratio", "vix", "dxy", "us10y", "fed_rate", "sp500_chg",
                           "funding_reset", "funding_rate", "fear_greed", "fear_greed_label", "btc_momentum",
                           "blowoff_score", "blowoff_signal", "flow_index", "flow_index_signal",
                           # released macro (FRED via Flow); BLS components ride in macro_prints
                           "core_cpi", "cpi_chg_3m", "cpi_accelerating", "fed_funds_rate", "unemployment",
                           "oil", "oil_chg"),
            "rotation": _pick(g("rotation_state"), "stage", "read", "sol_btc_divergence_pct",
                              "eth_btc_divergence_pct", "btc_extended", "sequence_hint"),
            "ma_200w": (g("ma_200w").get("coins") or {}).get(C),
            "pi_cycle": _pick(g("pi_cycle"), "ratio", "signal", "pi_cycle_factor"),
            "projection_7d": {**_pick((g("price_projection").get("assets") or {}).get(C), "current", "daily_pct",
                                      "daily_price", "band_low_pct", "band_high_pct", "weekly_pct"),
                              "cycle_position": g("price_projection").get("cycle_position")},
            "curated_projection": {**_pick((cp.get("assets") or {}).get(C), "current", "daily_pct", "daily_price",
                                           "band_low_pct", "band_high_pct"),
                                   "directional_tilt": cp.get("curated_directional_tilt"),
                                   "terminal_bias_pct": cp.get("terminal_bias_pct")},
            "forecast_weeks": {"projection": (g("forecast_curve").get("projection") or [])[:5],
                               "blend_mode": g("forecast_curve").get("blend_mode")},
            "euphoria": g("euphoria_state").get("state"),
            "hypotheses_firing": g("hypothesis_log").get("firing_now"),
        },
        "editorial": {
            "thesis": th.get("thesis_text"), "leverage_bias": th.get("leverage_bias"),
            "thesis_updated": th.get("updated_at"), "age_min": age_min(th.get("updated_at")),
            "calls": {k: ed_calls.get(k) for k in ("leverage_bias", f"{C.lower()}_call", "the_read", "falsification",
                                                   "next_phase", "next_phase_conviction", "daily_call") if k in ed_calls},
            "calls_age_h": {k: ed_age.get(k) for k in ("leverage_bias", f"{C.lower()}_call", "the_read", "daily_call")
                            if k in ed_age},
            "hit_rate_vs_mechanical": {"chain": (evm.get("summary") or {}).get(C),
                                       "windows": evm.get("summary_windows")},
        },
        "calibration": {
            **_pick(g("track_record"), "hit_rate_pct", "expectancy_net_pct", "current_streak"),
            "by_direction": (g("track_record").get("splits") or {}).get("by_direction"),
            **_pick(g("brain_forward_record"), "direction_hit_rate_pct", "direction_hit_rate_7d_pct"),
            "projection_error": (pa.get("coins") or {}).get(C),
            "projection_decision_grade": (pa.get("consumer_contract") or {}).get("decision_grade"),
            "projection_amber": (pa.get("consumer_contract") or {}).get("amber_now"),
            "model_consensus": _pick(g("brain_consensus").get("consensus"), "final_signal", "alert_tier",
                                     "consensus_confidence", "key_reasoning", "disagreements_noted"),
            "pattern_match": _pick(g("pattern_similarity"), "best_match", "pattern_type", "best_similarity",
                                   "margin_vs_second_pts", "cluster_label", "factor_coverage_pct"),
            "scoreboard": _pick(g("horse_race_scoreboard"), "state", "leverage_recommendation", "lev_context"),
        },
        "file_age_min": pull.get("file_age_min"),
    }
