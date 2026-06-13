# Changelog

## v0.2.0 — 2026-06-13

First public release. Everything below is the cumulative work since the v0.1.0 private-beta tag.

### Plan mode
- Tap-to-toggle Normal/Plan mode chip in the composer.
- Inline approve / proceed flow with seamless handoff when leaving plan mode mid-turn.
- "Summarize & start fresh" inherits plan mode and model from the source chat.
- Plan-mode CLI quirks (Exit plan mode? confirmation, ExitPlanMode hook matcher, ghost session-ended banners) all handled.

### Permissions
- MCP tool calls now flow through the same permission hook as Bash/WebFetch.
- Permission cards wait indefinitely instead of timing out.
- Plain-English titles on MCP cards.
- Auto-offer "reload HA YAML" as a permission card after an automation/script edit.
- Bash auto-allow list with Select all / Deselect all.

### Mobile push
- Push notification fired on every turn finish when the chat tab is backgrounded.
- Actionable Allow/Reject buttons on permission notifications.
- Conversation search inside the drawer.
- Notification deep-links now open the correct addon panel (was 401/404 on repo-installed addons).

### Stop button
- Hard-kills the CLI process; resets generating state when switching sessions.
- Defence-in-depth on Refine flow.

### iOS keyboard
- Header stays glued to the visible viewport during keyboard show/hide (in-iframe `window.parent.visualViewport` tracking).
- Composer buttons remain tappable while the keyboard is up.
- Layout no longer collapses on empty (new) chats.
- Plan/Normal mode chip stays in place with the keyboard.

### Settings
- Theme toggle (Dark / Light / Match system).
- Delete-all-data button.
- Glass-bar visual refresh.
- Notify-device picker shows live state.
- Settings-saved toast points at the actual chat menu actions.

### Diagnostics
- Tee raw CLI stdout for stuck-spinner diagnosis.
- Fallback to the CLI's own jsonl when stdout silently truncates.
- Synthesize `generation_ended` on terminal `stop_reason`.
- `generation_started` stamped with a server timestamp.

### Security
- AppArmor profile added.
- `hassio_api: true` for the supervisor-aware notification deep-link discovery (requires existing installs to **uninstall + reinstall**, not just update, to grant the new permission).

### UI polish
- WhatsApp-style "+" panel replaces the inline attach + mode chip row.
- Composer trimmed 25% to feel modern.
- Current activity surfaced in the typing caption.

### Docs
- `THIRD_PARTY_LICENSES.md` listing every runtime dep.
- README Acknowledgements section.

## v0.1.0 — 2026-06-10

Private beta tag. Initial scaffold of the addon: PTY-wrapped Claude CLI, SSE chat UI, OAuth login, sessions/projects, image attachments, audit log, hard-coded security policy.
