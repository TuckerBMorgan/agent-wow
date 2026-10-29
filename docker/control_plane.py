"""Trusted AWS operations used by the benchmark runner."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

def _s3_parts(uri):
    if not uri.startswith("s3://"):
        raise ValueError("expected an S3 URI")
    parts = uri[5:].split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError("expected an S3 URI with a bucket and key")
    return parts

def _table(params):
    return boto3.resource("dynamodb").Table(params["jobs_table"])

def load_params(uri):
    bucket, key = _s3_parts(uri)
    response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    params = json.loads(response["Body"].read())
    if not isinstance(params, dict):
        raise ValueError("params.json must contain an object")
    return params

def load_credential(params):
    response = boto3.client("secretsmanager").get_secret_value(
        SecretId=params["credential_secret_arn"]
    )
    credential = response.get("SecretString")
    if not credential:
        raise ValueError("credential secret must be a nonempty string")
    return credential

def upload_viewer_connection(params, path, batch_job_id):
    connection = json.loads(Path(path).read_text())
    connection.update(
        schema_version=1,
        job_id=params["job_id"],
        batch_job_id=batch_job_id,
    )
    bucket, _ = _s3_parts(params["output_s3_uri"])
    key = f'viewer/{params["job_id"]}/connection.json'
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=(json.dumps(connection, indent=2) + "\n").encode(),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    return f"s3://{bucket}/{key}"

def mark_running(params, started_at, viewer_uri):
    table = _table(params)
    deadline = time.monotonic() + 60
    while True:
        try:
            table.update_item(
                Key={"pk": f'JOB#{params["job_id"]}'},
                UpdateExpression=(
                    "SET #status = :running, started_at = :started_at, "
                    "updated_at = :started_at, "
                    "viewer_connection_s3_uri = :viewer_uri"
                ),
                ConditionExpression="#status = :scheduled",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":scheduled": "SCHEDULED",
                    ":running": "RUNNING",
                    ":started_at": started_at,
                    ":viewer_uri": viewer_uri,
                },
            )
            return
        except ClientError as error:
            if (
                error.response["Error"]["Code"]
                != "ConditionalCheckFailedException"
            ):
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError("Job did not become SCHEDULED") from error
            time.sleep(2)

def record_execution(params, report):
    """Upload the trusted execution report without assigning a verdict."""
    if report.get("job_id") != params["job_id"]:
        raise ValueError("execution report job_id does not match params.json")

    bucket, prefix = _s3_parts(params["output_s3_uri"])
    key = f"{prefix.rstrip('/')}/execution.json"
    body = json.dumps(report, indent=2, sort_keys=True).encode() + b"\n"
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )

    recorded_at = datetime.now(timezone.utc).isoformat()
    execution_uri = f"s3://{bucket}/{key}"
    _table(params).update_item(
        Key={"pk": f'JOB#{params["job_id"]}'},
        UpdateExpression=(
            "SET execution_s3_uri = :execution_uri, "
            "execution_recorded_at = :recorded_at, "
            "agent_exit_code = :agent_exit_code, "
            "agent_timed_out = :agent_timed_out, "
            "runner_error = :runner_error, updated_at = :recorded_at"
        ),
        ConditionExpression="#status = :running",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":running": "RUNNING",
            ":execution_uri": execution_uri,
            ":recorded_at": recorded_at,
            ":agent_exit_code": report.get("agent_exit_code"),
            ":agent_timed_out": report.get("agent_timed_out", False),
            ":runner_error": report.get("runner_error"),
        },
    )
    return execution_uri
