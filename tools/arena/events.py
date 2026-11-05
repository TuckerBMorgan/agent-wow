"""Readable feedback for the few WotLK packets used by this experiment.

Layouts: AzerothCore Unit.cpp SendAttackStateUpdate/SendSpellNonMeleeDamageLog,
and Spell.cpp SendCastResult. The original payload is also retained in events.json.
"""
import struct

SPELLS = {78: "Heroic Strike", 133: "Fireball", 7266: "Duel"}
FAILURES = {12: "bad target", 23: "caster dead", 40: "interrupted", 47: "line of sight",
            51: "moving", 61: "not facing target", 63: "spell not learned", 67: "not ready",
            85: "not enough mana/rage/energy", 97: "out of range", 105: "already casting",
            109: "target dead", 112: "target already dueling"}


def packed_guid(data, offset):
    mask, guid = data[offset], 0
    offset += 1
    for byte in range(8):
        if mask & (1 << byte):
            guid |= data[offset] << (8 * byte)
            offset += 1
    return guid, offset


def spell_name(spell):
    return f"{SPELLS.get(spell, 'spell')} ({spell})"


def describe(opcode, data, own, opponent):
    def who(guid):
        return "you" if guid == own else "opponent" if guid == opponent else f"unit {guid}"

    if opcode == 0x130:  # SMSG_CAST_FAILED
        _, spell, reason = struct.unpack_from("<BIB", data)
        return f"Your {spell_name(spell)} failed: {FAILURES.get(reason, f'error {reason}')}"
    if opcode in (0x14a, 0x250):  # Melee or spell damage.
        first, offset = packed_guid(data, 4 if opcode == 0x14a else 0)
        second, offset = packed_guid(data, offset)
        attacker, target = (first, second) if opcode == 0x14a else (second, first)
        if own not in (attacker, target):
            return ""
        spell = "melee"
        if opcode == 0x250:
            spell = spell_name(struct.unpack_from("<I", data, offset)[0])
            offset += 4
        damage = struct.unpack_from("<I", data, offset)[0]
        return f"{who(attacker)} -> {who(target)}: {damage} damage ({spell})"
    if opcode == 0x2b7:
        return f"Duel countdown: {struct.unpack('<I', data)[0] / 1000:g}s"
    if opcode == 0x16b:
        winner, loser = [name.decode() for name in data[1:].split(b"\0")[:2]]
        return f"Server result: {winner} defeated {loser}"
    return ""
