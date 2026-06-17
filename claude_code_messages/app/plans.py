"""Saved plan records — plans stored from plan-mode for later use.

Persisted to `<CLAUDE_CONFIG_DIR>/messages-plans.json` as a flat list.
Each record: {id, name, content, created_at}.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/config/claude-config"))
PLANS_FILE = CONFIG_DIR / "messages-plans.json"


def load() -> list[dict[str, Any]]:
    if not PLANS_FILE.exists():
        return []
    try:
        return json.loads(PLANS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save(plans: list[dict[str, Any]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PLANS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plans, indent=2), encoding="utf-8")
    tmp.replace(PLANS_FILE)


def create(name: str, content: str) -> dict[str, Any]:
    plans = load()
    record: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": name.strip() or "Untitled plan",
        "content": content,
        "created_at": time.time(),
    }
    plans.append(record)
    _save(plans)
    return record


def delete(plan_id: str) -> bool:
    plans = load()
    new = [p for p in plans if p["id"] != plan_id]
    if len(new) == len(plans):
        return False
    _save(new)
    return True


def delete_all() -> int:
    plans = load()
    _save([])
    return len(plans)
