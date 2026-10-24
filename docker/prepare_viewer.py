#!/opt/venv/bin/python3
"""Prepare the private spectator account; never touch the agent's account."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import boto3
import docker


def prepare_image():
    image = os.environ['AGENT_WOW_WORLD_IMAGE']
    registry = image.split('/')[0]
    if not registry.endswith('.amazonaws.com') or '.dkr.ecr.' not in registry:
        raise ValueError('The spectator world image must be in private ECR.')
    token = boto3.client('ecr').get_authorization_token()['authorizationData'][0]['authorizationToken']
    username, password = base64.b64decode(token).decode().split(':', 1)
    subprocess.run(['docker', 'login', '--username', username, '--password-stdin', registry],
                   input=password, text=True, stdout=subprocess.DEVNULL, check=True)


def prepare_account(root=Path('/run/agent-wow-observer'), instructions=Path('/workspace/AGENTS.md')):
    root.mkdir(mode=0o700, exist_ok=True)
    auth = root / 'auth.json'
    if not auth.exists():
        # AzerothCore's legacy account protocol limits passwords to 16 characters.
        auth.write_text(json.dumps({'username': 'WATCHER', 'password': secrets.token_hex(8)}))
        auth.chmod(0o600)
    account = json.loads(auth.read_text())
    if account.get('username') != 'WATCHER' or not account.get('password', '').isascii() or not account.get('password', '').isalnum() or len(account['password']) > 16:
        raise ValueError('Invalid observer account file.')
    env = dict(os.environ, AGENT_WOW_CONFIG_DIR=str(root), AGENT_WOW_DATA_DIR=str(root / 'data'))

    def client(*args):
        return subprocess.run(['agent-wow', *args], env=env, capture_output=True, text=True, timeout=30)

    world, = docker.from_env().containers.list(filters={'label': [
        'com.docker.compose.project=agent-wow-world', 'com.docker.compose.service=worldserver']})
    # Use the server's supported console commands, never SQL or game-state edits.
    if client('auth', 'login', '--timeout', '10s').returncode:
        connection = world.attach_socket(params={'stdin': True, 'stream': True})
        try:
            connection._sock.sendall(f"account create {account['username']} {account['password']}\n".encode())
        finally:
            connection.close()
        for _ in range(30):
            time.sleep(1)
            if client('auth', 'login', '--timeout', '10s').returncode == 0:
                break
        else:
            raise RuntimeError('Observer authentication failed; credentials omitted.')
    # Authentication confirms account creation has committed before granting access.
    connection = world.attach_socket(params={'stdin': True, 'stdout': True, 'stream': True})
    try:
        connection._sock.settimeout(30)
        connection._sock.sendall(b'account set gmlevel WATCHER 2 -1\n')
        reply = b''
        deadline = time.monotonic() + 30
        while b'security level of account WATCHER to 2.' not in reply:
            if time.monotonic() >= deadline:
                raise RuntimeError('Observer permission confirmation timed out.')
            chunk = connection._sock.recv(16384)
            if not chunk:
                raise RuntimeError('Observer permissions were not confirmed.')
            reply = (reply + chunk)[-65536:]
    finally:
        connection.close()
    listing = client('char', 'list', '--json')
    if listing.returncode:
        raise RuntimeError('Observer character listing failed.')
    if not json.loads(listing.stdout)['characters']:
        created = client('char', 'create', '--name', 'Observer', '--race', 'orc', '--class', 'warrior')
        if created.returncode:
            raise RuntimeError('Observer creation failed; existing characters were preserved.')
    boto3.client('s3').put_object(
        Bucket=os.environ['AWS_RESULTS_BUCKET'], Key=f"viewer/{os.environ['AWS_BATCH_JOB_ID']}/account.json",
        Body=json.dumps({**account, 'character': 'Observer'}).encode())
    note = '\nA separate GM character named Observer belongs to the browser viewer. Leave it alone; '
    note += 'your task applies only to characters on your regular AGENT account.\n'
    if note not in instructions.read_text():
        with instructions.open('a') as stream:
            stream.write(note)
    print('Browser observer account ready; the agent remains a regular player.', flush=True)


if __name__ == '__main__' and os.environ.get('AGENT_WOW_VIEWER') == '1':
    os.umask(0o077)
    if sys.argv[1:] == ['--image']:
        prepare_image()
    elif sys.argv[1:] == ['--account']:
        prepare_account()
    else:
        raise SystemExit('Usage: prepare-wow-viewer --image|--account')
