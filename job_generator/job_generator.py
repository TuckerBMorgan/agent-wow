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

SERIALIZER = TypeSerializer()

def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

def _put(table: str, item: dict[str, Any]) -> dict[str, Any]:
    return {"Put": {
        "TableName": table,
        "Item": {key: SERIALIZER.serialize(value) for key, value in item.items()},
        "ConditionExpression": "attribute_not_exists(pk)",
    }}

def _write_participant(output: Path, claim_url: str, job: dict[str, Any], token: str) -> Path:
    path = output / f'{job["job_id"]}.participant.json'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    participant = {
        "schema_version": 1, "job_id": job["job_id"], "case_id": job["case_id"],
        "claim_url": claim_url, "claim_token": token, "spec": job["spec"],
    }
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(participant, file, indent=2)
        file.write("\n")
    return path

def create_job_definitions(config: dict[str, Any], table_name: str, output_dir: str) -> list[Path]:
    trial_ids = config["trial_ids"]
    if (not trial_ids or len(trial_ids) != len(set(trial_ids))
            or not all(isinstance(value, str) and value for value in trial_ids)):
        raise ValueError("trial_ids must contain unique nonempty strings")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    dynamodb = boto3.client("dynamodb")
    participant_files = []
    characters = ({"race": race, "class": character_class}
                  for race, classes in config["characters"].items() for character_class in classes)
    for pair, run, evaluation, trial_id in product(
            characters, config["runs"], config["evaluations"], trial_ids):
        character = f'{pair["race"]} {pair["class"]}'.replace("-", " ")
        spec = {
            "benchmark": config["benchmark"], "evaluation": evaluation["id"],
            "trial_id": trial_id, **pair,
            "prompt": evaluation["prompt_template"].replace("[race/class]", character),
            "harness": run["harness"], "model": run["model"], "effort": run["effort"],
            "image_digest": config["runner_image_digest"],
            "timeout_seconds": config["timeout_seconds"],
        }
        case_id, job_id = _digest(spec), str(uuid.uuid4())
        token, created_at = secrets.token_urlsafe(32), datetime.now(timezone.utc).isoformat()
        job = {
            "pk": f"JOB#{job_id}", "record_type": "JOB", "job_id": job_id,
            "case_id": case_id, "attempt": 1, "status": "QUEUED", "spec": spec,
            "claim_token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": created_at, "updated_at": created_at,
        }
        case = {
            "pk": f"CASE#{case_id}", "record_type": "CASE", "case_id": case_id,
            "active_job_id": job_id, "latest_attempt": 1,
            "created_at": created_at, "updated_at": created_at,
        }
        path = _write_participant(output, config["claim_url"], job, token)
        dynamodb.transact_write_items(TransactItems=[_put(table_name, case), _put(table_name, job)])
        participant_files.append(path)
    return participant_files
