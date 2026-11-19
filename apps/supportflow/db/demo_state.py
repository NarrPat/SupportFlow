"""Read-only counters for a bounded persistent deployment startup check."""
import argparse
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--require-idle',action='store_true')
    args=parser.parse_args()
    database=os.environ.get('PGDATABASE','sf_app')
    if database!='sf_app':
        raise ValueError('Persistent startup observation requires sf_app')
    password=Path('/run/secrets/app_migrator_password').read_text().strip()
    with psycopg.connect(host=os.environ.get('PGHOST','postgres'),dbname=database,
                         user='sf_app_migrator',password=password,connect_timeout=5,
                         options='-c statement_timeout=5000',row_factory=dict_row) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        row=conn.execute('''SELECT count(*) FILTER (WHERE status IN ('queued','running','resume_queued')) AS runnable,
            coalesce(sum(model_attempts),0)::bigint AS model_attempts FROM runs''').fetchone()
    print('SF_DEMO_COUNTERS '+json.dumps(row))
    if args.require_idle and row['runnable']:
        raise SystemExit('Existing accepted work needs owner inspection before a zero-submission startup check')


if __name__=='__main__': main()
