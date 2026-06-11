"""Security guardrails — enforces the hard rules from SECURITY.md.

These functions are called by tool-call hooks before any file/Bash action
is executed by Claude. They CANNOT be disabled by the user at runtime.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

FORBIDDEN_PATHS: tuple[str, ...] = (
    "/config/secrets.yaml",
)

FORBIDDEN_GLOBS: tuple[str, ...] = (
    "**/.storage/auth*",
    "**/.storage/onboarding*",
    "**/*token*",
    "**/*password*",
    "**/*credential*",
    "**/.env*",
    # Claude CLI's combined settings + OAuth file. The token/credential globs
    # above don't catch it by name, so name it explicitly. Everything else
    # under /config/claude-config (plans, transcripts, etc.) stays writable.
    "**/.claude.json",
)

# Read is allowed; write/edit is blocked. Use for things Claude should be
# able to reason about but never modify (e.g. system-installed addon source).
# Note: /config/local_addons is INTENTIONALLY not here — that's where users
# develop their own addons, including this one, and writes must be allowed.
READONLY_PATHS: tuple[str, ...] = (
    "/addons",
)

PROTECTED_PATHS: tuple[str, ...] = (
    "/config/configuration.yaml",
    "/config/automations.yaml",
    "/config/scripts.yaml",
    "/config/scenes.yaml",
    "/config/groups.yaml",
    "/config/customize.yaml",
    "/config/ui-lovelace.yaml",
)

PROTECTED_GLOBS: tuple[str, ...] = (
    "/config/.storage/lovelace*",
)

DESTRUCTIVE_BASH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-[rRf]+[rRf\s]*"),
    re.compile(r"\bgit\s+push\s+(--force|-f)\b"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-f"),
    re.compile(r"\bha\s+core\s+(stop|restart)\b"),
    re.compile(r"\bha\s+host\s+(reboot|shutdown)\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bmkfs\."),
    re.compile(r"\bwipefs\b"),
    re.compile(r"\bsudo\b|\bsu\s+-"),
    re.compile(r"(curl|wget)\s+[^|]*\|\s*(sh|bash|zsh)"),
)


def is_forbidden(path: str) -> bool:
    p = str(Path(path).resolve())
    if any(p == fp or p.startswith(fp + "/") for fp in FORBIDDEN_PATHS):
        return True
    return any(fnmatch.fnmatch(p, g) for g in FORBIDDEN_GLOBS)


def is_readonly(path: str) -> bool:
    p = str(Path(path).resolve())
    return any(p == rp or p.startswith(rp + "/") for rp in READONLY_PATHS)


def is_protected(path: str) -> bool:
    p = str(Path(path).resolve())
    if p in PROTECTED_PATHS:
        return True
    return any(fnmatch.fnmatch(p, g) for g in PROTECTED_GLOBS)


def is_destructive_bash(cmd: str) -> bool:
    return any(pat.search(cmd) for pat in DESTRUCTIVE_BASH_PATTERNS)


def snapshot_path(original: str) -> str:
    """Return the .bak.<timestamp> path used when auto-backing-up a protected file."""
    import time as _t
    return f"{original}.bak.{int(_t.time())}"
