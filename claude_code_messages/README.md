# Claude Code Messages

A graphical chat interface for the [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI, designed for the Home Assistant companion app. Talk to Claude from your phone with a real messaging UI — not a webterminal.

![icon](logo.png)

> **Transparency:** this app was built with the help of Claude Code itself. Flagging upfront because I know some people prefer to avoid AI-assisted projects. The architecture and the security rules are mine; a lot of the implementation was a collaboration with the AI. The full security model is in [SECURITY.md](SECURITY.md) and all the code is open in this repo.

## Why

The existing terminal-based Claude apps work, but they're rough on mobile:

- xterm.js is hard to copy/paste from on iOS
- No image attachments
- No clean way to cancel mid-generation
- Permission prompts mixed in with chat output
- No progress visibility

This app wraps the same `claude` CLI but gives it a proper chat UI: bubbles, code blocks with copy buttons, image attachments, explicit Stop button, and inline approve/reject cards for permission prompts.

## Requirements

**Required**

- A **Claude account** with an active Claude Code subscription (Pro, Max, or Team). The app uses your subscription via OAuth — there is no usage-based API key required and your subscription billing applies as normal. Sign up at [claude.com](https://claude.com) if you don't already have one.

**Optional but recommended**

- A **Home Assistant long-lived access token**, if you want Claude to read entity states, call services, edit automations/scripts/dashboards, or trigger backups. Without it, Claude can still edit YAML files in `/config` but won't be able to interact with HA's live state.
  - Generate one from **HA → Profile (your avatar) → Security → Long-lived access tokens → Create token**
  - Paste it into the app under **Settings → Home Assistant integration**

## Features

- **OAuth login** — sign in with your Anthropic account; no API key required
- **Multiple conversations** — drawer-based session list, each with its own context
- **Plan mode** — per-conversation toggle to run Claude in read-only planning mode
- **Image attachments** — paste from clipboard, pick from camera roll, or upload a file
- **Markdown rendering** — fenced code blocks with copy buttons, bold/italic/lists/links
- **Permission cards** — inline Approve / Always allow / Reject for Bash and WebFetch
- **Stop button** — cancel mid-generation without killing the session
- **Resume** — pick up a session after the CLI exits or the app restarts
- **Audit log** — every tool call appended to `/config/claude-code-messages-audit.log`
- **Built-in security policy** — hard-coded rules that block forbidden file reads, destructive Bash, etc. Viewable in Settings.

## Installation

1. In Home Assistant: **Settings → Apps → Install apps** (blue button) → **⋮ → Repositories**
2. Paste `https://github.com/covertconvert/home-assistant-addons` → **Add**
3. Refresh the store; find **Claude Code Messages** under "Home Assistant Apps"
4. Install → Start → open from the sidebar

## First-run authentication

On first open you'll be prompted to authenticate your Claude account:

1. The app opens a Claude OAuth link
2. Sign in on the Anthropic site and paste the code back into the app
3. Token is saved to `/config/claude-config/` (persists across app updates)

Then, if you want Claude to talk to Home Assistant (recommended):

1. Open **Settings → Home Assistant integration**
2. Paste your HA URL (e.g. `http://homeassistant.local:8123`) and the long-lived token from your HA profile
3. Toggle the integration on — Claude can now read entity states, call services, and edit your automations

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

- Secrets/tokens/credentials are never readable (including your HA token and the app's own OAuth)
- Destructive Bash (`rm -rf`, `git reset --hard`, `ha core restart`, etc.) is blocked outright
- Protected files (`configuration.yaml`, `automations.yaml`, etc.) auto-snapshot to `<file>.bak.<timestamp>` before any edit
- Every tool call is appended to an audit log at `/config/claude-code-messages-audit.log`
- No telemetry, ever

⚠ Make sure your Home Assistant instance is backed up before connecting Claude to it. AI can make mistakes — automations, scripts, and dashboards can be modified or deleted.

## Credit

Inspired by [Claude Terminal for Home Assistant](https://github.com/heytcass/home-assistant-addons) by heytcass. This is a from-scratch implementation, not a fork — it uses an SSE-based chat protocol instead of xterm.js to give a better mobile UX.

## Acknowledgements

This add-on is the chat UI, session plumbing, permission system, and HA glue — all written from scratch. The heavy lifting on a few external pieces is done by:

- **[Claude Code CLI](https://github.com/anthropics/claude-code)** by Anthropic — the agent itself. CCM spawns and wraps `claude` in stream-json mode.
- **[ha-mcp](https://github.com/homeassistant-ai/ha-mcp)** by Julien ([@julienld](https://github.com/julienld)) and the homeassistant-ai contributors — the Model Context Protocol server that lets Claude read entity states, call services, and edit HA config when you enable the Home Assistant integration. Launched on demand via `uvx ha-mcp`. MIT-licensed.
- **[FastAPI](https://github.com/fastapi/fastapi)** + **[sse-starlette](https://github.com/sysid/sse-starlette)** — the backend server and the SSE streaming layer.
- **[uv / uvx](https://github.com/astral-sh/uv)** by Astral — used to fetch and run `ha-mcp` without polluting the addon image.

All listed projects are independent of CCM and carry their own licenses. Full per-dependency license breakdown is in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## License

MIT — see [LICENSE](LICENSE).
