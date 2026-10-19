"""Offline HTTP tests: fake credentials and a loopback-only OpenRouter server."""

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'docker/codex_openrouter_cache.py'
spec = importlib.util.spec_from_file_location('cache_adapter', HELPER)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def completed(cached=0):
    item = {'type': 'message', 'id': 'msg_mock', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'Mock success.', 'annotations': []}]}
    return {'type': 'response.completed', 'response': {
        'id': 'resp_mock', 'object': 'response', 'created_at': 1, 'status': 'completed',
        'output': [item], 'usage': {
            'input_tokens': 6000, 'output_tokens': 4, 'total_tokens': 6004,
            'input_tokens_details': {'cached_tokens': cached, 'cache_write_tokens': 6000 if not cached else 0},
            'output_tokens_details': {'reasoning_tokens': 0},
        },
    }}


class FakeRouter(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"models":[],"data":[]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append({'body': body, 'headers': dict(self.headers), 'path': self.path})
        final = completed(self.server.cached)
        final['type'] = getattr(self.server, 'event_type', 'response.completed')
        self.send_response(200)
        if getattr(self.server, 'json_response', False):
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(final['response']).encode())
            return
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        events = [
            {'type': 'response.created', 'response': {'id': 'resp_mock', 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': {**final['response']['output'][0], 'content': [], 'status': 'in_progress'}},
            {'type': 'response.output_text.delta', 'item_id': 'msg_mock', 'output_index': 0,
             'content_index': 0, 'delta': 'Mock success.'},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': final['response']['output'][0]},
            final,
        ]
        self.wfile.write(b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='wow-cache-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.usage_file = self.root / 'cache-usage.jsonl'
        self.router = ThreadingHTTPServer(('127.0.0.1', 0), FakeRouter)
        self.router.requests = []
        self.router.cached = 5000
        self.add_server(self.router)
        self.proxy = adapter.CacheProxy(self.usage_file, self.url(self.router))
        self.add_server(self.proxy)

    @staticmethod
    def url(server):
        return f'http://127.0.0.1:{server.server_port}'

    def add_server(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def post(self, model='anthropic/claude-sonnet-5'):
        request = Request(self.url(self.proxy) + '/api/v1/responses', data=json.dumps({
            'model': model, 'input': 'Local test prompt', 'stream': True,
            'prompt_cache_key': 'local-test-session',
        }).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer fake-key'})
        with contextlib.redirect_stdout(io.StringIO()), urlopen(request, timeout=5) as response:
            return response.read()

    def test_anthropic_directive_auth_and_stream_are_forwarded(self):
        data = self.post()
        captured = self.router.requests[0]
        self.assertEqual(captured['body']['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(captured['body']['prompt_cache_key'], 'local-test-session')
        self.assertEqual(captured['headers']['Authorization'], 'Bearer fake-key')
        self.assertEqual(captured['path'], '/api/v1/responses')
        self.assertIn(b'"type": "response.completed"', data)
        usage = json.loads(self.usage_file.read_text())
        self.assertEqual(usage['cached_tokens'], 5000)
        self.assertNotIn('fake-key', self.usage_file.read_text())
        self.assertNotIn('Local test prompt', self.usage_file.read_text())

    def test_openai_request_is_unchanged(self):
        self.post('openai/gpt-6.1-sol')
        self.assertNotIn('cache_control', self.router.requests[0]['body'])
        self.assertFalse(self.usage_file.exists())

    def test_json_response_usage_is_recorded(self):
        self.router.json_response = True
        response = json.loads(self.post())
        self.assertEqual(response['usage']['input_tokens'], 6000)
        self.assertEqual(json.loads(self.usage_file.read_text())['cached_tokens'], 5000)

    def test_output_limit_does_not_bypass_the_cache_guard(self):
        self.router.event_type = 'response.incomplete'
        self.router.cached = 0
        for _ in range(3):
            self.post()
        self.assertTrue(self.proxy.failed.is_set())

    def test_three_large_misses_block_further_upstream_requests(self):
        self.router.cached = 0
        for _ in range(3):
            self.post()
        self.assertTrue(self.proxy.failed.is_set())
        with self.assertRaises(HTTPError) as error:
            self.post()
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(len(self.router.requests), 3)

    def test_a_cache_hit_resets_consecutive_misses(self):
        self.router.cached = 0
        self.post()
        self.post()
        self.router.cached = 5000
        self.post()
        self.router.cached = 0
        self.post()
        self.post()
        self.assertFalse(self.proxy.failed.is_set())

    def test_small_prompts_do_not_trigger_the_large_prompt_guard(self):
        event = completed()
        event['response']['usage']['input_tokens'] = 500
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(4):
                self.proxy.record_usage(event, 'short-session')
        self.assertFalse(self.proxy.failed.is_set())

    def test_missing_usage_stops_after_three_unverifiable_responses(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                self.proxy.record_usage({'response': {}}, 'unknown-session')
        self.assertTrue(self.proxy.failed.is_set())

    def test_malformed_usage_cannot_be_mistaken_for_a_cache_hit(self):
        event = completed()
        event['response']['usage']['input_tokens_details']['cached_tokens'] = '0'
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                self.proxy.record_usage(event, 'bad-accounting')
        self.assertTrue(self.proxy.failed.is_set())

    def test_unwritable_usage_log_stops_spending(self):
        self.proxy.usage_file = self.root  # A directory cannot be opened as a log.
        self.proxy.record_usage(completed(5000), 'local-test-session')
        self.assertTrue(self.proxy.failed.is_set())

    def test_guard_terminates_codex_and_returns_failure(self):
        class Child:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout):
                return self.returncode

        self.proxy.failed.set()
        self.proxy.failure_reason = 'local test'
        child = Child()
        with patch.object(adapter, 'CacheProxy', return_value=self.proxy), \
                patch.object(adapter.threading, 'Thread'), \
                patch.object(adapter.subprocess, 'Popen', return_value=child) as popen, \
                patch.object(adapter.signal, 'signal'), \
                patch.object(sys, 'argv', ['adapter', '--usage-file', str(self.usage_file),
                                         'codex', '--config', 'model_provider="openrouter"', 'exec', '-']), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(adapter.main(), 70)
        self.assertEqual(child.returncode, -15)
        command = popen.call_args[0][0]
        config = f'model_providers.openrouter.base_url="{self.url(self.proxy)}/api/v1"'
        self.assertIn(config, command)
        self.assertLess(command.index(config), command.index('exec'))

    @unittest.skipUnless(os.environ.get('WOW_TEST_CODEX_BINARY'), 'Optional local Codex binary capture')
    def test_real_codex_request_uses_the_directive_without_any_api_credentials(self):
        binary = os.environ['WOW_TEST_CODEX_BINARY']
        environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(self.root),
                       'CODEX_HOME': str(self.root / 'codex-home'), 'OPENROUTER_API_KEY': 'fake-key'}
        Path(environment['CODEX_HOME']).mkdir()
        # Run the real wrapper as a child, replacing only its upstream hostname.
        # The original Codex URL also points locally, so no branch can bill an API.
        bootstrap = self.root / 'local-wrapper.py'
        bootstrap.write_text(
            'import importlib.util,sys\n'
            f'spec=importlib.util.spec_from_file_location("cache_adapter", {str(HELPER)!r})\n'
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
            'original=module.CacheProxy\n'
            f'module.CacheProxy=lambda path: original(path, upstream={self.url(self.router)!r})\n'
            'sys.exit(module.main())\n'
        )
        environment['PYTHONPATH'] = os.pathsep.join(sys.path)
        for expected in [None, {'type': 'ephemeral'}]:
            with self.subTest(adapter_enabled=expected is not None):
                provider = ('model_providers.openrouter={name="Local mock",'
                            f'base_url="{self.url(self.router)}/api/v1",wire_api="responses",'
                            'auth={command="printenv",args=["OPENROUTER_API_KEY"]}}')
                command = [
                    binary, '--config', 'model_provider="openrouter"', '--config', provider,
                    '--model', 'anthropic/claude-sonnet-5', 'exec', '--skip-git-repo-check',
                    '--cd', str(self.root), 'Reply briefly; do not use tools.',
                ]
                if expected is not None:
                    command = [sys.executable, str(bootstrap), '--usage-file',
                               str(self.root / 'wrapper-usage.jsonl'), *command]
                result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                self.assertEqual(self.router.requests[-1]['body'].get('cache_control'), expected)
        self.assertEqual(len(self.router.requests), 2)
        self.assertEqual(json.loads((self.root / 'wrapper-usage.jsonl').read_text())['cached_tokens'], 5000)


if __name__ == '__main__':
    unittest.main()
