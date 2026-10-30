#!/opt/venv/bin/python3
import gzip
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import boto3
import docker
from botocore.exceptions import ClientError

WORKSPACE = Path("/workspace")
LOGS = WORKSPACE / "logs"
COMPOSE = ["docker", "compose", "--project-directory", f"{WORKSPACE}/server", "--file", f"{WORKSPACE}/server/compose.yaml"]
STATE = {"agent": None, "dockerd": None, "docker_log": None}
TABLE = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
S3 = boto3.client("s3")
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
    ensure_account(env=trusted_env())
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
    connection = account | {"schema_version": 1, "job_id": params["job_id"],
                            "batch_job_id": os.environ["AWS_BATCH_JOB_ID"], "auth_port": 3724,
                            "world_port": 8085, "character": "Observer"}
    viewer_uri = put_s3_json(f'viewer/{params["job_id"]}/connection.json', connection)
    started_at = now()
    mark_running(params, started_at, viewer_uri)
    return started_at

def prepare_workspace():
    for name in ("config", "data", "modules", "journal", "logs", "server", "tmp"):
        (WORKSPACE / name).mkdir(parents=True, exist_ok=True)
    for source, destination in (("/opt/agent-wow", "agent-wow"), ("/opt/go-module-template", "go-module-template")):
        shutil.copytree(source, WORKSPACE / destination, dirs_exist_ok=True)
    shutil.copy2("/opt/workspace-template/AGENTS.md", WORKSPACE / "AGENTS.md")
    (WORKSPACE / "CLAUDE.md").symlink_to("AGENTS.md")
    (WORKSPACE / "azerothcore-reference").symlink_to("/opt/azerothcore-reference")
    shutil.copy2("/opt/server/compose.yaml", WORKSPACE / "server/compose.yaml")
    env_file = WORKSPACE / "server/.env"
    env_file.write_text(f"MYSQL_ROOT_PASSWORD={secrets.token_hex(24)}\n")
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

def agent_env():
    env = trusted_env()
    for name in tuple(env):
        if name.startswith("AWS_") or name in {"ARTIFACTS_BUCKET", "CREDENTIAL_SECRET_ARN", "JOB_ID",
                                                "JOBS_TABLE", "ECS_CONTAINER_METADATA_URI",
                                                "ECS_CONTAINER_METADATA_URI_V4", "DOCKER_HOST"}:
            env.pop(name, None)
    return env | {"HOME": "/home/node", "TMPDIR": f"{WORKSPACE}/tmp", "AWS_EC2_METADATA_DISABLED": "true"}

def sandbox(command):
    return ["runuser", "-u", "node", "--", "bwrap", "--die-with-parent", "--new-session", "--unshare-pid",
            "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--tmpfs", "/run", "--tmpfs", "/root",
            "--tmpfs", "/var/lib/docker", "--bind", str(WORKSPACE), str(WORKSPACE), "--tmpfs", f"{WORKSPACE}/server",
            "--bind", "/home/node", "/home/node", "--chdir", str(WORKSPACE), "--", *command]

def harness(params, credential):
    env = agent_env()
    if params["harness"] == "claude":
        env |= {"CLAUDE_CODE_OAUTH_TOKEN": credential, "DISABLE_AUTOUPDATER": "1"}
        command = ["claude", "-p", "--model", params["model"], "--effort", params["effort"],
                   "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"]
    else:
        auth, home = json.loads(credential), Path("/home/node/.codex")
        tokens = auth.get("tokens", {})
        if auth.get("auth_mode") != "chatgpt" or not all(tokens.get(name) for name in
                                                           ("access_token", "refresh_token", "id_token")): raise ValueError("invalid Codex subscription login")
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        auth.pop("OPENAI_API_KEY", None)
        write_json(home / "auth.json", auth)
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n')
        env["CODEX_HOME"] = str(home)
        command = ["codex", "--config", 'cli_auth_credentials_store="file"', "--config",
            'forced_login_method="chatgpt"', "--config", 'sandbox_workspace_write.network_access=true',
            "--model", params["model"], "--config",
            f'model_reasoning_effort="{params["effort"]}"', "--ask-for-approval", "never", "exec",
            "--sandbox", "workspace-write", "--cd", str(WORKSPACE), "--skip-git-repo-check",
            "--color", "never", "--output-last-message", str(LOGS / "final-report.txt"), "-"]
    checked(["chown", "-R", "node:node", str(WORKSPACE), "/home/node"])
    return sandbox(command), env

def run_agent(params, credential):
    command, env = harness(params, credential)
    with (LOGS / "agent.log").open("w") as log:
        agent = subprocess.Popen(command, cwd=WORKSPACE, env=env, stdin=subprocess.PIPE, stdout=log,
                                 stderr=subprocess.STDOUT, text=True, start_new_session=True)
        STATE["agent"] = agent
        try:
            agent.communicate(params["prompt"], timeout=params["timeout_seconds"])
            return agent.returncode, False
        except subprocess.TimeoutExpired:
            os.killpg(agent.pid, signal.SIGTERM)
            try:
                agent.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(agent.pid, signal.SIGKILL)
                agent.wait()
            return 124, True
        finally:
            STATE["agent"] = None

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
    if STATE["agent"] and STATE["agent"].poll() is None:
        os.killpg(STATE["agent"].pid, signal.SIGTERM)
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

def stop(signum, _frame):
    if STATE["agent"] and STATE["agent"].poll() is None:
        os.killpg(STATE["agent"].pid, signum)
    raise KeyboardInterrupt

def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    params, credential = {"job_id": os.environ["JOB_ID"]}, None
    phase, started, started_at = "load_job", time.monotonic(), now()
    report = {"agent_exit_code": None, "agent_timed_out": False, "runner_error": None,
              "state_artifact": None, "log_artifacts": {}}
    try:
        params, credential = load_job()
        phase = "prepare_workspace"; prepare_workspace()
        phase = "start_server"; start_server()
        phase = "prepare_accounts"; started_at = prepare_accounts(params)
        phase = "sandbox_setup"
        checked(["iptables", "-I", "OUTPUT", "-m", "owner", "--uid-owner", "node", "-d", "169.254.0.0/16", "-j", "REJECT"])
        phase = "run_agent"
        report["agent_exit_code"], report["agent_timed_out"] = run_agent(params, credential)
        phase = "snapshot_state"
        report["state_artifact"] = snapshot_state(params)
    except (Exception, KeyboardInterrupt) as error:
        report["runner_error"] = f"{phase}_failed"
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
    report |= {"job_id": params["job_id"], "started_at": started_at, "finished_at": now(),
               "elapsed_ms": round((time.monotonic() - started) * 1000)}
    try:
        record_execution(params, report)
    except Exception as error:
        print(f"Execution recording failed: {type(error).__name__}", flush=True)
        return 1
    return 0 if report["status"] == "SUCCEEDED" else 1
if __name__ == "__main__":
    raise SystemExit(main())
