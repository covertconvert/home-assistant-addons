"""Claude CLI subprocess wrapper.

Each Session owns one `claude` subprocess running in stream-json mode
(both input and output as JSONL). The subprocess is long-lived per
conversation so multi-turn context works.

Raw CLI events are translated into a small, stable event vocabulary the
frontend understands:

    user_message        { text, attachments }
    assistant_text      { id, text }            full assistant text block
    tool_use            { id, name, summary, input }
    tool_result         { tool_id, output, error }
    generation_started  {}
    generation_ended    { subtype, duration_ms? }
    error               { message }
    session_ended       {}

Permission enforcement is delegated to the PreToolUse hook (security.py);
the CLI runs with --permission-mode bypassPermissions so the hook is the
single source of truth.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auth import load_token
from persistence import discover_orphan_sessions, load_index, save_index, session_jsonl_path
from settings import write_mcp_config_if_enabled
import projects as projects_store

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
WORKDIR = os.environ.get("CLAUDE_WORKDIR", "/config")


_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}


def _image_block(path: str) -> dict | None:
    """Build a base64 image content block — the Anthropic API doesn't accept
    file-path sources, so we inline the bytes."""
    import logging
    p = Path(path)
    if not p.is_file():
        logging.warning("image not found: %s", path)
        return None
    raw = p.read_bytes()
    if not raw:
        logging.warning("image empty: %s", path)
        return None
    # Sniff actual format from magic bytes instead of trusting the extension —
    # iOS likes to rename heic→jpg, screenshots can mis-extension, etc.
    media_type = _sniff_media_type(raw) or _MEDIA_TYPES.get(p.suffix.lower())
    if not media_type:
        logging.warning("image format unrecognized: %s (size=%d)", path, len(raw))
        return None
    data = base64.b64encode(raw).decode("ascii")
    logging.info("attaching image %s (%s, %d bytes)", path, media_type, len(raw))
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _sniff_media_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _tool_summary(name: str, input_: dict) -> str:
    if name == "Bash":
        cmd = input_.get("command", "")
        return cmd if len(cmd) <= 80 else cmd[:77] + "…"
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return input_.get("file_path", "")
    if name == "Grep":
        return input_.get("pattern", "")
    if name == "Glob":
        return input_.get("pattern", "")
    if name == "WebFetch":
        return input_.get("url", "")
    return ""


@dataclass
class Session:
    id: str
    title: str = "New chat"
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    project_id: str | None = None
    model: str | None = None
    permission_mode: str = "default"
    proc: asyncio.subprocess.Process | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    _subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    _reader_task: asyncio.Task | None = None
    _stderr_task: asyncio.Task | None = None
    _interrupting: bool = False

    def subscribe(self) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]:
        """Atomic (history snapshot, live queue) — no event is in both.

        Append-and-snapshot run in one synchronous block, so any concurrent
        ``_emit`` either (a) already finished and is in the snapshot, or (b)
        hasn't started yet and will land in the new queue. Nothing in between.
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(q)
        return list(self.history), q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def start(self) -> None:
        """Launch the claude subprocess in stream-json mode.

        First boot of a session uses --session-id to pin the UUID; subsequent
        relaunches (rehydrated from disk, or restarted after the CLI exited)
        must use --resume because the CLI rejects a reused --session-id.
        """
        if self.proc is not None:
            return
        existing = session_jsonl_path(self.id).exists()
        mcp_config = write_mcp_config_if_enabled()
        notes_path = projects_store.notes_path_for_project(self.project_id) if self.project_id else None
        cmd = [
            CLAUDE_BIN,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            *(["--resume", self.id] if existing else ["--session-id", self.id]),
            *(["--mcp-config", str(mcp_config)] if mcp_config else []),
            *(["--model", self.model] if self.model else []),
            *(["--permission-mode", "plan"] if self.permission_mode == "plan" else []),
            *(["--append-system-prompt-file", str(notes_path)] if notes_path else []),
            "--verbose",
        ]
        env = {**os.environ, "FORCE_COLOR": "0"}
        token = load_token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=WORKDIR,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _emit(self, evt: dict) -> None:
        self.history.append(evt)
        self.last_activity = time.time()
        for q in list(self._subscribers):
            await q.put(evt)

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        import logging
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                rc = self.proc.returncode if self.proc else None
                logging.warning("claude stdout closed (returncode=%s)", rc)
                if self._interrupting:
                    # User-initiated stop. The subprocess will be respawned on
                    # the next message via ensure_started — keep the SSE stream
                    # alive so the UI doesn't show "Session ended".
                    self._interrupting = False
                    await self._emit({"type": "generation_ended", "subtype": "interrupted"})
                else:
                    await self._emit({"type": "session_ended", "returncode": rc})
                break
            try:
                raw = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            for evt in self._translate(raw):
                await self._emit(evt)

    def _translate(self, raw: dict) -> list[dict]:
        """Convert one CLI event into 0+ normalized frontend events."""
        kind = raw.get("type")

        if kind == "system":
            # init / config events — not useful to surface
            return []

        if kind == "assistant":
            msg = raw.get("message", {}) or {}
            msg_id = msg.get("id") or uuid.uuid4().hex
            out: list[dict] = []
            usage = msg.get("usage") or {}
            if usage:
                out.append({
                    "type": "usage",
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                    "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
                })
            for block in msg.get("content", []) or []:
                btype = block.get("type")
                if btype == "text":
                    out.append({
                        "type": "assistant_text",
                        "id": msg_id,
                        "text": block.get("text", ""),
                    })
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {}) or {}
                    out.append({
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": name,
                        "summary": _tool_summary(name, inp),
                        "input": inp,
                    })
            return out

        if kind == "user":
            # CLI echoes tool_result blocks back as user messages
            msg = raw.get("message", {}) or {}
            out = []
            for block in msg.get("content", []) or []:
                if block.get("type") == "tool_result":
                    out.append({
                        "type": "tool_result",
                        "tool_id": block.get("tool_use_id"),
                        "output": block.get("content", ""),
                        "error": bool(block.get("is_error")),
                    })
            return out

        if kind == "result":
            return [{
                "type": "generation_ended",
                "subtype": raw.get("subtype", "success"),
                "duration_ms": raw.get("duration_ms"),
            }]

        return []

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        import logging
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            logging.warning("claude stderr: %s", text)
            await self._emit({"type": "error", "message": text})

    async def send_message(self, text: str, attachments: list[str] | None = None) -> None:
        import logging
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("Session not started")
        attachments = attachments or []
        logging.info("send_message: text_len=%d attachments=%r", len(text), attachments)

        # Local echo so SSE replay includes the user's own turn.
        await self._emit({
            "type": "user_message",
            "text": text,
            "attachments": attachments,
        })
        await self._emit({"type": "generation_started"})

        # Auto-title from first user message.
        if self.title == "New chat" and text:
            self.title = (text[:40] + "…") if len(text) > 40 else text

        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for path in attachments:
            block = _image_block(path)
            if block:
                content.append(block)
        payload = {"type": "user", "message": {"role": "user", "content": content}}
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def respond_to_permission(self, approve: bool) -> None:
        # Permission flow is enforced by the PreToolUse hook (security.py).
        # Reserved for a future in-app approval channel.
        return

    async def interrupt(self) -> None:
        # Hard-stop: SIGINT → SIGTERM → SIGKILL. The CLI runs in stream-json
        # mode with plain pipes, so there's no keystroke channel — signals are
        # the only way to cancel mid-generation. Escalating guarantees the
        # process actually dies regardless of how the CLI handles SIGINT.
        if not self.proc or self.proc.returncode is not None:
            return
        self._interrupting = True
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                self.proc.send_signal(sig)
            except ProcessLookupError:
                self._interrupting = False
                return
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=1.0)
                return
            except asyncio.TimeoutError:
                continue

    async def clear_context(self) -> None:
        """Wipe this thread's context: stop the CLI process, delete the jsonl
        history file, and clear in-memory history. Next message spawns a fresh
        CLI session under the same UUID (jsonl is gone so --session-id works).
        """
        await self.stop()
        path = session_jsonl_path(self.id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        self.history.clear()
        await self._emit({"type": "system_message", "text": "Context cleared"})

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


class SessionManager:
    def __init__(self, max_sessions: int = 20) -> None:
        self.max_sessions = max_sessions
        self.sessions: dict[str, Session] = {}
        # Rehydrate sessions from disk so they survive addon restarts. We
        # only restore metadata + history; the claude subprocess starts on
        # first access (lazy via ensure_started).
        metas = load_index()
        known = {m["id"] for m in metas}
        metas.extend(discover_orphan_sessions(known))
        for meta in metas:
            sess = Session(
                id=meta["id"],
                title=meta.get("title", "New chat"),
                created_at=meta.get("created_at", time.time()),
                last_activity=meta.get("last_activity", time.time()),
                project_id=meta.get("project_id"),
                model=meta.get("model"),
                permission_mode=meta.get("permission_mode") or "default",
            )
            self.sessions[sess.id] = sess
        if self.sessions:
            self._persist()

    def _persist(self) -> None:
        save_index([
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "last_activity": s.last_activity,
                "project_id": s.project_id,
                "model": s.model,
                "permission_mode": s.permission_mode,
            }
            for s in self.sessions.values()
        ])

    async def create(self, title: str | None = None, project_id: str | None = None) -> Session:
        if len(self.sessions) >= self.max_sessions:
            oldest = min(self.sessions.values(), key=lambda s: s.last_activity)
            await self.delete(oldest.id)
        sess = Session(id=str(uuid.uuid4()), title=title or "New chat", project_id=project_id)
        await sess.start()
        self.sessions[sess.id] = sess
        self._persist()
        return sess

    def update(self, session_id: str, *, title: str | None = None,
               project_id: str | None = "__unset__",
               model: str | None = "__unset__") -> Session | None:
        """Rename, move, or change model. Sentinel allows None = clear."""
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        if title is not None:
            t = title.strip()
            if t:
                sess.title = t
        if project_id != "__unset__":
            sess.project_id = project_id
        if model != "__unset__":
            sess.model = model
        self._persist()
        return sess

    async def set_model(self, session_id: str, model: str | None) -> Session | None:
        """Change a session's model. Kills the current CLI process so the next
        message respawns with --model X (or unset)."""
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        sess.model = model
        await sess.stop()
        self._persist()
        return sess

    async def set_permission_mode(self, session_id: str, mode: str) -> Session | None:
        """Change a session's permission mode (default | plan). Kills the
        current CLI process so the next message respawns with the new flag."""
        if mode not in ("default", "plan"):
            return None
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        sess.permission_mode = mode
        await sess.stop()
        self._persist()
        return sess

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def ensure_started(self, session_id: str) -> Session | None:
        """Spawn the claude subprocess if it isn't running (or has exited)."""
        sess = self.sessions.get(session_id)
        if sess and (sess.proc is None or sess.proc.returncode is not None):
            sess.proc = None
            await sess.start()
        return sess

    def list(self) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    "id": s.id,
                    "title": s.title,
                    "created_at": s.created_at,
                    "last_activity": s.last_activity,
                    "project_id": s.project_id,
                    "model": s.model,
                    "permission_mode": s.permission_mode,
                }
                for s in self.sessions.values()
            ),
            key=lambda s: s["last_activity"],
            reverse=True,
        )

    async def delete(self, session_id: str) -> bool:
        sess = self.sessions.pop(session_id, None)
        if sess:
            await sess.stop()
            try:
                session_jsonl_path(session_id).unlink(missing_ok=True)
            except OSError:
                pass
            self._persist()
            return True
        return False

    def touch(self, session_id: str) -> None:
        """Update last_activity + persist; called on any session activity."""
        sess = self.sessions.get(session_id)
        if sess:
            sess.last_activity = time.time()
            self._persist()
