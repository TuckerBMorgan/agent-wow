#!/usr/bin/env bash
set -euo pipefail

# Builds and deploys the runner, then submits one new Codex job.
# OpenTofu changes are applied automatically.
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
cd "$script_dir"

export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export TF_VAR_artifacts_bucket_name="${TF_VAR_artifacts_bucket_name:-agent-wow-artifacts-143551238491-us-west-2}"
export TF_CLI_ARGS_apply="${TF_CLI_ARGS_apply:-} -auto-approve"

if ! jq -e '.auth_mode == "chatgpt" and
    ([.tokens.access_token, .tokens.refresh_token, .tokens.id_token] |
     all(type == "string" and length > 0))' "$HOME/.codex/auth.json" >/dev/null; then
    echo 'A Codex ChatGPT login in ~/.codex/auth.json is required.' >&2
    exit 1
fi

bash "$script_dir/publish_runner.sh" "$@"

{
    read -r published_region
    read -r runner_repository
    read -r runner_tag
    read -r client_tag
} < "$repo_root/tmp/runner-image.txt"
runner_registry="${runner_repository%%/*}"
runner_digest=$(aws ecr describe-images \
    --region "$published_region" \
    --registry-id "${runner_registry%%.*}" \
    --repository-name "${runner_repository##*/}" \
    --image-ids "imageTag=$runner_tag" \
    --query 'imageDetails[0].imageDigest' --output text)
client_digest=$(aws ecr describe-images \
    --region "$published_region" \
    --registry-id "${runner_registry%%.*}" \
    --repository-name "${runner_repository##*/}" \
    --image-ids "imageTag=$client_tag" \
    --query 'imageDetails[0].imageDigest' --output text)

uv run python - "$runner_digest" "$client_digest" <<'PY'
import json
from pathlib import Path
import re
import sys

digest, client_digest = sys.argv[1:]
if not all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in (digest, client_digest)):
    raise SystemExit("ECR did not return a valid image digest")
path = Path("benchmark.json")
config = json.loads(path.read_text())
config["runner_image_digest"] = digest
config["client_image_digest"] = client_digest
path.write_text(json.dumps(config, indent=2) + "\n")
print(f"Updated runner image digest: {digest}")
print(f"Updated client image digest: {client_digest}")
PY

tofu apply -auto-approve
uv run python run_job.py
