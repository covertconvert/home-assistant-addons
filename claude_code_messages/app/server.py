"""FastAPI backend for Claude Code Messages.

Endpoints:
    GET    /api/sessions                   list sessions
    POST   /api/sessions                   create a session
    GET    /api/sessions/{id}/stream       SSE stream of events
    POST   /api/sessions/{id}/message      send a user message
    POST   /api/sessions/{id}/interrupt    cancel current generation
    POST   /api/sessions/{id}/permission   respond approve/reject
    POST   /api/sessions/{id}/upload       upload an image attachment
    DELETE /api/sessions/{id}              kill a session
    GET    /healthz                        liveness
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiofiles
import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware

import ha_events
import plans as plans_store
import projects as projects_store
from audit import log as audit_log
from audit import log_info as audit_log_info
from audit import trim as audit_trim
from auth import AuthFlow, is_authed, save_token
from persistence import read_history, session_cost, session_latest_input_tokens
from session import Session, SessionManager, set_generation_ended_hook
from settings import add_webfetch_domain
from settings import load as load_settings
from settings import public_view as settings_public_view
from settings import save as save_settings

UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = 10
STATIC_DIR = Path(__file__).resolve().parent / "static"

# ── HA bearer-token auth (for direct API access from iOS / external clients) ──
# Requests arriving via HA ingress are already authenticated by HA's proxy layer
# and carry no Authorization header — we let those through untouched.
# Requests with Authorization: Bearer <ha-token> (e.g. from the iOS app via
# Nabu Casa) are validated once against the HA REST API and cached for 60 s.
_HA_CORE_URL = "http://homeassistant:8123/api/"
_TOKEN_CACHE_TTL = 60
_token_cache: dict[str, tuple[bool, float]] = {}
_EXEMPT_PATHS = {"/", "/healthz"}
_EXEMPT_PREFIXES = ("/static/", "/api/auth/")


async def _validate_ha_token(token: str) -> bool:
    now = time.monotonic()
    if token in _token_cache:
        valid, expires = _token_cache[token]
        if now < expires:
            return valid
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _HA_CORE_URL, headers={"Authorization": f"Bearer {token}"}
            )
        valid = resp.status_code == 200
    except Exception:
        valid = False
    _token_cache[token] = (valid, now + _TOKEN_CACHE_TTL)
    return valid


class _HABearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _EXEMPT_PATHS or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            # No Bearer header → ingress request, already authenticated by HA.
            return await call_next(request)
        if not await _validate_ha_token(auth[7:]):
            return JSONResponse({"detail": "Invalid or expired HA token"}, status_code=401)
        return await call_next(request)


app = FastAPI(title="Claude Code Messages", version="0.1.0")
app.add_middleware(_HABearerAuthMiddleware)
manager = SessionManager(max_sessions=int(os.environ.get("MAX_SESSIONS", "20")))
auth_flow: AuthFlow | None = None
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# request_id -> Future that the public /permission endpoint resolves with the
# user's decision string ("reject" | "allow_once" | "allow_domain"). The hook's
# HTTP call awaits it.
pending_permissions: dict[str, asyncio.Future[str]] = {}

# Per-session lock so parallel tool_use blocks (Claude often emits Bash + WebFetch
# together) surface as serialized prompts instead of overlapping cards. The
# second hook's HTTP request blocks at acquire() until the first resolves.
session_permission_locks: dict[str, asyncio.Lock] = {}

# session_id -> set of request_ids currently pending for that session. Used by
# the interrupt handler to cancel futures and unblock the lock on Stop.
session_pending_request_ids: dict[str, set[str]] = {}


class CodeBody(BaseModel):
    code: str


class TokenBody(BaseModel):
    token: str


@app.get("/api/auth/status")
async def auth_status() -> dict[str, bool]:
    return {"authed": is_authed()}


@app.post("/api/auth/start")
async def auth_start() -> dict[str, str]:
    import logging
    import traceback
    global auth_flow
    auth_flow = AuthFlow()
    try:
        url = await auth_flow.start()
    except Exception as e:
        logging.error("auth_start failed: %s\n%s", e, traceback.format_exc())
        if auth_flow:
            logging.error("auth buffer: %r", auth_flow._buffer[-1000:])
        auth_flow = None
        raise HTTPException(status_code=500, detail=str(e))
    audit_log("auth", "auth_started", {})
    return {"url": url}


@app.post("/api/auth/complete")
async def auth_complete(body: CodeBody) -> dict[str, bool]:
    import logging
    import traceback
    global auth_flow
    if not auth_flow:
        raise HTTPException(status_code=400, detail="No auth flow in progress")
    try:
        await auth_flow.submit_code(body.code)
    except Exception as e:
        logging.error("auth_complete failed: %s\n%s", e, traceback.format_exc())
        if auth_flow:
            logging.error("auth buffer after submit: %r", auth_flow._buffer[-1500:])
        audit_log("auth", "auth_failed", {"error": str(e)})
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        auth_flow = None
    audit_log("auth", "auth_completed", {})
    return {"ok": True}


@app.post("/api/auth/token")
async def auth_token(body: TokenBody) -> dict[str, bool]:
    token = body.token.strip()
    if not token.startswith("sk-ant-"):
        raise HTTPException(status_code=400, detail="Token must start with sk-ant-")
    if len(token) < 50:
        raise HTTPException(status_code=400, detail="Token looks too short")
    save_token(token)
    audit_log("auth", "auth_manual_token", {"len": len(token)})
    return {"ok": True}


@app.get("/")
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css_v = int((STATIC_DIR / "styles.css").stat().st_mtime)
    js_v = int((STATIC_DIR / "app.js").stat().st_mtime)
    html = html.replace("static/styles.css", f"static/styles.css?v={css_v}")
    html = html.replace("static/app.js", f"static/app.js?v={js_v}")
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/uploads/{name}")
async def get_upload(name: str) -> FileResponse:
    # Prevent traversal
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="bad name")
    path = UPLOAD_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


class CreateSessionBody(BaseModel):
    title: str | None = None
    project_id: str | None = None


class UpdateSessionBody(BaseModel):
    title: str | None = None
    # Use the literal string "__unset__" to signal "leave unchanged" — None is
    # a valid value meaning "unassign from any project".
    project_id: str | None = "__unset__"


class MessageBody(BaseModel):
    text: str
    attachments: list[str] = []


class PermissionBody(BaseModel):
    # Decisions: "reject" | "allow_once" | "allow_domain".
    # `approved` retained for legacy callers that only know yes/no.
    decision: str | None = None
    approved: bool | None = None
    request_id: str | None = None


class InternalPermissionBody(BaseModel):
    session_id: str
    tool_name: str
    tool_input: dict[str, Any]


class ProjectBody(BaseModel):
    name: str


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "sessions": len(manager.sessions)}


class SettingsBody(BaseModel):
    ha_mcp_enabled: bool
    ha_url: str
    # Empty string = "keep existing token" so the client never needs to read it
    # back. Pass a value to update; pass null/empty to leave unchanged.
    ha_token: str | None = None
    ask_bash: bool = True
    ask_webfetch: bool = True
    # Allows the client to clear the entire allowlist; per-domain adds happen
    # via the permission flow itself.
    webfetch_allowed_domains: list[str] | None = None
    # Per-command Bash auto-allow opt-ins. Each entry must appear in
    # settings.SAFE_BASH_COMMANDS; unknown entries are silently dropped.
    bash_auto_allow: list[str] | None = None
    # Full HA notify.* service names (e.g. notify.mobile_app_jons_iphone).
    notify_devices: list[str] | None = None
    # Audit log retention in days. 0 = keep forever.
    log_retention_days: int | None = None
    destructive_protected_integrations: list[str] | None = None
    destructive_entity_threshold: int | None = None


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    result = settings_public_view(load_settings())
    result["log_info"] = await asyncio.to_thread(audit_log_info)
    return result


@app.post("/api/settings")
async def update_settings(body: SettingsBody) -> dict[str, Any]:
    cur = load_settings()
    new = {
        **cur,
        "ha_mcp_enabled": body.ha_mcp_enabled,
        "ha_url": body.ha_url.strip(),
        "ask_bash": body.ask_bash,
        "ask_webfetch": body.ask_webfetch,
    }
    if body.ha_token:
        new["ha_token"] = body.ha_token.strip()
    if body.webfetch_allowed_domains is not None:
        new["webfetch_allowed_domains"] = [
            h.lower().strip() for h in body.webfetch_allowed_domains if h and h.strip()
        ]
    if body.bash_auto_allow is not None:
        from settings import SAFE_BASH_COMMANDS as _SAFE
        new["bash_auto_allow"] = [c for c in body.bash_auto_allow if c in _SAFE]
    if body.notify_devices is not None:
        new["notify_devices"] = [
            s.strip() for s in body.notify_devices
            if isinstance(s, str) and s.strip().startswith("notify.")
        ]
    if body.log_retention_days is not None:
        new["log_retention_days"] = max(0, body.log_retention_days)
    if body.destructive_protected_integrations is not None:
        new["destructive_protected_integrations"] = [
            s.strip().lower() for s in body.destructive_protected_integrations
            if isinstance(s, str) and s.strip()
        ]
    if body.destructive_entity_threshold is not None:
        new["destructive_entity_threshold"] = max(1, body.destructive_entity_threshold)
    save_settings(new)
    retention = new.get("log_retention_days", 90)
    if retention:
        asyncio.create_task(asyncio.to_thread(audit_trim, retention))
    audit_log("settings", "settings_updated", {
        "ha_mcp_enabled": new["ha_mcp_enabled"],
        "ha_url_set": bool(new["ha_url"]),
        "ha_token_set": bool(new["ha_token"]),
        "ask_bash": new["ask_bash"],
        "ask_webfetch": new["ask_webfetch"],
        "webfetch_allowed_count": len(new.get("webfetch_allowed_domains") or []),
        "bash_auto_allow": list(new.get("bash_auto_allow") or []),
        "notify_devices_count": len(new.get("notify_devices") or []),
    })
    return settings_public_view(new)


def _ha_request(url: str, token: str, *, method: str = "GET",
                body: dict | None = None, timeout: float = 8.0) -> Any:
    """Synchronous HA REST call. Caller wraps in asyncio.to_thread.

    Uses urllib so we don't pull in a third-party HTTP client just for this.
    Returns parsed JSON on 2xx, raises on anything else."""
    import json as _json
    import urllib.request
    import urllib.error
    payload = _json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url.rstrip("/"),
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        try:
            return _json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw.decode("utf-8", errors="replace")


_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}
_DEFAULT_CONTEXT_WINDOW = 200_000


def _fetch_config_entries(url: str, token: str) -> list[dict]:
    """Fetch all config entries from HA. Returns [] on any error."""
    try:
        result = _ha_request(f"{url}/api/config/config_entries/entry", token, timeout=5.0)
        return result if isinstance(result, list) else []
    except Exception:
        return []


def _fetch_entity_count_for_entry(url: str, token: str, entry_id: str) -> int:
    """Count entity registry entries belonging to a config entry. Returns 0 on error."""
    for path in ("/api/config/entity_registry/list", "/api/config/entity_registry"):
        try:
            entities = _ha_request(f"{url}{path}", token, timeout=5.0)
            if isinstance(entities, list):
                return sum(1 for e in entities if isinstance(e, dict) and e.get("config_entry_id") == entry_id)
        except Exception:
            continue
    return 0


_DESTRUCTIVE_SERVICE_WORDS = frozenset({"remove", "delete", "clear", "purge", "wipe", "reset"})
_UUID_RE = re.compile(r'^[0-9a-f]{32}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def _is_destructive_service(service: str) -> bool:
    return any(w in service.lower() for w in _DESTRUCTIVE_SERVICE_WORDS)


def _extract_entry_id(data: dict) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("entry_id", "config_entry_id"):
        val = data.get(key)
        if isinstance(val, str) and _UUID_RE.match(val.replace("-", "")):
            return val
    return None


async def _enrich_destructive_call(tool_name: str, tool_input: dict) -> dict:
    """For ha_call_service calls that look destructive, resolve the config entry
    and compute consequence text + type-to-confirm name. Returns {} if not applicable."""
    if tool_name != "mcp__home-assistant__ha_call_service":
        return {}
    service = (tool_input.get("service") or "").strip()
    if not _is_destructive_service(service):
        return {}
    data = tool_input.get("service_data") or tool_input.get("data") or {}
    entry_id = _extract_entry_id(data)
    if not entry_id:
        return {"destructive": True}

    s = load_settings()
    url = (s.get("ha_url") or "").strip()
    token = (s.get("ha_token") or "").strip()
    if not url or not token:
        return {"destructive": True}

    try:
        entries = await asyncio.to_thread(_fetch_config_entries, url, token)
        entry = next((e for e in entries if e.get("entry_id") == entry_id), None)
        if not entry:
            return {"destructive": True}

        entry_title = entry.get("title") or entry.get("domain") or entry_id
        entry_domain = (entry.get("domain") or "").lower()

        entity_count = await asyncio.to_thread(_fetch_entity_count_for_entry, url, token, entry_id)

        consequence = f"This will remove the {entry_title} integration"
        if entity_count > 0:
            noun = "entity" if entity_count == 1 else "entities"
            consequence += f" and {entity_count} associated {noun}"

        protected = set(s.get("destructive_protected_integrations") or [])
        threshold = int(s.get("destructive_entity_threshold") or 25)
        needs_type_confirm = (entry_domain in protected) or (entity_count >= threshold)

        result: dict = {
            "destructive": True,
            "consequence": consequence,
            "entry_title": entry_title,
        }
        if needs_type_confirm:
            result["confirm_name"] = entry_title
        return result
    except Exception:
        return {"destructive": True}


class HaTestBody(BaseModel):
    # Both optional — empty values fall back to saved settings, so the client
    # can validate just-typed credentials without first persisting them.
    url: str | None = None
    token: str | None = None


@app.post("/api/ha/test_token")
async def test_ha_token(body: HaTestBody) -> dict[str, Any]:
    """Validate an HA (url, token) pair by GET-ing /api/.

    Returns {ok: true} on success, or {ok: false, error: "..."} with a
    short human-readable reason. Never raises — the client treats this as
    informational, not blocking."""
    s = load_settings()
    url = (body.url if body.url is not None else s.get("ha_url") or "").strip().rstrip("/")
    token = (body.token or "").strip() or (s.get("ha_token") or "").strip()
    if not url:
        return {"ok": False, "error": "Home Assistant URL is empty."}
    if not token:
        return {"ok": False, "error": "No Long-Lived Access Token saved."}
    import urllib.error
    try:
        await asyncio.to_thread(_ha_request, f"{url}/api/", token, timeout=5.0)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "error": "Token rejected (401). Generate a new Long-Lived Access Token in your HA profile."}
        return {"ok": False, "error": f"HA returned HTTP {e.code}."}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return {"ok": False, "error": f"Couldn't reach HA: {reason}"}
    except Exception as e:
        return {"ok": False, "error": f"Validation failed: {e}"}
    return {"ok": True}


@app.get("/api/ha/notify_targets")
async def list_notify_targets() -> list[dict[str, str]]:
    """List every notify.mobile_app_* service the configured HA knows about.
    Reuses the HA URL + token saved for the MCP integration. Returns
    [] (not an error) if HA isn't configured yet."""
    s = load_settings()
    url, token = (s.get("ha_url") or "").strip(), (s.get("ha_token") or "").strip()
    if not url or not token:
        return []
    try:
        services = await asyncio.to_thread(
            _ha_request, f"{url}/api/services", token,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HA lookup failed: {e}") from e
    out: list[dict[str, str]] = []
    if isinstance(services, list):
        for entry in services:
            if not isinstance(entry, dict) or entry.get("domain") != "notify":
                continue
            for svc_name in (entry.get("services") or {}).keys():
                if isinstance(svc_name, str) and svc_name.startswith("mobile_app_"):
                    # Pretty label from mobile_app_jons_iphone → "Jons Iphone"
                    pretty = svc_name[len("mobile_app_"):].replace("_", " ").title()
                    out.append({"service": f"notify.{svc_name}", "label": pretty})
    out.sort(key=lambda x: x["label"])
    return out


@app.get("/api/ha/config_entries")
async def list_ha_config_entries() -> list[dict]:
    """List config entries for the destructive-ops settings UI (validation + picker)."""
    s = load_settings()
    url = (s.get("ha_url") or "").strip()
    token = (s.get("ha_token") or "").strip()
    if not url or not token:
        return []
    try:
        entries = await asyncio.to_thread(_fetch_config_entries, url, token)
        return [
            {"entry_id": e.get("entry_id"), "title": e.get("title"), "domain": e.get("domain")}
            for e in entries if isinstance(e, dict) and e.get("entry_id")
        ]
    except Exception:
        return []


class TurnFinishedBody(BaseModel):
    session_id: str
    title: str | None = None


# Sliding window — if the client said it was focused within this many seconds,
# we skip the push because the user is already looking at the reply on-screen.
FOCUS_FRESHNESS_SECONDS = 30


def _last_assistant_preview(sess: Session, max_len: int = 140) -> str:
    """Most recent assistant_text on the session, truncated. Used as the body
    of the turn-finished push so the user sees the reply on the lock screen."""
    for evt in reversed(sess.history):
        if evt.get("type") == "assistant_text":
            text = (evt.get("text") or "").strip()
            if text:
                return text if len(text) <= max_len else text[:max_len - 1] + "…"
    return ""


# Panel URL that HA's frontend uses to open this addon's iframe — reported
# by the frontend on load via /api/panel_url because (a) repository addons
# get a hash-prefixed slug not knowable from config.yaml, and (b) the panel
# URL format itself can vary across HA versions. Frontend reads
# `window.parent.location.pathname` and POSTs it here; backend uses
# whatever HA actually uses to navigate to the addon.
_panel_url_cache: str | None = None


class PanelUrlBody(BaseModel):
    path: str


@app.post("/api/panel_url")
async def report_panel_url(body: PanelUrlBody) -> dict[str, bool]:
    """Frontend reports the HA panel URL it's loaded inside. We cache the
    path (without query/hash) so notification deep-links open the same
    panel rather than a 401/404'ing slug-based guess."""
    global _panel_url_cache
    path = (body.path or "").strip()
    if not path.startswith("/"):
        return {"ok": False}
    # Strip query/hash; we append our own ?session=… per notification.
    for sep in ("?", "#"):
        i = path.find(sep)
        if i != -1:
            path = path[:i]
    _panel_url_cache = path
    return {"ok": True}


def _session_deep_link(session_id: str) -> str:
    """URL the mobile_app should open on notification tap. Prefers the
    panel URL the frontend reported (matches whatever HA actually serves);
    falls back to a slug-based guess if no frontend has connected yet."""
    base = _panel_url_cache or "/hassio/ingress/claude_code_messages"
    return f"{base}?session={session_id}"


async def _send_notification(payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one payload to every configured notify.mobile_app_* device.
    Returns {sent, failures, devices, skipped}. Callers add the audit log
    line themselves so the action name is meaningful."""
    s = load_settings()
    devices = list(s.get("notify_devices") or [])
    url, token = (s.get("ha_url") or "").strip(), (s.get("ha_token") or "").strip()
    if not devices or not url or not token:
        return {"sent": 0, "skipped": True, "devices": 0, "failures": []}
    sent, failures = 0, []
    for dev in devices:
        if not isinstance(dev, str) or not dev.startswith("notify."):
            continue
        svc = dev[len("notify."):]
        try:
            await asyncio.to_thread(
                _ha_request,
                f"{url}/api/services/notify/{svc}",
                token,
                method="POST",
                body=payload,
            )
            sent += 1
        except Exception as e:
            failures.append({"device": dev, "error": str(e)})
    return {"sent": sent, "failures": failures, "devices": len(devices)}


async def _send_turn_finished_push(session_id: str, title: str | None) -> dict[str, Any]:
    """Fire a push to every configured notify.mobile_app_* device via HA.
    Called both by the server-side generation_ended hook (the reliable path —
    fires even when the client's SSE has been suspended) and by the legacy
    client-driven endpoint kept as belt-and-braces."""
    sess = manager.get(session_id)
    chat_title = (title or "").strip()
    if chat_title.lower() in ("", "new chat", "untitled"):
        chat_title = ""
    notification_title = (
        f"Claude — {chat_title}" if chat_title else "Claude reply ready"
    )
    preview = _last_assistant_preview(sess) if sess else ""
    notification_body = preview or (
        f"Reply ready in {chat_title}" if chat_title else "Your reply is ready"
    )
    payload: dict[str, Any] = {
        "title": notification_title,
        "message": notification_body,
        "data": {"url": _session_deep_link(session_id)},
    }
    result = await _send_notification(payload)
    audit_log(session_id, "notify_turn_finished", {
        "sent": result.get("sent"),
        "devices": result.get("devices"),
        "failures": len(result.get("failures") or []),
    })
    return result


async def _send_permission_push(sess: Session, req_id: str, tool: str, description: str) -> dict[str, Any]:
    """Fire an actionable push for a permission card so the user can tap
    Allow/Reject without unlocking the app. No-op when the user is actively
    focused — the in-app card is already on screen."""
    if time.time() - sess.last_focused_at < FOCUS_FRESHNESS_SECONDS:
        return {"sent": 0, "skipped": True}
    short_tool = tool.split("__")[-1] if tool.startswith("mcp__") else tool
    body_preview = (description or "").strip()
    if len(body_preview) > 140:
        body_preview = body_preview[:139] + "…"
    payload: dict[str, Any] = {
        "title": f"Claude needs permission: {short_tool}",
        "message": body_preview or "Tap to review",
        "data": {
            "url": _session_deep_link(sess.id),
            "actions": [
                {"action": f"ccm_allow_{req_id}", "title": "Allow"},
                {"action": f"ccm_reject_{req_id}", "title": "Reject"},
            ],
            "tag": f"ccm-perm-{req_id}",
        },
    }
    result = await _send_notification(payload)
    audit_log(sess.id, "notify_permission_request", {
        "request_id": req_id,
        "tool": tool,
        "sent": result.get("sent"),
        "failures": len(result.get("failures") or []),
    })
    return result


async def _on_notification_action(action: str) -> None:
    """Dispatcher for mobile_app_notification_action events that begin with
    `ccm_`. Format: `ccm_<allow|reject>_<request_id>`. Resolves the matching
    pending permission future, then emits permission_resolved on the owning
    session so any open client UI hides the card."""
    parts = action.split("_", 2)
    if len(parts) != 3 or parts[0] != "ccm":
        return
    verb, req_id = parts[1], parts[2]
    if verb == "allow":
        resolve_with = "allow_once"
    elif verb == "reject":
        resolve_with = "reject"
    else:
        return
    fut = pending_permissions.pop(req_id, None)
    if fut and not fut.done():
        fut.set_result(resolve_with)
    target: Session | None = None
    for s in manager.sessions.values():
        for evt in reversed(s.history):
            if evt.get("type") == "permission_request" and evt.get("id") == req_id:
                target = s
                break
        if target:
            break
    if target is None:
        return
    await target._emit({
        "type": "permission_resolved",
        "id": req_id,
        "decision": resolve_with,
        "via": "notification",
    })
    audit_log(target.id, "permission_response", {
        "decision": resolve_with,
        "request_id": req_id,
        "via": "notification",
    })


ha_events.set_action_handler(_on_notification_action)


@app.on_event("startup")
async def _start_ha_listener() -> None:
    asyncio.create_task(ha_events.run_listener(load_settings))


@app.on_event("startup")
async def _trim_audit_log_on_start() -> None:
    retention = load_settings().get("log_retention_days", 90)
    if retention:
        await asyncio.to_thread(audit_trim, retention)


async def _on_generation_ended(sess: Session, evt: dict) -> None:
    """Server-side hook fired when any session emits a generation_ended event."""
    if evt.get("subtype") == "interrupted":
        return

    # Emit context usage so the frontend can warn before context fills up.
    # Fires regardless of focus state — the user needs this signal even when active.
    try:
        latest_input = await asyncio.to_thread(session_latest_input_tokens, sess.id)
        if latest_input > 0:
            window = _MODEL_CONTEXT_WINDOWS.get(sess.model or "", _DEFAULT_CONTEXT_WINDOW)
            await sess._emit({
                "type": "context_usage",
                "input_tokens": latest_input,
                "context_window": window,
                "pct": round(latest_input / window, 4),
            })
    except Exception:
        pass

    if time.time() - sess.last_focused_at < FOCUS_FRESHNESS_SECONDS:
        return
    try:
        await _send_turn_finished_push(sess.id, sess.title)
    except Exception:
        import logging
        logging.exception("turn-finished push failed for session %s", sess.id)


set_generation_ended_hook(_on_generation_ended)


@app.post("/api/notify/turn_finished")
async def notify_turn_finished(body: TurnFinishedBody) -> dict[str, Any]:
    """Legacy client-driven path — fires when the client receives
    generation_ended while the tab is hidden. Server-side hook covers the
    case where the SSE pipe died first; this endpoint stays as a fallback."""
    sess = manager.get(body.session_id)
    title = body.title or (sess.title if sess else None)
    return await _send_turn_finished_push(body.session_id, title)


class FocusBody(BaseModel):
    focused: bool


@app.post("/api/sessions/{session_id}/focus")
async def session_focus(session_id: str, body: FocusBody) -> dict[str, Any]:
    """Heartbeat from the client. focused=true while the user is looking at
    this session; focused=false on visibilitychange→hidden or pagehide."""
    sess = manager.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    sess.last_focused_at = time.time() if body.focused else 0.0
    return {"ok": True}


@app.get("/api/search")
async def search_sessions(q: str = "") -> dict[str, Any]:
    """Substring search across session titles and message bodies.
    Tool inputs/outputs are intentionally not searched — file dumps and grep
    results would dominate the rankings. Body matches scan user_message and
    assistant_text only. Returns up to 50 sessions; title hits rank first,
    then by recency. Min query length 2 chars to avoid scanning the whole
    history on every keystroke."""
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    ql = q.lower()
    qlen = len(q)
    results: list[dict[str, Any]] = []
    for sess in list(manager.sessions.values()):
        title_match = ql in (sess.title or "").lower()
        body_matches: list[dict[str, str]] = []
        try:
            for evt in read_history(sess.id):
                if evt.get("type") not in ("user_message", "assistant_text"):
                    continue
                txt = evt.get("text") or ""
                idx = txt.lower().find(ql)
                if idx == -1:
                    continue
                start = max(0, idx - 40)
                end = min(len(txt), idx + qlen + 40)
                body_matches.append({
                    "role": "user" if evt["type"] == "user_message" else "assistant",
                    "before": txt[start:idx],
                    "match": txt[idx:idx + qlen],
                    "after": txt[idx + qlen:end],
                })
                if len(body_matches) >= 2:
                    break
        except Exception:
            continue
        if not title_match and not body_matches:
            continue
        results.append({
            "session_id": sess.id,
            "title": sess.title,
            "project_id": sess.project_id,
            "last_activity": sess.last_activity,
            "title_match": title_match,
            "matches": body_matches,
        })
    results.sort(key=lambda r: (not r["title_match"], -r["last_activity"]))
    return {"results": results[:50]}


@app.delete("/api/data/all")
async def delete_all_data() -> dict[str, int]:
    """Wipe every session and project. Settings (Bash/WebFetch toggles,
    HA token, allowlist) are intentionally preserved."""
    n_sessions = await manager.delete_all()
    n_projects = projects_store.delete_all()
    plans_store.delete_all()
    audit_log("data", "delete_all", {"sessions": n_sessions, "projects": n_projects})
    return {"sessions": n_sessions, "projects": n_projects}


@app.get("/api/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    return manager.list()


@app.post("/api/sessions")
async def create_session(body: CreateSessionBody) -> dict[str, Any]:
    sess = await manager.create(title=body.title, project_id=body.project_id)
    audit_log(sess.id, "session_created", {"title": sess.title, "project_id": sess.project_id})
    return {"id": sess.id, "title": sess.title, "project_id": sess.project_id}


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, body: UpdateSessionBody) -> dict[str, Any]:
    sess = manager.update(session_id, title=body.title, project_id=body.project_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    audit_log(session_id, "session_updated", {"title": sess.title, "project_id": sess.project_id})
    return {"id": sess.id, "title": sess.title, "project_id": sess.project_id}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    ok = await manager.delete(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    audit_log(session_id, "session_deleted", {})
    return {"ok": True}


@app.get("/api/projects")
async def list_projects() -> list[dict[str, Any]]:
    return projects_store.load()


@app.post("/api/projects")
async def create_project(body: ProjectBody) -> dict[str, Any]:
    record = projects_store.create(body.name)
    audit_log("projects", "project_created", {"id": record["id"], "name": record["name"]})
    return record


class ReorderBody(BaseModel):
    ids: list[str]


@app.post("/api/projects/reorder")
async def reorder_projects(body: ReorderBody) -> list[dict[str, Any]]:
    return projects_store.reorder(body.ids)


@app.patch("/api/projects/{project_id}")
async def rename_project(project_id: str, body: ProjectBody) -> dict[str, Any]:
    record = projects_store.rename(project_id, body.name)
    if not record:
        raise HTTPException(status_code=404, detail="Project not found")
    audit_log("projects", "project_renamed", {"id": project_id, "name": record["name"]})
    return record


class NotesBody(BaseModel):
    notes: str


@app.get("/api/projects/{project_id}/notes")
async def get_project_notes(project_id: str) -> dict[str, str]:
    return {"notes": projects_store.read_notes(project_id)}


@app.put("/api/projects/{project_id}/notes")
async def put_project_notes(project_id: str, body: NotesBody) -> dict[str, bool]:
    if not projects_store.write_notes(project_id, body.notes):
        raise HTTPException(status_code=404, detail="Project not found")
    audit_log("projects", "project_notes_updated", {"id": project_id, "len": len(body.notes)})
    return {"ok": True}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict[str, bool]:
    if not projects_store.delete(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    # Orphan any sessions into Unsorted.
    for sess in list(manager.sessions.values()):
        if sess.project_id == project_id:
            manager.update(sess.id, project_id=None)
    audit_log("projects", "project_deleted", {"id": project_id})
    return {"ok": True}


# --- Saved plans -------------------------------------------------------------

class SavePlanBody(BaseModel):
    name: str
    content: str


@app.get("/api/plans")
async def list_plans() -> list[dict]:
    return plans_store.load()


@app.post("/api/plans")
async def save_plan(body: SavePlanBody) -> dict:
    name = body.name.strip()
    content = body.content.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    record = plans_store.create(name, content)
    audit_log("plans", "plan_saved", {"id": record["id"], "name": name})
    return record


@app.delete("/api/plans/{plan_id}")
async def delete_plan(plan_id: str) -> dict[str, bool]:
    if not plans_store.delete(plan_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    audit_log("plans", "plan_deleted", {"id": plan_id})
    return {"ok": True}


@app.post("/api/plans/{plan_id}/load")
async def load_plan(plan_id: str) -> dict:
    """Open a new plan-mode session seeded with a saved plan."""
    all_plans = plans_store.load()
    plan = next((p for p in all_plans if p["id"] == plan_id), None)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    new = await manager.create(
        title=plan["name"],
        permission_mode="plan",
    )
    seed = (
        "[Note for Claude — this message is from the app, not the user]\n"
        "The user has loaded a saved plan from a previous conversation. "
        "Read the plan below, then ask what they would like to do — "
        "refine it further, or approve it so you can start implementing.\n\n"
        f"=== Saved Plan: {plan['name']} ===\n"
        f"{plan['content']}\n"
        "=== End of Plan ==="
    )
    await new.send_message(seed, [], silent=True)
    audit_log("plans", "plan_loaded", {"plan_id": plan_id, "session_id": new.id})
    return {"id": new.id, "title": new.title, "project_id": new.project_id, "model": new.model}


@app.get("/api/sessions/{session_id}/stream")
async def stream_events(session_id: str) -> EventSourceResponse:
    sess = manager.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_gen():
        # Atomic snapshot + subscription: every event is delivered exactly once.
        # Events already in `history` come from `snapshot`; everything emitted
        # after the subscribe call comes from `queue`. For sessions rehydrated
        # from disk (no in-memory history yet), seed the snapshot from jsonl.
        snapshot, queue = sess.subscribe()
        try:
            if not snapshot:
                snapshot = list(read_history(session_id))
            # Drop permission cards that have already been resolved (or timed
            # out) — they're interaction-only, not conversation content. Active
            # ones (still in pending_permissions) stay so a reconnecting client
            # can still respond.
            snapshot = [
                e for e in snapshot
                if e.get("type") != "permission_request" or e.get("id") in pending_permissions
            ]
            if snapshot:
                yield {"event": "claude", "data": _safe_json({"type": "snapshot", "events": snapshot})}
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15)
                    yield {"event": "claude", "data": _safe_json(evt)}
                    if evt.get("type") == "session_ended":
                        break
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": str(time.time())}
        finally:
            sess.unsubscribe(queue)

    return EventSourceResponse(event_gen())


@app.post("/api/sessions/{session_id}/message")
async def post_message(session_id: str, body: MessageBody) -> dict[str, bool]:
    sess = await manager.ensure_started(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    await sess.send_message(body.text, body.attachments)
    manager.touch(session_id)
    audit_log(session_id, "user_message", {
        "text_len": len(body.text),
        "attachment_count": len(body.attachments),
    })
    return {"ok": True}


class ModelBody(BaseModel):
    model: str | None = None


@app.post("/api/sessions/{session_id}/model")
async def set_model(session_id: str, body: ModelBody) -> dict[str, Any]:
    sess = await manager.set_model(session_id, body.model)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    audit_log(session_id, "model_changed", {"model": body.model})
    return {"id": sess.id, "model": sess.model}


class EffortBody(BaseModel):
    effort: str | None = None


@app.post("/api/sessions/{session_id}/effort")
async def set_effort(session_id: str, body: EffortBody) -> dict[str, Any]:
    sess = await manager.set_effort(session_id, body.effort)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    audit_log(session_id, "effort_changed", {"effort": body.effort})
    return {"id": sess.id, "effort": sess.effort}


class PermissionModeBody(BaseModel):
    mode: str


@app.post("/api/sessions/{session_id}/permission_mode")
async def set_permission_mode(session_id: str, body: PermissionModeBody) -> dict[str, Any]:
    if body.mode not in ("default", "plan"):
        raise HTTPException(status_code=400, detail="mode must be 'default' or 'plan'")
    sess = await manager.set_permission_mode(session_id, body.mode)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    audit_log(session_id, "permission_mode_changed", {"mode": body.mode})
    return {"id": sess.id, "permission_mode": sess.permission_mode}


@app.post("/api/sessions/{session_id}/resume")
async def resume_session(session_id: str) -> dict[str, bool]:
    sess = await manager.ensure_started(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    audit_log(session_id, "session_resumed", {})
    return {"ok": True}


@app.post("/api/sessions/{session_id}/clear")
async def clear_context(session_id: str) -> dict[str, bool]:
    sess = manager.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    await sess.clear_context()
    audit_log(session_id, "context_cleared", {})
    return {"ok": True}


@app.get("/api/sessions/{session_id}/cost")
async def get_cost(session_id: str) -> dict[str, Any]:
    sess = manager.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    cost = await asyncio.to_thread(session_cost, session_id)
    latest = await asyncio.to_thread(session_latest_input_tokens, session_id)
    window = _MODEL_CONTEXT_WINDOWS.get(sess.model or "", _DEFAULT_CONTEXT_WINDOW)
    cost["latest_input"] = latest
    cost["context_window"] = window
    cost["context_pct"] = round(latest / window, 4) if latest else 0
    return cost


@app.post("/api/sessions/{session_id}/summarize_fresh")
async def summarize_fresh(session_id: str) -> dict[str, Any]:
    """Ask Claude to summarize this chat, then open a new chat seeded with
    that summary so the conversation can continue without burning context."""
    from auth import load_token
    import json as _json
    import urllib.request
    import urllib.error

    src = manager.get(session_id)
    if not src:
        raise HTTPException(status_code=404, detail="Session not found")
    token = load_token()
    if not token:
        raise HTTPException(status_code=401, detail="No OAuth token saved")

    # Flatten history into a plain transcript for the summarizer.
    lines: list[str] = []
    for evt in read_history(session_id):
        t = evt.get("type")
        if t == "user_message" and evt.get("text"):
            lines.append(f"User: {evt['text']}")
        elif t == "assistant_text" and evt.get("text"):
            lines.append(f"Assistant: {evt['text']}")
    if not lines:
        raise HTTPException(status_code=400, detail="Nothing to summarize yet")
    transcript = "\n\n".join(lines)[-60000:]  # cap to avoid blowing context

    prompt = (
        "Summarize the conversation below so a fresh Claude session can pick up where it "
        "left off. Cover: the user's goal, key decisions made, current state / what was "
        "just being worked on, any open questions or next steps. Keep it tight — bullets "
        "are fine. Do not add preamble; start with the summary directly.\n\n"
        f"=== Transcript ===\n{transcript}\n=== End ==="
    )
    body = _json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "claude-code-messages/0.1",
        },
    )

    def _summarize() -> str:
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = _json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Summarise: {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise HTTPException(status_code=502, detail=f"Summarise: {e.reason}") from e
        parts = [b.get("text", "") for b in (payload.get("content") or []) if b.get("type") == "text"]
        return "".join(parts).strip()

    summary = await asyncio.to_thread(_summarize)
    if not summary:
        raise HTTPException(status_code=502, detail="Summarise returned no text")

    new = await manager.create(
        title=f"{src.title} (continued)",
        project_id=src.project_id,
        model=src.model,
        permission_mode=src.permission_mode,
    )
    seed = (
        "Picking up from a previous chat. Here is the summary of where we left off; "
        "use it as context and acknowledge briefly, then wait for the next instruction.\n\n"
        f"{summary}"
    )
    await new.send_message(seed, [])
    audit_log(session_id, "summarize_fresh", {"new_session_id": new.id, "summary_chars": len(summary)})
    return {"id": new.id, "title": new.title, "project_id": new.project_id, "model": new.model}


@app.get("/api/usage")
async def get_usage() -> dict[str, Any]:
    """Read Anthropic's rate-limit headers via a 4-token ping. Mirrors what
    the iOS Claude app and the CLI's `/usage` view show."""
    from auth import load_token
    import json as _json
    import urllib.request
    import urllib.error

    token = load_token()
    if not token:
        raise HTTPException(status_code=401, detail="No OAuth token saved")

    body = _json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4,
        "messages": [{"role": "user", "content": "hi"}],
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "claude-code-messages/0.1",
        },
    )

    def _do() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Anthropic API: {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise HTTPException(status_code=502, detail=f"Anthropic API: {e.reason}") from e

        def _pct(key: str) -> float | None:
            try:
                return float(hdrs[key])
            except (KeyError, TypeError, ValueError):
                return None

        def _ts(key: str) -> int | None:
            try:
                return int(hdrs[key])
            except (KeyError, TypeError, ValueError):
                return None

        return {
            "five_hour_pct": _pct("anthropic-ratelimit-unified-5h-utilization"),
            "five_hour_reset": _ts("anthropic-ratelimit-unified-5h-reset"),
            "seven_day_pct": _pct("anthropic-ratelimit-unified-7d-utilization"),
            "seven_day_reset": _ts("anthropic-ratelimit-unified-7d-reset"),
            "representative": hdrs.get("anthropic-ratelimit-unified-representative-claim"),
            "status": hdrs.get("anthropic-ratelimit-unified-status"),
        }

    return await asyncio.to_thread(_do)


@app.post("/api/sessions/{session_id}/interrupt")
async def interrupt(session_id: str) -> dict[str, bool]:
    sess = manager.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess.proc is None:
        return {"ok": True}  # nothing to interrupt
    await sess.interrupt()
    # Cancel any permission futures waiting for user input. This releases the
    # session lock so the next generation can prompt normally, and tells the
    # frontend to remove stale cards.
    for req_id in list(session_pending_request_ids.pop(session_id, set())):
        fut = pending_permissions.pop(req_id, None)
        if fut and not fut.done():
            fut.cancel()
        await sess._emit({"type": "permission_resolved", "id": req_id})
    audit_log(session_id, "interrupt", {})
    return {"ok": True}


@app.post("/api/sessions/{session_id}/permission")
async def respond_permission(session_id: str, body: PermissionBody) -> dict[str, bool]:
    sess = manager.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    decision = body.decision
    if not decision:
        decision = "allow_once" if body.approved else "reject"
    if decision not in ("reject", "allow_once", "allow_domain", "allow_turn"):
        raise HTTPException(status_code=400, detail=f"Unknown decision: {decision}")
    # allow_turn: set the per-turn Bash trust flag, then resolve the awaiting
    # future as a regular allow_once so the current call proceeds.
    if decision == "allow_turn":
        sess._bash_trust_until_turn_end = True
        resolve_with = "allow_once"
    else:
        resolve_with = decision
    if body.request_id:
        fut = pending_permissions.pop(body.request_id, None)
        if fut and not fut.done():
            fut.set_result(resolve_with)
    audit_log(session_id, "permission_response", {"decision": decision, "request_id": body.request_id})
    return {"ok": True}


def _host_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url if "://" in url else f"http://{url}")
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


def _mcp_card_text(tool_name: str, tool_input: dict, entry_title: str = "") -> tuple[str, str]:
    """Human-readable (title, body) for an MCP tool permission card.

    Falls back to the raw tool name + truncated JSON for shapes we don't
    recognise — better than nothing, and keeps the original info visible.
    """
    raw_json = ""
    try:
        import json as _json
        raw_json = _json.dumps(tool_input, ensure_ascii=False)[:240]
    except (TypeError, ValueError):
        raw_json = str(tool_input)[:240]

    # Strip the mcp__<server>__ prefix for the fallback title.
    short = tool_name.split("__")[-1] if tool_name.startswith("mcp__") else tool_name

    if tool_name == "mcp__home-assistant__ha_call_service":
        domain = (tool_input.get("domain") or "").strip()
        service = (tool_input.get("service") or "").strip()
        target = tool_input.get("target") or {}
        entity = target.get("entity_id") if isinstance(target, dict) else None
        area = target.get("area_id") if isinstance(target, dict) else None
        data = tool_input.get("service_data") or tool_input.get("data") or {}

        # Common reload services get plain-English titles.
        reload_titles = {
            ("automation", "reload"): "Reload automations",
            ("script", "reload"): "Reload scripts",
            ("scene", "reload"): "Reload scenes",
            ("rest_command", "reload"): "Reload REST commands",
            ("template", "reload"): "Reload template entities",
            ("input_boolean", "reload"): "Reload input booleans",
            ("input_number", "reload"): "Reload input numbers",
            ("input_select", "reload"): "Reload input selects",
            ("input_text", "reload"): "Reload input text",
            ("input_datetime", "reload"): "Reload input datetimes",
            ("homeassistant", "reload_all"): "Reload all YAML configs",
            ("homeassistant", "restart"): "Restart Home Assistant",
            ("homeassistant", "check_config"): "Check HA config",
        }
        if (domain, service) in reload_titles:
            return reload_titles[(domain, service)], f"{domain}.{service}"

        target_str = ""
        if isinstance(entity, str):
            target_str = entity
        elif isinstance(entity, list) and entity:
            target_str = ", ".join(entity[:3]) + (f" +{len(entity)-3}" if len(entity) > 3 else "")
        elif isinstance(area, str):
            target_str = f"area {area}"

        if domain and service:
            title = f"{domain}.{service}"
            resolved = entry_title or target_str
            if resolved:
                title += f" → {resolved}"
            body = raw_json if data else (f"target: {target_str}" if target_str else "")
            return title, body

    if tool_name == "mcp__home-assistant__ha_config_set_automation":
        cfg = tool_input.get("config") or {}
        alias = cfg.get("alias") if isinstance(cfg, dict) else None
        aid = tool_input.get("automation_id") or (cfg.get("id") if isinstance(cfg, dict) else None)
        if alias:
            return (f"Save automation: {alias}", f"id: {aid or '(new)'}")
        return ("Save automation", raw_json)

    if tool_name == "mcp__home-assistant__ha_config_remove_automation":
        aid = tool_input.get("automation_id") or ""
        return (f"Delete automation {aid}".strip(), raw_json)

    if tool_name == "mcp__home-assistant__ha_config_set_script":
        cfg = tool_input.get("config") or {}
        alias = cfg.get("alias") if isinstance(cfg, dict) else None
        sid = tool_input.get("script_id") or ""
        if alias:
            return (f"Save script: {alias}", f"id: {sid or '(new)'}")
        return ("Save script", raw_json)

    if tool_name == "mcp__home-assistant__ha_config_remove_script":
        sid = tool_input.get("script_id") or ""
        return (f"Delete script {sid}".strip(), raw_json)

    if tool_name == "mcp__home-assistant__ha_config_set_helper":
        cfg = tool_input.get("config") or {}
        name = cfg.get("name") if isinstance(cfg, dict) else None
        domain = tool_input.get("domain") or ""
        if name:
            return (f"Save {domain} helper: {name}".strip(), raw_json)
        return ("Save helper", raw_json)

    if tool_name in (
        "mcp__home-assistant__ha_config_remove_helper",
        "mcp__home-assistant__ha_remove_helpers_integrations",
    ):
        hid = tool_input.get("helper_id") or tool_input.get("entity_id") or ""
        return (f"Delete helper {hid}".strip(), raw_json)

    if tool_name == "mcp__home-assistant__ha_config_set_dashboard":
        dash_id = tool_input.get("dashboard_id") or ""
        return (f"Save dashboard {dash_id}".strip(), raw_json)

    if tool_name == "mcp__home-assistant__ha_bulk_control":
        ops = tool_input.get("operations") or []
        n = len(ops) if isinstance(ops, list) else 0
        return (f"Bulk control: {n} operation{'s' if n != 1 else ''}", raw_json)

    if tool_name == "mcp__home-assistant__ha_backup_create":
        return ("Create backup", raw_json)
    if tool_name == "mcp__home-assistant__ha_backup_restore":
        return ("Restore backup", raw_json)
    if tool_name == "mcp__home-assistant__ha_manage_backup":
        action = (tool_input.get("action") or "").lower()
        if "restore" in action:
            return ("Restore backup", raw_json)
        return ("Create backup", raw_json)

    return (short, raw_json)


@app.post("/api/internal/permission")
async def internal_permission(body: InternalPermissionBody) -> dict[str, bool]:
    """Called by the PreToolUse hook when ask_bash / ask_webfetch is on. Emits
    a permission_request to the active session's SSE stream and blocks until
    the user picks Reject / Allow once / Allow domain (or times out → deny).

    A per-session lock serializes prompts: if Claude emits Bash + WebFetch in
    the same turn, both hook subprocesses POST here in parallel but only one
    card is shown at a time — the second waits at the lock acquire.
    """
    sess = manager.get(body.session_id)
    if not sess:
        return {"approved": False}

    # Per-turn Bash trust: the user has already approved "trust Bash this turn"
    # on an earlier card. Skip the prompt entirely until generation_ended resets
    # the flag. Only applies to Bash — WebFetch/MCP still ask each time.
    if body.tool_name == "Bash" and sess._bash_trust_until_turn_end:
        audit_log(body.session_id, "permission_auto_allowed", {
            "tool": body.tool_name,
            "reason": "bash_trust_turn",
        })
        return {"approved": True}

    # Per-command Bash auto-allow: the user has opted in to commands like
    # `ls`/`grep`/etc. that have no side effects and no network. Short-circuit
    # before locking + emitting a card, so a fast 4-command sequence doesn't
    # serialise needlessly.
    if body.tool_name == "Bash":
        from settings import is_safe_bash_command
        cmd = body.tool_input.get("command", "") if isinstance(body.tool_input, dict) else ""
        allowed = list(load_settings().get("bash_auto_allow") or [])
        if isinstance(cmd, str) and is_safe_bash_command(cmd, allowed):
            audit_log(body.session_id, "permission_auto_allowed", {
                "tool": body.tool_name,
                "reason": "bash_safe_command",
                "command": cmd[:120],
            })
            return {"approved": True}

    lock = session_permission_locks.setdefault(body.session_id, asyncio.Lock())
    async with lock:
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        pending_permissions[request_id] = fut
        session_pending_request_ids.setdefault(body.session_id, set()).add(request_id)

        import json as _json
        cmd = body.tool_input.get("command", "") if body.tool_name == "Bash" else ""
        url = ""
        if body.tool_name == "WebFetch":
            for k in ("url", "URL", "uri", "href"):
                v = body.tool_input.get(k)
                if isinstance(v, str) and v:
                    url = v
                    break
        host = _host_from_url(url)
        plan = ""
        if body.tool_name in ("exit_plan_mode", "ExitPlanMode"):
            plan = body.tool_input.get("plan", "") if isinstance(body.tool_input, dict) else ""
        title = ""
        if body.tool_name.startswith("mcp__"):
            title, mcp_body = _mcp_card_text(body.tool_name, body.tool_input)
            description = mcp_body or _json.dumps(body.tool_input)[:240]
        else:
            description = plan or cmd or url or _json.dumps(body.tool_input)[:240]
        enrichment: dict = {}
        if body.tool_name == "mcp__home-assistant__ha_call_service":
            enrichment = await _enrich_destructive_call(body.tool_name, body.tool_input)
        # Re-generate title with resolved name if available
        if body.tool_name.startswith("mcp__") and enrichment.get("entry_title"):
            title, mcp_body = _mcp_card_text(body.tool_name, body.tool_input, enrichment["entry_title"])
            description = mcp_body or _json.dumps(body.tool_input)[:240]
        evt = {
            "type": "permission_request",
            "id": request_id,
            "tool": body.tool_name,
            "description": description,
            "input": body.tool_input,
        }
        if title:
            evt["title"] = title
        if host:
            evt["domain"] = host
        # Destructive-op enrichment
        for key in ("destructive", "consequence", "confirm_name"):
            if enrichment.get(key):
                evt[key] = enrichment[key]
        await sess._emit(evt)
        audit_log(body.session_id, "permission_requested", {
            "request_id": request_id,
            "tool": body.tool_name,
            "domain": host,
            "summary": description[:200],
        })
        # Actionable push so the user can Allow/Reject from the lock screen.
        # _send_permission_push self-suppresses if the user is actively focused.
        asyncio.create_task(_send_permission_push(
            sess, request_id, body.tool_name, description,
        ))

        # No timeout — locking the phone or switching apps shouldn't cause the
        # CLI to silently get a denial. The hook subprocess on the CLI side
        # holds its HTTP request open; the CLI proc sits idle until the user
        # approves, denies, or hits Stop. Hitting Stop kills the CLI which
        # cleans up the hook subprocess.
        try:
            decision = await fut
        finally:
            session_pending_request_ids.get(body.session_id, set()).discard(request_id)

    if decision == "allow_domain" and host:
        try:
            await asyncio.to_thread(add_webfetch_domain, host)
        except Exception as e:
            audit_log(body.session_id, "permission_allowlist_failed", {"host": host, "error": str(e)})
    if (
        body.tool_name in ("ExitPlanMode", "exit_plan_mode")
        and sess.permission_mode == "plan"
    ):
        # Plan-mode flow. The CLI was started with --permission-mode plan as a
        # process-lifetime flag, so any ExitPlanMode resolution it processes
        # ends with the CLI crashing. Both Approve and Refine route through a
        # transparent kill-and-respawn:
        #
        # - Approve: return approved=True, flip session to default mode, then
        #   schedule _approve_handoff. Older CLIs (< 2.1.170) crash on the
        #   approved ExitPlanMode; newer ones emit a built-in "Exit plan mode?"
        #   confirmation tool_result and wait for the user. _approve_handoff
        #   covers both: if proc still alive, send a silent "Yes" to satisfy
        #   the confirmation; if dead, respawn and send "Proceed".
        #
        # - Refine: arm _plan_refine_pending and kill the proc ourselves a
        #   moment after returning to the hook. Killing eagerly stops Claude
        #   from generating any "ok proceeding…" interim text between the
        #   tool denial and the respawn. _read_stdout respawns in plan mode
        #   with an "ask me what to change" message.
        if decision in ("allow_once", "allow_domain"):
            sess.permission_mode = "default"
            manager._persist()
            audit_log(body.session_id, "permission_mode_changed", {"mode": "default", "reason": "exit_plan_mode_approved"})
            asyncio.create_task(_approve_handoff(sess))
            return {"approved": True}
        else:
            audit_log(body.session_id, "plan_refine_requested", {})
            asyncio.create_task(_refine_handoff(sess))
            return {"approved": False}
    return {"approved": decision in ("allow_once", "allow_domain")}


async def _approve_handoff(sess) -> None:
    """After ExitPlanMode is approved in plan mode, get Claude unstuck.

    CLI v2.1.170+ emits a built-in "Exit plan mode?" tool_result (is_error=true)
    on approval and waits for the user — without this, the user has to manually
    type "Yes" to proceed. Older CLIs crash the process instead. We handle both:

      proc alive  -> send silent "Yes" to confirm the built-in prompt
      proc dead   -> respawn in default mode and send silent "Proceed"
    """
    await asyncio.sleep(0.15)  # let the hook get its HTTP response first
    alive = sess.proc is not None and sess.proc.returncode is None
    audit_log(sess.id, "approve_handoff_start", {"proc_alive": alive})
    try:
        if alive:
            await sess.send_message("Yes", silent=True)
            audit_log(sess.id, "approve_handoff_confirmed", {})
        else:
            sess._silent_shutdown = True
            await sess.start()
            await sess.send_message("Proceed with the plan above.", silent=True)
            audit_log(sess.id, "approve_handoff_respawned", {"pid": sess.proc.pid if sess.proc else None})
    except Exception as e:
        audit_log(sess.id, "approve_handoff_failed", {"error": repr(e)})
        await sess._emit({"type": "error", "message": f"Plan approve failed: {e}"})
        await sess._emit({"type": "generation_ended", "subtype": "error"})


async def _refine_handoff(sess) -> None:
    """Synchronously kill the plan-mode proc, respawn under --resume in plan
    mode, and send a 'ask what to change' message. Audit-logged at each step
    so we can diagnose failures."""
    await asyncio.sleep(0.05)  # let the hook get its HTTP response first
    audit_log(sess.id, "refine_handoff_start", {"proc_alive": sess.proc is not None and sess.proc.returncode is None})
    sess._silent_shutdown = True  # the imminent stop() should emit nothing
    try:
        await sess.stop()
        audit_log(sess.id, "refine_handoff_after_stop", {})
        await sess.start()
        audit_log(sess.id, "refine_handoff_after_start", {"pid": sess.proc.pid if sess.proc else None})
        await sess.send_message(
            "I'd like to refine the plan you just showed before any coding. "
            "Ask me one specific question about which part of the plan to "
            "change, add, or remove. Don't propose a new plan until I answer.",
            silent=True,
        )
        audit_log(sess.id, "refine_handoff_after_send", {})
    except Exception as e:
        audit_log(sess.id, "refine_handoff_failed", {"error": repr(e)})
        await sess._emit({"type": "error", "message": f"Plan refine failed: {e}"})
        await sess._emit({"type": "generation_ended", "subtype": "error"})


@app.post("/api/sessions/{session_id}/upload")
async def upload(session_id: str, file: UploadFile = File(...)) -> dict[str, str]:
    sess = manager.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    suffix = Path(file.filename or "img").suffix or ".png"
    name = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / name
    size = 0
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                await out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large")
            await out.write(chunk)
    audit_log(session_id, "upload", {"path": str(dest), "size": size, "original_name": file.filename or ""})
    return {"path": str(dest), "name": name}


def _safe_json(evt: dict) -> str:
    try:
        return json.dumps(evt)
    except (TypeError, ValueError):
        return json.dumps({"type": "error", "data": "unserialisable event"})


# --- Audit log reader --------------------------------------------------------

def _build_audit_summary(entries: list) -> dict:
    from collections import Counter, defaultdict

    bash_cmds: Counter = Counter()
    bash_blocked: set = set()
    files: dict = defaultdict(lambda: {"reads": 0, "writes": 0})
    uploads: list = []
    blocked_events: list = []
    event_counts: Counter = Counter()
    blocked_count = 0

    WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

    for e in entries:
        etype = e.get("type", "")
        event_counts[etype] += 1
        payload = e.get("payload", {}) or {}

        if etype == "hook_allow":
            tool = payload.get("tool", "")
            inp = payload.get("input", {}) or {}
            if tool == "Bash":
                cmd = inp.get("command", "").strip()
                if cmd:
                    bash_cmds[cmd[:200]] += 1
            elif tool == "Read":
                path = inp.get("file_path", "")
                if path:
                    files[path]["reads"] += 1
            elif tool in WRITE_TOOLS:
                path = inp.get("file_path", "")
                if path:
                    files[path]["writes"] += 1

        elif etype == "hook_block":
            blocked_count += 1
            reason = payload.get("reason", "")
            inner = (payload.get("payload", {}) or {})
            tool = inner.get("tool", "")
            inp = inner.get("input", {}) or {}
            if tool == "Bash":
                cmd = inp.get("command", "").strip()
                if cmd:
                    bash_cmds[cmd[:200]] += 1
                    bash_blocked.add(cmd[:200])
            # Categorise: user-driven rejection vs hard security rule
            user_rejected = reason.startswith("user rejected") or reason == "plan_refine_requested"
            category = "user" if user_rejected else "rules"
            if tool == "Bash":
                detail = inp.get("command", "")[:150]
            elif tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
                detail = inp.get("file_path", "")
            elif tool == "WebFetch":
                detail = inp.get("url", inp.get("URL", ""))[:150]
            else:
                detail = tool or reason[:150]
            blocked_events.append({
                "ts": e.get("ts", 0),
                "tool": tool,
                "detail": detail,
                "reason": reason,
                "category": category,
            })

        elif etype == "upload":
            path = payload.get("path", "")
            size = payload.get("size", 0)
            original_name = payload.get("original_name", "")
            if path:
                uploads.append({"path": path, "size": size, "original_name": original_name, "ts": e.get("ts", 0)})

    top_bash = [
        {"cmd": cmd, "count": count, "blocked": cmd in bash_blocked}
        for cmd, count in bash_cmds.most_common(30)
    ]

    file_list = [
        {"path": path, "reads": counts["reads"], "writes": counts["writes"]}
        for path, counts in sorted(
            files.items(),
            key=lambda x: x[1]["reads"] + x[1]["writes"],
            reverse=True,
        )
    ]

    user_blocked  = [e for e in blocked_events if e["category"] == "user"]
    rules_blocked = [e for e in blocked_events if e["category"] == "rules"]

    return {
        "total_tool_calls": event_counts.get("hook_allow", 0) + event_counts.get("hook_block", 0),
        "blocked_count": blocked_count,
        "user_blocked_count": len(user_blocked),
        "rules_blocked_count": len(rules_blocked),
        "blocked_events": sorted(blocked_events, key=lambda x: x["ts"], reverse=True),
        "bash_commands": top_bash,
        "files": file_list,
        "uploads": sorted(uploads, key=lambda x: x["ts"], reverse=True)[:50],
        "event_counts": dict(event_counts),
    }


@app.get("/api/audit")
async def get_audit(
    search: str | None = None,
    since: float | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Return parsed audit log with summary + paginated entries."""
    from audit import AUDIT_LOG_PATH

    all_entries: list = []
    if AUDIT_LOG_PATH.exists():
        try:
            async with aiofiles.open(AUDIT_LOG_PATH, encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        all_entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

    # Date filter applies to both summary and log view
    if since is not None:
        all_entries = [e for e in all_entries if e.get("ts", 0) >= since]

    summary = _build_audit_summary(all_entries)

    # Search filter applies to log view only
    if search:
        sl = search.lower()
        display_entries = [e for e in all_entries if sl in json.dumps(e).lower()]
    else:
        display_entries = all_entries

    total = len(display_entries)
    page = list(reversed(display_entries))[offset: offset + limit]

    return {"total": total, "entries": page, "summary": summary}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8099")),
        log_level="info",
    )
