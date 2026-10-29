#!/opt/venv/bin/python3
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone

import control_plane


WORKSPACE = Path("/workspace")
PRIVATE = Path("/run/agent-wow-private")
PARAMS = PRIVATE / "params.json"
OUTPUT = PRIVATE / "output"
LOGS = WORKSPACE / "logs"
COMPOSE = [
    "docker", "compose",
    "--project-directory", str(WORKSPACE / "server"),
    "--file", str(WORKSPACE / "server/compose.yaml"),
]
STATE = {"agent": None, "dockerd": None, "docker_log": None}


def now():
    return datetime.now(timezone.utc).isoformat()


def trusted_env():
    environment = dict(os.environ)
    for name in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_AUTH_JSON",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        environment.pop(name, None)
    return environment

# what is this function for
def checked(command, **kwargs):
    kwargs.setdefault("env", trusted_env())
    return subprocess.run(command, check=True, **kwargs)


def load_job():
    # grab the params for this job
    params = control_plane.load_params(os.environ["PARAMS_S3_URI"])
    # make the folder we need for all of this, and then write the files into it
    PRIVATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    PRIVATE.chmod(0o700)
    PARAMS.write_text(json.dumps(params, indent=2) + "\n")
    PARAMS.chmod(0o600)
    return params, control_plane.load_credential(params)


def prepare_workspace():
    # make all of the folders we need, maybe we could move this to the dockerfile itself?
    for name in ("config", "data", "modules", "journal", "logs", "server", "tmp"):
        (WORKSPACE / name).mkdir(parents=True, exist_ok=True)

    # why are we doing this copy?
    for source, destination in (
        ("/opt/agent-wow", "agent-wow"),
        ("/opt/go-module-template", "go-module-template"),
    ):
        shutil.copytree(source, WORKSPACE / destination, dirs_exist_ok=True)
    # there is a lot of copying, why are we doing it this way?
    shutil.copy2("/opt/workspace-template/AGENTS.md", WORKSPACE / "AGENTS.md")
    (WORKSPACE / "CLAUDE.md").symlink_to("AGENTS.md")
    (WORKSPACE / "azerothcore-reference").symlink_to("/opt/azerothcore-reference")
    shutil.copy2("/opt/server/compose.yaml", WORKSPACE / "server/compose.yaml")

    env_file = WORKSPACE / "server/.env"
    env_file.write_text(f"MYSQL_ROOT_PASSWORD={secrets.token_hex(24)}\n")
    env_file.chmod(0o600)


def start_server():

    # create our log files
    docker_log = (LOGS / "dockerd.log").open("w")
    STATE["docker_log"] = docker_log
    STATE["dockerd"] = subprocess.Popen(
        [
            "dockerd", "--host=unix:///var/run/docker.sock",
            "--feature=containerd-snapshotter=false",
            "--storage-driver=overlay2",
        ],
        stdout=docker_log, stderr=subprocess.STDOUT, env=trusted_env(),
    )

    # start up the internal dockerimage
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0:
            break
        if STATE["dockerd"].poll() is not None:
            raise RuntimeError("Docker stopped during startup")
        time.sleep(1)
    else:
        raise RuntimeError("Docker did not start")

    # is this still right?
    archives = sorted(Path("/opt/server-images").glob("*.tar"))
    if len(archives) != 5:
        raise RuntimeError("expected five cached server images")

    for archive in archives:
        checked(["docker", "load", "--input", str(archive)])

    checked(COMPOSE + [
        "up", "--wait", "--wait-timeout", "1800", "--pull", "never",
    ])


def prepare_accounts(params):
    checked(["prepare_wow_account.py"])
    viewer_uri = subprocess.check_output(
        ["prepare_viewer_account.py", "--params", str(PARAMS)],
        text=True, env=trusted_env(),
    ).strip()
    if not viewer_uri.startswith("s3://"):
        raise ValueError("viewer helper did not return an S3 URI")
    started_at = now()
    control_plane.mark_running(params, started_at, viewer_uri)
    return started_at


def agent_env():
    environment = trusted_env()
    for name in tuple(environment):
        if name.startswith("AWS_") or name in {
            "PARAMS_S3_URI", "ECS_CONTAINER_METADATA_URI",
            "ECS_CONTAINER_METADATA_URI_V4", "DOCKER_HOST",
        }:
            environment.pop(name, None)
    environment.update(
        HOME="/home/node",
        TMPDIR=str(WORKSPACE / "tmp"),
        AWS_EC2_METADATA_DISABLED="true",
    )
    return environment


