#!/usr/bin/env bash
# Usage: ./rebuild-and-launch.sh ["prompt"] — rebuild, launch, and follow logs.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if (( $# > 1 )); then
  echo "Usage: $0 [\"prompt\"] — quote the prompt as one argument." >&2
  exit 2
fi
if (( $# == 1 )) && [[ ! "$1" =~ [^[:space:]] ]]; then
  echo 'Prompt must not be blank.' >&2
  exit 2
fi

WOW_TOFU="${WOW_TOFU:-$(command -v tofu || true)}"
WOW_TOFU="${WOW_TOFU:-/private/tmp/agent-wow-tofu-omqtgfn4/tofu}"
if [[ ! -x "$WOW_TOFU" ]]; then
  echo "OpenTofu not found. Install tofu or set WOW_TOFU to its executable path." >&2
  exit 1
fi

WOW_TAG="build-$(date -u +%Y%m%dT%H%M%SZ)"
WOW_REGISTRY=143551238491.dkr.ecr.us-west-2.amazonaws.com

# Build the local Dockerfile. Application source changes must be pushed first.
aws ecr get-login-password --region us-west-2 |
  docker login --username AWS --password-stdin "$WOW_REGISTRY"

docker buildx build --platform linux/amd64 \
  --build-arg AGENT_WOW_REF="$(git rev-parse HEAD)" \
  --tag "$WOW_REGISTRY/agent-wow:$WOW_TAG" --push .

# Save the new image version and update AWS (apply asks for confirmation).
printf 'aws_region = "us-west-2"\nimage_tag = "%s"\n' \
  "$WOW_TAG" > terraform.tfvars

"$WOW_TOFU" apply

# Launch the exact updated version, forwarding the optional prompt.
WOW_JOB_DEFINITION="$("$WOW_TOFU" output -raw job_definition)"
export WOW_JOB_DEFINITION
exec ./launch.sh "$@"
