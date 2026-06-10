# Home Assistant Apps

## Claude Code Messages

<img src="claude_code_messages/logo.png" align="right" width="160" alt="Claude Code Messages logo">

**A mobile-first chat interface for [Claude Code](https://docs.claude.com/en/docs/claude-code), inside Home Assistant.**

Talk to Claude from your phone with a proper messaging UI — not a webterminal. Edit your automations, debug your dashboards, control your devices, all from the HA companion app on the sofa.

> **Transparency:** this app was built with the help of Claude Code itself. Flagging upfront because I know some people prefer to avoid AI-assisted projects. The architecture and the security rules are mine; a lot of the implementation was a collaboration with the AI. The full security model is in [SECURITY.md](./claude_code_messages/SECURITY.md) and all the code is open in this repo.

### Why this exists

Existing Claude-on-HA apps run the CLI inside an xterm.js webterminal. That works on a laptop. On a phone it's painful:

- Copy/paste is awful in iOS Safari
- No image attachments — can't paste a screenshot of your dashboard
- No clean way to cancel a long generation
- Permission prompts mixed in with chat output
- No idea when Claude is thinking vs done

Claude Code Messages wraps the same `claude` CLI but exposes it as a real chat UI: bubbles, code blocks with copy buttons, image attachments from your camera roll, an explicit Stop button, and inline Approve / Reject cards for every Bash and WebFetch.

### What you can do with it

- **"Add a motion-triggered automation that turns the porch light on after sunset"** → Claude edits `automations.yaml`, snapshots a backup first, restarts the automation domain
- **"Why's the bedroom thermostat showing as unavailable?"** → Claude greps the HA log, checks the device's state, walks you through the integration page
- **"Build me a dashboard for my solar inverter"** → Paste a screenshot of the data you want, Claude generates the YAML, you commit it via the file editor app
- **"Run a backup before I do anything else"** → Single sentence; backup runs

All of this from your phone, on the way to bed.

### Features

- **OAuth login** with your Claude account — no API key needed; your existing Claude subscription (Pro / Max / Team) is what's charged
- **Multiple parallel conversations**, each with its own context
- **Plan mode** toggle — let Claude think through an approach without touching files
- **Image attachments** — clipboard paste, camera roll, or file picker
- **Markdown rendering** with copy buttons on every code block
- **Inline permission cards** — Approve / Always allow this domain / Reject
- **Stop button** that actually stops mid-generation
- **Resume** — pick a session back up after the app restarts
- **Audit log** at `/config/claude-code-messages-audit.log` — every tool call, every block, every snapshot
- **Hard-coded security rules** that you can review in Settings — secrets and auth files are forbidden, destructive shell commands are blocked, protected files auto-snapshot before edit

### Requirements

- A [Claude account](https://claude.com) with an active Claude Code subscription (Pro, Max, or Team)
- For full HA control: a [long-lived access token](https://www.home-assistant.io/docs/authentication/#your-account-profile) (HA → Profile → Security)

### Install

1. Home Assistant → **Settings → Apps → Install apps** (blue button) → **⋮ → Repositories**
2. Paste `https://github.com/covertconvert/home-assistant-addons` → **Add**
3. Refresh the store; find **Claude Code Messages** under "Home Assistant Apps"
4. Install → Start → open from the sidebar

Full details, configuration, and security model: **[Claude Code Messages README](./claude_code_messages/README.md)** · **[SECURITY.md](./claude_code_messages/SECURITY.md)**

---

## Credit

Inspired by [Claude Terminal for Home Assistant](https://github.com/heytcass/home-assistant-addons) by heytcass. From-scratch implementation, not a fork — SSE chat protocol instead of xterm.js, designed mobile-first.

## License

MIT — see each app's `LICENSE`.
