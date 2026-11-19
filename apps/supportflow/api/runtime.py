"""Application database, cookie policy and sanitized HTTP errors."""
import ipaddress
import logging
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from apps.supportflow.jobs import event_hints

from fastapi import Depends, Request
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel


class ErrorView(BaseModel):
    code: str
    message: str
    request_id: str
    retryable: bool
    details: dict


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class CookiePolicy:
    origin: str
    secure: bool
    session_name: str
    anonymous_name: str

    @classmethod
    def from_env(cls):
        profile = os.environ.get("SF_PROFILE", "https")
        origin = os.environ["SF_PUBLIC_ORIGIN"]
        url = urlsplit(origin)
        if (not url.hostname or url.username is not None or url.password is not None
                or origin != f"{url.scheme}://{url.netloc}" or not (url.port is None or 0 < url.port < 65536)):
            raise ValueError("SF_PUBLIC_ORIGIN must be an origin without credentials, path, query or fragment")
        if profile == "https" and url.scheme == "https":
            return cls(origin, True, "__Host-sf_session", "__Host-sf_anon")
        try:
            local = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            local = url.hostname == "localhost"
        if profile == "dev" and url.scheme == "http" and local:
            return cls(origin, False, "sf_dev_session", "sf_dev_anon")
        raise ValueError("Use HTTPS or explicitly select the loopback-only HTTP dev profile")


def database_pool() -> AsyncConnectionPool:
    database = os.environ.get("PGDATABASE", "sf_app")
    if database != "sf_app" and not re.fullmatch(r"sf_eval_[a-z0-9_]+_app", database):
        raise ValueError("Expected the SupportFlow application database")
    password = Path(os.environ["PGPASSWORD_FILE"]).read_text(encoding="utf-8").rstrip("\r\n")
    if not password or os.environ.get("PGUSER", "sf_app_runtime") != "sf_app_runtime":
        raise ValueError("Expected the SupportFlow runtime role and its password file")
    return AsyncConnectionPool(
        open=False, min_size=1, max_size=6, timeout=5, max_waiting=16,
        kwargs=dict(host=os.environ.get("PGHOST", "postgres"), port=int(os.environ.get("PGPORT", "5432")),
                    dbname=database, user="sf_app_runtime", password=password, connect_timeout=5,
                    application_name=os.environ.get("SF_API_INSTANCE", "supportflow-api"),
                    row_factory=dict_row, autocommit=True),
    )


async def connection(request: Request) -> AsyncIterator[AsyncConnection]:
    hints = []
    async with request.app.state.pool.connection() as conn:
        try:
            async with conn.transaction():
                yield conn
        except BaseException:
            event_hints.discard(conn)
            raise
        else:
            try:
                hints = await event_hints.committed_async(conn)
            except Exception as error:
                # An optional wakeup read must not turn a committed business
                # response into a retry. PostgreSQL history remains the source.
                event_hints.discard(conn)
                logging.getLogger('supportflow.event_hints').info('event_hint_read_unavailable error_type=%s', type(error).__name__)
    # The pool lease and all SQL transactions are gone before network I/O.
    try:
        await event_hints.publish_async(hints)
    except Exception as error:
        logging.getLogger('supportflow.event_hints').info('event_hint_publish_unavailable error_type=%s', type(error).__name__)


Db = Annotated[AsyncConnection, Depends(connection, scope="function")]
