"""DeepSeek runner tests use a local stub; no service or real key is used."""

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


SPEC = importlib.util.spec_from_file_location('deepseek_runner', Path(__file__).resolve().parents[1] / 'docker/run_deepseek.py')
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class DeepSeekRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        binary = self.root / 'dsh'
        binary.write_text('#!' + sys.executable + '\n' + '''import os,sys,pathlib
root=pathlib.Path(os.environ['DSH_TEST_ROOT'])
(root/'stdin').write_bytes(sys.stdin.buffer.read())
print((root/'events').read_text(),end='')
sys.exit(int(os.environ.get('DSH_TEST_EXIT','0')))
''')
        binary.chmod(0o755)
        self.events = [
            {'type': 'session', 'sessionId': 'test-session'},
            {'type': 'tool_call', 'tool': 'bash', 'input': {'command': 'printf harmless'}},
            {'type': 'status', 'phase': 'step_end', 'usage': {'inputTokens': 25, 'outputTokens': 10}},
            {'type': 'status', 'phase': 'turn_end', 'reason': 'completed'},
            {'type': 'final', 'text': 'Done fake-private-key'},
        ]
        (self.root / 'events').write_text(''.join(json.dumps(e) + '\n' for e in self.events))
        patch = mock.patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.defpath,
                                           'OPENROUTER_API_KEY': 'fake-private-key',
                                           'DSH_TEST_ROOT': str(self.root)}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)

    def run_agent(self, prompt='Create one orc warrior and complete the starting area.'):
        with contextlib.redirect_stdout(io.StringIO()):
            return RUNNER.run(prompt, workspace=self.workspace)

    def test_preserves_prompt_records_native_events_usage_and_redacts_key(self):
        prompt = 'Literal $(echo nope) `echo nope`\n"orc warrior"\n'
        self.assertEqual(self.run_agent(prompt), 0)
        self.assertEqual((self.root / 'stdin').read_text(), prompt)
        run_dir, = (self.workspace / 'logs/agent').iterdir()
        self.assertEqual((run_dir / 'task.txt').read_text(), prompt)
        self.assertEqual((run_dir / 'final-report.txt').read_text(), 'Done [REDACTED]\n')
        metadata = json.loads((run_dir / 'usage.json').read_text())
        self.assertEqual(metadata['session_id'], 'test-session')
        self.assertEqual(metadata['harness'], 'deepseek')
        self.assertEqual(metadata['step_usage'], [{'inputTokens': 25, 'outputTokens': 10}])
        for path in run_dir.iterdir():
            if path.is_file():
                self.assertNotIn('fake-private-key', path.read_text())

    def test_error_exit_is_not_masked_by_a_final_event(self):
        with mock.patch.dict(os.environ, {'DSH_TEST_EXIT': '1'}):
            self.assertEqual(self.run_agent(), 1)

    def test_missing_final_event_cannot_look_successful(self):
        (self.root / 'events').write_text(json.dumps(self.events[0]) + '\n')
        self.assertEqual(self.run_agent(), 1)

    def test_rejects_unsupported_model_effort_or_blank_prompt_before_startup(self):
        with mock.patch.object(RUNNER.subprocess, 'Popen') as spawn:
            for env in ({'DEEPSEEK_MODEL': 'anthropic/test'}, {'DEEPSEEK_EFFORT': 'typo'}, {'OPENROUTER_API_KEY': ''}):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    with self.assertRaises(ValueError):
                        self.run_agent()
            with self.assertRaises(ValueError):
                self.run_agent('  ')
            spawn.assert_not_called()

    def test_launcher_uses_native_deepseek_settings(self):
        overrides = launch.container_overrides('Full task', 'deepseek')
        self.assertEqual(overrides['command'], ['run-wow-agent', 'Full task'])
        self.assertEqual(overrides['environment'], [
            {'name': 'DEEPSEEK_MODEL', 'value': 'deepseek/deepseek-v4.1-flash-1017'},
            {'name': 'DEEPSEEK_EFFORT', 'value': 'high'},
        ])


if __name__ == '__main__':
    unittest.main()
