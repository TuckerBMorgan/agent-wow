
#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
cd "$repo_root"

# Setup and run the tofu files
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
export TF_VAR_artifacts_bucket_name="${TF_VAR_artifacts_bucket_name:-agent-wow-artifacts-143551238491-us-west-2}"

tofu -chdir="$script_dir" init
tofu -chdir="$script_dir" apply -target=aws_ecr_repository.runner

RUNNER_REPOSITORY="$(tofu -chdir="$script_dir" output -raw runner_repository_url)"
RUNNER_REGISTRY="${RUNNER_REPOSITORY%%/*}"
RUNNER_TAG="benchmark-v3-$(date -u +%Y%m%dT%H%M%SZ)"
RUNNER_COMMIT="$(git rev-parse HEAD)"

# log into aws
aws ecr get-login-password --region "$AWS_REGION" |
  docker login --username AWS --password-stdin "$RUNNER_REGISTRY"

# build the docker image
docker buildx build \
  --platform linux/amd64 \
  --build-arg "AGENT_WOW_REF=$RUNNER_COMMIT" \
  --tag "$RUNNER_REPOSITORY:$RUNNER_TAG" \
  --progress plain \
  --push \
  .

mkdir -p "$repo_root/tmp"
printf '%s\n' "$AWS_REGION" "$RUNNER_REPOSITORY" "$RUNNER_TAG" \
  > "$repo_root/tmp/runner-image.txt"
printf 'Uploaded %s:%s\n' "$RUNNER_REPOSITORY" "$RUNNER_TAG"

aws ecr describe-images \
  --region "$AWS_REGION" \
  --registry-id "${RUNNER_REGISTRY%%.*}" \
  --repository-name "${RUNNER_REPOSITORY##*/}" \
  --image-ids "imageTag=$RUNNER_TAG" \
  --query 'imageDetails[0].imageDigest' \
  --output text
