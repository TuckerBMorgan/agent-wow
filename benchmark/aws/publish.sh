#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."

region="${AWS_REGION:-us-west-2}"
stack="${HARBOR_STACK:-agent-wow-harbor}"
tag="${1:-worker-$(git rev-parse --short HEAD)}"
repository=$(aws ecr describe-repositories --region "$region" --repository-names "$stack" \
    --query 'repositories[0].repositoryUri' --output text)
aws ecr get-login-password --region "$region" \
    | docker login --username AWS --password-stdin "${repository%%/*}"
docker buildx build --platform linux/amd64 --file benchmark/Dockerfile \
    --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
    --tag "$repository:$tag" --push .
digest=$(aws ecr describe-images --region "$region" --repository-name "${repository#*/}" \
    --image-ids "imageTag=$tag" --query 'imageDetails[0].imageDigest' --output text)
printf '%s@%s\n' "$repository" "$digest" > benchmark/aws/image.txt
cat benchmark/aws/image.txt
