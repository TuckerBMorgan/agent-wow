import contextlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import provision


class ProvisionTests(unittest.TestCase):
    def test_native_secondary_default_does_not_weaken_primary_skill_validation(self):
        expected = {'professions': {'202': 450}}
        for rank in (450, 449):
            with self.subTest(rank=rank), tempfile.TemporaryDirectory() as directory:
                actual = {'professions': {'202': rank, '129': 270}}
                validator = SimpleNamespace(PRIMARY={202}, observed_build=lambda *a: actual,
                    check_specializations=lambda *a, **kw: None, normalize=lambda x: x,
                    differences=lambda a, b: [] if a == b else [{'field': 'professions'}])
                with patch.object(provision, 'export'), patch.object(provision.time, 'monotonic', side_effect=[0, 11]):
                    if rank == 450:
                        provision.verify_saved(None, 'world', 'Fighter', Path(directory)/'saved.sql', expected, validator, Path('dbc'))
                    else:
                        with self.assertRaises(ValueError):
                            provision.verify_saved(None, 'world', 'Fighter', Path(directory)/'saved.sql', expected, validator, Path('dbc'))

    def test_inventory_retries_inconsistent_native_save_without_mutating(self):
        inventory = [{'item': '95', 'bag': '0', 'slot': '23'}]
        snapshots = iter([{'item_instance': [], 'character_inventory': inventory},
                          {'item_instance': [{'guid': '95', 'itemEntry': '123'}], 'character_inventory': inventory}])
        calls = []

        def snapshot():
            calls.append(True)
            return next(snapshots)

        with patch.object(provision.time, 'sleep'):
            rows = provision.read_inventory(snapshot)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows[0]['itemEntry'], '123')

    def test_startup_uses_selected_character_and_does_not_create_a_replacement(self):
        for present in (True, False):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/'character.json').write_text('{"name":"Presetfighter","build":"builds/example/build.json"}')
                (root/'characters.json').write_text(json.dumps({'characters': [{'name':'Presetfighter'}] if present else []}))
                cli = root/'agent-wow'
                cli.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
r=Path(os.environ['FAKE_ROOT'])
with (r/'calls.jsonl').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1:3]==['auth','init']:sys.stdin.read()
if sys.argv[1:3]==['char','list']:print((r/'characters.json').read_text())
if sys.argv[1:3]==['char','play']:print('entered '+sys.argv[3])
''')
                cli.chmod(0o755)
                code = (Path(provision.__file__).parent/'client.py').read_text().replace('/workspace/logs/session.log',str(root/'session.log')).replace('/workspace/character.json',str(root/'character.json'))
                env = dict(os.environ,FAKE_ROOT=str(root),PATH=str(root)+os.pathsep+os.environ['PATH'],WOW_ACCOUNT='TESTPLAYER',WOW_PASSWORD='test-only',WOW_CHARACTER='Legacy',WOW_CLASS='warrior')
                result = subprocess.run([sys.executable,'-c',code],env=env,capture_output=True)
                calls = [json.loads(l) for l in (root/'calls.jsonl').read_text().splitlines()]
                self.assertFalse(any(c[:2]==['char','create'] for c in calls))
                self.assertEqual(result.returncode==0,present)
                if present:self.assertIn('entered Presetfighter',(root/'session.log').read_text())

    def test_saved_build_waits_for_async_persistence_without_reapplying(self):
        exports = []
        expected = {'equipment': 'expected'}
        validator = SimpleNamespace(observed_build=lambda *a: expected, normalize=lambda b: b,
            check_specializations=lambda *a, **kw: None)
        checks = iter([[{'field': 'equipment'}], []])
        validator.differences = lambda *a: next(checks)
        with patch.object(provision, 'export', lambda *a: exports.append(a)), patch.object(provision.time, 'sleep'):
            provision.verify_saved(None, 'world', 'Fighter', Path('dump.sql'), expected, validator, Path('dbc'))
        self.assertEqual(len(exports), 2)

    def test_missing_second_selection_cannot_stop_or_mutate_clients(self):
        calls = []

        def docker(*args, **kwargs):
            calls.append(args)
            if args[0] == 'compose':
                return str(args[-1]).encode()
            if args[0] == 'exec' and args[3] == 'client-1':
                return json.dumps({'path': 'builds/example/build.json', 'build': {}, 'source': {}, 'account': 'PLAYER'}).encode()
            raise RuntimeError('Selection missing')

        with tempfile.TemporaryDirectory() as directory, patch.object(provision, 'STATE', Path(directory)), \
                patch.object(provision, 'docker', docker), patch.object(sys, 'argv', ['provision.py']), \
                patch.object(provision.os, 'umask'), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'client-2: select'):
                provision.main()
        self.assertTrue(all(args[0] in ('compose', 'exec') for args in calls))

    def test_selection_cannot_escape_build_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'builds/example').mkdir(parents=True)
            (root / 'builds/example/build.json').write_text('{}')
            (root / 'builds/index.json').write_text(json.dumps({'builds': [{'build': 'example/build.json'}]}))
            code = 'import sys\n' + provision.SELECT.replace("r=Path('/workspace')", 'r=Path(sys.argv[1])')
            env = dict(os.environ, WOW_ACCOUNT='TESTPLAYER')
            for selected, succeeds in [('builds/example/build.json', True), ('../build.json', False), ('builds/index.json', False)]:
                (root / 'next-match.json').write_text(json.dumps({'build': selected}))
                result = subprocess.run([sys.executable, '-c', code, str(root)], env=env, capture_output=True)
                self.assertEqual(result.returncode == 0, succeeds, selected)
            (root / 'outside').mkdir()
            (root / 'outside/build.json').write_text('{}')
            (root / 'builds/link').symlink_to(root / 'outside', target_is_directory=True)
            (root / 'next-match.json').write_text('{"build":"builds/link/build.json"}')
            result = subprocess.run([sys.executable, '-c', code, str(root)], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_console_handles_prompt_split_across_docker_frames(self):
        client, server = socket.socketpair()
        console = provision.Console.__new__(provision.Console)
        console.sock = client
        observed = []

        def reply():
            observed.append(server.recv(1024))
            for data in (b'server info\nAC> ', b'World ready\nA', b'C>', b' '):
                server.sendall(struct.pack('>BxxxI', 1, len(data)) + data)
            server.close()

        thread = threading.Thread(target=reply)
        thread.start()
        try:
            self.assertEqual(console.command('server info'), 'server info\nAC> World ready\nAC> ')
            self.assertEqual(observed, [b'server info\n'])
        finally:
            console.close()
            thread.join()

    def test_console_disconnect_fails_without_retrying(self):
        client, server = socket.socketpair()
        console = provision.Console.__new__(provision.Console)
        console.sock = client
        server.close()
        try:
            with self.assertRaises((RuntimeError, BrokenPipeError)):
                console.command('server info')
        finally:
            console.close()


if __name__ == '__main__':
    unittest.main()
