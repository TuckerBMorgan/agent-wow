"""Bounded gameplay actions. Routes, rotations and quest selection belong to scripts."""
import asyncio
from functools import wraps
import math
import time

from .models import ActionResult, SDKError
from .protocol import pack, packed_guid
from .sdk import deadline


def action(function):
    @wraps(function)
    async def execute(self, *args, **kwargs):
        timeout = kwargs.get("timeout_s", function.__kwdefaults__["timeout_s"])
        started = time.monotonic()
        scope = deadline.set(min(deadline.get(), started + timeout))
        result = None
        try:
            async with asyncio.timeout(max(0, deadline.get() - started)), self._lock:
                self.sdk._check()
                if self.sdk.mode != "control":
                    raise SDKError("read_only", "Observe mode cannot perform game actions")
                self._interrupted = False
                value = await function(self, *args, **kwargs)
                result = value if isinstance(value, ActionResult) else ActionResult(True, data=value or {})
        except TimeoutError:
            result = ActionResult(False, reason="timeout", message="No confirmation before the action deadline")
        except (SDKError, ValueError, OSError) as error:
            result = ActionResult(False, reason=getattr(error, "reason", "invalid_argument"), message=str(error))
        except asyncio.CancelledError:
            result = ActionResult(False, phase="cancelled", reason="cancelled")
            raise
        finally:
            deadline.reset(scope)
            if result:
                self.sdk.logs.append(dict(action=function.__name__, duration_s=time.monotonic() - started,
                                          success=result.success, phase=result.phase, reason=result.reason,
                                          message=result.message, data=result.data))
        return result
    return execute


