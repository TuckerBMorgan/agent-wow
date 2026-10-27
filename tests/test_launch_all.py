"""Check that each level-10 case receives its own fixed prompt and run key."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import benchmark_results
import launch_all


class LaunchAllTests(unittest.TestCase):
    def test_level10_creates_independent_attempts_and_never_launches_claude(self):
        outputs = {key: {'value': value} for key, value in
                   {'url': 'https://results.example/', 'table': 'test-results', 'region': 'us-west-2'}.items()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('sys.argv', ['launch_all.py', '--benchmark', 'level-10', '--harness', 'codex']), \
                 patch.object(launch_all, '__file__', str(root / 'launch_all.py')), \
                 patch.object(launch_all, 'RACE_CLASSES', {'orc': ['warrior'], 'night-elf': ['druid']}), \
                 patch.object(benchmark_results.subprocess, 'check_output', return_value=json.dumps(outputs)), \
                 patch.object(benchmark_results.subprocess, 'run') as write, \
                 patch.object(launch_all, 'submit_job', return_value={'jobId': 'test-job'}) as submit:
                self.assertEqual(launch_all.main(), 0)
            self.assertEqual(submit.call_count, 2)
            configs = [json.loads(path.read_text()) for path in (root / 'config').glob('*/*.json')]
            self.assertEqual(len(configs), 2)
            self.assertEqual(len({config['key'] for config in configs}), 2)
            for call, goal in zip(submit.call_args_list, ['orc warrior', 'night elf druid']):
                overrides = call.args[3]
                self.assertEqual(overrides['command'], ['run-wow-agent', f'create a {goal} and adventure until they are level 10'])
                env = {item['name']: item['value'] for item in overrides['environment']}
                self.assertEqual(env['AGENT_WOW_HARNESS'], 'codex')
                self.assertIn(env['BENCHMARK_KEY'], {config['key'] for config in configs})
            for call in write.call_args_list:
                args = call.args[0]
                item = json.loads(args[args.index('--item') + 1])
                self.assertEqual(item['target_level'], {'N': '10'})
                self.assertIn(item['run_id']['S'], {hashlib.sha256(config['key'].encode()).hexdigest() for config in configs})
