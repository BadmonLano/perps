"""_self_description.py — Sprint 41 self-awareness module

(Public build: history trimmed; see TIMELINE.md.)
"""
import ast
import hashlib
import json
import os

# ── Workspace root (public build) ─────────────────────────────────────────
# WORKSPACE_DIR in the environment wins; otherwise the repo root is the
# folder that contains bots/ (works from bots/ and bots/sub-bots/).
def _resolve_workspace_root():
    _env = (os.environ.get("WORKSPACE_DIR") or "").strip()
    if _env:
        return os.path.abspath(os.path.expanduser(_env))
    _p = os.path.dirname(os.path.abspath(__file__))
    _root = None
    for _ in range(6):
        if os.path.basename(_p).lower() == "bots":
            _root = os.path.dirname(_p); break
        if os.path.isdir(os.path.join(_p, "bots")) and os.path.isdir(os.path.join(_p, "skills")):
            _root = _p; break
        _p = os.path.dirname(_p)
    _root = _root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # .env may not be loaded yet at import time: honour WORKSPACE_DIR set there too.
    for _envf in (os.path.join(_root, "bots", ".env"), os.path.join(_root, ".env")):
        try:
            with open(_envf, encoding="utf-8-sig") as _fh:
                for _ln in _fh:
                    if _ln.strip().startswith("WORKSPACE_DIR="):
                        _v = _ln.split("=", 1)[1].strip().strip('"').strip("'")
                        if _v:
                            return os.path.abspath(os.path.expanduser(_v))
        except OSError:
            pass
    return _root
WS_ROOT = _resolve_workspace_root()
WS_BOTS = os.path.join(WS_ROOT, "bots")
WS_BOT_LOGS = os.path.join(WS_ROOT, "bot_logs")
WS_JSON_RELAY = os.path.join(WS_ROOT, "json_relay")
import sys as _ws_sys
_ws_sub = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sub-bots")
if os.path.isdir(_ws_sub) and _ws_sub not in _ws_sys.path:
    _ws_sys.path.insert(0, _ws_sub)  # helper modules (_vision, edge_map, utils, ...) live in bots/sub-bots
for _d in (os.path.join(WS_ROOT, "inbox"), os.path.join(WS_ROOT, "inbox_results", "done"),
           os.path.join(WS_ROOT, "inbox_results", "failed"), os.path.join(WS_ROOT, "owner_inbox"),
           os.path.join(WS_ROOT, "learnings"), os.path.join(WS_ROOT, "rules"), WS_BOT_LOGS, WS_JSON_RELAY):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass
# ──────────────────────────────────────────────────────────────────────────

import re
from datetime import datetime
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")


