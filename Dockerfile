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
    && pip install --no-cache-dir grpcio grpcio-tools protobuf requests PyYAML docker

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
    python3 -c "import grpc, grpc_tools, google.protobuf, requests, yaml, docker"'

# These services run inside the nested daemon, so 127.0.0.1 is the same
# loopback address used by agent-wow and the default AzerothCore realmlist.
# MySQL is accessible only on the inner Compose network.
COPY <<'YAML' /opt/server/compose.yaml
name: agent-wow-world
x-game-env: &game-env
  AC_LOGIN_DATABASE_INFO: "database;3306;root;${MYSQL_ROOT_PASSWORD};acore_auth"
  AC_WORLD_DATABASE_INFO: "database;3306;root;${MYSQL_ROOT_PASSWORD};acore_world"
  AC_CHARACTER_DATABASE_INFO: "database;3306;root;${MYSQL_ROOT_PASSWORD};acore_characters"
  AC_DATA_DIR: /azerothcore/env/dist/data
services:
  database:
    image: mysql:8.4
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD:?Database password is required}
    volumes:
      - database:/var/lib/mysql
    healthcheck:
      # The temporary initialization server accepts sockets but not TCP.
      # Require a successful TCP query before starting the importer.
      test: [CMD-SHELL, 'mysql --protocol=TCP -h 127.0.0.1 -P 3306 -uroot -p"$$MYSQL_ROOT_PASSWORD" --execute="SELECT 1" >/dev/null 2>&1']
      interval: 5s
      timeout: 5s
      retries: 120
  db-import:
    image: acore/ac-wotlk-db-import:master
    platform: linux/amd64
    environment: *game-env
    volumes:
      - configuration:/azerothcore/env/dist/etc
    depends_on:
      database:
        condition: service_healthy
  client-data:
    image: acore/ac-wotlk-client-data:master
    platform: linux/amd64
    volumes:
      - client-data:/azerothcore/env/dist/data
  authserver:
    image: acore/ac-wotlk-authserver:master
    platform: linux/amd64
    environment: *game-env
    ports:
      - 127.0.0.1:3724:3724
    volumes:
      - configuration:/azerothcore/env/dist/etc
    healthcheck:
      test: [CMD, bash, -c, 'exec 3<>/dev/tcp/127.0.0.1/3724']
      interval: 5s
      timeout: 3s
      retries: 120
    depends_on:
      database:
        condition: service_healthy
      db-import:
        condition: service_completed_successfully
  worldserver:
    image: ${AGENT_WOW_WORLD_IMAGE:-acore/ac-wotlk-worldserver:master}
    platform: linux/amd64
    environment: *game-env
    stdin_open: true
    tty: true
    ports:
      - 127.0.0.1:8085:8085
    volumes:
      - configuration:/azerothcore/env/dist/etc
      - client-data:/azerothcore/env/dist/data:ro
    healthcheck:
      test: [CMD, bash, -c, 'exec 3<>/dev/tcp/127.0.0.1/8085']
      interval: 5s
      timeout: 3s
      retries: 360
    depends_on:
      authserver:
        condition: service_healthy
      database:
        condition: service_healthy
      db-import:
        condition: service_completed_successfully
      client-data:
        condition: service_completed_successfully
volumes:
  database:
  configuration:
  client-data:
YAML

# Provision a regular player account using the documented server console.
# No game database writes, GM privileges, character creation, or quest cheats.
# Credentials are generated on first launch, not baked into the image.
# The server's interactive console may echo its account-creation command;
# treat the nested server logs as private alongside the saved account file.
COPY --chmod=755 docker/prepare_wow_account.py /usr/local/bin/prepare-wow-account
COPY --chmod=755 docker/prepare_viewer.py /usr/local/bin/prepare-wow-viewer

COPY <<'TASK' /opt/orc-quest-task.txt
Create an night elf druid and complete every available quest in the starting area
TASK

COPY --chmod=755 docker/run_codex.py /usr/local/bin/run-wow-codex
COPY --chmod=755 docker/run_claude.py /usr/local/bin/run-wow-claude
COPY --chmod=755 <<'BASH' /usr/local/bin/run-wow-agent
#!/bin/bash
set -Eeuo pipefail
case "${AGENT_WOW_HARNESS:-codex}" in
    codex) exec run-wow-codex "$@" ;;
    claude) exec run-wow-claude "$@" ;;
    *) echo 'AGENT_WOW_HARNESS must be codex or claude.' >&2; exit 2 ;;
esac
BASH

RUN /opt/venv/bin/pip install --no-cache-dir boto3

# AWS Batch supplies the selected account credential through its execution role.
# Workers are disposable. Export only reports and quest evidence;
# credentials, databases, and Codex login/session directories stay local.
COPY --chmod=755 docker/run_batch_job.py /usr/local/bin/run-batch-job

COPY --chmod=755 <<'BASH' /usr/local/bin/start-agent-workspace
#!/bin/bash
set -Eeuo pipefail
umask 077

if [[ -n "${AWS_BATCH_JOB_ID:-}" && "${AGENT_WOW_BATCH_SUPERVISED:-}" != 1 ]]; then
    exec /usr/local/bin/run-batch-job "$@"
fi

# Bootstrap a named workspace volume only when it is empty.
mkdir -p /workspace/{config,data,modules,scripts,journal,logs,server,tmp}
if [[ ! -d /workspace/agent-wow/.git ]]; then
    cp -a /opt/agent-wow /workspace/agent-wow
fi
if [[ ! -f /workspace/AGENTS.md ]]; then
    cp /opt/workspace-template/AGENTS.md /workspace/AGENTS.md
