"""Fetch the pinned character catalog once; share its cache with both clients."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / 'local/catalog.json').read_text())
BUILDS = ROOT / 'tmp/local/builds' / PIN['sha256']


def fetch():
    if (BUILDS / 'index.json').is_file():
        return
    BUILDS.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=BUILDS.parent) as directory:
        archive = Path(directory) / 'builds.tar.gz'
        subprocess.run(['aws', 's3api', 'get-object', '--region', PIN['region'],
                        '--bucket', PIN['bucket'], '--key', PIN['key'],
                        '--version-id', PIN['version_id'], str(archive)],
                       check=True, stdout=subprocess.DEVNULL)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != PIN['sha256']:
            raise ValueError('Character catalog checksum does not match the pinned version')
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                if (not (member.isfile() or member.isdir()) or member.name.startswith('/')
                        or '..' in Path(member.name).parts):
                    raise ValueError('Unexpected character catalog archive entry')
            bundle.extractall(directory)
        (Path(directory) / 'builds').rename(BUILDS)
