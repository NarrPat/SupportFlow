"""Serialize SQL units on the fragment's original advisory-lock connection.

LangGraph's sync durability still schedules PostgresSaver writes in background
threads. Driver statement-level serialization does not protect a transaction's
whole body. This lock is per physical connection and reentrant for nested scopes;
it deliberately does not cover a node, model/tool HTTP call or graph.invoke.
"""
from contextlib import contextmanager
from threading import RLock

from psycopg import Connection


class SafeConnection(Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Keep psycopg's internal non-reentrant driver lock unchanged. Its methods
        # acquire that lock themselves, inside this separate outer scope lock.
        self._sf_scope_lock = RLock()

    @contextmanager
    def transaction(self, savepoint_name=None, force_rollback=False):
        with self._sf_scope_lock:
            with super().transaction(savepoint_name, force_rollback) as transaction:
                yield transaction

    @contextmanager
    def pipeline(self):
        with self._sf_scope_lock:
            with super().pipeline() as pipeline:
                yield pipeline

    def execute(self, query, params=None, *, prepare=None, binary=False):
        with self._sf_scope_lock:
            return super().execute(query, params, prepare=prepare, binary=binary)
