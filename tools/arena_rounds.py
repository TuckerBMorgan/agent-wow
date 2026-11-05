#!/usr/bin/env python3
"""Run with tools/arena.sh --rounds 5 (uses the existing Linux dev container).

Each player edits build.json and modules/arena/fighter.py, then receives feedback.
Use --manual for hand edits, or --editor to choose another coding-agent command.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import arena_build

EDITOR = "codex --ask-for-approval never exec --sandbox danger-full-access --skip-git-repo-check -"
PROMPT = """Improve your build and harness for a 1v1 duel. Edit only build.json and
modules/arena/fighter.py. Read previous feedback/*/summary.txt to guide your changes.
Builds are level-10 human warriors or mages with starter gear. Keep edits small.
"""


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def rpc(side, method, params=None):
    body = json.dumps(dict(jsonrpc="2.0", id=1, method="arena." + method, params=params or {})).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{8086 + side}/rpc", body,
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["result"]


def edit_players(players, records, editor):
    for side, (player, record) in enumerate(zip(players, records)):
        print(f"  Player {side}: editing (transcript: {record / 'editor.log'})", flush=True)
        if editor:
            with (record / "editor.log").open("w") as log:
                result = subprocess.run(shlex.split(editor), cwd=player, input=PROMPT,
                                        text=True, stdout=log, stderr=log, timeout=600)
            if result.returncode:
                raise RuntimeError(f"Editor exited {result.returncode}; see {record / 'editor.log'}")
        else:
            input(f"{player}\n{PROMPT}Press Enter when ready. ")
        for path in (player / "build.json", player / "modules/arena/fighter.py"):
            if path.is_file():
                shutil.copyfile(path, record / path.name)


def run_duel(args, players, records, world, number):
    builds, errors = [], {}
    for side, player in enumerate(players):
        try:
            build = arena_build.read_build(player / "build.json")
            if build["race"] != "human":
                raise ValueError("Both fighters must be human for the Goldshire starting location")
            builds.append(build)
        except (OSError, ValueError) as error:
            errors[side] = f"Invalid build: {error}"
    if errors:
        return [dict(outcome="error", reason=errors.get(side, "Opponent's build is invalid; duel not run"))
                for side in range(2)]

    processes, characters = [], []
    with ExitStack() as files:
        try:
            for side, (player, record, build) in enumerate(zip(players, records, builds)):
                config = args.output / f"account-{number}-{side}"
                config.mkdir(mode=0o700)
                account = dict(username="ARENA" + secrets.token_hex(5), password=secrets.token_hex(8))
                write_json(config / "auth.json", account)
                arena_build.console(world, f'account create {account["username"]} {account["password"]}', b"Account created")
                characters.append(arena_build.provision(build, None, config, world.id)["character"])
                name = characters[-1]["name"]
                arena_build.console(world, f"tele name {name} Goldshire", f"You are teleporting {name}".encode())
                env = os.environ | dict(AGENT_WOW_CONFIG_DIR=str(config), AGENT_WOW_DATA_DIR=str(config / "data"),
                    AGENT_WOW_AUTH_FILE_PATH=str(config / "auth.json"), AGENT_WOW_REALM_FILE_PATH=str(config / "realm.json"),
                    AGENT_WOW_MODULE_DIR=str(player / "modules"), AGENT_WOW_WORLDRPC_HOST="127.0.0.1",
                    AGENT_WOW_WORLDRPC_PORT=str(8086 + side), AGENT_WOW_WORLDSERVER_ADDRESS="127.0.0.1:8085")
                log = files.enter_context((record / "client.log").open("w"))
                processes.append(subprocess.Popen(["agent-wow", "char", "play", name], env=env, stdout=log, stderr=log))
            deadline = time.monotonic() + 120
            while True:
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("Game client exited; see this round's client.log")
                try:
                    if all(rpc(side, "status")["ready"] for side in range(2)):
                        break
                except urllib.error.URLError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("Game clients did not become ready; see this round's client.log")
                time.sleep(0.5)
            for side in range(2):
                rpc(side, "prepare", dict(opponent=str(characters[1-side]["guid"]), side=side))
            rpc(0, "challenge")
            print("  Duel requested", flush=True)
            deadline = time.monotonic() + args.seconds + 4
            while True:
                states = [rpc(side, "status") for side in range(2)]
                if any(state["error"] for state in states):
                    raise RuntimeError("; ".join(state["error"] for state in states if state["error"]))
                if (any(state["fighter_error"] for state in states)
                        or all(state["winner"] for state in states) or time.monotonic() >= deadline):
                    break
                time.sleep(0.5)
            states = [rpc(side, "status", {"events": True}) for side in range(2)]
            if not all(state["started"] for state in states):
                raise RuntimeError("Duel never started")
            winners = {state["winner"] for state in states if state["winner"]}
            if len(winners) > 1 or winners - {c["name"] for c in characters}:
                raise RuntimeError("Inconsistent duel result")
            winner = next(iter(winners), None)
            results = []
            for side, state in enumerate(states):
                outcome = "tie" if winner is None else "win" if winner == characters[side]["name"] else "lose"
                reason = "server_duel_winner" if winner else "time_limit"
                if any(s["fighter_error"] for s in states):
                    outcome = "error"
                    reason = state["fighter_error"] or "Opponent's fighter failed; duel stopped"
                results.append(dict(outcome=outcome, reason=reason, character=characters[side],
                                    opponent=characters[1-side], events=state["events"]))
            return results
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def save_feedback(records, number, results):
    for record, result in zip(records, results):
        events = result.pop("events", [])
        write_json(record / "events.json", events)
        write_json(record / "result.json", dict(round=number, **result))
        lines = [f"Round {number}: {result['outcome']}", result["reason"]]
        if "character" in result:
            lines.append(f"You: {result['character']['class']}; opponent: {result['opponent']['class']}")
        start = events[0]["time"] if events else 0
        lines += [f"{event['time'] - start:6.1f}s {event['message']}" for event in events if event.get("message")]
        (record / "summary.txt").write_text("\n".join(lines) + "\n")
    print(f"Round {number}: " + ", ".join(f"player {side} {r['outcome']}" for side, r in enumerate(results)), flush=True)


def run(args):
    if sys.platform != "linux":
        raise RuntimeError("Use tools/arena.sh to run in the existing Linux container")
    import docker

    try:
        world = docker.from_env().containers.get(args.worldserver)
    except docker.errors.DockerException as error:
        raise RuntimeError(f"Cannot access worldserver {args.worldserver}: {error}") from error
    if world.status != "running":
        raise RuntimeError(f"Worldserver {args.worldserver} is not running")
    if args.editor == EDITOR:
        if subprocess.run(["codex", "login", "status"], capture_output=True).returncode:
            raise RuntimeError("Log into Codex in agent-wow-arena-dev before running the arena")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    print(f"Arena output: {args.output}", flush=True)
    template = Path(__file__).resolve().parent / "arena"
    with (args.output / "setup.log").open("w") as log:
        subprocess.run(["docker", "build", "-t", "agent-wow-arena:prototype", "-f",
                        str(template / "Dockerfile"), str(template.parent.parent)], stdout=log, stderr=log, check=True)
    descriptor = subprocess.check_output(["docker", "run", "--rm", "agent-wow-arena:prototype", "cat", "/api/module.pb"])
    players = [args.output / f"player-{side}" for side in range(2)]
    for player in players:
        shutil.copytree(template, player / "modules/arena", ignore=shutil.ignore_patterns("__pycache__"))
        (player / "modules/arena/module.pb").write_bytes(descriptor)
        write_json(player / "build.json", {"race": "human", "class": "warrior", "gear": "starter"})
    for number in range(1, args.rounds + 1):
        print(f"Round {number}/{args.rounds}", flush=True)
        records = [player / "feedback" / f"round-{number}" for player in players]
        for record in records:
            record.mkdir(parents=True)
        try:
            edit_players(players, records, args.editor)
            results = run_duel(args, players, records, world, number)
        except Exception as error:
            save_feedback(records, number, [dict(outcome="error", reason=f"Run stopped: {error}") for _ in players])
            raise
        save_feedback(records, number, results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=Path("tmp") / time.strftime("arena-%Y%m%d-%H%M%S"))
    parser.add_argument("--worldserver", default="agent-wow-arena-prototype-worldserver-1")
    editor = parser.add_mutually_exclusive_group()
    editor.add_argument("--editor", default=EDITOR, help="Coding-agent command; defaults to Codex in the dev container")
    editor.add_argument("--manual", dest="editor", action="store_const", const=None, help="Pause for manual edits")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--seconds", type=int, default=90)
    args = parser.parse_args()
    if args.rounds < 1 or args.seconds < 1:
        parser.error("rounds and seconds must be positive")
    try:
        run(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Arena stopped: {error}\nOutput: {args.output}\n")
