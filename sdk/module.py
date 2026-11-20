"""Session-owned packet observer and leased packet transport for wow_sdk."""
from concurrent.futures import ThreadPoolExecutor
import math
import os
import queue
import struct
import threading
import time
import zlib

import grpc
from google.protobuf import empty_pb2, json_format, struct_pb2
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
import module_pb2_grpc
from api.module.v1 import session_pb2, session_pb2_grpc
from wow_sdk.catalog import Catalog
from wow_sdk.navigation import Navigation
from wow_sdk.protocol import NAMES, Reader, opcode, pack, packed_guid
from wow_sdk.state import State


class Gameplay(module_pb2_grpc.GameplayServicer):
    def __init__(self):
        self.lock = threading.RLock()
        self.state = State(os.environ["AGENT_WOW_CHARACTER_GUID"], os.environ["AGENT_WOW_CHARACTER_NAME"],
                           os.environ["AGENT_WOW_SESSION_ID"], Catalog("/data"))
        self.session = session_pb2_grpc.SessionStub(grpc.insecure_channel("unix:" + os.environ["AGENT_WOW_SESSION_SOCKET"]))
        self.navigation = Navigation("/data/mmaps")
        self.owner, self.expires = None, 0
        self.closed = False
        self.queries = queue.Queue()
        threading.Thread(target=self.watchdog, daemon=True).start()
        threading.Thread(target=self.query_worker, daemon=True).start()

    def query_worker(self):
        # Metadata bursts must not block the module's five-second packet callback.
        while not self.closed:
            try:
                kind, identifier, guid = self.queries.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                self.query(kind, identifier, guid)
            except grpc.RpcError as error:
                with self.lock:
                    self.state.queried.discard((kind, identifier))
                    self.state.event("query_error", kind_name=kind, id=identifier, message=str(error))

    def send(self, name, payload=b""):
        self.session.SendPacket(session_pb2.SendPacketRequest(opcode=opcode(name), payload=payload), timeout=1)

    def stop(self):
        if self.state.position:
            self.move(self.state.position, 0, "MSG_MOVE_STOP")
        self.send("CMSG_ATTACKSTOP")
        self.send("CMSG_CANCEL_AUTO_REPEAT_SPELL")
        self.send("CMSG_CANCEL_CAST", pack("I", 0))

    def watchdog(self):
        while not self.closed:
            time.sleep(.25)
            with self.lock:
                if self.owner and time.monotonic() >= self.expires:
                    try:
                        self.stop()
                    except grpc.RpcError:
                        pass
                    finally:
                        self.owner = None

    def move(self, position, flags, name):
        if len(position) != 4 or not all(math.isfinite(v) for v in position):
            raise ValueError("Movement requires finite x, y, z and orientation")
        clock = self.session.GetClock(empty_pb2.Empty(), timeout=1).client_time_ms
        self.send(name, packed_guid(self.state.guid) + pack("IHIffffI", flags, 0, clock, *position, 0))
        self.state.position = position[:]
        if self.state.guid in self.state.objects:
            self.state.objects[self.state.guid]["position"] = position[:]
        self.state.revision += 1

    def query(self, kind, identifier, guid=0):
        operations = {
            "item": ("CMSG_ITEM_QUERY_SINGLE", pack("IQ", identifier, 0)),
            "quest": ("CMSG_QUEST_QUERY", pack("I", identifier)),
            "creature": ("CMSG_CREATURE_QUERY", pack("IQ", identifier, guid)),
            "object": ("CMSG_GAMEOBJECT_QUERY", pack("IQ", identifier, guid)),
            "completed": ("CMSG_QUERY_QUESTS_COMPLETED", b""),
            "corpse": ("MSG_CORPSE_QUERY", b""),
        }
        self.send(*operations[kind])

    def OnPacket(self, packet, context):
        with self.lock:
            try:
                self.state.packet(packet.opcode, packet.payload)
                name = NAMES.get(packet.opcode, "")
                if name == "SMSG_NEW_WORLD":
                    self.send("MSG_MOVE_WORLDPORT_ACK")
                elif name == "MSG_MOVE_TELEPORT_ACK":
                    r = Reader(packet.payload)
                    guid, count = r.guid(), r.read("I")
                    if guid == self.state.guid:
                        clock = self.session.GetClock(empty_pb2.Empty(), timeout=1).client_time_ms
                        self.send(name, packed_guid(guid) + pack("II", count, clock))
                elif name.startswith("SMSG_FORCE_") and name.endswith(("_SPEED_CHANGE", "_RATE_CHANGE")):
                    r = Reader(packet.payload)
                    guid, count = r.guid(), r.read("I")
                    if name == "SMSG_FORCE_RUN_SPEED_CHANGE": r.skip(1)
                    speed = r.read("f")
                    if not math.isfinite(speed) or speed < 0: raise ValueError("Invalid movement speed")
                    if guid == self.state.guid and self.state.position:
                        clock = self.session.GetClock(empty_pb2.Empty(), timeout=1).client_time_ms
                        body = packed_guid(guid) + pack("I", count) + pack("IHIffffIf", 0, 0, clock, *self.state.position, 0, speed)
                        self.send(name.replace("SMSG_", "CMSG_") + "_ACK", body)
                        if name == "SMSG_FORCE_RUN_SPEED_CHANGE": self.state.speed = speed
                for query in self.state.query_requests(): self.queries.put(query)
            except (ValueError, IndexError, KeyError, struct.error, zlib.error, grpc.RpcError) as error:
                self.state.event("decode_error", opcode=NAMES.get(packet.opcode), message=str(error))
        return empty_pb2.Empty()

    def Execute(self, request, context):
        try:
            with self.lock:
                value = self.execute(json_format.MessageToDict(request))
        except (ValueError, KeyError, TypeError, OSError, grpc.RpcError) as error:
            value = {"error": {"reason": "request_failed", "message": str(error)}}
        return json_format.ParseDict(value, struct_pb2.Struct())

    def execute(self, request):
        operation = request["operation"]
        if self.closed: raise ValueError("Character session has closed")
        token = request.get("token", "")
        if operation == "acquire":
            if not token: raise ValueError("Missing execution token")
            if self.owner and time.monotonic() < self.expires: raise ValueError("Another script already controls this session")
            if self.owner: self.stop()
            self.owner, self.expires = token, time.monotonic() + 5
            return {"acquired": True}
        if operation == "snapshot":
            self.session.GetClock(empty_pb2.Empty(), timeout=1)
            if token:
                if token != self.owner or time.monotonic() >= self.expires: raise ValueError("Control lease lost")
                self.expires = time.monotonic() + 5
            return self.state.snapshot()
        if operation == "query":
            before = self.state.revision
            self.query(request["kind"], int(request.get("id", 0)))
            return {"revision": before}
        if not token or token != self.owner or time.monotonic() >= self.expires:
            raise ValueError("An active control lease is required")
        if operation == "release":
            try:
                self.stop()
            finally:
                self.owner = None
            return {"released": True}
        if operation == "stop":
            self.stop()
            return {"stopped": True}
        if operation == "path":
            return {"points": self.navigation.path(self.state.map_id, self.state.position[:3], request["position"])}
        if operation == "move":
            if request.get("epoch") != self.state.epoch: raise ValueError("World changed during movement")
            self.move(request["position"], int(request["flags"]), request["opcode"])
            return {"sent": True}
        if operation == "send":
            op = opcode(int(request["opcode"]))
            payload = bytes.fromhex(request.get("payload_hex", ""))
            if len(payload) > 32768: raise ValueError("World packet is too large")
            if not NAMES[op].startswith(("CMSG_", "MSG_")): raise ValueError("Expected an outbound client opcode")
            self.send(op, payload)
            return {"sent": True}
        raise ValueError("Unknown gameplay operation")

    def BeforeLogout(self, request, context):
        with self.lock:
            self.closed = True
            if self.owner:
                try: self.stop()
                finally: self.owner = None
            self.state.closed = True
        return empty_pb2.Empty()


def main():
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    module_pb2_grpc.add_GameplayServicer_to_server(Gameplay(), server)
    status = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(status, server)
    status.set("", health_pb2.HealthCheckResponse.SERVING)
    socket = os.environ["AGENT_WOW_MODULE_SOCKET"]
    server.add_insecure_port("unix:" + socket)
    server.start()
    os.chmod(socket, 0o666)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
