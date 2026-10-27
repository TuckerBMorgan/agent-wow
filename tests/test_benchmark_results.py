"""Check row authorization, completion checks, and HTTP retries."""
import hashlib
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import benchmark_results as results
from launch import container_overrides


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.key = 'a' * 43
        self.row = dict(run_id=hashlib.sha256(self.key.encode()).hexdigest(),
                        race='orc', **{'class': 'warrior'}, target_level=2)
        self.db = Mock()
        self.db.get_item.side_effect = lambda Key, **_: {'Item': self.row} if Key == {'run_id': self.row['run_id']} else {}
        self.payload = dict(started_at='2026-10-25T00:00:00Z', finished_at='2026-10-25T00:00:02Z',
                            elapsed_ms=2000, exit_code=0, harness='codex', model='gpt-6.1-sol', effort='medium',
                            characters=[dict(name='Orc', race='Orc', **{'class': 'Warrior'}, level=2)])

    def call(self, key=None, **changes):
        event = dict(requestContext={'http': {'method': 'POST'}}, headers={'authorization': 'Bearer ' + (key or self.key)},
                     body=json.dumps({**self.payload, **changes}))
        with patch.object(results, 'table', return_value=self.db):
            return results.handler(event, None)

    def test_key_cannot_write_another_row(self):
        self.assertEqual(self.call(key='b' * 43)['statusCode'], 403)
        self.db.update_item.assert_not_called()

    def test_read_access_is_separate_from_run_write_keys(self):
        event = {'requestContext': {'http': {'method': 'GET'}}, 'headers': {}}
        with patch.object(results, 'table', return_value=self.db), patch.dict(os.environ, READ_KEY_HASH=''):
            self.assertEqual(results.handler(event, None)['statusCode'], 403)
        with patch.object(results, 'table', return_value=self.db), patch.dict(
                os.environ, READ_KEY_HASH=hashlib.sha256(b'private-reader').hexdigest()):
            event['headers']['authorization'] = 'Bearer ' + self.key
            self.assertEqual(results.handler(event, None)['statusCode'], 403)
        self.db.scan.assert_not_called()

    def test_completed_upload_appears_without_a_publishing_step(self):
        self.assertEqual(self.call()['statusCode'], 200)
        result = self.db.update_item.call_args.kwargs['ExpressionAttributeValues'][':result']
        self.assertEqual(result['timing'], 'agent')
        row = {**self.row, 'result': {**result, 'elapsed_ms': Decimal('2000')}}
        historical = {**row, 'run_id': 'historical:job', 'target_level': None,
                      'result': {**result, 'status': 'unverified', 'timing': 'job'}}
        self.db.scan.side_effect = [
            {'Items': [self.row, {**row, 'result': {**result, 'status': 'failed'}}],
             'LastEvaluatedKey': {'run_id': 'cursor'}}, {'Items': [row, historical]},
        ]
        event = {'requestContext': {'http': {'method': 'GET'}}, 'headers': {'authorization': 'Bearer private-reader'}}
        with patch.object(results, 'table', return_value=self.db), patch.dict(
                os.environ, READ_KEY_HASH=hashlib.sha256(b'private-reader').hexdigest()):
            reply = results.handler(event, None)
        self.assertEqual(reply['statusCode'], 200)
        self.assertEqual(json.loads(reply['body']), {'rows': [row, historical], 'live': []})
        self.assertEqual(self.db.scan.call_args.kwargs['ExclusiveStartKey'], {'run_id': 'cursor'})
        self.assertEqual(self.db.scan.call_args.kwargs['ProjectionExpression'],
                         'run_id,race,#class,target_level,prompt,#result,#stream')

    def test_success_requires_saved_matching_character_and_target_level(self):
        for changes in ({}, {'exit_code': 1}, {'characters': []},
                        {'characters': [{**self.payload['characters'][0], 'race': 'Human'}]},
                        {'characters': [{**self.payload['characters'][0], 'level': 1}]}):
            with self.subTest(changes=changes):
                reply = self.call(target_level=1, **changes)
                self.assertEqual(reply['statusCode'], 200)
                self.assertEqual(json.loads(reply['body'])['status'], 'failed' if changes else 'succeeded')

    def test_bad_data_does_not_write(self):
        for changes in ({'elapsed_ms': -1}, {'elapsed_ms': True}, {'finished_at': 'yesterday'},
                        {'characters': [{'name': 'x', 'level': '2'}]}):
            with self.subTest(changes=changes):
                self.assertEqual(self.call(**changes)['statusCode'], 400)
        self.db.update_item.assert_not_called()

    def test_only_transient_upload_errors_are_retried(self):
        for code, calls in ((403, 1), (429, 3), (503, 3)):
            with self.subTest(code=code), patch.object(results, 'urlopen', side_effect=HTTPError(
                    'https://example.test', code, 'error', {}, None)) as request, patch.object(results.time, 'sleep'):
                with self.assertRaises(HTTPError):
                    results.post('https://example.test', self.key, self.payload)
                self.assertEqual(request.call_count, calls)

    def test_config_fixes_prompt_and_passes_key_separately(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / 'run.json'
            path.write_text(json.dumps(dict(url='https://example.test', key=self.key, prompt='Reach level 2.')))
            overrides = container_overrides(None, benchmark=path)
            self.assertEqual(overrides['command'], ['run-wow-agent', 'Reach level 2.'])
            self.assertNotIn(self.key, overrides['command'])
            with self.assertRaises(ValueError):
                container_overrides('Different task', benchmark=path)

    def stream_payload(self):
        return dict(kind='stream', channel='tuckerbullmorgan', job_id='12345678-1234-1234-1234-123456789012',
                    instance_id='i-0123456789abcdef0', character='Gorvash', harness='codex', model='gpt-6.1-sol', effort='medium')

    def test_stream_key_updates_only_its_run_without_fabricating_a_result(self):
        self.assertEqual(self.call(**self.stream_payload())['statusCode'], 200)
        update = self.db.update_item.call_args.kwargs
        self.assertEqual(update['Key'], {'run_id': self.row['run_id']})
        self.assertEqual(update['UpdateExpression'], 'SET #stream = :stream')
        self.assertIn('#stream.job_id = :job', update['ConditionExpression'])
        self.assertEqual(update['ExpressionAttributeValues'][':stream']['url'], 'https://www.twitch.tv/tuckerbullmorgan')

    def test_stream_rejects_injection_wrong_key_and_finalized_runs(self):
        self.assertEqual(self.call(key='b' * 43, **self.stream_payload())['statusCode'], 403)
        for field, value in [('channel', 'example.com/x'), ('character', 'Gorvash{ENTER}'), ('instance_id', '../secret')]:
            self.assertEqual(self.call(**{**self.stream_payload(), field: value})['statusCode'], 400)
        self.row['result'] = {'status': 'succeeded'}
        self.assertEqual(self.call(**self.stream_payload())['statusCode'], 409)
        self.db.update_item.assert_not_called()

    def test_live_link_requires_exact_running_job_fresh_real_output(self):
        stream = self.stream_payload()
        row = {**self.row, 'stream': stream}
        batch, s3, boto = Mock(), Mock(), Mock()
        boto.client.side_effect = lambda service, **_: batch if service == 'batch' else s3
        status = dict(job_id=stream['job_id'], channel=stream['channel'], mode='live', state='active',
                      updated_at='2026-10-26T22:00:00Z', output=dict(outputActive=True, outputBytes=100, outputReconnecting=False))
        now = results.datetime.fromisoformat(status['updated_at'].replace('Z', '+00:00')).timestamp() + 10
        batch.describe_jobs.return_value = {'jobs': [{'jobId': stream['job_id'], 'status': 'RUNNING'}]}
        with patch.dict(sys.modules, {'boto3': boto, 'botocore.config': Mock()}), \
                patch.dict(os.environ, RESULTS_BUCKET='bucket'), patch.object(results.time, 'time', return_value=now):
            def check(value):
                s3.get_object.return_value = {'Body': Mock(read=lambda: json.dumps(value))}
                return results.active_streams([row])
            self.assertEqual(check(status), [row])
            for change in ({'job_id': 'different'}, {'mode': 'test'}, {'state': 'stopped'},
                           {'updated_at': '2026-10-26T21:58:00Z'}, {'output': {'outputActive': False}},
                           {'output': {**status['output'], 'outputReconnecting': True}}):
                self.assertEqual(check({**status, **change}), [])
            batch.describe_jobs.return_value['jobs'][0]['status'] = 'SUCCEEDED'
            self.assertEqual(check(status), [])


if __name__ == '__main__':
    unittest.main()
