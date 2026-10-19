#!/opt/venv/bin/python3
"""Add Anthropic prompt caching to Codex's OpenRouter Responses requests."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import requests


def token_count(value):
    return value if type(value) is int and value >= 0 else None


class CacheProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, usage_file, upstream='https://openrouter.ai'):
        self.upstream = upstream
        self.usage_file = Path(usage_file)
        self.failed = threading.Event()
        self.failure_reason = ''
        self.lock = threading.Lock()
        self.misses = {}
        super().__init__(('127.0.0.1', 0), CacheHandler)

    def record_usage(self, event, session):
        usage = event.get('response', {}).get('usage') or {}
        details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}
        row = {
            'input_tokens': token_count(usage.get('input_tokens', usage.get('prompt_tokens'))),
            'cached_tokens': token_count(details.get('cached_tokens')) or 0,
            'cache_write_tokens': token_count(details.get('cache_write_tokens')),
            'output_tokens': token_count(usage.get('output_tokens', usage.get('completion_tokens'))),
        }
        with self.lock:
            try:
                with self.usage_file.open('a') as log:
                    log.write(json.dumps(row) + '\n')
            except OSError:
                self.failure_reason = 'cache usage could not be saved'
                self.failed.set()
                return
            print('Anthropic cache usage: ' + json.dumps(row), flush=True)
            if row['cached_tokens']:
                self.misses[session] = 0
            elif row['input_tokens'] is None or row['input_tokens'] >= 4096:
                # Allow the first write and two more attempts, then stop spending.
                self.misses[session] = self.misses.get(session, 0) + 1
                if self.misses[session] >= 3:
                    self.failure_reason = 'three large requests without cache hits'
                    self.failed.set()


class CacheHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Never log authorization headers, prompts, or response text.

    def error(self, status, message):
        body = json.dumps({'error': {'message': message, 'type': 'cache_proxy_error'}}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def forward(self):
        if not self.path.startswith('/api/v1/'):
            self.error(404, 'Unsupported API path.')
            return
        if self.server.failed.is_set():
            self.error(400, 'Anthropic cache verification failed; this run is stopped.')
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        cache_request = False
        session = 'default'
        if self.command == 'POST' and urlsplit(self.path).path == '/api/v1/responses':
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                self.error(400, 'Expected an uncompressed JSON Responses request.')
                return
            if not isinstance(payload, dict):
                self.error(400, 'Expected a JSON object.')
                return
            cache_request = str(payload.get('model', '')).startswith('anthropic/')
            if cache_request:
                # OpenRouter advances this cache breakpoint as history grows.
                # Five-minute caching costs less to write than the one-hour TTL.
                payload['cache_control'] = {'type': 'ephemeral'}
                session = (payload.get('session_id') or payload.get('prompt_cache_key')
                           or self.headers.get('x-session-id') or 'default')
                body = json.dumps(payload).encode()
        headers = {name: value for name, value in self.headers.items() if name.lower() not in {
            'host', 'content-length', 'connection', 'transfer-encoding', 'accept-encoding',
        }}
        headers['Accept-Encoding'] = 'identity'
        # Do not retry paid requests here; Codex already handles transport retries.
        started = False
        try:
            with requests.request(
                self.command, self.server.upstream + self.path, data=body or None,
                headers=headers, stream=True, timeout=(10, 360), allow_redirects=False,
            ) as response:
                self.send_response(response.status_code)
                self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                self.end_headers()
                started = True
                is_stream = 'text/event-stream' in response.headers.get('Content-Type', '')
                if not is_stream:
                    if cache_request and response.status_code == 200:
                        data = response.content
                        self.server.record_usage({'response': json.loads(data)}, session)
                        self.wfile.write(data)
                        return
                    for chunk in response.iter_content(chunk_size=8192):
                        self.wfile.write(chunk)
                    return
                for line in response.iter_lines():
                    if cache_request and line.startswith(b'data:'):
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            event = {}
                        if event.get('type') in ('response.completed', 'response.incomplete', 'response.failed'):
                            self.server.record_usage(event, session)
                    self.wfile.write(line + b'\n')
                    self.wfile.flush()
        except requests.RequestException:
            # No exception text: it may contain request metadata.
            self.close_connection = True
            print('OpenRouter cache adapter: upstream connection failed.', flush=True)
            if not started:
                self.error(502, 'OpenRouter connection failed.')
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    do_GET = forward
    do_POST = forward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--usage-file', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if 'exec' not in args.command:
        parser.error('Expected a codex exec command.')
    with CacheProxy(args.usage_file) as proxy:
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        index = args.command.index('exec')
        command = [*args.command[:index],
                   '--config', f'model_providers.openrouter.base_url="http://127.0.0.1:{proxy.server_port}/api/v1"',
                   '--config', 'model_providers.openrouter.supports_websockets=false',
                   *args.command[index:]]
        print('Anthropic prompt caching enabled; stopping after three consecutive large cache misses.', flush=True)
        child = subprocess.Popen(command)

        def stop(signum, _frame):
            if child.poll() is None:
                child.send_signal(signum)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while child.poll() is None:
                if proxy.failed.is_set():
                    print(f'Anthropic cache verification failed: {proxy.failure_reason}. Stopping this run.', flush=True)
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    break
                time.sleep(0.1)
            return 70 if proxy.failed.is_set() else (128 - child.returncode if child.returncode < 0 else child.returncode)
        finally:
            proxy.shutdown()


if __name__ == '__main__':
    sys.exit(main())
