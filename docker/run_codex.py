#!/opt/venv/bin/python3
"""Run Codex with a saved ChatGPT login and preserve its logs and final report."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


def prepare_login(home):
    raw = os.environ.pop('CODEX_AUTH_JSON', '')
    try:
        auth = json.loads(raw)
        tokens = auth.get('tokens', {})
        if auth.get('auth_mode') != 'chatgpt' or not all(
                tokens.get(key) for key in ('access_token', 'refresh_token', 'id_token')):
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise ValueError('A saved ChatGPT account login is required; API billing is disabled.') from None
    os.umask(0o077)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    auth.pop('OPENAI_API_KEY', None)
    path = home / 'auth.json'
    path.write_text(json.dumps(auth))
    path.chmod(0o600)
    (home / 'config.toml').write_text(
        'cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n')
    for name in ('CODEX_API_KEY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN'):
        os.environ.pop(name, None)
    os.environ.update(CODEX_HOME=str(home), CODEX_MODEL_PROVIDER='openai')
    return [value for value in tokens.values() if isinstance(value, str) and value]


def run(prompt=None, workspace=Path('/workspace'), default_task=Path('/opt/orc-quest-task.txt'),
        home=Path('/root/.codex')):
    model = os.environ.get('CODEX_MODEL', 'gpt-6.1-sol')
    effort = os.environ.get('CODEX_REASONING_EFFORT', 'medium')
    if not re.fullmatch(r'gpt-[A-Za-z0-9.-]+', model):
        raise ValueError('CODEX_MODEL must be a native GPT model ID.')
    if effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('CODEX_REASONING_EFFORT must be low, medium, high, xhigh, or max.')
    if prompt is not None and not prompt.strip():
        raise ValueError('Prompt must not be blank.')
    secrets = prepare_login(home)
    for name in ('.cache', '.docker', 'go'):
        (home.parent / name).mkdir(parents=True, exist_ok=True)
    logs = workspace / 'logs/agent'
    logs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%dT%H%M%SZ-', time.gmtime()), dir=logs))
    task = run_dir / 'task.txt'
    task.write_bytes(prompt.encode() if prompt is not None else default_task.read_bytes())
    command = ['codex', '--config', 'cli_auth_credentials_store="file"',
               '--config', 'forced_login_method="chatgpt"', '--config', 'model_provider="openai"',
               '--model', model, '--config', f'model_reasoning_effort="{effort}"',
               '--ask-for-approval', 'never', '--config', 'sandbox_workspace_write.network_access=true',
               'exec', '--sandbox', 'workspace-write', '--cd', str(workspace), '--skip-git-repo-check',
               '--add-dir', '/run', '--add-dir', str(home.parent / 'go'),
               '--add-dir', str(home.parent / '.cache'), '--add-dir', str(home.parent / '.docker'),
               '--color', 'never', '--output-last-message', str(run_dir / 'final-report.txt'), '-']
    child = None
    interrupted = 0

    def stop(number, _frame):
        nonlocal interrupted
        interrupted = number
        if child is not None and child.poll() is None:
            try:
                child.send_signal(number)
            except ProcessLookupError:
                pass

    previous = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        with (run_dir / 'agent.log').open('w', buffering=1) as log, task.open('rb') as stdin:
            def emit(text):
                for secret in secrets:
                    text = text.replace(secret, '[REDACTED]')
                log.write(text)
                print(text, end='', flush=True)

            emit(f'Codex authentication: ChatGPT account; model: {model}; effort: {effort}\n')
            emit(f'Run log: {run_dir}/agent.log\n')
            child = subprocess.Popen(command, cwd=workspace, stdin=stdin,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1)
            if interrupted:
                stop(interrupted, None)
            for line in child.stdout:
                emit(line)
            child.stdout.close()
            status = child.wait()
            status = 128 - status if status < 0 else status
            status = 128 + interrupted if interrupted else status
            (run_dir / 'agent-exit-code.txt').write_text(str(status) + '\n')
            emit(f'Codex finished with status {status}. Report: {run_dir}/final-report.txt\n')
            return status
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if child is not None and child.stdout is not None:
            child.stdout.close()
        for number, handler in previous.items():
            signal.signal(number, handler)


def main():
    if len(sys.argv) > 2:
        print('Usage: run-wow-codex ["prompt"] — quote the prompt as one argument.', file=sys.stderr)
        return 2
    try:
        return run(sys.argv[1] if len(sys.argv) == 2 else None)
    except (OSError, ValueError) as error:
        print(f'Codex launcher failed: {error}', file=sys.stderr)
        return 1 if isinstance(error, OSError) else 2


if __name__ == '__main__':
    sys.exit(main())
