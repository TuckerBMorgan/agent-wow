"""Shared client-side adapter. Agent-written code lives in /harness/harness.py."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import struct
import threading
import time

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.struct_pb2 import Struct
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from api.module.v1.session_pb2 import SendPacketRequest
from api.module.v1.session_pb2_grpc import SessionStub
import connector_pb2_grpc
from runner import Runner, TICK_MS
from state import State, Reader, movement


def packed(guid):
    octets = guid.to_bytes(8, 'little')
    return bytes([sum(1 << i for i, b in enumerate(octets) if b)]) + bytes(b for b in octets if b)


class Connector(connector_pb2_grpc.DuelServicer):
    def __init__(self, session=None, guid=None, name=None, harness='/harness/harness.py'):
        self.session = session or SessionStub(grpc.insecure_channel('unix:' + os.environ['AGENT_WOW_SESSION_SOCKET']))
        self.state = State(guid if guid is not None else int(os.environ['AGENT_WOW_CHARACTER_GUID']),
                           name if name is not None else os.environ['AGENT_WOW_CHARACTER_NAME'])
        self.harness = harness
        self.lock, self.control = threading.RLock(), threading.Lock()
        self.events, self.sequence = deque(maxlen=256), 0
        self.runner, self.arbiter, self.starts_at = None, None, None
        self.match = {'phase': 'waiting', 'opponent': None, 'winner': None}
        self.error = ''

    def send(self, opcode, data):
        self.session.SendPacket(SendPacketRequest(opcode=opcode, payload=data), timeout=1)
        with self.lock:
            self.events.append({'direction': 'out', 'opcode': opcode, 'payload_hex': data.hex()})
            if 0xb5 <= opcode <= 0xc1 or opcode in (0xda, 0xee):
                reader = Reader(data)
                if reader.guid() == self.state.guid:
                    self.state.position = movement(reader)

    def cast(self, spell, target):
        self.sequence = (self.sequence + 1) % 256
        self.send(0x12e, struct.pack('<BIBI', self.sequence, spell, 0, 2) + packed(target))

    def snapshot(self):
        with self.lock:
            if self.starts_at is not None and time.monotonic() >= self.starts_at and self.match['phase'] == 'countdown':
                self.match['phase'] = 'fighting'
            result = {'self': self.state.player(self.state.guid),
                      'opponent': self.state.player(self.match['opponent']) if self.match['opponent'] else None,
                      'map_id': self.state.map_id, 'match': dict(self.match),
                      'spells': sorted(self.state.spells),
                      'cooldowns': {str(s): max(0, (end - time.monotonic()) * 1000) for s, end in self.state.cooldowns.items()},
                      'events': list(self.events)}
            self.events.clear()
            # Do not pass references to mutable packet state outside the lock.
            return json.loads(json.dumps(result))

    def execute(self, actions):
        if not isinstance(actions, list) or len(actions) > 32:
            raise ValueError('Invalid action batch')
        encoded = []
        with self.lock:
            opponent, phase = self.match['opponent'], self.match['phase']
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError('Actions must be objects')
            kind = action.get('type')
            target = action.get('target', 'opponent')
            target = self.state.guid if target == 'self' else opponent if target == 'opponent' else int(target)
            if kind in ('cast', 'attack', 'target') and (type(target) is not int or not 0 < target < 2**64):
                raise ValueError('Action needs a valid target GUID')
            if kind == 'cast':
                spell = action.get('spell_id')
                if type(spell) is not int or not 0 < spell < 2**32:
                    raise ValueError('cast needs an integer spell_id')
                if phase != 'fighting' and target != self.state.guid:
                    raise ValueError('Opponent casts must wait for the duel to start')
                encoded.append(('cast', spell, target))
            elif kind in ('attack', 'target'):
                if kind == 'attack' and phase != 'fighting':
                    raise ValueError('Attacks must wait for the duel to start')
                encoded.append((0x141 if kind == 'attack' else 0x13d, struct.pack('<Q', target)))
            elif kind == 'stop_attack':
                encoded.append((0x142, b''))
            elif kind == 'packet':
                opcode = action.get('opcode')
                data = bytes.fromhex(action.get('payload_hex', ''))
                if type(opcode) is not int or not 0 < opcode < 0x51f or len(data) > 32768:
                    raise ValueError('Invalid packet action')
                encoded.append((opcode, data))
            else:
                raise ValueError('Unknown action type: ' + str(kind))
        for action in encoded:
            if self.runner and self.runner.stopped.is_set():
                break
            if action[0] == 'cast':
                self.cast(action[1], action[2])
            else:
                self.send(*action)

    def fail(self, reason):
        with self.lock:
            self.error, self.match['phase'] = reason, 'failed'
            if self.runner:
                self.runner.stopped.set()
        self.cancel_duel()

    def cancel_duel(self):
        with self.lock:
            arbiter, self.arbiter = self.arbiter, None
        if arbiter:
            self.send(0x16d, arbiter)

    def Prepare(self, request, context):
        with self.control:
            if self.runner and not self.runner.stopped.is_set():
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'Stop the previous match before preparing again')
            if self.runner:
                self.runner.close()
            with self.lock:
                if not self.state.ready:
                    context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'Initial character state is not ready')
                opponent = int(request['opponent'])
                if not 0 < opponent < 2**64 or opponent == self.state.guid:
                    context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'Invalid opponent GUID')
                self.match = {'phase': 'preparing', 'opponent': opponent, 'winner': None}
                self.error, self.starts_at, self.arbiter = '', None, None
                self.events.clear()
                initial = self.snapshot()
            build = None
            selected = os.environ.get('SELECTED_BUILD')
            if selected:
                path = (Path('/builds') / Path(selected).relative_to('builds')).resolve()
                if not path.is_relative_to('/builds'):
                    context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'Selected build escapes library')
                build = json.loads(path.read_text())
            self.runner = Runner(self.harness, self.snapshot, self.execute, self.fail)
            try:
                self.runner.start({'build': build, 'initial': initial, 'tick_ms': TICK_MS})
            except Exception as error:
                self.fail(str(error))
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, self.error)
        return Empty()

    def Challenge(self, request, context):
        with self.control:
            if not self.runner or self.runner.stopped.is_set() or self.match['phase'] != 'preparing':
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'Prepare the harness first')
            self.cast(7266, self.match['opponent'])
        return Empty()

    def Status(self, request, context):
        with self.lock:
            result = Struct()
            result.update(json.loads(json.dumps({'ready': self.state.ready, 'prepared': self.runner is not None and not self.runner.stopped.is_set(),
                           'tick_ms': TICK_MS, 'ticks': self.runner.ticks if self.runner else 0,
                           'match': dict(self.match), 'error': self.error, 'name': self.state.name,
                           'map_id': self.state.map_id, 'self': self.state.player(self.state.guid),
                           'opponent': self.state.player(self.match['opponent']) if self.match['opponent'] else None})))
            return result

    def OnPacket(self, packet, context):
        op, data = packet.opcode, packet.payload
        reply = None
        try:
            with self.lock:
                self.state.packet(op, data)
                self.events.append({'direction': 'in', 'opcode': op, 'payload_hex': data.hex()})
                if op == 0x167 and self.match['opponent']:
                    arbiter, challenger = struct.unpack_from('<QQ', data)
                    if challenger in (self.state.guid, self.match['opponent']):
                        self.arbiter = struct.pack('<Q', arbiter)
                        if challenger == self.match['opponent']:
                            reply = (0x16c, self.arbiter)
                elif op == 0x2b7 and self.match['opponent']:
                    self.match['phase'] = 'countdown'
                    self.starts_at = time.monotonic() + struct.unpack('<I', data)[0] / 1000
                elif op == 0x16b and self.state.name.encode() in data[1:].split(b'\0')[:2]:
                    self.match.update(phase='finished', winner=data[1:].split(b'\0')[0].decode())
                    self.arbiter = None
                    if self.runner:
                        self.runner.stopped.set()
                elif op == 0x16a and data == b'\0':
                    self.match['phase'] = 'finished'
                    if self.runner:
                        self.runner.stopped.set()
                elif op == 0x130 and struct.unpack_from('<I', data, 1)[0] == 7266:
                    self.error = 'Duel request rejected: ' + str(data[5])
                    self.match['phase'] = 'failed'
                    if self.runner:
                        self.runner.stopped.set()
            if op == 0x3e:
                self.send(0xdc, b'')  # MSG_MOVE_WORLDPORT_ACK.
            if reply:
                self.send(*reply)
        except (ValueError, struct.error, IndexError) as error:
            self.fail('World packet ' + hex(op) + ' decode failed: ' + str(error))
        return Empty()

    def Stop(self, request, context):
        with self.control:
            if self.runner:
                self.runner.close()
            self.cancel_duel()
            with self.lock:
                self.match['phase'] = 'finished'
        return Empty()

    def Cancel(self, request, context):
        return self.Stop(request, context)


def main():
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    connector_pb2_grpc.add_DuelServicer_to_server(Connector(), server)
    healthy = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(healthy, server)
    healthy.set('', health_pb2.HealthCheckResponse.SERVING)
    path = os.environ['AGENT_WOW_MODULE_SOCKET']
    server.add_insecure_port('unix:' + path)
    server.start()
    os.chmod(path, 0o666)
    server.wait_for_termination()


if __name__ == '__main__':
    main()
