"""_si_gate.py - which SI providers the sub-bots may call.

Order comes from LLM_MAIN / LLM_FALLBACK (grok | ollama | off), the same rule perps.py uses.
Defaults: grok main, ollama fallback. "off" skips that slot; both off = no SI calls at all,
and every caller falls back to its rule-based path (or skips the SI job).
Read at call time, so the values loaded from bots/.env at boot apply.
"""
import os

_OK = ("grok", "ollama", "off")


def order() -> list:
    m = (os.getenv("LLM_MAIN") or "grok").strip().lower()
    f = (os.getenv("LLM_FALLBACK") or "ollama").strip().lower()
    out = []
    for p in ((m if m in _OK else "grok"), (f if f in _OK else "ollama")):
        if p != "off" and p not in out:
            out.append(p)
    return out


def enabled() -> bool:
    return bool(order())


def allows(provider: str) -> bool:
    return provider in order()
