#!/usr/bin/env python3
"""Follow one AWS Batch job's status and logs without changing the job."""

import argparse
import json
import subprocess
import sys
import time


class AwsError(RuntimeError):
    pass


def aws(region, *args):
    try:
        result = subprocess.run(
            ['aws', *args, '--region', region, '--output', 'json', '--no-cli-pager'],
            capture_output=True, text=True, timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AwsError(str(error)) from error
    if result.returncode:
        raise AwsError(result.stderr.strip() or 'AWS CLI request failed.')
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--region', default='us-west-2')
    parser.add_argument('--log-group', default='/aws/batch/agent-wow')
    args = parser.parse_args()
    try:
        return follow(args.job_id, args.region, args.log_group)
    except KeyboardInterrupt:
        print(f'\nStopped watching {args.job_id}. The AWS job was not cancelled.', flush=True)
        return 130
    except AwsError as error:
        print(f'Watching stopped: {error}\nJob {args.job_id} was not cancelled.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
