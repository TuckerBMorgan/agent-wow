#!/opt/venv/bin/python3
"""Run Codex or Claude with account auth, saving logs and native final results."""

from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from agent_common import credential_redactor, model_settings


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


def log_claude_event(event, emit):
    kind = event.get('type')
    if kind == 'system':
        if event.get('subtype') == 'init':
            emit(f'Claude session: {event.get("session_id")}; model: {event.get("model")}')
            emit(f'Claude authentication source: {event.get("apiKeySource")}')
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
        emit(event.get('result') or json.dumps(event.get('errors', [])))


def run(prompt=None, workspace=Path('/workspace'), default_task=Path('/opt/orc-quest-task.txt'),
        home=Path('/root/.codex')):
    harness = os.environ.get('AGENT_WOW_HARNESS', 'codex')
    model, effort = model_settings(harness)
    label = harness.capitalize()
    if prompt is not None and not prompt.strip():
        raise ValueError('Prompt must not be blank.')

    os.umask(0o077)
    redact = credential_redactor()
    if harness == 'codex':
        prepare_login(home)
        env = dict(os.environ)
        for name in ('.cache', '.docker', 'go'):
            (home.parent / name).mkdir(parents=True, exist_ok=True)
    else:
        key = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')
        if not key.strip():
            raise ValueError('A nonempty CLAUDE_CODE_OAUTH_TOKEN is required.')
        env = dict(os.environ, DISABLE_AUTOUPDATER='1',
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_SUBAGENT_MODEL=model)
        # Select exactly one billing source. Never fall back to API credits.
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                     'CODEX_API_KEY', 'OPENAI_API_KEY', 'CODEX_AUTH_JSON',
                     'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_PROFILE', 'ANTHROPIC_MODEL',
                     'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
            env.pop(name, None)
        env['CLAUDE_CODE_OAUTH_TOKEN'] = key
        # Keep auxiliary requests on the chosen model, with no automatic model fallback.
        for role in ('OPUS', 'SONNET', 'HAIKU', 'FABLE'):
            env[f'ANTHROPIC_DEFAULT_{role}_MODEL'] = model

    logs = workspace / 'logs/agent'
    logs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%dT%H%M%SZ-', time.gmtime()), dir=logs))
    task = run_dir / 'task.txt'
    task.write_bytes(prompt.encode() if prompt is not None else default_task.read_bytes())
    if harness == 'codex':
        command = ['codex', '--config', 'cli_auth_credentials_store="file"',
                   '--config', 'forced_login_method="chatgpt"', '--config', 'model_provider="openai"',
                   '--model', model, '--config', f'model_reasoning_effort="{effort}"',
                   '--ask-for-approval', 'never', '--config', 'sandbox_workspace_write.network_access=true',
                   'exec', '--sandbox', 'workspace-write', '--cd', str(workspace), '--skip-git-repo-check',
                   '--add-dir', '/run', '--add-dir', str(home.parent / 'go'),
                   '--add-dir', str(home.parent / '.cache'), '--add-dir', str(home.parent / '.docker'),
                   '--color', 'never', '--output-last-message', str(run_dir / 'final-report.txt'), '-']
    else:
        command = ['claude', '-p', '--model', model, '--effort', effort,
                   '--dangerously-skip-permissions', '--output-format', 'stream-json', '--verbose']
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    child = None
    interrupted = 0
    result = None
    session = {}

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
        with (run_dir / 'agent.log').open('w', buffering=1) as log, task.open('rb') as stdin, \
             ((run_dir / 'events.jsonl').open('w', buffering=1) if harness == 'claude' else nullcontext()) as events:
            def emit(text):
                text = redact(text).rstrip('\n')
                log.write(text + '\n')
                print(text, flush=True)

            emit(f'{label} authentication: subscription; model: {model}; effort: {effort}')
            emit(f'Run log: {run_dir}/agent.log')
            child = subprocess.Popen(command, cwd=workspace, env=env, stdin=stdin,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1)
            if interrupted:
                stop(interrupted, None)
            for line in child.stdout:
                if harness == 'codex':
                    emit(line)
                    continue
                safe = redact(line)
                try:
                    event = json.loads(safe)
                except ValueError:
                    emit(safe)
                    continue
                if not isinstance(event, dict):
                    emit(safe)
                    continue
                events.write(json.dumps(event, ensure_ascii=False) + '\n')
                if event.get('type') == 'system' and event.get('subtype') == 'init':
                    session = event
                elif event.get('type') == 'result':
                    result = event
                log_claude_event(event, emit)
            child.stdout.close()
            exit_code = child.wait()
            exit_code = 128 - exit_code if exit_code < 0 else exit_code
            exit_code = 128 + interrupted if interrupted else exit_code
            if harness == 'claude':
                exit_code = exit_code or (1 if result is None or result.get('is_error') else 0)
                report = (result or {}).get('result') or '\n'.join(map(str, (result or {}).get('errors', [])))
                if not report:
                    report = f'Claude exited with status {exit_code} without a final result. See agent.log.'
                (run_dir / 'final-report.txt').write_text(report + '\n')
                metadata = {**(result or {}), 'harness': 'claude-code', 'provider': 'anthropic',
                            'billing': 'subscription',
                            'cost_note': 'CLI cost estimates are not a subscription charge.',
                            'requested_model': model, 'actual_model': session.get('model'), 'effort': effort,
                            'auth_source': session.get('apiKeySource'),
                            'claude_code_version': session.get('claude_code_version'),
                            'started_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
                            'elapsed_seconds': round(time.monotonic() - started, 3), 'exit_code': exit_code}
                (run_dir / 'usage.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
            # Codex writes its own final report through --output-last-message.
            (run_dir / 'agent-exit-code.txt').write_text(str(exit_code) + '\n')
            emit(f'{label} finished with status {exit_code}. Report: {run_dir}/final-report.txt')
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
        print(f'Agent launcher failed: {error}', file=sys.stderr)
        return 1 if isinstance(error, OSError) else 2


if __name__ == '__main__':
    sys.exit(main())
