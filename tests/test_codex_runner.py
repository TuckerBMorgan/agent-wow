"""Exercise saved account login and the real runner with a fake Codex CLI."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from launch import container_overrides

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('codex_runner', ROOT / 'docker/run_codex.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
FAKE_AUTH = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
             'tokens': {'access_token': 'fake-access', 'refresh_token': 'fake-refresh', 'id_token': 'fake-id'}}


class CodexAccountTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.home = self.root / 'home/.codex'
        binary = self.root / 'codex'
        binary.write_text('#!' + sys.executable + '\n' + '''import json,os,pathlib,sys
root=pathlib.Path(os.environ['CODEX_TEST_ROOT'])
args=sys.argv[1:]
(root/'request.json').write_text(json.dumps({'argv':args, 'stdin':sys.stdin.read(),
    'credential_names':[k for k in ('CODEX_AUTH_JSON','OPENAI_API_KEY','CODEX_API_KEY','CLAUDE_CODE_OAUTH_TOKEN') if k in os.environ]}))
pathlib.Path(args[args.index('--output-last-message')+1]).write_text('Finished task.\\n')
print('Agent output fake-access')
sys.exit(int(os.environ.get('CODEX_TEST_EXIT','0')))
''')
        binary.chmod(0o755)
        environment = {'PATH':str(self.root)+os.pathsep+os.defpath, 'CODEX_TEST_ROOT':str(self.root),
                       'CODEX_AUTH_JSON': json.dumps(FAKE_AUTH), 'CODEX_API_KEY':'wrong-api',
                       'OPENAI_API_KEY':'wrong-api', 'CLAUDE_CODE_OAUTH_TOKEN':'wrong-account'}
        environment_patch = patch.dict(os.environ, environment, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)

    def run_agent(self, prompt='Create an orc warrior.'):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.run(prompt, workspace=self.workspace, home=self.home)

    def test_account_login_is_private_and_cannot_fall_back_to_api_billing(self):
        runner.prepare_login(self.home)
        saved = json.loads((self.home / 'auth.json').read_text())
        self.assertEqual(saved['tokens'], FAKE_AUTH['tokens'])
        self.assertNotIn('OPENAI_API_KEY', saved)
        self.assertEqual(stat.S_IMODE((self.home / 'auth.json').stat().st_mode), 0o600)
        self.assertIn('forced_login_method = "chatgpt"', (self.home / 'config.toml').read_text())
        self.assertEqual(os.environ['CODEX_MODEL_PROVIDER'], 'openai')
        for name in ('CODEX_AUTH_JSON', 'CODEX_API_KEY', 'OPENAI_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):
            self.assertNotIn(name, os.environ)

    def test_missing_or_api_credentials_never_start_the_agent(self):
        for raw in ('', '{}', '{"auth_mode":"apikey","OPENAI_API_KEY":"fake"}'):
            with self.subTest(raw=raw), patch.dict(os.environ, {'CODEX_AUTH_JSON': raw}):
                with self.assertRaisesRegex(ValueError, 'API billing is disabled'):
                    self.run_agent()
                self.assertFalse(self.home.exists())
                self.assertFalse((self.root / 'request.json').exists())

    def test_exact_prompt_sandbox_and_logs_are_preserved(self):
        prompt = '--Create "Étoile"; $(false) `false`\nKeep tabs:\tand trailing newlines.\n\n'
        self.assertEqual(self.run_agent(prompt), 0)
        request = json.loads((self.root / 'request.json').read_text())
        self.assertEqual(request['stdin'], prompt)
        self.assertEqual(request['credential_names'], [])
        args = request['argv']
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn('model_provider="openai"', args)
        self.assertIn('model_reasoning_effort="medium"', args)
        self.assertEqual(args[args.index('--model')+1], 'gpt-6.1-sol')
        self.assertEqual(args[args.index('--sandbox')+1], 'workspace-write')
        self.assertEqual(args[args.index('--ask-for-approval')+1], 'never')
        run_dir, = (self.workspace / 'logs/agent').iterdir()
        self.assertEqual((run_dir / 'task.txt').read_text(), prompt)
        self.assertEqual((run_dir / 'final-report.txt').read_text(), 'Finished task.\n')
        self.assertNotIn('fake-access', (run_dir / 'agent.log').read_text())

    def test_default_task_and_error_exit_are_preserved(self):
        task = self.root / 'default-task.txt'
        task.write_text('Default task\n\n')
        with patch.dict(os.environ, {'CODEX_TEST_EXIT':'7'}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(None, self.workspace, task, self.home), 7)
        run_dir, = (self.workspace / 'logs/agent').iterdir()
        self.assertEqual((run_dir / 'task.txt').read_bytes(), task.read_bytes())
        self.assertEqual((run_dir / 'agent-exit-code.txt').read_text(), '7\n')

    def test_dispatch_preserves_prompt_and_rejects_unknown_harness(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        dispatch = re.search(r"COPY --chmod=755 <<'BASH' /usr/local/bin/run-wow-agent\n(.*?)\nBASH",
                             dockerfile, re.S).group(1)
        for harness in ('codex', 'claude'):
            executable = self.root / ('run-wow-' + harness)
            executable.write_text('#!' + sys.executable + '\nimport json,sys; print(json.dumps(sys.argv[1:]))\n')
            executable.chmod(0o755)
            prompt = 'Literal $(false) `false`\n"orc warrior"\n\n'
            result = subprocess.run(['bash', '-c', dispatch, 'run-wow-agent', prompt],
                                    env=dict(os.environ, AGENT_WOW_HARNESS=harness),
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), [prompt])
        result = subprocess.run(['bash', '-c', dispatch],
                                env=dict(os.environ, AGENT_WOW_HARNESS='unsupported'),
                                capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_launcher_uses_native_model_and_exact_prompt_for_account_runs(self):
        with patch.dict(os.environ, {}, clear=True):
            overrides = container_overrides('create an orc warrior and adventure and level up to level 20', 'codex')
        settings = {item['name']: item['value'] for item in overrides['environment']}
        self.assertEqual(settings['AGENT_WOW_HARNESS'], 'codex')
        self.assertEqual(settings['CODEX_MODEL'], 'gpt-6.1-sol')
        self.assertEqual(settings['CODEX_REASONING_EFFORT'], 'medium')
        self.assertEqual(overrides['command'][1], 'create an orc warrior and adventure and level up to level 20')


if __name__ == '__main__':
    unittest.main()
