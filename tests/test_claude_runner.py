"""Exercise subscription routing and credential handling with a fake Claude CLI."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import launch

SPEC = importlib.util.spec_from_file_location('claude_runner', Path(__file__).resolve().parents[1] / 'docker/run_claude.py')
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class ClaudeSubscriptionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root/'workspace'
        self.workspace.mkdir()
        binary = self.root/'claude'
        binary.write_text('#!' + sys.executable + '\n' + '''import json,os,pathlib,sys
root=pathlib.Path(os.environ['CLAUDE_TEST_ROOT'])
(root/'request.json').write_text(json.dumps({'argv':sys.argv[1:], 'stdin':sys.stdin.read(),
    'environment':{k:v for k,v in os.environ.items() if k.startswith(('ANTHROPIC_', 'CLAUDE_CODE_OAUTH'))}}))
print(json.dumps({'type':'system','subtype':'init','session_id':'test-session','model':'claude-opus-5-5','apiKeySource':'oauthToken'}))
print(json.dumps({'type':'result','is_error':False,'result':'Finished fake-subscription-token','total_cost_usd':7.5}))
sys.exit(int(os.environ.get('CLAUDE_TEST_EXIT','0')))
''')
        binary.chmod(0o755)
        environment = {'PATH':str(self.root)+os.pathsep+os.defpath, 'CLAUDE_TEST_ROOT':str(self.root),
                       'CLAUDE_CODE_OAUTH_TOKEN':'fake-subscription-token',
                       'ANTHROPIC_API_KEY':'wrong-billing-key',
                       'ANTHROPIC_AUTH_TOKEN':'wrong-billing-key','ANTHROPIC_BASE_URL':'https://wrong.example'}
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)

    def run_agent(self, prompt='Create one orc warrior and complete the starting area.'):
        with contextlib.redirect_stdout(io.StringIO()):
            return RUNNER.run(prompt, workspace=self.workspace)

    def test_subscription_cannot_inherit_api_credentials_or_gateway(self):
        self.assertEqual(self.run_agent(), 0)
        request = json.loads((self.root/'request.json').read_text())
        environment = request['environment']
        self.assertEqual(environment['CLAUDE_CODE_OAUTH_TOKEN'], 'fake-subscription-token')
        for name in ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL'):
            self.assertNotIn(name, environment)
        self.assertEqual(request['argv'][request['argv'].index('--model')+1], 'claude-opus-5-5')
        self.assertEqual(request['argv'][request['argv'].index('--effort')+1], 'medium')

    def test_native_events_prompt_and_billing_are_preserved_without_token(self):
        prompt = 'Literal $(false) `false`\n"orc warrior"'
        self.assertEqual(self.run_agent(prompt), 0)
        self.assertEqual(json.loads((self.root/'request.json').read_text())['stdin'], prompt)
        run_dir, = (self.workspace/'logs/agent').iterdir()
        metadata = json.loads((run_dir/'usage.json').read_text())
        self.assertEqual(metadata['billing'], 'subscription')
        self.assertEqual(metadata['provider'], 'anthropic')
        self.assertEqual(metadata['auth_source'], 'oauthToken')
        self.assertEqual(metadata['total_cost_usd'], 7.5)
        for path in run_dir.iterdir():
            self.assertNotIn('fake-subscription-token', path.read_text())

    def test_missing_subscription_token_never_falls_back_to_api_key(self):
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN':''}), \
             mock.patch.object(RUNNER.subprocess,'Popen') as spawn:
            with self.assertRaises(ValueError):
                self.run_agent()
            spawn.assert_not_called()

    def test_error_exit_is_preserved(self):
        with mock.patch.dict(os.environ, {'CLAUDE_TEST_EXIT':'1'}):
            self.assertEqual(self.run_agent(), 1)

    def test_launcher_records_subscription_selection_without_a_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            overrides = launch.container_overrides('Full orc warrior task', 'claude')
        self.assertEqual(overrides['command'], ['run-wow-agent', 'Full orc warrior task'])
        self.assertEqual(overrides['environment'], [
            {'name':'AGENT_WOW_HARNESS','value':'claude'},
            {'name':'CLAUDE_MODEL','value':'claude-opus-5-5'},
            {'name':'CLAUDE_EFFORT','value':'medium'},
        ])


if __name__ == '__main__':
    unittest.main()
