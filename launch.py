#!/usr/bin/env python3
"""Launch and watch an AWS Batch run; optionally rebuild or watch an existing job.

Usage: python3 launch.py [--rebuild] ["prompt"]
       python3 launch.py --watch JOB_ID
CLAUDE_MODEL selects an Anthropic model through OpenRouter (default: Opus 5.5).
CLAUDE_EFFORT sets reasoning effort (default: high).
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

from jobs import database, record_job


class AwsError(RuntimeError):
    def __init__(self, message, returncode=1):
        super().__init__(message)
        self.returncode = returncode


TRANSIENT_ERRORS = (
    'unexpected_eof_while_reading', 'eof occurred in violation of protocol',
    'connection reset', 'connection aborted', 'connection was closed',
    'could not connect to the endpoint', 'remote end closed connection',
    'timed out', 'timeout on endpoint', 'temporary failure in name resolution',
    'throttling', 'serviceunavailable',
)


def aws(region, *args):
    for attempt, delay in enumerate((2, 4, 8, 16, 30, None), start=1):
        try:
            return aws_request(region, *args)
        except AwsError as error:
            if delay is None or not any(marker in str(error).lower() for marker in TRANSIENT_ERRORS):
                raise
            print(f'Temporary AWS request failure; retrying in {delay}s '
                  f'({attempt}/5 retries).', file=sys.stderr, flush=True)
            time.sleep(delay)


def aws_request(region, *args):
    try:
        result = subprocess.run(
            ['aws', *args, '--region', region, '--output', 'json', '--no-cli-pager'],
            capture_output=True, text=True, timeout=40,
        )
    except subprocess.TimeoutExpired as error:
        raise AwsError('AWS CLI request timed out.') from error
    except OSError as error:
        raise AwsError(str(error)) from error
    if result.returncode:
        raise AwsError(result.stderr.strip() or 'AWS CLI request failed.', result.returncode)
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise AwsError('AWS CLI returned an invalid JSON response.') from error


def drain_logs(region, log_group, stream, token):
    # Empty pages can still have a continuation token. Stop only at token equality.
    # Bound each pass so busy logs cannot prevent checking the job's status.
    for _ in range(20):
        args = ['logs', 'get-log-events', '--log-group-name', log_group,
                '--log-stream-name', stream, '--start-from-head']
        if token is not None:
            args += ['--next-token', token]
        try:
            page = aws(region, *args)
        except AwsError as error:
            if 'ResourceNotFoundException' in str(error):
                return token, True  # The stream may not exist yet during startup.
            raise
        for event in page.get('events', []):
            message = event['message']
            print(message, end='' if message.endswith('\n') else '\n', flush=True)
        next_token = page['nextForwardToken']
        if next_token == token:
            return token, True
        token = next_token
    return token, False


def follow(job_id, region, log_group, poll_seconds=5, final_wait_seconds=15):
    print(f'Watching job {job_id}. Ctrl+C stops watching without cancelling it.', flush=True)
    last_status = stream = token = terminal_at = None
    missing_polls = 0
    while True:
        response = aws(
            region, 'batch', 'describe-jobs', '--jobs', job_id, '--query',
            '{jobs: jobs[].{status:status,statusReason:statusReason,'
            'container:{logStreamName:container.logStreamName,'
            'exitCode:container.exitCode,reason:container.reason}}}',
        )
        jobs = response.get('jobs', [])
        if not jobs:
            missing_polls += 1
            if missing_polls >= 6:
                raise AwsError(f'Job {job_id} was not found in {region}.')
            time.sleep(poll_seconds)
            continue
        missing_polls = 0
        job = jobs[0]
        status = job['status']
        if status != last_status:
            print(f'Status: {status}', flush=True)
            last_status = status
        terminal = status in ('SUCCEEDED', 'FAILED')
        if terminal and terminal_at is None:
            terminal_at = time.monotonic()
        container = job.get('container') or {}
        new_stream = container.get('logStreamName')
        if new_stream and new_stream != stream:
            stream, token = new_stream, None
            if terminal:
                terminal_at = time.monotonic()
        caught_up = True
        if stream:
            previous_token = token
            token, caught_up = drain_logs(region, log_group, stream, token)
            if terminal and token != previous_token:
                terminal_at = time.monotonic()
        # Allow late CloudWatch delivery after exit; finish only after draining it.
        if terminal and caught_up and (
            time.monotonic() - terminal_at >= final_wait_seconds
        ):
            exit_code = container.get('exitCode')
            suffix = f' (container exit {exit_code})' if exit_code is not None else ''
            print(f'Job {status}{suffix}.', flush=True)
            for reason in dict.fromkeys([job.get('statusReason'), container.get('reason')]):
                if reason:
                    print(reason, flush=True)
            return 0 if status == 'SUCCEEDED' else 1
        time.sleep(poll_seconds)


def container_overrides(prompt):
    model = os.environ.get('CLAUDE_MODEL', 'anthropic/claude-opus-5.5')
    effort = os.environ.get('CLAUDE_EFFORT', 'high')
    if not re.fullmatch(r'anthropic/[A-Za-z0-9_./:~+\[\]-]+', model):
        raise ValueError('CLAUDE_MODEL must be an Anthropic OpenRouter model ID.')
    if effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('CLAUDE_EFFORT must be low, medium, high, xhigh, or max.')
    overrides = {'environment': [
        {'name': 'CLAUDE_MODEL', 'value': model},
        {'name': 'CLAUDE_EFFORT', 'value': effort},
    ]}
    if prompt is not None:
        if not prompt.strip():
            raise ValueError('Prompt must not be blank.')
        overrides['command'] = ['run-wow-agent', prompt]
    return overrides


def submit_job(region, name, definition, overrides, queue='agent-wow'):
    # Check the database before submitting; never retry a submission automatically.
    with database():
        pass
    submission = aws_request(region, 'batch', 'submit-job', '--job-name', name,
                             '--job-queue', queue, '--job-definition', definition,
                             '--container-overrides', json.dumps(overrides))
    try:
        record_job({**submission, 'jobName': name, 'jobDefinition': definition,
                    'createdAt': int(time.time() * 1000), 'status': 'SUBMITTED',
                    'container': overrides}, region)
    except (OSError, sqlite3.Error):
        print(f'Job {submission["jobId"]} was submitted but could not be recorded. '
              'Save this job ID; the job is already running or queued. Do not resubmit it.', file=sys.stderr)
        raise
    return submission


def rebuild(region):
    root = Path(__file__).resolve().parent
    tofu = shutil.which(os.environ.get('WOW_TOFU') or 'tofu')
    if not tofu:
        raise ValueError('OpenTofu not found. On macOS, run brew install opentofu; '
                         'otherwise install tofu or set WOW_TOFU to its executable path.')
    repository = subprocess.check_output([tofu, 'output', '-raw', 'ecr_repository_url'], cwd=root, text=True).strip()
    password = subprocess.check_output(['aws', 'ecr', 'get-login-password', '--region', region], text=True)
    subprocess.run(['docker', 'login', '--username', 'AWS', '--password-stdin', repository.split('/')[0]],
                   input=password, text=True, cwd=root, check=True)
    # Build the local Dockerfile; application source is fetched from the pushed Git commit.
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    tag = time.strftime('build-%Y%m%dT%H%M%SZ', time.gmtime())
    subprocess.run(['docker', 'buildx', 'build', '--platform', 'linux/amd64',
                    '--build-arg', f'AGENT_WOW_REF={commit}', '--tag', f'{repository}:{tag}', '--push', '.'],
                   cwd=root, check=True)
    (root / 'terraform.tfvars').write_text(f'aws_region = {json.dumps(region)}\nimage_tag = {json.dumps(tag)}\n')
    subprocess.run([tofu, 'apply', '-auto-approve', '-input=false'], cwd=root, check=True)
    return subprocess.check_output([tofu, 'output', '-raw', 'job_definition'], cwd=root, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--rebuild', action='store_true', help='Build/push the image and apply OpenTofu before launching.')
    mode.add_argument('--watch', metavar='JOB_ID', help='Watch an existing job without launching another.')
    parser.add_argument('prompt', nargs='?', help='Override the image default task; quote as one argument.')
    parser.add_argument('--queue', default='agent-wow', help='Batch queue; agent-wow-test has higher priority.')
    parser.add_argument('--region', default='us-west-2')
    parser.add_argument('--log-group', default='/aws/batch/agent-wow')
    args = parser.parse_args()
    if args.watch is not None and (not args.watch.strip() or args.prompt is not None):
        parser.error('--watch requires a nonempty job ID and does not accept a prompt.')
    job_id = args.watch
    try:
        if not job_id:
            overrides = container_overrides(args.prompt)
            definition = rebuild(args.region) if args.rebuild else os.environ.get('WOW_JOB_DEFINITION', 'agent-wow')
            submission = submit_job(args.region, 'agent-wow', definition, overrides, args.queue)
            job_id = submission['jobId']
            print(json.dumps(submission), flush=True)
        return follow(job_id, args.region, args.log_group)
    except KeyboardInterrupt:
        message = f'Stopped watching {job_id}. The AWS job was not cancelled.' if job_id else 'Stopped.'
        print('\n' + message, flush=True)
        return 130
    except AwsError as error:
        if job_id:
            print(f'Watching stopped: {error}\nJob {job_id} was not cancelled.', file=sys.stderr)
        else:
            print(f'Launch failed: {error}', file=sys.stderr)
        return error.returncode
    except (OSError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(f'Launch failed: {error}', file=sys.stderr)
        return error.returncode if isinstance(error, subprocess.CalledProcessError) else 1
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    sys.exit(main())
