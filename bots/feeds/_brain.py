"""Optional SI brain for feeds (sync). Order from LLM_MAIN / LLM_FALLBACK (grok | ollama | off).
Both off, or nothing reachable -> returns "" and callers use their rule-based path."""
import json, urllib.request
from feeds._common import env


def order():
    ok = ("grok", "ollama", "off")
    m = (env("LLM_MAIN", "grok") or "grok").lower()
    f = (env("LLM_FALLBACK", "ollama") or "ollama").lower()
    out = []
    for p in ((m if m in ok else "grok"), (f if f in ok else "ollama")):
        if p != "off" and p not in out:
            out.append(p)
    return out


def enabled():
    return bool(order())


def generate(prompt, max_tokens=800, timeout=40.0):
    for p in order():
        try:
            if p == "grok":
                key = env("GROK_API_KEY") or env("XAI_API_KEY")
                if not key:
                    continue
                body = {"model": env("GROK_TEXT_MODEL", "grok-4.5") if env("GROK_TEXT_MODEL", "auto") != "auto" else "grok-4.5",
                        "max_tokens": max_tokens, "temperature": 0.2,
                        "messages": [{"role": "user", "content": prompt}]}
                req = urllib.request.Request("https://api.x.ai/v1/chat/completions", data=json.dumps(body).encode(),
                                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    t = json.loads(r.read())["choices"][0]["message"]["content"]
                if t:
                    return t
            elif p == "ollama":
                body = {"model": env("OLLAMA_MODEL", "qwen3:14b-q4_K_M"), "prompt": prompt, "stream": False,
                        "format": "json", "options": {"num_predict": max_tokens, "temperature": 0.2}}
                req = urllib.request.Request(env("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/generate",
                                             data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    t = json.loads(r.read()).get("response", "")
                if t:
                    return t
        except Exception:
            continue
    return ""
