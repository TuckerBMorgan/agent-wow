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
stop_process() {
    local pid="$1" timeout="$2" group="${3:-false}" target="$1" i
    [[ -n "$pid" ]] || return 0
    if [[ "$group" == true ]]; then target="-$pid"; fi
    kill -TERM -- "$target" 2>/dev/null || true
    for ((i=0; i<timeout; i++)); do
        kill -0 -- "$target" 2>/dev/null || break
        sleep 1
    done
    kill -KILL -- "$target" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}
cleanup() {
    trap - EXIT INT TERM
    stop_process "$child_pid" 10 "$child_is_group"
    "${compose[@]}" stop --timeout 30 >/dev/null 2>&1 || true
    stop_process "$daemon_pid" 30
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
"${compose[@]}" up --wait --wait-timeout 1800
env -u CODEX_AUTH_JSON -u CLAUDE_CODE_OAUTH_TOKEN prepare_wow_account.py
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
