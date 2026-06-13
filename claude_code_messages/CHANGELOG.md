# Changelog

All notable changes for **Claude Code Messages**. Format roughly follows [Keep a Changelog](https://keepachangelog.com/).

## [1.0.0] — TBD

First public release. Carries everything below.

---

## Pre-release highlights (0.1.x)

The app was developed over ~80 micro-versions on a single test instance before going public. Grouped by theme rather than per-version, since most numbered releases were one small change each.

### Chat & generation

- SSE-based chat protocol with bubbles, copy buttons on code blocks, markdown rendering
- Stop button that actually interrupts mid-generation (hard-kill of the CLI process when needed)
- "Typing" caption shows current activity (model thinking, tool running, etc.)
- Resume a conversation after the app or CLI restarts
- Search across all conversations
- Per-conversation Plan mode toggle with seamless handoff (summarise + start fresh)
- Inline N/P mode chip on the composer; tap to toggle Normal/Plan
- Synthetic `generation_ended` on terminal stop reasons so the UI never gets stuck on a phantom spinner
- Image attachments: clipboard paste, camera roll, file picker

### Permission system

- Inline Approve / Always-allow / Reject cards for every Bash and WebFetch call
- Cards wait indefinitely (no timeout) so a permission prompt is never silently lost
- Bash auto-allow list with select-all / deselect-all
- Plain-English titles on MCP tool permission cards
- Friendly labels on MCP tool runs in chat (e.g. `light.turn_on → light.lounge` instead of `mcp__home-assistant__ha_call_service`)
- HA token validation on save — bad URL or rejected token shows red immediately, not after a 30-second hang

### Drawer / sessions

- Multiple concurrent sessions, grouped under projects
- Projects collapse like a file tree; the Unsorted group collapses too
- Created-date subtitle under each chat title (Today / Yesterday / weekday / DD Mmm)
- Action menus flip upward when there isn't space below — fixes off-screen menus on the last row
- Split-button New Chat with chevron-menu for "New project" / actions
- Sidebar-panel SVG icon to distinguish the drawer toggle from HA's own hamburger

### iOS / mobile polish

- Composer keyboard-up detection via `visualViewport`
- Topbar pinned with `position: fixed` so iOS rubber-band doesn't drag it
- Thread rubber-band contained so the topbar stays put on overscroll
- Composer geometry held across the iOS file picker so the keyboard stays up
- Empty-thread layout grid fix
- Iframe-repositioning gated to iOS only (was clipping the sidebar on desktop Safari)

### Topbar / usage

- Donut ring quota meter for the 5-hour Claude usage window
- 90% banner when the window is near full
- Cost modal opens from the donut, X close + backdrop click both work
- Tightened topbar spacing and split-button layout

### Settings & UX

- WhatsApp-style "+" panel for attachments / Plan toggle / model picker
- Inline model picker (Auto / Opus 4.8 / Sonnet 4.6 / Haiku 4.5) inside the composer panel — no more off-screen floating menus
- Settings toast confirms what actually saved
- Theme toggle (light / dark)
- Glass-bar composer treatment
- Project instructions per project — editable from the project's ⋯ menu

### Notifications

- HA push notifications when a turn finishes (so you can leave the app and get notified)
- Actionable notifications — Approve / Reject permission requests from the notification itself
- Notification deep-links use the addon's real ingress slug, not a guessed one
- Notifications survive backgrounding the app

### Security

- Hard rules enforced in the `PreToolUse` hook (cannot be toggled off):
  - Forbidden files: `secrets.yaml`, `.claude.json`, `.storage/auth*`, `*token*`, `*password*`, `*credential*`, `.env*`
  - Protected files auto-snapshot to `<file>.bak.<timestamp>` before any edit
  - `/addons` is read-only
  - Destructive Bash patterns blocked outright (`rm -rf`, `git reset --hard`, `ha core restart`, `sudo`, pipe-to-shell, etc.)
- Every tool call (allowed and blocked) appended to `/config/claude-code-messages-audit.log`
- AppArmor profile for defence-in-depth
- SECURITY.md rewritten to honestly describe what is and isn't protected, with HA backups as the recovery floor
