# syntax=docker/dockerfile:1.7
# Self-contained Linux workspace for an agent playing WoW through agent-wow.
#
# This file does not copy anything from the host, including existing credentials.
# Build later: docker build -t agent-wow-sandbox .
# After pushing source changes, build with --build-arg AGENT_WOW_REF=<commit-sha>
# to fetch that exact revision instead of reusing a cached branch checkout.
# Run later:   docker run --privileged -it --stop-timeout 120 --name agent-wow-sandbox \
#                --env-file ./config/codex.env \
#                -v agent-wow-workspace:/workspace \
#                -v agent-wow-docker:/var/lib/docker \
#                -v agent-wow-codex:/root/.codex agent-wow-sandbox
#
# --privileged is required for this container's own Docker daemon. Do not mount
# the host Docker socket: gameplay modules need paths in this container.
# Build the agent workspace for the host architecture so Codex's sandbox runs
# natively. Only the AzerothCore services below require amd64 emulation on ARM.
# First startup downloads server images/game data and imports the databases.
# Allow several GB of disk and 10-20 minutes for that first startup.
# config/codex.env supplies CODEX_API_KEY at runtime; browser login is skipped.
# Keep this file private and supply a valid key on each deployment.
# If CODEX_API_KEY is absent, startup reuses a saved ChatGPT login or displays
# a device-login URL/code. That optional login persists in agent-wow-codex.
# The agent builds gameplay modules, creates an orc warrior, and works through
# the Valley of Trials quests. Progress and reports persist under /workspace.
# Append `bash` to the run command to open a shell instead of starting the agent.
# Authentication/CLI reference: https://learn.chatgpt.com/docs/auth
#                            https://learn.chatgpt.com/docs/cli/reference

ARG GO_VERSION=1.27.2
FROM golang:${GO_VERSION}-bookworm AS go-toolchain
FROM node:24-bookworm-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive
ARG CODEX_VERSION=latest
ARG AGENT_WOW_REPOSITORY=https://github.com/TuckerBMorgan/agent-wow.git
ARG AGENT_WOW_REF=main
ARG AZEROTHCORE_REF=master

ENV PATH="/usr/local/go/bin:/opt/venv/bin:/root/go/bin:${PATH}" \
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

RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
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
    && git clone --depth 1 https://github.com/agent-wow/agent-wow-workspace.git /opt/workspace-template \
    && git clone --depth 1 --branch "${AZEROTHCORE_REF}" \
         https://github.com/azerothcore/azerothcore-wotlk.git /opt/azerothcore-reference \
    && mkdir -p /opt/server /workspace

# Debian's /etc/profile resets PATH for the login shells used by Codex.
# Keep the same toolchain available to both ordinary and login shells.
COPY <<'PROFILE' /etc/profile.d/agent-wow-path.sh
export PATH="/usr/local/go/bin:/opt/venv/bin:/root/go/bin:$PATH"
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
    image: acore/ac-wotlk-worldserver:master
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
COPY --chmod=755 <<'PYTHON' /usr/local/bin/prepare-wow-account
#!/opt/venv/bin/python
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import docker

os.umask(0o077)
path = Path('/workspace/config/auth.json')
if not path.exists():
    path.write_text(json.dumps({'username': 'AGENT', 'password': secrets.token_hex(8)}))
account = json.loads(path.read_text())
path.chmod(0o600)

