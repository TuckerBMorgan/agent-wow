#!/usr/bin/env python3
"""Local job history. Run jobs.py to list recorded launches.

Model, effort, and prompt store explicit overrides; NULL means the default was used.
"""

import argparse
from contextlib import closing, contextmanager
import sqlite3
import time
from pathlib import Path

from agent_common import harness_spec


DB_PATH = Path(__file__).resolve().with_name('jobs.sqlite3')


@contextmanager
def database():
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as db:
        DB_PATH.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, job_name TEXT, region TEXT, job_definition TEXT,
            created_at INTEGER, provider TEXT, model TEXT, effort TEXT, prompt TEXT
        )''')
        yield db
        db.commit()


def record_job(job_id, name, region, definition, overrides):
    settings = {item['name']: item['value'] for item in overrides.get('environment', [])}
    harness = settings.get('AGENT_WOW_HARNESS', 'codex')
    spec = harness_spec(harness)
    command = overrides.get('command', [])
    prompt = command[1] if len(command) == 2 and command[0] == 'run-wow-agent' else None
    with database() as db:
        db.execute('INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (
            job_id, name, region, definition, int(time.time() * 1000),
            f'{harness}-subscription', settings.get(spec['model_env']), settings.get(spec['effort_env']), prompt,
        ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        with database() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created_at DESC').fetchall()
    except (OSError, sqlite3.Error) as error:
        parser.exit(1, f'Job history failed: {error}\n')
    print(f'{len(rows)} jobs in {DB_PATH}')
    for row in rows:
        print(f'{row["job_id"]}  {row["model"] or "default":30}  '
              f'{row["effort"] or "default":7}  {row["job_name"]}')


if __name__ == '__main__':
    main()
