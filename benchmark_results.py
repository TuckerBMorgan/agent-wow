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
from pathlib import Path
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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
        query = {'ProjectionExpression': 'benchmark_page,benchmark_snapshot', 'ConsistentRead': True}
        while True:
            page = db.scan(**query)
            items.extend(item for item in page.get('Items', []) if item.get('benchmark_page'))
            if not page.get('LastEvaluatedKey'):
                break
            query['ExclusiveStartKey'] = page['LastEvaluatedKey']
        rows = sorted((item['benchmark_page'] for item in items), key=lambda row: (row['race'], row['class'], row['agent']))
        return response(200, {'rows': rows, 'checked_at': max((item['benchmark_snapshot'] for item in items), default=None)})
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
                  and character['level'] >= row['target_level'])
        result.update(character=character, status='succeeded' if passed else 'failed',
                      job_id=str(data.get('job_id', ''))[:128])
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return response(400, 'Invalid result')
    try:
        db.update_item(Key={'run_id': run_id}, UpdateExpression='SET #result = :result',
                       ConditionExpression='attribute_exists(run_id) AND (attribute_not_exists(#result) OR #result = :result)',
                       ExpressionAttributeNames={'#result': 'result'},
                       ExpressionAttributeValues={':result': result})
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
        return
    from race_classes import RACE_CLASSES
    if args.character_class not in RACE_CLASSES.get(args.race, ()) or not 1 <= args.level <= 80:
        parser.error('Choose a supported race/class and a target level from 1 to 80.')
    if args.config.exists():
        parser.error('Config already exists; use a new filename for a new attempt.')
    infrastructure = Path(__file__).resolve().parent / 'benchmark'
    outputs = json.loads(subprocess.check_output(['tofu', f'-chdir={infrastructure}', 'output', '-json'], text=True))
    key = secrets.token_urlsafe(32)
    run_id = hashlib.sha256(key.encode()).hexdigest()
    prompt = (f'Create one {args.race.replace("-", " ")} {args.character_class.replace("-", " ")} '
              f'and adventure until you are level {args.level}.')
    item = {k: {'S': v} for k, v in {'run_id': run_id, 'prompt': prompt,
            'race': args.race.replace('-', ' '), 'class': args.character_class.replace('-', ' ')}.items()}
    item['target_level'] = {'N': str(args.level)}
    subprocess.run(['aws', 'dynamodb', 'put-item', '--table-name', outputs['table']['value'],
                    '--region', outputs['region']['value'], '--item', json.dumps(item),
                    '--condition-expression', 'attribute_not_exists(run_id)', '--no-cli-pager'], check=True)
    os.umask(0o077)
    args.config.parent.mkdir(parents=True, exist_ok=True)
    with args.config.open('x') as file:
        json.dump({'url': outputs['url']['value'], 'key': key, 'prompt': prompt}, file, indent=2)
        file.write('\n')
    print(f'Run {run_id} created. Private config: {args.config}')


if __name__ == '__main__':
    main()
