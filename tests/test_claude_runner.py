"""Offline Claude runner tests using a local executable, never a model API."""
import contextlib
import importlib.util
import io
import json
import os
import signal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PATH = Path(__file__).resolve().parents[1] / 'docker/run_claude.py'
spec = importlib.util.spec_from_file_location('claude_runner', PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

STUB = '''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['TEST_ROOT'])
(root/'input').write_bytes(sys.stdin.buffer.read())
(root/'config.json').write_text(json.dumps({
 'args':sys.argv[1:], 'base_url':os.environ['ANTHROPIC_BASE_URL'],
 'token':os.environ['ANTHROPIC_AUTH_TOKEN'], 'api_key':os.environ['ANTHROPIC_API_KEY'],
 'openrouter_key_in_child':'OPENROUTER_API_KEY' in os.environ,
 'opus':os.environ['ANTHROPIC_DEFAULT_OPUS_MODEL']}))
print(json.dumps({'type':'system','subtype':'init','model':'anthropic/claude-opus-5.5','session_id':'fake-session'}))
print(json.dumps({'type':'system','subtype':'api_retry','attempt':1,'max_retries':3,'retry_delay_ms':10,'error_status':429,'error':'rate_limit'}))
print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Working '+os.environ['ANTHROPIC_AUTH_TOKEN']},{'type':'tool_use','name':'Bash','input':{'command':'go version'}}],'usage':{'input_tokens':20,'cache_read_input_tokens':1000}}}))
if not os.environ.get('TEST_NO_RESULT'):
 print(json.dumps({'type':'result','is_error':bool(os.environ.get('TEST_ERROR_RESULT')),'result':'Mock final report.','session_id':'fake-session','usage':{'cache_read_input_tokens':1000}}))
sys.exit(int(os.environ.get('TEST_EXIT','0')))
'''

class ClaudeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root/'bin').mkdir()
        stub = self.root/'bin/claude'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.default = self.root/'task.txt'
        self.default.write_text('Default task.\n')
        self.console = io.StringIO()
        self.env = {'PATH':str(self.root/'bin')+':'+os.environ['PATH'], 'HOME':str(self.root),
                    'TEST_ROOT':str(self.root),'OPENROUTER_API_KEY':'offline-fake-key'}

    def execute(self, prompt=None, **environment):
        with patch.dict(os.environ,{**self.env,**environment},clear=True), contextlib.redirect_stdout(self.console):
            return runner.run(prompt,self.root/'workspace',self.default)

    def run_dir(self):
        return next((self.root/'workspace/logs/agent').iterdir())

    def test_preserves_exact_prompt_configures_auth_and_streams_results(self):
        prompt = '--Create "Étoile"; $(touch nope) `echo nope`\n\t$HOME\n\n'
        self.assertEqual(self.execute(prompt),0)
        directory = self.run_dir()
        self.assertEqual((directory/'task.txt').read_text(),prompt)
        self.assertEqual((self.root/'input').read_text(),prompt)
        settings = json.loads((self.root/'config.json').read_text())
        self.assertEqual(settings['base_url'],'https://openrouter.ai/api')
        self.assertEqual(settings['token'],'offline-fake-key')
        self.assertEqual(settings['api_key'],'')
        self.assertFalse(settings['openrouter_key_in_child'])
        self.assertEqual(settings['opus'],'anthropic/claude-opus-5.5')
        args = settings['args']
        self.assertEqual(args[args.index('--model')+1],'anthropic/claude-opus-5.5')
        self.assertEqual(args[args.index('--effort')+1],'high')
        self.assertIn('--dangerously-skip-permissions',args)
        self.assertNotIn('--bare',args)
        self.assertEqual((directory/'final-report.txt').read_text(),'Mock final report.\n')
        metadata = json.loads((directory/'usage.json').read_text())
        self.assertEqual(metadata['usage']['cache_read_input_tokens'],1000)
        self.assertEqual(metadata['session_id'],'fake-session')
        self.assertGreaterEqual(metadata['elapsed_seconds'],0)
        self.assertIn('API retry:',self.console.getvalue())
        self.assertIn('Tool Bash:',self.console.getvalue())
        for path in directory.iterdir():
            self.assertNotIn('offline-fake-key',path.read_text())
        self.assertNotIn('offline-fake-key',self.console.getvalue())

    def test_baked_prompt_and_custom_effort(self):
        self.assertEqual(self.execute(CLAUDE_EFFORT='medium'),0)
        self.assertEqual((self.root/'input').read_bytes(),self.default.read_bytes())
        self.assertEqual(json.loads((self.run_dir()/'usage.json').read_text())['effort'],'medium')

    def test_process_failure_is_preserved(self):
        self.assertEqual(self.execute('Task',TEST_EXIT='17'),17)
        self.assertEqual((self.run_dir()/'agent-exit-code.txt').read_text(),'17\n')

    def test_error_result_cannot_be_mistaken_for_success(self):
        self.assertEqual(self.execute('Task',TEST_ERROR_RESULT='1'),1)

    def test_missing_final_result_cannot_be_mistaken_for_success(self):
        self.assertEqual(self.execute('Task',TEST_NO_RESULT='1'),1)
        self.assertIn('without a final result',(self.run_dir()/'final-report.txt').read_text())

    def test_termination_is_forwarded_and_recorded(self):
        handlers = {}
        child = MagicMock()
        child.poll.return_value = None

        def interrupted_output():
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return iter(())

        def finished():
            child.poll.return_value = 143
            return -signal.SIGTERM

        child.stdout.__iter__.side_effect = interrupted_output
        child.wait.side_effect = finished
        with patch.object(runner.signal, 'signal', side_effect=lambda number, handler: handlers.update({number: handler})), \
             patch.object(runner.subprocess, 'Popen', return_value=child):
            self.assertEqual(self.execute('Task'),143)
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual((self.run_dir()/'agent-exit-code.txt').read_text(),'143\n')

    def test_rejects_invalid_configuration_before_launch(self):
        for values in ({'OPENROUTER_API_KEY':''},{'CLAUDE_MODEL':'openai/model'},
                       {'CLAUDE_MODEL':'anthropic/model; touch nope'},{'CLAUDE_EFFORT':'none'}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.execute('Task',**values)
        with self.assertRaises(ValueError):
            self.execute(' \n\t')
        self.assertFalse((self.root/'input').exists())

if __name__ == '__main__':
    unittest.main()
