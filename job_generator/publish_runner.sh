
#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
  echo "Usage: publish_runner.sh [COMMIT]" >&2
  exit 1
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
cd "$repo_root"

RUNNER_COMMIT=$(git rev-parse --verify "${1:-HEAD}^{commit}")
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/agent-wow-build.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT
git clone --quiet --no-checkout --no-hardlinks "$repo_root" "$build_dir/source"
git -C "$build_dir/source" checkout --quiet --detach "$RUNNER_COMMIT"
printf 'Building runtime images from commit %s\n' "$RUNNER_COMMIT"

# Setup and run the tofu files
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
export TF_VAR_artifacts_bucket_name="${TF_VAR_artifacts_bucket_name:-agent-wow-artifacts-143551238491-us-west-2}"

tofu -chdir="$script_dir" init
tofu -chdir="$script_dir" apply -target=aws_ecr_repository.runner

RUNNER_REPOSITORY="$(tofu -chdir="$script_dir" output -raw runner_repository_url)"
RUNNER_REGISTRY="${RUNNER_REPOSITORY%%/*}"
RUNNER_TAG="benchmark-v3-server-$(date -u +%Y%m%dT%H%M%SZ)"
CLIENT_TAG="${RUNNER_TAG/server/client}"

# log into aws
aws ecr get-login-password --region "$AWS_REGION" |
  docker login --username AWS --password-stdin "$RUNNER_REGISTRY"

# Both Dockerfiles and their source come from the same committed checkout.
for role in server client; do
  tag="$RUNNER_TAG"
  if [[ "$role" == client ]]; then tag="$CLIENT_TAG"; fi
  docker buildx build \
    --platform linux/amd64 \
    --file "$build_dir/source/Dockerfile.$role" \
    --label "org.opencontainers.image.revision=$RUNNER_COMMIT" \
    --tag "$RUNNER_REPOSITORY:$tag" \
    --progress plain \
    --push \
    "$build_dir/source"
done

mkdir -p "$repo_root/tmp"
printf '%s\n' "$AWS_REGION" "$RUNNER_REPOSITORY" "$RUNNER_TAG" "$CLIENT_TAG" \
  > "$repo_root/tmp/runner-image.txt"
printf 'Uploaded %s:%s\n' "$RUNNER_REPOSITORY" "$RUNNER_TAG"

aws ecr describe-images \
  --region "$AWS_REGION" \
  --registry-id "${RUNNER_REGISTRY%%.*}" \
  --repository-name "${RUNNER_REPOSITORY##*/}" \
  --image-ids "imageTag=$RUNNER_TAG" \
  --query 'imageDetails[0].imageDigest' \
  --output text
