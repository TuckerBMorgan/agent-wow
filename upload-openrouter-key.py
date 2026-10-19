#!/usr/bin/env python3
"""Upload config/openrouter.env to the secret created by OpenTofu.

Usage: python3 upload-openrouter-key.py
The key is read as data, never sourced as shell code or printed.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parent
    path = root / 'config/openrouter.env'
    if not path.is_file():
        sys.exit('Save OPENROUTER_API_KEY=your-key in config/openrouter.env first.')
    values = [line.partition('=')[2].strip() for line in path.read_text().splitlines()
              if line.partition('=')[0].strip() == 'OPENROUTER_API_KEY']
    if len(values) != 1 or not values[0]:
        sys.exit('Set exactly one nonempty OPENROUTER_API_KEY in config/openrouter.env. Use the raw key without quotes.')

    tofu = os.environ.get('WOW_TOFU') or shutil.which('tofu') or '/private/tmp/agent-wow-tofu-omqtgfn4/tofu'
    if not shutil.which(tofu) or not shutil.which('aws'):
        sys.exit('Install AWS CLI and OpenTofu, or set WOW_TOFU to your OpenTofu executable.')
    result = subprocess.run([tofu, 'output', '-raw', 'openrouter_secret_arn'], cwd=root,
                            capture_output=True, text=True)
    arn = result.stdout.strip()
    if result.returncode or not arn.startswith('arn:') or ':secretsmanager:' not in arn:
        sys.exit('Apply main.tf first so OpenTofu provides openrouter_secret_arn.')

    # AWS CLI cannot reliably read /dev/stdin here. NamedTemporaryFile creates
    # an owner-only file and deletes it afterward; the key stays out of argv.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as secret_file:
        secret_file.write(values[0])
        secret_file.flush()
        result = subprocess.run([
            'aws', 'secretsmanager', 'put-secret-value', '--region', arn.split(':')[3],
            '--secret-id', arn, '--secret-string', f'file://{secret_file.name}',
            '--no-cli-pager',
        ], text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={**os.environ, 'AWS_CLI_AUTO_PROMPT': 'off'})
    if result.returncode:
        detail = result.stderr.replace(values[0], '[REDACTED]').strip()
        sys.exit(f'OpenRouter key upload failed:\n{detail}')
    print('OpenRouter key uploaded. New AWS jobs will use it.')


if __name__ == '__main__':
    main()
