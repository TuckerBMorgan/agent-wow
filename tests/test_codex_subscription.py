import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from launch import container_overrides

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('account_runner', ROOT / 'docker/run_codex_subscription.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
FAKE_AUTH = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
             'tokens': {'access_token': 'fake-access', 'refresh_token': 'fake-refresh', 'id_token': 'fake-id'}}


class CodexAccountTests(unittest.TestCase):
    def test_account_login_is_private_and_cannot_fall_back_to_api_billing(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'CODEX_AUTH_JSON': json.dumps(FAKE_AUTH), 'CODEX_API_KEY': 'fake-api',
                'OPENAI_API_KEY': 'fake-api', 'OPENROUTER_API_KEY': 'fake-router',
                'CODEX_MODEL_PROVIDER': 'openrouter'}, clear=True):
            home = Path(directory) / 'codex'
            runner.prepare_login(home)
            saved = json.loads((home / 'auth.json').read_text())
            self.assertEqual(saved['tokens'], FAKE_AUTH['tokens'])
            self.assertNotIn('OPENAI_API_KEY', saved)
            self.assertEqual(stat.S_IMODE((home / 'auth.json').stat().st_mode), 0o600)
            self.assertIn('forced_login_method = "chatgpt"', (home / 'config.toml').read_text())
            self.assertEqual(os.environ['CODEX_MODEL_PROVIDER'], 'openai')
            for name in ('CODEX_AUTH_JSON', 'CODEX_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY'):
                self.assertNotIn(name, os.environ)

    def test_missing_or_api_credentials_never_start_the_agent(self):
        for raw in ('', '{}', '{"auth_mode":"apikey","OPENAI_API_KEY":"fake"}'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {'CODEX_AUTH_JSON': raw}, clear=True):
                home = Path(directory) / 'codex'
                with self.assertRaisesRegex(ValueError, 'API billing is disabled'):
                    runner.prepare_login(home)
                self.assertFalse(home.exists())

    def test_launcher_uses_native_model_and_exact_prompt_for_account_runs(self):
        with patch.dict(os.environ, {}, clear=True):
            overrides = container_overrides('create an orc warrior and adventure and level up to level 20',
                                            'codex-subscription')
        settings = {item['name']: item['value'] for item in overrides['environment']}
        self.assertEqual(settings['CODEX_AUTH_MODE'], 'subscription')
        self.assertEqual(settings['CODEX_MODEL'], 'gpt-6.1-sol')
        self.assertEqual(settings['CODEX_REASONING_EFFORT'], 'medium')
        self.assertEqual(overrides['command'][1], 'create an orc warrior and adventure and level up to level 20')


if __name__ == '__main__':
    unittest.main()
