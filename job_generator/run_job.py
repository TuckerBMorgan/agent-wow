import argparse
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
from itertools import product
import json
import os
from pathlib import Path
import re
import sys
import uuid

import boto3
from botocore.exceptions import ClientError


def now():
    return datetime.now(timezone.utc).isoformat()


def submit(spec, definition, queue, credential, table, batch, secrets):
    job_id, timestamp = str(uuid.uuid4()), now()
    case_id = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    secret_arn = batch_id = None
    try:
        table.put_item(Item={
            "pk": f"JOB#{job_id}", "record_type": "JOB", "job_id": job_id,
            "case_id": case_id, "status": "QUEUED", "spec": spec,
            "batch_job_definition": definition, "batch_job_queue": queue,
            "created_at": timestamp, "updated_at": timestamp,
        }, ConditionExpression="attribute_not_exists(pk)")
        secret_arn = secrets.create_secret(Name=f"agent-wow/jobs/{job_id}", SecretString=credential)["ARN"]
        batch_id = batch.submit_job(jobName=f"agent-wow-{job_id}", jobQueue=queue,
            jobDefinition=definition, containerOverrides={"environment": [
                {"name": "JOB_ID", "value": job_id},
                {"name": "CREDENTIAL_SECRET_ARN", "value": secret_arn},
            ]})["jobId"]
        table.update_item(Key={"pk": f"JOB#{job_id}"},
            UpdateExpression="SET #status=:scheduled, batch_job_id=:batch, scheduled_at=:at, updated_at=:at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":scheduled": "SCHEDULED", ":batch": batch_id, ":at": now()})
    except (Exception, KeyboardInterrupt):
        if batch_id:
            with suppress(Exception):
                batch.terminate_job(jobId=batch_id, reason="Job submission failed")
        if secret_arn:
            with suppress(Exception):
                secrets.delete_secret(SecretId=secret_arn, ForceDeleteWithoutRecovery=True)
        with suppress(Exception):
            table.update_item(Key={"pk": f"JOB#{job_id}"}, ConditionExpression="attribute_exists(pk)",
                UpdateExpression="SET #status=:failed, runner_error=:error, updated_at=:at",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":failed": "FAILED", ":error": "submission_failed", ":at": now()})
        print(f"Submission failed for job {job_id}", file=sys.stderr)
        raise
    print(json.dumps({"job_id": job_id, "batch_job_id": batch_id, "status": "SCHEDULED"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Submit a benchmark directly to AWS Batch.")
    parser.add_argument("--all", action="store_true", help="Submit the full matching matrix for this harness.")
    parser.add_argument("--harness", choices=("codex", "claude"), default="codex")
    parser.add_argument("--credential-file", type=Path, help="Codex auth JSON or a private Claude OAuth token file.")
    parser.add_argument("--race")
    parser.add_argument("--class", dest="character_class")
    parser.add_argument("--evaluation")
    parser.add_argument("--trial-id")
    parser.add_argument("--job-definition", default="agent-wow-runner", help="Name, name:revision, or ARN.")
    parser.add_argument("--job-queue", default="agent-wow-runner")
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name("benchmark.json").read_text())
    characters = [{"race": race, "class": cls} for race, classes in config["characters"].items()
                  for cls in classes if (not args.race or race == args.race)
                  and (not args.character_class or cls == args.character_class)]
    runs = [run for run in config["runs"] if run["harness"] == args.harness]
    evaluations = [item for item in config["evaluations"] if not args.evaluation or item["id"] == args.evaluation]
    if not characters or not runs or not evaluations:
        parser.error("No matching character, harness, or evaluation in benchmark.json")
    trials = [args.trial_id] if args.trial_id else config["trial_ids"] if args.all else [uuid.uuid4().hex]
    if not args.all:
        characters, runs, evaluations = characters[:1], runs[:1], evaluations[:1]

    path = args.credential_file
    if path is None:
        if args.harness == "claude":
            parser.error("Claude requires --credential-file containing its OAuth token")
        path = Path.home() / ".codex/auth.json"
    credential = path.expanduser().read_text().strip()
    if not credential or len(credential.encode()) > 65536:
        parser.error("The credential file must contain between 1 and 65536 bytes")
    if args.harness == "codex":
        auth = json.loads(credential)
        if auth.get("auth_mode") != "chatgpt" or not all(auth.get("tokens", {}).get(key)
                for key in ("access_token", "refresh_token", "id_token")):
            parser.error("Codex requires a ChatGPT subscription login")
    elif credential.startswith("{"):
        parser.error("Claude requires a raw OAuth token, not Codex auth JSON")

    session = boto3.Session(region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2")
    batch = session.client("batch")
    query = ({"jobDefinitions": [args.job_definition]} if ":" in args.job_definition else
             {"jobDefinitionName": args.job_definition, "status": "ACTIVE"})
    definitions = [item for page in batch.get_paginator("describe_job_definitions").paginate(**query)
                   for item in page["jobDefinitions"] if item["status"] == "ACTIVE"]
    if not definitions:
        parser.error("No active Batch job definition found")
    definition = max(definitions, key=lambda item: item["revision"])
    container = definition["containerProperties"]
    environment = {item["name"]: item["value"] for item in container["environment"]}
    server_digest = container["image"].rsplit("@", 1)[-1]
    client_digest = environment["CLIENT_IMAGE_DIGEST"]
    if not all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in (server_digest, client_digest)):
        parser.error("The Batch definition must pin both server and client images by digest")
    table = session.resource("dynamodb").Table(environment["JOBS_TABLE"])
    secrets = session.client("secretsmanager")
    for character, run, evaluation, trial in product(characters, runs, evaluations, trials):
        description = f'{character["race"]} {character["class"]}'.replace("-", " ")
        spec = {
            "benchmark": config["benchmark"], "evaluation": evaluation["id"], "trial_id": trial,
            **character, **run, "prompt": evaluation["prompt_template"].replace("[race/class]", description),
            "image_digest": server_digest, "client_image_digest": client_digest,
            "timeout_seconds": config["timeout_seconds"],
        }
        submit(spec, definition["jobDefinitionArn"], args.job_queue, credential, table, batch, secrets)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except ClientError as error:
        print(f'AWS request failed: {error.response["Error"]["Code"]}', file=sys.stderr)
        raise SystemExit(1)
