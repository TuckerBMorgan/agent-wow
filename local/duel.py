#!/usr/bin/env python3
"""Provision both selected builds, stage them in Gurubashi, and run one duel.

Run python3 local/duel.py [--check] [--seconds 180]. Results: tmp/local/duels/.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import secrets
import time

import provision
from provision import rpc


def staged(state, player):
    actor = state.get('self', {})
    position = actor.get('position')
    return (state.get('ready') and state.get('name') == player['name']
            and actor.get('guid') == player['guid'] and actor.get('health', 0) > 0
            and actor['health'] == actor.get('max_health')
            and actor.get('power') == (0 if actor.get('power_type') in (1, 6) else actor.get('max_power'))
            and state.get('map_id') == 0 and position
            and math.dist(position[:2], (-13277.4, 127.372)) < 5)


def winner(states, players):
    winners = {s['match']['winner'] for s in states if s['match'].get('winner')}
    if len(winners) > 1 or winners - {p['name'] for p in players}:
        raise RuntimeError('The two clients reported inconsistent duel winners')
    return next(iter(winners)) if winners and all(s['match'].get('winner') for s in states) else None


def fight(ids, players, seconds):
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3)
    output = provision.ROOT / 'tmp/local/duels' / run_id
    output.mkdir(parents=True)
    containers = [ids[c] for c in provision.CLIENTS]
    result = {'run_id': run_id, 'started_at': datetime.now(timezone.utc).isoformat(),
              'players': players, 'outcome': 'error', 'winner': None}
    started = time.monotonic()
    states = []

    def observe(log, connecting=False):
        try:
            states = [rpc(c, 'duel.status') for c in containers]
        except RuntimeError:
            if connecting:  # Login starts the RPC server asynchronously.
                return []
            raise
        for state, player in zip(states, players):
            position = state.get('self', {}).get('position')
            if position:
                log.write(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(),
                    'run_id': run_id, 'event': 'position', 'character': player['name'],
                    'map_id': int(state['map_id']), 'x': position[0], 'y': position[1], 'z': position[2],
                    'elapsed_ms': (time.monotonic() - started) * 1000, 'state': state}) + '\n')
        log.flush()
        errors = [s['error'] for s in states if s.get('error')]
        if errors:
            raise RuntimeError('; '.join(errors))
        return states

    try:
        with (output / 'states.jsonl').open('w') as log:
            log.write(json.dumps({'timestamp': result['started_at'], 'run_id': run_id,
                                  'event': 'task_started', 'character': None}) + '\n')
            log.flush()
            result['harness_sha256'] = {p['name']: provision.docker('exec', c, 'sha256sum',
                '/workspace/modules/duel/harness.py').decode().split()[0] for p, c in zip(players, containers)}
            deadline = time.monotonic() + 90
            while True:
                states = observe(log, connecting=True)
                if states and all(staged(s, p) for s, p in zip(states, players)):
                    result['initial_states'] = states
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Both selected characters did not become ready in Gurubashi Arena')
                time.sleep(0.5)
            for index, container in enumerate(containers):
                rpc(container, 'duel.prepare', {'opponent': players[1 - index]['guid']})
            deadline = time.monotonic() + 10  # Equal time for the two harnesses' pre-duel buffs.
            while time.monotonic() < deadline:
                states = observe(log)
                time.sleep(0.5)
            if not all(s.get('opponent', {}).get('known') and s.get('prepared') for s in states):
                raise RuntimeError('The two harnesses did not become ready with a visible opponent')
            rpc(containers[0], 'duel.challenge')
            print('Duel requested: ' + ' vs '.join(p['name'] for p in players), flush=True)
            deadline = time.monotonic() + 15
            while True:
                states = observe(log)
                if all(s['match']['phase'] == 'fighting' for s in states) or winner(states, players):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('The server did not start the duel')
                time.sleep(0.25)
            deadline = time.monotonic() + seconds
            while True:
                states = observe(log)
                result['winner'] = winner(states, players)
                if result['winner']:
                    result['outcome'] = 'finished'
                    break
                if all(s['match']['phase'] == 'finished' for s in states):
                    raise RuntimeError('The server interrupted the duel without a winner')
                if time.monotonic() >= deadline:
                    result['outcome'] = 'draw'
                    result['reason'] = 'time_limit'
                    break
                time.sleep(0.5)
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        result['states'] = states
        result['elapsed_ms'] = (time.monotonic() - started) * 1000
        for container in containers:
            try:
                rpc(container, 'duel.stop')
            except Exception:
                result.setdefault('cleanup_errors', []).append('Could not stop harness in ' + container)
        provision.save(output / 'result.json', result)
        with (output / 'states.jsonl').open('a') as log:
            log.write(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), 'run_id': run_id,
                'event': 'task_finished', 'character': None, 'elapsed_seconds': result['elapsed_ms'] / 1000,
                'outcome': 'completed' if result['outcome'] != 'error' and not result.get('cleanup_errors') else 'failed'}) + '\n')
        print('Result: ' + str(output / 'result.json'), flush=True)
    if result.get('cleanup_errors'):
        raise RuntimeError('Could not stop both harnesses; inspect result.json')
    print('Winner: ' + result['winner'] if result['winner'] else 'Draw: time limit reached', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate both builds without provisioning or fighting')
    parser.add_argument('--seconds', type=int, default=180, help='Maximum fighting time (default: 180 seconds)')
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    provision.run(check=args.check, on_ready=lambda ids, players: fight(ids, players, args.seconds))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error))
