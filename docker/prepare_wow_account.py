#!/opt/venv/bin/python
"""Create a game account through the world console and confirm authentication."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import docker


def worldserver():
    world = docker.from_env().containers.list(filters={'label': [
        'com.docker.compose.project=agent-wow-world',
        'com.docker.compose.service=worldserver',
    ]})
    return world[0]


def ensure_account(root=Path('/workspace/config'), username=None, env=None):
    """Reuse saved credentials; a supplied username requires that exact account."""
    os.umask(0o077)
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    path = root / 'auth.json'
    if not path.exists():
        path.write_text(json.dumps({
            'username': username or 'AGENT',
            'password': secrets.token_hex(8),
        }))
    path.chmod(0o600)
    account = json.loads(path.read_text())

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

def ensure_viewer(root=Path('/run/agent-wow-observer')):
    env = dict(
        os.environ,
        AGENT_WOW_CONFIG_DIR=str(root),
        AGENT_WOW_DATA_DIR=str(root / 'data'),
    )
    account, client = ensure_account(root, 'WATCHER', env)

    connection = worldserver().attach_socket(
        params={'stdin': True, 'stdout': True, 'stream': True}
    )
    try:
        connection._sock.settimeout(30)
        connection._sock.sendall(b'account set gmlevel WATCHER 2 -1\n')
        reply = b''
        deadline = time.monotonic() + 30
        while b'security level of account WATCHER to 2.' not in reply:
            if time.monotonic() >= deadline:
                raise RuntimeError('Viewer permission confirmation timed out.')
            chunk = connection._sock.recv(16384)
            if not chunk:
                raise RuntimeError('Viewer permissions were not confirmed.')
            reply = (reply + chunk)[-65536:]
    finally:
        connection.close()

    listing = client('char', 'list', '--json')
    if listing.returncode:
        raise RuntimeError('Viewer character listing failed.')
    characters = json.loads(listing.stdout)['characters']
    if not any(character['name'] == 'Observer' for character in characters):
        created = client(
            'char', 'create', '--name', 'Observer',
            '--race', 'orc', '--class', 'warrior',
        )
        if created.returncode:
            raise RuntimeError('Viewer character creation failed.')

    connection_path = root / 'connection.json'
    connection_path.write_text(json.dumps({
        **account,
        'auth_port': 3724,
        'world_port': 8085,
        'character': 'Observer',
    }, indent=2) + '\n')
    connection_path.chmod(0o600)
    return connection_path


if __name__ == '__main__':
    if sys.argv[1:] == ['--player']:
        ensure_account()
    elif sys.argv[1:] == ['--viewer']:
        print(ensure_viewer())
    else:
        raise SystemExit('Usage: prepare_wow_account.py --player|--viewer')
