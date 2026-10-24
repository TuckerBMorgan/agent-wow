#!/opt/venv/bin/python3
"""Install this job's ChatGPT login and run the existing Codex game launcher."""
import json
import os
from pathlib import Path
import sys


def prepare_login(home=Path('/root/.codex')):
    raw = os.environ.pop('CODEX_AUTH_JSON', '')
    try:
        auth = json.loads(raw)
        tokens = auth.get('tokens', {})
        if auth.get('auth_mode') != 'chatgpt' or not all(
                tokens.get(key) for key in ('access_token', 'refresh_token', 'id_token')):
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise ValueError('A saved ChatGPT account login is required; API billing is disabled.') from None
    os.umask(0o077)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    auth.pop('OPENAI_API_KEY', None)
    path = home / 'auth.json'
    path.write_text(json.dumps(auth))
    path.chmod(0o600)
    (home / 'config.toml').write_text(
        'cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n')
    for name in ('CODEX_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY',
                 'CODEX_SECRET_ARN', 'OPENROUTER_SECRET_ARN'):
        os.environ.pop(name, None)
    os.environ.update(CODEX_HOME=str(home), CODEX_AUTH_MODE='subscription',
                      CODEX_MODEL_PROVIDER='openai')


def main():
    prepare_login()
    print('Codex authentication: ChatGPT account; API billing disabled.', flush=True)
    os.execv('/usr/local/bin/run-wow-agent-base', ['run-wow-agent-base', *sys.argv[1:]])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Codex account startup failed ({type(error).__name__}).', file=sys.stderr)
        sys.exit(1)
