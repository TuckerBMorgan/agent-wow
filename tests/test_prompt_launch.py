"""Credential-free tests for runtime prompts and the AWS launch scripts.

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
elif tool == 'tofu':
    if args == ['output', '-raw', 'job_definition']:
        print(os.environ['WOW_TEST_NEW_DEFINITION'])
    elif args != ['apply']:
        raise SystemExit('Unexpected OpenTofu call in test: ' + repr(args))
elif tool == 'git':
    if args == ['rev-parse', 'HEAD']:
        print('0123456789abcdef0123456789abcdef01234567')
    else:
        raise SystemExit('Unexpected Git call in test: ' + repr(args))
elif tool == 'codex':
    if 'exec' in args:
        (root / 'codex-stdin').write_bytes(sys.stdin.buffer.read())
        Path(option('--output-last-message')).write_text('local test report\n')
        print('local test agent output')
        raise SystemExit(int(os.environ.get('WOW_TEST_CODEX_EXIT', '0')))
    elif 'login' not in args:
        raise SystemExit('Unexpected Codex call in test: ' + repr(args))
elif tool == 'watch-job.py':
    raise SystemExit(int(os.environ.get('WOW_TEST_WATCH_EXIT', '0')))
else:
    raise SystemExit('Unknown test command: ' + tool)
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
        for tool in ('aws', 'docker', 'tofu', 'git', 'codex'):
            executable = self.bin / tool
            executable.write_text('#!' + sys.executable + '\n' + STUB)
            executable.chmod(0o755)
        self.project = self.root / 'project'
        self.project.mkdir()
        for script in ('launch.sh', 'rebuild-and-launch.sh'):
            shutil.copyfile(REPOSITORY / script, self.project / script)
            (self.project / script).chmod(0o755)
        # The watcher has its own mocked AWS tests. Keep this fixture focused
        # on forwarding the newly submitted job ID and its exit status.
        (self.project / 'watch-job.py').write_text(STUB)
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
        return subprocess.run(
            [str(self.project / name), *args], cwd=cwd or self.project,
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
        watchers = [c for c in calls if c['tool'] == 'watch-job.py']
        self.assertEqual(len(watchers), 1)
        self.assertEqual(watchers[0]['args'], ['local-test-job', '--region', 'us-west-2'])
        self.assertLess(calls.index(submissions[0]), calls.index(watchers[0]))

    def prepare_runtime(self):
        script = dockerfile_heredoc('/usr/local/bin/run-wow-agent', 'BASH')
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
        result = self.run_script('launch.sh', self.prompt)
        self.assert_succeeded(result)
        self.assert_watched_once()
        payload = self.submission()
        self.assertEqual(payload['containerOverrides']['command'], ['run-wow-agent', self.prompt])
        self.assertEqual(payload['jobDefinition'], 'agent-wow')

    def test_launch_without_prompt_uses_job_default_and_latest_revision(self):
        self.assert_succeeded(self.run_script('launch.sh', AWS_DEFAULT_OUTPUT='text'))
        self.assert_watched_once()
        payload = self.submission()
        self.assertNotIn('containerOverrides', payload)
        self.assertEqual(payload['jobDefinition'], 'agent-wow')

    def test_launch_can_pin_job_definition_without_changing_prompt(self):
        self.assert_succeeded(self.run_script(
            'launch.sh', self.prompt, WOW_JOB_DEFINITION=NEW_JOB_DEFINITION,
        ))
        payload = self.submission()
        self.assertEqual(payload['jobDefinition'], NEW_JOB_DEFINITION)
        self.assertEqual(payload['containerOverrides']['command'], ['run-wow-agent', self.prompt])

    def test_launch_rejects_invalid_prompts_before_aws(self):
        for args in [('',), (' \n\t\r ',), ('one', 'two')]:
            with self.subTest(args=args):
                result = self.run_script('launch.sh', *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                self.assertEqual(self.records('calls.jsonl'), [])

    def test_launch_finds_sibling_watcher_from_another_directory(self):
        self.assert_succeeded(self.run_script('launch.sh', self.prompt, cwd=self.root))
        self.assert_watched_once()
        self.assertEqual(self.submission()['containerOverrides']['command'], ['run-wow-agent', self.prompt])

    def test_launch_propagates_watcher_failure(self):
        result = self.run_script('launch.sh', WOW_TEST_WATCH_EXIT='1')
        self.assertEqual(result.returncode, 1)
        self.assert_watched_once()

    def test_launch_propagates_watcher_interruption(self):
        result = self.run_script('launch.sh', WOW_TEST_WATCH_EXIT='130')
        self.assertEqual(result.returncode, 130)
        self.assert_watched_once()

    def test_launch_submit_failure_does_not_watch_or_retry(self):
        result = self.run_script('launch.sh', WOW_TEST_SUBMIT_EXIT='17')
        self.assertEqual(result.returncode, 17)
        self.assertIn(b'Local test submission failed.', result.stderr)
        calls = self.records('calls.jsonl')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['tool'], 'aws')
        self.assertEqual(calls[0]['args'][:2], ['batch', 'submit-job'])
        self.assertEqual(self.records('submissions.jsonl'), [])

    def test_runtime_records_and_sends_exact_custom_prompt(self):
        self.prepare_runtime()
        result = self.run_script('run-wow-agent', self.prompt, CODEX_API_KEY='local-test-fake-key')
        self.assert_succeeded(result)
        self.assertEqual(self.runtime_task().read_bytes(), self.prompt.encode())
        self.assertEqual((self.root / 'codex-stdin').read_bytes(), self.prompt.encode())
        calls = self.records('calls.jsonl')
        self.assertEqual(len(calls), 1)
        self.assertIn('exec', calls[0]['args'])

    def test_runtime_without_argument_uses_baked_task(self):
        self.prepare_runtime()
        self.assert_succeeded(self.run_script('run-wow-agent', CODEX_API_KEY='local-test-fake-key'))
        self.assertEqual(self.runtime_task().read_bytes(), self.default_task)
        self.assertEqual((self.root / 'codex-stdin').read_bytes(), self.default_task)

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
        result = self.run_script('rebuild-and-launch.sh', self.prompt)
        self.assert_succeeded(result)
        payload = self.submission()
        self.assertEqual(payload['jobDefinition'], NEW_JOB_DEFINITION)
        self.assertEqual(payload['containerOverrides']['command'], ['run-wow-agent', self.prompt])
        calls = self.records('calls.jsonl')
        self.assertTrue(any(c['tool'] == 'docker' and c['args'][:2] == ['buildx', 'build'] for c in calls))
        self.assertTrue(any(c['tool'] == 'tofu' and c['args'] == ['apply'] for c in calls))
        self.assert_watched_once()

    def test_rebuild_without_prompt_keeps_job_default(self):
        self.assert_succeeded(self.run_script('rebuild-and-launch.sh'))
        self.assert_watched_once()
        payload = self.submission()
        self.assertEqual(payload['jobDefinition'], NEW_JOB_DEFINITION)
        self.assertNotIn('containerOverrides', payload)

    def test_rebuild_propagates_watcher_failure(self):
        result = self.run_script('rebuild-and-launch.sh', WOW_TEST_WATCH_EXIT='1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.records('submissions.jsonl')), 1)
        self.assert_watched_once()

    def test_rebuild_propagates_watcher_interruption(self):
        result = self.run_script('rebuild-and-launch.sh', WOW_TEST_WATCH_EXIT='130')
        self.assertEqual(result.returncode, 130)
        self.assert_watched_once()

    def test_rebuild_submit_failure_does_not_watch_or_retry(self):
        result = self.run_script('rebuild-and-launch.sh', WOW_TEST_SUBMIT_EXIT='17')
        self.assertEqual(result.returncode, 17)
        calls = self.records('calls.jsonl')
        submissions = [c for c in calls if c['tool'] == 'aws' and c['args'][:2] == ['batch', 'submit-job']]
        self.assertEqual(len(submissions), 1)
        self.assertFalse(any(c['tool'] == 'watch-job.py' for c in calls))
        self.assertEqual(self.records('submissions.jsonl'), [])

    def test_rebuild_rejects_invalid_prompts_before_build_or_aws(self):
        for args in [('',), (' \n\t\r ',), ('one', 'two')]:
            with self.subTest(args=args):
                result = self.run_script('rebuild-and-launch.sh', *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                self.assertEqual(self.records('calls.jsonl'), [])
                self.assertFalse((self.project / 'terraform.tfvars').exists())


if __name__ == '__main__':
    unittest.main()
