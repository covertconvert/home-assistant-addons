# Claude Code Messages — Security & Privacy

The app enforces a layered defense: hard rules (code-enforced, can't be bypassed), user-controllable prompts (you decide per-action), and soft rules (guide Claude's behaviour but not enforced).

**Last reviewed: 2026-06-11 (v0.1.3)**

---

## Hard rules (enforced — no bypass)

These fire from the `PreToolUse` hook before any tool runs. They cannot be disabled by toggling settings or approving prompts.

### 1. Forbidden files (read & write blocked)

These never get read into Claude's context, never get edited, never get deleted. The hook returns "deny" if Claude tries.

- `/config/secrets.yaml`
- `**/.claude.json` (the Claude CLI's auth file — Claude can't see its own creds)
- Anything matching `**/.storage/auth*`
- Anything matching `**/.storage/onboarding*`
- Anything matching `**/*token*`, `**/*password*`, `**/*credential*`, `**/.env*`

### 2. Read-only paths (writes blocked)

- `/addons` — reads allowed (so Claude can reason about installed apps), but Write/Edit/MultiEdit/NotebookEdit are blocked.

### 3. Protected files (auto-snapshot before edit)

The hook auto-copies these to `<file>.bak.<timestamp>` before any Write/Edit/MultiEdit, so the change is recoverable.

- `/config/configuration.yaml`
- `/config/automations.yaml`
- `/config/scripts.yaml`
- `/config/scenes.yaml`
- `/config/groups.yaml`
- `/config/customize.yaml`
- `/config/ui-lovelace.yaml`
- Any file matching `/config/.storage/lovelace*`

**Note:** deletion of these files via the `Bash` tool (e.g. plain `rm`) is **not** path-blocked — it goes through the standard Bash permission flow (see below). Recovery is via the `.bak.*` snapshot or a HA backup.

### 4. Destructive Bash commands — blocked outright

The hook blocks Bash commands matching any of these patterns. There's no "approve once" — the command is rejected and Claude must take a different path.

- `rm -rf` (any flag combination including `-r`/`-R`/`-f`)
- `git push --force` / `git push -f`
- `git reset --hard`
- `git clean -f`
- `ha core stop` / `ha core restart`
- `ha host reboot` / `ha host shutdown`
- `dd if=…`
- `mkfs.*` / `wipefs`
- Anything invoking `sudo` or `su -`
- `curl … | sh`, `curl … | bash`, `wget … | sh`, etc. (pipe-to-shell)

### 5. Audit log

Every tool call (Read, Edit, Write, Bash, WebFetch, etc.) — both allowed and blocked — is appended to `/config/claude-code-messages-audit.log` with timestamp, tool name, arguments, and outcome. The file is created mode 0600 on first run. Append-only from the app's view; rotate or clear it manually.

---

## User-controllable prompts (in Settings)

These are ON by default. Turn them off only if you trust Claude to act without confirmation in your environment.

- **Ask before Bash** — every shell command shows an Approve / Reject card. Independent of the destructive list above (which is *always* blocked regardless of this setting).
- **Ask before WebFetch** — every outbound HTTP fetch shows a card with the URL. You can pick *Allow once* or *Always allow this domain* (the domain list lives in the app's settings file).

When the toggle is off, calls go straight through with no prompt — still audit-logged.

---

## Soft rules (in `CLAUDE.md`)

These guide Claude's behaviour; they're not enforced by code. Trust them to the same degree you trust Claude to follow instructions.

- Never restart Home Assistant without explicit user approval (reload is fine; restart is not)
- Never run `git commit --no-verify` or otherwise bypass pre-commit hooks
- Confirm understanding before non-trivial edits (restate + propose + ask)
- Don't add features, refactor, or introduce abstractions beyond what the task requires
- Don't create new markdown/README files unless explicitly requested
- Prefer editing existing files over creating new ones
- When in doubt about a destructive action, ask first

---

## Privacy

- OAuth credentials live in the Claude CLI's `.claude.json`. It's in the **Forbidden** list above, so the tool layer can never read it. The rest of `/config/claude-config/` (plans, conversation history) is writable so Claude can manage its own working files there.
- The app does not collect telemetry, usage stats, or crash reports.
- You can revoke OAuth from your Anthropic dashboard at any time; the app respects revocation on the next request.
- The audit log records the *arguments* passed to tools (file paths, command strings, URL hosts). It does **not** record file contents read by Claude or assistant message text.
- Conversation history stays on the HAOS host (`/config/claude-config/`'s jsonl files). Never auto-uploaded anywhere except to Anthropic as part of the conversation itself.
- Image attachments are uploaded to `/data/uploads/` inside the app, sent to Anthropic as part of the message payload, and not pushed anywhere else.

---

## What this does NOT protect against

Being honest about limits:

- **A user who manually approves a destructive prompt.** Ask-before-Bash makes accidents harder, not impossible.
- **Plain `rm` of a protected file** — only `rm -rf` is auto-blocked; plain `rm` is allowed if you approve the Bash prompt. Snapshots are your recovery path.
- **Outbound HTTP to arbitrary domains** — the app's network egress is not firewalled. WebFetch confirmation is the only gate, and only if `ask_webfetch` is on.
- **Anthropic-side data handling** — covered by your account's terms with Anthropic, not by this app.
- **Physical access to the HAOS host.**
- **A maliciously-crafted CLAUDE.md or prompt** that tricks Claude into framing a harmful action as benign. The hard-rule list above is the real backstop here.

---

## Planned (not yet implemented)

Mentioned in earlier drafts but not enforced today. Tracked for a future release:

- Path-based protection on `Bash` (e.g. blocking any command whose arguments contain a protected file path)
- Network egress firewall limiting outbound HTTP to Anthropic + the local HA instance
- Pattern matching for `dd of=…` (currently only `dd if=…` is caught)
- Editing of `/etc/` files via Bash redirection (currently not specifically blocked beyond the `sudo` rule)

---

## Review checklist (before any release)

- [ ] Hard-rule file list is current with HA's file layout
- [ ] Destructive Bash patterns tested against attempt scenarios
- [ ] Audit log path is writable, mode 0600
- [ ] OAuth dir permissions are 0700
- [ ] No new third-party dependencies introduce telemetry
- [ ] CLAUDE.md soft rules reflect current best practice
- [ ] Privacy section accurately describes what's stored and transmitted
- [ ] "Planned" section reflects the current backlog
