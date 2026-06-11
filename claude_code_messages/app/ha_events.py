"""HA WebSocket listener for mobile_app_notification_action events.

Maintains a long-lived connection to Home Assistant's /api/websocket,
subscribes to the `mobile_app_notification_action` event type, and routes
any action string starting with `ccm_` to a registered async handler.

Used to let the user tap Allow / Reject directly on a push notification
without opening the chat UI. Reconnects with exponential backoff on
disconnect; silently idles when HA is unconfigured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from collections.abc import Awaitable, Callable

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]

_handler: Callable[[str], Awaitable[None]] | None = None


def set_action_handler(fn: Callable[[str], Awaitable[None]] | None) -> None:
    """Server installs the dispatcher here; called for each ccm_* action."""
    global _handler
    _handler = fn


def _ws_url(ha_url: str) -> str:
    parsed = urllib.parse.urlparse(ha_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/api/websocket"


async def _one_connection(ha_url: str, token: str) -> None:
    url = _ws_url(ha_url)
    async with websockets.connect(url, max_size=2**20, open_timeout=10) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected greeting from HA WS: {hello!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"HA WS auth failed: {auth_resp!r}")
        await ws.send(json.dumps({
            "id": 1,
            "type": "subscribe_events",
            "event_type": "mobile_app_notification_action",
        }))
        logging.info("ha-events: connected and subscribed")
        while True:
            raw = await ws.recv()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "event":
                continue
            data = (msg.get("event") or {}).get("data") or {}
            action = data.get("action")
            if not isinstance(action, str) or not action.startswith("ccm_"):
                continue
            if _handler is None:
                continue
            try:
                await _handler(action)
            except Exception:
                logging.exception("ha-events: handler raised for %s", action)


async def run_listener(get_settings: Callable[[], dict]) -> None:
    """Long-running background task. Re-reads settings on each idle cycle so
    a URL/token change is picked up without restarting the addon."""
    if websockets is None:
        logging.warning("ha-events: `websockets` not installed — actionable notifications disabled")
        return
    backoff = 5
    while True:
        s = get_settings()
        url = (s.get("ha_url") or "").strip()
        token = (s.get("ha_token") or "").strip()
        devices = list(s.get("notify_devices") or [])
        if not url or not token or not devices:
            await asyncio.sleep(30)
            backoff = 5
            continue
        try:
            await _one_connection(url, token)
            backoff = 5
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning("ha-events: connection failed (%s); retrying in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
