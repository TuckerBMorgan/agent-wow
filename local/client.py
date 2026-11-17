#!/usr/bin/env python3
"""Connect an idle local character; coding agents are started interactively."""
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
    game("auth", "init", data=f"{os.environ['WOW_ACCOUNT']}\n{os.environ['WOW_PASSWORD']}\n")
    for attempt in range(12):
        try:
            game("auth", "login", "--timeout", "10s")
            break
        except subprocess.CalledProcessError:
            if attempt == 11:
                raise
            time.sleep(3)
    selected = Path("/workspace/character.json")
    character = json.loads(selected.read_text()) if selected.exists() else None
    name = character["name"] if character else os.environ["WOW_CHARACTER"]
    if character:
        os.environ["AGENT_WOW_SELECTED_BUILD"] = character["build"]
    characters = json.loads(game("char", "list", "--json", "--timeout", "30s"))["characters"]
    if not any(character["name"] == name for character in characters):
        if selected.exists():
            raise SystemExit("Provisioned character is missing from this account")
        game("char", "create", "--name", name, "--race", "human",
             "--class", os.environ["WOW_CLASS"], "--timeout", "30s")
    os.execvp("agent-wow", ["agent-wow", "char", "play", name, "--timeout", "30s"])
except subprocess.CalledProcessError as error:
    raise SystemExit(f"Client startup failed: {' '.join(error.cmd)} (exit {error.returncode})")
