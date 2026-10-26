#!/usr/bin/env python3
"""Launch and watch an AWS Batch run; optionally rebuild or watch an existing job.

Usage: python3 launch.py [--harness codex|claude] [--rebuild] ["prompt"]
       python3 launch.py --watch JOB_ID
Both harnesses use your saved account and default to medium effort.
CODEX_MODEL selects the OpenAI model (gpt-6.1-sol by default).
Claude uses CLAUDE_MODEL and CLAUDE_EFFORT (Opus 5.5 and medium by default).
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from jobs import database, record_job
from agent_common import HARNESSES, harness_spec, model_settings


JOB_DEFINITIONS = {harness: f'agent-wow-{harness}-subscription' for harness in HARNESSES}


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
        job = aws(
            region, 'batch', 'describe-jobs', '--jobs', job_id, '--query',
            'jobs[0].{status:status,statusReason:statusReason,'
            'logStreamName:container.logStreamName,'
            'exitCode:container.exitCode,reason:container.reason}',
        )
        if not job:
            missing_polls += 1
            if missing_polls >= 6:
                raise AwsError(f'Job {job_id} was not found in {region}.')
            time.sleep(poll_seconds)
            continue
        missing_polls = 0
        status = job['status']
        if status != last_status:
            print(f'Status: {status}', flush=True)
            last_status = status
        terminal = status in ('SUCCEEDED', 'FAILED')
        if terminal and terminal_at is None:
            terminal_at = time.monotonic()
        new_stream = job.get('logStreamName')
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
            exit_code = job.get('exitCode')
            suffix = f' (container exit {exit_code})' if exit_code is not None else ''
            print(f'Job {status}{suffix}.', flush=True)
            for reason in dict.fromkeys([job.get('statusReason'), job.get('reason')]):
                if reason:
                    print(reason, flush=True)
            return 0 if status == 'SUCCEEDED' else 1
        time.sleep(poll_seconds)


def container_overrides(prompt, harness='codex', benchmark=None):
    spec = harness_spec(harness)
    model, effort = model_settings(harness)
    overrides = {'environment': [
        {'name': 'AGENT_WOW_HARNESS', 'value': harness},
        {'name': spec['model_env'], 'value': model},
        {'name': spec['effort_env'], 'value': effort},
    ]}
    if benchmark:
        config = json.loads(Path(benchmark).read_text())
        if (not isinstance(config, dict) or any(not isinstance(config.get(k), str) or not config[k].strip()
                                               for k in ('url', 'key', 'prompt'))
                or not config['url'].startswith('https://')):
            raise ValueError('Invalid benchmark config: expected HTTPS url, key, and prompt.')
        if prompt is not None and prompt != config['prompt']:
            raise ValueError('The benchmark config fixes the prompt; omit the prompt argument.')
        prompt = config['prompt']
        overrides['environment'] += [{'name': 'BENCHMARK_' + name.upper(), 'value': config[name]}
                                     for name in ('url', 'key')]
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
        record_job(submission['jobId'], name, region, definition, overrides)
    except (OSError, sqlite3.Error):
        print(f'Job {submission["jobId"]} was submitted but could not be recorded. '
              'Save this job ID; the job is already running or queued. Do not resubmit it.', file=sys.stderr)
        raise
    return submission


def rebuild(region, harness='codex'):
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
    # Generated overrides leave the user's infrastructure settings intact.
    variables_path = root / 'launch.auto.tfvars.json'
    variables = json.loads(variables_path.read_text()) if variables_path.exists() else {}
    variables.update(aws_region=region, image_tag=tag)
    variables_path.write_text(json.dumps(variables, indent=2) + '\n')
    subprocess.run([tofu, 'apply', '-auto-approve', '-input=false'], cwd=root, check=True)
    definitions = json.loads(subprocess.check_output(
        [tofu, 'output', '-json', 'job_definitions'], cwd=root, text=True))
    return definitions[harness]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--rebuild', action='store_true', help='Build/push the agent image and apply OpenTofu before launching.')
    mode.add_argument('--watch', metavar='JOB_ID', help='Watch an existing job without launching another.')
    parser.add_argument('prompt', nargs='?', help='Override the image default task; quote as one argument.')
    parser.add_argument('--harness', choices=JOB_DEFINITIONS,
                        default=os.environ.get('AGENT_WOW_HARNESS', 'codex'))
    parser.add_argument('--queue', default='agent-wow', help='Batch queue; agent-wow-test has higher priority.')
    parser.add_argument('--region', default='us-west-2')
    parser.add_argument('--log-group', default='/aws/batch/agent-wow')
    parser.add_argument('--benchmark', type=Path, help='Private run config created by benchmark_results.py.')
    args = parser.parse_args()
    if args.watch is not None and (not args.watch.strip() or args.prompt is not None or args.benchmark):
        parser.error('--watch requires a nonempty job ID and does not accept a prompt or benchmark.')
    job_id = args.watch
    try:
        if not job_id:
            overrides = container_overrides(args.prompt, args.harness, args.benchmark)
            name = JOB_DEFINITIONS[args.harness]
            definition = rebuild(args.region, args.harness) if args.rebuild else os.environ.get('WOW_JOB_DEFINITION', name)
            submission = submit_job(args.region, name, definition, overrides, args.queue)
            job_id = submission['jobId']
            print(json.dumps(submission), flush=True)
        return follow(job_id, args.region, args.log_group)
    except KeyboardInterrupt:
        message = f'Stopped watching {job_id}. The AWS job was not cancelled.' if job_id else 'Stopped.'
        print('\n' + message, flush=True)
        return 130
    except (AwsError, OSError, sqlite3.Error, subprocess.CalledProcessError) as error:
        if job_id:
            print(f'Watching stopped: {error}\nJob {job_id} was not cancelled.', file=sys.stderr)
        else:
            print(f'Launch failed: {error}', file=sys.stderr)
        return getattr(error, 'returncode', 1)
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    sys.exit(main())
