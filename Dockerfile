# syntax=docker/dockerfile:1.7
# Self-contained Linux workspace for an agent playing WoW through agent-wow.
#
# Runtime helpers are copied from docker/; application source is fetched remotely.
# Credential files are excluded from the build context.
# Build later: docker build -t agent-wow-sandbox .
# After pushing source changes, build with --build-arg AGENT_WOW_REF=<commit-sha>
# to fetch that exact revision instead of reusing a cached branch checkout.
# Run with a saved account credential supplied through --env-file ./config/account.env.
# Select AGENT_WOW_HARNESS=codex (default) or claude and pass a quoted task:
# docker run --privileged --stop-timeout 120 --env-file ./config/account.env \
#   -v agent-wow-workspace:/workspace -v agent-wow-docker:/var/lib/docker \
#   agent-wow-sandbox run-wow-agent "your task"
# Codex requires CODEX_AUTH_JSON; Claude requires CLAUDE_CODE_OAUTH_TOKEN.
# The container starts its own Docker daemon; do not mount the host socket.
# Reports and journal records persist under /workspace and are exported to S3 in Batch.

ARG GO_VERSION=1.27.2
FROM golang:${GO_VERSION}-bookworm AS go-toolchain
FROM node:24-bookworm-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive
ARG CODEX_VERSION=0.163.0
ARG CLAUDE_VERSION=2.1.306
ARG AGENT_WOW_REPOSITORY=https://github.com/TuckerBMorgan/agent-wow.git
ARG AGENT_WOW_REF=main
ARG AZEROTHCORE_REF=master

ENV PATH="/usr/local/go/bin:/opt/venv/bin:/root/go/bin:/home/node/go/bin:${PATH}" \
    DOCKER_HOST=unix:///var/run/docker.sock \
    AGENT_WOW_CONFIG_DIR=/workspace/config \
    AGENT_WOW_DATA_DIR=/workspace/data \
    AGENT_WOW_MODULE_DIR=/workspace/modules \
    AGENT_WOW_AUTHSERVER_HOST=127.0.0.1 \
    AGENT_WOW_AUTHSERVER_PORT=3724 \
    AGENT_WOW_WORLDRPC_HOST=127.0.0.1 \
    AGENT_WOW_WORLDRPC_PORT=8086 \
    PYTHONUNBUFFERED=1

COPY --from=go-toolchain /usr/local/go /usr/local/go

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git openssh-client gnupg jq ripgrep \
      build-essential cmake ninja-build pkg-config clang libssl-dev zlib1g-dev \
      protobuf-compiler python3 python3-dev python3-pip python3-venv \
      unzip zip less nano procps iproute2 iptables netcat-openbsd tini util-linux bubblewrap \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
         -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --global "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && npm cache clean --force \
    && (getent group docker >/dev/null || groupadd --system docker) \
    && usermod -aG docker node \
    && python3 -m venv /opt/venv \
    && pip install --no-cache-dir grpcio grpcio-tools protobuf requests PyYAML docker boto3

# Clone the public code at build time; retain it for the agent to inspect/edit.
RUN git init /opt/agent-wow \
    && git -C /opt/agent-wow remote add origin "${AGENT_WOW_REPOSITORY}" \
    && git -C /opt/agent-wow fetch --depth 1 origin "${AGENT_WOW_REF}" \
    && git -C /opt/agent-wow checkout --detach FETCH_HEAD \
    && cd /opt/agent-wow \
    && go mod download \
    && go build -trimpath -o /usr/local/bin/agent-wow . \
    && GOBIN=/usr/local/bin go install google.golang.org/protobuf/cmd/protoc-gen-go \
    && GOBIN=/usr/local/bin go install google.golang.org/grpc/cmd/protoc-gen-go-grpc

RUN git clone --depth 1 https://github.com/agent-wow/go-module-template.git /opt/go-module-template \
    && git clone --depth 1 https://github.com/TuckerBMorgan/agent-wow-workspace.git /opt/workspace-template \
    && git clone --depth 1 --branch "${AZEROTHCORE_REF}" \
         https://github.com/azerothcore/azerothcore-wotlk.git /opt/azerothcore-reference \
    && mkdir -p /opt/server /workspace

# Debian's /etc/profile resets PATH for the login shells used by Codex.
# Keep the same toolchain available to both ordinary and login shells.
COPY <<'PROFILE' /etc/profile.d/agent-wow-path.sh
export PATH="/usr/local/go/bin:/opt/venv/bin:$HOME/go/bin:$PATH"
PROFILE

# Catch missing toolchain paths during the build, before starting a game run.
RUN /bin/bash -lc 'set -eu; \
    test "$(command -v go)" = /usr/local/go/bin/go; \
    go version; \
    for tool in gofmt protoc-gen-go protoc-gen-go-grpc; do command -v "$tool"; done; \
    test "$(command -v python3)" = /opt/venv/bin/python3; \
    python3 -c "import grpc, grpc_tools, google.protobuf, requests, yaml, docker, boto3"'

# These services run inside the nested daemon, so 127.0.0.1 is the same
# loopback address used by agent-wow and the default AzerothCore realmlist.
# MySQL is accessible only on the inner Compose network.
COPY docker/compose.yaml /opt/server/compose.yaml

# Provision a regular player account using the documented server console.
# No game database writes, GM privileges, character creation, or quest cheats.
# Credentials are generated on first launch, not baked into the image.
# The server's interactive console may echo its account-creation command;
# treat the nested server logs as private alongside the saved account file.
COPY --chmod=755 docker/prepare_wow_account.py /usr/local/bin/prepare_wow_account.py
# Spectator implementation is maintained in the separate viewer repository.
ARG VIEWER_REF=3e5f0adaa8f820ad46c944a0d27299b18710f404
ADD --chmod=755 https://raw.githubusercontent.com/TuckerBMorgan/agent-wow-viewer/${VIEWER_REF}/docker/prepare_viewer.py /usr/local/bin/prepare-wow-viewer

COPY <<'TASK' /opt/default-task.txt
Create an night elf druid and complete every available quest in the starting area
TASK

COPY agent_common.py benchmark_results.py /usr/local/bin/
COPY --chmod=755 docker/run_agent.py /usr/local/bin/run-wow-agent

# AWS Batch supplies the selected account credential through its execution role.
# Workers are disposable. Export only reports and quest evidence;
# credentials, databases, and Codex login/session directories stay local.
COPY --chmod=755 docker/run_batch_job.py /usr/local/bin/run-batch-job

COPY --chmod=755 docker/start-agent-workspace.sh /usr/local/bin/start-agent-workspace

WORKDIR /workspace
ENV TMPDIR=/workspace/tmp
VOLUME ["/workspace", "/var/lib/docker", "/root/.codex"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-agent-workspace"]
CMD ["run-wow-agent"]
