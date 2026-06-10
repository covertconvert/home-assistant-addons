"""Session metadata persistence + history replay.

The `claude` CLI writes every turn to a per-session jsonl file under
`<CLAUDE_CONFIG_DIR>/projects/<cwd-dashified>/<session-id>.jsonl`. That
file is the source of truth for conversation content — we just need to
read it on resume and translate into our frontend event vocabulary.

Our own metadata (title, last_activity) is small and not in the jsonl,
so we persist it to a single JSON index file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/config/claude-config"))
WORKDIR = os.environ.get("CLAUDE_WORKDIR", "/config")
INDEX_FILE = CONFIG_DIR / "messages-sessions.json"


def _project_dir() -> Path:
    """CLI dasherizes the cwd: /config → -config, /foo/bar → -foo-bar."""
    slug = WORKDIR.replace("/", "-")
    return CONFIG_DIR / "projects" / slug


def session_jsonl_path(session_id: str) -> Path:
    return _project_dir() / f"{session_id}.jsonl"


def load_index() -> list[dict[str, Any]]:
    if not INDEX_FILE.exists():
        return []
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save_index(sessions: list[dict[str, Any]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def discover_orphan_sessions(known_ids: set[str]) -> list[dict[str, Any]]:
    """Find jsonl files for sessions not yet in the index — happens once,
    when the persistence file is first introduced and pre-existing CLI
    sessions need to be adopted. Seeds title from the first user text."""
    pdir = _project_dir()
    if not pdir.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in pdir.glob("*.jsonl"):
        sid = path.stem
        if sid in known_ids:
            continue
        title = "Recovered chat"
        for evt in read_history(sid):
            if evt.get("type") == "user_message" and evt.get("text"):
                t = evt["text"]
                title = (t[:40] + "…") if len(t) > 40 else t
                break
        mtime = path.stat().st_mtime
        found.append({
            "id": sid,
            "title": title,
            "created_at": mtime,
            "last_activity": mtime,
        })
    return found


def read_history(session_id: str) -> Iterator[dict[str, Any]]:
    """Yield frontend-shaped events reconstructed from the CLI's jsonl.

    Skips queue-operation / attachment / last-prompt noise. Emits one
    event per text block and per tool_use / tool_result block, mirroring
    what Session._translate does for live events.
    """
    path = _project_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield from _translate(raw)
    except OSError:
        return


def _translate(raw: dict) -> Iterator[dict]:
    kind = raw.get("type")
    if kind == "user":
        msg = raw.get("message", {}) or {}
        # Coalesce text + image blocks into a single user_message so the
        # rendered turn matches what was originally sent. The /data/uploads
        # file is gone by the time we replay, but the base64 payload sits
        # right here in the jsonl — emit it as a data URL.
        text_parts: list[str] = []
        attachments: list[dict] = []
        tool_results: list[dict] = []
        content = msg.get("content", []) or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text", "")
                # The CLI injects this literal marker into the jsonl whenever
                # it receives SIGINT mid-generation. Render as a system "Stopped"
                # line to match the live UX instead of a fake user turn.
                if txt.strip() == "[Request interrupted by user]":
                    tool_results.append({"type": "system_message", "text": "Stopped"})
                    continue
                # CLI wraps slash-commands (/clear, /compact, etc.) and other
                # internal hooks in <local-command-*> / <command-*> tags.
                # Skip — they're not real user input.
                stripped = txt.lstrip()
                if stripped.startswith("<local-command-") or stripped.startswith("<command-"):
                    continue
                text_parts.append(txt)
            elif btype == "image":
                src = block.get("source", {}) or {}
                if src.get("type") == "base64" and src.get("data"):
                    media = src.get("media_type", "image/png")
                    attachments.append({"dataUrl": f"data:{media};base64,{src['data']}"})
            elif btype == "tool_result":
                tool_results.append({
                    "type": "tool_result",
                    "tool_id": block.get("tool_use_id"),
                    "output": block.get("content", ""),
                    "error": bool(block.get("is_error")),
                })
        if text_parts or attachments:
            yield {
                "type": "user_message",
                "text": "".join(text_parts),
                "attachments": attachments,
            }
        yield from tool_results
    elif kind == "assistant":
        msg = raw.get("message", {}) or {}
        msg_id = msg.get("id") or raw.get("uuid", "")
        a_content = msg.get("content", []) or []
        if isinstance(a_content, str):
            a_content = [{"type": "text", "text": a_content}]
        for block in a_content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                yield {"type": "assistant_text", "id": msg_id, "text": block.get("text", "")}
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {}) or {}
                yield {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": name,
                    "summary": _tool_summary(name, inp),
                    "input": inp,
                }


def session_cost(session_id: str) -> dict[str, int]:
    """Sum token usage across the session's jsonl. Returns zeros if no file."""
    totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    path = _project_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return totals
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if raw.get("type") != "assistant":
                    continue
                u = ((raw.get("message") or {}).get("usage") or {})
                totals["input"] += int(u.get("input_tokens") or 0)
                totals["output"] += int(u.get("output_tokens") or 0)
                totals["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)
                totals["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
    except OSError:
        pass
    return totals


def _tool_summary(name: str, input_: dict) -> str:
    if name == "Bash":
        cmd = input_.get("command", "")
        return cmd if len(cmd) <= 80 else cmd[:77] + "…"
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return input_.get("file_path", "")
    if name in ("Grep", "Glob"):
        return input_.get("pattern", "")
    if name == "WebFetch":
        return input_.get("url", "")
    return ""