def _sha256_file(path: str) -> str:
    """Compute SHA-256 of a file's bytes. Returns 'sha256:<hex>' or ''."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return f"sha256:{h.hexdigest()}"
    except Exception:
        return ""


def _extract_version_from_docstring(source: str) -> str:
    """Pull version string from module docstring. Looks for patterns like
    'perps.py, v5.7' or 'v5.6' in the first 60 lines."""
    head = "\n".join(source.split("\n")[:60])
    # Look for typical version patterns
    patterns = [
        r'\.py[,\s]+v(\d+\.\d+(?:\.\d+)?)',          # 'foo.py v1.2' or 'foo.py, v1.2'
        r'^\s*[Vv]ersion[:\s]+v?(\d+\.\d+(?:\.\d+)?)',  # 'Version: 1.2'
        r'v(\d+\.\d+(?:\.\d+)?)\s*[—\-,]',            # 'v1.2 —' or 'v1.2 -'
    ]
    for pat in patterns:
        m = re.search(pat, head, re.MULTILINE)
        if m:
            return f"v{m.group(1)}"
    return "unknown"


def _extract_tunable_params(tree: ast.AST, source: str) -> dict:
    """Walk the AST looking for TUNABLE_PARAMS = {...} assignments.
    Returns the parsed dict structure (best-effort)."""
    result = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # Single-target assignments like TUNABLE_PARAMS = {...}
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id != "TUNABLE_PARAMS":
            continue
        if not isinstance(node.value, ast.Dict):
            continue

        # Walk the dict literal — keys are strings (param names),
        # values are dict literals with type/min/max/default/description
        for key_node, val_node in zip(node.value.keys, node.value.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            param_name = key_node.value
            param_spec = {}

            if isinstance(val_node, ast.Dict):
                for sk, sv in zip(val_node.keys, val_node.values):
                    if not isinstance(sk, ast.Constant) or not isinstance(sk.value, str):
                        continue
                    spec_key = sk.value
                    # Extract constant values; skip non-constants
                    if isinstance(sv, ast.Constant):
                        param_spec[spec_key] = sv.value
                    elif isinstance(sv, ast.List):
                        # List literal — capture as list of constants
                        items = []
                        for item in sv.elts:
                            if isinstance(item, ast.Constant):
                                items.append(item.value)
                        param_spec[spec_key] = items
                    elif isinstance(sv, ast.UnaryOp) and isinstance(sv.op, ast.USub):
                        # Negative number literal: -5 is parsed as UnaryOp
                        if isinstance(sv.operand, ast.Constant):
                            param_spec[spec_key] = -sv.operand.value
                    # else: skip (function calls, name refs, etc.)
            result[param_name] = param_spec
        # We found the dict, stop walking
        break

    return result


def _extract_action_dispatch(tree: ast.AST, dispatch_fn_name: str = None) -> list:
    """Walk the AST looking for any function with `action == "..."` or
    `action in (...)` patterns. Sprint 41 v1.0.1: broadened to scan ALL
    functions, not just those named like 'process_signal'. Some bots
    dispatch inline inside other loop functions (poll_and_execute_signals).

    If dispatch_fn_name is provided, ONLY scan that function. Otherwise all.
    """
    actions = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # If a specific dispatcher is requested, filter to it
        if dispatch_fn_name and dispatch_fn_name not in node.name:
            continue

        # Walk inside this function for `action == "..."` and `action in (...)` checks
        for sub in ast.walk(node):
            if isinstance(sub, ast.Compare):
                # Left side should be a Name 'action' (or similar)
                if not isinstance(sub.left, ast.Name):
                    continue
                if sub.left.id not in ("action", "raw_action", "act", "recommended_action"):
                    continue
                for op, comp in zip(sub.ops, sub.comparators):
                    if isinstance(op, ast.Eq):
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                            actions.add(comp.value)
                    elif isinstance(op, ast.In):
                        # action in ("buy", "sell", ...)
                        if isinstance(comp, (ast.Tuple, ast.List)):
                            for item in comp.elts:
                                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                                    actions.add(item.value)
    return sorted(actions)


def _extract_query_actions(tree: ast.AST) -> list:
    """Find `_QUERY_ACTIONS = {...}` dicts
    at module level. Extract the keys (action names)."""
    actions = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        # Match common query-action dict naming
        if not (target.id.endswith("_QUERY_ACTIONS") or target.id == "QUERY_ACTIONS"):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                actions.add(k.value)
    return sorted(actions)


def _extract_path_constants(tree: ast.AST) -> dict:
    """Find module-level path constants and resolve them symbolically."""
    # Pass 1 — gather all module-level simple Name assignments
    raw: dict[str, ast.AST] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        raw[node.targets[0].id] = node.value

    # Pass 2 — resolve with a symbol table + memo + recursion guard
    resolved: dict[str, str] = {}
    _resolving: set = set()

    def _resolve_name(nm: str) -> str:
        if nm in resolved:
            return resolved[nm]
        if nm in _resolving:
            return ""  # cycle guard
        if nm not in raw:
            # Unknown module-level name (likely a runtime base) -> sentinel
            return "<base>"
        _resolving.add(nm)
        val = _resolve_node(raw[nm])
        _resolving.discard(nm)
        if val:
            resolved[nm] = val
        return val

    def _resolve_node(node) -> str:
        # Reuse the scalar resolver for constants / os.path.join / + / f-string
        scalar = _resolve_path_value(node)
        if scalar:
            return scalar
        # pathlib: <expr> / "segment"  (ast.BinOp with ast.Div)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = _resolve_node(node.left)
            right = _resolve_node(node.right)
            if left and right:
                # normalize to forward-slash joins (registry matches on tail)
                return left.rstrip("/") + "/" + right.lstrip("/")
            return ""
        # Path("literal") or Path(NAME)
        if isinstance(node, ast.Call):
            fn = node.func
            is_path_ctor = (
                (isinstance(fn, ast.Name) and fn.id == "Path") or
                (isinstance(fn, ast.Attribute) and fn.attr == "Path")
            )
            if is_path_ctor:
                if node.args:
                    inner = _resolve_node(node.args[0])
                    return inner or "<base>"
                return "<base>"
            # os.path.dirname(...) / abspath(...) etc -> runtime base
            if isinstance(fn, ast.Attribute) and fn.attr in (
                "dirname", "abspath", "realpath", "expanduser", "cwd", "home"
            ):
                return "<base>"
        # Bare Name reference to another constant
        if isinstance(node, ast.Name):
            return _resolve_name(node.id)
        return ""

    out: dict[str, str] = {}
    for name in raw:
        if not (name.endswith("_FILE") or name.endswith("_DIR") or name.endswith("_PATH")):
            continue
        val = _resolve_name(name)
        if val and val != "<base>":
            out[name] = val
    return out


def _resolve_path_value(node) -> str:
    """Best-effort resolve a string value from an AST node. Returns "" if unresolvable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # os.path.join(...) — concatenate string constants
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "join":
            parts = []
            for arg in node.args:
                resolved = _resolve_path_value(arg)
                if resolved:
                    parts.append(resolved)
                else:
                    return ""  # one part unresolvable → bail
            return os.sep.join(parts) if parts else ""
    # String concatenation: "a" + "b"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_path_value(node.left)
        right = _resolve_path_value(node.right)
        if left and right:
            return left + right
    # f-string (JoinedStr) — only resolve if every part is a constant
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                return ""  # has interpolation, skip
        return "".join(parts)
    return ""


