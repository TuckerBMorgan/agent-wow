import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class GameAccountTests(unittest.TestCase):
    def test_saved_credentials_are_reused_and_account_mismatch_is_rejected(self):
        docker = MagicMock()
        source = Path(__file__).resolve().parents[1] / 'docker/prepare_wow_account.py'
        spec = importlib.util.spec_from_file_location('prepare_wow_account', source)
        accounts = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'docker': docker}):
            spec.loader.exec_module(accounts)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / 'auth.json'
            original = json.dumps({'username': 'EXISTING', 'password': 'AlreadySaved123'})
            auth.write_text(original)
            with patch.object(accounts.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                account, _ = accounts.ensure_account(root)
                self.assertEqual(account['username'], 'EXISTING')
                with self.assertRaises(ValueError):
                    accounts.ensure_account(root, 'OTHER')
            self.assertEqual(auth.read_text(), original)
            docker.from_env.assert_not_called()


if __name__ == '__main__':
    unittest.main()
