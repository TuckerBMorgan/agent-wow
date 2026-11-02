#!/opt/venv/bin/python3
"""Run one agent on its disposable client machine."""

import json
import os
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.config import Config

WORKSPACE = Path("/workspace")
LOGS = WORKSPACE / "logs"
STATE = {"agent": None, "dockerd": None, "docker_log": None, "transcript_error": None}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def agent_env(server_host):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AWS_") and key not in {
               "JOB_INPUT_URL", "ARTIFACTS_BUCKET", "OPENAI_API_KEY", "CODEX_API_KEY",
               "OPENAI_BASE_URL", "CODEX_AUTH_JSON", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}}
    return env | {"HOME": "/home/node", "TMPDIR": str(WORKSPACE / "tmp"),
                  "DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": "default",
                  "AGENT_WOW_AUTHSERVER_HOST": server_host, "AGENT_WOW_AUTHSERVER_PORT": "3724",
                  "AGENT_WOW_WORLDSERVER_ADDRESS": f"{server_host}:8085",
                  "AGENT_WOW_WORLDRPC_HOST": "127.0.0.1", "AGENT_WOW_WORLDRPC_PORT": "8086",
                  "AWS_EC2_METADATA_DISABLED": "true"}


def prepare_workspace(player, host):
    for name in ("config", "data", "modules", "journal", "logs", "tmp"):
        (WORKSPACE / name).mkdir(parents=True, exist_ok=True)
    for source, destination in (("/opt/agent-wow", "agent-wow"), ("/opt/go-module-template", "go-module-template")):
        shutil.copytree(source, WORKSPACE / destination, dirs_exist_ok=True)
    shutil.copy2("/opt/workspace-template/AGENTS.md", WORKSPACE / "AGENTS.md")
    (WORKSPACE / "CLAUDE.md").symlink_to("AGENTS.md")
    (WORKSPACE / "azerothcore-reference").symlink_to("/opt/azerothcore-reference")
    write_json(WORKSPACE / "config/auth.json", player)
    write_json(WORKSPACE / "config.yaml", {
        "config_dir": str(WORKSPACE / "config"), "data_dir": str(WORKSPACE / "data"),
        "module_dir": str(WORKSPACE / "modules"), "authserver": {"host": host, "port": 3724},
        "worldserver_address": f"{host}:8085", "worldrpc": {"host": "127.0.0.1", "port": 8086}})


