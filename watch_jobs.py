#!/usr/bin/env python3
"""A small, automatically refreshing dashboard for active AWS Batch jobs."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import shutil
import sys
import time

from launch import AwsError, aws_request


STATES = ('SUBMITTED', 'PENDING', 'RUNNABLE', 'STARTING', 'RUNNING')
COLORS = {'RUNNING': '32', 'STARTING': '36'}


def active_jobs(region):
    def fetch(state):
        return aws_request(region, 'batch', 'list-jobs', '--job-queue', 'agent-wow',
                           '--job-status', state, '--query',
                           'jobSummaryList[].{id:jobId,name:jobName,status:status,created:createdAt}')

    with ThreadPoolExecutor(max_workers=5) as pool:
        # A job can appear twice if its status changes between requests.
        jobs = {job['id']: job for page in pool.map(fetch, STATES) for job in page}
    return sorted(jobs.values(), key=lambda job: (-STATES.index(job['status']), job['created']))


def dashboard(jobs, region, interval, color=False):
    width = shutil.get_terminal_size((100, 24)).columns
    id_width = 36 if width >= 110 else 8
    name_width = max(16, min(45, width - id_width - 27))
    running = sum(job['status'] == 'RUNNING' for job in jobs)
    starting = sum(job['status'] == 'STARTING' for job in jobs)
    queued = len(jobs) - running - starting
    lines = [
        'AGENT WOW  |  AWS Batch',
        f'{running} running   {starting} starting   {queued} queued',
        '',
        f' {"STATUS":<10} {"JOB":<{name_width}} {"AGE":>8}  JOB ID',
        ' ' + '─' * (name_width + id_width + 23),
    ]
    now = time.time()
    for job in jobs:
        minutes = max(0, int((now - job['created'] / 1000) / 60))
        age = f'{minutes // 60}h {minutes % 60:02}m' if minutes >= 60 else f'{minutes}m'
        name = ''.join(char if char.isprintable() else '?' for char in job['name'])
        if len(name) > name_width:
            name = name[:name_width - 1] + '…'
        status = f'{job["status"]:<10}'
        if color:
            status = f'\033[{COLORS.get(job["status"], "33")}m{status}\033[0m'
        lines.append(f' {status} {name:<{name_width}} {age:>8}  {job["id"][:id_width]}')
    if not jobs:
        lines.append(' No active jobs.')
    lines += ['', f'{region} / agent-wow  ·  Updated {datetime.now():%H:%M:%S}  ·  Refresh {interval:g}s',
              'Age = time since submission. Ctrl+C exits; jobs keep running.']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region', default='us-west-2')
    parser.add_argument('--interval', type=int, default=5, help='Seconds between refreshes (default: 5).')
    args = parser.parse_args()
    if args.interval < 1:
        parser.error('--interval must be at least 1 second.')
    terminal = sys.stdout.isatty()
    try:
        if terminal:
            print('\033[?25l', end='', flush=True)
        while True:
            try:
                screen = dashboard(active_jobs(args.region), args.region, args.interval,
                                   color=terminal)
            except AwsError as error:
                screen = ('AGENT WOW  |  Could not refresh'
                          f'\n\n{error}\n\nRetrying in {args.interval}s. Ctrl+C exits.')
            print(('\033[H\033[2J' if terminal else '') + screen, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if terminal:
            print('\033[?25h', end='', flush=True)


if __name__ == '__main__':
    main()
