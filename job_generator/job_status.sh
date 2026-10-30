#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: job_status.sh JOB_ID" >&2
    exit 1
fi

region="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
key=$(jq -cn --arg pk "JOB#$1" '{pk: {S: $pk}}')
batch_job_id=$(aws dynamodb get-item \
    --region "$region" --table-name agent-wow-jobs \
    --key "$key" --consistent-read \
    --projection-expression batch_job_id \
    --query 'Item.batch_job_id.S' --output text --no-cli-pager)

if [[ -z "$batch_job_id" || "$batch_job_id" == "None" ]]; then
    echo "No AWS Batch job found for job_id $1 (missing or not yet submitted)." >&2
    exit 1
fi

status=$(aws batch describe-jobs --region "$region" \
    --jobs "$batch_job_id" --query 'jobs[0].status' \
    --output text --no-cli-pager)

if [[ -z "$status" || "$status" == "None" ]]; then
    echo "AWS Batch no longer has a record of job $batch_job_id." >&2
    exit 1
fi

printf '%s\n' "$status"
