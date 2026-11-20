"""Session-local observations built exclusively from received game packets."""
from collections import deque
import math
import struct
import time
import zlib

from . import decoders
from .protocol import NAMES, Reader, fields, movement, object_movement


class State:
    def __init__(self, guid, name, session_id, catalog):
        self.guid, self.name, self.session_id, self.catalog = int(guid), name, session_id, catalog
        self.objects, self.known_spells, self.cooldowns = {}, set(), {}
        self.metadata = {kind: {} for kind in ("creature", "object", "item", "quest")}
        self.queried = set()
        self.position, self.map_id, self.speed = None, None, None
        self.spells_ready, self.closed = False, False
        self.revision, self.epoch, self.cast_count = 0, 0, 0
        self.events = deque(maxlen=256)
        self.dialog = self.vendor = self.trainer = self.loot = self.corpse = None
        self.completed, self.completed_revision = [], 0
        self.attacking_guid = None

    def event(self, kind, **data):
        self.revision += 1
        self.events.append(dict(revision=self.revision, kind=kind, at=time.time(), **data))

    def query_requests(self):
        result = []
        for guid, obj in self.objects.items():
            entry = obj["fields"].get(3)
            kind = {1: "item", 2: "item", 3: "creature", 5: "object"}.get(obj.get("type"))
            key = (kind, entry)
            if kind and entry and key not in self.queried:
                self.queried.add(key)
                result.append((kind, entry, guid))
        for quest in self.quest_log():
            key = ("quest", quest["id"])
            if key not in self.queried:
                self.queried.add(key)
                result.append(("quest", quest["id"], 0))
        for source, field in ((self.dialog, "choices"), (self.vendor, "items"), (self.loot, "items")):
            for item in (source or {}).get(field, []):
                key = ("item", item["id"])
                if item["id"] and key not in self.queried:
                    self.queried.add(key)
                    result.append(("item", item["id"], 0))
        return result

    def updates(self, data):
        r = Reader(data)
        for _ in range(r.read("I")):
            kind = r.read("B")
            if kind in (4, 5):
                for _ in range(r.read("I")):
                    self.objects.pop(r.guid(), None)
                continue
            guid = r.guid()
            obj = self.objects.setdefault(guid, dict(fields={}, position=None, type=None))
            if kind in (2, 3):
                obj["type"] = r.read("B")
                # Create-object masks omit zero-valued fields; those values are known.
                if obj["type"] in (3, 4):
                    obj["fields"].update({i: 0 for i in (18, 19, 24, 32, 55, 59, 82, 150)})
            if kind in (1, 2, 3):
                position, speed = object_movement(r)
                if position is not None: obj["position"] = position
                if guid == self.guid and speed and 0 < speed < 100: self.speed = speed
            if kind in (0, 2, 3): obj["fields"].update(fields(r))
            elif kind != 1: raise ValueError("Unknown object update type")
            obj["seen"] = time.monotonic()
            if guid == self.guid and obj["position"] is not None: self.position = obj["position"]

    def packet(self, op, data):
        name, r = NAMES.get(op, hex(op)), Reader(data)
        self.revision += 1
        if name in ("SMSG_LOGIN_VERIFY_WORLD", "SMSG_NEW_WORLD"):
            self.map_id = r.read("I")
            self.position = list(r.read("ffff"))
            self.epoch += 1
            self.objects.clear()
            self.dialog = self.vendor = self.trainer = self.loot = None
            self.attacking_guid = None
            self.event("world_changed", map_id=self.map_id)
        elif name in ("SMSG_UPDATE_OBJECT", "SMSG_COMPRESSED_UPDATE_OBJECT"):
            if name == "SMSG_COMPRESSED_UPDATE_OBJECT":
                size = r.read("I")
                if size > 8 << 20: raise ValueError("Oversized object update")
                inflater = zlib.decompressobj()
                decoded = inflater.decompress(data[4:], size + 1)
                if len(decoded) != size or not inflater.eof: raise ValueError("Invalid compressed object update")
                data = decoded
            self.updates(data)
        elif name == "SMSG_DESTROY_OBJECT":
            self.objects.pop(r.read("Q"), None)
        elif name.startswith("MSG_MOVE_") and name != "MSG_MOVE_TELEPORT_ACK":
            guid = r.guid()
            position = movement(r)
            if guid in self.objects: self.objects[guid]["position"] = position
            if guid == self.guid: self.position = position
        elif name == "MSG_MOVE_TELEPORT_ACK":
            guid, counter = r.guid(), r.read("I")
            position = movement(r)
            if guid == self.guid:
                self.position = position
                self.epoch += 1
            self.event("teleport", guid=str(guid), counter=counter)
        elif name == "SMSG_MONSTER_MOVE":
            guid = r.guid()
            r.skip(1)
            start = r.read("fff")
            r.skip(4)
            kind = r.read("B")
            if guid not in self.objects: return
            obj = self.objects[guid]
            obj["position"] = list(start) + [0]
            if kind == 1:
                obj.pop("spline", None)
                return
            r.skip({2: 12, 3: 8, 4: 4}.get(kind, 0))
            flags = r.read("I")
            if flags & 0x200000: r.skip(5)
            duration = r.read("I")
            if flags & 0x800: r.skip(8)
            count = r.read("I")
            if count:
                target = r.read("fff")
                if flags & (0x40000 | 0x2000):
                    for _ in range(count - 1): target = r.read("fff")
                obj["spline"] = (start, target, time.monotonic(), duration / 1000)
        elif name == "SMSG_INITIAL_SPELLS":
            r.skip(1)
            self.known_spells = {r.read("IH")[0] for _ in range(r.read("H"))}
            declared = r.read("H")
            count, partial = divmod(len(data) - r.pos, 16)
            if partial or count > declared: raise ValueError("Invalid initial spell cooldowns")
            for _ in range(count):
                spell, _, _, duration, category = r.read("IHHII")
                self.cooldowns[spell] = time.monotonic() + max(duration, category) / 1000
            self.spells_ready = True
        elif name == "SMSG_LEARNED_SPELL":
            spell = r.read("I")
            self.known_spells.add(spell)
            self.event("learned_spell", spell_id=spell)
        elif name == "SMSG_REMOVED_SPELL":
            self.known_spells.discard(r.read("I"))
        elif name == "SMSG_SPELL_COOLDOWN":
            owner = r.read("Q")
            r.skip(1)
            while r.pos < len(data):
                spell, duration = r.read("II")
                if owner == self.guid: self.cooldowns[spell] = time.monotonic() + duration / 1000
        elif name in ("SMSG_SPELL_START", "SMSG_SPELL_GO"):
            caster, unit, sequence, spell = r.guid(), r.guid(), r.read("B"), r.read("I")
            if self.guid in (caster, unit):
                self.event("cast_started" if name.endswith("START") else "cast_completed", spell_id=spell, sequence=sequence)
        elif name == "SMSG_CAST_FAILED":
            sequence, spell, reason = r.read("BIB")
            self.event("cast_failed", sequence=sequence, spell_id=spell, code=reason)
        elif name in ("SMSG_CREATURE_QUERY_RESPONSE", "SMSG_GAMEOBJECT_QUERY_RESPONSE"):
            identifier = r.read("I")
            if identifier & 0x80000000: return
            kind = "creature" if name == "SMSG_CREATURE_QUERY_RESPONSE" else "object"
            object_type = None
            if kind == "object":
                object_type = r.read("I")
                r.skip(4)
            self.metadata[kind][str(identifier)] = dict(id=identifier, name=r.string(), object_type=object_type)
        elif name == "SMSG_ITEM_QUERY_SINGLE_RESPONSE":
            info = decoders.item(data)
            if info.get("spells"):
                info["spells"] = [{**self.catalog.spells.get(s["id"], {}), **s} for s in info["spells"]]
            self.metadata["item"][str(info["id"])] = info
        elif name == "SMSG_QUEST_QUERY_RESPONSE":
            info = decoders.quest(data)
            self.metadata["quest"][str(info["id"])] = info
        elif name == "SMSG_GOSSIP_MESSAGE" or name in ("SMSG_QUESTGIVER_QUEST_LIST", "SMSG_QUESTGIVER_QUEST_DETAILS", "SMSG_QUESTGIVER_REQUEST_ITEMS", "SMSG_QUESTGIVER_OFFER_REWARD"):
            self.dialog = decoders.dialog(name, data)
            self.dialog["revision"] = self.revision
            for q in self.dialog["quests"]:
                self.metadata["quest"].setdefault(str(q["id"]), dict(id=q["id"], title=q["title"]))
            if self.dialog.get("quest_id"):
                identifier = self.dialog["quest_id"]
                self.metadata["quest"].setdefault(str(identifier), dict(id=identifier, title=self.dialog["title"]))
            self.event("dialog", npc_guid=self.dialog["npc_guid"], quest_id=self.dialog.get("quest_id"))
        elif name == "SMSG_GOSSIP_COMPLETE":
            self.dialog = None
        elif name == "SMSG_QUERY_QUESTS_COMPLETED_RESPONSE":
            self.completed = [r.read("I") for _ in range(r.read("I"))]
            self.completed_revision = self.revision
        elif name == "SMSG_QUESTGIVER_QUEST_COMPLETE":
            quest_id = r.read("I")
            self.event("quest_rewarded", quest_id=quest_id)
        elif name in ("SMSG_QUESTGIVER_QUEST_INVALID", "SMSG_QUESTGIVER_QUEST_FAILED"):
            self.event("quest_failed", code=r.read("I"))
        elif name == "SMSG_LIST_INVENTORY":
            self.vendor = decoders.vendor(data)
            self.vendor["revision"] = self.revision
        elif name == "SMSG_TRAINER_LIST":
            self.trainer = decoders.trainer(data)
            self.trainer["revision"] = self.revision
        elif name == "SMSG_LOOT_RESPONSE":
            self.loot = decoders.loot(data)
            self.loot["revision"] = self.revision
            self.event("loot_opened", guid=self.loot["guid"])
        elif name == "SMSG_LOOT_RELEASE_RESPONSE":
            self.loot = None
        elif name in ("SMSG_ATTACKSTART", "SMSG_ATTACKSTOP"):
            attacker = r.read("Q") if name.endswith("START") else r.guid()
            target = r.read("Q") if name.endswith("START") else r.guid()
            if attacker == self.guid: self.attacking_guid = str(target) if name.endswith("START") else None
            self.event("attack_started" if name.endswith("START") else "attack_stopped", attacker=str(attacker), target=str(target))
        elif name == "MSG_CORPSE_QUERY":
            if r.read("B"):
                map_id, x, y, z, corpse_map = r.read("IfffI")
                self.corpse = dict(map_id=map_id, position=[x, y, z])
        elif name == "SMSG_CORPSE_RECLAIM_DELAY":
            self.event("corpse_delay", delay_ms=r.read("I"))
        elif name in ("SMSG_INVENTORY_CHANGE_FAILURE", "SMSG_BUY_FAILED", "SMSG_SELL_ITEM"):
            self.event("inventory_result", opcode=name, payload_hex=data.hex())

    def entity(self, guid, obj):
        f = obj["fields"]
        kind = {3: "npc", 4: "player", 5: "object"}.get(obj.get("type"), "item")
        info = self.metadata[{"npc": "creature", "player": "creature", "object": "object", "item": "item"}[kind]].get(str(f.get(3)), {})
        position = obj["position"]
        if "spline" in obj:
            start, target, at, duration = obj["spline"]
            fraction = min(1, max(0, (time.monotonic() - at) / max(.001, duration)))
            position = [start[i] + (target[i] - start[i]) * fraction for i in range(3)] + [0]
        unit = kind in ("npc", "player")
        health, maximum = (f.get(24), f.get(32)) if unit else (None, None)
        power_type = (f[23] >> 24) & 255 if unit and 23 in f else None
        power = f.get(25 + power_type) if power_type is not None and power_type < 7 else None
        max_power = f.get(33 + power_type) if power_type is not None and power_type < 7 else None
        return dict(guid=str(guid), session_id=self.session_id, epoch=self.epoch, entry_id=f.get(3), kind=kind,
                    name=self.name if guid == self.guid else info.get("name"), position=position,
                    health=health, max_health=maximum, health_fraction=health / maximum if health is not None and maximum else None,
                    power=power, max_power=max_power, power_type=power_type, level=f.get(54),
                    alive=health > 0 if health is not None else None, in_combat=bool(f[59] & 0x80000) if 59 in f else None,
                    flags=f.get(59) if unit else f.get(9), npc_flags=f.get(82) if unit else None,
                    object_type=info.get("object_type"), object_state=f.get(19, 0) & 255 if kind == "object" else None,
                    target_guid=str(f.get(18, 0) | f.get(19, 0) << 32) if unit else None,
                    reaction=self.catalog.reaction(self.objects.get(self.guid, {}).get("fields", {}).get(55), f.get(55)),
                    distance=math.dist(self.position[:3], position[:3]) if self.position and position else None)

    def quest_log(self):
        f = self.objects.get(self.guid, {}).get("fields", {})
        quests = []
        for slot in range(25):
            base = 158 + slot * 5
            if not f.get(base): continue
            identifier, flags = f[base], f.get(base + 1)
            counts = [(f.get(base + 2 + i // 2, 0) >> (16 * (i % 2))) & 0xffff for i in range(4)]
            quests.append(dict(id=identifier, title=self.metadata["quest"].get(str(identifier), {}).get("title"),
                               objectives_complete=bool(flags & 1) if flags is not None else None, counts=counts))
        return quests

    def inventory(self):
        f = self.objects.get(self.guid, {}).get("fields", {})
        guid_at = lambda values, index: values.get(index, 0) | values.get(index + 1, 0) << 32
        positions = [(255, slot, guid_at(f, 324 + slot * 2)) for slot in range(39)]
        capacity = 16
        for bag in range(19, 23):
            fields_ = self.objects.get(guid_at(f, 324 + bag * 2), {}).get("fields", {})
            size = fields_.get(64, 0)
            capacity += size
            positions += [(bag, slot, guid_at(fields_, 66 + slot * 2)) for slot in range(size)]
        items, equipped = [], {}
        for bag, slot, guid in positions:
            if not guid: continue
            fields_ = self.objects.get(guid, {}).get("fields", {})
            info = dict(self.metadata["item"].get(str(fields_.get(3)), {}))
            info.update(guid=str(guid), id=fields_.get(3), bag=bag, slot=slot, count=fields_.get(14),
                        durability=fields_.get(60), max_durability=fields_.get(61), session_id=self.session_id)
            if bag == 255 and slot < 19: equipped[str(slot)] = info
            elif bag != 255 or slot >= 23: items.append(info)
        return dict(items=items, free_slots=capacity - len(items), capacity=capacity), equipped

    def snapshot(self):
        own = self.objects.get(self.guid)
        player = self.entity(self.guid, own) if own else None
        if player:
            f = own["fields"]
            player.update(position=self.position, ghost=bool(f[150] & 16) if 150 in f else None,
                          xp=f.get(634), next_level_xp=f.get(635), money=f.get(1170), run_speed=self.speed,
                          attacking_guid=self.attacking_guid, position_source="client")
        inventory, equipment = self.inventory()
        spells = []
        for identifier in sorted(self.known_spells):
            info = dict(self.catalog.spells.get(identifier, dict(id=identifier, name=None, rank=None, rank_name=None,
                                                                 max_range=None, min_range=None, cast_time_ms=None)))
            info["cooldown_s"] = max(0, self.cooldowns.get(identifier, 0) - time.monotonic())
            spells.append(info)
        enrich = lambda entry: {**self.metadata["item"].get(str(entry["id"]), {}), **entry}
        dialog = dict(self.dialog) if self.dialog else None
        if dialog: dialog["choices"] = [enrich(c) for c in dialog["choices"]]
        vendor = dict(self.vendor) if self.vendor else None
        if vendor: vendor["items"] = [enrich(i) for i in vendor["items"]]
        trainer = dict(self.trainer) if self.trainer else None
        if trainer: trainer["spells"] = [{**self.catalog.spells.get(s["id"], {}), **s} for s in trainer["spells"]]
        return dict(session_id=self.session_id, epoch=self.epoch, revision=self.revision, map_id=self.map_id,
                    ready=not self.closed and bool(own and self.position and self.spells_ready), player=player,
                    entities=[self.entity(g, o) for g, o in self.objects.items() if o.get("type") in (3, 4, 5)],
                    inventory=inventory, equipment=equipment, quests=self.quest_log(), spells=spells, dialog=dialog,
                    vendor=vendor, trainer=trainer, loot=self.loot, corpse=self.corpse, events=list(self.events),
                    completed_quests=self.completed, completed_revision=self.completed_revision, metadata=self.metadata)
