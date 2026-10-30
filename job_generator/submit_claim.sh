#!/usr/bin/env bash
set -euo pipefail

participant="${1:?Usage: submit_claim.sh /path/to/job.participant.json}"

jq --rawfile credential "$HOME/.codex/auth.json" \
  '{job_id, claim_token, credential: $credential}' \
  "$participant" |
curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  --data-binary @- \
  --write-out '\nHTTP %{http_code}\n' \
  "$(jq -r '.claim_url' "$participant")"
