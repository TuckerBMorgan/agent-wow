import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from contextlib import suppress

import boto3
from botocore.exceptions import ClientError


TABLE = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
SECRETS = boto3.client("secretsmanager")
BATCH = boto3.client("batch")
JOB_QUEUE = os.environ["BATCH_JOB_QUEUE"]
JOB_DEFINITION = os.environ["BATCH_JOB_DEFINITION"]


def now():
    return datetime.now(timezone.utc).isoformat()


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "cache-control": "no-store",
            "content-type": "application/json",
        },
        "body": json.dumps(body),
    }


def request_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body, validate=True).decode()
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError
    job_id = value.get("job_id")
    claim_token = value.get("claim_token")
    credential = value.get("credential")
    if not all(isinstance(item, str) and item for item in (job_id, claim_token, credential)):
        raise ValueError
    if str(uuid.UUID(job_id)) != job_id or len(claim_token) > 256:
        raise ValueError
    if len(credential.encode()) > 65536:
        raise ValueError
    return job_id, claim_token, credential


def update_job(job_id, update, condition, **values):
    TABLE.update_item(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression=update,
        ConditionExpression=condition,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={f":{name}": value for name, value in values.items()},
    )


def mark_incomplete(job_id, reason):
    with suppress(Exception):
        timestamp = now()
        update_job(
            job_id,
            "SET #status=:incomplete, incomplete_reason=:reason, updated_at=:at",
            "#status=:claiming",
            claiming="CLAIMING", incomplete="INCOMPLETE", reason=reason, at=timestamp,
        )


def handler(event, _context):
    try:
        job_id, claim_token, credential = request_body(event)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        return response(400, {"error": "invalid request"})

    phase = "claim"
    claimed = False
    secret_arn = None
    batch_job_id = None
    try:
        timestamp = now()
        update_job(
            job_id,
            "SET #status=:claiming, updated_at=:at REMOVE claim_token_hash",
            "#status=:queued AND claim_token_hash=:token",
            queued="QUEUED", claiming="CLAIMING",
            token=hashlib.sha256(claim_token.encode()).hexdigest(), at=timestamp,
        )
        claimed = True

        phase = "credential"
        secret_arn = SECRETS.create_secret(
            Name=f"agent-wow/jobs/{job_id}",
            SecretString=credential,
            Tags=[{"Key": "agent-wow-job-id", "Value": job_id}],
        )["ARN"]

        phase = "batch"
        batch_job_id = BATCH.submit_job(
            jobName=f"agent-wow-{job_id}",
            jobQueue=JOB_QUEUE,
            jobDefinition=JOB_DEFINITION,
            containerOverrides={
                "environment": [
                    {"name": "JOB_ID", "value": job_id},
                    {"name": "CREDENTIAL_SECRET_ARN", "value": secret_arn},
                ]
            },
        )["jobId"]

        phase = "schedule"
        scheduled_at = now()
        update_job(
            job_id,
            "SET #status=:scheduled, batch_job_id=:batch, scheduled_at=:at, updated_at=:at",
            "#status=:claiming",
            claiming="CLAIMING", scheduled="SCHEDULED", batch=batch_job_id, at=scheduled_at,
        )
        return response(202, {"job_id": job_id, "status": "SCHEDULED"})
    except ClientError as error:
        if (not claimed and
                error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"):
            return response(409, {"error": "job cannot be claimed"})
        failure = error
    except Exception as error:
        failure = error

    if batch_job_id:
        with suppress(Exception):
            BATCH.terminate_job(jobId=batch_job_id, reason="Claim setup failed")
    if secret_arn:
        with suppress(Exception):
            SECRETS.delete_secret(SecretId=secret_arn, ForceDeleteWithoutRecovery=True)
    if claimed:
        mark_incomplete(job_id, f"claim_{phase}_failed")
    print(f"Claim failed for {job_id} during {phase}: {type(failure).__name__}")
    return response(500, {"error": "claim setup failed"})
