"""Edit only start and tick. Callbacks run in a separate process.

start(context) runs once per Prepare, after login and initial state are ready.
Return any private memory; tick(observation, memory) receives that same object.
tick runs every 100 ms and must finish within 100 ms. Return [] to do nothing.

Observations: self/opponent (client-visible health, resources, position, raw fields),
spells (known spell IDs), cooldowns (reported remaining milliseconds), match,
tick (index, actual dt_ms, elapsed_ms), and events since the previous tick.
Unknown opponent data is None. No hidden server state is exposed.

Actions:
  {"type": "cast", "spell_id": 123, "target": "self" or "opponent" or GUID string}
  {"type": "attack", "target": "opponent"}
  {"type": "stop_attack"}
  {"type": "target", "target": "opponent"}
  {"type": "packet", "opcode": 123, "payload_hex": "..."}  # advanced game protocol

Game actions go through tick's return value. [] keeps ongoing server actions;
start is for local setup. Check observation['match']['phase'] before attacking.
"""


def start(context):
    return {}


def tick(observation, memory):
    return []
