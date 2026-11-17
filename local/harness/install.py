#!/usr/bin/env python3
"""Install the shared image and module in the two existing duel workspaces."""
import argparse
import io
import shutil
from pathlib import Path
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
IMAGE = 'agent-wow-harness:local'


def docker(*args, data=None):
    result = subprocess.run(['docker', *map(str, args)], input=data, stdout=subprocess.PIPE, check=True)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT / 'tmp/local/duel-workspace')
    args = parser.parse_args()
    target = args.workspace / 'modules/duel'
    target.mkdir(parents=True, exist_ok=True)
    for name in ('harness.py', 'module.yaml', 'compose.yaml'):
        if name != 'harness.py' or not (target / name).exists():
            shutil.copy2(HERE / name, target / name)
    with tempfile.TemporaryDirectory(prefix='harness-build-') as directory:
        context = Path(directory)
        (context / 'api/module/v1').mkdir(parents=True)
        shutil.copy2(ROOT / 'api/module/v1/session.proto', context / 'api/module/v1/session.proto')
        for name in ('Dockerfile', 'connector.proto', 'connector.py', 'runner.py', 'state.py'):
            shutil.copy2(HERE / name, context / name)
        subprocess.run(['docker', 'build', '-t', IMAGE, str(context)], check=True)
    container = docker('create', IMAGE).decode().strip()
    try:
        docker('cp', container + ':/api/module.pb', target / 'module.pb')
    finally:
        docker('rm', container)
    compose = ['compose', '--env-file', ROOT / 'tmp/local/.env', '-f', ROOT / 'local/compose.yaml', 'ps', '-q']
    # Stream the shared image into each client's independent Docker daemon.
    for client in ('client-1', 'client-2'):
        cid = docker(*compose, client).decode().strip()
        if not cid:
            raise RuntimeError(client + ' is not running')
        image = subprocess.Popen(['docker', 'save', IMAGE], stdout=subprocess.PIPE)
        try:
            subprocess.run(['docker', 'exec', '-i', cid, 'docker', 'load'], stdin=image.stdout,
                           stdout=subprocess.DEVNULL, check=True)
            image.stdout.close()
            if image.wait():
                raise RuntimeError('Image export failed')
        finally:
            if image.poll() is None:
                image.terminate()
                image.wait()
        exists = docker('exec', cid, 'sh', '-c', 'test -f /workspace/modules/duel/harness.py && echo yes || echo no').decode().strip() == 'yes'
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w') as archive:
            for name in ('harness.py', 'module.yaml', 'compose.yaml', 'module.pb'):
                if name != 'harness.py' or not exists:
                    archive.add(target / name, arcname='modules/duel/' + name)
        docker('exec', '-i', '--user', 'node', cid, 'tar', '-xf', '-', '-C', '/workspace', data=payload.getvalue())
        print(client + ': installed; existing harness.py preserved', flush=True)
    print('No clients restarted and no duels started. The module loads on the next character login.')


if __name__ == '__main__':
    main()
