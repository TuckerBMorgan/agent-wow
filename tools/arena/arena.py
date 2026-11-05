"""Duel setup and packet recording inside the existing module runtime."""
import os
import struct
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.struct_pb2 import Struct
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from api.module.v1.session_pb2 import SendPacketRequest
from api.module.v1.session_pb2_grpc import SessionStub
import arena_pb2_grpc
from events import describe, spell_name


def packed_guid(guid):
    octets = guid.to_bytes(8, "little")
    return bytes([sum(1 << i for i, b in enumerate(octets) if b)]) + bytes(b for b in octets if b)


class Arena(arena_pb2_grpc.ArenaServicer):
    def __init__(self):
        self.session = SessionStub(grpc.insecure_channel("unix:" + os.environ["AGENT_WOW_SESSION_SOCKET"]))
        self.guid = int(os.environ["AGENT_WOW_CHARACTER_GUID"])
        self.name = os.environ["AGENT_WOW_CHARACTER_NAME"]
        self.position, self.opponent = None, None
        self.arbiter = None
        self.events, self.winner, self.loser, self.error = [], "", "", ""
        self.fighter_error = ""
        self.started, self.sequence = False, 0
        self.finished = threading.Event()

    def send(self, opcode, payload):
        self.session.SendPacket(SendPacketRequest(opcode=opcode, payload=payload), timeout=3)
        if opcode == 0x12e:
            spell = struct.unpack_from("<I", payload, 1)[0]
            self.events.append(dict(time=time.time(), opcode=opcode, payload_hex=payload.hex(),
                                    direction="out", message=f"You requested {spell_name(spell)}"))

    def cast(self, spell):
        self.sequence = (self.sequence + 1) % 256
        self.send(0x12e, struct.pack("<BIBI", self.sequence, spell, 0, 2) + packed_guid(self.opponent))

    def Prepare(self, request, context):
        self.opponent = int(request["opponent"])
        _, x, y, z, _ = self.position
        side = int(request["side"])
        clock = self.session.GetClock(Empty(), timeout=3).client_time_ms
        self.send(0xee, packed_guid(self.guid) + struct.pack("<IHIffffI", 0, 0, clock,
                  x + side * 2, y, z, side * 3.14159265, 0))
        return Empty()

    def Challenge(self, request, context):
        self.cast(7266)  # Native duel request.
        return Empty()

    def Status(self, request, context):
        result = Struct()
        result.update(dict(ready=self.position is not None, started=self.started,
                           winner=self.winner, loser=self.loser, error=self.error, fighter_error=self.fighter_error))
        if "events" in request and request["events"]:
            result["events"] = list(self.events)
        return result

    def OnPacket(self, packet, context):
        op, data = packet.opcode, packet.payload
        if op == 0x16b and self.name.encode() not in data[1:].split(b"\0")[:2]:
            return Empty()  # Duel results are also broadcast to nearby spectators.
        try:
            message = describe(op, data, self.guid, self.opponent)
        except (IndexError, ValueError, struct.error) as error:
            message = f"Could not decode packet {op:#x}: {error}"
        self.events.append(dict(time=time.time(), opcode=op, payload_hex=data.hex(), message=message))
        if op == 0x236:
            self.position = struct.unpack("<Iffff", data)
        elif op == 0x167:
            challenger = struct.unpack_from("<Q", data, 8)[0]
            if challenger in (self.guid, self.opponent):
                self.arbiter = data[:8]
                if challenger == self.opponent:
                    self.send(0x16c, self.arbiter)  # Accept the opponent's duel.
        elif op == 0x2b7 and not self.started:
            self.started = True
            threading.Thread(target=self.combat, args=(struct.unpack("<I", data)[0],), daemon=True).start()
        elif op == 0x16b:
            self.winner, self.loser = [name.decode() for name in data[1:].split(b"\0")[:2]]
            self.finished.set()
        elif op == 0x16a and data == b"\0":
            self.error = "Server interrupted the duel"
        elif op == 0x130 and struct.unpack_from("<I", data, 1)[0] == 7266:
            self.error = f"Duel request rejected: {data[5]}"
        return Empty()

    def combat(self, countdown):
        try:
            if not self.finished.wait(countdown / 1000 + 0.1):
                from fighter import fight
                fight(self)
        except grpc.RpcError as error:
            self.error = f"Game connection failed: {error}"
        except Exception:
            self.fighter_error = traceback.format_exc()

    def Stop(self, request, context):
        cancel = self.arbiter and not self.finished.is_set()
        self.finished.set()
        if cancel:
            self.send(0x16d, self.arbiter)  # End failed/timed-out duels before logging out.
        return Empty()


server = grpc.server(ThreadPoolExecutor(max_workers=4))
arena_pb2_grpc.add_ArenaServicer_to_server(Arena(), server)
healthy = health.HealthServicer()
health_pb2_grpc.add_HealthServicer_to_server(healthy, server)
healthy.set("", health_pb2.HealthCheckResponse.SERVING)
path = os.environ["AGENT_WOW_MODULE_SOCKET"]
server.add_insecure_port("unix:" + path)
server.start()
os.chmod(path, 0o666)
server.wait_for_termination()
