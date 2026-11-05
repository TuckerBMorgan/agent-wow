"""Edit this harness. arena exposes send(opcode, bytes), cast(spell), and events.

events contains world packets (opcode and payload_hex) and readable messages.
Outgoing cast requests have direction="out". The existing agent-wow client owns
the connection; send/cast use its module callbacks.
"""
import json
import struct


def fight(arena):
    build = json.load(open("/build.json"))
    arena.send(0x141, struct.pack("<Q", arena.opponent))  # CMSG_ATTACKSWING
    spell = 78 if build["class"] == "warrior" else 133
    while not arena.finished.is_set():
        arena.cast(spell)
        arena.finished.wait(3)