fi
# Both harnesses read the same workspace instructions.
if [[ ! -e /workspace/CLAUDE.md ]]; then
    ln -s AGENTS.md /workspace/CLAUDE.md
fi
if [[ ! -d /workspace/go-module-template ]]; then
    cp -a /opt/go-module-template /workspace/go-module-template
fi
if [[ ! -e /workspace/azerothcore-reference ]]; then
    ln -s /opt/azerothcore-reference /workspace/azerothcore-reference
fi
if [[ ! -f /workspace/server/.env ]]; then
    printf 'MYSQL_ROOT_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" \
        > /workspace/server/.env
fi
cp /opt/server/compose.yaml /workspace/server/compose.yaml
compose=(env -u CODEX_AUTH_JSON -u CLAUDE_CODE_OAUTH_TOKEN docker compose --project-directory /workspace/server -f /workspace/server/compose.yaml)

# Start a real local daemon so module bind mounts resolve in this filesystem.
# Share image layers on the dedicated ext4 /var/lib/docker volume.
if mountpoint -q /var/run/docker.sock || docker info >/dev/null 2>&1; then
    echo 'Refusing a mounted or active Docker socket. Use a dedicated nested daemon.' >&2
    exit 1
fi
if [[ -f /var/run/docker.pid ]] && kill -0 "$(cat /var/run/docker.pid)" 2>/dev/null; then
    echo 'A Docker daemon process is already running; refusing a second daemon.' >&2
    exit 1
fi
# Stale local files can remain after a forced stop; neither is a host mount.
rm -f /var/run/docker.sock /var/run/docker.pid
mkdir -p /var/lib/docker
child_pid=''
child_is_group=false
daemon_pid=''
cleanup() {
    trap - EXIT INT TERM
    if [[ -n "$child_pid" ]]; then
        child_target="$child_pid"
        if [[ "$child_is_group" == true ]]; then child_target="-$child_pid"; fi
        kill -TERM -- "$child_target" 2>/dev/null || true
        for ((i=0; i<10; i++)); do
            kill -0 -- "$child_target" 2>/dev/null || break
            sleep 1
        done
        kill -KILL -- "$child_target" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
    "${compose[@]}" stop --timeout 30 >/dev/null 2>&1 || true
    if [[ -n "$daemon_pid" ]]; then
        kill -TERM "$daemon_pid" 2>/dev/null || true
        for ((i=0; i<30; i++)); do
            kill -0 "$daemon_pid" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$daemon_pid" 2>/dev/null || true
        wait "$daemon_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
env -u CODEX_AUTH_JSON -u CLAUDE_CODE_OAUTH_TOKEN dockerd --host=unix:///var/run/docker.sock \
    --feature=containerd-snapshotter=false --storage-driver=overlay2 \
    > /workspace/logs/dockerd.log 2>&1 &
daemon_pid=$!
for ((i=0; i<120; i++)); do
    docker info >/dev/null 2>&1 && break
    kill -0 "$daemon_pid" 2>/dev/null || break
    sleep 1
done
if ! docker info >/dev/null 2>&1; then
    echo 'Docker daemon failed. Check --privileged and /workspace/logs/dockerd.log.' >&2
    exit 1
fi

env -u CODEX_AUTH_JSON -u CLAUDE_CODE_OAUTH_TOKEN prepare-wow-viewer --image
"${compose[@]}" up -d
service_ready() {
    local container_id
    container_id=$("${compose[@]}" ps -q "$1")
    [[ -n "$container_id" ]] && \
        [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container_id")" == healthy ]]
}
ready=false
for ((i=0; i<900; i++)); do
    if service_ready authserver && service_ready worldserver; then
        ready=true
        break
    fi
    sleep 2
done
if [[ "$ready" != true ]]; then
    echo 'Game server startup timed out. Inspect docker compose logs in /workspace/server.' >&2
    exit 1
fi
env -u CODEX_AUTH_JSON -u CLAUDE_CODE_OAUTH_TOKEN prepare-wow-account
env -u CODEX_AUTH_JSON -u CLAUDE_CODE_OAUTH_TOKEN prepare-wow-viewer --account

cat <<'MESSAGE'

Workspace ready in /workspace.
  agent-wow source:      /workspace/agent-wow
  gameplay modules:      /workspace/modules
  module template:       /workspace/go-module-template
  server source:         /workspace/azerothcore-reference
  logs and records:      /workspace/logs and /workspace/journal

The default command starts Codex with your saved ChatGPT login.
Set AGENT_WOW_HARNESS=claude to use your Claude account instead.
Pass a quoted prompt to run-wow-agent to override the image's default task.
Agent logs and final reports are stored in /workspace/logs/agent.
MESSAGE

# Preserve terminal input while waiting in the supervisor, so TERM triggers
# ordered cleanup instead of simultaneously terminating the nested daemon.
set +e
if [[ "${1:-}" == run-wow-agent ]]; then
    # Give the headless agent, log writer, and game clients one process group
    # so cleanup can stop them all before shutting down the game server.
    if [[ "${AGENT_WOW_HARNESS:-codex}" == claude ]]; then
        # Claude Code requires an unprivileged user for unattended tool execution.
        mkdir -p /home/node/{.claude,.cache,.docker,go}
        chown -R node:node /workspace /home/node
        setsid --wait runuser -u node -- "$@" <&0 &
    else
        setsid --wait "$@" <&0 &
    fi
    child_is_group=true
else
    "$@" <&0 &
fi
child_pid=$!
wait "$child_pid"
status=$?
child_pid=''
set -e
exit "$status"
BASH

WORKDIR /workspace
ENV TMPDIR=/workspace/tmp
VOLUME ["/workspace", "/var/lib/docker", "/root/.codex"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-agent-workspace"]
CMD ["run-wow-agent"]
