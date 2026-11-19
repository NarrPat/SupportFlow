"""Optional Redis wakeups. Committed PostgreSQL events remain authoritative.

Intended product path: apps/supportflow/jobs/event_hints.py. No broker payload is
forwarded to an SSE client, and failed hints never retry a business transaction.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
from threading import RLock
from time import monotonic
from urllib.parse import quote
from weakref import WeakKeyDictionary

import anyio
from psycopg.pq import TransactionStatus
import redis
import redis.asyncio as async_redis


SOCKET_SECONDS = 0.15
MAX_PENDING = 1024
_pending = WeakKeyDictionary()
_guard = RLock()
_log = logging.getLogger('supportflow.event_hints')


@dataclass(frozen=True)
class Candidate:
    tenant_id: str
    run_id: str
    seq: int
    writer_xid: str
    occurred_at: object


def configured():
    try:
        return Path(os.environ.get('SF_REDIS_PASSWORD_FILE', '/run/secrets/redis_password')).is_file()
    except OSError:
        return False


def redis_url():
    secret = Path(os.environ.get('SF_REDIS_PASSWORD_FILE', '/run/secrets/redis_password')).read_text().strip()
    if not secret:
        raise ValueError('Event wakeup credential is empty')
    return 'redis://:' + quote(secret, safe='') + '@redis:6379/0'


def channel(tenant_id, run_id):
    for value in (tenant_id, run_id):
        if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is None:
            raise ValueError('Invalid event wakeup identity')
    return f'supportflow:events:{tenant_id}:{run_id}'


def discard(conn):
    with _guard:
        try:
            _pending.pop(conn, None)
        except TypeError:
            pass  # An uninstrumented SQL double may not support weak references.


def _remember(conn, run, observed):
    if observed is None:
        return
    candidate = Candidate(run['tenant_id'], run['run_id'], run['last_event_seq'],
                          observed['writer_xid'], observed['occurred_at'])
    with _guard:
        pending = _pending.setdefault(conn, {})
        if len(pending) >= MAX_PENDING:
            # Hints are deliberately lossy. DB polling remains complete.
            pending.clear()
        pending[(candidate.tenant_id, candidate.run_id, candidate.seq)] = candidate


def mark_sync(conn, run):
    if not configured():
        return
    row = conn.execute('''SELECT xmin::text AS writer_xid,occurred_at FROM run_events
        WHERE tenant_id=%s AND run_id=%s AND seq=%s''',
        (run['tenant_id'], run['run_id'], run['last_event_seq'])).fetchone()
    _remember(conn, run, row)


async def mark_async(conn, run):
    if not configured():
        return
    row = await (await conn.execute('''SELECT xmin::text AS writer_xid,occurred_at FROM run_events
        WHERE tenant_id=%s AND run_id=%s AND seq=%s''',
        (run['tenant_id'], run['run_id'], run['last_event_seq']))).fetchone()
    _remember(conn, run, row)


def idle(conn):
    return (not conn.closed and not conn.broken and conn.autocommit
            and conn.info.transaction_status == TransactionStatus.IDLE)


def _take(conn):
    if conn.closed or conn.broken:
        discard(conn)
        return []
    if not idle(conn):
        # Releasing a savepoint is not a commit. Keep candidates for the outer
        # transaction rather than doing Redis I/O while row locks are held.
        return []
    with _guard:
        return list(_pending.pop(conn, {}).values())


VERIFY_SQL = '''SELECT e.tenant_id,e.run_id,e.seq FROM run_events e
    JOIN runs r USING(tenant_id,run_id) WHERE e.tenant_id=%s AND e.run_id=%s
    AND e.seq=%s AND e.xmin::text=%s AND e.occurred_at=%s AND e.seq<=r.last_event_seq'''


def committed_sync(conn):
    if not configured():
        return []
    selected = {}
    for candidate in _take(conn):
        row = conn.execute(VERIFY_SQL, (candidate.tenant_id, candidate.run_id,
            candidate.seq, candidate.writer_xid, candidate.occurred_at)).fetchone()
        if row is not None:
            key = row['tenant_id'], row['run_id']
            if row['seq'] > selected.get(key, {}).get('seq', -1):
                selected[key] = row
    return list(selected.values())


async def committed_async(conn):
    if not configured():
        return []
    selected = {}
    for candidate in _take(conn):
        row = await (await conn.execute(VERIFY_SQL, (candidate.tenant_id, candidate.run_id,
            candidate.seq, candidate.writer_xid, candidate.occurred_at))).fetchone()
        if row is not None:
            key = row['tenant_id'], row['run_id']
            if row['seq'] > selected.get(key, {}).get('seq', -1):
                selected[key] = row
    return list(selected.values())


def payload(hint):
    if type(hint['seq']) is not int or hint['seq'] < 1:
        raise ValueError('Invalid event wakeup sequence')
    return json.dumps({'run_id': hint['run_id'], 'seq': hint['seq']}, separators=(',', ':'))


def publish_sync(hints):
    if not hints or not configured():
        return []
    client = redis.Redis.from_url(redis_url(), decode_responses=True,
        socket_connect_timeout=SOCKET_SECONDS, socket_timeout=SOCKET_SECONDS)
    result = []
    deadline = monotonic() + 0.6
    try:
        for hint in hints:
            if monotonic() >= deadline:
                break
            try:
                count = client.publish(channel(hint['tenant_id'], hint['run_id']), payload(hint))
            except (redis.RedisError, OSError) as error:
                _log.info('event_hint_unavailable error_type=%s', type(error).__name__)
                count = None
            result.append({'run_id': hint['run_id'], 'seq': hint['seq'], 'subscribers': count})
    finally:
        client.close()
    return result


async def publish_async(hints):
    if not hints or not configured():
        return []
    client = async_redis.Redis.from_url(redis_url(), decode_responses=True,
        socket_connect_timeout=SOCKET_SECONDS, socket_timeout=SOCKET_SECONDS)
    result = []
    try:
        with anyio.move_on_after(0.6):
            for hint in hints:
                try:
                    count = await client.publish(channel(hint['tenant_id'], hint['run_id']), payload(hint))
                except (redis.RedisError, OSError) as error:
                    _log.info('event_hint_unavailable error_type=%s', type(error).__name__)
                    count = None
                result.append({'run_id': hint['run_id'], 'seq': hint['seq'], 'subscribers': count})
    finally:
        with anyio.move_on_after(0.2, shield=True):
            await client.aclose()
    return result


def flush_sync(conn):
    hints = committed_sync(conn)
    if not hints:
        return []
    # committed_sync only queries an IDLE autocommit connection. Verify again
    # before network use so this function never hides a transaction around I/O.
    if hints and not idle(conn):
        raise RuntimeError('Event wakeup attempted inside a transaction')
    return publish_sync(hints)


@contextmanager
def transaction(conn):
    try:
        with conn.transaction():
            yield
    except BaseException:
        discard(conn)
        raise
    else:
        try:
            flush_sync(conn)
        except Exception as error:
            # The transaction already committed. A hint failure cannot undo it
            # or turn its caller into a business retry.
            _log.info('event_hint_flush_unavailable error_type=%s', type(error).__name__)


class Listener:
    """One optional Redis lease per SSE, never a PostgreSQL lease."""
    def __init__(self, tenant_id, run_id, *, observer=None):
        self.name = channel(tenant_id, run_id)
        self.client = self.pubsub = None
        self.observer = observer

    async def _observe(self, kind, values):
        if self.observer is not None:
            await self.observer(kind, values)

    async def __aenter__(self):
        await self._observe('before_subscribe', {'channel': self.name})
        if not configured():
            return self
        try:
            self.client = async_redis.Redis.from_url(redis_url(), decode_responses=True,
                socket_connect_timeout=SOCKET_SECONDS, socket_timeout=SOCKET_SECONDS)
            self.pubsub = self.client.pubsub()
            await self.pubsub.subscribe(self.name)
            with anyio.fail_after(0.6):
                while True:
                    message = await self.pubsub.get_message(ignore_subscribe_messages=False, timeout=0.25)
                    if message and message.get('type') == 'subscribe':
                        await self._observe('subscribed', {'message': message})
                        break
        except (redis.RedisError, OSError, TimeoutError, ValueError) as error:
            _log.info('event_subscribe_unavailable error_type=%s', type(error).__name__)
            await self.close()
        return self

    async def wait(self, seconds=1.0):
        if self.pubsub is None:
            await anyio.sleep(seconds)
            return None
        try:
            with anyio.move_on_after(seconds):
                while True:
                    message = await self.pubsub.get_message(ignore_subscribe_messages=True,
                                                          timeout=min(seconds, 0.25))
                    if message is not None:
                        await self._observe('message', {'message': message})
                        return message
            return None
        except (redis.RedisError, OSError) as error:
            _log.info('event_listener_unavailable error_type=%s', type(error).__name__)
            await self.close()
            return None

    async def close(self):
        # Cleanup has its own bound. A failed Redis connection must not keep the
        # HTTP generator alive; DB polling has no dependency on Redis teardown.
        try:
            with anyio.move_on_after(0.2, shield=True):
                for resource in (self.pubsub, self.client):
                    if resource is not None:
                        try:
                            await resource.aclose()
                        except (redis.RedisError, OSError) as error:
                            _log.info('event_listener_close_unavailable error_type=%s', type(error).__name__)
        finally:
            self.pubsub = self.client = None

    async def __aexit__(self, *args):
        await self.close()
