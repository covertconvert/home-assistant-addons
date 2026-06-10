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
}


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
