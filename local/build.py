#!/usr/bin/env python3
"""Read pinned client data and compare supplied builds with native character dumps."""

from collections import defaultdict
import csv
import re
import struct

# The order is the stock equipment slot order, including shirt and tabard.
SLOTS = ("head neck shoulders shirt chest waist legs feet wrists hands ring1 ring2 "
         "trinket1 trinket2 back main_hand off_hand ranged tabard").split()
APPEARANCE = ("gender", "skin", "face", "hair_style", "hair_color", "facial_style")
PROFESSIONS = {164, 165, 171, 182, 186, 197, 202, 333, 393, 755, 773, 129, 185, 356}
PRIMARY = PROFESSIONS - {129, 185, 356}
INSERT = re.compile(r"INSERT INTO `(\w+)` \((.*?)\) VALUES \((.*)\);")


def supported(build):
    if len(build['specializations']) != 1 or build['active_specialization'] != 0 or build['inventory']:
        raise ValueError('Select a supplied single-specialization build with an empty backpack')
    if any(i['random_property_id'] for i in build['equipment'].values() if i):
        raise ValueError('Random-property items are not supported')


def dbc_rows(path):
    data = path.read_bytes()
    magic, count, columns, width, strings = struct.unpack_from("<4s4I", data)
    if magic != b"WDBC" or width != columns * 4 or len(data) != 20 + count * width + strings:
        raise ValueError(f"Invalid client data: {path.name}")
    return {row[0]: row for row in struct.iter_unpack(f"<{columns}I", data[20:20 + count * width])}


def tables(path):
    result = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line or line.startswith("IMPORTANT NOTE:"):
            continue
        match = INSERT.fullmatch(line)
        if not match:
            raise ValueError("Expected an AzerothCore native character dump")
        table, columns, values = match.groups()
        columns = re.findall(r"`(\w+)`", columns)
        values = next(csv.reader([values], quotechar="'", escapechar="\\", skipinitialspace=True))
        if len(columns) != len(values):
            raise ValueError(f"Invalid dump row for {table}")
        result[table].append(dict(zip(columns, values)))
    return result


def observed_build(path, name, dbc):
    saved = tables(path)
    if len(saved["characters"]) != 1:
        raise ValueError("Expected exactly one character in the dump")
    character = saved["characters"][0]
    if character["name"] != name or int(character["level"]) != 80 or int(character["online"]):
        raise ValueError("Expected an offline level-80 character with the runner-assigned name")
    if int(character["playerFlags"]) & 8 or int(character["at_login"]):
        raise ValueError("Character has GM mode or a pending login change")
    guid = character["guid"]
    equipment = dict.fromkeys(SLOTS)
    inventory = []
    instances = {row["guid"]: row for row in saved["item_instance"]}
    for location in saved["character_inventory"]:
        row = instances[location["item"]]
        if location["guid"] != guid or row["owner_guid"] != guid:
            raise ValueError("Dump contains inventory owned by another character")
        slot = int(location["slot"])
        enchants = list(map(int, row["enchantments"].split()))[::3]
        value = {"item_id": int(row["itemEntry"]), "random_property_id": int(row["randomPropertyId"]),
                 "enchant_id": enchants[0], "socket_enchant_id": enchants[6], "gem_enchant_ids": enchants[2:5]}
        if location["bag"] == "0" and slot < len(SLOTS):
            if equipment[SLOTS[slot]] is not None or int(row["count"]) != 1:
                raise ValueError("Invalid equipped item in dump")
            equipment[SLOTS[slot]] = value
        else:
            inventory.append(dict(value, count=int(row["count"])))
    if int(character['talentGroupsCount']) != 1 or int(character['activeTalentGroup']) != 0:
        raise ValueError('Expected one active specialization')
    talents = [int(r['spell']) for r in saved['character_talent'] if int(r['specMask']) & 1]
    ranks = {spell: rank for row in dbc_rows(dbc / 'Talent.dbc').values()
             for rank, spell in enumerate(row[4:9], 1) if spell}
    glyphs = [r for r in saved['character_glyphs'] if int(r['talentGroup']) == 0]
    spec = {'talent_rank_spell_ids': talents, 'unspent_points': 71 - sum(ranks[s] for s in talents),
            'glyph_ids': [int(glyphs[0][f'glyph{i}']) for i in range(1, 7)] if glyphs else [0] * 6}
    appearance = dict(zip(APPEARANCE, map(lambda key: int(character[key]),
        ("gender", "skin", "face", "hairStyle", "hairColor", "facialStyle"))))
    professions = {row["skill"]: int(row["value"]) for row in saved["character_skills"]
                   if int(row["skill"]) in PROFESSIONS}
    return {"race_id": int(character["race"]), "class_id": int(character["class"]),
            "appearance": appearance, "equipment": equipment, "inventory": inventory,
            "ammo_id": int(character["ammoId"]), "specializations": [spec],
            "active_specialization": int(character["activeTalentGroup"]), "professions": professions}


def normalize(build):
    return dict(build, specializations=[dict(build['specializations'][0],
        talent_rank_spell_ids=sorted(build['specializations'][0]['talent_rank_spell_ids']))])
