# Changelog

All notable changes for **Claude Code Messages**. Format roughly follows [Keep a Changelog](https://keepachangelog.com/).

## [1.2.0] — 2026-06-25

### Security & destructive-op protection

- Pre-Bash snapshot of `core.config_entries`, `core.entity_registry`, and `core.device_registry` before every shell command — content-dedup means read-only commands (ls, git log, etc.) don't generate noise; a snapshot is only written when the file has actually changed
- Config entry UUID resolution on permission cards — destructive `ha_call_service` calls now show the human-readable integration name and entity count instead of a raw UUID
- Destructive calls render with a red warning card ("Destructive Action"), red consequence text, and a two-tap confirm (first tap arms, second tap fires) so reflex approvals can't cause damage
- Critical integrations require typing the integration name before the Allow button activates — fires when the integration is in the protected list or the entity count meets the configurable threshold
- New "Destructive operation protection" section in Settings: choose which integration types always require type-to-confirm (pre-populated with mesh/hub protocols: MQTT, ZHA, Z-Wave JS, Matter, deCONZ, Thread, HomeKit, Bluetooth) and set your own entity count threshold (default: 25 — adjust down for small setups, up if you regularly tinker with large domains)

### Context window

- Context warning card appears inline in the chat thread at 75% context fill, upgrading to red danger styling at 90%; shows token count, a progress bar, and a "Summarise & start fresh" button to avoid compaction mid-task
- Card updates in-place each turn — no repeated cards, just one that changes state as context fills; dismiss button if you want it out of the way
- Context bar added to the Usage & Model modal alongside the existing 5h and 7d rate-limit bars — shows current chat context fill as a percentage; persists correctly across page reloads and addon restarts

### Home Assistant integration

- Upgraded ha-mcp from 3.5.1 → 7.8.1 — brings in the latest HA tool set and performance improvements
- Fixed permission card labels broken by tool renames in ha-mcp v7
- Enabled tool search so Claude can discover the full HA tool catalogue beyond the core pinned set
- Trimmed the pinned core tool list to reduce context overhead on every turn

### iOS / mobile polish

- Preserve HA sidebar offset on iPad — was overlapping the sidebar on iPad layouts
- Scope keyboard-reposition to iPhone only — was incorrectly clipping the sidebar on iPad
- Use `pvv` height for keyboard on iPad to avoid touching iframe position

---

## [1.1.0] — 2026-06-17

### Chat & generation

- SVG fenced code blocks render inline in chat; Download PNG button exports any diagram
- Context compaction surfaces in the UI with a "Summarising…" caption and a post-compaction banner so it's never silent
- Resume banner shown when a conversation continues after an unexpected CLI respawn
- Per-session draft persistence — your unsent message is restored when you switch back to a chat
- Tap the chat title in the topbar to rename inline without opening a menu
- Chat titles auto-capped and trimmed to word boundaries; live character counter on the rename field

### Drawer & sessions

- Saved Plans — save, browse, and reload plan-mode plans directly from the drawer
- Project reordering via up/down actions in the project context menu
- Session cards show last-activity date in the subtitle

### Usage & model

- Usage & Model panel replaces the old cost modal — model picker and effort selector in one place
- Switch model mid-chat without starting a new session
- Effort level selector: Low / Medium / High / XHigh / Max

### Permission & security

- Permission cards restyled — orange "Permission Required" header, bordered card, compact pill buttons (Allow once / Trust Bash this turn / Reject)
- Blocked tool modal splits the count into "you rejected" vs "security rule" chips with per-category titles
- Audit dashboard in Settings — browsable log with retention controls

### UI redesign

- Floating transparent topbar — frosted background with session name + model pill stacked at centre; split button (✎ new chat | ▾ menu)
- Model pill shows a live usage doughnut (green → amber → red by zone) and opens the Usage & Model modal on tap
- Modernised drawer — frosted search pill, twin-bubble "return to conversation" button, pinned new-chat + settings footer
- Session list as frosted rounded cards per group; accent-filled active row; pill count badges on projects
- Action menu: centred full-bleed rows; destructive actions (Clear context, Delete) shown with red fill and border
- Compose: pale blue send arrow + pale red stop square
- Softer blue-grey slate dark theme — cards lift gently against the background

### Reliability

- Mobile stall fix — client staleness watchdog + `visibilitychange` reconnect; fixes silent stream death on mobile that only recovered by switching chats
- Pending permission cards re-rendered on reconnect so they're never silently lost
- Stop button no longer disappears mid-stream during an interrupt
- Large output crash fix (parser crashed on very long CLI output lines)
- "No response requested" phantom message eliminated (root cause: CLI's built-in resume prompt)
- Optimistic stop — UI updates immediately on tap, doesn't wait for SSE confirmation

---

## [1.0.0] — 2026-06-13

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
