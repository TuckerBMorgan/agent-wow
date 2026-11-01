#!/opt/venv/bin/python3
import gzip
import hashlib
import json
import os
import secrets
import shutil
import shlex
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import boto3
import docker
from botocore.exceptions import ClientError
from botocore.config import Config

WORKSPACE = Path("/workspace")
LOGS = WORKSPACE / "logs"
COMPOSE = ["docker", "compose", "--project-directory", f"{WORKSPACE}/server", "--file", f"{WORKSPACE}/server/compose.yaml"]
STATE = {"client": None, "input_version": None, "dockerd": None, "docker_log": None}
TABLE = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
S3 = boto3.client("s3", config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=15,
                                     retries={"max_attempts": 2}))
EC2 = boto3.client("ec2", config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}))
BUCKET = os.environ["ARTIFACTS_BUCKET"]

def now():
    return datetime.now(timezone.utc).isoformat()

def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n"); path.chmod(0o600)

def trusted_env():
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY",
                 "OPENAI_BASE_URL", "CODEX_AUTH_JSON", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(name, None)
    return env

def checked(command, **kwargs):
    kwargs.setdefault("env", trusted_env()); return subprocess.run(command, check=True, **kwargs)

def put_s3_json(key, value, sort_keys=False):
    S3.put_object(Bucket=BUCKET, Key=key, Body=(json.dumps(value, indent=2, sort_keys=sort_keys) + "\n").encode(),
                  ContentType="application/json", ServerSideEncryption="AES256")
    return f"s3://{BUCKET}/{key}"

def put_s3_file(key, path, **kwargs):
    with path.open("rb") as body:
        S3.put_object(Bucket=BUCKET, Key=key, Body=body, ServerSideEncryption="AES256", **kwargs)
    return f"s3://{BUCKET}/{key}"

def load_job():
    job_id = os.environ["JOB_ID"]
    job = TABLE.get_item(Key={"pk": f"JOB#{job_id}"}, ConsistentRead=True)["Item"]
    params = {"job_id": job_id, "case_id": job["case_id"], **job["spec"]}
    params["timeout_seconds"] = int(params["timeout_seconds"])
    secrets_manager = boto3.client("secretsmanager")
    secret_id = os.environ["CREDENTIAL_SECRET_ARN"]
    credential = secrets_manager.get_secret_value(SecretId=secret_id).get("SecretString")
    if not credential:
        raise ValueError("credential secret must be a nonempty string")
    secrets_manager.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
    return params, credential

def mark_running(params, started_at, viewer_uri):
    deadline = time.monotonic() + 60
    while True:
        try:
            TABLE.update_item(Key={"pk": f'JOB#{params["job_id"]}'},
                UpdateExpression="SET #status=:running, started_at=:at, updated_at=:at, viewer_connection_s3_uri=:viewer",
                ConditionExpression="#status=:scheduled", ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":scheduled": "SCHEDULED", ":running": "RUNNING", ":at": started_at, ":viewer": viewer_uri})
            return
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException": raise
            if time.monotonic() >= deadline: raise RuntimeError("Job did not become SCHEDULED") from error
            time.sleep(2)

def record_execution(params, report):
    if report["job_id"] != params["job_id"]:
        raise ValueError("execution report job_id does not match params.json")
    report["status"] = ("SUCCEEDED" if report["agent_exit_code"] == 0
                        and not report["agent_timed_out"] and report["runner_error"] is None else "FAILED")
    uri = put_s3_json(f'runs/{params["job_id"]}/execution.json', report, True)
    recorded_at = now()
    TABLE.update_item(Key={"pk": f'JOB#{params["job_id"]}'},
        UpdateExpression="SET #status=:finished, finished_at=:finished_at, execution_s3_uri=:uri, execution_recorded_at=:at, agent_exit_code=:code, agent_timed_out=:timed_out, runner_error=:error, log_artifacts=:logs, updated_at=:at",
        ConditionExpression="#status=:scheduled OR #status=:running", ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":scheduled": "SCHEDULED", ":running": "RUNNING",
            ":finished": report["status"], ":finished_at": report["finished_at"],
            ":uri": uri, ":at": recorded_at, ":logs": report["log_artifacts"],
            ":code": report["agent_exit_code"], ":timed_out": report["agent_timed_out"], ":error": report["runner_error"]})

def upload_logs(params, report):
    for name in ("agent.log", "final-report.txt"):
        path = LOGS / name
        if path.is_file():
            report["log_artifacts"][name] = put_s3_file(
                f'runs/{params["job_id"]}/logs/{name}', path, ContentType="text/plain; charset=utf-8")

