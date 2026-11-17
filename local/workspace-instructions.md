# Instructions for AI agents

This workspace is for you to select character builds and write gameplay modules
that win 1v1 duels on AzerothCore using agent-wow.

Useful resources to reference:
- agent-wow: https://github.com/agent-wow/agent-wow
- azerothcore: https://github.com/azerothcore/azerothcore-wotlk
- wow client-data: https://github.com/wowgaming/client-data/releases
- agent-wow module template: https://github.com/agent-wow/go-module-template

## Arena rounds

During an arena run, you work on your assigned branch in rounds: choose a build,
improve your gameplay modules, submit, review match results, and repeat. Your
workspace persists between rounds.

- Pull the latest feedback before editing and read previous results and match
  logs in `./feedback/`. There may be no feedback in the first round.
- Choose an existing `./builds/<name>/build.json` that meets the run's restrictions.
  Read `./builds/index.json` for the available builds, sources and limitations.
  Do not create or edit builds or the index. You may switch builds between rounds.
- You may create or change your gameplay modules and supporting scripts.
  Implement `start(context)` and `tick(observation, memory)` in
  `./modules/duel/harness.py`; the shared connector calls tick every 100 ms.
  Use the character and opponent details supplied for each match.
- Select your build in `./next-match.json`, for example:
  `{"build": "builds/death-knight-unholy-pvp/build.json"}`.
- For harness preparation, select a build, implement the callbacks, run offline
  checks, and finish with a short description. Stop there; the coordinator will
  request character provisioning and live matches separately.
- Commit and push to your assigned branch only when the coordinator requests
  a submission.

The coordinator records your submitted commit and uses its selected build and
harness against every opponent that round. It provisions characters, starts
matches, and writes your results (win, lose, or tie) and match logs back to your
branch under `./feedback/` before your next turn. Do not modify that feedback,
the coordinator's files, or the opponent's workspace. Resume editing when asked
to begin the next round.

## System checks

Check these requirements before gameplay. If the game connection is unavailable,
report it and continue choosing a build and writing the harness; postpone live
tests until it is restored.

### 1. Dependencies

- Linux
- AzerothCore servers
- Go 1.27.1 or newer
- Docker and Docker Compose
- `protoc` CLI
- `agent-wow` CLI

### 2. Auth

Run `agent-wow auth status` to check if your auth session is valid. If it fails,
report the error; do not log in again while a gameplay session is running.

### 3. Local Docker setup

- AzerothCore runs in separate containers at `authserver:3724` and
  `worldserver:8085`. The realm may advertise `127.0.0.1:8085`; inside this
  workspace that is the wrong address. The client already overrides it.
- `docker ps` shows only this workspace's gameplay modules, so an empty list
  does not mean the game servers are down. Use the server addresses above.
- If sandboxed network or Docker checks say "operation not permitted" or
  "permission denied", retry the check with the available approval mechanism;
  do not interpret sandbox restrictions as a server outage.

## Gameplay sessions

- The local container starts its character session automatically. Its control
  API is `http://127.0.0.1:8086/rpc`; reuse it rather than starting another client.
- While connected, do not run `agent-wow char list`, `char create`, `char delete`,
  another `char play`, or `auth login` on this account. A second world login
  disconnects the existing character session. A different RPC port does not help.
- The coordinator provisions the selected build and prepares matches. You can
  choose a build and edit the harness before a character is connected.
- A dropped game connection leaves the workspace and Codex running. Inspect
  `./logs/session.log`. If the control API is down and no `agent-wow` process is
  running, reconnect with `/usr/local/bin/local-client &`.

## Workspace directories

Any assets used or generated for your task must be kept within this workspace. That
includes configuration files, data, modules, scripts, etc. Some directories are available
for you to use.

- `./modules`: agent-wow modules you build.
- `./builds`: Supplied character builds and their index; select one without editing.
- `./next-match.json`: Selects the build to submit for the next round.
- `./feedback`: Results and match logs supplied by the coordinator.
- `./scripts`: Scripts you use to interact with the gameplay session.
- `./journal`: Relevant information and findings that can be useful in future sessions.
- `./logs`: Any log outputs for debugging.
- `./.sandbox`: Not relevant for gameplay. Do not run, edit, or save anything here.

## Required task journal

Maintain `./journal/events.jsonl` for every task. Use JSON Lines: one complete JSON
object per line, with no Markdown or surrounding array. Append and flush records
as events happen; do not overwrite existing entries or wait until the task ends.

Every record must include:

- `timestamp`: the actual UTC time in ISO 8601 format, such as
  `2026-10-20T20:00:00.000Z`.
- `run_id`: a unique string chosen when work begins and reused for this task's
  records, so positions and timing can be matched to the same run.
- `event`: `task_started`, `position`, or `task_finished`.
- `character`: the character's name, or `null` if no character has been selected.

Record these events:

- **Task start:** append `task_started` immediately when you begin work, before
  system checks, research, or gameplay.
- **Character positions:** append `position` immediately after entering the world,
  at least once every five seconds while a gameplay session is active, and after
  a map change. Collect these automatically in the gameplay module or control
  script. Include `map_id` (integer) and `x`, `y`, `z` (numbers in the game's world
  coordinate system). Record the character's observed position from the game
  client or API, not a planned destination or an invented coordinate. If multiple
  characters are played, record each one's positions under its own name.
- **Task finish:** append `task_finished` before your final response, including
  when you stop because of a blocker or failure. Add `outcome`, one of `completed`,
  `blocked`, `failed`, or `interrupted`, and `elapsed_seconds`, a number calculated
  from this run's start and finish times. Use `completed` only when the task is
  complete. If you receive a stop request and can still write, record
  `interrupted` before exiting.

Example format (illustrative values only; write actual observations and times):

```jsonl
{"timestamp":"2026-10-20T20:00:00.000Z","run_id":"example-run","event":"task_started","character":null}
{"timestamp":"2026-10-20T20:05:00.000Z","run_id":"example-run","event":"position","character":"ExampleOrc","map_id":1,"x":123.4,"y":456.7,"z":10.2}
{"timestamp":"2026-10-20T20:30:00.000Z","run_id":"example-run","event":"task_finished","character":"ExampleOrc","outcome":"completed","elapsed_seconds":1800.0}
```
