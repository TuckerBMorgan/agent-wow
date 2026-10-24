"""Local SQLite bookkeeping tests. No real AWS calls or jobs are used."""

import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import jobs
import launch
import launch_all


class JobHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'jobs.sqlite3'
        patch = mock.patch.object(jobs, 'DB_PATH', self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def rows(self):
        with jobs.database() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY job_id')]

    def test_submission_records_exact_prompt_and_settings_without_secrets(self):
        prompt = 'Create "Orc"; don\'t evaluate $(anything).\nAnother line.'
        overrides = {'command': ['run-wow-agent', prompt], 'environment': [
            {'name': 'CODEX_MODEL_PROVIDER', 'value': 'openrouter'},
            {'name': 'CODEX_MODEL', 'value': 'anthropic/test'},
            {'name': 'CODEX_REASONING_EFFORT', 'value': 'high'},
            {'name': 'OPENROUTER_API_KEY', 'value': 'fake-secret-must-not-persist'},
        ]}
        with mock.patch.object(launch, 'aws_request', return_value={'jobId': 'one'}) as api:
            launch.submit_job('test-region', 'test-job', 'test-definition:1', overrides)
        self.assertEqual(api.call_count, 1)
        row, = self.rows()
        self.assertEqual(row['prompt'], prompt)
        self.assertEqual(row['provider'], 'openrouter')
        self.assertEqual(row['model'], 'anthropic/test')
        self.assertEqual(row['effort'], 'high')
        self.assertEqual(row['region'], 'test-region')
        self.assertEqual(row['status'], 'SUBMITTED')
        self.assertEqual(row['job_definition'], 'test-definition:1')
        self.assertNotIn(b'fake-secret', self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_submission_is_not_recorded_or_retried(self):
        with mock.patch.object(launch, 'aws_request', side_effect=launch.AwsError('failure')) as api:
            with self.assertRaises(launch.AwsError):
                launch.submit_job('test-region', 'test-job', 'test-definition', {})
        self.assertEqual(api.call_count, 1)
        self.assertEqual(self.rows(), [])

    def test_native_deepseek_submission_records_its_model_and_effort(self):
        with mock.patch.dict(launch.os.environ, {}, clear=True):
            overrides = launch.container_overrides('Full orc warrior task', 'deepseek')
        with mock.patch.object(launch, 'aws_request', return_value={'jobId': 'deepseek-job'}):
            launch.submit_job('test-region', 'deepseek-test', 'agent-wow-deepseek:1', overrides)
        row, = self.rows()
        self.assertEqual(row['model'], 'deepseek/deepseek-v4.1-flash-1017')
        self.assertEqual(row['effort'], 'high')
        self.assertEqual(row['provider'], 'openrouter')

    def test_database_failure_before_submission_prevents_launch(self):
        with mock.patch.object(launch, 'database', side_effect=sqlite3.OperationalError('read only')):
            with mock.patch.object(launch, 'aws_request') as api:
                with self.assertRaises(sqlite3.Error):
                    launch.submit_job('test-region', 'test-job', 'test-definition', {})
        api.assert_not_called()

    def test_subscription_history_is_distinguished_from_openrouter_billing(self):
        with mock.patch.dict(launch.os.environ, {}, clear=True):
            overrides=launch.container_overrides('Orc warrior task','claude-subscription')
        with mock.patch.object(launch,'aws_request',return_value={'jobId':'subscription-job'}):
            launch.submit_job('test-region','subscription-test','agent-wow-claude-subscription:1',overrides)
        row, = self.rows()
        self.assertEqual(row['provider'],'claude-subscription')
        self.assertEqual(row['model'],'claude-opus-5-5')
        self.assertEqual(row['effort'],'medium')

    def test_failure_to_record_after_submission_reports_job_id_without_retry(self):
        output = io.StringIO()
        with mock.patch.object(launch, 'aws_request', return_value={'jobId': 'already-submitted'}) as api:
            with mock.patch.object(launch, 'record_job', side_effect=sqlite3.OperationalError('disk full')):
                with contextlib.redirect_stderr(output), self.assertRaises(sqlite3.Error):
                    launch.submit_job('test-region', 'test-job', 'test-definition', {})
        self.assertEqual(api.call_count, 1)
        self.assertIn('already-submitted', output.getvalue())
        self.assertIn('Save this job ID', output.getvalue())

    def test_matrix_launcher_uses_shared_history(self):
        with mock.patch.object(launch_all, 'RACE_CLASSES', {'orc': ('warrior', 'hunter')}):
            with mock.patch.object(launch_all.sys, 'argv', ['launch_all.py']):
                with mock.patch.object(launch_all.os, 'environ', {}):
                    with mock.patch.object(launch, 'aws_request', side_effect=[{'jobId': 'one'}, {'jobId': 'two'}]):
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(launch_all.main(), 0)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['job_name'] for row in rows}, {'agent-wow-orc-warrior', 'agent-wow-orc-hunter'})
        self.assertTrue(all(row['prompt'].startswith('Create one orc ') for row in rows))

    def test_claude_matrix_routes_and_records_native_claude_settings(self):
        environment = {'CLAUDE_MODEL': 'anthropic/claude-opus-5.5', 'CLAUDE_EFFORT': 'medium',
                       'CODEX_MODEL': 'must-not-be-recorded'}
        with mock.patch.object(launch_all, 'RACE_CLASSES', {'orc': ('warrior', 'hunter')}), \
             mock.patch.object(launch_all.sys, 'argv', ['launch_all.py', '--harness', 'claude']), \
             mock.patch.dict(launch_all.os.environ, environment, clear=True), \
             mock.patch.object(launch, 'aws_request', side_effect=[{'jobId': 'one'}, {'jobId': 'two'}]) as api, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launch_all.main(), 0)
        self.assertEqual(api.call_count, 2)
        for call in api.call_args_list:
            args = call.args
            self.assertEqual(args[args.index('--job-definition') + 1], 'agent-wow-claude')
        rows = self.rows()
        self.assertEqual({row['job_name'] for row in rows}, {
            'agent-wow-claude-orc-warrior', 'agent-wow-claude-orc-hunter',
        })
        self.assertTrue(all(row['provider'] == 'openrouter' for row in rows))
        self.assertTrue(all(row['model'] == 'anthropic/claude-opus-5.5' for row in rows))
        self.assertTrue(all(row['effort'] == 'medium' for row in rows))
        self.assertTrue(all(row['prompt'].startswith('Create one orc ') for row in rows))


if __name__ == '__main__':
    unittest.main()
