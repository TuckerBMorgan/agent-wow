#!/opt/venv/bin/python
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import docker

os.umask(0o077)
path = Path('/workspace/config/auth.json')
if not path.exists():
    path.write_text(json.dumps({'username': 'AGENT', 'password': secrets.token_hex(8)}))
account = json.loads(path.read_text())
path.chmod(0o600)

def login():
    return subprocess.run(['agent-wow', 'auth', 'login', '--timeout', '10s'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

if not login():
    client = docker.from_env()
    world = client.containers.list(filters={'label': [
        'com.docker.compose.project=agent-wow-world',
        'com.docker.compose.service=worldserver',
    ]})
    if len(world) != 1:
        raise SystemExit('Expected exactly one worldserver container.')
    # These are generated credentials, restricted to console-safe characters.
    if not all(value.isascii() and value.isalnum() for value in account.values()):
        raise SystemExit('The game account file contains unsupported console characters.')
    connection = world[0].attach_socket(params={'stdin': True, 'stream': True})
    try:
        command = f"account create {account['username']} {account['password']}\n"
        connection._sock.sendall(command.encode())
    finally:
        connection.close()
    for _ in range(30):
        if login():
            break
        time.sleep(2)
    else:
        raise SystemExit('Game account login failed. Inspect the worldserver console; credentials were not changed.')
print('Game account ready. agent-wow is authenticated on the local realm.')
