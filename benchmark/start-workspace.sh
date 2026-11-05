#!/usr/bin/env bash
set -euo pipefail

# The agent controls this trial's nested engine, never the host Docker socket.
dockerd --host=unix:///var/run/docker.sock --storage-driver=overlay2 \
    > /workspace/logs/dockerd.log 2>&1 &
for attempt in {1..60}; do
    if docker info > /dev/null 2>&1; then
        exec "$@"
    fi
    sleep 1
done
cat /workspace/logs/dockerd.log >&2
exit 1
