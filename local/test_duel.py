import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duel


PLAYERS = [{'name': 'Alpha', 'guid': '10'}, {'name': 'Beta', 'guid': '20'}]


def state(index, phase='waiting', winner=None):
    return {'ready': True, 'prepared': True, 'name': PLAYERS[index]['name'], 'map_id': 0,
            'self': {'guid': PLAYERS[index]['guid'], 'health': 100, 'max_health': 100,
                     'power_type': 6, 'power': 0, 'max_power': 1000,
                     'position': [-13277.4, 127.372, 26.1418, 1]},
            'opponent': {'known': True}, 'match': {'phase': phase, 'winner': winner}, 'error': ''}


class DuelTests(unittest.TestCase):
    def test_staging_requires_the_correct_live_character_and_arena(self):
        valid = state(0)
        self.assertTrue(duel.staged(valid, PLAYERS[0]))
        for changes in ({'map_id': 1}, {'name': 'Other'}, {'ready': False}):
            self.assertFalse(duel.staged(dict(valid, **changes), PLAYERS[0]))
        for changes in ({'guid': '99'}, {'health': 0}, {'health': 50}, {'power': 100}, {'position': [0, 0, 0, 0]}):
            changed = copy.deepcopy(valid)
            changed['self'].update(changes)
            self.assertFalse(duel.staged(changed, PLAYERS[0]))

    def test_winner_requires_matching_server_reports(self):
        self.assertIsNone(duel.winner([state(0, winner='Alpha'), state(1)], PLAYERS))
        self.assertEqual(duel.winner([state(0, winner='Alpha'), state(1, winner='Alpha')], PLAYERS), 'Alpha')
        for winners in (('Alpha', 'Beta'), ('Other', 'Other')):
            with self.assertRaises(RuntimeError):
                duel.winner([state(0, winner=winners[0]), state(1, winner=winners[1])], PLAYERS)

    def test_preparation_failure_stops_both_harnesses_and_saves_error(self):
        calls = []
        connecting = True

        def rpc(container, method, params=None):
            nonlocal connecting
            calls.append((container, method))
            if method == 'duel.status':
                if connecting:
                    connecting = False
                    raise RuntimeError('RPC not listening yet')
                return state(0 if container == 'a' else 1)
            if method == 'duel.prepare' and container == 'b':
                raise RuntimeError('start failed')
            return {}

        with tempfile.TemporaryDirectory() as directory, patch.object(duel.provision, 'ROOT', Path(directory)), \
                patch.object(duel, 'rpc', rpc), patch.object(duel.provision, 'docker', return_value=b'checksum file'), \
                patch.object(duel.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'start failed'):
                duel.fight({'client-1': 'a', 'client-2': 'b'}, PLAYERS, 1)
            results = list(Path(directory).rglob('result.json'))
            self.assertEqual(len(results), 1)
            result = json.loads(results[0].read_text())
            self.assertEqual(result['outcome'], 'error')
            self.assertEqual(result['error'], 'start failed')
        self.assertEqual([c for c in calls if c[1] == 'duel.stop'], [('a', 'duel.stop'), ('b', 'duel.stop')])
        self.assertFalse(any(method == 'duel.challenge' for _, method in calls))


if __name__ == '__main__':
    unittest.main()
