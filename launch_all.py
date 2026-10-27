#!/usr/bin/env python3
"""Queue every race/class pairing; AWS Batch starts jobs as capacity opens.

Uses the existing image and the same model/effort settings as launch.py.
Each invocation submits a fresh set of jobs.
"""

import argparse
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from benchmark_results import create_run
from launch import AwsError, JOB_DEFINITIONS, container_overrides, submit_job
from race_classes import RACE_CLASSES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', choices=JOB_DEFINITIONS,
                        default=os.environ.get('AGENT_WOW_HARNESS', 'codex'))
    parser.add_argument('--region', default='us-west-2')
    parser.add_argument('--benchmark', choices=('starting-area', 'level-10'), default='starting-area')
    args = parser.parse_args()
    try:
        overrides = container_overrides(None, args.harness)
        name = JOB_DEFINITIONS[args.harness]
        definition = os.environ.get('WOW_JOB_DEFINITION', name)
        if args.benchmark == 'level-10':
            configs = Path(__file__).resolve().parent / 'config'
            configs.mkdir(exist_ok=True)
            configs = Path(tempfile.mkdtemp(prefix='level-10-', dir=configs))
        for race, classes in RACE_CLASSES.items():
            for character_class in classes:
                if args.benchmark == 'level-10':
                    config = configs / f'{race}-{character_class}.json'
                    create_run(race, character_class, 10, config)
                    overrides = container_overrides(None, args.harness, config)
                else:
                    prompt = (f'Create one {race.replace("-", " ")} {character_class.replace("-", " ")} '
                              'and complete the starting area.')
                    overrides['command'] = ['run-wow-agent', prompt]
                job = submit_job(args.region, f'{name}-{args.benchmark}-{race}-{character_class}', definition, overrides)
                print(f'{race} / {character_class}: {job["jobId"]}', flush=True)
        print('All pairings queued. AWS Batch will run them as capacity becomes available.')
        return 0
    except KeyboardInterrupt:
        print('\nStopped submitting. Already-submitted jobs were not cancelled.', file=sys.stderr)
        return 130
    except (AwsError, OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(f'Submission stopped: {error}\nAlready-submitted jobs were not cancelled.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
