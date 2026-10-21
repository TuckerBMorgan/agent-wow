#!/opt/venv/bin/python3
"""Log OpenRouter errors, supply retry advice, and enable Anthropic caching."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import HTTPError


HOP_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade',
}
ERROR_BODY_LIMIT = 16384
ERROR_HEADERS = {'retry-after', 'x-request-id', 'cf-ray', 'content-type', 'content-encoding', 'date'}
SECRET_FIELDS = re.compile(r'password|dbpw|db_pass|api[_ -]?key|secret|authorization|cookie|access[_ -]?token|refresh[_ -]?token', re.I)
SECRET_TEXT = re.compile(
    r'\b(?:Bearer|Basic)\s+[^\s"\'<>]+|\bsk-[A-Za-z0-9_-]+'
    r'|(?:password|DBPW|DB_PASS|api[_ -]?key|secret|access[_ -]?token|refresh[_ -]?token)'
    r'["\']?\s*[:=]\s*(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;<>}]+)', re.I,
)


def retry_delay(response):
    if response.status_code != 429 or 'Retry-After' in response.headers:
        return None
    try:
        # OpenRouter's reset timestamp is Unix milliseconds. Keep Codex's retry
        # budget, but supply the header it needs to retry this otherwise fatal 429.
        reset = int(response.headers['X-RateLimit-Reset'])
        if reset <= 0:
            return None
        return math.ceil(max(0, reset / 1000 - time.time())) + random.randint(1, 3)
    except (KeyError, ValueError, OverflowError):
        return None


def log_http_error(response, preview, *, path, model, request_bytes, elapsed, secrets=(), retry_after=None):
    # Only response errors are sampled. Never dump request headers or prompt bodies.
    def redact(value):
        if isinstance(value, dict):
            return {key: '[REDACTED]' if SECRET_FIELDS.search(key) else redact(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(filter(None, secrets), key=len, reverse=True):
                value = value.replace(secret, '[REDACTED]')
            return SECRET_TEXT.sub('[REDACTED]', value)
        return value

    text = preview[:ERROR_BODY_LIMIT].decode('utf-8', errors='replace')
    try:
        body = json.loads(text)
    except ValueError:
        body = text
    record = {
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'status': response.status_code, 'path': urlsplit(path).path, 'model': model,
        'request_bytes': request_bytes, 'elapsed_seconds': round(elapsed, 3),
        'headers': {name: value for name, value in response.headers.items()
                    if name.lower() in ERROR_HEADERS or name.lower().startswith('x-ratelimit-')},
        'body': body, 'body_truncated': len(preview) > ERROR_BODY_LIMIT,
        'retry_after_added_seconds': retry_after,
    }
    print('OpenRouter HTTP error: ' + json.dumps(redact(record)), file=sys.stderr, flush=True)


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
        pass  # Disable the HTTP server's ordinary access logging.

    def forward(self):
        if not self.path.startswith('/api/v1/'):
            self.send_error(404, 'Unsupported API path.')
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        model = None
        if self.command == 'POST' and urlsplit(self.path).path == '/api/v1/responses':
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError
                model = payload.get('model')
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
        requested_at = time.monotonic()
        secrets = [os.environ.get(name, '') for name in
                   ('CODEX_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY')]
        for name, value in self.headers.items():
            if SECRET_FIELDS.search(name):
                secrets.extend([value, value.removeprefix('Bearer ').removeprefix('Basic ')])
        try:
            # Codex owns retries; preserve upstream advice and fill only its missing header.
            with requests.request(self.command, self.server.upstream + self.path, data=body or None,
                                  headers=headers, stream=True, timeout=(10, 360), allow_redirects=False) as response:
                retry_after = retry_delay(response)
                self.send_response(response.status_code)
                for name, value in forward_headers(response.headers).items():
                    self.send_header(name, value)
                if retry_after is not None:
                    self.send_header('Retry-After', str(retry_after))
                self.send_header('Connection', 'close')
                self.end_headers()
                started = True
                preview = bytearray()
                try:
                    while chunk := response.raw.read1(65536, decode_content=False):
                        if response.status_code >= 400:
                            preview.extend(chunk[:max(0, ERROR_BODY_LIMIT + 1 - len(preview))])
                        self.wfile.write(chunk)
                        self.wfile.flush()
                finally:
                    if response.status_code >= 400:
                        log_http_error(response, preview, path=self.path, model=model,
                                       request_bytes=len(body), elapsed=time.monotonic() - requested_at,
                                       secrets=secrets, retry_after=retry_after)
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
        print('OpenRouter error diagnostics enabled; Anthropic requests also receive the cache directive.', flush=True)
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