def start_docker():
    log = (LOGS / "client-dockerd.log").open("w")
    STATE["docker_log"] = log
    STATE["dockerd"] = subprocess.Popen([
        "dockerd", "--host=unix:///var/run/docker.sock", "--feature=containerd-snapshotter=false",
        "--storage-driver=overlay2"], stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 120
    while subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        if STATE["dockerd"].poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError("client Docker did not start")
        time.sleep(1)
    shutil.chown("/var/run/docker.sock", group="node")
    Path("/var/run/docker.sock").chmod(0o660)


def harness(params, credential, server_host):
    env = agent_env(server_host)
    if params["harness"] == "claude":
        env |= {"CLAUDE_CODE_OAUTH_TOKEN": credential, "DISABLE_AUTOUPDATER": "1"}
        command = ["claude", "-p", "--model", params["model"], "--effort", params["effort"],
                   "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"]
    elif params["harness"] == "codex":
        auth = json.loads(credential)
        tokens = auth.get("tokens", {})
        if auth.get("auth_mode") != "chatgpt" or not all(tokens.get(name) for name in
                ("access_token", "refresh_token", "id_token")):
            raise ValueError("invalid Codex subscription login")
        home = Path("/home/node/.codex")
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        auth.pop("OPENAI_API_KEY", None)
        write_json(home / "auth.json", auth)
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n')
        env["CODEX_HOME"] = str(home)
        command = ["codex", "--config", 'cli_auth_credentials_store="file"', "--config",
            'forced_login_method="chatgpt"', "--model", params["model"], "--config",
            f'model_reasoning_effort="{params["effort"]}"', "--ask-for-approval", "never", "exec",
            "--sandbox", "danger-full-access", "--cd", str(WORKSPACE), "--skip-git-repo-check",
            "--color", "never", "--json", "--output-last-message", str(LOGS / "final-report.txt"), "-"]
    else:
        raise ValueError("unsupported agent harness")
    subprocess.run(["chown", "-R", "node:node", str(WORKSPACE), "/home/node"], check=True)
    return ["runuser", "-u", "node", "--", *command], env


def stop_agent():
    agent = STATE["agent"]
    if agent and agent.poll() is None:
        try:
            os.killpg(agent.pid, signal.SIGTERM)
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(agent.pid, signal.SIGKILL)
            agent.wait()
        except ProcessLookupError:
            pass


def upload_log(s3, bucket, prefix, name):
    path = LOGS / name
    if path.is_file():
        path.resolve().relative_to(WORKSPACE.resolve())
        s3.put_object(Bucket=bucket, Key=prefix + name, Body=path.read_bytes(),
            ServerSideEncryption="AES256", ContentType="text/plain; charset=utf-8")


def stream_agent_log(s3, bucket, prefix, stopped):
    uploaded = None
    while not stopped.wait(15):
        try:
            info = (LOGS / "agent.log").stat()
            version = (info.st_size, info.st_mtime_ns)
            if version != uploaded:
                upload_log(s3, bucket, prefix, "agent.log")
                uploaded = version
        except Exception as error:
            print(f"Live log upload failed: {type(error).__name__}", flush=True)


def record_output(stream, log, transcript, messages):
    try:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                event = {"type": "harness_output", "text": line.rstrip("\n")}
            if not isinstance(event, dict):
                event = {"type": "harness_output", "text": line.rstrip("\n")}
            transcript.write(json.dumps(event) + "\n")
            item = event.get("item", {})
            if not isinstance(item, dict):
                item = {}
            text = item.get("text") or item.get("aggregated_output")
            if item.get("command"):
                text = f'$ {item["command"]}\n{text or ""}'
            if not isinstance(text, str):
                text = line
            log.write((text or line).rstrip("\n") + "\n")
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                messages.write(f'Assistant:\n{item["text"]}\n\n')
            elif event.get("type") == "assistant":
                message = event.get("message", {})
                for block in (message.get("content") or []) if isinstance(message, dict) else []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        messages.write(f'Assistant:\n{block["text"]}\n\n')
    except Exception as error:
        STATE["transcript_error"] = type(error).__name__
        print(f"Transcript capture failed: {type(error).__name__}", flush=True)
    finally:
        stream.close()


def run_agent(params, command, env, s3, bucket, prefix):
    with (LOGS / "agent.log").open("w", buffering=1) as log, \
            (LOGS / "transcript.jsonl").open("w", buffering=1) as transcript, \
            (LOGS / "messages.txt").open("w", buffering=1) as messages:
        transcript.write(json.dumps({"type": "input", "role": "user", "text": params["prompt"]}) + "\n")
        messages.write(f'User:\n{params["prompt"]}\n\n')
        agent = subprocess.Popen(command, cwd=WORKSPACE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, text=True, encoding="utf-8", errors="replace", start_new_session=True)
        STATE["agent"] = agent
        STATE["transcript_error"] = None
        reader = threading.Thread(target=record_output, args=(agent.stdout, log, transcript, messages), daemon=True)
        reader.start()
        stopped = threading.Event()
        uploader = threading.Thread(target=stream_agent_log, args=(s3, bucket, prefix, stopped), daemon=True)
        uploader.start()
        try:
            try:
                agent.stdin.write(params["prompt"])
                agent.stdin.close()
            except BrokenPipeError:
                pass
            agent.wait(timeout=int(params["timeout_seconds"]))
            return agent.returncode, False
        except subprocess.TimeoutExpired:
            stop_agent()
            return 124, True
        finally:
            stop_agent()
            reader.join(timeout=10)
            if reader.is_alive():
                STATE["transcript_error"] = "output_stream_still_open"
            STATE["agent"] = None
            stopped.set()
            uploader.join()
            if STATE["transcript_error"]:
                raise RuntimeError("agent transcript capture failed")


def upload_artifacts(s3, bucket, prefix):
    archive = Path("/run/agent-wow-client-artifacts.tar.gz")
    excluded = {"config", "config.yaml", "auth.json", "data", "tmp", ".git", ".codex", ".claude",
                ".cache", "node_modules", "__pycache__", ".venv", "venv"}

    def keep(info):
        parts = Path(info.name).parts
        if any(part in excluded or part == ".env" or part.startswith(".env.") for part in parts):
            return None
        return info if info.isfile() or info.isdir() else None

    with tarfile.open(archive, "w:gz", dereference=False) as bundle:
        for path in WORKSPACE.iterdir():
            bundle.add(path, arcname=path.name, filter=keep)
    archive.chmod(0o600)
    with archive.open("rb") as body:
        s3.put_object(Bucket=bucket, Key=prefix + "client-artifacts.tar.gz", Body=body,
            ServerSideEncryption="AES256", ContentType="application/gzip")


def stop(_signum, _frame):
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    s3 = boto3.client("s3", config=Config(connect_timeout=5, read_timeout=10,
                                        retries={"max_attempts": 2}))
    # EC2 instance-profile sessions use the instance ID as their session name.
    # IAM restricts writes to this exact caller's clients/<aws:userid>/ prefix.
    identity = boto3.client("sts").get_caller_identity()["UserId"]
    prefix = f"clients/{identity}/"
    bucket = os.environ["ARTIFACTS_BUCKET"]
    params, phase = {"job_id": os.environ["JOB_ID"]}, "load_input"
    report = {"agent_exit_code": None, "agent_timed_out": False, "runner_error": None}
    try:
        url = os.environ.pop("JOB_INPUT_URL")
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.load(response)
        del url
        params, credential, host = payload["params"], payload["credential"], payload["server_host"]
        phase = "prepare_workspace"
        prepare_workspace(payload["player"], host)
        del payload
        phase = "start_docker"
        start_docker()
        phase = "prepare_harness"
        command, env = harness(params, credential, host)
        del credential
        phase = "game_login"
        subprocess.run(["runuser", "-u", "node", "--", "agent-wow", "auth", "login", "--timeout", "10s"],
            cwd=WORKSPACE, env=env, check=True, capture_output=True, timeout=30)
        subprocess.run(["runuser", "-u", "node", "--", "agent-wow", "char", "list", "--json"],
            cwd=WORKSPACE, env=env, check=True, capture_output=True, timeout=30)
        s3.put_object(Bucket=bucket, Key=prefix + "ready.json", ServerSideEncryption="AES256",
                      Body=json.dumps({"job_id": params["job_id"]}).encode())
        phase = "run_agent"
        report["agent_exit_code"], report["agent_timed_out"] = run_agent(params, command, env, s3, bucket, prefix)
    except (Exception, KeyboardInterrupt) as error:
        report["runner_error"] = f"client_{phase}_failed"
        print(f"Client failed during {phase}: {type(error).__name__}", flush=True)
    finally:
        stop_agent()
    try:
        for name in ("agent.log", "final-report.txt", "client-dockerd.log", "transcript.jsonl", "messages.txt"):
            upload_log(s3, bucket, prefix, name)
        upload_artifacts(s3, bucket, prefix)
    except Exception as error:
        report["runner_error"] = report["runner_error"] or "client_artifact_upload_failed"
        print(f"Client artifact upload failed: {type(error).__name__}", flush=True)
    try:
        s3.put_object(Bucket=bucket, Key=prefix + "client-result.json", ServerSideEncryption="AES256",
            ContentType="application/json", Body=json.dumps(report | {"job_id": params.get("job_id")}).encode())
    finally:
        daemon = STATE["dockerd"]
        if daemon and daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
        if STATE["docker_log"]:
            STATE["docker_log"].close()
    return 0 if report["agent_exit_code"] == 0 and not report["runner_error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
