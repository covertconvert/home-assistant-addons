"""OAuth flow wrapper for the Claude CLI.

`claude setup-token` is a TTY-only command: without a real terminal it
prints nothing and waits forever. We spawn it under a pseudoterminal so it
sees a TTY, set the winsize wide so the OAuth URL doesn't line-wrap, then
scrape stdout for the URL and (after the user submits a code) the token.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import struct
import subprocess
import termios
from pathlib import Path

import pyte

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/config/claude-config"))
TOKEN_PATH = CONFIG_DIR / "oauth_token"

PTY_ROWS = 40
PTY_COLS = 500

URL_RE = re.compile(r"https?://claude\.com/cai/oauth/[^\s\"<>]+")
TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def is_authed() -> bool:
    return TOKEN_PATH.exists() and TOKEN_PATH.stat().st_size > 0


def load_token() -> str | None:
    if not is_authed():
        return None
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_token(token: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token.strip() + "\n", encoding="utf-8")
    TOKEN_PATH.chmod(0o600)


def _set_winsize(fd: int, rows: int = PTY_ROWS, cols: int = PTY_COLS) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class AuthFlow:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.master_fd: int | None = None
        self.oauth_url: str | None = None
        self._url_ready = asyncio.Event()
        self._done = asyncio.Event()
        self.token: str | None = None
        self.error: str | None = None
        self._buffer = ""
        self._screen = pyte.Screen(PTY_COLS, PTY_ROWS)
        self._stream = pyte.ByteStream(self._screen)

    async def start(self) -> str:
        loop = asyncio.get_running_loop()
        master_fd, slave_fd = pty.openpty()
        _set_winsize(master_fd)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.master_fd = master_fd

        self.proc = subprocess.Popen(
            [CLAUDE_BIN, "setup-token"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env={**os.environ, "TERM": "xterm-256color", "FORCE_COLOR": "0"},
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        loop.add_reader(master_fd, self._on_pty_data)

        try:
            await asyncio.wait_for(self._url_ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            self._terminate()
            preview = self._buffer[-800:] if self._buffer else "(empty)"
            raise RuntimeError(f"timeout waiting for URL; last output: {preview!r}")
        assert self.oauth_url
        return self.oauth_url

    async def submit_code(self, code: str) -> str:
        if self.master_fd is None or not self.proc:
            raise RuntimeError("No auth flow in progress")
        # CLI enables bracketed paste (\x1b[?2004h). Real terminals wrap
        # pasted content with \x1b[200~ ... \x1b[201~. Send the code that way
        # so the React/ink-based prompt actually accepts it, then \r for Enter.
        payload = f"\x1b[200~{code.strip()}\x1b[201~\r".encode("utf-8")
        os.write(self.master_fd, payload)
        try:
            await asyncio.wait_for(self._done.wait(), timeout=120)
        except asyncio.TimeoutError:
            self._dump_debug("timeout")
            self._terminate()
            preview = self._buffer[-800:] if self._buffer else "(empty)"
            raise RuntimeError(f"timeout waiting for token; last output: {preview!r}")
        if self.error or not self.token:
            self._dump_debug("error")
            raise RuntimeError(self.error or "No token received")
        save_token(self.token)
        return self.token

    def _dump_debug(self, reason: str) -> None:
        """Write raw buffer + rendered pyte screen to /config for inspection."""
        try:
            path = Path("/config/claude-auth-debug.log")
            with path.open("w", encoding="utf-8") as f:
                f.write(f"=== reason: {reason} ===\n\n")
                f.write("=== RAW BUFFER ===\n")
                f.write(self._buffer)
                f.write("\n\n=== PYTE RENDERED SCREEN ===\n")
                for i, line in enumerate(self._screen.display):
                    f.write(f"{i:3d}: {line!r}\n")
            path.chmod(0o644)
        except OSError:
            pass

    def _on_pty_data(self) -> None:
        if self.master_fd is None:
            return
        try:
            data = os.read(self.master_fd, 4096)
        except (BlockingIOError, OSError):
            return
        if not data:
            return
        self._buffer += data.decode("utf-8", errors="replace")
        # Render the byte stream as a real terminal would. Cursor-position
        # escapes paint chars at the right column instead of leaving them
        # scrambled in our raw buffer.
        self._stream.feed(data)
        rendered = "\n".join(self._screen.display)

        if not self.oauth_url:
            # URL may still wrap at our winsize boundary; whitespace-flatten
            # the rendered screen and search.
            flat = re.sub(r"\s+", "", rendered)
            m = URL_RE.search(flat)
            if m:
                self.oauth_url = m.group(0)
                self._url_ready.set()

        if not self.token:
            # Token is a single visible string on screen — search the rendered
            # display directly, then validate it looks like a full token.
            m = TOKEN_RE.search(rendered.replace(" ", ""))
            if m and len(m.group(0)) >= 100:
                self.token = m.group(0)
                self._done.set()

    def _terminate(self) -> None:
        loop = asyncio.get_event_loop()
        if self.master_fd is not None:
            try:
                loop.remove_reader(self.master_fd)
            except (ValueError, OSError):
                pass
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except ProcessLookupError:
                pass
