#!/usr/bin/env python3
"""Install the shared connector in both clients, preserving their submitted harnesses."""
import argparse
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
IMAGE = 'agent-wow-harness:local'


def docker(*args):
    return subprocess.check_output(['docker', *map(str, args)])


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    subprocess.run(['docker', 'build', '--build-context', 'api=' + str(ROOT / 'api'),
                    '-t', IMAGE, str(HERE)], check=True)
    with tempfile.TemporaryDirectory(prefix='duel-module-') as directory:
        directory = Path(directory)
        container = docker('create', IMAGE).decode().strip()
        try:
            docker('cp', container + ':/api/module.pb', directory / 'module.pb')
        finally:
            docker('rm', container)
        docker('save', '-o', directory / 'image.tar', IMAGE)
        for client in ('client-1', 'client-2'):
            cid = docker('compose', '--env-file', ROOT / 'tmp/local/.env',
                         '-f', ROOT / 'local/compose.yaml', 'ps', '-q', client).decode().strip()
            if not cid:
                raise RuntimeError(client + ' is not running')
            with (directory / 'image.tar').open('rb') as stream:
                subprocess.run(['docker', 'exec', '-i', cid, 'docker', 'load'],
                               stdin=stream, stdout=subprocess.DEVNULL, check=True)
            exists = docker('exec', cid, 'sh', '-c',
                'test -f /workspace/modules/duel/harness.py && echo yes || echo no').strip() == b'yes'
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode='w') as archive:
                for name in ('harness.py', 'module.yaml', 'compose.yaml', 'module.pb'):
                    if name != 'harness.py' or not exists:
                        archive.add((directory if name == 'module.pb' else HERE) / name,
                                    arcname='modules/duel/' + name)
            subprocess.run(['docker', 'exec', '-i', '--user', 'node', cid,
                            'tar', '-xf', '-', '-C', '/workspace'], input=payload.getvalue(), check=True)
            print(client + ': connector installed; harness preserved; loads on next login', flush=True)


if __name__ == '__main__':
    main()
