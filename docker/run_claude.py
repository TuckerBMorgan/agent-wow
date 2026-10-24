#!/opt/venv/bin/python3
"""Run Claude Code with OpenRouter or subscription auth, preserving native events."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


DEFAULT_MODEL = 'anthropic/claude-opus-5.5'
DEFAULT_EFFORT = 'medium'


def run(prompt=None, workspace=Path('/workspace'), default_task=Path('/opt/orc-quest-task.txt')):
    auth_mode = os.environ.get('CLAUDE_AUTH_MODE', 'openrouter')
    if auth_mode not in ('openrouter', 'subscription'):
        raise ValueError('CLAUDE_AUTH_MODE must be openrouter or subscription.')
    subscription = auth_mode == 'subscription'
    model = os.environ.get('CLAUDE_MODEL', 'claude-opus-5-5' if subscription else DEFAULT_MODEL)
    effort = os.environ.get('CLAUDE_EFFORT', DEFAULT_EFFORT)
    pattern = r'claude-[A-Za-z0-9.-]+' if subscription else r'anthropic/[A-Za-z0-9_./:~+\[\]-]+'
    if not re.fullmatch(pattern, model):
        raise ValueError('CLAUDE_MODEL must match the selected authentication provider.')
    if effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('CLAUDE_EFFORT must be low, medium, high, xhigh, or max.')
    if prompt is not None and not prompt.strip():
        raise ValueError('Prompt must not be blank.')
    key_name = 'CLAUDE_CODE_OAUTH_TOKEN' if subscription else 'OPENROUTER_API_KEY'
    key = os.environ.get(key_name, '')
    if not key.strip():
        raise ValueError(f'A nonempty {key_name} is required.')

    os.umask(0o077)
    logs = workspace / 'logs/agent'
    logs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%dT%H%M%SZ-', time.gmtime()), dir=logs))
    task = run_dir / 'task.txt'
    task.write_bytes(prompt.encode() if prompt is not None else default_task.read_bytes())
    env = dict(os.environ, DISABLE_AUTOUPDATER='1',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_SUBAGENT_MODEL=model)
    # Select exactly one billing source. Never fall back to API credits.
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                 'OPENROUTER_API_KEY', 'CODEX_API_KEY', 'OPENAI_API_KEY',
                 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_PROFILE', 'ANTHROPIC_MODEL',
                 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        env.pop(name, None)
    if subscription:
        env['CLAUDE_CODE_OAUTH_TOKEN'] = key
    else:
        env.update(ANTHROPIC_BASE_URL='https://openrouter.ai/api', ANTHROPIC_AUTH_TOKEN=key,
                   ANTHROPIC_API_KEY='')
    # Keep auxiliary requests on the chosen model, with no automatic model fallback.
    for role in ('OPUS', 'SONNET', 'HAIKU', 'FABLE'):
        env[f'ANTHROPIC_DEFAULT_{role}_MODEL'] = model
    command = ['claude', '-p', '--model', model, '--effort', effort,
               '--dangerously-skip-permissions', '--output-format', 'stream-json', '--verbose']
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    child = None
    interrupted = 0
    result = None
    actual_model = None
    cli_version = None
    auth_source = None

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
        with (run_dir / 'agent.log').open('w', buffering=1) as log, \
             (run_dir / 'events.jsonl').open('w', buffering=1) as events, task.open('rb') as stdin:
            def emit(text):
                text = text.replace(key, '[REDACTED]')
                log.write(text + '\n')
                print(text, flush=True)

            emit(f'Claude Code authentication: {auth_mode}; model: {model}; effort: {effort}')
            emit(f'Run log: {run_dir}/agent.log')
            child = subprocess.Popen(command, cwd=workspace, env=env, stdin=stdin,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1)
            if interrupted:
                stop(interrupted, None)
            for line in child.stdout:
                safe = line.replace(key, '[REDACTED]')
                try:
                    event = json.loads(safe)
                except ValueError:
                    emit(safe.rstrip('\n'))
                    continue
                if not isinstance(event, dict):
                    emit(safe.rstrip('\n'))
                    continue
                events.write(json.dumps(event, ensure_ascii=False) + '\n')
                kind = event.get('type')
                if kind == 'system':
                    if event.get('subtype') == 'init':
                        actual_model = event.get('model')
                        cli_version = event.get('claude_code_version')
                        auth_source = event.get('apiKeySource')
                        emit(f'Claude session: {event.get("session_id")}; model: {actual_model}')
                        emit(f'Claude authentication source: {auth_source}')
                    elif event.get('subtype') == 'api_retry':
                        emit('API retry: ' + json.dumps({k: event.get(k) for k in
                             ('attempt', 'max_retries', 'retry_delay_ms', 'error_status', 'error')}))
                elif kind in ('assistant', 'user'):
                    message = event.get('message') or {}
                    content = message.get('content', [])
                    if isinstance(content, str):
                        emit(content)
                    else:
                        for block in content:
                            if block.get('type') == 'text':
                                emit(block.get('text', ''))
                            elif block.get('type') == 'tool_use':
                                emit(f'Tool {block.get("name")}: ' + json.dumps(block.get('input'), ensure_ascii=False))
                            elif block.get('type') == 'tool_result':
                                value = block.get('content', '')
                                emit(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                    if message.get('usage'):
                        emit('Claude usage: ' + json.dumps(message['usage']))
                elif kind == 'result':
                    result = event
                    emit(event.get('result') or json.dumps(event.get('errors', [])))
            child.stdout.close()
            exit_code = child.wait()
            exit_code = 128 - exit_code if exit_code < 0 else exit_code
            exit_code = (128 + interrupted if interrupted else exit_code) or (
                1 if result is None or result.get('is_error') else 0)
            report = (result or {}).get('result') or '\n'.join(map(str, (result or {}).get('errors', [])))
            if not report:
                report = f'Claude exited with status {exit_code} without a final result. See agent.log.'
            (run_dir / 'final-report.txt').write_text(report + '\n')
            (run_dir / 'agent-exit-code.txt').write_text(str(exit_code) + '\n')
            metadata = {**(result or {}), 'harness': 'claude-code',
                        'provider': 'anthropic' if subscription else 'openrouter',
                        'billing': 'subscription' if subscription else 'api',
                        'cost_note': 'CLI cost estimates are not a subscription charge.' if subscription else 'Use OpenRouter records for actual charges.',
                        'requested_model': model, 'actual_model': actual_model, 'effort': effort,
                        'auth_source': auth_source,
                        'claude_code_version': cli_version,
                        'started_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
                        'elapsed_seconds': round(time.monotonic() - started, 3), 'exit_code': exit_code}
            (run_dir / 'usage.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
            emit(f'Claude finished with status {exit_code}. Report: {run_dir}/final-report.txt')
            return exit_code
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
        print('Usage: run-wow-agent ["prompt"] — quote the prompt as one argument.', file=sys.stderr)
        return 2
    try:
        return run(sys.argv[1] if len(sys.argv) == 2 else None)
    except (OSError, ValueError) as error:
        print(f'Claude launcher failed: {error}', file=sys.stderr)
        return 1 if isinstance(error, OSError) else 2


if __name__ == '__main__':
    sys.exit(main())
