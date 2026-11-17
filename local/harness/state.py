"""Client-visible 3.3.5a player state. Unobserved values remain unknown."""
import struct
import time
import zlib


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, fmt):
        result = struct.unpack_from('<' + fmt, self.data, self.pos)
        self.pos += struct.calcsize('<' + fmt)
        return result[0] if len(result) == 1 else result

    def skip(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError('Truncated world packet')
        self.pos += count

    def guid(self):
        mask = self.read('B')
        return sum(self.read('B') << (8 * i) for i in range(8) if mask & (1 << i))


def movement(reader):
    flags, flags2, _ = reader.read('IHI')
    position = list(reader.read('ffff'))
    if flags & 0x200:
        reader.guid()
        reader.skip(21)
        if flags2 & 0x400:
            reader.skip(4)
    if flags & (0x200000 | 0x2000000) or flags2 & 0x20:
        reader.skip(4)
    reader.skip(4)
    if flags & 0x1000:
        reader.skip(16)
    if flags & 0x4000000:
        reader.skip(4)
    return position


def object_movement(reader):
    flags, position = reader.read('H'), None
    if flags & 0x20:
        start = reader.pos
        position = movement(reader)
        reader.skip(36)  # Nine native movement speeds.
        if struct.unpack_from('<I', reader.data, start)[0] & 0x8000000:
            spline = reader.read('I')
            if spline & 0x20000:
                reader.skip(4)
            elif spline & 0x10000:
                reader.skip(8)
            elif spline & 0x8000:
                reader.skip(12)
            reader.skip(28)
            reader.skip(reader.read('I') * 12)
            reader.skip(13)
    elif flags & 0x100:
        reader.guid()
        position = list(reader.read('fff'))
        reader.skip(12)
        position.append(reader.read('f'))
        reader.skip(4)
    elif flags & 0x40:
        position = list(reader.read('ffff'))
    for flag, size in [(8, 4), (16, 4)]:
        if flags & flag:
            reader.skip(size)
    if flags & 4:
        reader.guid()
    for flag, size in [(2, 4), (0x80, 8), (0x200, 8)]:
        if flags & flag:
            reader.skip(size)
    return position


def fields(reader):
    masks = [reader.read('I') for _ in range(reader.read('B'))]
    return {32 * block + bit: reader.read('I') for block, mask in enumerate(masks)
            for bit in range(32) if mask & (1 << bit)}


class State:
    def __init__(self, guid, name):
        self.guid, self.name = guid, name
        self.map_id, self.position = None, None
        self.players, self.spells, self.cooldowns = {}, set(), {}
        self.spells_ready = False

    @property
    def ready(self):
        return self.position is not None and self.guid in self.players and self.spells_ready

    def packet(self, opcode, data):
        reader = Reader(data)
        if opcode in (0x236, 0x3e):
            self.map_id = reader.read('I')
            self.position = list(reader.read('ffff'))
            if opcode == 0x3e:
                self.players.clear()
        elif opcode == 0x12a:  # Initial spells, then explicit spell/category cooldowns.
            reader.skip(1)
            self.spells = {reader.read('IH')[0] for _ in range(reader.read('H'))}
            declared = reader.read('H')
            count, partial = divmod(len(data) - reader.pos, 16)
            # AzerothCore counts hidden cooldowns but omits their records from this packet.
            if partial or count > declared:
                raise ValueError('Invalid initial cooldown records')
            for _ in range(count):
                spell, _, _, duration, category_duration = reader.read('IHHII')
                self.cooldowns[spell] = time.monotonic() + max(duration, category_duration) / 1000
            self.spells_ready = True
        elif opcode == 0x12b:
            self.spells.add(reader.read('I'))
        elif opcode == 0x134:  # SMSG_SPELL_COOLDOWN, for this character only.
            owner = reader.read('Q')
            reader.skip(1)
            while reader.pos < len(data):
                spell, duration = reader.read('II')
                if owner == self.guid:
                    self.cooldowns[spell] = time.monotonic() + duration / 1000
        elif opcode in (0xa9, 0x1f6):
            if opcode == 0x1f6:
                size = reader.read('I')
                if size > 8 << 20:
                    raise ValueError('Object update exceeds size limit')
                inflater = zlib.decompressobj()
                try:
                    decoded = inflater.decompress(data[4:], size + 1)
                except zlib.error as error:
                    raise ValueError('Invalid compressed object update') from error
                if len(decoded) != size or not inflater.eof:
                    raise ValueError('Invalid compressed object update')
                reader = Reader(decoded)
            for _ in range(reader.read('I')):
                kind = reader.read('B')
                if kind in (4, 5):
                    for _ in range(reader.read('I')):
                        guid = reader.guid()
                        if kind == 4:
                            self.players.pop(guid, None)
                    continue
                guid = reader.guid()
                position, values = None, {}
                if kind == 0:
                    values = fields(reader)
                elif kind == 1:
                    position = object_movement(reader)
                elif kind in (2, 3):
                    object_type = reader.read('B')
                    position = object_movement(reader)
                    values = fields(reader)
                    if object_type == 4:
                        self.players[guid] = {'fields': {}, 'position': None, 'updated_at': time.monotonic()}
                else:
                    raise ValueError('Unknown object update type')
                if guid in self.players:
                    player = self.players[guid]
                    player['fields'].update(values)
                    if position is not None:
                        player['position'] = position
                    player['updated_at'] = time.monotonic()
                    if guid == self.guid and position is not None:
                        self.position = position
        elif opcode == 0xaa:
            self.players.pop(reader.read('Q'), None)
        elif 0xb5 <= opcode <= 0xc1 or opcode in (0xda, 0xee):
            guid = reader.guid()
            position = movement(reader)
            if guid in self.players:
                self.players[guid].update(position=position, updated_at=time.monotonic())
                if guid == self.guid:
                    self.position = position

    def player(self, guid):
        player = self.players.get(guid)
        values = player['fields'] if player else {}
        power_type = (values.get(23, 0) >> 24) & 0xff if player else None
        return {'guid': str(guid), 'known': player is not None,
                'position': self.position if guid == self.guid else player['position'] if player else None,
                'health': values.get(24, 0) if player else None,
                'max_health': values.get(32, 0) if player else None,
                'power_type': power_type,
                'power': values.get(25 + power_type, 0) if player and power_type < 7 else None,
                'max_power': values.get(33 + power_type, 0) if player and power_type < 7 else None,
                'flags': values.get(59, 0) if player else None,
                'age_ms': (time.monotonic() - player['updated_at']) * 1000 if player else None,
                'fields': values}