def _extract_loop_registrations(tree: ast.AST) -> list:
    """Find all `asyncio.create_task(<name>(...))` and the contents of
    `asyncio.gather(...)` to enumerate loops the bot runs."""
    loops = set()
    for node in ast.walk(tree):
        # asyncio.create_task(foo())
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in ("create_task", "ensure_future"):
                    if node.args and isinstance(node.args[0], ast.Call):
                        inner = node.args[0].func
                        if isinstance(inner, ast.Name):
                            loops.add(inner.id)
                        elif isinstance(inner, ast.Attribute):
                            loops.add(inner.attr)
                # asyncio.gather(loop1(), loop2(), ...)
                if node.func.attr == "gather":
                    for arg in node.args:
                        if isinstance(arg, ast.Call):
                            inner = arg.func
                            if isinstance(inner, ast.Name):
                                loops.add(inner.id)
                            elif isinstance(inner, ast.Attribute):
                                loops.add(inner.attr)
    return sorted(loops)


def _extract_json_relay_channels(paths: dict) -> list:
    """From the path constants dict, pick out any value that references
    a json_relay folder."""
    channels = []
    for name, val in paths.items():
        if "json_relay" in val.lower():
            channels.append({"path_const": name, "path_value": val})
    return channels


def _classify_output_durability(name: str, val: str) -> str:
    """durable = a learning/state artifact worth protecting from retention.
    ephemeral = cache/dedup/relay-request scratch that is MEANT to churn.

    log_janitor protects 'durable' only; protecting churn-caches would fight
    their own rotation. Classification is by naming convention so it stays
    correct as new files appear (no hardcoded file list)."""
    n = (name + " " + val).lower()
    ephemeral_markers = (
        "cache", "dedup", "_request", "_response", "_req", "_res",
        "scratch", "tmp", "temp", "_pending", "_queue", "heartbeat",
    )
    if "json_relay" in val.lower():
        return "ephemeral"   # relay channels are transient message-passing
    for m in ephemeral_markers:
        if m in n:
            return "ephemeral"
    return "durable"


def _extract_state_files(paths: dict) -> list:
    """From the path constants dict, pick out any value referencing
    bot_logs/ — these are state files the bot owns or reads. Each is tagged
    durable|ephemeral so downstream consumers (log_janitor protection,
    corpus tools, docs) can read this registry instead of hardcoding.
    """
    state = []
    for name, val in paths.items():
        if "bot_logs" in val.lower():
            state.append({
                "const": name,
                "path": val,
                "filename": os.path.basename(val.rstrip("/")),
                "is_dir": name.endswith("_DIR"),
                "durability": _classify_output_durability(name, val),
            })
    return state


def _infer_skill_dependencies(source: str) -> list:
    """Best-effort: scan source for skill references like 'perps-boss',
    'forensics', 'warden' — names mentioned in docstrings or comments."""
    candidates = [
        "perps-boss", "forensics", "pnl-tracker", "autoagent", "code-monitor",
        "warden", "owner-ping", "morning-brief", "learnings-summarizer",
    ]
    found = []
    head = source[:5000]  # only check first 5K chars (header docstring area)
    for c in candidates:
        if c in head:
            found.append(c)
    return found


