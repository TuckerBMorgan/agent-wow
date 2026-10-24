#!/usr/bin/env python3
"""A small, automatically refreshing dashboard for active AWS Batch jobs."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from launch import AwsError, aws_request


STATES = ('SUBMITTED', 'PENDING', 'RUNNABLE', 'STARTING', 'RUNNING')
COLORS = {'RUNNING': '32', 'STARTING': '36'}
OPENROUTER_ENV = Path(__file__).resolve().parent / 'config/openrouter.env'


def openrouter_credits():
    # Read the local key as data; never source the file or print HTTP error bodies.
    try:
        key = os.environ.get('OPENROUTER_API_KEY', '').strip()
        if not key and OPENROUTER_ENV.is_file():
            values = [line.partition('=')[2].strip() for line in OPENROUTER_ENV.read_text().splitlines()
                      if line.partition('=')[0].strip() == 'OPENROUTER_API_KEY']
            key = values[0] if len(values) == 1 else ''
        if not key:
            return 'not configured (config/openrouter.env)'
        request = Request('https://openrouter.ai/api/v1/credits',
                          headers={'Authorization': f'Bearer {key}'})
        with urlopen(request, timeout=5) as response:
            data = json.load(response)['data']
        remaining = float(data['total_credits']) - float(data['total_usage'])
        if not math.isfinite(remaining):
            raise ValueError
        return f'${remaining:,.2f}'
    except HTTPError as error:
        return f'unavailable (HTTP {error.code})'
    except (OSError, ValueError, KeyError, TypeError):
        return 'unavailable (lookup failed)'


def active_jobs(region):
    def fetch(state):
        return aws_request(region, 'batch', 'list-jobs', '--job-queue', 'agent-wow',
                           '--job-status', state, '--query',
                           'jobSummaryList[].{id:jobId,name:jobName,status:status,created:createdAt}')

    with ThreadPoolExecutor(max_workers=5) as pool:
        # A job can appear twice if its status changes between requests.
        jobs = {job['id']: job for page in pool.map(fetch, STATES) for job in page}
    return sorted(jobs.values(), key=lambda job: (-STATES.index(job['status']), job['created']))


def dashboard(jobs, region, interval, color=False, credits='not checked'):
    width = shutil.get_terminal_size((100, 24)).columns
    id_width = 36 if width >= 110 else 8
    name_width = max(16, min(45, width - id_width - 27))
    running = sum(job['status'] == 'RUNNING' for job in jobs)
    starting = sum(job['status'] == 'STARTING' for job in jobs)
    queued = len(jobs) - running - starting
    lines = [
        'AGENT WOW  |  AWS Batch',
        f'{running} running   {starting} starting   {queued} queued',
        f'OpenRouter credits: {credits}',
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
    credits, next_credit_refresh = 'not checked', 0
    try:
        if terminal:
            print('\033[?25l', end='', flush=True)
        while True:
            if time.monotonic() >= next_credit_refresh:
                credits = openrouter_credits()
                next_credit_refresh = time.monotonic() + 30
            try:
                screen = dashboard(active_jobs(args.region), args.region, args.interval,
                                   color=terminal, credits=credits)
            except AwsError as error:
                screen = (f'AGENT WOW  |  Could not refresh\nOpenRouter credits: {credits}'
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
