# Claude Code Messages — Security & Privacy

The app enforces a layered defense: hard rules (code-enforced, can't be bypassed by toggling settings), user-controllable prompts (you decide per-action), and soft rules (guide Claude's behaviour but not enforced). Underneath all of it sits one floor that matters more than any rule: a working Home Assistant backup.

**Last reviewed: 2026-06-13 (v1.0.0)**

-----

## The floor: HA backups

Read this before the rules below, because it is the rule that holds the others up.

This tool gives an AI agent read and write access to your config and a shell on your HA box. The rules in this document reduce accidents. They do not make the agent incapable of damage, because an agent that can edit your config can also break it. That tension is the whole point of the tool: enough access to be useful is also enough access to cause harm.

So the real safety net is recovery. If something goes badly wrong, a protected file deleted, a config corrupted, a bad edit you didn't catch, your path back is to restore from a backup. A recovery path you have never tested is not a recovery path.

Before you let the agent do real work:

- Turn on automatic Home Assistant backups.
- Send them off-box (a backup that dies with the host is not a backup).
- Restore one at least once, so you know the path works.

Everything below is what catches mistakes earlier. Backups are what catch the ones that get through.

-----

## What is and isn't protected

Every path under `/config` falls into one of three buckets. Knowing which bucket a file is in tells you exactly how recoverable a mistake is.

**1. Never touched (tool-layer block, best-effort).** Secrets and auth files. The `PreToolUse` hook denies the Read, Write, Edit, and Delete tools against these, so the agent's normal tools cannot reach them. This is enforced at the tool layer, not the OS layer, so it is a strong default but not an absolute wall: a raw shell command can still reach a file the Read tool refuses (see the Bash note below and the "does NOT protect against" section). Treat these as protected from the agent's ordinary behaviour, not as physically sealed.

**2. Auto-snapshotted (recoverable from .bak).** The core editable configs. The hook copies each to `<file>.bak.<timestamp>` before any edit, so a bad change is recoverable without reaching for a full backup.

**3. Everything else (recoverable from a HA backup only).** All other files under `/config`. No per-file block, no auto-snapshot. If the agent damages one of these and you approve the action, your recovery path is a HA backup. This is most of your config directory, which is why the backup floor above is the control that actually matters.

The exact file lists for buckets 1 and 2 are in the hard rules below.

-----

## Hard rules (enforced)

These fire from the `PreToolUse` hook before any tool runs. They cannot be disabled by toggling settings or approving prompts.

### 1. Forbidden files (Read, Write, Edit, Delete tools blocked)

The hook returns "deny" if Claude tries to read, edit, write, or delete any of these through its tools. Note the scope: this blocks the agent's file tools, not every possible shell command. A `cat` through Bash is covered in the limits section.

