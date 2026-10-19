#!/bin/sh
# Start a fresh full run and follow its logs, optionally supplying a different prompt.
# Usage: ./launch.sh 'Create a night elf druid and complete the starting quests.'
set -eu

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [\"prompt\"] — quote the prompt as one argument." >&2
  exit 2
fi
if [ "$#" -eq 1 ]; then
  overrides=$(python3 -c '
import json
import sys

prompt = sys.argv[1]
if not prompt.strip():
    sys.exit("Prompt must not be blank.")
print(json.dumps({"command": ["run-wow-agent", prompt]}))
' "$1")
  set -- --container-overrides "$overrides"
fi

WOW_SUBMISSION=$(aws batch submit-job \
  --region us-west-2 \
  --job-name agent-wow \
  --job-queue agent-wow \
  --job-definition "${WOW_JOB_DEFINITION:-agent-wow}" \
  --output json --no-cli-pager "$@")
printf '%s\n' "$WOW_SUBMISSION"
WOW_JOB_ID=$(printf '%s' "$WOW_SUBMISSION" | python3 -c 'import json, sys; print(json.load(sys.stdin)["jobId"])')
exec python3 "$(dirname -- "$0")/watch-job.py" "$WOW_JOB_ID" --region us-west-2
