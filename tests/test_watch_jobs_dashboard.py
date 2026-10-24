"""Dashboard balance and failure handling; no real keys, AWS, or network calls."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

import watch_jobs


class DashboardCreditsTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {}, clear=True).start()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key_file = Path(self.directory.name) / 'openrouter.env'
        mock.patch.object(watch_jobs, 'OPENROUTER_ENV', self.key_file).start()
        self.request = mock.patch.object(watch_jobs, 'urlopen').start()

    def response(self, data):
        self.request.return_value.__enter__.return_value = io.StringIO(json.dumps(data))

    def test_uses_local_key_and_calculates_remaining_account_balance(self):
        self.key_file.write_text('OPENROUTER_API_KEY=fake-local-key\n')
        self.response({'data': {'total_credits': 100.5, 'total_usage': 25.75}})
        self.assertEqual(watch_jobs.openrouter_credits(), '$74.75')
        request = self.request.call_args.args[0]
        self.assertEqual(request.full_url, 'https://openrouter.ai/api/v1/credits')
        self.assertEqual(request.get_header('Authorization'), 'Bearer fake-local-key')
        self.assertEqual(self.request.call_args.kwargs['timeout'], 5)

    def test_environment_key_takes_precedence(self):
        self.key_file.write_text('OPENROUTER_API_KEY=fake-local-key\n')
        self.response({'data': {'total_credits': 10, 'total_usage': 10}})
        with mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': 'fake-env-key'}):
            self.assertEqual(watch_jobs.openrouter_credits(), '$0.00')
        request = self.request.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer fake-env-key')

    def test_missing_key_does_not_make_a_request(self):
        self.assertIn('not configured', watch_jobs.openrouter_credits())
        self.request.assert_not_called()

    def test_errors_do_not_leak_keys_or_stop_the_dashboard(self):
        self.key_file.write_text('OPENROUTER_API_KEY=fake-private-key\n')
        for error, expected in (
            (HTTPError('https://openrouter.ai', 403, 'fake-private-key', {}, None), 'HTTP 403'),
            (URLError('fake-private-key'), 'lookup failed'),
        ):
            with self.subTest(error=type(error).__name__):
                self.request.side_effect = error
                result = watch_jobs.openrouter_credits()
                self.assertIn(expected, result)
                screen = watch_jobs.dashboard([], 'test-region', 5, credits=result)
                self.assertIn('No active jobs.', screen)
                self.assertNotIn('fake-private-key', screen)

    def test_invalid_balance_does_not_display_an_invented_amount(self):
        self.key_file.write_text('OPENROUTER_API_KEY=fake-key\n')
        for data in ({}, {'data': None}, {'data': {'total_credits': 'nan', 'total_usage': 0}}):
            with self.subTest(data=data):
                self.response(data)
                self.assertIn('unavailable', watch_jobs.openrouter_credits())

    def test_balance_refreshes_every_thirty_seconds_and_survives_aws_errors(self):
        now = 0

        def sleep(seconds):
            nonlocal now
            now += seconds
            if now >= 40:
                raise KeyboardInterrupt

        output = io.StringIO()
        with mock.patch.object(watch_jobs.sys, 'argv', ['watch_jobs.py']), \
             mock.patch.object(watch_jobs, 'openrouter_credits', side_effect=['$100.00', '$99.00']) as balance, \
             mock.patch.object(watch_jobs, 'active_jobs', side_effect=watch_jobs.AwsError('AWS unavailable')), \
             mock.patch.object(watch_jobs.time, 'monotonic', side_effect=lambda: now), \
             mock.patch.object(watch_jobs.time, 'sleep', side_effect=sleep), \
             contextlib.redirect_stdout(output):
            watch_jobs.main()
        self.assertEqual(balance.call_count, 2)
        self.assertIn('OpenRouter credits: $100.00', output.getvalue())
        self.assertIn('OpenRouter credits: $99.00', output.getvalue())
        self.assertIn('AWS unavailable', output.getvalue())


if __name__ == '__main__':
    unittest.main()
