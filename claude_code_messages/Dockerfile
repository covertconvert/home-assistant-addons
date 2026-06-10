ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    NODE_MAJOR=20

# System deps: Node 20, Python, jq, git (claude often shells to git)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg jq git python3 python3-pip python3-venv \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Python backend
WORKDIR /app
COPY app/requirements.txt /app/requirements.txt
RUN python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt \
    && python3 -m pip install --break-system-packages --no-cache-dir uv

COPY app/ /app/
COPY rootfs/ /

RUN chmod +x /etc/services.d/backend/run \
    && chmod +x /etc/cont-init.d/00-init.sh

# OAuth/config persistence dir — bind via HOME at runtime
ENV CLAUDE_CONFIG_DIR=/config/claude-config

EXPOSE 8099
