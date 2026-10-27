"""Check row authorization, completion checks, and HTTP retries."""
import hashlib
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
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
        self.assertEqual(json.loads(reply['body']), {'rows': [row, historical]})
        self.assertEqual(self.db.scan.call_args.kwargs['ExclusiveStartKey'], {'run_id': 'cursor'})
        self.assertEqual(self.db.scan.call_args.kwargs['ProjectionExpression'],
                         'run_id,race,#class,target_level,prompt,#result')

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


if __name__ == '__main__':
    unittest.main()
