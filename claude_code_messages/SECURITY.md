# Claude Code Messages — Security & Privacy Rules

These are the guardrails the addon MUST enforce so that Claude can edit your HA config without ever causing real damage. Split into hard rules (enforced by code/hooks — can't be bypassed) and soft rules (in `CLAUDE.md`, guide Claude's behaviour).

**Last reviewed: 2026-06-09 (draft v0.1)**
**Next review due: before first public release**

---

## Hard rules (enforced — no bypass)

### 1. Forbidden files (read & write blocked)

These never get read into Claude's context, never get sent to the API, never get edited or deleted. The hook returns an "access denied" error if Claude tries.

- `/config/secrets.yaml`
- `/config/.storage/auth*`
- `/config/.storage/onboarding*`
- Anything matching glob `**/*token*`, `**/*password*`, `**/*credential*`, `**/.env*`
- `/config/claude-config/` (the addon's own OAuth dir — Claude can't see its own creds)

### 2. Protected files (deletion blocked, auto-backup before edit)

Edits allowed, but the addon auto-snapshots to `<file>.bak.<timestamp>` first. `rm` is blocked.

- `/config/configuration.yaml`
- `/config/automations.yaml`
- `/config/scripts.yaml`
- `/config/scenes.yaml`
- `/config/groups.yaml`
- `/config/customize.yaml`
- `/config/ui-lovelace.yaml`
- Any file matching `/config/.storage/lovelace*`

### 3. Destructive Bash commands — require explicit confirmation EVERY time

These bypass the "remember my approval" mechanism. Even if user has approved Bash for the session, these trigger a fresh confirm:

- `rm -rf` (any form)
- `git push --force`, `git push -f`
- `git reset --hard`
- `git clean -f`
- `ha core stop`, `ha core restart`
- `ha host reboot`, `ha host shutdown`
- `dd`, `mkfs.*`, `wipefs`
- Anything piping into `sudo`, `su`, or modifying `/etc/`
- `curl ... | sh`, `wget ... | sh` (pipe-to-shell)

### 4. Network restrictions

- Outbound HTTP allowed only via Claude's official API endpoints and the OAuth flow
- No telemetry or analytics endpoints, ever
- Image uploads stay local — never sent except as part of the Anthropic API call

### 5. Audit log

Every tool call (Read, Edit, Write, Bash, WebFetch, etc.) appended to `/config/claude-code-panel-audit.log` with timestamp, tool name, arguments, and outcome. Log is append-only from the addon's view. User can review or rotate manually.

---

## Soft rules (in `CLAUDE.md` — Claude is asked to follow)

These guide Claude's behaviour. They're not enforced by code, but they shape its decisions.

- Never restart Home Assistant without explicit user approval (reload is fine; restart is not)
- Never run `git commit --no-verify` or otherwise bypass pre-commit hooks
- Confirm understanding before making non-trivial edits (restate + propose + ask)
- Don't add features, refactor, or introduce abstractions beyond what the task requires
- Don't create new markdown/README files unless explicitly requested
- Prefer editing existing files over creating new ones
- When in doubt about a destructive action, ask first

---

## Privacy

- OAuth creds live in `/config/claude-config/` only. Never logged, never read by any tool call.
- The addon does not collect telemetry, usage stats, or crash reports.
- User can revoke OAuth from Anthropic's dashboard at any time; addon respects revocation immediately.
- The audit log contains the *contents* of edits Claude makes — be aware if sharing logs for debugging.
- All conversation history stays on the HAOS host. Optionally exportable, never auto-uploaded.

---

## What this does NOT protect against

Be honest about the limits:

- A user who manually approves a destructive Bash prompt anyway. The UI makes this hard but not impossible.
- Anthropic-side data handling — covered by your account's terms with Anthropic, not by us.
- Physical access to the HAOS host.
- A maliciously-crafted CLAUDE.md or prompt that tricks Claude into framing a harmful action as benign. The hard-rule list above is the real backstop here.

---

## Review checklist (before any release)

- [ ] Hard-rule file list is current with HA's file layout
- [ ] Destructive Bash patterns are tested against actual attempt scenarios
- [ ] Audit log path is writable and not world-readable
- [ ] OAuth dir permissions are 0700
- [ ] No new third-party dependencies introduce telemetry
- [ ] CLAUDE.md soft rules reflect actual current best practice
- [ ] Privacy section accurately describes what's stored/transmitted
