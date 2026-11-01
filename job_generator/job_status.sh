#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 || ( $# -eq 2 && "$2" != "--follow" ) ]]; then
    echo "Usage: job_status.sh JOB_ID [--follow]" >&2
    exit 1
fi

region="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
AWS_DEFAULT_REGION="$region" uv run --project "$script_dir" python - "$@" <<'PY'
from datetime import datetime
import sys
import time
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

job_id = sys.argv[1]
follow = len(sys.argv) == 3
table = boto3.resource("dynamodb").Table("agent-wow-jobs")
batch = boto3.client("batch")
s3 = boto3.client("s3", config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}))


def watch():
    previous, offset, source = None, 0, None
    while True:
        job = table.get_item(Key={"pk": f"JOB#{job_id}"}, ConsistentRead=True,
            ProjectionExpression="#status,batch_job_id,client_log_s3_uri,log_artifacts",
            ExpressionAttributeNames={"#status": "status"}).get("Item")
        if not job:
            raise RuntimeError(f"Job {job_id} was not found")
        batch_id = job.get("batch_job_id")
        status = "NOT_SUBMITTED"
        if batch_id:
            jobs = batch.describe_jobs(jobs=[batch_id])["jobs"]
            if not jobs:
                raise RuntimeError(f"AWS Batch no longer has a record of {batch_id}")
            status = jobs[0]["status"]
        if not follow:
            if not batch_id:
                raise RuntimeError(f"No AWS Batch job found for {job_id} (not yet submitted)")
            print(status)
            return 0
        current = (status, job["status"])
        if current != previous:
            print(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] '
                  f'Batch: {status}; application: {job["status"]}', flush=True)
            previous = current
        uri = job.get("client_log_s3_uri") or job.get("log_artifacts", {}).get("agent.log")
        if uri:
            if uri != source:
                source, offset = uri, 0
            location = urlsplit(uri)
            try:
                response = s3.get_object(Bucket=location.netloc, Key=location.path.lstrip("/"))
                with response["Body"] as body:
                    data = body.read()
                if len(data) < offset:
                    offset = 0
                sys.stdout.buffer.write(data[offset:])
                sys.stdout.buffer.flush()
                offset = len(data)
            except ClientError as error:
                if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                    raise
        if status in {"SUCCEEDED", "FAILED"} or job["status"] == "INCOMPLETE":
            return 0 if status == "SUCCEEDED" else 1
        time.sleep(15)


try:
    raise SystemExit(watch())
except KeyboardInterrupt:
    raise SystemExit(0)
except ClientError as error:
    print(f'AWS request failed: {error.response["Error"]["Code"]}', file=sys.stderr)
    raise SystemExit(1)
except RuntimeError as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)
PY