def world_console(command, expect=None):
    containers = docker.from_env().containers.list(filters={"label": ["com.docker.compose.project=agent-wow-world",
                                                                        "com.docker.compose.service=worldserver"]})
    connection = containers[0].attach_socket(params={"stdin": True, "stdout": bool(expect), "stream": True})
    try:
        connection._sock.settimeout(30)
        connection._sock.sendall((command + "\n").encode())
        reply, deadline = b"", time.monotonic() + 30
        while expect and expect not in reply:
            if time.monotonic() >= deadline: raise RuntimeError("World console confirmation timed out")
            chunk = connection._sock.recv(16384)
            if not chunk: raise RuntimeError("World console closed before confirmation")
            reply = (reply + chunk)[-65536:]
    finally:
        connection.close()

def ensure_account(root=WORKSPACE / "config", username="AGENT", env=None):
    os.umask(0o077)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    auth = root / "auth.json"
    if not auth.exists():
        write_json(auth, {"username": username, "password": secrets.token_hex(8)})
    account = json.loads(auth.read_text())
    client = lambda *args: subprocess.run(["agent-wow", *args], env=env, capture_output=True, text=True, timeout=30)
    if client("auth", "login", "--timeout", "10s").returncode:
        world_console(f'account create {account["username"]} {account["password"]}')
        for _ in range(30):
            if not client("auth", "login", "--timeout", "10s").returncode: break
            time.sleep(2)
        else:
            raise RuntimeError("Game account login failed; credentials were not changed")
    return account, client

def prepare_accounts(params):
    player, _ = ensure_account(env=trusted_env())
    root = Path("/run/agent-wow-observer")
    env = trusted_env() | {"AGENT_WOW_CONFIG_DIR": str(root), "AGENT_WOW_DATA_DIR": str(root / "data")}
    account, client = ensure_account(root, "WATCHER", env)
    world_console("account set gmlevel WATCHER 2 -1", b"security level of account WATCHER to 2.")
    listing = client("char", "list", "--json")
    if listing.returncode:
        raise RuntimeError("Viewer character listing failed")
    if not any(character["name"] == "Observer" for character in json.loads(listing.stdout)["characters"]):
        if client("char", "create", "--name", "Observer", "--race", "orc", "--class", "warrior").returncode:
            raise RuntimeError("Viewer character creation failed")
    connection = account | {"schema_version": 1, "job_id": params["job_id"], "host": server_address(),
                            "batch_job_id": os.environ["AWS_BATCH_JOB_ID"], "auth_port": 3724,
                            "world_port": 8085, "character": "Observer"}
    viewer_uri = put_s3_json(f'viewer/{params["job_id"]}/connection.json', connection)
    return player, viewer_uri