class Bot:
    def __init__(self, sdk):
        self.sdk, self._lock = sdk, asyncio.Lock()
        self._interrupted, self._sequence = False, 0

    def _state(self):
        if self._interrupted:
            raise SDKError("stopped", "Action stopped")
        return self.sdk.get_state()

    async def _wait(self, predicate):
        while True:
            state = self._state()
            value = predicate(state)
            if value:
                return value
            await asyncio.sleep(.05)

    def _entity(self, target, kind=None):
        state = self._state()
        if isinstance(target, dict):
            if target.get("session_id") != state.session_id or target.get("epoch") != state.epoch:
                raise SDKError("stale_target", "Select the target again in the current world session")
            entity = next((e for e in state.entities if e.guid == target.get("guid")), None)
        else:
            candidates = [e for e in state.entities if self.sdk._matches(e, target) and e.distance is not None
                          and (kind is None or e.kind == kind)]
            entity = min(candidates, key=lambda e: e.distance, default=None)
        if entity is None or (kind and entity.kind != kind) or entity.position is None:
            raise SDKError("target_missing", "Target is not visible")
        return entity

    def _item(self, query):
        state = self._state()
        if isinstance(query, dict) and query.get("session_id") not in (None, state.session_id):
            raise SDKError("stale_target", "Item belongs to a different session")
        item = next((i for i in [*state.inventory.items, *state.equipment.values()]
                     if self.sdk._matches(i, query)), None)
        if item is None or item.id is None or item.count is None:
            raise SDKError("item_missing", "Item is not present in the observed inventory")
        return item

    @staticmethod
    def _quantity(state, identifier):
        return sum(i.count or 0 for i in [*state.inventory.items, *state.equipment.values()] if i.id == identifier)

    @staticmethod
    def _event(state, cursor, kind, **fields):
        return next((e for e in state.events if e.revision > cursor and e.kind == kind
                     and all(e.get(k) == v for k, v in fields.items())), None)

    async def _send(self, name, payload=b""):
        return await self.sdk.send_packet(name, payload)

    async def _face(self, target):
        await self.sdk._refresh()
        target = self._entity(target)
        player = self._state().player
        x, y, z, orientation = player.position
        orientation = math.atan2(target.position[1] - y, target.position[0] - x)
        await self.sdk._command("move", position=[x, y, z, orientation], flags=0,
                                epoch=self._state().epoch, opcode="MSG_MOVE_SET_FACING")

    async def _walk(self, position, stop_distance):
        if isinstance(position, dict): position = [position[k] for k in ("x", "y", "z")]
        position = list(position)
        if len(position) != 3 or not all(math.isfinite(v) for v in position) or stop_distance < 0:
            raise ValueError("Provide finite x, y, z and a nonnegative stop_distance")
        state = self._state()
        if math.dist(state.player.position[:3], position) <= stop_distance:
            return dict(position=state.player.position, position_source="client")
        epoch = state.epoch
        path = (await self.sdk._command("path", position=position))["points"]
        current = list(state.player.position)
        moving = False
        try:
            for waypoint in path:
                while math.dist(current[:3], waypoint) > .05:
                    state = self._state()
                    if state.epoch != epoch: raise SDKError("world_changed", "Movement interrupted by teleport or map change")
                    if state.player.alive is False and not state.player.ghost:
                        raise SDKError("dead", "Release spirit before moving")
                    if math.dist(current[:3], position) <= stop_distance:
                        return dict(position=current, position_source="client")
                    speed = state.player.run_speed
                    if not speed or not math.isfinite(speed) or speed <= 0:
                        raise SDKError("unknown_speed", "No usable run speed has been observed")
                    distance = math.dist(current[:3], waypoint)
                    duration = min(.1, distance / speed)
                    await asyncio.sleep(duration)
                    fraction = min(1, speed * duration / distance)
                    orientation = math.atan2(waypoint[1] - current[1], waypoint[0] - current[0])
                    current = [current[i] + (waypoint[i] - current[i]) * fraction for i in range(3)] + [orientation]
                    await self.sdk._command("move", position=current, flags=1,
                                            epoch=epoch,
                                            opcode="MSG_MOVE_HEARTBEAT" if moving else "MSG_MOVE_START_FORWARD")
                    moving = True
            if math.dist(current[:3], position) > max(stop_distance, .5):
                raise SDKError("unreachable", "Navigation path did not reach the requested destination")
            return dict(position=current, position_source="client")
        finally:
            await self.sdk._stop()

    async def _approach(self, target, distance=3):
        entity = self._entity(target)
        if entity.distance > distance:
            await self._walk(entity.position[:3], max(0, distance - .5))
        entity = self._entity(entity)
        if entity.distance > distance + .5:
            raise SDKError("out_of_range", "Target moved out of interaction range")
        await self._face(entity)
        return entity

    async def _talk(self, npc):
        target = await self._approach(self._entity(npc, "npc"))
        cursor = self._state().revision
        await self._send("CMSG_GOSSIP_HELLO", pack("Q", int(target.guid)))
        return await self._wait(lambda s: s.dialog if s.dialog and s.dialog.npc_guid == target.guid
                                and s.dialog.revision > cursor else None)

    @action
    async def walk_to(self, position, *, stop_distance=2, timeout_s=60):
        return await self._walk(position, stop_distance)

    @action
    async def talk_to(self, npc, *, timeout_s=30):
        return dict(dialog=await self._talk(npc))

    @action
    async def choose_dialog(self, option, *, timeout_s=10):
        dialog = self._state().dialog
        if not dialog or dialog.type != "gossip_message": raise SDKError("no_dialog", "Open a gossip menu first")
        chosen = next((o for o in dialog.options if self.sdk._matches(o, option, key="text")), None)
        if chosen is None: raise SDKError("option_missing", "Option is not in the current menu")
        if chosen.coded: raise SDKError("unsupported", "Coded gossip options require a raw packet")
        await self._send("CMSG_GOSSIP_SELECT_OPTION", pack("QII", int(dialog.npc_guid), dialog.menu_id, chosen.id))
        await self._wait(lambda s: s.dialog is None or s.dialog.revision > dialog.revision)
        return dict(dialog=self._state().dialog)

    async def _interact_object(self, target):
        target = await self._approach(self._entity(target, "object"))
        if target.object_type is None:
            target = await self._wait(lambda s: next((e for e in s.entities if e.guid == target.guid
                                                      and e.object_type is not None), None))
        cursor = self._state().revision
        related = {int(key) for key, q in self._state().metadata.quest.items()
                   if any(t.entry_id == -target.entry_id for t in q.get("targets", []))}
        quests = [q for q in self._state().quests if q.id in related]
        if target.object_type == 3:
            await self._cast(6478, target)  # The ordinary client Opening spell for unlocked chests.
        else:
            await self._send("CMSG_GAMEOBJ_USE", pack("Q", int(target.guid)))
        def changed(state):
            current = next((e for e in state.entities if e.guid == target.guid), None)
            return current is None or (current.flags, current.object_state) != (target.flags, target.object_state) or [q for q in state.quests if q.id in related] != quests
        state = await self._wait(lambda s: s if (s.loot and s.loot.guid == target.guid and s.loot.revision > cursor)
                                or (s.dialog and s.dialog.npc_guid == target.guid and s.dialog.revision > cursor) or changed(s) else None)
        return dict(loot=state.loot, dialog=state.dialog)

    @action
    async def interact_object(self, target, *, timeout_s=30):
        return await self._interact_object(target)

    def _quest_id(self, query, dialog):
        if isinstance(query, int): return query
        if isinstance(query, dict) and query.get("id"): return int(query["id"])
        quests = [*dialog.quests, *self._state().quests, *self._state().metadata.quest.values()]
        found = next((q for q in quests if self.sdk._matches(q, query, key="title")), None)
        if found is None: raise SDKError("quest_missing", "Quest is not in the current dialog or observed quest metadata")
        return found.id

    async def _quest_dialog(self, npc, quest):
        target = self._entity(npc, "npc")
        dialog = await self._talk(target)
        identifier = self._quest_id(quest, dialog)
        return target, identifier

    @action
    async def accept_quest(self, npc, quest, *, timeout_s=30):
        target, identifier = await self._quest_dialog(npc, quest)
        if any(q.id == identifier for q in self._state().quests): return dict(quest_id=identifier, already_accepted=True)
        cursor = self._state().revision
        await self._send("CMSG_QUESTGIVER_QUERY_QUEST", pack("QIB", int(target.guid), identifier, 0))
        await self._wait(lambda s: s.dialog and s.dialog.npc_guid == target.guid and s.dialog.get("quest_id") == identifier
                         and s.dialog.type == "questgiver_quest_details" and s.dialog.revision > cursor)
        await self._send("CMSG_QUESTGIVER_ACCEPT_QUEST", pack("QII", int(target.guid), identifier, 0))
        await self._wait(lambda s: any(q.id == identifier for q in s.quests))
        return dict(quest_id=identifier)

    @action
    async def turn_in_quest(self, npc, quest, *, reward=None, timeout_s=30):
        target, identifier = await self._quest_dialog(npc, quest)
        cursor = self._state().revision
        await self._send("CMSG_QUESTGIVER_COMPLETE_QUEST", pack("QI", int(target.guid), identifier))
        dialog = await self._wait(lambda s: s.dialog if s.dialog and s.dialog.npc_guid == target.guid
                                 and s.dialog.get("quest_id") == identifier and s.dialog.revision > cursor else None)
        if dialog.type == "questgiver_request_items":
            await self._send("CMSG_QUESTGIVER_REQUEST_REWARD", pack("QI", int(target.guid), identifier))
            dialog = await self._wait(lambda s: s.dialog if s.dialog and s.dialog.npc_guid == target.guid
                                     and s.dialog.get("quest_id") == identifier and s.dialog.type == "questgiver_offer_reward"
                                     and s.dialog.revision > dialog.revision else None)
        if dialog.type != "questgiver_offer_reward": raise SDKError("quest_incomplete", "Quest giver did not offer a reward")
        if len(dialog.choices) > 1 and reward is None:
            return ActionResult(False, reason="reward_required", message="Choose a reward item", data=dict(choices=dialog.choices))
        choice = next((c for c in dialog.choices if reward is None or self.sdk._matches(c, reward)), None)
        if reward is not None and choice is None and not isinstance(reward, (int, dict)):
            for candidate in dialog.choices:
                info = await self.sdk.item_info(candidate.id)
                if self.sdk._matches(info, reward):
                    choice = candidate
                    break
        if reward is not None and choice is None: raise SDKError("reward_missing", "Requested reward is not offered")
        cursor = self._state().revision
        await self._send("CMSG_QUESTGIVER_CHOOSE_REWARD", pack("QII", int(target.guid), identifier, choice.index if choice else 0))
        await self._wait(lambda s: self._event(s, cursor, "quest_rewarded", quest_id=identifier))
        return dict(quest_id=identifier)

    @action
    async def loot(self, target, *, item_ids=None, take_money=True, timeout_s=30):
        target = await self._approach(self._entity(target))
        opened = self._state().loot
        if not opened or opened.guid != target.guid:
            cursor = self._state().revision
            if target.kind == "object": await self._interact_object(target)
            else: await self._send("CMSG_LOOT", pack("Q", int(target.guid)))
            opened = await self._wait(lambda s: s.loot if s.loot and s.loot.guid == target.guid and s.loot.revision > cursor else None)
        if opened.get("error") is not None: raise SDKError("loot_failed", str(opened.error))
        collected = []
        try:
            if take_money and opened.money:
                money = self._state().player.money
                await self._send("CMSG_LOOT_MONEY")
                await self._wait(lambda s: money is not None and s.player.money is not None and s.player.money > money)
            for item in opened.items:
                if item.status != 0 or (item_ids is not None and item.id not in item_ids): continue
                before = self._quantity(self._state(), item.id)
                await self._send("CMSG_AUTOSTORE_LOOT_ITEM", pack("B", item.slot))
                await self._wait(lambda s: self._quantity(s, item.id) >= before + item.count)
                collected.append(dict(id=item.id, count=item.count))
        finally:
            await self._send("CMSG_LOOT_RELEASE", pack("Q", int(target.guid)))
        return dict(items=collected)

    def _next_cast(self):
        self._sequence = (self._sequence + 1) % 256
        return self._sequence

    def _targets(self, target):
        if target is None: return pack("I", 0), None
        entity = self._entity(target)
        return pack("I", 0x800 if entity.kind == "object" else 2) + packed_guid(int(entity.guid)), entity

    async def _cast(self, spell, target):
        spell = self.sdk.find_spell(spell)
        if spell is None: raise SDKError("spell_missing", "Spell is not known")
        if spell.cooldown_s > 0: raise SDKError("cooldown", "Spell is on cooldown")
        targets, entity = self._targets(target)
        if entity:
            maximum = spell.friendly_max_range if entity.reaction == "friendly" else spell.max_range
            if maximum and entity.distance > maximum:
                entity = await self._approach(entity, max(1, maximum - 1))
            if spell.min_range and entity.distance < spell.min_range:
                raise SDKError("too_close", "Target is inside the spell's minimum range")
            await self._face(entity)
        sequence, cursor = self._next_cast(), self._state().revision
        await self._send("CMSG_CAST_SPELL", pack("BIB", sequence, spell.id, 0) + targets)
        event = await self._wait(lambda s: self._event(s, cursor, "cast_failed", sequence=sequence, spell_id=spell.id)
                                 or self._event(s, cursor, "cast_completed", sequence=sequence, spell_id=spell.id))
        if event.kind == "cast_failed": raise SDKError("cast_failed", f"Server rejected cast: {event.code}")
        return dict(spell_id=spell.id, target_guid=entity.guid if entity else None)

    @action
    async def cast(self, spell, target=None, *, timeout_s=30):
        return await self._cast(spell, target)

    @action
    async def attack(self, target, *, mode="melee", timeout_s=30):
        target = self._entity(target)
        if target.alive is not True: raise SDKError("target_dead", "Target is not known to be alive")
        if mode == "ranged":
            return ActionResult(True, phase="started", data=await self._cast(75, target))
        if mode != "melee": raise ValueError("mode must be melee or ranged")
        target = await self._approach(target, 3)
        cursor = self._state().revision
        await self._send("CMSG_SET_SELECTION", pack("Q", int(target.guid)))
        await self._send("CMSG_ATTACKSWING", pack("Q", int(target.guid)))
        await self._wait(lambda s: self._event(s, cursor, "attack_started", attacker=s.player.guid, target=target.guid))
        return ActionResult(True, phase="started", data=dict(target_guid=target.guid))

    @action
    async def stop_attack(self, *, timeout_s=2):
        attacking = self._state().player.attacking_guid
        cursor = self._state().revision
        await self._send("CMSG_CANCEL_AUTO_REPEAT_SPELL")
        await self._send("CMSG_ATTACKSTOP")
        if attacking:
            await self._wait(lambda s: self._event(s, cursor, "attack_stopped", attacker=s.player.guid))
        return ActionResult(True, phase="stopped" if attacking else "sent", data=dict(confirmed=bool(attacking)))

    async def stop(self, *, timeout_s=2):
        self._interrupted = True
        try:
            if self.sdk.mode != "control" or not self.sdk._claimed:
                raise SDKError("read_only", "Observe mode cannot stop a controlling script")
            async with asyncio.timeout(timeout_s): await self.sdk._stop()
            return ActionResult(True, phase="stopped")
        except (SDKError, OSError, TimeoutError) as error:
            return ActionResult(False, reason="stop_failed", message=str(error))

    @action
    async def equip(self, item, *, slot=None, timeout_s=10):
        item = self._item(item)
        if slot is None:
            await self._send("CMSG_AUTOEQUIP_ITEM", pack("BB", item.bag, item.slot))
        else:
            if not isinstance(slot, int) or not 0 <= slot < 19: raise ValueError("slot must be an equipment slot from 0 to 18")
            await self._send("CMSG_AUTOEQUIP_ITEM_SLOT", pack("QB", int(item.guid), slot))
        await self._wait(lambda s: any(i.guid == item.guid for key, i in s.equipment.items() if slot is None or int(key) == slot))
        return dict(item_guid=item.guid)

    async def _use_item(self, item, target, rest=None):
        item = self._item(item)
        info = await self.sdk.item_info(item.id)
        spell_ids = [s.id for s in info.get("spells", []) if s.trigger == 0]
        if not spell_ids: raise SDKError("not_usable", "Item has no on-use spell")
        if rest and not any(a in ({8, 20, 84} if rest == "food" else {21, 24, 85})
                            for s in info.spells for a in s.get("aura_types", [])):
            raise SDKError("wrong_item", f"Item has no observed {rest} effect")
        targets, entity = self._targets(target)
        sequence, cursor = self._next_cast(), self._state().revision
        await self._send("CMSG_USE_ITEM", pack("BBBIQIB", item.bag, item.slot, sequence, spell_ids[0], int(item.guid), 0, 0) + targets)
        event = await self._wait(lambda s: next((e for e in s.events if e.revision > cursor and e.kind in ("cast_failed", "cast_completed")
                                               and e.get("sequence") == sequence and e.get("spell_id") in spell_ids), None))
        if event.kind == "cast_failed": raise SDKError("cast_failed", f"Server rejected item use: {event.code}")
        return dict(item_guid=item.guid, spell_id=event.spell_id)

    @action
    async def use_item(self, item, target=None, *, timeout_s=30):
        return await self._use_item(item, target)

    @action
    async def eat(self, food, *, timeout_s=10):
        return ActionResult(True, phase="started", data=await self._use_item(food, None, "food"))

    @action
    async def drink(self, item, *, timeout_s=10):
        return ActionResult(True, phase="started", data=await self._use_item(item, None, "drink"))

    @action
    async def release_spirit(self, *, timeout_s=10):
        if self._state().player.alive is not False: raise SDKError("not_dead", "Character is not dead")
        await self._send("CMSG_REPOP_REQUEST", pack("B", 0))
        await self._wait(lambda s: s.player.ghost)
        await self.sdk._call("query", kind="corpse")
        return dict(ghost=True)

    @action
    async def reclaim_corpse(self, *, timeout_s=10):
        player = self._state().player
        if not player.ghost: raise SDKError("not_ghost", "Release spirit first")
        await self._send("CMSG_RECLAIM_CORPSE", pack("Q", int(player.guid)))
        await self._wait(lambda s: s.player.alive and not s.player.ghost)

    async def _service(self, npc, kind, opcode):
        target = await self._approach(self._entity(npc, "npc"))
        cursor = self._state().revision
        await self._send(opcode, pack("Q", int(target.guid)))
        return await self._wait(lambda s: s[kind] if s[kind] and s[kind].npc_guid == target.guid
                                and s[kind].revision > cursor else None)

    @action
    async def vendor_stock(self, vendor, *, timeout_s=30):
        return dict(stock=await self._service(vendor, "vendor", "CMSG_LIST_INVENTORY"))

    @action
    async def buy(self, vendor, item, quantity=1, *, timeout_s=30):
        if not isinstance(quantity, int) or quantity < 1: raise ValueError("quantity must be a positive number of vendor bundles")
        stock = await self._service(vendor, "vendor", "CMSG_LIST_INVENTORY")
        chosen = next((i for i in stock.items if self.sdk._matches(i, item)), None)
        if chosen is None and not isinstance(item, (int, dict)):
            for candidate in stock.items:
                info = await self.sdk.item_info(candidate.id)
                if self.sdk._matches(info, item):
                    chosen = candidate
                    break
        if chosen is None: raise SDKError("item_missing", "Item is not sold by this vendor")
        if chosen.extended_cost: raise SDKError("unsupported", "Extended-cost purchases require a raw packet")
        before = self._quantity(self._state(), chosen.id)
        await self._send("CMSG_BUY_ITEM", pack("QIIIB", int(stock.npc_guid), chosen.id, chosen.slot, quantity, 0))
        await self._wait(lambda s: self._quantity(s, chosen.id) >= before + quantity * chosen.bundle_size)
        return dict(item_id=chosen.id, count=quantity * chosen.bundle_size)

    @action
    async def sell(self, vendor, item, quantity=None, *, timeout_s=30):
        target = await self._approach(self._entity(vendor, "npc"))
        item = self._item(item)
        quantity = item.count if quantity is None else quantity
        if not isinstance(quantity, int) or not 1 <= quantity <= item.count: raise ValueError("quantity must fit in the selected stack")
        before = self._quantity(self._state(), item.id)
        await self._send("CMSG_SELL_ITEM", pack("QQI", int(target.guid), int(item.guid), quantity))
        await self._wait(lambda s: self._quantity(s, item.id) <= before - quantity)
        return dict(item_id=item.id, count=quantity)

    @action
    async def repair(self, vendor, item=None, *, timeout_s=30):
        target = await self._approach(self._entity(vendor, "npc"))
        inventory = [*self._state().inventory.items, *self._state().equipment.values()]
        chosen = self._item(item) if item is not None else None
        damaged = [i for i in ([chosen] if chosen else inventory) if i.durability is not None and i.max_durability and i.durability < i.max_durability]
        if not damaged: return dict(repaired=[], already_repaired=True)
        await self._send("CMSG_REPAIR_ITEM", pack("QQB", int(target.guid), int(chosen.guid) if chosen else 0, 0))
        def repaired(s):
            items = {i.guid: i for i in [*s.inventory.items, *s.equipment.values()]}
            return all(i.guid in items and items[i.guid].durability == i.max_durability for i in damaged)
        await self._wait(repaired)
        return dict(repaired=[i.guid for i in damaged])

    @action
    async def trainer_spells(self, trainer, *, timeout_s=30):
        return dict(training=await self._service(trainer, "trainer", "CMSG_TRAINER_LIST"))

    @action
    async def learn_spell(self, trainer, spell, *, timeout_s=30):
        training = await self._service(trainer, "trainer", "CMSG_TRAINER_LIST")
        chosen = next((s for s in training.spells if self.sdk._matches(s, spell)), None)
        if chosen is None or not chosen.available: raise SDKError("spell_unavailable", "Trainer does not offer this spell for learning")
        before = {s.id for s in self._state().spells}
        expected = {chosen.id, *chosen.get("triggers", [])} - {0}
        await self._send("CMSG_TRAINER_BUY_SPELL", pack("QI", int(training.npc_guid), chosen.id))
        learned = await self._wait(lambda s: ({p.id for p in s.spells} - before) & expected)
        return dict(spell_ids=sorted(learned))
