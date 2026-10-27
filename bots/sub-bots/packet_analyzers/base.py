"""
packet_analyzers/base.py

Common interface for packet analyzers. Each vertical (here: perps) implements
this interface; dispatch.analyze_packet() routes to the right one.

Design principle: each analyzer owns ONE vertical. No more monolithic
_pre_analyze_packet with if/elif chains. No more dual-path sim calls.
One vertical -> one analyzer -> one fair_probability estimate -> done.

Analyzers are pure: they take a packet + context, they return an analysis
dict. They do NOT mutate the packet. They do NOT write files. The caller
handles orchestration.
"""
from __future__ import annotations
from typing import Optional, List


class AnalysisResult:
    """Standard output shape from every analyzer. Drop-in compatible with
    the pre_analysis dict structure so downstream code (edge calc, Kelly
    sizing, correlation checks) reads one shape.

    Fields:
      fair_prob_yes:      analyzer's estimate of P(YES resolves) in [0, 1]
      confidence:         "high" | "medium" | "low" | None
      model:              name of the method used (e.g. "vegas_consensus_9books")
      data_sources:       list of signals consumed (e.g. ["vegas", "h2h", "injuries"])
      note:               human-readable summary (shown to the agent)
      warnings:           list of concerns (e.g. "stale vegas data", "thin book")
      metadata:           analyzer-specific extras (vegas details, weather readings, etc.)
      status:             "analyzed" | "skipped" | "error"
      skip_reason:        if status=skipped, why (e.g. "no_vegas_coverage", "unsupported")
    """
    __slots__ = (
        "fair_prob_yes", "confidence", "model", "data_sources",
        "note", "warnings", "metadata", "status", "skip_reason",
    )

    def __init__(
        self,
        fair_prob_yes: Optional[float] = None,
        confidence: Optional[str] = None,
        model: str = "unknown",
        data_sources: Optional[List[str]] = None,
        note: str = "",
        warnings: Optional[List[str]] = None,
        metadata: Optional[dict] = None,
        status: str = "analyzed",
        skip_reason: Optional[str] = None,
    ):
        self.fair_prob_yes = fair_prob_yes
        self.confidence = confidence
        self.model = model
        self.data_sources = data_sources or []
        self.note = note
        self.warnings = warnings or []
        self.metadata = metadata or {}
        self.status = status
        self.skip_reason = skip_reason

    def to_dict(self) -> dict:
        """Emit as dict for packet.pre_analysis.fair_analysis."""
        out = {
            "fair_prob_yes":  self.fair_prob_yes,
            "confidence":     self.confidence,
            "model":          self.model,
            "data_sources":   self.data_sources,
            "note":           self.note,
            "warnings":       self.warnings,
            "status":         self.status,
        }
        if self.skip_reason:
            out["skip_reason"] = self.skip_reason
        # Merge metadata at top level (analyzer-specific fields like vegas_consensus,
        # h2h_record, nws_comparison, etc.)
        for k, v in (self.metadata or {}).items():
            if k not in out:
                out[k] = v
        return out


class PacketAnalyzer:
    """Base class. Subclasses implement `analyze(packet, context)` returning
    AnalysisResult. Sync is fine for stateless computation; analyzers that
    need to fetch external data (vegas, injuries) should be `async`."""
    name: str = "base"
    vertical_prefixes: tuple = ()  # e.g. ("sports_",) - which verticals this handles

    def handles(self, vertical: str) -> bool:
        return any(vertical.startswith(p) for p in self.vertical_prefixes)

    async def analyze(self, packet: dict, context: dict) -> AnalysisResult:
        raise NotImplementedError
