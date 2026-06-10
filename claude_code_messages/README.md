# Claude Code Messages

A graphical chat interface for the [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI, designed for the Home Assistant companion app. Talk to Claude from your phone with a real messaging UI — not a webterminal.

![icon](logo.png)

## Why

The existing terminal-based Claude addons work, but they're rough on mobile:

- xterm.js is hard to copy/paste from on iOS
- No image attachments
- No clean way to cancel mid-generation
- Permission prompts mixed in with chat output
- No progress visibility

This addon wraps the same `claude` CLI but gives it a proper chat UI: bubbles, code blocks with copy buttons, image attachments, explicit Stop button, and inline approve/reject cards for permission prompts.

## Features

- **OAuth login** — sign in with your Anthropic account; no API key required
- **Multiple conversations** — drawer-based session list, each with its own context
- **Plan mode** — per-conversation toggle to run Claude in read-only planning mode
- **Image attachments** — paste from clipboard, pick from camera roll, or upload a file
- **Markdown rendering** — fenced code blocks with copy buttons, bold/italic/lists/links
- **Permission cards** — inline Approve / Always allow / Reject for Bash and WebFetch
- **Stop button** — cancel mid-generation without killing the session
- **Resume** — pick up a session after the CLI exits or the addon restarts
- **Audit log** — every tool call appended to `/config/claude-code-messages-audit.log`
- **Built-in security policy** — hard-coded rules that block forbidden file reads, destructive Bash, etc. Viewable in Settings.

## Installation

This is a local addon. Drop the folder into your local addons share:

1. SMB into your HA host and copy the `claude_code_messages/` folder into `/addons/`
2. Home Assistant → Settings → Add-ons → Add-on Store → ⋮ → **Check for updates**
3. Find **Claude Code Messages** under "Local add-ons" → Install
4. Start the addon and open it from the sidebar

## First-run authentication

On first open you'll be prompted to authenticate:

1. The addon opens a Claude OAuth link
2. Sign in on the Anthropic site and paste the code back into the addon
3. Token is saved to `/config/claude-config/` (persists across addon updates)

## Configuration options

| Option | Default | Description |
|---|---|---|
| `CLAUDE_WORKDIR` | `/config` | Working directory the CLI is launched in. All Claude tool calls are scoped here. |
| `AUDIT_LOG` | `/config/claude-code-messages-audit.log` | Where to append the tool-call audit log. |
| `MAX_SESSIONS` | `20` | Maximum number of concurrent sessions before old ones are evicted. |

## Settings (in-app)

- **Ask before Bash** (default ON) — show a permission card before any shell command
- **Ask before WebFetch** (default ON) — show a permission card before any outbound HTTP fetch
- **Custom CLAUDE.md notes** — appended to the CLAUDE.md Claude sees, for project-wide guidance
- **View built-in security policy** — opens the hard-coded rules in a modal

## Security

Hard rules can't be bypassed even by an in-app approval. See [SECURITY.md](SECURITY.md) for the full list. Summary:

- Secrets/tokens/credentials are never readable
- Destructive Bash (`rm -rf`, `git reset --hard`, `ha core restart`, etc.) require fresh confirmation every time
- Protected files (`configuration.yaml`, `automations.yaml`, etc.) auto-snapshot before edit, deletion blocked
- Outbound calls limited to the Anthropic API and your HA instance
- No telemetry

## Credit

Inspired by [Claude Terminal for Home Assistant](https://github.com/heytcass/home-assistant-addons) by heytcass. This is a from-scratch implementation, not a fork — it uses an SSE-based chat protocol instead of xterm.js to give a better mobile UX.

## License

MIT — see [LICENSE](LICENSE).
