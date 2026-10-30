"""Small filesystem-backed boto3 substitute for the runner smoke test."""

import json
import os
from pathlib import Path

from botocore.exceptions import ClientError


ROOT = Path(os.environ["LOCAL_AWS_ROOT"])


class S3:
    def put_object(self, Bucket, Key, Body, **_kwargs):
        path = ROOT / "s3" / Bucket / Key
        path.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(Body, "read"):
            Body = Body.read()
        path.write_bytes(Body.encode() if isinstance(Body, str) else Body)


class SecretsManager:
    def get_secret_value(self, SecretId):
        return {"SecretString": (ROOT / "secrets" / SecretId).read_text()}

    def delete_secret(self, SecretId, ForceDeleteWithoutRecovery):
        assert ForceDeleteWithoutRecovery is True
        (ROOT / "secrets" / SecretId).unlink()
        return {}


class Table:
    def __init__(self, name):
        self.root = ROOT / "dynamodb" / name

    def get_item(self, Key, ConsistentRead):
        assert ConsistentRead is True
        return {"Item": json.loads((self.root / f'{Key["pk"]}.json').read_text())}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ConditionExpression,
        ExpressionAttributeNames,
        ExpressionAttributeValues,
    ):
        path = self.root / f'{Key["pk"]}.json'
        item = json.loads(path.read_text())
        condition_name, condition_value = ConditionExpression.split("=", 1)
        condition_name = ExpressionAttributeNames.get(
            condition_name.strip(), condition_name.strip()
        )
        expected = ExpressionAttributeValues[condition_value.strip()]
        if item.get(condition_name) != expected:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "UpdateItem",
            )
        assignments = UpdateExpression.removeprefix("SET ").split(",")
        for assignment in assignments:
            name, value = assignment.split("=", 1)
            name = ExpressionAttributeNames.get(name.strip(), name.strip())
            item[name] = ExpressionAttributeValues[value.strip()]
        path.write_text(json.dumps(item, indent=2) + "\n")


class DynamoDB:
    def Table(self, name):
        return Table(name)


def client(service_name):
    if service_name == "s3":
        return S3()
    if service_name == "secretsmanager":
        return SecretsManager()
    raise ValueError(f"unsupported local AWS client: {service_name}")


def resource(service_name):
    if service_name == "dynamodb":
        return DynamoDB()
    raise ValueError(f"unsupported local AWS resource: {service_name}")
