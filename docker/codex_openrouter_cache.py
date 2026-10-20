#!/opt/venv/bin/python3
"""Inject Anthropic's cache directive into Codex's OpenRouter requests."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import HTTPError


HOP_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade',
}


def forward_headers(headers):
    blocked = HOP_HEADERS | {name.strip().lower() for name in headers.get('Connection', '').split(',')}
    return {name: value for name, value in headers.items() if name.lower() not in blocked}


class CacheProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream='https://openrouter.ai'):
        self.upstream = upstream
        super().__init__(('127.0.0.1', 0), CacheHandler)


class CacheHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Never log credentials, prompts, or response text.

    def forward(self):
        if not self.path.startswith('/api/v1/'):
            self.send_error(404, 'Unsupported API path.')
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if self.command == 'POST' and urlsplit(self.path).path == '/api/v1/responses':
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError
                if str(payload.get('model', '')).startswith('anthropic/'):
                    payload['cache_control'] = {'type': 'ephemeral'}
                    body = json.dumps(payload).encode()
            except (ValueError, UnicodeError):
                self.send_error(400, 'Expected a JSON Responses request.')
                return
        headers = {name: value for name, value in forward_headers(self.headers).items()
                   if name.lower() not in {'host', 'content-length', 'accept-encoding'}}
        headers['Accept-Encoding'] = 'identity'
        started = False
        try:
            # Codex handles retries. Relay status, retry headers, and response bytes unchanged.
            with requests.request(self.command, self.server.upstream + self.path, data=body or None,
                                  headers=headers, stream=True, timeout=(10, 360), allow_redirects=False) as response:
                self.send_response(response.status_code)
                for name, value in forward_headers(response.headers).items():
                    self.send_header(name, value)
                self.send_header('Connection', 'close')
                self.end_headers()
                started = True
                while chunk := response.raw.read1(65536, decode_content=False):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (requests.RequestException, HTTPError):
            if not started:
                self.send_error(502, 'OpenRouter connection failed.')
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    do_GET = forward
    do_POST = forward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if 'exec' not in args.command:
        parser.error('Expected a codex exec command.')
    with CacheProxy() as proxy:
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        index = args.command.index('exec')
        command = [*args.command[:index],
                   '--config', f'model_providers.openrouter.base_url="http://127.0.0.1:{proxy.server_port}/api/v1"',
                   '--config', 'model_providers.openrouter.supports_websockets=false',
                   *args.command[index:]]
        print('Anthropic prompt caching enabled.', flush=True)
        child = subprocess.Popen(command)

        def stop(signum, _frame):
            if child.poll() is None:
                child.send_signal(signum)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            code = child.wait()
            return 128 - code if code < 0 else code
        finally:
            proxy.shutdown()


if __name__ == '__main__':
    sys.exit(main())
