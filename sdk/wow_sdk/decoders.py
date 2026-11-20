"""Decode public query and interaction responses for client build 12340."""
from .protocol import Reader


def item(data):
    r = Reader(data)
    identifier = r.read("I")
    if identifier & 0x80000000:
        return dict(id=identifier & 0x7fffffff, exists=False)
    cls, subclass, _ = r.read("IIi")
    name = r.string()
    for _ in range(3): r.string()
    display, quality, flags, flags2, buy, sell, slot = r.read("7I")
    classes, races, level, required_level = r.read("iiII")
    r.skip(28)
    max_count, stack_size, bag_slots = r.read("iiI")
    stats = [dict(type=r.read("I"), value=r.read("i")) for _ in range(r.read("I"))]
    r.skip(8)
    damage = [dict(min=r.read("f"), max=r.read("f"), school=r.read("I")) for _ in range(2)]
    armor = r.read("I")
    r.skip(24)
    delay, ammo, _ = r.read("IIf")
    spells = []
    for _ in range(5):
        spell, trigger, charges, cooldown, category, category_cooldown = r.read("iIiiIi")
        if spell > 0:
            spells.append(dict(id=spell, trigger=trigger, charges=charges, cooldown_ms=cooldown))
    return dict(id=identifier, exists=True, name=name, item_class=cls, subclass=subclass,
                quality=quality, inventory_type=slot, allowed_classes=classes, allowed_races=races,
                level=level, required_level=required_level, buy_price=buy, sell_price=sell,
                stack_size=stack_size, bag_slots=bag_slots, stats=stats, damage=damage,
                armor=armor, delay_ms=delay, ammo_type=ammo, spells=spells)


def quest(data):
    r = Reader(data)
    identifier = r.read("I")
    if identifier & 0x80000000:
        return dict(id=identifier & 0x7fffffff, exists=False)
    method, level, minimum, zone, kind = r.read("IiIiI")
    r.skip(20)
    next_quest, xp, money = r.read("IIi")
    r.skip(20)
    source_item, flags = r.read("II")
    r.skip(20)
    rewards = [dict(id=r.read("I"), count=r.read("I")) for _ in range(4)]
    choices = [dict(id=r.read("I"), count=r.read("I")) for _ in range(6)]
    r.skip(60)
    poi_map, poi_x, poi_y, _ = r.read("IffI")
    title, objectives, details, area, completed_text = [r.string() for _ in range(5)]
    targets = []
    for _ in range(4):
        entry, count, drop_item, drop_count = r.read("IIII")
        if entry & 0x80000000: entry = -(entry & 0x7fffffff)
        targets.append(dict(entry_id=entry, count=count, drop_item=drop_item, drop_count=drop_count))
    items = [dict(id=r.read("I"), count=r.read("I")) for _ in range(6)]
    objective_text = [r.string() for _ in range(4)]
    return dict(id=identifier, exists=True, title=title, level=level, minimum_level=minimum,
                objectives=objectives, details=details, flags=flags, next_quest=next_quest,
                targets=targets, required_items=[i for i in items if i["id"]],
                rewards=[i for i in rewards if i["id"]], choices=[i for i in choices if i["id"]],
                objective_text=objective_text)


def dialog(name, data):
    r = Reader(data)
    guid = str(r.read("Q"))
    result = dict(npc_guid=guid, type=name.removeprefix("SMSG_").lower(), options=[], quests=[], choices=[])
    if name == "SMSG_GOSSIP_MESSAGE":
        result["menu_id"], result["text_id"] = r.read("II")
        for _ in range(r.read("I")):
            option_id, icon, coded, money = r.read("IBBI")
            result["options"].append(dict(id=option_id, icon=icon, coded=bool(coded), cost=money,
                                          text=r.string(), prompt=r.string()))
        count = r.read("I")
    elif name == "SMSG_QUESTGIVER_QUEST_LIST":
        result["text"] = r.string()
        r.skip(8)
        count = r.read("B")
    else:
        if name == "SMSG_QUESTGIVER_QUEST_DETAILS": r.skip(8)
        result["quest_id"] = r.read("I")
        result["title"], result["text"] = r.string(), r.string()
        if name == "SMSG_QUESTGIVER_OFFER_REWARD":
            r.skip(9)
            r.skip(r.read("I") * 8)
            for index in range(r.read("I")):
                item_id, count, display = r.read("III")
                result["choices"].append(dict(index=index, id=item_id, count=count))
        return result
    for _ in range(count):
        quest_id, icon, level, flags, repeatable = r.read("IIiIB")
        result["quests"].append(dict(id=quest_id, icon=icon, level=level, title=r.string()))
    return result


def vendor(data):
    r = Reader(data)
    guid, count = r.read("QB")
    items = []
    for _ in range(count):
        slot, item_id, display, available, price, durability, bundle, extended = r.read("IIIiIIII")
        items.append(dict(slot=slot, id=item_id, available=available, price=price,
                          bundle_size=bundle, extended_cost=extended))
    return dict(npc_guid=str(guid), items=items)


def trainer(data):
    r = Reader(data)
    guid, kind, count = r.read("QII")
    spells = []
    for _ in range(count):
        spell_id, state, cost, _, _, level, skill, rank, a, b, c = r.read("IBIIIB5I")
        spells.append(dict(id=spell_id, available=state == 0, state=state, cost=cost,
                           required_level=level, skill=skill, skill_rank=rank))
    return dict(npc_guid=str(guid), spells=spells)


def loot(data):
    r = Reader(data)
    guid, kind = r.read("QB")
    if not kind:
        return dict(guid=str(guid), money=0, items=[], error=r.read("B"))
    money, count = r.read("IB")
    items = []
    for _ in range(count):
        slot, item_id, quantity, display, suffix, prop, status = r.read("B5IB")
        items.append(dict(slot=slot, id=item_id, count=quantity, status=status))
    return dict(guid=str(guid), money=money, items=items)
