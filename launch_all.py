#!/usr/bin/env python3
"""Queue every race/class pairing; AWS Batch starts jobs as capacity opens.

Uses the existing image and the same CLAUDE_* settings as launch.py.
Each invocation submits a fresh set of jobs.
"""

import argparse
import os
import sqlite3
import sys

from launch import AwsError, container_overrides, submit_job
from race_classes import RACE_CLASSES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region', default='us-west-2')
    args = parser.parse_args()
    definition = os.environ.get('WOW_JOB_DEFINITION', 'agent-wow')
    try:
        overrides = container_overrides(None)
        for race, classes in RACE_CLASSES.items():
            for character_class in classes:
                prompt = (f'Create one {race.replace("-", " ")} {character_class.replace("-", " ")} '
                          'and complete the starting area.')
                overrides['command'] = ['run-wow-agent', prompt]
                job = submit_job(args.region, f'agent-wow-{race}-{character_class}', definition, overrides)
                print(f'{race} / {character_class}: {job["jobId"]}', flush=True)
        print('All pairings queued. AWS Batch will run them as capacity becomes available.')
        return 0
    except KeyboardInterrupt:
        print('\nStopped submitting. Already-submitted jobs were not cancelled.', file=sys.stderr)
        return 130
    except (AwsError, OSError, ValueError, sqlite3.Error) as error:
        print(f'Submission stopped: {error}\nAlready-submitted jobs were not cancelled.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
