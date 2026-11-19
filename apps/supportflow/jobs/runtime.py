"""One short-lived synchronous connection per worker fragment or scanner pass."""
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row

from .safe_connection import SafeConnection


def connect(component: str):
    database = os.environ.get("PGDATABASE", "sf_app")
    if database != "sf_app" and not re.fullmatch(r"sf_eval_[a-z0-9_]+_app", database):
        raise ValueError("Expected the SupportFlow application database")
    if os.environ.get("PGUSER", "sf_app_runtime") != "sf_app_runtime":
        raise ValueError("Expected the SupportFlow runtime role")
    password = Path(os.environ["PGPASSWORD_FILE"]).read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise ValueError("Expected a database password")
    return SafeConnection.connect(
        host=os.environ.get("PGHOST", "postgres"), port=int(os.environ.get("PGPORT", "5432")),
        dbname=database, user="sf_app_runtime", password=password, connect_timeout=5,
        application_name=f"supportflow-{component}-{os.getpid()}",
        autocommit=True, row_factory=dict_row, prepare_threshold=0,
        options="-c lock_timeout=1000 -c statement_timeout=5000",
    )


def trace(kind: str, **fields):
    # Callers pass identifiers, fixed reason codes and numeric measurements only.
    logging.getLogger("supportflow.jobs").info(json.dumps({"kind": kind, **fields}, sort_keys=True))


def broker_url():
    override = os.environ.get('SF_BROKER_URL')
    if override:
        return override
    secret = Path(os.environ.get('SF_REDIS_PASSWORD_FILE', '/run/secrets/redis_password')).read_text().strip()
    if not secret:
        raise ValueError('Redis credential file is empty')
    return 'redis://:' + quote(secret, safe='') + '@redis:6379/0'
