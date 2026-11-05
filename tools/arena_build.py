#!/usr/bin/env python3
"""Validate an arena build or provision it on the runner's local realm."""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

LEVEL = 10
CHOICES = {
    "human": ["warrior", "mage"],
    "orc": ["warrior", "shaman"],
}
GEAR = "starter"


def read_build(path):
    with path.open() as source:
        build = json.load(source)
    if not isinstance(build, dict) or build.keys() != {"race", "class", "gear"}:
        raise ValueError("A build needs exactly three fields: race, class, and gear")
    if not all(isinstance(value, str) for value in build.values()):
        raise ValueError("Build fields must be strings")
    if build["class"] not in CHOICES.get(build["race"], []):
        raise ValueError("Unsupported race/class combination; see options")
    if build["gear"] != GEAR:
        raise ValueError("This prototype supports the starter gear kit")
    return build


def console(world, command, expected):
    # Use the native console, as the existing server runner does.
    connection = world.attach_socket(params={"stdin": True, "stdout": True, "stream": True})
    try:
        sock = connection._sock
        sock.settimeout(5)
        sock.sendall((command + "\n").encode())
        deadline = time.monotonic() + 15
        reply = b""
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(16384)
            except (TimeoutError, socket.timeout):
                continue
            if not chunk:
                break
            reply = (reply + chunk)[-65536:]
            if expected in reply:
                return
        raise ValueError("World console did not confirm the command")
    finally:
        connection.close()


def provision(build, name, config_dir, worldserver):
    import docker

    if name is not None and not re.fullmatch(r"[A-Z][a-z]{1,11}", name):
        raise ValueError("Use a character name of 2–12 letters with an initial capital")
    config_dir = config_dir.resolve()
    if not (config_dir / "auth.json").is_file():
        raise ValueError("The runner must provide auth.json for a dedicated player account")

    world = docker.from_env().containers.get(worldserver)
    ports = world.attrs["NetworkSettings"]["Ports"]
    if (world.labels.get("com.docker.compose.service") != "worldserver"
            or {"HostIp": "127.0.0.1", "HostPort": "8085"} not in (ports.get("8085/tcp") or [])):
        raise ValueError("Select the runner's worldserver bound to 127.0.0.1:8085")

    env = os.environ | {
        "AGENT_WOW_CONFIG_DIR": str(config_dir),
        "AGENT_WOW_AUTH_FILE_PATH": str(config_dir / "auth.json"),
        "AGENT_WOW_REALM_FILE_PATH": str(config_dir / "realm.json"),
        "AGENT_WOW_DATA_DIR": str(config_dir / "data"),
        "AGENT_WOW_AUTHSERVER_HOST": "127.0.0.1",
        "AGENT_WOW_AUTHSERVER_PORT": "3724",
        "AGENT_WOW_WORLDSERVER_ADDRESS": "127.0.0.1:8085",
    }

    def client(*args):
        result = subprocess.run(["agent-wow", *args], env=env, capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise ValueError(f"agent-wow {' '.join(args[:2])} failed: {result.stderr.strip()}")
        return result.stdout

    def characters():
        return json.loads(client("char", "list", "--json"))["characters"]

    def matches(character):
        return (character["name"] == name and character["race"].lower() == build["race"]
                and character["class"].lower() == build["class"])

    client("auth", "login")
    existing = characters()
    if not existing:
        name_args = ["--name", name] if name else []
        client("char", "create", *name_args, "--race", build["race"], "--class", build["class"])
        existing = characters()
    if name is None and len(existing) == 1:
        name = existing[0]["name"]
    if len(existing) != 1 or not matches(existing[0]) or existing[0]["level"] not in (1, LEVEL):
        raise ValueError("Expected a dedicated account with only the submitted character")
    if existing[0]["level"] == 1:
        console(world, f"character level {name} {LEVEL}", f"You changed level of {name} to {LEVEL}.".encode())
    created = characters()
    if len(created) != 1 or not matches(created[0]) or created[0]["level"] != LEVEL:
        raise ValueError("Client did not confirm the requested character and level")
    return {"build": build, "character": created[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("options", help="List the choices available to an agent")
    commands.add_parser("check", help="Validate an agent submission offline").add_argument("build", type=Path)
    server = commands.add_parser("provision", help="Apply a submission from the trusted server runner")
    server.add_argument("build", type=Path)
    server.add_argument("--name", required=True)
    server.add_argument("--config-dir", type=Path, required=True, help="Dedicated player account's client config")
    server.add_argument("--worldserver", required=True, help="Local Compose worldserver container name or ID")
    args = parser.parse_args()
    try:
        if args.command == "options":
            result = {"level": LEVEL, "race_classes": CHOICES, "gear": [GEAR], "talents": "unspent"}
        else:
            build = read_build(args.build)
            result = {"build": build, "level": LEVEL, "talents": "unspent"}
            if args.command == "provision":
                result = provision(build, args.name, args.config_dir, args.worldserver)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
