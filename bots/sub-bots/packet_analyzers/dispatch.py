"""
packet_analyzers/dispatch.py

Routes packets to the right analyzer based on vertical (here: perps).

Registry is a simple list; first analyzer whose handles(vertical)=True wins.
Order matters if prefixes overlap (put more-specific first).

Verticals NOT in any analyzer get status=skipped with skip_reason=no_analyzer:
the agent judges those from the skill playbook without a computed fair_prob.
"""
from __future__ import annotations
from typing import List

from .base import PacketAnalyzer, AnalysisResult
from .perps_edge import PerpsEdgeAnalyzer


# Instantiate once (analyzers are stateless)
ANALYZER_REGISTRY: List[PacketAnalyzer] = [
    PerpsEdgeAnalyzer(),
]


def find_analyzer(vertical: str) -> PacketAnalyzer:
    """Return the first analyzer that handles this vertical, or None."""
    for a in ANALYZER_REGISTRY:
        if a.handles(vertical):
            return a
    return None


async def analyze_packet(packet: dict, context: dict) -> dict:
    """Entry point.

    Returns a dict ready to merge into packet.pre_analysis.fair_analysis.
    Always returns a dict (never raises). On any internal failure, returns
    a skip result with reason.

    context can contain: feed_context, balance, open_positions, today_bets, etc.
    Analyzers only use what they need.
    """
    vertical = packet.get("vertical", "unknown")
    analyzer = find_analyzer(vertical)

    if analyzer is None:
        return AnalysisResult(
            status="skipped",
            skip_reason="no_analyzer_for_vertical",
            fair_prob_yes=None,
            model="agent_judgment_required",
            note=f"vertical '{vertical}' has no analyzer - The agent uses skill playbook",
        ).to_dict()

    try:
        result = await analyzer.analyze(packet, context)
        return result.to_dict()
    except Exception as e:
        return AnalysisResult(
            status="error",
            skip_reason="analyzer_raised",
            model=analyzer.name,
            note=f"{analyzer.name} raised: {str(e)[:200]}",
            warnings=["analyzer_exception"],
        ).to_dict()