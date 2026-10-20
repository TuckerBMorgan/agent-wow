#!/opt/venv/bin/python3
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

import boto3
from botocore.config import Config


class Results:
    def __init__(self, client, bucket, job_id, workspace=Path('/workspace')):
        self.client = client
        self.bucket = bucket
        self.prefix = f'runs/{job_id}'
        self.workspace = workspace
        self.secrets = sorted({
            value.encode() for name in ('CODEX_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY')
            if (value := os.environ.get(name))
        }, key=len, reverse=True)
        self.uploaded = {}

    def put(self, key, data):
        for secret in self.secrets:
            data = data.replace(secret, b'[REDACTED]')
        self.client.put_object(Bucket=self.bucket, Key=f'{self.prefix}/{key}', Body=data)

    def status(self, state, exit_code=None):
        self.put('status.json', json.dumps({
            'state': state,
            'process_exit_code': exit_code,
            'provider': os.environ.get('CODEX_MODEL_PROVIDER', 'openai'),
            'model_override': os.environ.get('CODEX_MODEL'),
            'reasoning_effort_override': os.environ.get('CODEX_REASONING_EFFORT'),
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'quest_completion': 'Verify server-confirmed turn-ins in journal; exit 0 alone is not proof.',
        }).encode())

    def snapshot(self, deadline=lambda: float('inf')):
        ok = True
        for relative in ('journal', 'logs/agent'):
            root = self.workspace / relative
            if root.resolve() != self.workspace.resolve() / relative or not root.is_dir():
                continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
                for name in files:
                    if time.monotonic() >= deadline():
                        return False
                    path = Path(directory) / name
                    try:
                        # Never follow a report symlink into credentials or read a FIFO/device.
                        path.resolve().relative_to(root.resolve())
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(fd, 'rb') as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode):
                                continue
                            key = path.relative_to(self.workspace).as_posix()
                            signature = (info.st_size, info.st_mtime_ns)
                            if self.uploaded.get(key) == signature:
                                continue
                            data = stream.read()
                        self.put(key, data)
                        self.uploaded[key] = signature
                    except (FileNotFoundError, ValueError):
                        continue
                    except OSError as error:
                        if path.is_symlink():
                            continue
                        print(f'Result export failed ({type(error).__name__}).', flush=True)
                        ok = False
                    except Exception as error:
                        # SDK exception messages can contain request data; log only the type.
                        print(f'Result export failed ({type(error).__name__}).', flush=True)
                        ok = False
        return ok


def run(results, command):
    child = None
    interrupted = 0
    forwarded = False
    stop_by = float('inf')

    def forward_stop():
        nonlocal forwarded
        if interrupted and child is not None and not forwarded:
            forwarded = True
            try:
                child.send_signal(interrupted)
            except ProcessLookupError:
                pass

    def stop(signum, _frame):
        nonlocal interrupted, stop_by
        if not interrupted:
            interrupted = signum
            # Batch allows about 30 seconds before SIGKILL. Export during cleanup.
            stop_by = time.monotonic() + 20
            forward_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Fail before starting an expensive game if the result destination is inaccessible.
    results.status('starting')
    if interrupted:
        return 128 + interrupted
    environment = dict(os.environ, AGENT_WOW_BATCH_SUPERVISED='1')
    child = subprocess.Popen(command, env=environment)
    forward_stop()  # Also handle a signal received while Popen was starting.
    next_export = 0
    while True:
        exit_code = child.poll()
        if interrupted or exit_code is not None or time.monotonic() >= next_export:
            exported = results.snapshot(deadline=lambda: stop_by)
            status = 'interrupted' if interrupted else ('exited' if exit_code is not None else 'running')
            try:
                results.status(status, 128 + interrupted if interrupted else exit_code)
            except Exception as error:
                print(f'Result status export failed ({type(error).__name__}).', flush=True)
                exported = False
            if interrupted:
                try:
                    child.wait(timeout=max(0, stop_by - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
                return 128 + interrupted
            if exit_code is not None:
                # Preserve agent failure; successful execution still requires a final export.
                return (128 - exit_code if exit_code < 0 else exit_code) or (0 if exported else 74)
            next_export = time.monotonic() + 60
        time.sleep(0.5)


def main():
    bucket = os.environ.get('AWS_RESULTS_BUCKET')
    job_id = os.environ.get('AWS_BATCH_JOB_ID')
    if not bucket or not job_id:
        print('Batch requires AWS_RESULTS_BUCKET and AWS_BATCH_JOB_ID.', flush=True)
        return 1
    provider = os.environ.get('CODEX_MODEL_PROVIDER', 'openai')
    key_name, secret_name = {
        'openai': ('CODEX_API_KEY', 'CODEX_SECRET_ARN'),
        'openrouter': ('OPENROUTER_API_KEY', 'OPENROUTER_SECRET_ARN'),
    }.get(provider, (None, None))
    if key_name is None:
        print('CODEX_MODEL_PROVIDER must be openai or openrouter.', flush=True)
        return 2
    if provider == 'openrouter' and not os.environ.get('CODEX_MODEL', '').strip():
        print('OpenRouter requires CODEX_MODEL with an OpenRouter model ID.', flush=True)
        return 2
    client_config = Config(
        connect_timeout=3, read_timeout=5, retries={'total_max_attempts': 1},
    )
    # ARNs are safe job configuration; the key stays in memory and is inherited
    # only by this job's child process. Local runs can supply the key directly.
    secret_arn = os.environ.get(secret_name)
    if not os.environ.get(key_name) and secret_arn:
        secrets = boto3.client('secretsmanager', config=client_config)
        os.environ[key_name] = secrets.get_secret_value(SecretId=secret_arn)['SecretString']
    if not os.environ.get(key_name, '').strip():
        print(f'Batch requires a nonempty {key_name} for {provider}.', flush=True)
        return 1
    client = boto3.client('s3', config=client_config)
    results = Results(client, bucket, job_id)
    print(f'Results: s3://{bucket}/runs/{job_id}/', flush=True)
    return run(results, ['/usr/local/bin/start-agent-workspace', *sys.argv[1:]])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'Batch supervisor failed ({type(error).__name__}); see CloudWatch logs.', flush=True)
        sys.exit(1)
