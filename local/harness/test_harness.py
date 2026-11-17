import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.struct_pb2 import Struct
from google.protobuf.json_format import MessageToDict
from api.module.v1.session_pb2 import ClockResponse, WorldPacket
from api.module.v1 import session_pb2_grpc
import connector_pb2_grpc
from connector import Connector, packed
from runner import Runner, TICK_MS
from state import State


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for condition')
        time.sleep(0.01)


def player_block(guid, hp):
    values = {23: 1 << 24, 24: hp, 25: 20, 32: 100, 33: 100, 59: 0}
    masks = [sum(1 << (i % 32) for i in values if i // 32 == block) for block in range(2)]
    fields = bytes([2]) + struct.pack('<II', *masks) + b''.join(struct.pack('<I', v) for _, v in sorted(values.items()))
    return bytes([3]) + packed(guid) + bytes([4]) + struct.pack('<Hffff', 0x40, 1, 2, 3, 0) + fields


class FakeSession(session_pb2_grpc.SessionServicer):
    def __init__(self):
        self.sent = []

    def SendPacket(self, request, context):
        self.sent.append((time.monotonic(), request.opcode, bytes(request.payload)))
        return Empty()

    def GetClock(self, request, context):
        return ClockResponse(client_time_ms=int(time.monotonic() * 1000) & 0xffffffff)


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'harness.py'
        self.path.write_text('def start(context): return {}\ndef tick(observation, memory): return []\n')
        self.fake = FakeSession()
        self.sessions = grpc.server(ThreadPoolExecutor(max_workers=4))
        session_pb2_grpc.add_SessionServicer_to_server(self.fake, self.sessions)
        self.sessions.add_insecure_port('unix:' + str(self.root / 'session.sock'))
        self.sessions.start()
        self.session_channel = grpc.insecure_channel('unix:' + str(self.root / 'session.sock'))
        self.connector = Connector(session_pb2_grpc.SessionStub(self.session_channel), 42, 'Tester', self.path)
        self.server = grpc.server(ThreadPoolExecutor(max_workers=4))
        connector_pb2_grpc.add_DuelServicer_to_server(self.connector, self.server)
        self.server.add_insecure_port('unix:' + str(self.root / 'module.sock'))
        self.server.start()
        self.channel = grpc.insecure_channel('unix:' + str(self.root / 'module.sock'))
        self.stub = connector_pb2_grpc.DuelStub(self.channel)

    def tearDown(self):
        self.connector.Stop(Empty(), None)
        self.server.stop(0).wait()
        self.sessions.stop(0).wait()
        self.channel.close()
        self.session_channel.close()
        self.temp.cleanup()

    def packet(self, opcode, data):
        self.stub.OnPacket(WorldPacket(opcode=opcode, payload=data), timeout=2)

    def login(self):
        self.packet(0x236, struct.pack('<Iffff', 0, 1, 2, 3, 0))
        self.packet(0xa9, struct.pack('<I', 2) + player_block(42, 80) + player_block(77, 60))
        self.packet(0x12a, struct.pack('<BHIHH', 0, 1, 133, 0, 0))

    def prepare(self):
        request = Struct()
        request.update({'opponent': '77'})
        self.stub.Prepare(request, timeout=6)

    def test_callbacks_start_once_and_tick_at_100ms_with_persistent_memory(self):
        trace = self.root / 'trace.jsonl'
        self.path.write_text('''import json,time
def start(context):
    with open(%r,'a') as f:f.write(json.dumps({'start':True,'health':context['initial']['self']['health']})+'\\n')
    return {'n':0}
def tick(observation,memory):
    memory['n']+=1
    with open(%r,'a') as f:f.write(json.dumps({'n':memory['n'],'index':observation['tick']['index'],'time':time.monotonic()})+'\\n')
    return [{'type':'target','target':'opponent'}]
''' % (str(trace), str(trace)))
        self.login()
        self.assertFalse(trace.exists())  # Import/setup waits for match preparation.
        self.prepare()
        until(lambda: len(self.fake.sent) >= 4)
        self.stub.Stop(Empty(), timeout=2)
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual([r for r in records if r.get('start')], [{'start': True, 'health': 80}])
        ticks = records[1:]
        self.assertEqual([r['n'] for r in ticks], list(range(1, len(ticks) + 1)))
        self.assertEqual([r['index'] for r in ticks], list(range(len(ticks))))
        self.assertEqual(TICK_MS, 100)
        self.assertGreaterEqual(ticks[3]['time'] - ticks[0]['time'], 0.25)
        self.assertLess(ticks[3]['time'] - ticks[0]['time'], 0.7)
        self.assertTrue(all(op == 0x13d and data == struct.pack('<Q', 77) for _, op, data in self.fake.sent))
        count = len(self.fake.sent)
        time.sleep(0.15)
        self.assertEqual(len(self.fake.sent), count)

    def test_cast_action_and_duel_completion(self):
        self.path.write_text('''def start(context): return {'cast':False}
def tick(observation,memory):
    if observation['match']['phase']=='fighting' and not memory['cast']:
        memory['cast']=True
        return [{'type':'cast','spell_id':133,'target':'opponent'}]
    return []
''')
        self.login()
        self.prepare()
        self.assertFalse(self.fake.sent)
        self.packet(0x2b7, struct.pack('<I', 0))
        until(lambda: any(op == 0x12e for _, op, _ in self.fake.sent))
        data = next(data for _, op, data in self.fake.sent if op == 0x12e)
        self.assertEqual(struct.unpack_from('<I', data, 1)[0], 133)
        self.assertEqual(data[10:], packed(77))
        self.packet(0x16b, b'\0Tester\0Opponent\0')
        until(lambda: not self.connector.runner.process.is_alive())
        self.assertEqual(self.connector.match['phase'], 'finished')

    def test_hung_callback_fails_without_blocking_packet_delivery(self):
        self.path.write_text('import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ndef start(context): return {}\ndef tick(observation,memory):\n while True: pass\n')
        self.login()
        self.prepare()
        until(lambda: self.connector.error)
        started = time.monotonic()
        self.packet(0x130, struct.pack('<BIB', 1, 133, 97))
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertIn('time limit', self.connector.error)
        until(lambda: not self.connector.runner.process.is_alive())

    def test_start_exception_and_missing_initial_state_are_reported(self):
        with self.assertRaises(grpc.RpcError):
            self.prepare()
        self.path.write_text('def start(context): raise ValueError("start broke")\ndef tick(o,m): return []\n')
        self.login()
        with self.assertRaises(grpc.RpcError):
            self.prepare()
        self.assertIn('start broke', self.connector.error)

    def test_invalid_action_batch_has_no_partial_effect(self):
        self.connector.match.update(phase='fighting', opponent=77)
        with self.assertRaises(ValueError):
            self.connector.execute([{'type': 'target', 'target': 'opponent'}, {'type': 'invalid'}])
        self.assertFalse(self.fake.sent)

    def test_status_exposes_character_and_position_through_real_rpc(self):
        self.login()
        status = MessageToDict(self.stub.Status(Struct(), timeout=2))
        self.assertEqual(status['name'], 'Tester')
        self.assertEqual(status['map_id'], 0)
        self.assertEqual(status['self']['guid'], '42')
        self.assertEqual(status['self']['fields']['24'], 80)
        self.assertEqual(status['self']['position'], [1, 2, 3, 0])

    def test_rejected_duel_stops_the_harness_and_reports_server_failure(self):
        self.login()
        self.prepare()
        self.packet(0x130, struct.pack('<BIB', 1, 7266, 112))
        until(lambda: not self.connector.runner.process.is_alive())
        self.assertIn('Duel request rejected: 112', self.connector.error)
        self.assertEqual(self.connector.match['phase'], 'failed')


class StateTests(unittest.TestCase):
    def test_native_initial_spells_with_hidden_cooldowns(self):
        packet = json.loads((Path(__file__).parent / 'fixtures/initial-spells.json').read_text())
        data = bytes.fromhex(packet['payload_hex'])
        state = State(4, 'NativeFixture')
        state.packet(packet['opcode'], data)
        self.assertTrue(state.spells_ready)
        self.assertGreater(len(state.spells), 100)
        self.assertEqual(len(state.cooldowns), 1)
        with self.assertRaises(ValueError):
            state.packet(packet['opcode'], data[:-1])

    def test_compressed_updates_and_out_of_range_do_not_invent_opponent_state(self):
        state = State(42, 'Tester')
        data = struct.pack('<I', 2) + player_block(42, 80) + player_block(77, 60)
        state.packet(0x1f6, struct.pack('<I', len(data)) + zlib.compress(data))
        self.assertEqual(state.player(77)['health'], 60)
        state.packet(0xa9, struct.pack('<IBI', 1, 4, 1) + packed(77))
        self.assertIsNone(state.player(77)['health'])
        self.assertFalse(state.player(77)['known'])

    def test_rejects_bad_compressed_size_and_parses_living_update(self):
        state = State(42, 'Tester')
        with self.assertRaises(ValueError):
            state.packet(0x1f6, struct.pack('<I', 100) + zlib.compress(b'short'))
        with self.assertRaises(ValueError):
            state.packet(0x1f6, struct.pack('<I', 100) + b'bad zlib')
        block = player_block(42, 80)
        prefix = bytes([3]) + packed(42) + bytes([4])
        stationary_size = len(prefix) + 18
        living = struct.pack('<H', 0x21) + struct.pack('<IHIffffI', 0, 0, 1000, 4, 5, 6, 0, 0) + struct.pack('<9f', *([7] * 9))
        state.packet(0xa9, struct.pack('<I', 1) + prefix + living + block[stationary_size:])
        self.assertEqual(state.player(42)['health'], 80)
        self.assertEqual(state.player(42)['position'], [4, 5, 6, 0])


class RunnerTests(unittest.TestCase):
    def test_slow_submission_skips_deadlines_instead_of_catching_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'harness.py'
            path.write_text('def start(c): return {}\ndef tick(o,m): return []\n')
            calls, failed = [], []

            def submit(actions):
                calls.append(time.monotonic())
                time.sleep(0.15)

            runner = Runner(path, dict, submit, failed.append)
            runner.start({})
            try:
                until(lambda: len(calls) >= 3)
            finally:
                runner.close()
            self.assertFalse(failed)
            self.assertGreater(calls[1] - calls[0], 0.22)


if __name__ == '__main__':
    unittest.main()
