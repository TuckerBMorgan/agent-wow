#!/opt/venv/bin/python
"""Create a game account through the world console and confirm authentication."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

import docker


def worldserver():
    world = docker.from_env().containers.list(filters={'label': [
        'com.docker.compose.project=agent-wow-world',
        'com.docker.compose.service=worldserver',
    ]})
    if len(world) != 1:
        raise RuntimeError('Expected exactly one worldserver container.')
    return world[0]


def ensure_account(root=Path('/workspace/config'), username=None, env=None):
    """Reuse saved credentials; a supplied username requires that exact account."""
    os.umask(0o077)
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / 'auth.json'
    if not path.exists():
        path.write_text(json.dumps({'username': username or 'AGENT', 'password': secrets.token_hex(8)}))
    path.chmod(0o600)
    account = json.loads(path.read_text())
    if (any(not isinstance(account.get(key), str) or not account[key].isascii() or not account[key].isalnum()
            for key in ('username', 'password')) or len(account['password']) > 16
            or (username is not None and account['username'] != username)):
        raise ValueError('Invalid game account file; credentials were not changed.')

    def client(*args):
        return subprocess.run(['agent-wow', *args], env=env, capture_output=True, text=True, timeout=30)

    if client('auth', 'login', '--timeout', '10s').returncode:
        connection = worldserver().attach_socket(params={'stdin': True, 'stream': True})
        try:
            connection._sock.sendall(f"account create {account['username']} {account['password']}\n".encode())
        finally:
            connection.close()
        for _ in range(30):
            if client('auth', 'login', '--timeout', '10s').returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError('Game account login failed; credentials were not changed.')
    return account, client


if __name__ == '__main__':
    ensure_account()
    print('Game account ready. agent-wow is authenticated on the local realm.')
