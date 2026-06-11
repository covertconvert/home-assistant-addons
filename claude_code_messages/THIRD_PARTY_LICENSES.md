# Third-party licenses

Claude Code Messages is itself MIT-licensed (see `LICENSE`). It stands on
the shoulders of a number of third-party projects, none of whose source
is bundled into this repository — they are fetched at addon build time
by `pip`, `npm`, `apt`, or `uvx`. This file is a courtesy notice listing
every direct runtime dependency, the license each carries, and the
upstream URL so anyone auditing the addon can verify the chain.

If a dependency is added or removed, this file should be updated in the
same change.

---

## Python runtime (pip — see `app/requirements.txt`)

| Project | License | Upstream |
|---|---|---|
| [fastapi](https://github.com/fastapi/fastapi) | MIT | Sebastián Ramírez + contributors |
| [uvicorn](https://github.com/encode/uvicorn) `[standard]` | BSD-3-Clause | Encode + contributors |
| [sse-starlette](https://github.com/sysid/sse-starlette) | BSD-3-Clause | sysid + contributors |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 | Marcelo Trylesinski + contributors |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | Samuel Colvin + contributors |
| [aiofiles](https://github.com/Tinche/aiofiles) | Apache-2.0 | Tin Tvrtković + contributors |
| **[pyte](https://github.com/selectel/pyte)** | **LGPL-3.0** | Selectel + contributors |

### Note on pyte (LGPL-3.0)

`pyte` is used by `app/auth.py` for terminal emulation during the Claude
Code CLI OAuth login flow. It is included as an unmodified `pip` install.

In line with **LGPL-3.0 §4–5**:
- We do **not** statically link, vendor, or modify `pyte`'s source. It
  remains an independent, swappable component installed by `pip`.
- A user wishing to replace `pyte` with a modified version can do so by
  building a custom Docker image with their own copy installed, or by
  installing it on top of the existing image at runtime.
- This notice is the "prominent notice" required by LGPL §4(d)(1).

If `pyte` is ever forked or modified inline, the modified copy must
itself be re-licensed under LGPL-3.0 and source must be offered to
recipients of the binary.

---

## Node runtime (npm — installed by `Dockerfile`)

| Project | License | Upstream |
|---|---|---|
| [@anthropic-ai/claude-code](https://github.com/anthropics/claude-code) | Anthropic Commercial Terms (proprietary) | Anthropic, PBC |

The Claude Code CLI is the agent itself. CCM does not redistribute the
package — the `Dockerfile` installs it from the public npm registry at
build time, exactly as any end user would. Use of the CLI is subject to
[Anthropic's Commercial Terms](https://www.anthropic.com/legal/commercial-terms).

---

## Spawned on demand (uvx — runtime only)

| Project | License | Upstream |
|---|---|---|
| [ha-mcp](https://github.com/homeassistant-ai/ha-mcp) | MIT | Julien ([@julienld](https://github.com/julienld)) + homeassistant-ai contributors |

`ha-mcp` is the Model Context Protocol server that powers the optional
"Home Assistant integration" toggle in Settings. When enabled, Claude's
CLI launches it via `uvx ha-mcp` and connects over stdio; it talks to
your Home Assistant instance using the URL + long-lived token you save.

CCM builds the launch command and persists the token securely; the
actual HA-tools layer (entity reads, service calls, automation/script/
dashboard edits, backups) is `ha-mcp`'s implementation, not ours.

---

## Build / install tooling

| Project | License | Upstream |
|---|---|---|
| [uv / uvx](https://github.com/astral-sh/uv) | MIT OR Apache-2.0 | Astral Software Inc. |
| [Node.js](https://github.com/nodejs/node) (apt) | MIT (with bundled libraries under various permissive licenses) | OpenJS Foundation |
| [CPython](https://github.com/python/cpython) (apt) | PSF License (BSD-compatible) | Python Software Foundation |
| [git](https://git-scm.com), [curl](https://curl.se), [jq](https://jqlang.github.io/jq), [gnupg](https://gnupg.org), [ca-certificates](https://packages.debian.org/sid/ca-certificates) | various permissive | distro packages, fetched via apt |

These are installed inside the addon's container at build time; the
addon does not redistribute their binaries.

---

## Base image / addon runtime

| Project | License | Upstream |
|---|---|---|
| [s6-overlay](https://github.com/just-containers/s6-overlay) | ISC | just-containers |
| [bashio](https://github.com/hassio-addons/bashio) | Apache-2.0 | Franck Nijhof / Home Assistant Community Add-ons |

These come from the Home Assistant base image and are not redistributed
by this repository.

---

## Summary

- **No copyleft restriction on CCM's MIT license.** `pyte` is LGPL-3.0
  but only used as an unmodified pip dependency, which the LGPL
  explicitly allows from MIT-licensed callers.
- **No NOTICE-file obligation** is triggered, since we do not vendor any
  Apache-2.0 dependency's source into this repository.
- **No proprietary code is redistributed.** The single proprietary
  runtime dependency (Anthropic's `claude-code` CLI) is installed from
  the public npm registry at addon build time.
