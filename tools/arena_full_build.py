#!/usr/bin/env python3
"""Check a level-80 build submission and compare it with a trusted native pdump."""

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import re
import struct

# The order is the stock equipment slot order, including shirt and tabard.
SLOTS = ("head neck shoulders shirt chest waist legs feet wrists hands ring1 ring2 "
         "trinket1 trinket2 back main_hand off_hand ranged tabard").split()
APPEARANCE = ("gender", "skin", "face", "hair_style", "hair_color", "facial_style")
PROFESSIONS = {164, 165, 171, 182, 186, 197, 202, 333, 393, 755, 773, 129, 185, 356}
PRIMARY = PROFESSIONS - {129, 185, 356}
# Stock pairs from the client creation catalog.
RACE_CLASSES = {
    1: [1, 2, 4, 5, 6, 8, 9],
    2: [1, 3, 4, 6, 7, 9],
    3: [1, 2, 3, 4, 5, 6],
    4: [1, 3, 4, 5, 6, 11],
    5: [1, 4, 5, 6, 8, 9],
    6: [1, 3, 6, 7, 11],
    7: [1, 4, 6, 8, 9],
    8: [1, 3, 4, 5, 6, 7, 8],
    10: [2, 3, 4, 5, 6, 8, 9],
    11: [1, 2, 3, 5, 6, 7, 8],
}
ITEM_FIELDS = {"item_id", "random_property_id", "enchant_id", "socket_enchant_id", "gem_enchant_ids"}
INSERT = re.compile(r"INSERT INTO `(\w+)` \((.*?)\) VALUES \((.*)\);")


def fields(value, keys, where):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{where} requires exactly: {', '.join(sorted(keys))}")