- `/config/secrets.yaml`
- `**/.claude.json` (the Claude CLI's own auth file)
- `**/.storage/auth*`
- `**/.storage/onboarding*`
- `**/*token*`, `**/*password*`, `**/*credential*`, `**/.env*`

### 2. Read-only paths (writes blocked)

- `/addons` reads are allowed (so Claude can reason about installed apps), but Write, Edit, MultiEdit, and NotebookEdit are blocked.

### 3. Protected files (auto-snapshot before edit)

The hook auto-copies these to `<file>.bak.<timestamp>` before any Write, Edit, or MultiEdit, so the change is recoverable.

- `/config/configuration.yaml`
- `/config/automations.yaml`
- `/config/scripts.yaml`
- `/config/scenes.yaml`
- `/config/groups.yaml`
- `/config/customize.yaml`
- `/config/ui-lovelace.yaml`
- Any file matching `/config/.storage/lovelace*`

Deletion of these via the Bash tool (e.g. a plain `rm`) is not path-blocked. It goes through the standard Bash permission flow below. Recovery is via the `.bak.*` snapshot or a HA backup.

### 4. Destructive Bash commands (blocked outright)

The hook blocks Bash commands matching any of these patterns. There is no "approve once": the command is rejected and Claude must take a different path.

- `rm -rf` (any flag combination including `-r` / `-R` / `-f`)
- `git push --force` / `git push -f`
- `git reset --hard`
- `git clean -f`
- `ha core stop` / `ha core restart`
- `ha host reboot` / `ha host shutdown`
- `dd if=…`
- `mkfs.*` / `wipefs`
- anything invoking `sudo` or `su -`
- `curl … | sh`, `curl … | bash`, `wget … | sh` and other pipe-to-shell forms

**This blocklist is a tripwire, not a wall.** Its job is the accidental case: the agent proposing a destructive command and you reflex-approving the card. It is not a boundary against a determined or injection-steered agent. A blocklist in front of a shell loses to interpreters (`python -c`, `perl -e`), in-place editors (`sed -i`, `truncate`, `: >`), variable and glob indirection, and command substitution. Rely on the buckets above and on backups for real recovery, not on this list catching every phrasing.

### 5. Audit log

Every tool call (Read, Edit, Write, Bash, WebFetch, and so on), both allowed and blocked, is appended to `/config/claude-code-messages-audit.log` with timestamp, tool name, arguments, and outcome. The file is created mode `0600` on first run. Append-only from the app's view; rotate or clear it manually.

-----

## User-controllable prompts (in Settings)

These are ON by default. Turn them off only if you trust Claude to act without confirmation in your environment.

- **Ask before Bash** — every shell command shows an Approve / Reject card. Independent of the destructive blocklist above, which is always enforced regardless of this setting.
- **Ask before WebFetch** — every outbound HTTP fetch shows a card with the URL. You can pick "Allow once" or "Always allow this domain" (the domain list lives in the app's settings file).

When a toggle is off, calls go straight through with no prompt, still audit-logged.

-----

## Soft rules (in `CLAUDE.md`)

These guide Claude's behaviour but are not enforced by code. Trust them to the same degree you trust Claude to follow instructions.

- Never restart Home Assistant without explicit user approval (reload is fine; restart is not).
- Never run `git commit --no-verify` or otherwise bypass pre-commit hooks.
- Confirm understanding before non-trivial edits (restate, propose, ask).
- Don't add features, refactor, or introduce abstractions beyond what the task requires.
- Don't create new markdown or README files unless explicitly requested.
- Prefer editing existing files over creating new ones.
- When in doubt about a destructive action, ask first.

-----

## Privacy

- OAuth credentials live in the Claude CLI's `.claude.json`, which is in the Forbidden list above, so the agent's file tools cannot read it. The rest of `/config/claude-config/` (plans, conversation history) is writable so Claude can manage its own working files there.
- The app does not collect telemetry, usage stats, or crash reports.
- You can revoke OAuth from your Anthropic dashboard at any time; the app respects revocation on the next request.
- The audit log records the arguments passed to tools (file paths, command strings, URL hosts). It does not record file contents read by Claude or assistant message text.
- Conversation history stays on the HAOS host (`/config/claude-config/`'s jsonl files). It is never auto-uploaded anywhere except to Anthropic as part of the conversation itself.
- Image attachments are uploaded to `/data/uploads/` inside the app, sent to Anthropic as part of the message payload, and not pushed anywhere else.

-----

## What this does NOT protect against

Being honest about the limits:

- **A user who manually approves a destructive prompt.** Ask-before-Bash makes accidents harder, not impossible.
- **Reading a protected file through Bash.** The Read tool is blocked against `secrets.yaml` and the other forbidden files, but a shell command such as `cat /config/secrets.yaml` can still pull the contents into the conversation, and from there they could leave over an approved WebFetch. Anything reachable by the shell should be treated as readable by the agent, regardless of the Read-tool block. Deletion you recover from a backup; an exfiltrated secret you cannot, so rotate any credential you think may have been exposed.
- **Plain `rm` of a protected file.** Only `rm -rf` is auto-blocked; a plain `rm` is allowed if you approve the Bash prompt. Snapshots and backups are your recovery path.
- **Outbound HTTP to arbitrary domains.** The app's network egress is not firewalled. WebFetch confirmation is the only gate, and only if "Ask before WebFetch" is on.
- **Anthropic-side data handling.** Covered by your account's terms with Anthropic, not by this app.
- **Physical access to the HAOS host.**
- **A maliciously crafted `CLAUDE.md` or prompt** that tricks Claude into framing a harmful action as benign. The hard-rule list and the backup floor are the real backstops here, not the soft rules.

-----

## Planned

No security features are committed for the next release. The HA backup floor is the load-bearing control; the rules above catch most accidents, and additional guardrails have diminishing returns until backup hygiene is in place.

If you have a specific concern, open an issue.
