#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

keep=${KEEP_TEST_ENV:-0}
platform=${TEST_PLATFORM:-linux/amd64}
run_id="agent-wow-test-$$"
image="$run_id:latest"
container="$run_id-runner"
builder="$run_id-builder"
test_root=$(mktemp -d "${TMPDIR:-/tmp}/agent-wow-runner-test.XXXXXX")
builder_created=0
image_built=0

cleanup() {
    status=$?
    trap - EXIT
    if [ "$status" -ne 0 ] && [ -f "$test_root/workspace/logs/dockerd.log" ]; then
        echo "Last nested Docker log lines:" >&2
        tail -n 100 "$test_root/workspace/logs/dockerd.log" >&2 || true
    fi
    docker rm -f "$container" >/dev/null 2>&1 || true
    if [ "$keep" = 1 ]; then
        echo "Preserved test directory: $test_root"
        echo "Preserved image: $image"
        echo "Preserved builder: $builder"
    else
        if [ "$image_built" = 1 ]; then
            docker image rm -f "$image" >/dev/null 2>&1 || true
        fi
        if [ "$builder_created" = 1 ]; then
            docker buildx rm "$builder" >/dev/null 2>&1 || true
        fi
        rm -rf -- "$test_root"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker info >/dev/null
docker buildx version >/dev/null

mkdir -p \
    "$test_root/state/s3/params" \
    "$test_root/state/s3/output" \
    "$test_root/state/secrets" \
    "$test_root/state/dynamodb/agent-wow-jobs" \
    "$test_root/workspace"

cat > "$test_root/state/s3/params/params.json" <<'JSON'
{
  "job_id": "local-test",
  "benchmark": "agent-wow-v3",
  "trial_id": "local",
  "race": "human",
  "class": "warrior",
  "prompt": "Complete the local runner smoke test.",
  "harness": "claude",
  "model": "local-stub",
  "effort": "low",
  "timeout_seconds": 60,
  "credential_secret_arn": "local-credential",
  "output_s3_uri": "s3://output/local-test",
  "jobs_table": "agent-wow-jobs"
}
JSON
printf '%s' 'local-claude-token' > "$test_root/state/secrets/local-credential"
cat > "$test_root/state/dynamodb/agent-wow-jobs/JOB#local-test.json" <<'JSON'
{"pk":"JOB#local-test","status":"SCHEDULED"}
JSON

agent_wow_ref=${AGENT_WOW_REF:-$(git rev-parse HEAD)}
docker buildx create --name "$builder" --driver docker-container >/dev/null
builder_created=1
docker buildx build \
    --builder "$builder" \
    --platform "$platform" \
    --build-arg "AGENT_WOW_REF=$agent_wow_ref" \
    --tag "$image" \
    --load \
    .
image_built=1

docker run --rm \
    --name "$container" \
    --platform "$platform" \
    --privileged \
    --env AWS_ACCESS_KEY_ID=test \
    --env AWS_SECRET_ACCESS_KEY=test \
    --env AWS_DEFAULT_REGION=us-east-1 \
    --env AWS_EC2_METADATA_DISABLED=true \
    --env AWS_BATCH_JOB_ID=local-batch-job \
    --env LOCAL_AWS_ROOT=/run/local-aws \
    --env PARAMS_S3_URI=s3://params/params.json \
    --mount "type=bind,src=$test_root/state,dst=/run/local-aws" \
    --mount "type=bind,src=$test_root/workspace,dst=/workspace" \
    --mount "type=bind,src=$repo_root/docker/test_support/boto3.py,dst=/usr/local/bin/boto3.py,readonly" \
    --mount "type=bind,src=$repo_root/docker/test_support/claude,dst=/usr/local/bin/claude,readonly" \
    "$image"

test -s "$test_root/state/s3/output/local-test/execution.json"
test -s "$test_root/state/s3/output/viewer/local-test/connection.json"
test -s "$test_root/workspace/logs/agent.log"
python3 - "$test_root/state/dynamodb/agent-wow-jobs/JOB#local-test.json" <<'PY'
import json
import sys

job = json.load(open(sys.argv[1]))
assert job["status"] == "RUNNING"
assert job["agent_exit_code"] == 0
assert job["agent_timed_out"] is False
assert job["runner_error"] is None
assert job["execution_s3_uri"] == "s3://output/local-test/execution.json"
PY

echo "Runner smoke test passed."
