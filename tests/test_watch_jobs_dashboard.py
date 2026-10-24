"""Dashboard failure handling; no real AWS calls."""

import contextlib
import io
import unittest
from unittest import mock

import watch_jobs


class DashboardTests(unittest.TestCase):
    def test_dashboard_keeps_refreshing_after_aws_errors(self):
        now = 0

        def sleep(seconds):
            nonlocal now
            now += seconds
            if now >= 40:
                raise KeyboardInterrupt

        output = io.StringIO()
        with mock.patch.object(watch_jobs.sys, 'argv', ['watch_jobs.py']), \
             mock.patch.object(watch_jobs, 'active_jobs', side_effect=watch_jobs.AwsError('AWS unavailable')), \
             mock.patch.object(watch_jobs.time, 'sleep', side_effect=sleep), \
             contextlib.redirect_stdout(output):
            watch_jobs.main()
        self.assertIn('AWS unavailable', output.getvalue())


if __name__ == '__main__':
    unittest.main()
