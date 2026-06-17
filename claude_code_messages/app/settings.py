"""App-level settings (user-configurable, persisted to disk).

Currently:
    ha_mcp_enabled  bool   — whether to attach the HA MCP server to new sessions
    ha_url          str    — base URL of the user's HA instance
    ha_token        str    — long-lived access token (kept server-side, never echoed)

The token is stored on disk in plaintext but the file is chmod 0600. We mount
this directory from /config so it persists across addon updates.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/config/claude-config"))
SETTINGS_FILE = CONFIG_DIR / "messages-app-settings.json"

DEFAULTS: dict[str, Any] = {
    "ha_mcp_enabled": False,
    "ha_url": "",
    "ha_token": "",
    "ask_bash": True,
    "ask_webfetch": True,
    "webfetch_allowed_domains": [],
    # Per-command auto-allow list for Bash. Each entry is the bare command
    # name (e.g. "ls"). Auto-allow only fires when the Bash command starts
    # with one of these AND contains no compositional metacharacters (see
    # SHELL_METACHARS below). Off by default — opt-in per command.
    "bash_auto_allow": [],
    # HA companion-app notification on turn finish. Each entry is a full
    # service name like "notify.mobile_app_jons_iphone". Only fires when
    # the CCM tab is hidden at the moment `generation_ended` arrives.
    "notify_devices": [],
    # Audit log retention in days. 0 = keep forever.
    "log_retention_days": 90,
}


# Bare commands the user can opt into auto-allowing. The keys are the only
# tokens that can appear as the first word of an auto-allowed Bash command.
# `description` is shown next to the checkbox in Settings. Read-only,
# no network, no side effects, no privilege escalation.
SAFE_BASH_COMMANDS: dict[str, str] = {
    "ls": "List directory contents.",
    "pwd": "Print the current working directory.",
    "cat": "Print file contents to the terminal.",
    "head": "Print the first lines of a file.",
    "tail": "Print the last lines of a file.",
    "wc": "Count lines, words, and bytes in a file.",
    "file": "Identify a file's type.",
    "stat": "Print a file's size, dates, and permissions.",
    "du": "Print disk usage of a path.",
    "df": "Print free disk space.",
    "grep": "Search for a pattern inside files.",
    "find": "Find files by name, size, or date.",
    "which": "Look up where a command lives on PATH.",
    "date": "Print the current date and time.",
    "uname": "Print kernel / OS info.",
    "whoami": "Print the current user name.",
    "echo": "Print arguments back to stdout.",
    "git status": "Show changed / untracked files.",
    "git diff": "Show pending changes.",
    "git log": "Show commit history.",
    "git show": "Show a specific commit's contents.",
    "git branch": "List branches.",
}

# Compositional / redirection metacharacters that would let a user-supplied
# command escape the safe-prefix check. Presence of any of these → still ask.
# Newline included so a multi-line command can't sneak past either.
SHELL_METACHARS: tuple[str, ...] = (
    ";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r",
)


def is_safe_bash_command(command: str, allowed: list[str]) -> bool:
    """True iff the Bash command starts with one of the user-enabled safe
    commands AND contains no shell metacharacters that would let it do
    more than the bare command. Strips leading whitespace; ignores
    trailing arguments — they're either flags or paths."""
    if not command or not allowed:
        return False
    if any(meta in command for meta in SHELL_METACHARS):
        return False
    s = command.strip()
    # Tokenise loosely: split on whitespace, look at first 1-2 tokens.
    parts = s.split()
    if not parts:
        return False
    first = parts[0]
    two = " ".join(parts[:2]) if len(parts) >= 2 else ""
    for entry in allowed:
        if entry in SAFE_BASH_COMMANDS and (first == entry or two == entry):
            return True
    return False


def load() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return dict(DEFAULTS)
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save(d: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)
    try:
        SETTINGS_FILE.chmod(0o600)
    except OSError:
        pass


def public_view(d: dict[str, Any]) -> dict[str, Any]:
    """Client-safe view — never includes the raw token."""
    return {
        "ha_mcp_enabled": bool(d.get("ha_mcp_enabled")),
        "ha_url": d.get("ha_url", ""),
        "ha_token_set": bool(d.get("ha_token")),
        "ask_bash": bool(d.get("ask_bash")),
        "ask_webfetch": bool(d.get("ask_webfetch")),
        "webfetch_allowed_domains": list(d.get("webfetch_allowed_domains") or []),
        "bash_auto_allow": list(d.get("bash_auto_allow") or []),
        "safe_bash_commands": SAFE_BASH_COMMANDS,
        "notify_devices": list(d.get("notify_devices") or []),
        "log_retention_days": int(d.get("log_retention_days") or 90),
    }


def add_webfetch_domain(host: str) -> list[str]:
    """Persist a host into the WebFetch allowlist. Returns the new list."""
    if not host:
        return list(load().get("webfetch_allowed_domains") or [])
    cur = load()
    existing = list(cur.get("webfetch_allowed_domains") or [])
    h = host.lower().strip()
    if h and h not in existing:
        existing.append(h)
        cur["webfetch_allowed_domains"] = existing
        save(cur)
    return existing


MCP_CONFIG_PATH = Path("/tmp/messages-mcp-config.json")


def write_mcp_config_if_enabled() -> Path | None:
    """Materialize the per-launch --mcp-config file. Returns the path if
    HA MCP is enabled + configured; None otherwise. Caller passes the path
    to `claude --mcp-config <path>`."""
    s = load()
    if not s["ha_mcp_enabled"] or not s["ha_url"] or not s["ha_token"]:
        # Clear stale file so a re-launch after disabling doesn't keep using it.
        MCP_CONFIG_PATH.unlink(missing_ok=True)
        return None
    config = {
        "mcpServers": {
            "home-assistant": {
                "type": "stdio",
                "command": "uvx",
                "args": ["--index-strategy", "unsafe-best-match", "ha-mcp@3.5.1"],
                "env": {
                    "HOMEASSISTANT_URL": s["ha_url"],
                    "HOMEASSISTANT_TOKEN": s["ha_token"],
                },
            }
        }
    }
    MCP_CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    try:
        MCP_CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return MCP_CONFIG_PATH
