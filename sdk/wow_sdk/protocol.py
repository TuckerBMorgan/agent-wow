"""Client-visible 3.3.5a wire values; no server/database access."""
import json
from pathlib import Path
import struct

OPCODES = json.loads(Path(__file__).with_name("opcodes.json").read_text())
NAMES = {value: key for key, value in OPCODES.items()}


def opcode(value):
    value = OPCODES[value] if isinstance(value, str) else value
    if type(value) is not int or not 0 < value < 0x520:
        raise ValueError("Invalid world opcode")
    return value


def pack(fmt, *values):
    return struct.pack("<" + fmt, *values)


def packed_guid(guid):
    data = int(guid).to_bytes(8, "little")
    return bytes([sum(1 << i for i, byte in enumerate(data) if byte)]) + bytes(b for b in data if b)


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, fmt):
        result = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += struct.calcsize("<" + fmt)
        return result[0] if len(result) == 1 else result

    def skip(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("Truncated world packet")
        self.pos += size

    def string(self):
        end = self.data.index(0, self.pos)
        result = self.data[self.pos:end].decode("utf-8", "replace")
        self.pos = end + 1
        return result

    def guid(self):
        mask = self.read("B")
        return sum(self.read("B") << (8 * i) for i in range(8) if mask & (1 << i))


def movement(reader):
    flags, extra, _ = reader.read("IHI")
    position = list(reader.read("ffff"))
    if flags & 0x200:
        reader.guid()
        reader.skip(21)
        if extra & 0x400:
            reader.skip(4)
    if flags & 0x2200000 or extra & 0x20:
        reader.skip(4)
    reader.skip(4)
    if flags & 0x1000:
        reader.skip(16)
    if flags & 0x4000000:
        reader.skip(4)
    return position


def object_movement(reader):
    flags, position, speed = reader.read("H"), None, None
    if flags & 0x20:
        start = reader.pos
        position = movement(reader)
        speeds = reader.read("9f")
        speed = speeds[1]
        if struct.unpack_from("<I", reader.data, start)[0] & 0x8000000:
            spline = reader.read("I")
            if spline & 0x20000:
                reader.skip(4)
            elif spline & 0x10000:
                reader.skip(8)
            elif spline & 0x8000:
                reader.skip(12)
            reader.skip(28)
            reader.skip(reader.read("I") * 12 + 13)
    elif flags & 0x100:
        reader.guid()
        position = list(reader.read("fff"))
        reader.skip(12)
        position.append(reader.read("f"))
        reader.skip(4)
    elif flags & 0x40:
        position = list(reader.read("ffff"))
    for flag, size in ((8, 4), (16, 4)):
        if flags & flag:
            reader.skip(size)
    if flags & 4:
        reader.guid()
    for flag, size in ((2, 4), (0x80, 8), (0x200, 8)):
        if flags & flag:
            reader.skip(size)
    return position, speed


def fields(reader):
    masks = [reader.read("I") for _ in range(reader.read("B"))]
    return {block * 32 + bit: reader.read("I") for block, mask in enumerate(masks)
            for bit in range(32) if mask & (1 << bit)}
