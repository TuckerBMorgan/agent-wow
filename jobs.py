#!/usr/bin/env python3
"""Local job history. Run jobs.py to list recorded launches.

Model, effort, and prompt store explicit overrides; NULL means the default was used.
"""

import argparse
from contextlib import closing, contextmanager
import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().with_name('jobs.sqlite3')


@contextmanager
def database():
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as db:
        DB_PATH.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, job_name TEXT, region TEXT, job_definition TEXT,
            created_at INTEGER, status TEXT, provider TEXT, model TEXT, effort TEXT,
            prompt TEXT, started_at INTEGER, stopped_at INTEGER, exit_code INTEGER
        )''')
        yield db
        db.commit()


def record_job(job, region):
    container = job.get('container') or {}
    settings = {item['name']: item['value'] for item in container.get('environment') or []}
    prefix = 'DEEPSEEK' if 'DEEPSEEK_MODEL' in settings else ('CLAUDE' if 'CLAUDE_MODEL' in settings else 'CODEX')
    command = container.get('command') or []
    prompt = command[1] if len(command) == 2 and command[0] == 'run-wow-agent' else None
    with database() as db:
        db.execute('INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (
            job['jobId'], job['jobName'], region, job.get('jobDefinition'),
            job.get('createdAt'), job['status'],
            ('claude-subscription' if settings.get('CLAUDE_AUTH_MODE') == 'subscription' else 'openrouter')
            if prefix != 'CODEX' else ('codex-subscription' if settings.get('CODEX_AUTH_MODE') == 'subscription'
                                      else settings.get('CODEX_MODEL_PROVIDER', 'openai')),
            settings.get(f'{prefix}_MODEL'),
            settings.get('CODEX_REASONING_EFFORT' if prefix == 'CODEX' else f'{prefix}_EFFORT'), prompt,
            job.get('startedAt'), job.get('stoppedAt'), container.get('exitCode'),
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
        print(f'{row["job_id"]}  {row["status"]:10}  {row["model"] or "default":30}  '
              f'{row["effort"] or "default":7}  {row["job_name"]}')


if __name__ == '__main__':
    main()