def sandbox(command):
    return [
        "runuser", "-u", "node", "--",
        "bwrap", "--die-with-parent", "--new-session", "--unshare-pid",
        "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/run", "--tmpfs", "/root", "--tmpfs", "/var/lib/docker",
        "--bind", str(WORKSPACE), str(WORKSPACE),
        "--tmpfs", str(WORKSPACE / "server"),
        "--bind", "/home/node", "/home/node",
        "--chdir", str(WORKSPACE), "--", *command,
    ]


def harness(params, credential):
    environment = agent_env()
    checked(["chown", "-R", "node:node", str(WORKSPACE), "/home/node"])
    if params["harness"] == "claude":
        environment.update(
            CLAUDE_CODE_OAUTH_TOKEN=credential,
            DISABLE_AUTOUPDATER="1",
        )
        command = [
            "claude", "-p", "--model", params["model"],
            "--effort", params["effort"],
            "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose",
        ]
        return sandbox(command), environment

    auth = json.loads(credential)
    tokens = auth.get("tokens", {})
    if auth.get("auth_mode") != "chatgpt" or not all(
        tokens.get(name) for name in ("access_token", "refresh_token", "id_token")
    ):
        raise ValueError("invalid Codex subscription login")
    home = Path("/home/node/.codex")
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    auth.pop("OPENAI_API_KEY", None)
    (home / "auth.json").write_text(json.dumps(auth))
    (home / "auth.json").chmod(0o600)
    (home / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        'forced_login_method = "chatgpt"\n'
    )
    environment["CODEX_HOME"] = str(home)
    command = [
        "codex",
        "--config", 'cli_auth_credentials_store="file"',
        "--config", 'forced_login_method="chatgpt"',
        "--model", params["model"],
        "--config", f'model_reasoning_effort="{params["effort"]}"',
        "--ask-for-approval", "never",
        "exec", "--sandbox", "workspace-write",
        "--cd", str(WORKSPACE), "--skip-git-repo-check",
        "--color", "never",
        "--output-last-message", str(LOGS / "final-report.txt"), "-",
    ]
    return sandbox(command), environment


def run_agent(params, credential):
    command, environment = harness(params, credential)
    with (LOGS / "agent.log").open("w") as log:
        agent = subprocess.Popen(
            command, cwd=WORKSPACE, env=environment,
            stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
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

def cleanup():
    agent = STATE["agent"]
    dockerd = STATE["dockerd"]
    if agent and agent.poll() is None:
        os.killpg(agent.pid, signal.SIGTERM)
    if dockerd and dockerd.poll() is None:
        subprocess.run(
            COMPOSE + ["down", "--timeout", "30"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=trusted_env(),
        )
        dockerd.terminate()
        try:
            dockerd.wait(timeout=30)
        except subprocess.TimeoutExpired:
            dockerd.kill()
            dockerd.wait()
    if STATE["docker_log"]:
        STATE["docker_log"].close()


def stop(signum, _frame):
    agent = STATE["agent"]
    if agent and agent.poll() is None:
        os.killpg(agent.pid, signum)
    raise KeyboardInterrupt


def main():
    # why are we doing this?
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # null out
    params = credential = None
    phase = "load_job"
    started = time.monotonic()
    started_at = now()
    report = {
        "agent_exit_code": None,
        "agent_timed_out": False,
        "runner_error": None,
    }

    try:
        params, credential = load_job()
        phase = "prepare_workspace"
        prepare_workspace()
        phase = "start_server"
        start_server()
        phase = "prepare_accounts"
        started_at = prepare_accounts(params)
        phase = "sandbox_setup"
        checked([
            "iptables", "-I", "OUTPUT", "-m", "owner",
            "--uid-owner", "node", "-d", "169.254.0.0/16",
            "-j", "REJECT",
        ])
        phase = "run_agent"
        report["agent_exit_code"], report["agent_timed_out"] = run_agent(
            params, credential
        )
    except (Exception, KeyboardInterrupt) as error:
        report["runner_error"] = f"{phase}_failed"
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / "runner.log").write_text(
            f"Runner failed during {phase}: {type(error).__name__}\n"
        )
    finally:
        cleanup()

    if not params:
        return 1
    report.update(
        job_id=params["job_id"], started_at=started_at, finished_at=now(),
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "execution.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    try:
        control_plane.record_execution(params, report)
    except Exception as error:
        print(f"Execution recording failed: {type(error).__name__}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