def login():
    return subprocess.run(['agent-wow', 'auth', 'login', '--timeout', '10s'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

if not login():
    client = docker.from_env()
    world = client.containers.list(filters={'label': [
        'com.docker.compose.project=agent-wow-world',
        'com.docker.compose.service=worldserver',
    ]})
    if len(world) != 1:
        raise SystemExit('Expected exactly one worldserver container.')
    # These are generated credentials, restricted to console-safe characters.
    if not all(value.isascii() and value.isalnum() for value in account.values()):
        raise SystemExit('The game account file contains unsupported console characters.')
    connection = world[0].attach_socket(params={'stdin': True, 'stream': True})
    try:
        command = f"account create {account['username']} {account['password']}\n"
        connection._sock.sendall(command.encode())
    finally:
        connection.close()
    for _ in range(30):
        if login():
            break
        time.sleep(2)
    else:
        raise SystemExit('Game account login failed. Inspect the worldserver console; credentials were not changed.')
print('Game account ready. agent-wow is authenticated on the local realm.')
PYTHON

COPY <<'TASK' /opt/orc-quest-task.txt
Create an night elf druid and complete every available quest in the starting area
TASK

COPY --chmod=755 <<'BASH' /usr/local/bin/run-wow-agent
#!/bin/bash
set -Eeuo pipefail
if (( $# > 1 )); then
    echo 'Usage: run-wow-agent ["prompt"] — quote the prompt as one argument.' >&2
    exit 2
fi
if (( $# == 1 )) && [[ ! "$1" =~ [^[:space:]] ]]; then
    echo 'Prompt must not be blank.' >&2
    exit 2
fi
umask 077
mkdir -p /root/.codex /root/.cache /root/.docker /root/go /workspace/logs/agent

# codex exec reads CODEX_API_KEY directly from the runtime environment.
# File storage is used only for the optional ChatGPT login fallback.
codex_cli=(codex --config 'cli_auth_credentials_store="file"')
if [[ ${CODEX_API_KEY+x} ]]; then
    if [[ -z "$CODEX_API_KEY" ]]; then
        echo 'CODEX_API_KEY is empty. Supply a valid key through --env-file ./config/codex.env.' >&2
        exit 1
    fi
    echo 'Using the runtime API key for Codex; browser sign-in is skipped.'
else
    if ! "${codex_cli[@]}" login status >/dev/null 2>&1; then
        echo 'Sign in using the device-login URL and code below. The game task will start after sign-in.'
        if ! "${codex_cli[@]}" login --device-auth; then
            echo 'Codex sign-in failed. Enable device-code login in ChatGPT settings, then restart the container.' >&2
            exit 1
        fi
    fi
    if ! "${codex_cli[@]}" login status >/dev/null 2>&1; then
        echo 'Codex has no saved login. The gameplay agent was not started.' >&2
        exit 1
    fi
fi

run_dir=$(mktemp -d "/workspace/logs/agent/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
if (( $# == 1 )); then
    printf '%s' "$1" > "$run_dir/task.txt"
else
    cp /opt/orc-quest-task.txt "$run_dir/task.txt"
fi
echo "Starting the game task. Run log: $run_dir/agent.log"

# Keep the workspace sandbox, allowing the local Docker socket and build caches.
# Network access is required for the game connection and module dependencies.
# 'never' prevents unattended runs from waiting on approval prompts; denied
# operations still fail inside the configured sandbox.
set +e
"${codex_cli[@]}" --ask-for-approval never \
    --config sandbox_workspace_write.network_access=true \
    exec --sandbox workspace-write --cd /workspace --skip-git-repo-check \
    --add-dir /run --add-dir /root/go --add-dir /root/.cache --add-dir /root/.docker \
    --color never --output-last-message "$run_dir/final-report.txt" \
    - < "$run_dir/task.txt" 2>&1 | tee "$run_dir/agent.log"
pipeline_status=("${PIPESTATUS[@]}")
set -e
status=${pipeline_status[0]}
printf '%s\n' "$status" > "$run_dir/agent-exit-code.txt"
if (( status != 0 )); then
    echo "Codex exited with status $status. Saved progress and logs remain in /workspace." >&2
elif (( pipeline_status[1] != 0 )); then
    echo 'The agent log could not be saved completely.' >&2
    status=1
else
    echo "Agent run finished. Review $run_dir/final-report.txt and /workspace/journal for quest results."
fi
# A successful CLI exit is not independent proof that every quest was completed.
exit "$status"
BASH

RUN /opt/venv/bin/pip install --no-cache-dir boto3

# AWS Batch workers are disposable. Export only reports and quest evidence;
# credentials, databases, and Codex login/session directories stay local.
COPY --chmod=755 <<'PYTHON' /usr/local/bin/run-batch-job
#!/opt/venv/bin/python3
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

import boto3
from botocore.config import Config


class Results:
    def __init__(self, client, bucket, job_id, workspace=Path('/workspace')):
        self.client = client
        self.bucket = bucket
        self.prefix = f'runs/{job_id}'
        self.workspace = workspace
        self.secret = os.environ.get('CODEX_API_KEY', '').encode()
        self.uploaded = {}

    def put(self, key, data):
        if self.secret:
            data = data.replace(self.secret, b'[REDACTED]')
        self.client.put_object(Bucket=self.bucket, Key=f'{self.prefix}/{key}', Body=data)

    def status(self, state, exit_code=None):
        self.put('status.json', json.dumps({
            'state': state,
            'process_exit_code': exit_code,
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'quest_completion': 'Verify server-confirmed turn-ins in journal; exit 0 alone is not proof.',
        }).encode())

    def snapshot(self, deadline=lambda: float('inf')):
        ok = True
        for relative in ('journal', 'logs/agent'):
            root = self.workspace / relative
            if root.resolve() != self.workspace.resolve() / relative or not root.is_dir():
                continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
                for name in files:
                    if time.monotonic() >= deadline():
                        return False
                    path = Path(directory) / name
                    try:
                        # Never follow a report symlink into credentials or read a FIFO/device.
                        path.resolve().relative_to(root.resolve())
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(fd, 'rb') as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode):
                                continue
                            key = path.relative_to(self.workspace).as_posix()
                            signature = (info.st_size, info.st_mtime_ns)
                            if self.uploaded.get(key) == signature:
                                continue
                            data = stream.read()
                        self.put(key, data)
                        self.uploaded[key] = signature
                    except (FileNotFoundError, ValueError):
                        continue
                    except OSError as error:
                        if path.is_symlink():
                            continue
                        print(f'Result export failed ({type(error).__name__}).', flush=True)
                        ok = False
                    except Exception as error:
                        # SDK exception messages can contain request data; log only the type.
                        print(f'Result export failed ({type(error).__name__}).', flush=True)
                        ok = False
        return ok


def run(results, command):
    child = None
    interrupted = 0
    forwarded = False
    stop_by = float('inf')

    def forward_stop():
        nonlocal forwarded
        if interrupted and child is not None and not forwarded:
            forwarded = True
            try:
                child.send_signal(interrupted)
            except ProcessLookupError:
                pass

    def stop(signum, _frame):
        nonlocal interrupted, stop_by
        if not interrupted:
            interrupted = signum
            # Batch allows about 30 seconds before SIGKILL. Export during cleanup.
            stop_by = time.monotonic() + 20
            forward_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Fail before starting an expensive game if the result destination is inaccessible.
    results.status('starting')
    if interrupted:
        return 128 + interrupted
    environment = dict(os.environ, AGENT_WOW_BATCH_SUPERVISED='1')
    child = subprocess.Popen(command, env=environment)
    forward_stop()  # Also handle a signal received while Popen was starting.
    next_export = 0
    while True:
        exit_code = child.poll()
        if interrupted or exit_code is not None or time.monotonic() >= next_export:
            exported = results.snapshot(deadline=lambda: stop_by)
            status = 'interrupted' if interrupted else ('exited' if exit_code is not None else 'running')
            try:
                results.status(status, 128 + interrupted if interrupted else exit_code)
            except Exception as error:
                print(f'Result status export failed ({type(error).__name__}).', flush=True)
                exported = False
            if interrupted:
                try:
                    child.wait(timeout=max(0, stop_by - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
                return 128 + interrupted
            if exit_code is not None:
                # Preserve agent failure; successful execution still requires a final export.
                return (128 - exit_code if exit_code < 0 else exit_code) or (0 if exported else 74)
            next_export = time.monotonic() + 60
        time.sleep(0.5)


def main():
    bucket = os.environ.get('AWS_RESULTS_BUCKET')
    job_id = os.environ.get('AWS_BATCH_JOB_ID')
    if not bucket or not job_id or not os.environ.get('CODEX_API_KEY'):
        print('Batch requires AWS_RESULTS_BUCKET, AWS_BATCH_JOB_ID, and a nonempty CODEX_API_KEY.', flush=True)
        return 1
    client = boto3.client('s3', config=Config(
        connect_timeout=3, read_timeout=5, retries={'total_max_attempts': 1},
    ))
    results = Results(client, bucket, job_id)
    print(f'Results: s3://{bucket}/runs/{job_id}/', flush=True)
    return run(results, ['/usr/local/bin/start-agent-workspace', *sys.argv[1:]])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'Batch supervisor failed ({type(error).__name__}); see CloudWatch logs.', flush=True)
        sys.exit(1)
PYTHON

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
compose=(env -u CODEX_API_KEY docker compose --project-directory /workspace/server -f /workspace/server/compose.yaml)

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
env -u CODEX_API_KEY dockerd --host=unix:///var/run/docker.sock \
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
env -u CODEX_API_KEY prepare-wow-account

cat <<'MESSAGE'

Workspace ready in /workspace.
  agent-wow source:      /workspace/agent-wow
  gameplay modules:      /workspace/modules
  module template:       /workspace/go-module-template
  server source:         /workspace/azerothcore-reference
  logs and records:      /workspace/logs and /workspace/journal

The default command starts the game task using CODEX_API_KEY when supplied.
Pass a quoted prompt to run-wow-agent to override the image's default task.
Without an API key, it uses a saved ChatGPT login or requests device login.
Agent logs and final reports are stored in /workspace/logs/agent.
MESSAGE

# Preserve terminal input while waiting in the supervisor, so TERM triggers
# ordered cleanup instead of simultaneously terminating the nested daemon.
set +e
if [[ "${1:-}" == run-wow-agent ]]; then
    # Give the headless agent, log writer, and game clients one process group
    # so cleanup can stop them all before shutting down the game server.
    setsid --wait "$@" <&0 &
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
