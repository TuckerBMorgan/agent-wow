#!/usr/bin/env bash
set -euo pipefail

# Harbor stops the agent container before running this hook in the observer.
# Read the actual characters over the public game protocol, outside its workspace.
mkdir -p /evidence
for attempt in {1..10}; do
    if agent-wow auth login --timeout 10s > /dev/null \
        && agent-wow char list --json --completed-quests --timeout 60s > /evidence/characters.tmp; then
        mv /evidence/characters.tmp /evidence/characters.json
        exit 0
    fi
    sleep 3
done
exit 1
