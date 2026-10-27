"""packet_analyzers - pure analysis helpers for Perps packets.

One analyzer per vertical (here: perps_edge). Callers (the agent's tooling or a
packet build) pass a packet + context to dispatch.analyze_packet(); nothing here
reads or writes files, and nothing here trades.
"""
from .base import PacketAnalyzer, AnalysisResult
from .dispatch import analyze_packet, ANALYZER_REGISTRY

__all__ = ["PacketAnalyzer", "AnalysisResult", "analyze_packet", "ANALYZER_REGISTRY"]
