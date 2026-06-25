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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auth import load_token
from persistence import discover_orphan_sessions, load_index, save_index, session_jsonl_path
from settings import write_mcp_config_if_enabled
import projects as projects_store

_generation_ended_hook: Callable[["Session", dict], Awaitable[None]] | None = None


def set_generation_ended_hook(
    hook: Callable[["Session", dict], Awaitable[None]] | None,
) -> None:
    """Server installs a coroutine here to react to generation_ended events
    independently of any connected SSE client. Used so the push-notification
    fires even when the browser tab has been backgrounded and its SSE pipe
    has been suspended by the OS."""
    global _generation_ended_hook
    _generation_ended_hook = hook

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
WORKDIR = os.environ.get("CLAUDE_WORKDIR", "/config")

# Appended to every CCM session's system prompt. Drives Claude to *do* the
# follow-up action (reload YAML, etc.) and let the permission card confirm,
# rather than telling the user to do it manually. Each line is a small nudge,
# not a hard rule — Claude can still skip it when context calls for it.
CCM_NUDGE_PROMPT = (
    "You are running inside the Claude Code Messages Home Assistant addon. "
    "After editing any HA YAML file under /config (automations.yaml, scripts.yaml, "
    "scenes.yaml, configuration.yaml, or files under packages/ or lovelace/), "
    "immediately call the matching Home Assistant reload service via the "
    "home-assistant MCP (ha_call_service with domain=automation/script/scene/"
    "homeassistant and service=reload). The user will see a permission card and "
    "can approve or reject — that's the right place to confirm, not a chat "
    "instruction telling them to reload manually.\n\n"
    "When producing SVG diagrams, mockups, or any graphic output, output the SVG "
    "inline in your response wrapped in a ```svg fenced code block. Do NOT write "
    "SVGs to files unless the user explicitly asks for a file. The chat interface "
    "renders ```svg blocks directly — raw <svg> tags without the fence will not "
    "render correctly.\n\n"
    "Do NOT use the AskUserQuestion tool — this interface cannot render it and the "
    "CLI auto-fails it. When you need the user to choose between options, just ask "
    "in a normal chat message and let them reply in text.\n\n"
    "When you have several questions for the user, ask them ONE AT A TIME — one "
    "question per message — and wait for the reply before asking the next, rather "
    "than batching them into a single message. This one-at-a-time style suits the "
    "chat interface and keeps each answer focused. Offer clear lettered options "
    "(A/B/…) for choices, and for pick-any questions say so and include a 'none' "
    "option."
)


# Sentinel prefix on the synthetic "you were interrupted" instruction block.
# Sent to the model on the first message after a Stop, then stripped from the
# rehydrated history by persistence so the user never sees it. Must stay in
# sync with the matching check in persistence.py.
INTERRUPT_NOTE_TAG = "[ccm-interrupt-note]"
INTERRUPT_NOTE_END = "[/ccm-interrupt-note]"

# Marker the model is asked to echo for the CLI's auto-injected resume turn.
RESUME_ACK_MARKER = "<<ccm-resume-ack>>"
# Resume prompt fed to the CLI via CLAUDE_CODE_RESUME_PROMPT. It replaces the
# default "Continue from where you left off." so the synthetic resume turn
# produces a fixed, filterable reply instead of a confusing "No response
# requested." See [[project-ccm-resume-prompt]] memory for the full story.
RESUME_PROMPT_SENTINEL = (
    "This is an automated session-resume ping with no task attached. "
    f"Reply with exactly this token and nothing else: {RESUME_ACK_MARKER}"
)
# Shown in the thread when the CLI is respawned with --resume for a reason the
# user didn't initiate (crash, addon restart, or context compaction). Deliberate
# respawns (Stop, model/effort switch, plan refine) suppress it — they're
# expected and already have their own UI feedback.
RESUME_BANNER_TEXT = (
    "Conversation resumed after a context reset — anything that was in progress "
    "may be incomplete. If your last request didn't finish, please send it again."
)

# Longest a chat title may be stored as (user rename or auto-title).
MAX_TITLE_LEN = 50
# Auto-titles aim shorter so they read as a punchy phrase in the topbar pill.
AUTO_TITLE_LEN = 30


