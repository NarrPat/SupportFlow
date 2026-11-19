"""Bounded, prefix-only PostgreSQL event retention; no business state changes.

Intended product path: apps/supportflow/jobs/event_retention.py.
"""
import argparse
from contextlib import closing
from datetime import timedelta
import json
import os

from .runtime import connect


DEFAULT_DAYS = 7
DEFAULT_RUN_LIMIT = 10
DEFAULT_ROW_LIMIT = 100
INTERVAL_SECONDS = 60


def processing_now(conn):
    return conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']


def prune_once(conn, *, days=DEFAULT_DAYS, run_limit=DEFAULT_RUN_LIMIT, row_limit=DEFAULT_ROW_LIMIT):
    if (type(days) is not int or not 1 <= days <= 3650 or
            type(run_limit) is not int or not 1 <= run_limit <= 100 or
            type(row_limit) is not int or not 1 <= row_limit <= 1000):
        raise ValueError('Invalid event retention bounds')
    now = processing_now(conn)
    cutoff = now - timedelta(days=days)
    # Only runs whose first retained row is old can make progress. No skipped
    # middle event can be deleted, even if wall time moved backwards later.
    candidates = conn.execute('''SELECT r.run_id FROM runs r
        JOIN LATERAL (SELECT occurred_at FROM run_events e
            WHERE e.run_id=r.run_id ORDER BY seq LIMIT 1) first_event ON true
        WHERE first_event.occurred_at<%s ORDER BY r.run_id LIMIT %s''',
        (cutoff, run_limit)).fetchall()
    reports = []
    for candidate in candidates:
        with conn.transaction():
            run = conn.execute('SELECT * FROM runs WHERE run_id=%s FOR UPDATE SKIP LOCKED',
                               (candidate['run_id'],)).fetchone()
            if run is None:
                reports.append({'run_id': candidate['run_id'], 'skipped_locked': True, 'deleted_seqs': []})
                continue
            rows = conn.execute('''SELECT seq,occurred_at FROM run_events WHERE run_id=%s
                ORDER BY seq LIMIT %s''', (run['run_id'], row_limit)).fetchall()
            boundary = None
            for row in rows:
                if row['occurred_at'] >= cutoff:
                    break
                boundary = row['seq']
            deleted = [] if boundary is None else conn.execute('''DELETE FROM run_events
                WHERE run_id=%s AND seq<=%s RETURNING seq''', (run['run_id'], boundary)).fetchall()
            reports.append({'run_id': run['run_id'], 'skipped_locked': False,
                'deleted_seqs': sorted(row['seq'] for row in deleted), 'last_seq': run['last_event_seq']})
    return {'processing_now': now.isoformat(), 'cutoff': cutoff.isoformat(), 'days': days,
            'run_limit': run_limit, 'row_limit': row_limit, 'runs': reports}


def configured_days():
    value = os.environ.get('SF_EVENT_RETENTION_DAYS', str(DEFAULT_DAYS))
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 3650:
        raise ValueError('Invalid event retention days')
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', required=True)
    args = parser.parse_args()
    with closing(connect('event-retention')) as conn:
        report = prune_once(conn, days=configured_days())
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