def integer(value, where, minimum=0, maximum=2**31 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{where} must be an integer from {minimum} to {maximum}")


def item(value, where, inventory=False):
    fields(value, ITEM_FIELDS | ({"count"} if inventory else set()), where)
    integer(value["item_id"], f"{where}.item_id", 1)
    integer(value["random_property_id"], f"{where}.random_property_id", -(2**31))
    for key in ("enchant_id", "socket_enchant_id"):
        integer(value[key], f"{where}.{key}")
    gems = value["gem_enchant_ids"]
    if not isinstance(gems, list) or len(gems) != 3:
        raise ValueError(f"{where}.gem_enchant_ids needs three entries")
    for gem in gems:
        integer(gem, f"{where}.gem_enchant_ids")
    if inventory:
        integer(value["count"], f"{where}.count", 1)


def id_list(value, where, length=None):
    if not isinstance(value, list) or (length is not None and len(value) != length):
        raise ValueError(f"{where} needs a list" + (f" of {length} entries" if length is not None else ""))
    for entry in value:
        integer(entry, where, int(length is None))


def read_build(path):
    build = json.loads(path.read_text())
    fields(build, {"race_id", "class_id", "appearance", "equipment", "inventory",
                   "ammo_id", "specializations", "active_specialization", "professions"}, "build")
    for key in ("race_id", "class_id"):
        integer(build[key], key, 1)
    if build["class_id"] not in RACE_CLASSES.get(build["race_id"], []):
        raise ValueError("That race/class combination cannot be created on the stock realm")
    fields(build["appearance"], APPEARANCE, "appearance")
    for key, value in build["appearance"].items():
        integer(value, f"appearance.{key}", maximum=1 if key == "gender" else 255)
    fields(build["equipment"], SLOTS, "equipment")
    for slot, value in build["equipment"].items():
        if value is not None:
            item(value, f"equipment.{slot}")
    if not isinstance(build["inventory"], list):
        raise ValueError("inventory must be a list")
    for index, value in enumerate(build["inventory"]):
        item(value, f"inventory[{index}]", inventory=True)
    integer(build["ammo_id"], "ammo_id")
    specs = build["specializations"]
    if not isinstance(specs, list) or len(specs) not in (1, 2):
        raise ValueError("Declare one or two specializations")
    integer(build["active_specialization"], "active_specialization", maximum=len(specs) - 1)
    for spec in specs:
        fields(spec, {"talent_rank_spell_ids", "glyph_ids", "unspent_points"}, "specialization")
        id_list(spec["talent_rank_spell_ids"], "talent_rank_spell_ids")
        if len(set(spec["talent_rank_spell_ids"])) != len(spec["talent_rank_spell_ids"]):
            raise ValueError("Each talent rank spell may appear only once per specialization")
        id_list(spec["glyph_ids"], "glyph_ids", length=6)
        integer(spec["unspent_points"], "unspent_points", maximum=71)
    professions = build["professions"]
    if not isinstance(professions, dict) or any(str(skill) not in {str(p) for p in PROFESSIONS} for skill in professions):
        raise ValueError("professions must map stock profession skill IDs to ranks")
    if sum(str(skill) in professions for skill in PRIMARY) > 2:
        raise ValueError("A character can learn at most two primary professions")
    for skill, rank in professions.items():
        integer(rank, f"professions.{skill}", 1, 450)
    return build


def dbc_rows(path):
    data = path.read_bytes()
    magic, count, columns, width, strings = struct.unpack_from("<4s4I", data)
    if magic != b"WDBC" or width != columns * 4 or len(data) != 20 + count * width + strings:
        raise ValueError(f"Invalid client data: {path.name}")
    return {row[0]: row for row in struct.iter_unpack(f"<{columns}I", data[20:20 + count * width])}


def check_specializations(build, directory, from_dump=False):
    talents = dbc_rows(directory / "Talent.dbc")
    tabs = dbc_rows(directory / "TalentTab.dbc")
    glyphs = dbc_rows(directory / "GlyphProperties.dbc")
    slots = dbc_rows(directory / "GlyphSlot.dbc")
    slot_types = {row[2] - 1: row[1] for row in slots.values() if 1 <= row[2] <= 6}
    class_mask = 1 << (build["class_id"] - 1)
    ranks = {spell: (row, rank) for row in talents.values() if tabs[row[1]][20] & class_mask
             for rank, spell in enumerate(row[4:9], 1) if spell}
    for index, spec in enumerate(build["specializations"]):
        selected = {}
        for spell in spec["talent_rank_spell_ids"]:
            if spell not in ranks:
                raise ValueError(f"Spec {index}: {spell} is not a talent rank for this class")
            row, rank = ranks[spell]
            if row[0] in selected:
                raise ValueError(f"Spec {index}: multiple ranks for talent {row[0]}")
            selected[row[0]] = rank
        remaining = 71 - sum(selected.values())
        if from_dump:
            spec["unspent_points"] = remaining
        if remaining < 0 or spec["unspent_points"] != remaining:
            raise ValueError(f"Spec {index}: spent and unspent points must total 71")
        for talent_id in selected:
            row = talents[talent_id]
            earlier = sum(rank for other, rank in selected.items()
                          if talents[other][1] == row[1] and talents[other][2] < row[2])
            if earlier < row[2] * 5 or (row[13] and selected.get(row[13], 0) <= row[16]):
                raise ValueError(f"Spec {index}: talent {talent_id} is missing prerequisites")
        filled = [glyph for glyph in spec["glyph_ids"] if glyph]
        if len(filled) != len(set(filled)):
            raise ValueError(f"Spec {index}: duplicate glyphs")
        for slot, glyph in enumerate(spec["glyph_ids"]):
            if glyph and (glyph not in glyphs or glyphs[glyph][2] != slot_types[slot]):
                raise ValueError(f"Spec {index}: glyph {glyph} does not fit slot {slot}")



def observed_build(path, name):
    tables = defaultdict(list)
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
        tables[table].append(dict(zip(columns, values)))
    if len(tables["characters"]) != 1:
        raise ValueError("Expected exactly one character in the dump")
    character = tables["characters"][0]
    if character["name"] != name or int(character["level"]) != 80 or int(character["online"]):
        raise ValueError("Expected an offline level-80 character with the runner-assigned name")
    if int(character["playerFlags"]) & 8 or int(character["at_login"]):
        raise ValueError("Character has GM mode or a pending login change")
    guid = character["guid"]
    equipment = dict.fromkeys(SLOTS)
    inventory = []
    instances = {row["guid"]: row for row in tables["item_instance"]}
    for location in tables["character_inventory"]:
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
    specs = []
    for index in range(int(character["talentGroupsCount"])):
        glyphs = [row for row in tables["character_glyphs"] if int(row["talentGroup"]) == index]
        specs.append({
            "talent_rank_spell_ids": [int(row["spell"]) for row in tables["character_talent"]
                                      if int(row["specMask"]) & (1 << index)],
            "glyph_ids": [int(glyphs[0][f"glyph{i}"]) for i in range(1, 7)] if glyphs else [0] * 6,
        })
    appearance = dict(zip(APPEARANCE, map(lambda key: int(character[key]),
        ("gender", "skin", "face", "hairStyle", "hairColor", "facialStyle"))))
    professions = {row["skill"]: int(row["value"]) for row in tables["character_skills"]
                   if int(row["skill"]) in PROFESSIONS}
    return {"race_id": int(character["race"]), "class_id": int(character["class"]),
            "appearance": appearance, "equipment": equipment, "inventory": inventory,
            "ammo_id": int(character["ammoId"]), "specializations": specs,
            "active_specialization": int(character["activeTalentGroup"]), "professions": professions}


def normalize(build):
    # Backpack position and stack splits are irrelevant. Every item and count
    # still participates in the comparison.
    counts = Counter()
    for entry in build["inventory"]:
        identity = {key: entry[key] for key in ITEM_FIELDS}
        counts[json.dumps(identity, sort_keys=True)] += entry["count"]
    return dict(build, inventory=[dict(json.loads(key), count=count) for key, count in sorted(counts.items())],
                specializations=[dict(spec, talent_rank_spell_ids=sorted(spec["talent_rank_spell_ids"]))
                                 for spec in build["specializations"]])


def differences(expected, actual, path="build"):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() | actual.keys()):
            yield from differences(expected.get(key), actual.get(key), f"{path}.{key}")
    elif isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        for index, (want, got) in enumerate(zip(expected, actual)):
            yield from differences(want, got, f"{path}[{index}]")
    elif expected != actual:
        yield {"field": path, "expected": expected, "actual": actual}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    example = commands.add_parser("from-dump", help="Print a complete submission from an existing character")
    example.add_argument("dump", type=Path)
    example.add_argument("--name", required=True)
    commands.add_parser("check", help="Check the submission format").add_argument("build", type=Path)
    verify = commands.add_parser("verify", help="Compare a submission with a trusted server export")
    verify.add_argument("build", type=Path)
    verify.add_argument("dump", type=Path)
    verify.add_argument("--name", required=True)
    for command in (example, verify, commands.choices["check"]):
        command.add_argument("--dbc", type=Path, required=True, help="Pinned 3.3.5a client data directory")
    args = parser.parse_args()
    try:
        if args.command == "from-dump":
            actual = observed_build(args.dump, args.name)
            check_specializations(actual, args.dbc, from_dump=True)
            print(json.dumps(normalize(actual), indent=2))
            return 0
        build = read_build(args.build)
        check_specializations(build, args.dbc)
        if args.command == "check":
            print("Build declaration is complete.")
            return 0
        actual = observed_build(args.dump, args.name)
        check_specializations(actual, args.dbc, from_dump=True)
        errors = list(differences(normalize(build), normalize(actual)))
        print(json.dumps({"matches": not errors, "differences": errors}, indent=2))
        return int(bool(errors))
    except (OSError, ValueError, KeyError, IndexError, struct.error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
