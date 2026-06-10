#!/usr/bin/env bash
# Persist OAuth creds and read addon options.
set -e

OPTIONS=/data/options.json
mkdir -p /var/run/s6/container_environment

if [ -f "$OPTIONS" ]; then
  for key in $(jq -r 'keys[]' "$OPTIONS"); do
    value=$(jq -r --arg k "$key" '.[$k]' "$OPTIONS")
    printf '%s' "$value" > "/var/run/s6/container_environment/$key"
  done
fi

# OAuth dir lives under /config so it survives addon updates
mkdir -p /config/claude-config
chmod 700 /config/claude-config
printf '%s' "/config/claude-config" > /var/run/s6/container_environment/CLAUDE_CONFIG_DIR

# Audit log file
AUDIT="${AUDIT_LOG:-/config/claude-code-messages-audit.log}"
touch "$AUDIT"
chmod 600 "$AUDIT"

# Register PreToolUse hook so security.py fires before every Bash/Write/Edit.
# Written every boot so addon updates always ship the current rules.
SETTINGS=/config/claude-config/settings.json
cat > "$SETTINGS" <<'JSON'
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Read|Write|Edit|MultiEdit|NotebookEdit|WebFetch",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /app/hook.py"
          }
        ]
      }
    ]
  }
}
JSON
chmod 600 "$SETTINGS"
