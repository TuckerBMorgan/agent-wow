#!/opt/venv/bin/python3
"""Run the official DeepSeek Harness headlessly through OpenRouter."""

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


DEFAULT_MODEL = 'deepseek/deepseek-v4.1-flash-1017'
DEFAULT_EFFORT = 'high'


def profile_patch(model, effort):
    # JSON is valid YAML. The profile refers to a credential name, never its value.
    return [
        {'id': 'llm-deepseek', 'disabled': True},
        {'id': 'llm-deepseek-account', 'disabled': True},
        {'id': 'session-telemetry-otel', 'disabled': True},
        {'id': 'agent-default-model', 'config': {
            'provider': 'openrouter', 'model': model, 'reasoningEffort': effort,
        }},
        {'id': 'llm-pi-ai', 'config': {'providers': {'openrouter': {
            'apiKeyEnv': 'OPENROUTER_API_KEY', 'api': 'openai-completions',
            'baseURL': 'https://openrouter.ai/api/v1',
            'compat': {'supportsDeveloperRole': False, 'maxTokensField': 'max_tokens',
                       'thinkingFormat': 'openrouter'},
            'models': [{'id': model, 'contextWindow': 1048576, 'maxTokens': 32768,
                        'reasoningEfforts': {'low': 'low', 'high': 'high', 'max': 'max'}}],
        }}}},
    ]


def run(prompt=None, workspace=Path('/workspace'), default_task=Path('/opt/orc-quest-task.txt')):
    model = os.environ.get('DEEPSEEK_MODEL', DEFAULT_MODEL)
    effort = os.environ.get('DEEPSEEK_EFFORT', DEFAULT_EFFORT)
    if not re.fullmatch(r'deepseek/[A-Za-z0-9_./:~+-]+', model):
        raise ValueError('DEEPSEEK_MODEL must be a DeepSeek OpenRouter model ID.')
    if effort not in ('low', 'high', 'max'):
        raise ValueError('DEEPSEEK_EFFORT must be low, high, or max.')
    if prompt is not None and not prompt.strip():
        raise ValueError('Prompt must not be blank.')
    key = os.environ.get('OPENROUTER_API_KEY', '')
    if not key.strip():
        raise ValueError('A nonempty OPENROUTER_API_KEY is required.')

    os.umask(0o077)
    logs = workspace / 'logs/agent'
    logs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%dT%H%M%SZ-', time.gmtime()), dir=logs))
    task = run_dir / 'task.txt'
    task.write_bytes(prompt.encode() if prompt is not None else default_task.read_bytes())
    patch = run_dir / 'headless.patch.json'
    patch.write_text(json.dumps(profile_patch(model, effort), indent=2) + '\n')
    env = dict(os.environ, DSH_HOME=str(run_dir / 'dsh'), DSH_PERMISSION_MODE='danger-full-access')
    for name in ('CODEX_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', 'DEEPSEEK_API_KEY'):
        env.pop(name, None)
    command = ['dsh', '--profile', 'headless', '--patch', str(patch), '--json', '-']
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    child = None
    interrupted = 0
    report = None
    session_id = None
    usage = []
    turn_reason = None

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

            emit(f'DeepSeek Harness 0.2.1-rc.2 through OpenRouter; model: {model}; effort: {effort}')
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
                if kind == 'session':
                    session_id = event.get('sessionId')
                elif kind == 'final':
                    report = event.get('text', '')
                elif kind == 'status':
                    if event.get('phase') == 'step_end' and event.get('usage'):
                        usage.append(event['usage'])
                    if event.get('phase') == 'turn_end':
                        turn_reason = event.get('reason')
                emit(event.get('text', '') if kind in ('text', 'thinking', 'final') else json.dumps(event, ensure_ascii=False))
            child.stdout.close()
            status = child.wait()
            status = 128 - status if status < 0 else status
            status = (128 + interrupted if interrupted else status) or (1 if report is None else 0)
            (run_dir / 'final-report.txt').write_text((report or f'DeepSeek exited with status {status}. See agent.log.') + '\n')
            (run_dir / 'agent-exit-code.txt').write_text(str(status) + '\n')
            metadata = {'harness': 'deepseek', 'harness_version': '0.2.1-rc.2', 'provider': 'openrouter',
                        'model': model, 'effort': effort, 'session_id': session_id,
                        'started_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
                        'elapsed_seconds': round(time.monotonic() - started, 3), 'exit_code': status,
                        'turn_end_reason': turn_reason, 'step_usage': usage}
            (run_dir / 'usage.json').write_text(json.dumps(metadata, indent=2) + '\n')
            emit(f'DeepSeek finished with status {status}. Report: {run_dir}/final-report.txt')
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
        print('Usage: run-wow-agent ["prompt"]', file=sys.stderr)
        return 2
    try:
        return run(sys.argv[1] if len(sys.argv) == 2 else None)
    except (OSError, ValueError) as error:
        # Keep exception details out of logs: they may include a credential-bearing environment.
        print(f'DeepSeek launcher failed ({type(error).__name__}).', file=sys.stderr)
        return 1 if isinstance(error, OSError) else 2


if __name__ == '__main__':
    sys.exit(main())