def _smart_title(text: str, limit: int = AUTO_TITLE_LEN) -> str:
    """Trim to `limit` chars on a word boundary, appending … when cut."""
    t = " ".join(text.split())  # collapse whitespace/newlines
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0]
    if len(cut) < limit * 0.6:  # no sensible word break — hard cut
        cut = t[:limit]
    return cut.rstrip() + "…"


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
    effort: str | None = None
    permission_mode: str = "default"
    proc: asyncio.subprocess.Process | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    _subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    _reader_task: asyncio.Task | None = None
    _stderr_task: asyncio.Task | None = None
    _tail_task: asyncio.Task | None = None
    _interrupting: bool = False
    _interrupted_last_turn: bool = False
    # True between seeing the CLI's synthetic resume-prompt user turn and the end
    # of the model's reply to it — used to swallow that whole spurious turn.
    _suppressing_resume: bool = False
    # Armed when we start a --resume session, before the synthetic resume-prompt
    # user event has arrived on stdout. The CLI sometimes never echoes the
    # resume-prompt user turn on stdout (emitting it only to the JSONL), so the
    # normal RESUME_ACK_MARKER detection in the user handler never fires and
    # _suppressing_resume is never set. This flag lets the assistant handler
    # detect the resume ack from its stop_reason=stop_sequence instead.
    _awaiting_resume_ack: bool = False
    _silent_shutdown: bool = False
    _plan_refine_pending: bool = False
    # Set True for respawns the user initiated (Stop, model/effort switch, plan
    # refine) so the next synthetic resume turn does NOT show RESUME_BANNER_TEXT.
    # Default False means an unexpected resume (crash/compaction) DOES warn.
    _suppress_next_resume_banner: bool = False
    # True while a thinking content block is streaming (between its
    # content_block_start and content_block_stop in the partial-message stream).
    _thinking_streaming: bool = False
    # Events seen on stdout — used to dedupe the jsonl-tail backup reader.
    # CLI v2.1.170 in --output-format stream-json sometimes stops emitting to
    # stdout mid-turn while still writing to its per-session jsonl. The tail
    # reader catches what stdout missed.
    _seen_uuids: set[str] = field(default_factory=set)
    # User has tapped "Trust Bash this turn" on a permission card. Subsequent
    # Bash PreToolUse hook calls auto-approve until generation_ended fires.
    _bash_trust_until_turn_end: bool = False
    _gen_last_activity: float = 0.0
    _watchdog_task: asyncio.Task | None = None
    # Heartbeat timestamp from the client telling us the user is actively
    # looking at this session. Zero means stale/away. Server-side
    # turn-finished push uses this to decide whether to notify.
    last_focused_at: float = 0.0

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
        notes_text = ""
        if notes_path:
            try:
                notes_text = notes_path.read_text(encoding="utf-8").strip()
            except OSError:
                notes_text = ""
        combined_prompt = (
            f"{CCM_NUDGE_PROMPT}\n\n{notes_text}" if notes_text else CCM_NUDGE_PROMPT
        )
        cmd = [
            CLAUDE_BIN,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            *(["--resume", self.id] if existing else ["--session-id", self.id]),
            *(["--mcp-config", str(mcp_config)] if mcp_config else []),
            *(["--model", self.model] if self.model else []),
            *(["--effort", self.effort] if self.effort else []),
            *(["--permission-mode", "plan"] if self.permission_mode == "plan" else []),
            "--append-system-prompt", combined_prompt,

        ]
        env = {**os.environ, "FORCE_COLOR": "0"}
        # The CLI auto-injects a "resume prompt" as a synthetic user turn when a
        # session is respawned with --resume (e.g. after a Stop). Its default is
        # "Continue from where you left off.", which makes the model emit a
        # spurious "No response requested." before the user's real message is
        # handled. Override it with a sentinel that asks for a fixed marker, then
        # filter that marker out in _translate so the user never sees the turn.
        env["CLAUDE_CODE_RESUME_PROMPT"] = RESUME_PROMPT_SENTINEL
        token = load_token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        # Arm the resume-ack suppression before the process starts so it's
        # race-free: the stdout/JSONL events from the synthetic resume turn can
        # arrive the moment the process writes them, and the flag must already
        # be set by then.
        if existing:
            self._awaiting_resume_ack = True
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=WORKDIR,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # New process group so we can killpg() the whole tree on stop.
            # The `claude` binary is a wrapper that spawns a node child; a
            # plain SIGINT to the parent doesn't reach the child, which is
            # why the stop button used to be dead during long generations.
            start_new_session=True,
            # Default asyncio limit is 64 KB — large tool results (file reads,
            # bash output) can exceed that, crashing _read_stdout silently.
            # 10 MB covers any realistic CLI event.
            limit=10 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._tail_task = asyncio.create_task(self._tail_jsonl())

    async def _emit(self, evt: dict) -> None:
        # Thinking-stream events are live-only: high volume and ephemeral, so we
        # don't persist them to history (no snapshot replay, no jsonl bloat).
        if evt.get("type") not in ("thinking_start", "thinking_delta", "thinking_stop"):
            self.history.append(evt)
        self.last_activity = time.time()
        self._gen_last_activity = time.time()
        etype = evt.get("type")
        # Reset per-turn Bash trust at both ends of a turn. generation_ended is
        # the normal path; generation_started is the safety net for turns that
        # ended abnormally (crash, pending permission never resolved) without
        # emitting generation_ended, which would otherwise leave the flag set
        # indefinitely across future turns.
        if etype in ("generation_started", "generation_ended"):
            self._bash_trust_until_turn_end = False
        # Watchdog: start a timer on generation_started that kills the process
        # if no event arrives for GENERATION_WATCHDOG_SECS. Cancelled on
        # generation_ended so normal completions are unaffected.
        if etype == "generation_started":
            if self._watchdog_task and not self._watchdog_task.done():
                self._watchdog_task.cancel()
            self._watchdog_task = asyncio.create_task(self._watchdog())
        elif etype == "generation_ended":
            if self._watchdog_task and not self._watchdog_task.done():
                self._watchdog_task.cancel()
            self._watchdog_task = None
        for q in list(self._subscribers):
            await q.put(evt)
        if etype == "generation_ended" and _generation_ended_hook is not None:
            asyncio.create_task(_generation_ended_hook(self, evt))

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        import logging
        # Tee raw CLI stdout to a debug file so a stuck-spinner repro tells us
        # exactly what event shapes the CLI is emitting. Size-bounded: if it has
        # grown past the cap, start fresh so it can't grow without limit (the old
        # code claimed to truncate but opened append-only, so it never did).
        debug_path = "/config/ccm-stdout-debug.log"
        try:
            if os.path.exists(debug_path) and os.path.getsize(debug_path) > 5_000_000:
                open(debug_path, "w").close()
            debug_f = open(debug_path, "a", buffering=1)
            debug_f.write(f"\n=== session {self.id} start ts={time.time():.3f} ===\n")
        except OSError:
            debug_f = None
        while True:
            try:
                line = await self.proc.stdout.readline()
            except ValueError:
                # Line exceeded the stream reader limit (shouldn't happen with
                # 10 MB limit, but guard anyway). Drain to the next newline so
                # the stream stays in sync; _tail_jsonl covers the missed event.
                import logging as _log
                _log.warning("ccm: stdout line too long, draining to next newline")
                try:
                    while True:
                        chunk = await self.proc.stdout.read(65536)
                        if not chunk or b'\n' in chunk:
                            break
                except Exception:
                    pass
                continue
            if not line:
                rc = self.proc.returncode if self.proc else None
                logging.warning("claude stdout closed (returncode=%s)", rc)
                if self._plan_refine_pending:
                    # Plan-mode refine handoff. User tapped Refine; the server
                    # killed the proc immediately (so Claude doesn't generate
                    # an interim "ok proceeding…" before any natural crash).
                    # Respawn still in plan mode and auto-inject a "ask me what
                    # to change" message so Claude asks a question instead of
                    # resubmitting.
                    self._plan_refine_pending = False
                    self._suppress_next_resume_banner = True
                    self.proc = None
                    asyncio.create_task(self._continue_after_plan_refine())
                elif self._silent_shutdown:
                    # Mode/model switch kill. We do not want any banner —
                    # the next message will respawn the proc cleanly.
                    self._silent_shutdown = False
                    self._suppress_next_resume_banner = True
                elif self._interrupting:
                    # User-initiated stop. The subprocess will be respawned on
                    # the next message via ensure_started — keep the SSE stream
                    # alive so the UI doesn't show "Session ended".
                    self._interrupting = False
                    self._interrupted_last_turn = True
                    self._suppress_next_resume_banner = True
                    await self._emit({"type": "generation_ended", "subtype": "interrupted"})
                else:
                    await self._emit({"type": "session_ended", "returncode": rc})
                break
            if debug_f:
                try:
                    debug_f.write(f"{time.time():.3f} {line.decode('utf-8', errors='replace')}")
                except (OSError, UnicodeError):
                    pass
            try:
                raw = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            u = raw.get("uuid")
            if u:
                self._seen_uuids.add(u)
            for evt in self._translate(raw):
                await self._emit(evt)

    def _translate(self, raw: dict) -> list[dict]:
        """Convert one CLI event into 0+ normalized frontend events."""
        kind = raw.get("type")

        # Defensive: stop_reason can ride on multiple event shapes — top-level
        # (`result`), nested in `message` (`assistant`), or inside `delta` for
        # streaming `message_delta` events. Catch any terminal stop_reason
        # regardless of carrier so the UI spinner doesn't get stranded.
        sr = (
            raw.get("stop_reason")
            or (raw.get("message") or {}).get("stop_reason")
            or (raw.get("delta") or {}).get("stop_reason")
        )
        terminal_sr = sr in ("end_turn", "stop_sequence", "max_tokens")

        # Live thinking stream (only present with --include-partial-messages).
        # Defensive: the partial event may be wrapped under "event" or arrive at
        # the top level. We ONLY extract thinking; any other shape returns [] so
        # a wrong guess can never affect text/tool rendering (those come from the
        # complete `assistant` message, handled below and left untouched).
        ev = None
        if kind == "stream_event":
            ev = raw.get("event") or {}
        elif kind in ("content_block_start", "content_block_delta", "content_block_stop"):
            ev = raw
        if ev is not None:
            et = ev.get("type")
            if et == "content_block_start" and (ev.get("content_block") or {}).get("type") == "thinking":
                self._thinking_streaming = True
                return [{"type": "thinking_start"}]
            if et == "content_block_delta" and (ev.get("delta") or {}).get("type") == "thinking_delta":
                d = ev.get("delta") or {}
                # Some models (e.g. Opus 4.8) redact the thinking TEXT but still
                # stream estimated_tokens — use that as a live progress counter.
                return [{"type": "thinking_delta", "text": d.get("thinking", ""), "tokens": d.get("estimated_tokens")}]
            if et == "content_block_stop" and self._thinking_streaming:
                self._thinking_streaming = False
                return [{"type": "thinking_stop"}]
            return []

        if kind == "system":
            if raw.get("subtype") == "status" and raw.get("status") == "compacting":
                return [{"type": "compacting"}]
            return []

        if kind == "assistant":
            # Swallow the model's reply to the CLI's synthetic resume-prompt turn
            # (detected on the preceding user turn). Independent of what the model
            # actually says — it does NOT reliably echo the marker, so we can't
            # rely on text matching. Clear once the turn terminates.
            if self._suppressing_resume:
                if terminal_sr:
                    self._suppressing_resume = False
                return []
            # Tail-JSONL path: the CLI often does NOT emit the synthetic resume
            # user turn on stdout, so _suppressing_resume is never set via the
            # user-event handler. Detect the resume ack from its stop_reason
            # instead: the ack marker <<ccm-resume-ack>> is the stop sequence, so
            # any stop_sequence response while we're still awaiting the ack is
            # the resume turn. Set _suppressing_resume so the result event is
            # also suppressed (clearing the flag there, not here).
            if self._awaiting_resume_ack and sr == "stop_sequence":
                self._awaiting_resume_ack = False
                self._suppressing_resume = True
                return []
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
                    text = block.get("text", "")
                    # Drop the model's reply to the CLI's synthetic resume turn.
                    # It echoes RESUME_ACK_MARKER (we set the resume prompt to
                    # request exactly that); suppressing it hides the spurious
                    # "No response requested."-style turn after a Stop+resume.
                    if RESUME_ACK_MARKER in text:
                        continue
                    out.append({
                        "type": "assistant_text",
                        "id": msg_id,
                        "text": text,
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
            # CLI v2.1.170 sometimes ends a turn cleanly (assistant message with
            # stop_reason=end_turn) without emitting a follow-up `result` event.
            # Without that, the UI's spinner runs forever. Synthesize an end on
            # any terminal stop_reason — a duplicate generation_ended (if the
            # CLI does later emit result) is harmless; a missing one isn't.
            if terminal_sr:
                out.append({"type": "generation_ended", "subtype": "success"})
            return out

        if kind == "user":
            msg = raw.get("message", {}) or {}
            blocks = msg.get("content", []) or []
            # Detect the CLI's synthetic resume-prompt turn by its text (it carries
            # RESUME_ACK_MARKER because we set CLAUDE_CODE_RESUME_PROMPT). Arm
            # suppression so the model's reply to it is swallowed, and emit nothing
            # for the synthetic turn itself.
            for block in blocks:
                if block.get("type") == "text" and RESUME_ACK_MARKER in (block.get("text") or ""):
                    self._suppressing_resume = True
                    self._awaiting_resume_ack = False  # stdout path saw it; tail path won't need it
                    # Deliberate respawn (Stop / switch / refine) → stay silent.
                    # Unexpected respawn (crash / compaction / addon restart) →
                    # warn that in-progress work may be incomplete.
                    if self._suppress_next_resume_banner:
                        self._suppress_next_resume_banner = False
                        return []
                    return [{"type": "system_message", "text": RESUME_BANNER_TEXT}]
            # CLI echoes tool_result blocks back as user messages
            out = []
            for block in blocks:
                if block.get("type") == "tool_result":
                    out.append({
                        "type": "tool_result",
                        "tool_id": block.get("tool_use_id"),
                        "output": block.get("content", ""),
                        "error": bool(block.get("is_error")),
                    })
            return out

        if kind == "result":
            # Tail end of the suppressed resume turn — swallow its terminator too
            # so no stray generation_ended reaches the UI.
            if self._suppressing_resume:
                self._suppressing_resume = False
                self._awaiting_resume_ack = False
                return []
            return [{
                "type": "generation_ended",
                "subtype": raw.get("subtype", "success"),
                "duration_ms": raw.get("duration_ms"),
            }]

        # Unknown event kind. Log it once so we can spot new CLI event shapes
        # (e.g. `message_delta`) and extend handling. Still synthesize end on a
        # terminal stop_reason riding the unknown event.
        import logging
        logging.warning("ccm: unknown CLI event kind=%r keys=%s", kind, list(raw.keys())[:8])
        if terminal_sr:
            return [{"type": "generation_ended", "subtype": "success"}]
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

    async def _tail_jsonl(self) -> None:
        """Backup reader for events the CLI writes to its per-session jsonl
        but silently drops from stdout. Observed on CLI v2.1.170 in
        ``--output-format stream-json`` after Edit tool calls — the tool_result
        and the model's follow-up message land in the jsonl but never on the
        pipe, so the UI sees the spinner stuck on the Edit forever.

        Dedup is by event ``uuid`` (shared between stdout and jsonl events).
        Anything stdout has already emitted gets skipped; anything missing is
        translated and emitted, same shape as the stdout path.
        """
        import logging
        path = session_jsonl_path(self.id)
        # Start at end-of-file: history was rehydrated from this same jsonl
        # before _tail_jsonl was launched, so everything currently in the file
        # is already in self.history. Only new appends matter.
        pos = path.stat().st_size if path.exists() else 0
        while self.proc is not None and self.proc.returncode is None:
            await asyncio.sleep(2.0)
            try:
                if not path.exists():
                    continue
                size = path.stat().st_size
                if size < pos:
                    # File truncated/rotated. Reset.
                    pos = 0
                if size == pos:
                    continue
                with path.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError as e:
                logging.warning("ccm: jsonl tail read failed: %s", e)
                continue
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = raw.get("uuid")
                if u and u in self._seen_uuids:
                    continue
                if u:
                    self._seen_uuids.add(u)
                logging.warning("ccm: jsonl-tail recovered event uuid=%s type=%s", u, raw.get("type"))
                for evt in self._translate(raw):
                    await self._emit(evt)

    async def send_message(self, text: str, attachments: list[str] | None = None,
                           *, silent: bool = False) -> None:
        """Send a user-turn message to the CLI.

        silent=True skips the user_message echo and the auto-title — for
        server-orchestrated messages (plan handoff / refine) where the user
        already expressed intent via a button tap and shouldn't see a
        synthetic message in chat history.
        """
        import logging
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("Session not started")
        attachments = attachments or []
        logging.info("send_message: text_len=%d attachments=%r silent=%s", len(text), attachments, silent)

        # Local echo so SSE replay includes the user's own turn (unless silent).
        if not silent:
            await self._emit({
                "type": "user_message",
                "text": text,
                "attachments": attachments,
            })
        await self._emit({"type": "generation_started", "started_at": int(time.time() * 1000)})

        # Auto-title from first user message — only for real user turns.
        if not silent and self.title == "New chat" and text:
            self.title = _smart_title(text)

        content: list[dict[str, Any]] = []
        # After a user-initiated Stop, the resumed jsonl ends on an interrupted
        # turn, which makes the model open its next reply with a confused
        # "No response requested."-style acknowledgement. --append-system-prompt
        # can't fix this because --resume reuses the session's stored system
        # prompt. A separate instruction text block is the only channel
        # guaranteed to reach the model on resume. It carries INTERRUPT_NOTE_TAG
        # so persistence can skip it on reload (the user never sees it); the
        # real user text rides in its own block below, untouched.
        cli_text = text
        if self._interrupted_last_turn:
            self._interrupted_last_turn = False
            # Single block — the CLI merges multiple text blocks into one, which
            # broke the separate-block approach (note ran into the user's text
            # and persistence skipped the whole merged block on reload, losing
            # the message). Keep it one block with an end delimiter so reload
            # can strip exactly the note and preserve the real text.
            if text:
                cli_text = (
                    f"{INTERRUPT_NOTE_TAG} Your previous turn was interrupted by "
                    "the user pressing Stop. Do not mention or acknowledge the "
                    "interruption — just answer the message after the marker "
                    f"normally. {INTERRUPT_NOTE_END}\n{text}"
                )
        if cli_text:
            content.append({"type": "text", "text": cli_text})
        for path in attachments:
            block = _image_block(path)
            if block:
                content.append(block)
        payload = {"type": "user", "message": {"role": "user", "content": content}}
        payload_json = json.dumps(payload)
        # Diagnostic: tee the exact outbound payload to the stdout-debug file so
        # a "No response requested" repro shows precisely what the model
        # received (text blocks, injected note, resume state) alongside the
        # stdout responses already logged there. Drop image base64 to keep it
        # readable.
        try:
            with open("/config/ccm-stdout-debug.log", "a", buffering=1) as _dbg:
                safe = [b for b in content if b.get("type") != "image"]
                _dbg.write(
                    f"{time.time():.3f} >>> OUTBOUND session={self.id} "
                    f"interrupted_resume={'yes' if INTERRUPT_NOTE_TAG in payload_json else 'no'} "
                    f"content={json.dumps(safe)}\n"
                )
        except OSError:
            pass
        self.proc.stdin.write((payload_json + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _continue_after_plan_refine(self) -> None:
        """Respawn after a refine-plan handoff and ask Claude to seek clarification."""
        import logging
        try:
            await self.start()
            await self.send_message(
                "I'd like to refine the plan you just showed before any coding. "
                "Ask me one specific question about which part of the plan to "
                "change, add, or remove. Don't propose a new plan until I answer.",
                silent=True,
            )
        except Exception as e:
            logging.exception("plan refine continuation failed")
            await self._emit({"type": "error", "message": f"Plan refine failed: {e}"})
            await self._emit({"type": "generation_ended", "subtype": "error"})

    async def respond_to_permission(self, approve: bool) -> None:
        # Permission flow is enforced by the PreToolUse hook (security.py).
        # Reserved for a future in-app approval channel.
        return

    async def _watchdog(self) -> None:
        """Kill the generation if no event has been emitted for 20 minutes.
        Prevents a stuck CLI subprocess from running forever when the hook or
        API hangs with no way for the user to recover short of restarting."""
        try:
            while True:
                await asyncio.sleep(60)
                if self.proc is None or self.proc.returncode is not None:
                    return
                idle = time.time() - self._gen_last_activity
                if idle >= 20 * 60:
                    mins = int(idle // 60)
                    await self._emit({
                        "type": "system_message",
                        "text": f"Generation killed: no progress for {mins} minutes.",
                    })
                    await self.interrupt()
                    return
        except asyncio.CancelledError:
            pass

    async def interrupt(self) -> None:
        # Hard-stop: SIGTERM → SIGKILL. The `claude` binary is a wrapper that
        # spawns a node child; signalling only the wrapper leaves the child
        # alive. We try the process group first (start_new_session=True at
        # spawn) and fall back to a direct signal so a missing/wrong pgid
        # doesn't leave the proc up. We also skip SIGINT — the wrapper used
        # to trap it without forwarding, which is exactly why Stop looked
        # dead.
        if not self.proc or self.proc.returncode is not None:
            return
        self._interrupting = True
        # Set here (not only in the _read_stdout EOF handler) so it's race-free:
        # the next send_message is guaranteed to see it even if the stdout
        # reader hasn't processed EOF yet. Consumed on the first resumed message.
        self._interrupted_last_turn = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            self._signal_all(sig)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=1.0)
                return
            except asyncio.TimeoutError:
                continue
        self._interrupting = False

    def _signal_all(self, sig: int) -> None:
        """Send sig to the whole process group, then to the direct child as
        belt-and-suspenders. Either may fail; that's fine — only one needs to
        land for the proc to die."""
        if not self.proc:
            return
        pid = self.proc.pid
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self.proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

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
        # SIGTERM the whole tree, escalate to SIGKILL on timeout.
        if self.proc and self.proc.returncode is None:
            self._signal_all(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._signal_all(signal.SIGKILL)
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
                effort=meta.get("effort"),
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
                "effort": s.effort,
                "permission_mode": s.permission_mode,
            }
            for s in self.sessions.values()
        ])

    async def create(self, title: str | None = None, project_id: str | None = None,
                     model: str | None = None, permission_mode: str = "default") -> Session:
        if len(self.sessions) >= self.max_sessions:
            oldest = min(self.sessions.values(), key=lambda s: s.last_activity)
            await self.delete(oldest.id)
        sess = Session(
            id=str(uuid.uuid4()),
            title=title or "New chat",
            project_id=project_id,
            model=model,
            permission_mode=permission_mode,
        )
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
            t = " ".join(title.split())[:MAX_TITLE_LEN]
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
        # Silent shutdown: the kill should emit no banner at all. The next
        # message respawns the proc cleanly with the new flag.
        sess._silent_shutdown = True
        await sess.stop()
        self._persist()
        return sess

    async def set_effort(self, session_id: str, effort: str | None) -> Session | None:
        """Change a session's effort level. Kills the current CLI process so the next
        message respawns with --effort X (or unset)."""
        sess = self.sessions.get(session_id)
        if not sess:
            return None
        sess.effort = effort
        sess._silent_shutdown = True
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
        sess._silent_shutdown = True
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
                    "effort": s.effort,
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

    async def delete_all(self) -> int:
        """Stop and wipe every session. Returns the number deleted."""
        ids = list(self.sessions.keys())
        for sid in ids:
            await self.delete(sid)
        return len(ids)

    def touch(self, session_id: str) -> None:
        """Update last_activity + persist; called on any session activity."""
        sess = self.sessions.get(session_id)
        if sess:
            sess.last_activity = time.time()
            self._persist()
