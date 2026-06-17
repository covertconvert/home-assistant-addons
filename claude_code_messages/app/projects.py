"""Project records — lightweight groupings of sessions.

Persisted to `<CLAUDE_CONFIG_DIR>/messages-projects.json` as a flat list. A
project is just `{id, name, created_at}`; the link to sessions lives on the
session record (`project_id`), not here, so deleting a project doesn't have
to update session records — sessions with an orphan project_id are simply
shown under "Unsorted" until reassigned.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/config/claude-config"))
PROJECTS_FILE = CONFIG_DIR / "messages-projects.json"
WORKDIR = Path(os.environ.get("CLAUDE_WORKDIR", "/config"))
NOTES_DIR = WORKDIR / "claude-project-notes"


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def _unique_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def notes_path_for(slug: str) -> Path:
    return NOTES_DIR / f"{slug}.md"


def read_notes(project_id: str) -> str:
    for p in load():
        if p["id"] == project_id:
            path = notes_path_for(p.get("slug") or _slugify(p["name"]))
            if path.exists():
                try:
                    return path.read_text(encoding="utf-8")
                except OSError:
                    return ""
            return ""
    return ""


def write_notes(project_id: str, text: str) -> bool:
    for p in load():
        if p["id"] == project_id:
            slug = p.get("slug") or _slugify(p["name"])
            NOTES_DIR.mkdir(parents=True, exist_ok=True)
            notes_path_for(slug).write_text(text, encoding="utf-8")
            return True
    return False


def notes_path_for_project(project_id: str) -> Path | None:
    for p in load():
        if p["id"] == project_id:
            slug = p.get("slug") or _slugify(p["name"])
            path = notes_path_for(slug)
            return path if path.exists() else None
    return None


def load() -> list[dict[str, Any]]:
    if not PROJECTS_FILE.exists():
        return []
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save(projects: list[dict[str, Any]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROJECTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(projects, indent=2), encoding="utf-8")
    tmp.replace(PROJECTS_FILE)


def create(name: str) -> dict[str, Any]:
    projects = load()
    taken = {p.get("slug") for p in projects if p.get("slug")}
    nm = name.strip() or "Untitled"
    record = {
        "id": str(uuid.uuid4()),
        "name": nm,
        "slug": _unique_slug(_slugify(nm), taken),
        "created_at": time.time(),
    }
    projects.append(record)
    save(projects)
    return record


def rename(project_id: str, name: str) -> dict[str, Any] | None:
    projects = load()
    for p in projects:
        if p["id"] == project_id:
            nm = name.strip() or p["name"]
            old_slug = p.get("slug") or _slugify(p["name"])
            taken = {q.get("slug") for q in projects if q.get("slug") and q["id"] != project_id}
            new_slug = _unique_slug(_slugify(nm), taken)
            p["name"] = nm
            p["slug"] = new_slug
            if old_slug != new_slug:
                old_path = notes_path_for(old_slug)
                if old_path.exists():
                    try:
                        old_path.rename(notes_path_for(new_slug))
                    except OSError:
                        pass
            save(projects)
            return p
    return None


def delete(project_id: str) -> bool:
    projects = load()
    new = [p for p in projects if p["id"] != project_id]
    if len(new) == len(projects):
        return False
    save(new)
    return True


def reorder(ids: list[str]) -> list[dict[str, Any]]:
    projects = load()
    by_id = {p["id"]: p for p in projects}
    reordered = [by_id[i] for i in ids if i in by_id]
    # Append any IDs not in the request (safety net) at the end.
    seen = set(ids)
    for p in projects:
        if p["id"] not in seen:
            reordered.append(p)
    save(reordered)
    return reordered


def delete_all() -> int:
    """Wipe every project record. Returns the number deleted. Notes files
    under NOTES_DIR are left on disk so the user can recover them; only
    the index is cleared."""
    projects = load()
    save([])
    return len(projects)
