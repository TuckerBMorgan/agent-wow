#!/usr/bin/env bash
# Reuse the arena dev container and its existing Codex login.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f /.dockerenv ]]; then
  exec python3 tools/arena_rounds.py "$@"
fi
container="${ARENA_CONTAINER:-agent-wow-arena-dev}"
docker start "$container" >/dev/null
exec docker exec -i --workdir "$PWD" "$container" python3 tools/arena_rounds.py "$@"
