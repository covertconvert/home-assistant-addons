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


def log_info() -> dict:
    """Return size, oldest entry timestamp, and entry count without loading all entries."""
    if not AUDIT_LOG_PATH.exists():
        return {"size_bytes": 0, "oldest_ts": None, "entry_count": 0}
    size = AUDIT_LOG_PATH.stat().st_size
    oldest_ts: float | None = None
    count = 0
    try:
        with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("ts")
                    if ts and (oldest_ts is None or ts < oldest_ts):
                        oldest_ts = ts
                    count += 1
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return {"size_bytes": size, "oldest_ts": oldest_ts, "entry_count": count}


def trim(retention_days: int) -> int:
    """Remove entries older than retention_days. Returns number of entries removed.
    retention_days=0 means keep forever (no-op)."""
    if retention_days <= 0 or not AUDIT_LOG_PATH.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    kept: list[str] = []
    removed = 0
    try:
        with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                    if entry.get("ts", 0) >= cutoff:
                        kept.append(stripped)
                    else:
                        removed += 1
                except json.JSONDecodeError:
                    kept.append(stripped)  # preserve malformed lines
    except OSError:
        return 0
    if removed == 0:
        return 0
    try:
        tmp = AUDIT_LOG_PATH.with_suffix(".log.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
        tmp.replace(AUDIT_LOG_PATH)
    except OSError:
        pass
    return removed
