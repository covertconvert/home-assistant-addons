"""Claude Code PreToolUse hook — enforces hard rules from security.py.

Registered via settings.json in CLAUDE_CONFIG_DIR. Receives a JSON payload on
stdin describing the pending tool call, then:

  exit 0  -> allow
  exit 2  -> block (stderr is shown to Claude as the reason)

Side-effect: when a PROTECTED file is about to be modified, copies the current
contents to <path>.bak.<timestamp> so the change is recoverable.

Every decision (allow/block/snapshot) is appended to the audit log.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import log as audit_log  # noqa: E402
from security import (  # noqa: E402
    is_destructive_bash,
    is_forbidden,
    is_protected,
    is_readonly,
    snapshot_path,
)
from settings import load as load_settings  # noqa: E402

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

SESSION_ID_KEY = "session_id"

# Server runs inside the same container; the addon binds to 8099 by default.
PERMISSION_URL = f"http://127.0.0.1:{os.environ.get('PORT', '8099')}/api/internal/permission"
PERMISSION_TIMEOUT_S = 600  # 10 minutes for the user to respond


def _block(reason: str, session_id: str, payload: dict) -> None:
    audit_log(session_id, "hook_block", {"reason": reason, "payload": payload})
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked by Claude Code Messages security: {reason}",
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.exit(0)


def _allow(session_id: str, payload: dict) -> None:
    audit_log(session_id, "hook_allow", payload)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "approved by Claude Code Messages security",
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.exit(0)


def _snapshot(path: str, session_id: str) -> None:
    src = Path(path)
    if not src.exists():
        return
    dest = snapshot_path(path)
    try:
        shutil.copy2(src, dest)
        audit_log(session_id, "hook_snapshot", {"src": path, "dest": dest})
    except OSError as e:
        audit_log(session_id, "hook_snapshot_failed", {"src": path, "error": str(e)})


def _ask_user(session_id: str, tool_name: str, tool_input: dict) -> bool:
    """Block until the user clicks Approve/Reject in the UI. Returns True for
    approve. Any error → False (fail-closed; better to block than slip through).
    """
    body = json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }).encode("utf-8")
    req = urllib.request.Request(
        PERMISSION_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PERMISSION_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return False
    return bool(payload.get("approved"))


def _url_host(url: str) -> str:
    """Extract bare host from a URL. Returns '' if unparseable."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url if "://" in url else f"http://{url}")
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


def _paths_from_input(tool_name: str, tool_input: dict) -> list[str]:
    """Return every filesystem path referenced by this tool call."""
    paths: list[str] = []
    if tool_name in ("Write", "Edit", "Read", "NotebookEdit"):
        if "file_path" in tool_input:
            paths.append(tool_input["file_path"])
    elif tool_name == "MultiEdit":
        if "file_path" in tool_input:
            paths.append(tool_input["file_path"])
    return paths


def main() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("hook: malformed JSON on stdin\n")
        sys.exit(1)

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    session_id = event.get(SESSION_ID_KEY, "unknown")

    summary = {"tool": tool_name, "input": tool_input}

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if is_destructive_bash(cmd):
            _block(f"destructive bash pattern: {cmd[:120]}", session_id, summary)
        try:
            ask = bool(load_settings().get("ask_bash"))
        except Exception:
            ask = True
        if ask:
            if not _ask_user(session_id, tool_name, tool_input):
                _block(f"user rejected bash: {cmd[:120]}", session_id, summary)
        _allow(session_id, summary)
        return

    if tool_name == "exit_plan_mode" or tool_name == "ExitPlanMode":
        # Claude calls this when it's done planning and wants to start coding.
        # Always ask — the plan was already streamed as an assistant message,
        # so the user has it in front of them and can decide whether to start
        # coding or refine the plan first.
        if not _ask_user(session_id, tool_name, tool_input):
            # User chose "Refine plan". The deny reason becomes the tool_result
            # Claude sees, so phrase it as a directive: ask what to change
            # rather than just acknowledging the rejection.
            audit_log(session_id, "hook_block", {"reason": "plan_refine_requested", "payload": summary})
            sys.stdout.write(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "The user wants to refine the plan before implementation. "
                        "Stay in plan mode. Ask them what they'd like to change, "
                        "add, or remove, then propose a revised plan."
                    ),
                }
            }))
            sys.exit(0)
        _allow(session_id, summary)
        return

    if tool_name == "WebFetch":
        url = ""
        for k in ("url", "URL", "uri", "href"):
            v = tool_input.get(k)
            if isinstance(v, str) and v:
                url = v
                break
        try:
            s = load_settings()
            ask = bool(s.get("ask_webfetch"))
            allowed = {h.lower() for h in (s.get("webfetch_allowed_domains") or [])}
        except Exception:
            ask = True
            allowed = set()
        host = _url_host(url)
        if host and host.lower() in allowed:
            _allow(session_id, summary)
            return
        if ask:
            if not _ask_user(session_id, tool_name, tool_input):
                _block(f"user rejected webfetch: {url[:120]}", session_id, summary)
        _allow(session_id, summary)
        return

    for path in _paths_from_input(tool_name, tool_input):
        if is_forbidden(path):
            _block(f"forbidden path: {path}", session_id, summary)
        if tool_name in WRITE_TOOLS and is_readonly(path):
            _block(f"read-only path (writes blocked): {path}", session_id, summary)
        if tool_name in WRITE_TOOLS and is_protected(path):
            _snapshot(path, session_id)

    _allow(session_id, summary)


if __name__ == "__main__":
    main()
