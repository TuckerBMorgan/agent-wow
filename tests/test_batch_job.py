"""Credential-free regression tests for the container Batch supervisor.

Run with: python3 -m unittest discover -s tests -p 'test_batch_job.py'
No Docker daemon, AWS account, OpenAI API, or game server is used.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


def load_wrapper():
    path = Path(__file__).resolve().parents[1] / 'docker/run_batch_job.py'
    boto3 = types.ModuleType('boto3')
    boto3.client = mock.Mock(side_effect=AssertionError('Real AWS calls are forbidden in tests'))
    botocore = types.ModuleType('botocore')
    config = types.ModuleType('botocore.config')
    config.Config = mock.Mock()
    module = types.ModuleType('batch_job_under_test')
    with mock.patch.dict(sys.modules, {
        'boto3': boto3, 'botocore': botocore, 'botocore.config': config,
    }):
        exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
    return module


WRAPPER = load_wrapper()
FAKE_KEY = 'unit-test-only-api-key-not-a-real-credential'


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, **request):
        self.calls.append(request)
        self.objects[request['Key']] = request['Body']


class BatchJobTests(unittest.TestCase):
    def setUp(self):
        # Never inherit real credentials or inspect the caller's credential files.
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {'CODEX_API_KEY': FAKE_KEY}, clear=True).start()
        self.directory = tempfile.TemporaryDirectory(prefix='agent-wow-batch-test-')
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name) / 'workspace'
        self.workspace.mkdir()
        self.client = FakeS3()
        self.results = WRAPPER.Results(self.client, 'test-results', 'test-job', self.workspace)
        self.console = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.console)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def write(self, relative, content=b'quest evidence'):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def statuses(self):
        return [json.loads(call['Body']) for call in self.client.calls
                if call['Key'] == 'runs/test-job/status.json']

    def test_exports_only_reports_and_journal_and_redacts_exact_api_key(self):
        self.write('journal/quest.json', b'{"turn_in_confirmed": true}')
        self.write('logs/agent/run/final-report.txt', b'finished')
        self.write('logs/agent/run/agent.log', b'before ' + FAKE_KEY.encode() + b' after')
        self.write('config/codex.env', FAKE_KEY.encode())
        self.write('.codex/auth.json', b'private login')
        self.write('server/.env', b'private database password')

        self.assertTrue(self.results.snapshot())

        self.assertEqual(set(self.client.objects), {
            'runs/test-job/journal/quest.json',
            'runs/test-job/logs/agent/run/final-report.txt',
            'runs/test-job/logs/agent/run/agent.log',
        })
        self.assertEqual(self.client.objects['runs/test-job/logs/agent/run/agent.log'],
                         b'before [REDACTED] after')
        self.assertTrue(all(call['Bucket'] == 'test-results' for call in self.client.calls))
        self.assertNotIn(FAKE_KEY.encode(), b''.join(self.client.objects.values()))

    def test_loads_only_the_selected_provider_secret(self):
        for provider, key_name, arn in (
            ('openai', 'CODEX_API_KEY', 'fake-openai-secret'),
            ('openrouter', 'OPENROUTER_API_KEY', 'fake-openrouter-secret'),
        ):
            with self.subTest(provider=provider):
                secrets = mock.Mock()
                secrets.get_secret_value.return_value = {'SecretString': FAKE_KEY}
                environment = {
                    'AWS_RESULTS_BUCKET': 'test-results', 'AWS_BATCH_JOB_ID': 'test-job',
                    'CODEX_MODEL_PROVIDER': provider, 'CODEX_MODEL': 'test-model',
                    'CODEX_SECRET_ARN': 'fake-openai-secret',
                    'OPENROUTER_SECRET_ARN': 'fake-openrouter-secret',
                }
                with mock.patch.dict(os.environ, environment, clear=True), \
                     mock.patch.object(WRAPPER.boto3, 'client', side_effect=[secrets, self.client]), \
                     mock.patch.object(WRAPPER, 'run', return_value=0):
                    self.assertEqual(WRAPPER.main(), 0)
                    self.assertEqual(os.environ[key_name], FAKE_KEY)
                    other_key = 'OPENROUTER_API_KEY' if provider == 'openai' else 'CODEX_API_KEY'
                    self.assertNotIn(other_key, os.environ)
                secrets.get_secret_value.assert_called_once_with(SecretId=arn)

    def test_skips_symlink_files_directories_and_fifo(self):
        outside = Path(self.directory.name) / 'private'
        outside.mkdir()
        (outside / 'secret.txt').write_bytes(b'must never be exported')
        self.write('journal/real.txt')
        (self.workspace / 'journal/file-link').symlink_to(outside / 'secret.txt')
        (self.workspace / 'journal/directory-link').symlink_to(outside, target_is_directory=True)
        # Even an in-root symlink must not be uploaded as an additional artifact.
        (self.workspace / 'journal/internal-link').symlink_to(self.workspace / 'journal/real.txt')
        os.mkfifo(self.workspace / 'journal/pipe')

        self.assertTrue(self.results.snapshot())

        self.assertEqual(set(self.client.objects), {'runs/test-job/journal/real.txt'})

    def test_skips_symlink_in_logs_parent(self):
        outside = Path(self.directory.name) / 'private-logs'
        (outside / 'agent').mkdir(parents=True)
        (outside / 'agent/secret.txt').write_bytes(b'must never be exported')
        (self.workspace / 'logs').symlink_to(outside, target_is_directory=True)
        self.write('journal/real.txt')

        self.assertTrue(self.results.snapshot())

        self.assertEqual(set(self.client.objects), {'runs/test-job/journal/real.txt'})

    def test_export_failure_does_not_mark_file_uploaded_so_it_can_retry(self):
        self.write('journal/quest.json')
        with mock.patch.object(self.client, 'put_object', side_effect=PermissionError('fake denial')):
            self.assertFalse(self.results.snapshot())
        self.assertTrue(self.results.snapshot())
        self.assertIn('runs/test-job/journal/quest.json', self.client.objects)

    def run_with_exit(self, exit_code):
        child = mock.Mock()
        child.poll.return_value = exit_code
        with mock.patch.object(WRAPPER.signal, 'signal'), \
             mock.patch.object(WRAPPER.subprocess, 'Popen', return_value=child) as launch:
            actual = WRAPPER.run(self.results, ['fake-game-launcher'])
        self.assertEqual(launch.call_args.args[0], ['fake-game-launcher'])
        self.assertEqual(launch.call_args.kwargs['env']['AGENT_WOW_BATCH_SUPERVISED'], '1')
        return actual

    def test_preserves_success_failure_and_signal_exit_codes(self):
        for child_code, expected in ((0, 0), (7, 7), (-signal.SIGKILL, 137)):
            with self.subTest(child_code=child_code):
                self.assertEqual(self.run_with_exit(child_code), expected)
                self.assertEqual(self.statuses()[-1]['state'], 'exited')
                self.assertEqual(self.statuses()[-1]['process_exit_code'], child_code)
                self.assertIn('exit 0 alone is not proof', self.statuses()[-1]['quest_completion'])

    def test_failed_final_artifact_upload_fails_otherwise_successful_job(self):
        self.write('journal/quest.json')
        original = self.client.put_object

        def reject_artifact(**request):
            if request['Key'].endswith('/quest.json'):
                raise PermissionError('fake denial')
            return original(**request)

        with mock.patch.object(self.client, 'put_object', side_effect=reject_artifact):
            self.assertNotEqual(self.run_with_exit(0), 0)
            self.assertEqual(self.run_with_exit(7), 7)

    def test_failed_final_status_upload_fails_otherwise_successful_job(self):
        original = self.client.put_object

        def reject_final_status(**request):
            if request['Key'].endswith('/status.json') and json.loads(request['Body'])['state'] == 'exited':
                raise PermissionError('fake denial')
            return original(**request)

        with mock.patch.object(self.client, 'put_object', side_effect=reject_final_status):
            self.assertNotEqual(self.run_with_exit(0), 0)

    def test_preflight_s3_failure_prevents_child_launch(self):
        with mock.patch.object(WRAPPER.signal, 'signal'), \
             mock.patch.object(self.client, 'put_object', side_effect=PermissionError('fake denial')), \
             mock.patch.object(WRAPPER.subprocess, 'Popen') as launch:
            with self.assertRaises(PermissionError):
                WRAPPER.run(self.results, ['fake-game-launcher'])
        launch.assert_not_called()

    def test_sigterm_is_forwarded_and_results_exported_before_slow_cleanup(self):
        report = self.write('journal/progress.txt', b'before interruption')
        handlers = {}
        events = []
        child = mock.Mock()
        child.poll.return_value = None
        child.send_signal.side_effect = lambda value: events.append(('signal', value))

        def slow_cleanup(timeout):
            events.append(('wait', timeout))
            raise subprocess.TimeoutExpired('fake-game-launcher', timeout)

        child.wait.side_effect = slow_cleanup
        original = self.client.put_object

        def record_upload(**request):
            events.append(('upload', request['Key'], request['Body']))
            return original(**request)

        def interrupt(_seconds):
            report.write_bytes(b'latest confirmed progress at interruption')
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with mock.patch.object(WRAPPER.signal, 'signal', side_effect=lambda number, handler: handlers.update({number: handler})), \
             mock.patch.object(WRAPPER.subprocess, 'Popen', return_value=child), \
             mock.patch.object(WRAPPER.time, 'sleep', side_effect=interrupt), \
             mock.patch.object(WRAPPER.time, 'monotonic', return_value=100.0), \
             mock.patch.object(self.client, 'put_object', side_effect=record_upload):
            status = WRAPPER.run(self.results, ['fake-game-launcher'])

        self.assertEqual(status, 143)
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual(self.client.objects['runs/test-job/journal/progress.txt'],
                         b'latest confirmed progress at interruption')
        self.assertEqual(self.statuses()[-1]['state'], 'interrupted')
        self.assertEqual(self.statuses()[-1]['process_exit_code'], 143)
        signal_index = events.index(('signal', signal.SIGTERM))
        wait_index = next(index for index, event in enumerate(events) if event[0] == 'wait')
        latest_upload = ('upload', 'runs/test-job/journal/progress.txt',
                         b'latest confirmed progress at interruption')
        self.assertLess(signal_index, events.index(latest_upload))
        self.assertLess(events.index(latest_upload), wait_index)
        self.assertLessEqual(events[wait_index][1], 20)

    def test_signal_during_popen_is_forwarded_once_after_child_is_available(self):
        self.write('journal/progress.txt', b'confirmed before interruption')
        for number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=number):
                handlers = {}
                child = mock.Mock()
                child.poll.return_value = None

                def launch(*_args, **_kwargs):
                    # At this point run() has not yet assigned its child variable.
                    handlers[number](number, None)
                    handlers[number](number, None)
                    child.send_signal.assert_not_called()
                    return child

                with mock.patch.object(WRAPPER.signal, 'signal', side_effect=lambda sig, handler: handlers.update({sig: handler})), \
                     mock.patch.object(WRAPPER.subprocess, 'Popen', side_effect=launch), \
                     mock.patch.object(WRAPPER.time, 'monotonic', return_value=100.0):
                    status = WRAPPER.run(self.results, ['fake-game-launcher'])

                self.assertEqual(status, 128 + number)
                child.send_signal.assert_called_once_with(number)
                child.wait.assert_called_once()
                self.assertEqual(self.statuses()[-1]['state'], 'interrupted')
                self.assertEqual(self.statuses()[-1]['process_exit_code'], 128 + number)
                self.assertIn('runs/test-job/journal/progress.txt', self.client.objects)

    def test_child_disappearing_before_signal_delivery_still_exports(self):
        self.write('journal/progress.txt')
        handlers = {}
        child = mock.Mock()
        child.poll.return_value = 0
        child.send_signal.side_effect = ProcessLookupError('fake child already exited')

        def launch(*_args, **_kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return child

        with mock.patch.object(WRAPPER.signal, 'signal', side_effect=lambda number, handler: handlers.update({number: handler})), \
             mock.patch.object(WRAPPER.subprocess, 'Popen', side_effect=launch):
            self.assertEqual(WRAPPER.run(self.results, ['fake-game-launcher']), 143)

        self.assertIn('runs/test-job/journal/progress.txt', self.client.objects)
        self.assertEqual(self.statuses()[-1]['state'], 'interrupted')

    def test_batch_without_key_fails_before_creating_aws_client(self):
        with mock.patch.dict(os.environ, {
            'AWS_BATCH_JOB_ID': 'test-job', 'AWS_RESULTS_BUCKET': 'test-results',
        }, clear=True), mock.patch.object(WRAPPER.boto3, 'client') as client:
            self.assertNotEqual(WRAPPER.main(), 0)
        client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