def prepare_workspace():
    for name in ("config", "data", "logs", "server"):
        (WORKSPACE / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2("/opt/server/compose.yaml", WORKSPACE / "server/compose.yaml")
    env_file = WORKSPACE / "server/.env"
    env_file.write_text(f"MYSQL_ROOT_PASSWORD={secrets.token_hex(24)}\nSERVER_BIND_ADDRESS=0.0.0.0\n")
    env_file.chmod(0o600)

def start_server():
    log = (LOGS / "dockerd.log").open("w")
    STATE["docker_log"] = log
    STATE["dockerd"] = subprocess.Popen(["dockerd", "--host=unix:///var/run/docker.sock",
        "--feature=containerd-snapshotter=false", "--storage-driver=overlay2"],
        stdout=log, stderr=subprocess.STDOUT, env=trusted_env())
    deadline = time.monotonic() + 120
    while subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        if STATE["dockerd"].poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError("Docker did not start")
        time.sleep(1)
    archives = sorted(Path("/opt/server-images").glob("*.tar"))
    if len(archives) != 5: raise RuntimeError("expected five cached server images")
    for archive in archives:
        checked(["docker", "load", "--input", str(archive)])
    checked(COMPOSE + ["up", "--wait", "--wait-timeout", "1800", "--pull", "never"])

def server_address():
    if os.environ.get("SERVER_HOST"):
        return os.environ["SERVER_HOST"]
    # Batch EC2 jobs use host networking. This selects the host's primary IP
    # without contacting the destination or reading instance credentials.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.connect(("1.1.1.1", 80))
        return connection.getsockname()[0]

def client_data(prefix, name):
    try:
        value = S3.get_object(Bucket=BUCKET, Key=prefix + name)
    except ClientError as error:
        if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return None
        raise
    with value["Body"] as body:
        if value["ContentLength"] > 65536:
            raise ValueError("client report is too large")
        return json.loads(body.read())

def client_userdata(input_url, image, job_id, timeout_seconds):
    region = EC2.meta.region_name
    registry = image.split("/", 1)[0]
    command = shlex.join(["docker", "run", "--rm", "--privileged", "--network", "host",
        "--env", f"JOB_INPUT_URL={input_url}", "--env", f"ARTIFACTS_BUCKET={BUCKET}",
        "--env", f"JOB_ID={job_id}",
        "--env", f"AWS_DEFAULT_REGION={region}", image])
    # Only a short-lived download capability is in user data. Actual game and
    # model credentials are fetched privately by the client and never logged.
    return ("#!/bin/bash\nset -euo pipefail\ntrap 'shutdown -h now' EXIT\n"
        f"shutdown -h +{(int(timeout_seconds) + 2159) // 60}\n"
        "dnf install -y docker awscli-2\nsystemctl start docker\n"
        f"aws ecr get-login-password --region {shlex.quote(region)} | "
        f"docker login --username AWS --password-stdin {shlex.quote(registry)}\n"
        + command + "\n")

def run_client(params, credential, player, viewer_uri, report):
    digest = params["client_image_digest"]
    if digest != os.environ["CLIENT_IMAGE_DIGEST"]:
        raise ValueError("job client image does not match the deployed client image")
    host = server_address()
    key = f'inputs/{params["job_id"]}.json'
    payload = {"params": params, "credential": credential, "player": player, "server_host": host}
    uploaded = S3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(payload).encode(),
                             ServerSideEncryption="AES256", ContentType="application/json")
    STATE["input_version"] = (key, uploaded["VersionId"])
    url = S3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key,
        "VersionId": uploaded["VersionId"]}, ExpiresIn=1800)
    image = os.environ["CLIENT_REPOSITORY"] + "@" + digest
    launched = EC2.run_instances(
        LaunchTemplate={"LaunchTemplateId": os.environ["CLIENT_LAUNCH_TEMPLATE_ID"],
                        "Version": os.environ["CLIENT_LAUNCH_TEMPLATE_VERSION"]},
        MinCount=1, MaxCount=1, ClientToken=params["job_id"],
        UserData=client_userdata(url, image, params["job_id"], params["timeout_seconds"]),
        TagSpecifications=[{"ResourceType": "instance", "Tags": [
            {"Key": "agent-wow-role", "Value": "client"},
            {"Key": "agent-wow-job-id", "Value": params["job_id"]}]}],
    )["Instances"][0]
    instance = launched["InstanceId"]
    STATE["client"] = instance
    print(f"Client instance: {instance}", flush=True)
    prefix = f'clients/{os.environ["CLIENT_ROLE_ID"]}:{instance}/'
    client_log_uri = f"s3://{BUCKET}/{prefix}agent.log"
    report.update(client_instance_id=instance, client_image_digest=digest, server_private_ip=host,
                  client_ami_id=launched["ImageId"], client_private_ip=launched["PrivateIpAddress"],
                  client_log_s3_uri=client_log_uri)
    TABLE.update_item(Key={"pk": f'JOB#{params["job_id"]}'},
        UpdateExpression="SET client_instance_id=:client, client_image_digest=:image, server_private_ip=:host, client_private_ip=:ip, client_ami_id=:ami, client_log_s3_uri=:log",
        ExpressionAttributeValues={":client": instance, ":image": digest, ":host": host,
                                   ":ip": launched["PrivateIpAddress"], ":ami": launched["ImageId"],
                                   ":log": client_log_uri})
    ready, deadline = False, time.monotonic() + 1800
    while True:
        readiness = client_data(prefix, "ready.json") if not ready else None
        if readiness:
            if readiness.get("job_id") != params["job_id"]:
                raise ValueError("client readiness job_id does not match")
            ready, deadline = True, time.monotonic() + params["timeout_seconds"] + 120
            report["started_at"] = now()
            mark_running(params, report["started_at"], viewer_uri)
            delete_input()
        result = client_data(prefix, "client-result.json")
        if result is not None:
            if result.get("job_id") != params["job_id"]:
                raise ValueError("client report job_id does not match")
            for name in ("agent_exit_code", "agent_timed_out", "runner_error"):
                report[name] = result[name]
            for name in ("agent.log", "final-report.txt", "client-dockerd.log"):
                source = prefix + name
                try:
                    key = f'runs/{params["job_id"]}/logs/{name}'
                    S3.copy_object(Bucket=BUCKET, Key=key, CopySource={"Bucket": BUCKET, "Key": source},
                                   ServerSideEncryption="AES256")
                    report["log_artifacts"][name] = f"s3://{BUCKET}/{key}"
                except ClientError as error:
                    if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                        raise
            return
        try:
            state = EC2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
        except ClientError as error:
            if error.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                raise
            state = "pending"
        if state in {"shutting-down", "terminated", "stopping", "stopped"}:
            report["runner_error"] = "client_stopped_without_report"
            raise RuntimeError("client stopped without an execution report; inspect its EC2 console")
        if time.monotonic() >= deadline:
            report["agent_timed_out"] = ready
            report["agent_exit_code"] = 124 if ready else None
            report["runner_error"] = "client_execution_deadline_exceeded" if ready else "client_startup_deadline_exceeded"
            raise TimeoutError("client startup or execution deadline exceeded")
        time.sleep(5)

