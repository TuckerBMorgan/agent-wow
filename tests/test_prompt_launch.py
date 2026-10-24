"""Credential-free tests for runtime prompts and the AWS launcher.

Run with: python3 -m unittest discover -s tests -p 'test_prompt_launch.py'
Every external service command is replaced by a local stub. No image is built,
job submitted, credential read, or game server contacted.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
NEW_JOB_DEFINITION = 'arn:aws:batch:us-west-2:123456789012:job-definition/test-game:9'

# This shared executable is installed under each external command's name.
# It records calls inside the temporary test directory and has no network code.
STUB = r'''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ['WOW_TEST_DIR'])
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps({'tool': tool, 'args': args}) + '\n')

def option(name):
    return args[args.index(name) + 1]

def json_option(name):
    value = option(name)
    if value.startswith('file://'):
        value = Path(value[7:]).read_text()
    return json.loads(value)

if tool == 'aws':
    if args[:2] == ['ecr', 'get-login-password']:
        print('fake-password-only-for-local-test')
    elif args[:2] == ['batch', 'submit-job']:
        if os.environ.get('WOW_TEST_SUBMIT_EXIT'):
            print('Local test submission failed.', file=sys.stderr)
            raise SystemExit(int(os.environ['WOW_TEST_SUBMIT_EXIT']))
        payload = json_option('--cli-input-json') if '--cli-input-json' in args else {}
        for argument, key in [('--job-definition', 'jobDefinition'),
                              ('--job-queue', 'jobQueue'), ('--job-name', 'jobName')]:
            if argument in args:
                payload[key] = option(argument)
        if '--container-overrides' in args:
            payload['containerOverrides'] = json_option('--container-overrides')
        with (root / 'submissions.jsonl').open('a') as log:
            log.write(json.dumps(payload) + '\n')
        print(json.dumps({'jobId': 'local-test-job'}))
    else:
        raise SystemExit('Unexpected AWS call in test: ' + repr(args))
elif tool == 'docker':
    if args[:1] == ['login']:
        sys.stdin.buffer.read()
    elif args[:2] != ['buildx', 'build']:
        raise SystemExit('Unexpected Docker call in test: ' + repr(args))
    elif os.environ.get('WOW_TEST_BUILD_EXIT'):
        raise SystemExit(int(os.environ['WOW_TEST_BUILD_EXIT']))
elif tool == 'tofu':
    if args == ['output', '-raw', 'job_definition']:
        print(os.environ['WOW_TEST_NEW_DEFINITION'])
    elif args == ['output', '-raw', 'ecr_repository_url']:
        print('123456789012.dkr.ecr.us-west-2.amazonaws.com/agent-wow')
    elif args != ['apply', '-auto-approve', '-input=false']:
        raise SystemExit('Unexpected OpenTofu call in test: ' + repr(args))
elif tool == 'git':
    if args == ['rev-parse', 'HEAD']:
        print('0123456789abcdef0123456789abcdef01234567')
    else:
        raise SystemExit('Unexpected Git call in test: ' + repr(args))
elif tool == 'codex':
    if 'exec' in args:
        (root / 'codex-key-names.json').write_text(json.dumps([
            name for name in ('CODEX_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY')
            if os.environ.get(name)
        ]))
        (root / 'codex-stdin').write_bytes(sys.stdin.buffer.read())
        Path(option('--output-last-message')).write_text('local test report\n')
        print('local test agent output')
        raise SystemExit(int(os.environ.get('WOW_TEST_CODEX_EXIT', '0')))
    elif 'login' not in args:
        raise SystemExit('Unexpected Codex call in test: ' + repr(args))
elif tool == 'codex-openrouter-cache':
    os.execvp(args[0], args)
else:
    raise SystemExit('Unknown test command: ' + tool)
'''


RUNNER = r'''
import importlib.util
import json
import os
from pathlib import Path
import sys

root = Path(os.environ['WOW_TEST_DIR'])
spec = importlib.util.spec_from_file_location('launch', root / 'project/launch.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

def follow(job_id, region, log_group):
    with (root / 'calls.jsonl').open('a') as log:
        log.write(json.dumps({'tool': 'watch', 'args': [
            job_id, '--region', region, '--log-group', log_group,
        ]}) + '\n')
    status = int(os.environ.get('WOW_TEST_WATCH_EXIT', '0'))
    if status == 130:
        raise KeyboardInterrupt
    return status

# Watching has its own mocked AWS tests. Exercise the real launcher entry point
# while replacing the wait loop so this fixture cannot contact a live service.
launcher.follow = follow
sys.exit(launcher.main())
'''


def dockerfile_heredoc(destination, delimiter):
    text = (REPOSITORY / 'Dockerfile').read_text()
    match = re.search(
        r"^COPY(?: --chmod=\d+)? <<'" + re.escape(delimiter) + r"' "
        + re.escape(destination) + r'\n(.*?)^' + re.escape(delimiter) + '$',
        text, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f'Dockerfile heredoc not found: {destination}')
    return match.group(1)


class PromptLaunchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='agent-wow-prompt-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for tool in ('aws', 'docker', 'tofu', 'git', 'codex', 'codex-openrouter-cache'):
            executable = self.bin / tool
            executable.write_text('#!' + sys.executable + '\n' + STUB)
            executable.chmod(0o755)
        self.project = self.root / 'project'
        self.project.mkdir()
        shutil.copyfile(REPOSITORY / 'launch.py', self.project / 'launch.py')
        shutil.copyfile(REPOSITORY / 'jobs.py', self.project / 'jobs.py')
        (self.project / 'launch-runner.py').write_text(RUNNER)
        # Never inherit real credentials, profiles, keys, or user configuration.
        self.env = {
            'PATH': str(self.bin) + ':/usr/bin:/bin:/usr/sbin:/sbin',
            'HOME': str(self.root / 'home'),
            'LC_ALL': 'C',
            'WOW_TEST_DIR': str(self.root),
            'WOW_TOFU': str(self.bin / 'tofu'),
            'WOW_TEST_NEW_DEFINITION': NEW_JOB_DEFINITION,
        }
        self.sentinel = self.root / 'must-not-be-created'
        self.prompt = (
            '--Create a druid named "Étoile"; keep single quotes \'literal\'.\n'
            f'$(touch {self.sentinel}) `touch {self.sentinel}` $HOME \\path\n'
            'Retain a tab:\tand trailing newlines.\n\n'
        )

    def run_script(self, name, *args, cwd=None, **environment):
        command = [sys.executable, str(self.project / 'launch-runner.py')] if name == 'launch.py' else [str(self.project / name)]
        return subprocess.run(
            [*command, *args], cwd=cwd or self.project,
            env={**self.env, **environment}, capture_output=True, timeout=15,
        )

    def records(self, filename):
        path = self.root / filename
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def submission(self):
        submissions = self.records('submissions.jsonl')
        self.assertEqual(len(submissions), 1)
        return submissions[0]

    def assert_succeeded(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertFalse(self.sentinel.exists(), 'Prompt content was evaluated by a shell')

    def assert_watched_once(self):
        calls = self.records('calls.jsonl')
        submissions = [c for c in calls if c['tool'] == 'aws' and c['args'][:2] == ['batch', 'submit-job']]
        self.assertEqual(len(submissions), 1)
        args = submissions[0]['args']
        self.assertIn('--output', args)
        self.assertEqual(args[args.index('--output') + 1], 'json')
        watchers = [c for c in calls if c['tool'] == 'watch']
        self.assertEqual(len(watchers), 1)
        self.assertEqual(watchers[0]['args'], [
            'local-test-job', '--region', 'us-west-2', '--log-group', '/aws/batch/agent-wow',
        ])
        self.assertLess(calls.index(submissions[0]), calls.index(watchers[0]))

    def prepare_runtime(self):
        script = dockerfile_heredoc('/usr/local/bin/run-wow-agent', 'BASH')
        script = script.replace('/usr/local/bin/codex-openrouter-cache', str(self.bin / 'codex-openrouter-cache'))
        # The shipped script uses container-only absolute paths. Redirect each
        # root to this fixture so tests cannot touch a real /workspace or /root.
        roots = ('workspace', 'root', 'opt', 'run')
        script = re.sub(
            r'/(?:workspace|root|opt|run)(?=/|\b)',
            lambda match: str(self.root / match.group(0).lstrip('/')),
            script,
        )
        for name in roots:
            (self.root / name).mkdir(exist_ok=True)
        self.default_task = dockerfile_heredoc('/opt/orc-quest-task.txt', 'TASK').encode()
        (self.root / 'opt/orc-quest-task.txt').write_bytes(self.default_task)
        runtime = self.project / 'run-wow-agent'
        runtime.write_text(script)
        runtime.chmod(0o755)

    def runtime_task(self):
        tasks = list((self.root / 'workspace/logs/agent').glob('*/task.txt'))
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def test_launch_preserves_literal_multiline_prompt(self):
        result = self.run_script('launch.py', '--', self.prompt)
        self.assert_succeeded(result)
        self.assert_watched_once()
        payload = self.submission()
        self.assertEqual(payload['containerOverrides']['command'], ['run-wow-agent', self.prompt])
        self.assertEqual(payload['jobDefinition'], 'agent-wow')

    def test_launch_without_prompt_uses_job_default_and_latest_revision(self):
        self.assert_succeeded(self.run_script('launch.py', AWS_DEFAULT_OUTPUT='text'))
        self.assert_watched_once()
        payload = self.submission()
        self.assertNotIn('command', payload['containerOverrides'])
        self.assertEqual(payload['containerOverrides']['environment'], [
            {'name': 'CODEX_MODEL_PROVIDER', 'value': 'openrouter'},
            {'name': 'CODEX_MODEL', 'value': 'openai/gpt-6.1-sol'},
            {'name': 'CODEX_REASONING_EFFORT', 'value': 'high'},
        ])
        self.assertEqual(payload['jobDefinition'], 'agent-wow')

    def test_launch_can_pin_job_definition_without_changing_prompt(self):
        self.assert_succeeded(self.run_script(
            'launch.py', '--', self.prompt, WOW_JOB_DEFINITION=NEW_JOB_DEFINITION,
        ))
        payload = self.submission()
        self.assertEqual(payload['jobDefinition'], NEW_JOB_DEFINITION)
        self.assertEqual(payload['containerOverrides']['command'], ['run-wow-agent', self.prompt])

    def test_launch_can_use_test_queue(self):
        self.assert_succeeded(self.run_script('launch.py', '--queue', 'agent-wow-test', '--', self.prompt))
        self.assertEqual(self.submission()['jobQueue'], 'agent-wow-test')
        self.assert_watched_once()

    def test_launch_can_select_direct_openai(self):
        self.assert_succeeded(self.run_script('launch.py', CODEX_MODEL_PROVIDER='openai'))
        self.assertEqual(self.submission()['containerOverrides']['environment'], [
            {'name': 'CODEX_MODEL_PROVIDER', 'value': 'openai'},
            {'name': 'CODEX_MODEL', 'value': 'gpt-6.1-sol'},
            {'name': 'CODEX_REASONING_EFFORT', 'value': 'high'},
        ])

    def test_claude_launch_routes_prompt_and_ignores_codex_settings(self):
        self.assert_succeeded(self.run_script(
            'launch.py', '--harness', 'claude', '--', self.prompt,
            CODEX_MODEL_PROVIDER='not-used', CODEX_MODEL='not-used', CODEX_REASONING_EFFORT='not-used',
        ))
        payload = self.submission()
        self.assertEqual(payload['jobDefinition'], 'agent-wow-claude')
        self.assertEqual(payload['jobName'], 'agent-wow-claude')
        self.assertEqual(payload['containerOverrides']['command'], ['run-wow-agent', self.prompt])
        self.assertEqual(payload['containerOverrides']['environment'], [
            {'name': 'CLAUDE_MODEL', 'value': 'anthropic/claude-opus-5.5'},
            {'name': 'CLAUDE_EFFORT', 'value': 'medium'},
        ])
        self.assert_watched_once()

    def test_claude_launch_accepts_other_anthropic_model_and_effort(self):
        self.assert_succeeded(self.run_script(
            'launch.py', '--harness', 'claude', CLAUDE_MODEL='anthropic/claude-fable-5.1', CLAUDE_EFFORT='high',
        ))
        self.assertEqual(self.submission()['containerOverrides']['environment'], [
            {'name': 'CLAUDE_MODEL', 'value': 'anthropic/claude-fable-5.1'},
            {'name': 'CLAUDE_EFFORT', 'value': 'high'},
        ])

    def test_claude_rejects_invalid_settings_before_submitting(self):
        for environment in ({'CLAUDE_MODEL': ''}, {'CLAUDE_MODEL': 'openai/gpt-6.1-sol'},
                            {'CLAUDE_EFFORT': 'none'}, {'CLAUDE_EFFORT': 'typo'}):
            with self.subTest(environment=environment):
                result = self.run_script('launch.py', '--harness', 'claude', **environment)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.records('calls.jsonl'), [])

    def test_claude_rebuild_cannot_build_or_submit_a_codex_image(self):
        result = self.run_script('launch.py', '--harness', 'claude', '--rebuild')
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'Codex Dockerfile', result.stderr)
        self.assertEqual(self.records('calls.jsonl'), [])

    def test_launch_rejects_invalid_prompts_before_aws(self):
        for args in [('',), (' \n\t\r ',), ('one', 'two')]:
            with self.subTest(args=args):
                result = self.run_script('launch.py', *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                self.assertEqual(self.records('calls.jsonl'), [])

    def test_launch_finds_sibling_watcher_from_another_directory(self):
        self.assert_succeeded(self.run_script('launch.py', '--', self.prompt, cwd=self.root))
        self.assert_watched_once()
        self.assertEqual(self.submission()['containerOverrides']['command'], ['run-wow-agent', self.prompt])

    def test_launch_propagates_watcher_failure(self):
        result = self.run_script('launch.py', WOW_TEST_WATCH_EXIT='1')
        self.assertEqual(result.returncode, 1)
        self.assert_watched_once()

    def test_launch_propagates_watcher_interruption(self):
        result = self.run_script('launch.py', WOW_TEST_WATCH_EXIT='130')
        self.assertEqual(result.returncode, 130)
        self.assert_watched_once()

    def test_launch_submit_failure_does_not_watch_or_retry(self):
        result = self.run_script('launch.py', WOW_TEST_SUBMIT_EXIT='17')
        self.assertEqual(result.returncode, 17)
        self.assertIn(b'Local test submission failed.', result.stderr)
        calls = self.records('calls.jsonl')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['tool'], 'aws')
        self.assertEqual(calls[0]['args'][:2], ['batch', 'submit-job'])
        self.assertEqual(self.records('submissions.jsonl'), [])

    def test_launch_forwards_openrouter_provider_and_model(self):
        self.assert_succeeded(self.run_script(
            'launch.py', CODEX_MODEL_PROVIDER='openrouter', CODEX_MODEL='openai/gpt-6.1-sol',
        ))
        self.assertEqual(self.submission()['containerOverrides']['environment'], [
            {'name': 'CODEX_MODEL_PROVIDER', 'value': 'openrouter'},
            {'name': 'CODEX_MODEL', 'value': 'openai/gpt-6.1-sol'},
            {'name': 'CODEX_REASONING_EFFORT', 'value': 'high'},
        ])
        self.assert_watched_once()

    def test_launch_and_rebuild_forward_reasoning_effort(self):
        for args in [(), ('--rebuild',)]:
            with self.subTest(args=args):
                (self.root / 'submissions.jsonl').unlink(missing_ok=True)
                self.assert_succeeded(self.run_script(
                    'launch.py', *args, CODEX_MODEL_PROVIDER='openrouter',
                    CODEX_MODEL='openai/gpt-6.1-sol', CODEX_REASONING_EFFORT='high',
                ))
                self.assertIn({'name': 'CODEX_REASONING_EFFORT', 'value': 'high'},
                              self.submission()['containerOverrides']['environment'])

    def test_launch_rejects_invalid_effort_before_building_or_submitting(self):
        for model, effort in [('gpt-6.1-sol', 'none'), ('openai/gpt-6.1-sol', 'minimal'),
                              ('openai/gpt-6.1-sol', 'ultra'), ('test-model', ''),
                              ('test-model', 'hihg'), ('test-model', 'high"; touch nope')]:
            with self.subTest(model=model, effort=effort):
                result = self.run_script('launch.py', '--rebuild', CODEX_MODEL=model,
                                         CODEX_REASONING_EFFORT=effort)
                self.assertEqual(result.returncode, 2)
                self.assertIn(b'effort', result.stderr.lower())
                self.assertEqual(self.records('calls.jsonl'), [])

    def test_watch_reconnects_without_building_or_submitting(self):
        self.assert_succeeded(self.run_script(
            'launch.py', '--watch', 'existing-job', CODEX_MODEL_PROVIDER='openrouter',
        ))
        self.assertEqual(self.records('submissions.jsonl'), [])
        self.assertEqual(self.records('calls.jsonl'), [{
            'tool': 'watch', 'args': ['existing-job', '--region', 'us-west-2',
                                     '--log-group', '/aws/batch/agent-wow'],
        }])

    def test_watch_rejects_invalid_arguments(self):
        for args in [('--watch', 'existing-job', '--rebuild'), ('--watch', 'existing-job', 'prompt'),
                     ('--watch', ''), ('--watch', ' \t ')]:
            with self.subTest(args=args):
                self.assertNotEqual(self.run_script('launch.py', *args).returncode, 0)
                self.assertEqual(self.records('calls.jsonl'), [])

    def test_rebuild_failure_does_not_submit_a_job(self):
        result = self.run_script('launch.py', '--rebuild', WOW_TEST_BUILD_EXIT='7')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.records('submissions.jsonl'), [])
        self.assertFalse(any(c['tool'] == 'watch' for c in self.records('calls.jsonl')))

    def test_runtime_records_and_sends_exact_custom_prompt(self):
        self.prepare_runtime()
        result = self.run_script('run-wow-agent', self.prompt, CODEX_API_KEY='local-test-fake-key')
        self.assert_succeeded(result)
        self.assertEqual(self.runtime_task().read_bytes(), self.prompt.encode())
        self.assertEqual((self.root / 'codex-stdin').read_bytes(), self.prompt.encode())
        calls = self.records('calls.jsonl')
        self.assertEqual(len(calls), 1)
        self.assertIn('exec', calls[0]['args'])
        self.assertFalse(any(arg.startswith('model_reasoning_effort=') for arg in calls[0]['args']))
        self.assertIn(b'Reasoning effort override: unset (Codex/provider default)', result.stdout)

    def test_runtime_without_argument_uses_baked_task(self):
        self.prepare_runtime()
        self.assert_succeeded(self.run_script('run-wow-agent', CODEX_API_KEY='local-test-fake-key'))
        self.assertEqual(self.runtime_task().read_bytes(), self.default_task)
        self.assertEqual((self.root / 'codex-stdin').read_bytes(), self.default_task)

    def test_runtime_openrouter_uses_selected_model_and_only_its_key(self):
        self.prepare_runtime()
        result = self.run_script(
            'run-wow-agent', self.prompt, CODEX_MODEL_PROVIDER='openrouter',
            CODEX_MODEL='openai/gpt-6.1-sol', OPENROUTER_API_KEY='local-fake-router-key',
            CODEX_API_KEY='local-fake-openai-key', OPENAI_API_KEY='local-fake-other-key',
        )
        self.assert_succeeded(result)
        calls = self.records('calls.jsonl')
        self.assertEqual([call['tool'] for call in calls], ['codex-openrouter-cache', 'codex'])
        self.assertIn('exec', calls[-1]['args'])
        args = calls[-1]['args']
        self.assertEqual(args[args.index('--model') + 1], 'openai/gpt-6.1-sol')
        self.assertEqual(json.loads((self.root / 'codex-key-names.json').read_text()), ['OPENROUTER_API_KEY'])

    def test_runtime_anthropic_uses_cache_adapter(self):
        self.prepare_runtime()
        result = self.run_script(
            'run-wow-agent', self.prompt, CODEX_MODEL_PROVIDER='openrouter',
            CODEX_MODEL='anthropic/claude-sonnet-5', OPENROUTER_API_KEY='local-fake-router-key',
        )
        self.assert_succeeded(result)
        calls = self.records('calls.jsonl')
        self.assertEqual([call['tool'] for call in calls], ['codex-openrouter-cache', 'codex'])
        self.assertEqual(calls[0]['args'], ['codex', *calls[1]['args']])
        self.assertEqual((self.root / 'codex-stdin').read_bytes(), self.prompt.encode())

    def test_runtime_passes_explicit_effort_to_codex_including_through_cache_adapter(self):
        self.prepare_runtime()
        for model, effort in [('openai/gpt-6.1-sol', 'high'), ('openai/gpt-6-sol', 'none'),
                              ('anthropic/claude-sonnet-5', 'medium')]:
            with self.subTest(model=model, effort=effort):
                self.assert_succeeded(result := self.run_script(
                    'run-wow-agent', self.prompt, CODEX_MODEL_PROVIDER='openrouter',
                    CODEX_MODEL=model, CODEX_REASONING_EFFORT=effort,
                    OPENROUTER_API_KEY='local-fake-router-key',
                ))
                args = self.records('calls.jsonl')[-1]['args']
                setting = f'model_reasoning_effort="{effort}"'
                self.assertIn(setting, args)
                self.assertEqual(args[args.index(setting) - 1], '--config')
                self.assertLess(args.index(setting), args.index('exec'))
                self.assertIn(f'Reasoning effort override: {effort}'.encode(), result.stdout)

    def test_runtime_rejects_invalid_effort_before_authentication(self):
        self.prepare_runtime()
        for model, effort in [('gpt-6.1-sol', 'none'), ('openai/gpt-6.1-sol', 'minimal'),
                              ('openai/gpt-6.1-sol', 'ultra'), ('test-model', ''),
                              ('test-model', 'hihg'), ('test-model', 'high"; touch nope')]:
            with self.subTest(model=model, effort=effort):
                result = self.run_script('run-wow-agent', CODEX_MODEL=model,
                                         CODEX_REASONING_EFFORT=effort)
                self.assertEqual(result.returncode, 2)
                self.assertIn(b'effort', result.stderr.lower())
                self.assertEqual(self.records('calls.jsonl'), [])

    @unittest.skipUnless(os.environ.get('WOW_TEST_CODEX_BINARY'), 'Optional local Codex binary capture')
    def test_runtime_sol_effort_reaches_real_codex_request_without_api_credentials(self):
        from http.server import ThreadingHTTPServer
        from test_openrouter_cache import FakeRouter

        router = ThreadingHTTPServer(('127.0.0.1', 0), FakeRouter)
        router.requests = []
        router.cached = 5000
        threading.Thread(target=router.serve_forever, daemon=True).start()
        self.addCleanup(router.server_close)
        self.addCleanup(router.shutdown)
        self.prepare_runtime()
        script = self.project / 'run-wow-agent'
        # Both auth and inference stay on loopback with an isolated Codex home.
        script.write_text(script.read_text().replace('https://openrouter.ai/api/v1',
                          f'http://127.0.0.1:{router.server_port}/api/v1'))
        (self.bin / 'codex').unlink()
        (self.bin / 'codex').symlink_to(os.environ['WOW_TEST_CODEX_BINARY'])
        codex_home = self.root / 'offline-codex-home'
        codex_home.mkdir()
        for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
            with self.subTest(effort=effort):
                self.assert_succeeded(self.run_script(
                    'run-wow-agent', 'Reply briefly; do not use tools.',
                    CODEX_MODEL_PROVIDER='openrouter', CODEX_MODEL='openai/gpt-6.1-sol',
                    CODEX_REASONING_EFFORT=effort, CODEX_HOME=str(codex_home),
                    OPENROUTER_API_KEY='offline-fake-key',
                ))
                self.assertEqual(router.requests[-1]['body']['reasoning']['effort'], effort)
        self.assertEqual(len(router.requests), 5)

    def test_runtime_rejects_invalid_prompts_before_authentication(self):
        self.prepare_runtime()
        for args in [('',), (' \n\t\r ',), ('one', 'two')]:
            with self.subTest(args=args):
                result = self.run_script('run-wow-agent', *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                self.assertEqual(self.records('calls.jsonl'), [])
                self.assertFalse((self.root / 'codex-stdin').exists())

    def test_runtime_propagates_codex_failure_and_preserves_prompt(self):
        self.prepare_runtime()
        result = self.run_script(
            'run-wow-agent', self.prompt, CODEX_API_KEY='local-test-fake-key', WOW_TEST_CODEX_EXIT='17',
        )
        self.assertEqual(result.returncode, 17)
        task = self.runtime_task()
        self.assertEqual(task.read_bytes(), self.prompt.encode())
        self.assertEqual((task.parent / 'agent-exit-code.txt').read_text().strip(), '17')
        self.assertTrue((task.parent / 'agent.log').exists())

    def test_rebuild_forwards_prompt_to_new_job_definition(self):
        result = self.run_script('launch.py', '--rebuild', '--', self.prompt)
        self.assert_succeeded(result)
        payload = self.submission()
        self.assertEqual(payload['jobDefinition'], NEW_JOB_DEFINITION)
        self.assertEqual(payload['containerOverrides']['command'], ['run-wow-agent', self.prompt])
        calls = self.records('calls.jsonl')
        self.assertTrue(any(c['tool'] == 'docker' and c['args'][:2] == ['buildx', 'build'] for c in calls))
        self.assertTrue(any(c['tool'] == 'tofu' and c['args'] == ['apply', '-auto-approve', '-input=false'] for c in calls))
        self.assert_watched_once()

    def test_rebuild_without_prompt_keeps_job_default(self):
        self.assert_succeeded(self.run_script('launch.py', '--rebuild'))
        self.assert_watched_once()
        payload = self.submission()
        self.assertEqual(payload['jobDefinition'], NEW_JOB_DEFINITION)
        self.assertNotIn('command', payload['containerOverrides'])

    def test_rebuild_propagates_watcher_failure(self):
        result = self.run_script('launch.py', '--rebuild', WOW_TEST_WATCH_EXIT='1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.records('submissions.jsonl')), 1)
        self.assert_watched_once()

    def test_rebuild_propagates_watcher_interruption(self):
        result = self.run_script('launch.py', '--rebuild', WOW_TEST_WATCH_EXIT='130')
        self.assertEqual(result.returncode, 130)
        self.assert_watched_once()

    def test_rebuild_submit_failure_does_not_watch_or_retry(self):
        result = self.run_script('launch.py', '--rebuild', WOW_TEST_SUBMIT_EXIT='17')
        self.assertEqual(result.returncode, 17)
        calls = self.records('calls.jsonl')
        submissions = [c for c in calls if c['tool'] == 'aws' and c['args'][:2] == ['batch', 'submit-job']]
        self.assertEqual(len(submissions), 1)
        self.assertFalse(any(c['tool'] == 'watch' for c in calls))
        self.assertEqual(self.records('submissions.jsonl'), [])

    def test_rebuild_rejects_invalid_prompts_before_build_or_aws(self):
        for args in [('',), (' \n\t\r ',), ('one', 'two')]:
            with self.subTest(args=args):
                result = self.run_script('launch.py', '--rebuild', *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                self.assertEqual(self.records('calls.jsonl'), [])
                self.assertFalse((self.project / 'terraform.tfvars').exists())


if __name__ == '__main__':
    unittest.main()
