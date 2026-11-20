"""Read-only client DBC metadata; never connects to game databases."""
from pathlib import Path
import re
import struct


def dbc(path):
    data = Path(path).read_bytes()
    magic, count, fields, size, strings_size = struct.unpack_from("<4s4I", data)
    if magic != b"WDBC" or size != fields * 4 or len(data) != 20 + count * size + strings_size:
        raise ValueError(f"Invalid client DBC: {path}")
    strings = data[20 + count * size:]
    rows = {struct.unpack_from("<I", data, offset)[0]: struct.unpack_from("<" + str(fields) + "I", data, offset)
            for offset in range(20, 20 + count * size, size)}
    return rows, lambda offset: strings[offset:strings.index(0, offset)].decode("utf-8", "replace")


class Catalog:
    def __init__(self, directory):
        directory = Path(directory) / "dbc"
        self.spells, self.factions = {}, {}
        if not (directory / "Spell.dbc").is_file():
            return
        spells, text = dbc(directory / "Spell.dbc")
        ranges, _ = dbc(directory / "SpellRange.dbc")
        times, _ = dbc(directory / "SpellCastTimes.dbc")
        for spell_id, row in spells.items():
            rank_name = text(row[153])
            rank = re.search(r"\d+", rank_name)
            limits = ranges.get(row[46])
            cast_time = times.get(row[28])
            float_at = lambda value: struct.unpack("<f", struct.pack("<I", value))[0]
            self.spells[spell_id] = dict(id=spell_id, name=text(row[136]), rank_name=rank_name,
                rank=int(rank[0]) if rank else 0, cast_time_ms=cast_time[1] if cast_time else 0,
                min_range=float_at(limits[1]) if limits else None,
                max_range=float_at(limits[3]) if limits else None,
                friendly_max_range=float_at(limits[4]) if limits else None,
                effects=list(row[71:74]), triggers=list(row[116:119]), aura_types=list(row[95:98]))
        if (directory / "FactionTemplate.dbc").is_file():
            self.factions, _ = dbc(directory / "FactionTemplate.dbc")

    def reaction(self, mine, theirs):
        a, b = self.factions.get(mine), self.factions.get(theirs)
        if not a or not b:
            return None
        if b[1] and b[1] in a[6:10]: return "hostile"
        if b[1] and b[1] in a[10:14]: return "friendly"
        if a[5] & b[3]: return "hostile"
        if a[4] & b[3] or b[4] & a[3]: return "friendly"
        return "neutral"