def delete_input():
    if STATE["input_version"]:
        key, version = STATE["input_version"]
        S3.delete_object(Bucket=BUCKET, Key=key, VersionId=version)
        STATE["input_version"] = None

def snapshot_state(params):
    checked(COMPOSE + ["stop", "--timeout", "60", "worldserver", "authserver"],
            stdout=subprocess.DEVNULL)
    root = Path("/run/agent-wow-state")
    root.mkdir(mode=0o700)
    archive = root / "acore_characters.sql.gz"
    errors = root / "mysqldump.log"
    command = COMPOSE + ["exec", "-T", "database", "sh", "-c",
        'exec mysqldump --single-transaction --quick --hex-blob --no-tablespaces '
        '-uroot -p"$MYSQL_ROOT_PASSWORD" --databases acore_characters']
    with archive.open("wb") as output, errors.open("wb") as error_log:
        dump = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_log, env=trusted_env())
        try:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                shutil.copyfileobj(dump.stdout, compressed)
        finally:
            dump.stdout.close()
            returncode = dump.wait()
    if returncode:
        raise RuntimeError("character database dump failed")
    digest = hashlib.sha256()
    with archive.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    key = f'runs/{params["job_id"]}/acore_characters.sql.gz'
    uri = put_s3_file(key, archive, ContentType="application/sql", ContentEncoding="gzip")
    return {"database": "acore_characters", "format": "mysql-dump-gzip", "uri": uri,
            "sha256": digest.hexdigest(), "size_bytes": archive.stat().st_size}

def cleanup():
    errors = []
    if STATE["client"]:
        try:
            EC2.terminate_instances(InstanceIds=[STATE["client"]])
        except Exception as error:
            errors.append(error)
    try:
        delete_input()
    except Exception as error:
        errors.append(error)
    dockerd = STATE["dockerd"]
    if dockerd and dockerd.poll() is None:
        subprocess.run(COMPOSE + ["down", "--timeout", "30"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=trusted_env())
        dockerd.terminate()
        try:
            dockerd.wait(timeout=30)
        except subprocess.TimeoutExpired:
            dockerd.kill()
            dockerd.wait()
    if STATE["docker_log"]:
        STATE["docker_log"].close()
    if errors:
        raise errors[0]

def stop(signum, _frame):
    raise KeyboardInterrupt

def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    params, credential = {"job_id": os.environ["JOB_ID"]}, None
    phase, started, started_at = "load_job", time.monotonic(), now()
    report = {"agent_exit_code": None, "agent_timed_out": False, "runner_error": None,
              "state_artifact": None, "log_artifacts": {}, "started_at": started_at}
    try:
        params, credential = load_job()
        phase = "prepare_workspace"; prepare_workspace()
        phase = "start_server"; start_server()
        phase = "prepare_accounts"; player, viewer_uri = prepare_accounts(params)
        phase = "run_client"
        run_client(params, credential, player, viewer_uri, report)
        phase = "snapshot_state"
        report["state_artifact"] = snapshot_state(params)
    except (Exception, KeyboardInterrupt) as error:
        report["runner_error"] = report["runner_error"] or f"{phase}_failed"
        print(f"Runner failed during {phase}: {type(error).__name__}", flush=True)
    finally:
        try:
            cleanup()
        except (Exception, KeyboardInterrupt) as error:
            report["runner_error"] = report["runner_error"] or "cleanup_failed"
            print(f"Runner cleanup failed: {type(error).__name__}", flush=True)
    try:
        upload_logs(params, report)
    except Exception as error:
        report["runner_error"] = report["runner_error"] or "log_upload_failed"
        print(f"Log upload failed: {type(error).__name__}", flush=True)
    report |= {"job_id": params["job_id"], "finished_at": now(),
               "elapsed_ms": round((time.monotonic() - started) * 1000)}
    try:
        record_execution(params, report)
    except Exception as error:
        print(f"Execution recording failed: {type(error).__name__}", flush=True)
        return 1
    return 0 if report["status"] == "SUCCEEDED" else 1
if __name__ == "__main__":
    raise SystemExit(main())
