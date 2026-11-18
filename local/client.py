#!/usr/bin/env python3
"""Connect the imported fighter; stay idle until the coordinator selects one."""
import json
import os
from pathlib import Path
import subprocess
import time


def game(*args, data=None):
    return subprocess.run(["agent-wow", *args], input=data, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout


log = Path("/workspace/logs/session.log")
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("w") as output:
    os.dup2(output.fileno(), 1)
    os.dup2(output.fileno(), 2)
try:
    selected = Path("/workspace/character.json")
    if not selected.exists():
        raise SystemExit(0)
    character = json.loads(selected.read_text())
    game("auth", "init", data=f"{os.environ['WOW_ACCOUNT']}\n{os.environ['WOW_PASSWORD']}\n")
    for attempt in range(12):
        try:
            game("auth", "login", "--timeout", "10s")
            break
        except subprocess.CalledProcessError:
            if attempt == 11:
                raise
            time.sleep(3)
    name = character["name"]
    deadline = time.monotonic() + 60
    while True:
        roster = json.loads(game("char", "list", "--json", "--timeout", "5s"))
        if any(c["name"] == name for c in roster["characters"]):
            break
        if time.monotonic() >= deadline:
            raise SystemExit("Imported character did not become available")
        time.sleep(1)
    os.environ["AGENT_WOW_SELECTED_BUILD"] = character["build"]
    # char play already verifies that this account owns the selected character.
    os.execvp("agent-wow", ["agent-wow", "char", "play", name, "--timeout", "30s"])
except subprocess.CalledProcessError as error:
    raise SystemExit(f"Client startup failed: {' '.join(error.cmd)} (exit {error.returncode})")
