import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import viewer


class ViewerTests(unittest.TestCase):
    def test_account_runs_enable_the_spectator_with_the_selected_harness(self):
        config = {'job_definitions': {'codex': 'codex-definition', 'claude': 'claude-definition'},
                  'viewer_job_queue': 'viewer-queue', 'world_watch_image': 'world-image'}
        for harness in ('codex', 'claude'):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as directory, \
                    patch.object(viewer, 'ROOT', Path(directory)), \
                    patch.object(viewer, 'outputs', return_value=config), \
                    patch.object(viewer, 'windows', return_value='viewer-instance'), \
                    patch.object(viewer, 'wait_ready'), patch.object(viewer, 'attach') as attach, \
                    patch.object(viewer, 'connect', return_value=0), \
                    patch.object(viewer, 'submit_job', return_value={'jobId': 'account-job'}) as submit, \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, 'argv', ['viewer.py', '--launch', '--harness', harness,
                                             '--prompt', 'reach level 20']), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(viewer.main(), 0)
            args = submit.call_args.args
            self.assertEqual(args[2], f'{harness}-definition')
            self.assertEqual(args[3]['command'], ['run-wow-agent', 'reach level 20'])
            settings = {item['name']: item['value'] for item in args[3]['environment']}
            self.assertEqual(settings['AGENT_WOW_HARNESS'], harness)
            self.assertEqual(settings['AGENT_WOW_VIEWER'], '1')
            self.assertEqual(settings['AGENT_WOW_WORLD_IMAGE'], 'world-image')
            attach.assert_called_once_with('account-job', 'viewer-instance', config)

    def test_completed_job_is_not_attached(self):
        with patch.object(viewer, 'aws', return_value=[{'status': 'SUCCEEDED'}]), patch.object(viewer, 'upload') as upload:
            with self.assertRaisesRegex(RuntimeError, 'already SUCCEEDED'):
                viewer.attach('job', 'viewer', {})
            upload.assert_not_called()

    def test_existing_viewer_is_reused(self):
        with patch.object(viewer, 'instances', return_value=['i-existing']), patch.object(viewer, 'start') as start:
            self.assertEqual(viewer.windows({}), 'i-existing')
            start.assert_not_called()

    def test_multiple_viewers_are_rejected(self):
        with patch.object(viewer, 'instances', return_value=['i-one', 'i-two']):
            with self.assertRaisesRegex(RuntimeError, 'Multiple Windows'):
                viewer.windows({})


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.instructions = self.root / 'AGENTS.md'
        self.instructions.write_text('Play fairly.\n')
        self.aws = MagicMock()
        self.docker = MagicMock()
        self.world = MagicMock()
        self.world.attach_socket.return_value._sock.recv.return_value = b'You change security level of account WATCHER to 2.'
        self.docker.from_env.return_value.containers.list.return_value = [self.world]
        with patch.dict(sys.modules, {'boto3': self.aws, 'docker': self.docker}), \
                patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'docker'), *sys.path]):
            self.accounts = importlib.import_module('prepare_wow_account')
            self.module = importlib.import_module('prepare_viewer')

    def test_saved_player_credentials_are_reused_but_cannot_be_used_for_observer(self):
        auth = self.root / 'auth.json'
        original = json.dumps({'username': 'EXISTING', 'password': 'AlreadySaved123'})
        auth.write_text(original)
        with patch.object(self.accounts.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            account, _ = self.accounts.ensure_account(self.root)
            self.assertEqual(account['username'], 'EXISTING')
            with self.assertRaises(ValueError):
                self.accounts.ensure_account(self.root, 'WATCHER')
        self.assertEqual(auth.read_text(), original)
        self.world.attach_socket.assert_not_called()

    def test_observer_creation_uses_separate_account_and_no_agent_mutations(self):
        results = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                   subprocess.CompletedProcess([], 0, '{"characters":[]}'), subprocess.CompletedProcess([], 0)]
        with patch.object(self.module.subprocess, 'run', side_effect=results) as run, \
                patch.object(self.module.time, 'sleep'), \
                patch.dict('os.environ', {'AWS_RESULTS_BUCKET': 'bucket', 'AWS_BATCH_JOB_ID': 'job'}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.module.prepare_account(self.root / 'observer', self.instructions)
        sent = ''.join(call.args[0].decode() for call in self.world.attach_socket.return_value._sock.sendall.call_args_list)
        self.assertIn('account create WATCHER ', sent)
        self.assertIn('account set gmlevel WATCHER 2 -1', sent)
        self.assertNotIn('AGENT', sent)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['env']['AGENT_WOW_CONFIG_DIR'], str(self.root / 'observer'))
        exported = self.aws.client.return_value.put_object.call_args.kwargs
        self.assertEqual(exported['Key'], 'viewer/job/account.json')
        self.assertEqual(json.loads(exported['Body'])['character'], 'Observer')
        self.assertEqual(len(json.loads(exported['Body'])['password']), 16)

    def test_saved_observer_is_not_duplicated(self):
        results = [subprocess.CompletedProcess([], 0),
                   subprocess.CompletedProcess([], 0, '{"characters":[{"name":"Observer"}]}')]
        with patch.object(self.module.subprocess, 'run', side_effect=results) as run, \
                patch.dict('os.environ', {'AWS_RESULTS_BUCKET': 'bucket', 'AWS_BATCH_JOB_ID': 'job'}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.module.prepare_account(self.root / 'observer', self.instructions)
        self.assertEqual(run.call_count, 2)
        sent = self.world.attach_socket.return_value._sock.sendall.call_args.args[0].decode()
        self.assertEqual(sent, 'account set gmlevel WATCHER 2 -1\n')

    def test_unconfirmed_permissions_prevent_exporting_an_unusable_observer(self):
        self.world.attach_socket.return_value._sock.recv.return_value = b''
        with patch.object(self.module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.dict('os.environ', {'AWS_RESULTS_BUCKET': 'bucket', 'AWS_BATCH_JOB_ID': 'job'}):
            with self.assertRaisesRegex(RuntimeError, 'permissions were not confirmed'):
                self.module.prepare_account(self.root / 'observer', self.instructions)
        self.aws.client.return_value.put_object.assert_not_called()


if __name__ == '__main__':
    unittest.main()
