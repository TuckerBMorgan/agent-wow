# Duel workspace

Select a supplied level-80 build and write a harness that wins 1v1 duels.

- Read `builds/index.json`, then select an existing `builds/<name>/build.json`
  in `next-match.json`, for example:
  `{"build":"builds/death-knight-unholy-pvp/build.json"}`.
- Do not create or edit the supplied builds or index.
- Implement `start(context)` and `tick(observation, memory)` in
  `modules/duel/harness.py`. Read its template for observations and actions.
  The shared connector calls tick every 100 ms.
- For harness preparation, select a build, implement callbacks, run offline
  checks, and stop. The coordinator provisions characters and starts matches.
- Read prior feedback in `feedback/`; do not modify it or the other workspace.
- Commit or push only when the coordinator asks. Keep assets in this workspace.

## Local environment

- AzerothCore is at `authserver:3724` and `worldserver:8085`, not localhost.
  `docker ps` inside this container shows only this client's gameplay modules.
- The workspace stays running independently of its game connection. Before a
  character is provisioned, there is no game session; continue writing the harness.
- After provisioning, reuse the control API at `http://127.0.0.1:8086/rpc`.
  Do not run another `auth login`, `char list/create/delete/play` while connected:
  a second world login on this account disconnects the first.
- Check `logs/session.log` for connection errors. If the API is down and no
  `agent-wow` process is running, reconnect with `/usr/local/bin/local-client &`.
- If sandbox restrictions block Docker or networking, use the available approval
  mechanism; do not treat a permission denial as a server outage.
- Reference source is in `agent-wow/` and `/opt/azerothcore-reference`.
  Do not change server internals. Use public game APIs and client packets.

## Journal

Append and flush JSON Lines to `journal/events.jsonl` as events happen. Every
record needs actual UTC ISO `timestamp`, one task `run_id`, `event`, and
`character` (name or null). Never overwrite existing records.

- Append `task_started` before checks or work.
- While playing, record observed `position` immediately on entry, every five
  seconds, and after map changes. Include integer `map_id` and numeric `x/y/z`.
- Before the final response, append `task_finished`, actual `elapsed_seconds`,
  and `outcome`: `completed`, `blocked`, `failed`, or `interrupted`.
