"""Credential-free AWS Batch log watcher tests; no network or jobs are used."""

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('launch', REPOSITORY / 'launch.py')
watcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watcher)

REGION = 'us-west-2'
GROUP = '/aws/batch/agent-wow'
JOB_ID = 'test-job-id'
STREAM = 'agent-wow/default/test-job-id'


def option(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def job(status, stream=None, reason=None):
    result = {'jobId': JOB_ID, 'status': status, 'container': {}}
    if stream:
        result['container']['logStreamName'] = stream
    if reason:
        result['statusReason'] = reason
    return {'jobs': [result]}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        if self.sleeps > 50:
            raise AssertionError('Watcher did not stop after the terminal state')
        self.now += seconds


class WatchJobTests(unittest.TestCase):
    def follow(self, api, *, final_wait_seconds=0):
        clock = Clock()
        output = io.StringIO()
        with mock.patch.object(watcher, 'aws', side_effect=lambda *args: api(clock, *args)) as call:
            with mock.patch.object(watcher.time, 'monotonic', side_effect=clock.monotonic):
                with mock.patch.object(watcher.time, 'sleep', side_effect=clock.sleep):
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        result = watcher.follow(
                            JOB_ID, REGION, GROUP, poll_seconds=1,
                            final_wait_seconds=final_wait_seconds,
                        )
        return result, output.getvalue(), call.call_args_list, clock

    def test_queued_job_waits_for_own_stream_and_drains_delayed_final_logs(self):
        states = iter([job('RUNNABLE'), job('RUNNABLE'), job('RUNNING', STREAM)])

        def api(clock, region, *args):
            self.assertEqual(region, REGION)
            if args[:2] == ('batch', 'describe-jobs'):
                self.assertEqual(option(args, '--jobs'), JOB_ID)
                return next(states, job('SUCCEEDED', STREAM))
            self.assertEqual(args[:2], ('logs', 'get-log-events'))
            self.assertEqual(option(args, '--log-group-name'), GROUP)
            self.assertEqual(option(args, '--log-stream-name'), STREAM)
            token = option(args, '--next-token')
            if token is None:
                return {'events': [{'message': 'first gameplay log'}], 'nextForwardToken': 'one'}
            if clock.now >= 5 and token == 'one':
                return {'events': [{'message': 'delayed final report'}], 'nextForwardToken': 'two'}
            return {'events': [], 'nextForwardToken': token}

        result, output, calls, clock = self.follow(api, final_wait_seconds=5)
        self.assertEqual(result, 0)
        self.assertEqual(output.count('RUNNABLE'), 1, 'Only status changes should be reported')
        self.assertIn('RUNNING', output)
        self.assertIn('SUCCEEDED', output)
        self.assertEqual(output.count('first gameplay log'), 1)
        self.assertEqual(output.count('delayed final report'), 1)
        self.assertGreaterEqual(clock.now, 5)
        self.assertTrue(calls)

    def test_stream_change_discards_the_previous_stream_token(self):
        new_stream = STREAM + '-retry'
        states = iter([job('RUNNING', STREAM), job('RUNNING', new_stream)])
        seen = set()

        def api(clock, region, *args):
            if args[:2] == ('batch', 'describe-jobs'):
                return next(states, job('SUCCEEDED', new_stream))
            self.assertEqual(args[:2], ('logs', 'get-log-events'))
            stream = option(args, '--log-stream-name')
            token = option(args, '--next-token')
            if stream not in seen:
                self.assertIsNone(token, 'Tokens belong to one stream and must not be reused')
                seen.add(stream)
                return {'events': [{'message': 'line-' + stream}], 'nextForwardToken': stream + '-token'}
            self.assertEqual(token, stream + '-token')
            return {'events': [], 'nextForwardToken': token}

        result, output, _, _ = self.follow(api)
        self.assertEqual(result, 0)
        self.assertEqual(seen, {STREAM, new_stream})
        self.assertIn('line-' + new_stream, output)

    def test_failed_job_before_stream_exists_returns_failure_and_reason(self):
        def api(clock, region, *args):
            self.assertEqual(args[:2], ('batch', 'describe-jobs'))
            return job('FAILED', reason='Could not start the container')

        result, output, _, _ = self.follow(api)
        self.assertEqual(result, 1)
        self.assertIn('FAILED', output)
        self.assertIn('Could not start the container', output)

    def test_terminal_job_waits_for_delayed_stream_metadata_and_creation(self):
        def api(clock, region, *args):
            if args[:2] == ('batch', 'describe-jobs'):
                return job('SUCCEEDED', STREAM if clock.now >= 3 else None)
            if clock.now < 5:
                raise watcher.AwsError('ResourceNotFoundException')
            token = option(args, '--next-token')
            if token is None:
                return {'events': [{'message': 'late stream final log'}], 'nextForwardToken': 'one'}
            return {'events': [], 'nextForwardToken': token}

        result, output, _, clock = self.follow(api, final_wait_seconds=5)
        self.assertEqual(result, 0)
        self.assertEqual(output.count('late stream final log'), 1)
        self.assertGreaterEqual(clock.now, 10)

    def test_empty_page_with_new_token_does_not_hide_following_logs(self):
        pages = [
            {'events': [], 'nextForwardToken': 'one'},
            {'events': [{'message': 'one log line'}], 'nextForwardToken': 'two'},
            {'events': [], 'nextForwardToken': 'two'},
        ]
        output = io.StringIO()
        with mock.patch.object(watcher, 'aws', side_effect=pages) as api:
            with contextlib.redirect_stdout(output):
                token, caught_up = watcher.drain_logs(REGION, GROUP, STREAM, None)
        self.assertEqual(token, 'two')
        self.assertTrue(caught_up)
        self.assertEqual(api.call_count, 3)
        self.assertIsNone(option(api.call_args_list[0].args, '--next-token'))
        self.assertEqual(option(api.call_args_list[1].args, '--next-token'), 'one')
        self.assertEqual(option(api.call_args_list[2].args, '--next-token'), 'two')
        self.assertEqual(output.getvalue().count('one log line'), 1)

    def test_busy_stream_has_bounded_page_fetches(self):
        page_count = 0

        def api(region, *args):
            nonlocal page_count
            page_count += 1
            if page_count > 20:
                raise AssertionError('Live log stream starved job status checks')
            return {'events': [], 'nextForwardToken': str(page_count)}

        with mock.patch.object(watcher, 'aws', side_effect=api):
            token, caught_up = watcher.drain_logs(REGION, GROUP, STREAM, None)
        self.assertEqual(page_count, 20)
        self.assertEqual(token, '20')
        self.assertFalse(caught_up)

    def test_stream_creation_delay_is_retryable(self):
        with mock.patch.object(watcher, 'aws', side_effect=watcher.AwsError('ResourceNotFoundException')):
            self.assertEqual(watcher.drain_logs(REGION, GROUP, STREAM, None), (None, True))

    def test_log_permission_errors_are_not_silently_ignored(self):
        with mock.patch.object(watcher, 'aws', side_effect=watcher.AwsError('AccessDeniedException')):
            with self.assertRaisesRegex(watcher.AwsError, 'AccessDeniedException'):
                watcher.drain_logs(REGION, GROUP, STREAM, None)

    def test_aws_cli_json_is_parsed_and_profile_output_is_overridden(self):
        completed = subprocess.CompletedProcess([], 0, stdout='{"jobs": []}', stderr='')
        with mock.patch.object(watcher.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(watcher.aws(REGION, 'batch', 'describe-jobs', '--jobs', JOB_ID), {'jobs': []})
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'aws')
        self.assertEqual(option(command, '--region'), REGION)
        self.assertEqual(option(command, '--output'), 'json')
        self.assertIn('--no-cli-pager', command)

    def test_aws_cli_failure_has_a_useful_error(self):
        completed = subprocess.CompletedProcess([], 254, stdout='', stderr='ExpiredToken: sign in again')
        with mock.patch.object(watcher.subprocess, 'run', return_value=completed):
            with self.assertRaisesRegex(watcher.AwsError, 'ExpiredToken'):
                watcher.aws(REGION, 'batch', 'describe-jobs', '--jobs', JOB_ID)

    def test_ssl_eof_is_retried_then_returns_response(self):
        failed = subprocess.CompletedProcess([], 255, stdout='', stderr=(
            'SSL validation failed: [SSL: UNEXPECTED_EOF_WHILE_READING] '
            'EOF occurred in violation of protocol'
        ))
        success = subprocess.CompletedProcess([], 0, stdout='{"jobs": []}', stderr='')
        with mock.patch.object(watcher.subprocess, 'run', side_effect=[failed, success]) as run, \
             mock.patch.object(watcher.time, 'sleep') as sleep, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(watcher.aws(REGION, 'batch', 'describe-jobs', '--jobs', JOB_ID), {'jobs': []})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args, run.call_args_list[1].args)
        sleep.assert_called_once_with(2)

    def test_request_timeout_is_retried_without_restarting_job(self):
        timeout = subprocess.TimeoutExpired(['aws'], 40)
        success = subprocess.CompletedProcess([], 0, stdout='{"jobs": []}', stderr='')
        with mock.patch.object(watcher.subprocess, 'run', side_effect=[timeout, success]) as run, \
             mock.patch.object(watcher.time, 'sleep') as sleep, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(watcher.aws(REGION, 'batch', 'describe-jobs', '--jobs', JOB_ID), {'jobs': []})
        self.assertTrue(all(call.args[0][1:3] == ['batch', 'describe-jobs'] for call in run.call_args_list))
        sleep.assert_called_once_with(2)

    def test_transient_retries_are_bounded(self):
        failed = subprocess.CompletedProcess([], 255, stdout='', stderr='Connection reset by peer')
        with mock.patch.object(watcher.subprocess, 'run', return_value=failed) as run, \
             mock.patch.object(watcher.time, 'sleep') as sleep, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(watcher.AwsError, 'Connection reset'):
                watcher.aws(REGION, 'batch', 'describe-jobs', '--jobs', JOB_ID)
        self.assertEqual(run.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8, 16, 30])

    def test_permission_and_certificate_errors_are_not_retried(self):
        for error in ('AccessDeniedException', 'ExpiredToken', 'SSL: CERTIFICATE_VERIFY_FAILED'):
            with self.subTest(error=error):
                failed = subprocess.CompletedProcess([], 254, stdout='', stderr=error)
                with mock.patch.object(watcher.subprocess, 'run', return_value=failed) as run, \
                     mock.patch.object(watcher.time, 'sleep') as sleep:
                    with self.assertRaises(watcher.AwsError):
                        watcher.aws(REGION, 'batch', 'describe-jobs', '--jobs', JOB_ID)
                run.assert_called_once()
                sleep.assert_not_called()

    def test_main_reports_aws_errors_and_nonzero_exit(self):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['launch.py', '--watch', JOB_ID]):
            with mock.patch.object(watcher, 'follow', side_effect=watcher.AwsError('AccessDenied: view job')):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(watcher.main(), 1)
        self.assertIn('AccessDenied', output.getvalue())

    def test_ctrl_c_stops_watching_without_cancelling_job(self):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['launch.py', '--watch', JOB_ID]):
            with mock.patch.object(watcher, 'aws', side_effect=KeyboardInterrupt) as api:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(watcher.main(), 130)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[1:3], ('batch', 'describe-jobs'))
        message = output.getvalue().lower()
        self.assertIn('not cancelled', message)


if __name__ == '__main__':
    unittest.main()
