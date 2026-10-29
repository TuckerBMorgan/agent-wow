import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer


_serializer = TypeSerializer()
RETRYABLE_STATES = {"FAILED", "INCOMPLETE"}


# Convert Python values to DynamoDB's wire format.
def _marshall(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        key: _serializer.serialize(value)
        for key, value in item.items()
    }


# Hash the complete specification so one planned trial shares one case.
def _case_id(spec: dict[str, Any]) -> str:
    value = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


# Write the only file that contains the raw claim token.
def _write_participant(
    output: Path,
    claim_url: str,
    job: dict[str, Any],
    claim_token: str,
) -> Path:
    participant = {
        "schema_version": 1,
        "job_id": job["job_id"],
        "case_id": job["case_id"],
        "claim_url": claim_url,
        "claim_token": claim_token,
        "spec": job["spec"],
    }
    path = output / f'{job["job_id"]}.participant.json'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(participant, file, indent=2)
        file.write("\n")
    return path


# Build one immutable Job attempt and its participant file.
def _new_attempt(
    spec: dict[str, Any],
    case_id: str,
    attempt: int,
    claim_url: str,
    output: Path,
) -> tuple[dict[str, Any], Path]:
    job_id = str(uuid.uuid4())
    claim_token = secrets.token_urlsafe(32)
    created_at = datetime.now(timezone.utc).isoformat()

    job = {
        "pk": f"JOB#{job_id}",
        "record_type": "JOB",
        "job_id": job_id,
        "case_id": case_id,
        "attempt": attempt,
        "status": "QUEUED",
        "spec": spec,
        "claim_token_hash": hashlib.sha256(
            claim_token.encode()
        ).hexdigest(),
        "created_at": created_at,
        "updated_at": created_at,
    }
    path = _write_participant(output, claim_url, job, claim_token)
    return job, path


def create_job_definitions(
    config: dict[str, Any],
    table_name: str,
    output_dir: str,
) -> list[Path]:
    # Prepare the database client and participant-file directory.
    dynamodb = boto3.client("dynamodb")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    participant_files = []
    trial_ids = config["trial_ids"]
    if (
        not trial_ids
        or any(
            not isinstance(trial_id, str) or not trial_id
            for trial_id in trial_ids
        )
        or len(set(trial_ids)) != len(trial_ids)
    ):
        raise ValueError("trial_ids must contain unique nonempty strings")

    # Expand every evaluation, character, model run, and planned trial.
    for pair, run, evaluation, trial_id in product(
        config["race_class_pairs"],
        config["runs"],
        config["evaluations"],
        trial_ids,
    ):
        character = f'{pair["race"]} {pair["class"]}'.replace("-", " ")
        prompt = evaluation["prompt_template"].replace(
            "[race/class]",
            character,
        )

        # This specification is copied unchanged into params.json.
        spec = {
            "benchmark": config["benchmark"],
            "evaluation": evaluation["id"],
            "trial_id": trial_id,
            "race": pair["race"],
            "class": pair["class"],
            "prompt": prompt,
            "harness": run["harness"],
            "model": run["model"],
            "effort": run["effort"],
            "image_digest": config["runner_image_digest"],
            "timeout_seconds": config["timeout_seconds"],
        }
        case_id = _case_id(spec)
        job, participant_path = _new_attempt(
            spec,
            case_id,
            1,
            config["claim_url"],
            output,
        )
        created_at = job["created_at"]

        # The Case reserves this specification for the active attempt.
        case = {
            "pk": f"CASE#{case_id}",
            "record_type": "CASE",
            "case_id": case_id,
            "active_job_id": job["job_id"],
            "latest_attempt": 1,
            "created_at": created_at,
            "updated_at": created_at,
        }

        try:
            # Create the Case and Job together or create neither.
            dynamodb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": table_name,
                            "Item": _marshall(case),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": table_name,
                            "Item": _marshall(job),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except Exception:
            # Keep the participant file: the transaction may have committed even
            # when the client receives an error.
            raise

        participant_files.append(participant_path)

    return participant_files
