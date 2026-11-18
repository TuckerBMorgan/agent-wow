#!/usr/bin/env python3
"""Import the two selected canonical characters and hold the lock through their duel."""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import select
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'tmp/local/provision'
BUILDS = ROOT / 'local/builds'
CLIENTS = ('client-1', 'client-2')


def docker(*args, data=None, timeout=90):
    result = subprocess.run(['docker', *map(str, args)], input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Docker operation failed: ' + str(args[0]))
    return result.stdout


def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def rpc(container, method, params=None, timeout=10):
    body = json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params or {}))
    reply = json.loads(docker('exec', '--user', 'node', container, 'curl', '-fsS',
        '--max-time', str(timeout), '-H', 'Content-Type: application/json', '-d', body,
        'http://127.0.0.1:8086/rpc'))
    if 'error' in reply:
        raise RuntimeError(method + ' failed: ' + reply['error'].get('message', 'RPC error'))
    return reply['result']


def process_running(container, name):
    return json.loads(docker('exec', container, 'sh', '-c',
        'pgrep -x "$1" >/dev/null && echo true || echo false', 'sh', name))


def character_name(prefix='Duel'):
    return prefix + ''.join(secrets.SystemRandom().sample('bcdfhjklmnpqrstvwxyz', 12 - len(prefix)))


class Console:
    def __init__(self, container):
        self.process = subprocess.Popen(['docker', 'attach', '--sig-proxy=false', container],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def command(self, command):
        if '\n' in command or '\r' in command:
            raise ValueError('Console command must be one line')
        marker = character_name('Fence')
        end = ("Character '" + marker + "' does not exist.").encode()
        self.process.stdin.write((command + '\npinfo ' + marker + '\n').encode())
        self.process.stdin.flush()
        reply, deadline = b'', time.monotonic() + 30
        # A unique read-only command marks completion despite unrelated console output.
        while end not in reply:
            if not select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                raise RuntimeError('World console timed out')
            part = os.read(self.process.stdout.fileno(), 65536)
            if not part:
                raise RuntimeError('World console disconnected')
            reply += part
        return reply.decode(errors='replace')

    def close(self):
        self.process.terminate()  # --sig-proxy=false keeps the server running.
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()


def run(check=False, on_ready=None):
    STATE.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((BUILDS / 'index.json').read_text())
    choices = {'builds/' + entry['build']: entry for entry in catalog['builds']}
    with (STATE / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        compose = ['compose', '--env-file', ROOT / 'tmp/local/.env', '-f', ROOT / 'local/compose.yaml']
        ids = {service: docker(*compose, 'ps', '-q', service).decode().strip()
               for service in (*CLIENTS, 'worldserver')}
        if not all(ids.values()):
            raise ValueError('Start the local server and both clients with local/run.py up first')
        plans = []
        for client in CLIENTS:
            selected = json.loads(docker('exec', '--user', 'node', ids[client], 'cat', '/workspace/next-match.json'))
            if not isinstance(selected, dict) or not isinstance(selected.get('build'), str) or selected['build'] not in choices:
                raise ValueError(client + ': select a supplied builds/<name>/build.json')
            account = docker('exec', ids[client], 'printenv', 'WOW_ACCOUNT').decode().strip()
            if not re.fullmatch(r'[A-Z0-9]{1,16}', account):
                raise ValueError('Unexpected local player account name')
            plans.append((client, account, choices[selected['build']]))
        print(json.dumps([dict(client=c, build=e['build']) for c, _, e in plans], indent=2), flush=True)
        if check:
            return
        if any(process_running(ids[c], 'codex') for c in CLIENTS):
            raise ValueError('Finish and exit Codex in both clients before starting a match')
        for client in CLIENTS:
            if process_running(ids[client], 'agent-wow'):
                rpc(ids[client], 'session.logout', timeout=40)
        console = Console(ids['worldserver'])
        created, players = [], []
        try:
            for client, account, entry in plans:
                name = character_name()
                reply = console.command('pdump load /duel-builds/' + entry['template'] + ' ' + account + ' ' + name)
                if 'Character loaded successfully!' not in reply:
                    raise RuntimeError('Native template import failed for ' + entry['build'])
                created.append((client, name))
                player = dict(name=name, build='builds/' + entry['build'], template_sha256=entry['sha256'], catalog_version=catalog['version'])
                target = STATE / (client + '-character.json')
                save(target, player)
                target.chmod(0o644)
                docker('cp', target, ids[client] + ':/workspace/character.json')
                docker('exec', '-d', '--user', 'node', '--workdir', '/workspace', ids[client], '/usr/local/bin/local-client')
                players.append(player)
                print(client + ': imported ' + entry['build'] + ' as ' + name, flush=True)
            if on_ready:
                on_ready(ids, players)
        finally:
            errors = []
            try:
                for client, name in created:
                    try:
                        try:
                            if process_running(ids[client], 'agent-wow'):
                                rpc(ids[client], 'session.logout', timeout=40)
                        except RuntimeError:
                            pass  # Native erase also kicks an unresponsive game session.
                        reply = console.command('character erase ' + name)
                        if 'deleted' not in reply.lower():
                            raise RuntimeError('Native character removal failed')
                        docker('exec', '--user', 'node', ids[client], 'rm', '-f', '/workspace/character.json')
                    except Exception:
                        errors.append(name)
            finally:
                console.close()
            if errors:
                raise RuntimeError('Could not clean up match characters: ' + ', '.join(errors))


if __name__ == '__main__':
    raise SystemExit('Use python3 local/duel.py [--check] [--seconds 180]')
