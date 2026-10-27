"""Level-target results: Lambda handler, HTTP client, and private run-key creation.

Deploy: tofu -chdir=benchmark init && tofu -chdir=benchmark apply
Create: python3 benchmark_results.py create orc warrior 2 config/benchmark.json
Run: python3 launch.py --benchmark config/benchmark.json
Retry a saved upload: python3 benchmark_results.py send CONFIG benchmark-result.json
"""

import argparse
from datetime import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def active_streams(rows):
    """A channel link is live only for its bound job and fresh OBS telemetry."""
    candidates = [row for row in rows if row.get('stream') and not row.get('result')]
    if not candidates or not os.environ.get('RESULTS_BUCKET'):
        return []
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=1, read_timeout=2, retries={'total_max_attempts': 1})
    batch = boto3.client('batch', config=config)
    s3 = boto3.client('s3', config=config)
    jobs = {}
    ids = list({row['stream']['job_id'] for row in candidates})
    for offset in range(0, len(ids), 100):
        jobs.update({job['jobId']: job['status'] for job in batch.describe_jobs(jobs=ids[offset:offset+100])['jobs']})
    live = []
    for row in candidates:
        stream = row['stream']
        if jobs.get(stream['job_id']) != 'RUNNING':
            continue
        try:
            reply = s3.get_object(Bucket=os.environ['RESULTS_BUCKET'],
                                  Key=f"viewer/{stream['instance_id']}/stream-status.json")
            status = json.loads(reply['Body'].read())
            age = time.time() - datetime.fromisoformat(status['updated_at'].replace('Z', '+00:00')).timestamp()
            output = status.get('output', {})
            if (status.get('job_id') != stream['job_id'] or status.get('channel') != stream['channel'] or status.get('mode') != 'live'
                    or status.get('state') != 'active' or not 0 <= age <= 90
                    or not output.get('outputActive') or output.get('outputReconnecting')
                    or not output.get('outputBytes')):
                continue
            live.append({k: v for k, v in row.items() if k != 'result'})
        except Exception:
            # A failed health check must never advertise a stale channel as this run.
            continue
    return live


def save_stream(db, run_id, row, data):
    try:
        stream = {k: data[k] for k in ('channel', 'job_id', 'instance_id', 'character', 'harness', 'model', 'effort')}
        patterns = {'channel': r'[a-z0-9_]{3,25}', 'job_id': r'[a-f0-9-]{36}',
                    'instance_id': r'i-[a-f0-9]{17}', 'character': r'[A-Za-z]{2,12}'}
        if any(not isinstance(stream[k], str) or not re.fullmatch(pattern, stream[k]) for k, pattern in patterns.items()):
            raise ValueError()
        if stream['harness'] not in ('codex', 'claude') or any(
                not isinstance(stream[k], str) or not 0 < len(stream[k]) <= 128 for k in ('model', 'effort')):
            raise ValueError()
        if row.get('result'):
            return response(409, 'Run already finalized')
        stream['url'] = 'https://www.twitch.tv/' + stream['channel']
    except (KeyError, TypeError, ValueError):
        return response(400, 'Invalid stream')
    try:
        db.update_item(Key={'run_id': run_id}, UpdateExpression='SET #stream = :stream',
                       ConditionExpression='attribute_exists(run_id) AND attribute_not_exists(#result) AND '
                                           '(attribute_not_exists(#stream) OR #stream.job_id = :job)',
                       ExpressionAttributeNames={'#stream': 'stream', '#result': 'result'},
                       ExpressionAttributeValues={':stream': stream, ':job': stream['job_id']})
    except db.meta.client.exceptions.ConditionalCheckFailedException:
        return response(409, 'Run belongs to another job or is finalized')
    return response(200, 'stream linked')


def table():
    import boto3  # Supplied by Lambda; local key creation uses the existing AWS CLI.
    return boto3.resource('dynamodb').Table(os.environ['TABLE'])


def response(code, status):
    return {'statusCode': code, 'body': json.dumps({'status': status} if isinstance(status, str) else status, default=float),
            'headers': {'content-type': 'application/json', 'cache-control': 'no-store'}}


def handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method')
    key = event.get('headers', {}).get('authorization', '').removeprefix('Bearer ')
    if method == 'GET':
        read_hash = os.environ.get('READ_KEY_HASH', '')
        if not read_hash or not secrets.compare_digest(hashlib.sha256(key.encode()).hexdigest(), read_hash):
            return response(403, 'Invalid read key')
        db = table()
        items = []
        query = {'ProjectionExpression': 'run_id,race,#class,target_level,prompt,#result,#stream',
                 'ExpressionAttributeNames': {'#class': 'class', '#result': 'result', '#stream': 'stream'}, 'ConsistentRead': True}
        while True:
            page = db.scan(**query)
            items.extend(page.get('Items', []))
            if not page.get('LastEvaluatedKey'):
                break
            query['ExclusiveStartKey'] = page['LastEvaluatedKey']
        completed = [row for row in items if row.get('result', {}).get('status') in ('succeeded', 'unverified')
                     and row['result']['exit_code'] == 0]
        try:
            live = active_streams(items)
        except Exception:
            live = []
        return response(200, {'rows': sorted(completed, key=lambda row: row['result']['finished_at'], reverse=True),
                              'live': live})
    if method != 'POST':
        return response(405, 'GET or POST required')
    if len(key) != 43:
        return response(403, 'Invalid key')
    run_id = hashlib.sha256(key.encode()).hexdigest()  # The key selects its only writable row.
    db = table()
    row = db.get_item(Key={'run_id': run_id}, ConsistentRead=True).get('Item')
    if not row:
        return response(403, 'Invalid key')
    try:
        body = event.get('body') or ''
        if len(body) > 16384:
            raise ValueError()
        data = json.loads(body)
        if data.get('kind') == 'stream':
            return save_stream(db, run_id, row, data)
        result = {k: data[k] for k in ('started_at', 'finished_at', 'elapsed_ms',
                                      'exit_code', 'harness', 'model', 'effort')}
        for field in ('started_at', 'finished_at'):
            if not datetime.fromisoformat(result[field].replace('Z', '+00:00')).tzinfo:
                raise ValueError()
        if (type(result['elapsed_ms']) is not int or result['elapsed_ms'] < 0
                or type(result['exit_code']) is not int
                or result['harness'] not in ('codex', 'claude')
                or any(not isinstance(result[k], str) or not 0 < len(result[k]) <= 128
                       for k in ('model', 'effort'))):
            raise ValueError()
        characters = data['characters']
        character = {k: characters[0][k] for k in ('name', 'race', 'class', 'level')} if len(characters) == 1 else None
        if character and (type(character['level']) is not int or not 1 <= character['level'] <= 80
                          or any(not isinstance(character[k], str) for k in ('name', 'race', 'class'))):
            raise ValueError()
        passed = (result['exit_code'] == 0 and character is not None
                  and character['race'].casefold() == row['race']
                  and character['class'].casefold() == row['class']
                  and (row.get('target_level') is None or character['level'] >= row['target_level']))
        result.update(character=character, status=('unverified' if row.get('target_level') is None else 'succeeded') if passed else 'failed',
                      job_id=str(data.get('job_id', ''))[:128], timing='agent')
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return response(400, 'Invalid result')
    try:
        db.update_item(Key={'run_id': run_id}, UpdateExpression='SET #result = :result',
                       ConditionExpression='attribute_exists(run_id) AND (attribute_not_exists(#result) OR #result = :result) '
                                           'AND (attribute_not_exists(#stream) OR #stream.job_id = :job)',
                       ExpressionAttributeNames={'#result': 'result', '#stream': 'stream'},
                       ExpressionAttributeValues={':result': result, ':job': result['job_id']})
    except db.meta.client.exceptions.ConditionalCheckFailedException:
        return response(409, 'Run already finalized')
    return response(200, result['status'])


def post(url, key, result):
    request = Request(url, json.dumps(result).encode(), method='POST',
                      headers={'authorization': 'Bearer ' + key, 'content-type': 'application/json'})
    for delay in (1, 2, None):
        try:
            with urlopen(request, timeout=10) as reply:
                return json.load(reply)['status']
        except (URLError, TimeoutError) as error:
            if delay is None or isinstance(error, HTTPError) and error.code != 429 and error.code < 500:
                raise
            time.sleep(delay)


def create_run(race, character_class, level, config):
    from race_classes import RACE_CLASSES
    if character_class not in RACE_CLASSES.get(race, ()) or (level is not None and not 1 <= level <= 80):
        raise ValueError('Choose a supported race/class and a target level from 1 to 80.')
    if config.exists():
        raise ValueError('Config already exists; use a new filename for a new attempt.')
    infrastructure = Path(__file__).resolve().parent / 'benchmark'
    outputs = json.loads(subprocess.check_output(['tofu', f'-chdir={infrastructure}', 'output', '-json'], text=True))
    key = secrets.token_urlsafe(32)
    run_id = hashlib.sha256(key.encode()).hexdigest()
    prompt = (f'create a {race.replace("-", " ")} {character_class.replace("-", " ")} '
              f'and adventure until they are level {level}') if level is not None else (
              f'Create one {race.replace("-", " ")} {character_class.replace("-", " ")} and complete the starting area.')
    item = {k: {'S': v} for k, v in {'run_id': run_id, 'prompt': prompt,
            'race': race.replace('-', ' '), 'class': character_class.replace('-', ' ')}.items()}
    item['target_level'] = {'N': str(level)} if level is not None else {'NULL': True}
    subprocess.run(['aws', 'dynamodb', 'put-item', '--table-name', outputs['table']['value'],
                    '--region', outputs['region']['value'], '--item', json.dumps(item),
                    '--condition-expression', 'attribute_not_exists(run_id)', '--no-cli-pager'], check=True)
    os.umask(0o077)
    config.parent.mkdir(parents=True, exist_ok=True)
    with config.open('x') as file:
        json.dump({'url': outputs['url']['value'], 'key': key, 'prompt': prompt}, file, indent=2)
        file.write('\n')
    print(f'Run {run_id} created. Private config: {config}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Create one level-target attempt and its private config.')
    create.add_argument('race')
    create.add_argument('character_class')
    create.add_argument('level', type=int)
    create.add_argument('config', type=Path)
    send = commands.add_parser('send', help='Retry an existing result without replaying the task.')
    send.add_argument('config', type=Path)
    send.add_argument('result', type=Path)
    args = parser.parse_args()
    if args.command == 'send':
        config = json.loads(args.config.read_text())
        print(post(config['url'], config['key'], json.loads(args.result.read_text())))
    else:
        try:
            create_run(args.race, args.character_class, args.level, args.config)
        except ValueError as error:
            parser.error(str(error))


if __name__ == '__main__':
    main()
