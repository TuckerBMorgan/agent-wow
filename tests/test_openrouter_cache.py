"""Offline HTTP tests: fake credentials and a loopback-only OpenRouter server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
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
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        self.server.requests.append({'body': body, 'raw': raw, 'headers': dict(self.headers), 'path': self.path})
        status = getattr(self.server, 'status', 200)
        if getattr(self.server, 'errors_left', 0):
            self.server.errors_left -= 1
            status = 429
        self.send_response(status)
        if status != 200:
            self.send_header('Content-Type', 'application/json')
            self.send_header('Retry-After', '1')
            self.send_header('X-RateLimit-Remaining', '0')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Local mock rate limit"}}')
            return
        final = completed(self.server.cached)
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
        for index, event in enumerate(events):
            self.wfile.write(b'data: ' + json.dumps(event).encode() + b'\n\n')
            self.wfile.flush()
            if index == 0 and hasattr(self.server, 'stream_pause'):
                self.server.stream_pause.wait(5)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='wow-cache-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.router = ThreadingHTTPServer(('127.0.0.1', 0), FakeRouter)
        self.router.requests = []
        self.router.cached = 5000
        self.add_server(self.router)
        self.proxy = adapter.CacheProxy(self.url(self.router))
        self.add_server(self.proxy)

    @staticmethod
    def url(server):
        return f'http://127.0.0.1:{server.server_port}'

    def add_server(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def request(self, model='anthropic/claude-opus-5.5'):
        return Request(self.url(self.proxy) + '/api/v1/responses', data=json.dumps({
            'model': model, 'input': 'Local test prompt', 'stream': True,
            'prompt_cache_key': 'local-test-session', 'reasoning': {'effort': 'high'},
        }, indent=2).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer fake-key'})

    def test_directive_is_added_without_changing_prompt_auth_model_or_effort(self):
        request = self.request()
        with urlopen(request, timeout=5) as response:
            data = response.read()
        captured = self.router.requests[0]
        self.assertEqual(captured['body'], {**json.loads(request.data), 'cache_control': {'type': 'ephemeral'}})
        self.assertEqual(captured['headers']['Authorization'], 'Bearer fake-key')
        self.assertEqual(captured['path'], '/api/v1/responses')
        self.assertIn(b'"type": "response.completed"', data)

    def test_other_models_request_bytes_are_unchanged(self):
        request = self.request('openai/gpt-6.1-sol')
        with urlopen(request, timeout=5) as response:
            response.read()
        self.assertEqual(self.router.requests[0]['raw'], request.data)

    def test_model_discovery_is_forwarded(self):
        with urlopen(self.url(self.proxy) + '/api/v1/models?client_version=test', timeout=5) as response:
            self.assertEqual(json.load(response), {'models': [], 'data': []})

    def test_json_response_and_usage_are_relayed_without_parsing(self):
        self.router.json_response = True
        with urlopen(self.request(), timeout=5) as response:
            self.assertEqual(response.read(), json.dumps(completed(5000)['response']).encode())

    def test_rate_limit_status_headers_and_body_survive(self):
        self.router.status = 429
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.request(), timeout=5)
        with caught.exception as response:
            self.assertEqual(response.code, 429)
            self.assertEqual(response.headers['Retry-After'], '1')
            self.assertEqual(response.headers['X-RateLimit-Remaining'], '0')
            self.assertEqual(response.read(), b'{"error":{"message":"Local mock rate limit"}}')
        self.assertEqual(len(self.router.requests), 1, 'The proxy must not retry requests itself')

    def test_cache_misses_do_not_stop_the_agent(self):
        self.router.cached = 0
        for _ in range(4):
            with urlopen(self.request(), timeout=5) as response:
                self.assertIn(b'"type": "response.completed"', response.read())
        self.assertEqual(len(self.router.requests), 4)

    def test_stream_is_forwarded_before_upstream_finishes(self):
        self.router.stream_pause = threading.Event()
        try:
            with urlopen(self.request(), timeout=2) as response:
                self.assertIn(b'response.created', response.readline())
                self.assertFalse(self.router.stream_pause.is_set())
                self.router.stream_pause.set()
                response.read()
        finally:
            self.router.stream_pause.set()

    def test_wrapper_preserves_child_failure_and_signal_exit_codes(self):
        for program, expected in [('import sys; sys.exit(7)', 7),
                                  ('import os,signal; os.kill(os.getpid(), signal.SIGTERM)', 143)]:
            with self.subTest(expected=expected):
                result = subprocess.run([sys.executable, str(HELPER), sys.executable, '-c', program, 'exec'],
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr.decode())

    @unittest.skipUnless(os.environ.get('WOW_TEST_CODEX_BINARY'), 'Optional local Codex binary capture')
    def test_real_codex_caching_effort_and_retries_without_api_credentials(self):
        binary = os.environ['WOW_TEST_CODEX_BINARY']
        environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(self.root),
                       'CODEX_HOME': str(self.root / 'codex-home'), 'OPENROUTER_API_KEY': 'fake-key'}
        Path(environment['CODEX_HOME']).mkdir()
        # Both the original Codex URL and the proxy's upstream point to loopback.
        bootstrap = self.root / 'local-wrapper.py'
        bootstrap.write_text(
            'import importlib.util,sys\n'
            f'spec=importlib.util.spec_from_file_location("cache_adapter", {str(HELPER)!r})\n'
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
            'original=module.CacheProxy\n'
            f'module.CacheProxy=lambda: original(upstream={self.url(self.router)!r})\n'
            'sys.exit(module.main())\n'
        )
        environment['PYTHONPATH'] = os.pathsep.join(sys.path)
        for wrapped, errors in [(False, 0), (True, 0), (True, 1)]:
            with self.subTest(wrapped=wrapped, rate_limits=errors):
                self.router.errors_left = errors
                previous = len(self.router.requests)
                provider = ('model_providers.openrouter={name="Local mock",'
                            f'base_url="{self.url(self.router)}/api/v1",wire_api="responses",'
                            'auth={command="printenv",args=["OPENROUTER_API_KEY"]}}')
                command = [binary, '--config', 'model_provider="openrouter"', '--config', provider,
                           '--config', 'model_reasoning_effort="high"', '--model', 'anthropic/claude-opus-5.5',
                           'exec', '--skip-git-repo-check', '--cd', str(self.root), 'Reply briefly; do not use tools.']
                if wrapped:
                    command = [sys.executable, str(bootstrap), *command]
                result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                captured = self.router.requests[previous:]
                self.assertEqual(len(captured), 1 + errors)
                for request in captured:
                    self.assertEqual(request['body'].get('cache_control'), {'type': 'ephemeral'} if wrapped else None)
                    self.assertEqual(request['body']['reasoning']['effort'], 'high')
                self.assertIn('Mock success.', result.stdout)


if __name__ == '__main__':
    unittest.main()