def generate_self_description(source_file_path: str, output_dir: str = None) -> dict:
    """Generate a structured self-description for the bot file at source_file_path.

    Args:
        source_file_path: absolute path to the bot's .py file. Typically `__file__`.
        output_dir: where to write <bot_name>_self_description.json. If None,
            tries to find bot_logs/ relative to source_file_path.

    Returns:
        The generated dict. Also writes to disk.
    """
    if not os.path.isfile(source_file_path):
        return {"error": f"source not found: {source_file_path}"}

    with open(source_file_path, "r", encoding="utf-8") as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"error": f"AST parse failed: {e}"}

    bot_name = os.path.splitext(os.path.basename(source_file_path))[0]

    actions = _extract_action_dispatch(tree)

    tunable_params = _extract_tunable_params(tree, source)
    query_actions = _extract_query_actions(tree)
    paths = _extract_path_constants(tree)
    loops = _extract_loop_registrations(tree)
    state_files = _extract_state_files(paths)
    json_relay_channels = _extract_json_relay_channels(paths)
    skills = _infer_skill_dependencies(source)
    version = _extract_version_from_docstring(source)
    file_hash = _sha256_file(source_file_path)
    file_size = os.path.getsize(source_file_path)

    desc = {
        "bot_name":              bot_name,
        "version":               version,
        "source_file_path":      source_file_path,
        "source_file_hash":      file_hash,
        "source_file_size":      file_size,
        "generated_at":          datetime.now(EST).isoformat(),
        "actions_supported":     {
            "signal_actions":     actions,
            "query_actions":      query_actions,
        },
        "tunable_params":        tunable_params,
        "state_files_owned":     state_files,
        "loops_running":         loops,
        "json_relay_channels":   json_relay_channels,
        "skill_dependencies":    skills,
        # Sprint 41: for downstream gap-detection in meeting watcher
        "tunable_param_names":   sorted(tunable_params.keys()),
        "signal_action_names":   sorted(actions),
        "query_action_names":    sorted(query_actions),
    }

    # Determine output directory
    if output_dir is None:
        # Walk up from source_file_path looking for a 'bot_logs' sibling
        bot_dir = os.path.dirname(source_file_path)
        candidate = WS_BOT_LOGS
        if os.path.isdir(candidate):
            output_dir = candidate
        else:
            output_dir = WS_BOT_LOGS
            try:
                os.makedirs(output_dir, exist_ok=True)
            except Exception:
                output_dir = bot_dir  # fallback

    out_path = os.path.join(output_dir, f"{bot_name}_self_description.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(desc, f, indent=2, default=str)
        desc["written_to"] = out_path
    except Exception as e:
        desc["write_error"] = str(e)

    return desc


def detect_source_patched(source_file_path: str, output_dir: str = None) -> dict:
    """Compare current source hash against the cached self-description's hash.

    Returns:
        {
          "patched":       bool,           # True if hash mismatch
          "current_hash":  "sha256:...",
          "cached_hash":   "sha256:..." or None,
          "cached_version": "v..." or None,
        }
    """
    bot_name = os.path.splitext(os.path.basename(source_file_path))[0]
    if output_dir is None:
        bot_dir = os.path.dirname(source_file_path)
        output_dir = WS_BOT_LOGS

    cache_path = os.path.join(output_dir, f"{bot_name}_self_description.json")
    current_hash = _sha256_file(source_file_path)

    if not os.path.isfile(cache_path):
        return {
            "patched":        False,
            "current_hash":   current_hash,
            "cached_hash":    None,
            "cached_version": None,
            "first_boot":     True,
        }

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        cached_hash = cached.get("source_file_hash")
        cached_version = cached.get("version")
    except Exception:
        return {
            "patched":        False,
            "current_hash":   current_hash,
            "cached_hash":    None,
            "cached_version": None,
            "cache_read_error": True,
        }

    return {
        "patched":        (cached_hash != current_hash) if cached_hash else False,
        "current_hash":   current_hash,
        "cached_hash":    cached_hash,
        "cached_version": cached_version,
        "first_boot":     False,
    }


if __name__ == "__main__":
    # CLI usage: python _self_description.py <path_to_bot.py>
    import sys
    if len(sys.argv) < 2:
        print("Usage: python _self_description.py <path_to_bot.py>")
        sys.exit(1)
    desc = generate_self_description(sys.argv[1])
    print(json.dumps(desc, indent=2, default=str))