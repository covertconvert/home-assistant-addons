"""Append-only audit log for every tool call Claude makes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG", "/config/claude-code-messages-audit.log"))


def log(session_id: str, event_type: str, payload: dict) -> None:
    entry = {
        "ts": time.time(),
        "session_id": session_id,
        "type": event_type,
        "payload": payload,
    }
    try:
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        # Don't crash the addon if the log volume is full / read-only
        pass
