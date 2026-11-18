#!/usr/bin/env python3
"""Local realm + two clients. First start:
python3 local/run.py up

Open either workspace: python3 local/run.py exec --user node client-1 bash
Inside it, run codex and sign in with your subscription account.
After both agents select builds and write harnesses, exit Codex and run local/duel.py.
Inspect: python3 local/run.py ps; stop: python3 local/run.py down
Workspaces and logins persist until their Docker volumes are removed.
"""
import os
from pathlib import Path
import secrets
import subprocess
import sys

from catalog import BUILDS, fetch


root = Path(__file__).resolve().parents[1]
state = root / "tmp/local"
state.mkdir(parents=True, exist_ok=True, mode=0o700)
os.umask(0o077)
env_file = state / ".env"
if not env_file.exists():
    values = {"LOCAL_DB_PASSWORD": secrets.token_hex(24),
              "LOCAL_ALPHA_PASSWORD": secrets.token_hex(8),
              "LOCAL_BETA_PASSWORD": secrets.token_hex(8)}
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
values['DUEL_BUILDS'] = str(BUILDS)
env_file.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
(state / "accounts.txt").write_text(
    f"account create LOCALALPHA {values['LOCAL_ALPHA_PASSWORD']}\n"
    f"account create LOCALBETA {values['LOCAL_BETA_PASSWORD']}\n")
compose = ["docker", "compose", "--env-file", str(env_file), "-f", str(root / "local/compose.yaml")]
args = sys.argv[1:] or ["up"]
if args == ["up"]:
    fetch()
    duel = state / "duel-workspace"
    revision = "3038885c717b6cc79e8ad5ac9f5643e1b3d8e756"
    if not duel.exists():
        subprocess.run(["git", "clone", "--depth", "1",
                        "https://github.com/TuckerBMorgan/agent-wow-workspace-1v1-arena.git", str(duel)], check=True)
    head = subprocess.run(["git", "-C", str(duel), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != revision:
        subprocess.run(["git", "-C", str(duel), "fetch", "--depth", "1", "origin", revision], check=True)
        subprocess.run(["git", "-C", str(duel), "checkout", "-B", "main", "FETCH_HEAD"], check=True)
    if "WORKSPACE_IMAGE" not in os.environ:
        subprocess.run(["docker", "build", "-t", "agent-wow-workspace:local",
                        "--build-arg", "WORKSPACE_REF=4d50bd316ae2498314f9ee5f4edcdaad7b06c216",
                        "-f", "benchmark/Dockerfile", "."], cwd=root, check=True)
    subprocess.run(compose + ["build", "client-1"], check=True)
    subprocess.run(compose + ["up", "-d", "--wait", "--wait-timeout", "1800"], check=True)
    subprocess.run([sys.executable, str(root / "local/harness/install.py")], check=True)
    raise SystemExit(0)
os.execvp("docker", compose + args)
