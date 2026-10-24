"""Exercise both account harnesses with local fake CLIs; no credentials or network."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('agent_runner', ROOT / 'docker/run_agent.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
FAKE_AUTH = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': 'wrong-api',
             'tokens': {'access_token': 'fake-access', 'refresh_token': 'fake-refresh', 'id_token': 'fake-id'}}
FAKE_CLI = r'''
import json, os, pathlib, signal, sys, time
root = pathlib.Path(os.environ['AGENT_TEST_ROOT'])
harness = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
(root / 'request.json').write_text(json.dumps({
    'harness': harness, 'argv': args, 'stdin': sys.stdin.read(),
    'environment': {k: v for k, v in os.environ.items()
                    if k.startswith(('ANTHROPIC_', 'CLAUDE_', 'CODEX_', 'OPENAI_'))},
}))
if os.environ.get('AGENT_TEST_SIGNAL'):
    def stop(number, frame):
        (root / 'received-signal.txt').write_text(str(number))
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    os.kill(os.getppid(), signal.SIGTERM)
    time.sleep(3)
    sys.exit(9)
if harness == 'codex':
    pathlib.Path(args[args.index('--output-last-message') + 1]).write_text('Finished task.\n')
    print('Agent output fake-access')
else:
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'test-session',
                      'model': 'claude-opus-5-5', 'apiKeySource': 'oauthToken'}))
    if not os.environ.get('AGENT_TEST_NO_RESULT'):
        print(json.dumps({'type': 'result', 'is_error': bool(os.environ.get('AGENT_TEST_ERROR')),
                          'result': 'Finished fake-subscription-token', 'total_cost_usd': 7.5}))
sys.exit(int(os.environ.get('AGENT_TEST_EXIT', '0')))
'''


class AgentRunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.home = self.root / 'home/.codex'
        for harness in ('codex', 'claude'):
            binary = self.root / harness
            binary.write_text('#!' + sys.executable + '\n' + FAKE_CLI)
            binary.chmod(0o755)
        self.env = {
            'PATH': str(self.root) + os.pathsep + os.defpath, 'AGENT_TEST_ROOT': str(self.root),
            'CODEX_AUTH_JSON': json.dumps(FAKE_AUTH), 'CODEX_API_KEY': 'wrong-api',
            'OPENAI_API_KEY': 'wrong-api', 'CLAUDE_CODE_OAUTH_TOKEN': 'fake-subscription-token',
            'ANTHROPIC_API_KEY': 'wrong-api', 'ANTHROPIC_AUTH_TOKEN': 'wrong-api',
            'ANTHROPIC_BASE_URL': 'https://wrong.example',
        }
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)

    def run_agent(self, prompt='Create an orc warrior.', default_task=None, **environment):
        with patch.dict(os.environ, {**self.env, **environment}, clear=True), \
             contextlib.redirect_stdout(io.StringIO()):
            status = runner.run(prompt, self.workspace, default_task, self.home)
        self.request = json.loads((self.root / 'request.json').read_text())
        self.run_dir = max((self.workspace / 'logs/agent').iterdir(), key=lambda p: p.stat().st_mtime_ns)
        return status

    def test_default_codex_login_is_private_and_cannot_fall_back_to_api_billing(self):
        self.assertEqual(self.run_agent(), 0)
        self.assertEqual(self.request['harness'], 'codex')
        saved = json.loads((self.home / 'auth.json').read_text())
        self.assertEqual(saved['tokens'], FAKE_AUTH['tokens'])
        self.assertNotIn('OPENAI_API_KEY', saved)
        self.assertEqual(stat.S_IMODE((self.home / 'auth.json').stat().st_mode), 0o600)
        self.assertIn('forced_login_method = "chatgpt"', (self.home / 'config.toml').read_text())
        environment = self.request['environment']
        self.assertEqual(environment['CODEX_MODEL_PROVIDER'], 'openai')
        for name in ('CODEX_AUTH_JSON', 'CODEX_API_KEY', 'OPENAI_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
            self.assertNotIn(name, environment)
        args = self.request['argv']
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn('model_provider="openai"', args)
        self.assertIn('model_reasoning_effort="medium"', args)
        self.assertEqual(args[args.index('--model') + 1], 'gpt-6.1-sol')
        self.assertEqual(args[args.index('--sandbox') + 1], 'workspace-write')
        self.assertEqual(args[args.index('--ask-for-approval') + 1], 'never')

    def test_claude_subscription_cannot_inherit_api_credentials_or_gateway(self):
        self.assertEqual(self.run_agent(AGENT_WOW_HARNESS='claude'), 0)
        self.assertEqual(self.request['harness'], 'claude')
        environment = self.request['environment']
        self.assertEqual(environment['CLAUDE_CODE_OAUTH_TOKEN'], 'fake-subscription-token')
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                     'CODEX_AUTH_JSON', 'OPENAI_API_KEY', 'CODEX_API_KEY'):
            self.assertNotIn(name, environment)
        args = self.request['argv']
        self.assertEqual(args[args.index('--model') + 1], 'claude-opus-5-5')
        self.assertEqual(args[args.index('--effort') + 1], 'medium')

    def test_missing_account_credentials_never_start_either_agent(self):
        for environment in (
                {'CODEX_AUTH_JSON': ''}, {'CODEX_AUTH_JSON': '{}'},
                {'CODEX_AUTH_JSON': '{"auth_mode":"apikey","OPENAI_API_KEY":"fake"}'},
                {'AGENT_WOW_HARNESS': 'claude', 'CLAUDE_CODE_OAUTH_TOKEN': ''}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                self.run_agent(**environment)
            self.assertFalse(self.home.exists())
            self.assertFalse((self.root / 'request.json').exists())

    def test_invalid_harness_never_starts_an_agent(self):
        with self.assertRaises(ValueError):
            self.run_agent(AGENT_WOW_HARNESS='unsupported')
        self.assertFalse((self.root / 'request.json').exists())

    def test_exact_prompt_and_redacted_output_are_preserved_for_both_harnesses(self):
        prompt = '--Create "Étoile"; $(false) `false`\nKeep tabs:\tand trailing newlines.\n\n'
        for harness in ('codex', 'claude'):
            with self.subTest(harness=harness):
                self.assertEqual(self.run_agent(prompt, AGENT_WOW_HARNESS=harness), 0)
                self.assertEqual(self.request['stdin'], prompt)
                self.assertEqual((self.run_dir / 'task.txt').read_text(), prompt)
                self.assertTrue((self.run_dir / 'final-report.txt').read_text())
                for path in self.run_dir.iterdir():
                    self.assertNotIn('fake-access', path.read_text())
                    self.assertNotIn('fake-subscription-token', path.read_text())

    def test_claude_native_events_and_subscription_metadata_are_preserved(self):
        self.assertEqual(self.run_agent(AGENT_WOW_HARNESS='claude'), 0)
        events = [json.loads(line) for line in (self.run_dir / 'events.jsonl').read_text().splitlines()]
        self.assertEqual([event['type'] for event in events], ['system', 'result'])
        metadata = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertEqual(metadata['billing'], 'subscription')
        self.assertEqual(metadata['provider'], 'anthropic')
        self.assertEqual(metadata['auth_source'], 'oauthToken')
        self.assertEqual(metadata['total_cost_usd'], 7.5)
        self.assertEqual((self.run_dir / 'final-report.txt').read_text(), 'Finished [REDACTED]\n')

    def test_default_task_and_error_exit_are_preserved_for_both_harnesses(self):
        task = self.root / 'default-task.txt'
        task.write_text('Default task\n\n')
        for harness in ('codex', 'claude'):
            with self.subTest(harness=harness):
                self.assertEqual(self.run_agent(None, task, AGENT_WOW_HARNESS=harness, AGENT_TEST_EXIT='7'), 7)
                self.assertEqual((self.run_dir / 'task.txt').read_bytes(), task.read_bytes())
                self.assertEqual((self.run_dir / 'agent-exit-code.txt').read_text(), '7\n')

    def test_claude_cannot_succeed_without_a_successful_final_result(self):
        for setting in ('AGENT_TEST_NO_RESULT', 'AGENT_TEST_ERROR'):
            with self.subTest(setting=setting):
                self.assertEqual(self.run_agent(AGENT_WOW_HARNESS='claude', **{setting: '1'}), 1)

    def test_termination_is_forwarded_and_recorded_for_both_harnesses(self):
        script = (
            'import importlib.util, pathlib, sys; '
            f'spec=importlib.util.spec_from_file_location("runner", {str(ROOT / "docker/run_agent.py")!r}); '
            'runner=importlib.util.module_from_spec(spec); spec.loader.exec_module(runner); '
            f'sys.exit(runner.run("Test cancellation", workspace=pathlib.Path({str(self.workspace)!r}), '
            f'home=pathlib.Path({str(self.home)!r})))'
        )
        for harness in ('codex', 'claude'):
            with self.subTest(harness=harness):
                (self.root / 'received-signal.txt').unlink(missing_ok=True)
                result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                                        env={**self.env, 'AGENT_WOW_HARNESS': harness, 'AGENT_TEST_SIGNAL': '1'},
                                        timeout=10)
                self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
                self.assertEqual((self.root / 'received-signal.txt').read_text(), str(int(signal.SIGTERM)))
                run_dir = max((self.workspace / 'logs/agent').iterdir(), key=lambda p: p.stat().st_mtime_ns)
                self.assertEqual((run_dir / 'agent-exit-code.txt').read_text(), '143\n')


if __name__ == '__main__':
    unittest.main()
