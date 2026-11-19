"""One resource lock and one business transaction for each internal grant.

The sandbox authenticates the backend assertion in app.py. It rechecks the facts
owned by this database; app membership and other services are not queried here.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from apps.supportflow.domain.clock import business_now
from apps.supportflow.domain.contracts import Subscription
from apps.supportflow.domain.grant_commands import GrantCommand, GrantCommandEnvelope, InternalGrantReceipt, ReceiptQuery
from apps.supportflow.domain.grant_contracts import GrantHistory, GrantReceipt
from apps.supportflow.domain.grant_policy import _history_reason, load_policy_bundle

LEGACY_RECEIPT_COLUMNS = tuple(GrantReceipt.model_fields)
RECEIPT_COLUMNS = tuple(InternalGrantReceipt.model_fields)


class SandboxGrantError(Exception):
    def __init__(self, status: int, code: str, *, retryable: bool = False):
        self.status, self.code, self.retryable = status, code, retryable


def grant_envelope(*, data=None, error=None, source="sandbox.temporary_grants", retryable=False):
    return GrantCommandEnvelope(ok=error is None, data=data, error_code=error, retryable=retryable,
        source=source, observed_at=datetime.now(timezone.utc)).model_dump(mode="json")


def connect(*, readonly=False):
    password = Path(os.environ.get("PGPASSWORD_FILE", "/run/secrets/sandbox_runtime_password")).read_text().strip()
    return psycopg.connect(host=os.environ.get("PGHOST", "postgres"), port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "sf_sandbox"), user="sf_sandbox_runtime", password=password,
        autocommit=True, row_factory=dict_row, connect_timeout=2,
        options="-c statement_timeout=4000 -c lock_timeout=3000",
        application_name="supportflow-sandbox-receipt" if readonly else "supportflow-sandbox-grant")


def grant_hook(point, conn, command):
    """Production no-op; a private disposable eval bootstrap can replace this.

    The application never imports fixtures or accepts a fault/clock HTTP header.
    """


def commit_reference(conn, command):
    return business_now(real_now=conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"])


def _receipt(row, *, tenant_id, customer_id, subscription_id, canonical_args_hash, command_replay=False):
    if row is None:
        return None
    if any(row[name] != value for name, value in (
            ("tenant_id", tenant_id), ("customer_id", customer_id), ("subscription_id", subscription_id))):
        if command_replay and row["tenant_id"] == tenant_id:
            raise SandboxGrantError(409, "IDEMPOTENCY_CONFLICT")
        raise SandboxGrantError(404, "NOT_FOUND")
    if row["canonical_args_hash"] != canonical_args_hash:
        raise SandboxGrantError(409, "IDEMPOTENCY_CONFLICT")
    try:
        return InternalGrantReceipt.model_validate(row)
    except (ValueError, TypeError, OverflowError):
        raise SandboxGrantError(409, "RECEIPT_INCOMPATIBLE") from None


def _lookup(conn, action_id, *, tenant_id, customer_id, subscription_id, canonical_args_hash, command_replay=False):
    row = conn.execute("SELECT " + ",".join(RECEIPT_COLUMNS) + " FROM action_receipts WHERE action_id=%s",
                       (action_id,)).fetchone()
    return _receipt(row, tenant_id=tenant_id, customer_id=customer_id, subscription_id=subscription_id,
                    canonical_args_hash=canonical_args_hash, command_replay=command_replay)


def get_receipt(action_id: str, tenant_id: str, query: ReceiptQuery):
    try:
        with connect(readonly=True) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                receipt = _lookup(conn, action_id, tenant_id=tenant_id, **query.model_dump())
                if receipt is None:
                    raise SandboxGrantError(404, "NOT_FOUND")
                return receipt
    except psycopg.Error:
        raise SandboxGrantError(503, "DEPENDENCY_UNAVAILABLE", retryable=True) from None


def _validate_current(conn, command, subscription, now, policy_row):
    try:
        pinned = load_policy_bundle()
        # Python equality considers True == 1 and False == 0; policy constants
        # are a typed contract, so compare their canonical JSON representations.
        if (json.dumps(policy_row, sort_keys=True, separators=(",", ":"), allow_nan=False) !=
                json.dumps(pinned, sort_keys=True, separators=(",", ":"), allow_nan=False)
                or command.policy_version != pinned["policy_version"]):
            raise ValueError("Current policy differs from the product pin")
    except (ValueError, TypeError, OSError):
        raise SandboxGrantError(409, "POLICY_INVALID") from None
    if now >= command.proposal_expires_at:
        raise SandboxGrantError(409, "PROPOSAL_EXPIRED")
    if subscription.business_version != command.expected_business_version:
        raise SandboxGrantError(409, "BUSINESS_VERSION_CHANGED")
    if subscription.business_version == 2**63-1:
        raise SandboxGrantError(409, "BUSINESS_VERSION_EXHAUSTED")
    if subscription.plan_status != "active":
        code = "PLAN_NOT_ACTIVE" if subscription.plan_status in {"inactive", "cancelled", "suspended"} else "FACTS_INCOMPLETE"
        raise SandboxGrantError(409, code)
    rows = conn.execute("SELECT " + ",".join(LEGACY_RECEIPT_COLUMNS) + " FROM action_receipts "
        "WHERE tenant_id=%s AND customer_id=%s AND subscription_id=%s "
        "ORDER BY committed_business_at DESC,action_id LIMIT 51",
        (command.tenant_id, command.customer_id, command.subscription_id)).fetchall()
    try:
        history = GrantHistory(complete=len(rows)<=50, receipts=[GrantReceipt.model_validate(row) for row in rows[:50]])
        if len(rows)>50 or _history_reason(history, subscription, command.tenant_id, command.customer_id, now):
            raise ValueError("Incomplete or inconsistent committed grant history")
    except (ValueError, TypeError, OverflowError):
        raise SandboxGrantError(409, "GRANT_HISTORY_INVALID") from None
    if subscription.temporary_seats and subscription.temporary_expires_at is not None and now < subscription.temporary_expires_at:
        raise SandboxGrantError(409, "TEMPORARY_GRANT_ACTIVE")
    if rows and now < max(row["committed_business_at"] for row in rows)+timedelta(seconds=2592000):
        raise SandboxGrantError(409, "GRANT_COOLDOWN_ACTIVE")
    if subscription.base_seats-subscription.used_seats >= 1:
        raise SandboxGrantError(409, "CAPACITY_NOT_EXHAUSTED")
    if subscription.base_seats+command.seats-subscription.used_seats < 1:
        raise SandboxGrantError(409, "GRANT_CAPACITY_INSUFFICIENT")


def grant(command: GrantCommand, *, tenant_id: str):
    # Revalidate mutable/model_construct instances at the storage boundary too.
    try:
        command = GrantCommand.model_validate_json(command.model_dump_json())
    except (ValueError, TypeError):
        raise SandboxGrantError(422, "VALIDATION_ERROR") from None
    if command.tenant_id != tenant_id:
        raise SandboxGrantError(404, "NOT_FOUND")
    try:
        command.validate_canonical_hash()
    except (ValueError, OverflowError):
        raise SandboxGrantError(409, "IDEMPOTENCY_CONFLICT") from None
    scope = dict(tenant_id=tenant_id, customer_id=command.customer_id,
                 subscription_id=command.subscription_id, canonical_args_hash=command.canonical_args_hash, command_replay=True)
    try:
        with connect() as conn:
            with conn.transaction():
                receipt = _lookup(conn, command.action_id, **scope)
                if receipt is not None:
                    return receipt, False
                row = conn.execute("SELECT subscription_id,base_seats,used_seats,temporary_seats,temporary_expires_at,"
                    "plan_status,business_version FROM subscriptions WHERE tenant_id=%s AND customer_id=%s "
                    "AND subscription_id=%s FOR UPDATE",
                    (tenant_id, command.customer_id, command.subscription_id)).fetchone()
                if row is None:
                    raise SandboxGrantError(404, "NOT_FOUND")
                receipt = _lookup(conn, command.action_id, **scope)
                if receipt is not None:
                    return receipt, False
                try:
                    subscription = Subscription.model_validate(row)
                except ValidationError:
                    raise SandboxGrantError(409, "FACTS_INCOMPLETE") from None
                grant_hook("before_policy_check", conn, command)
                # This SELECT under the subscription lock is the policy-check
                # linearization point; the runtime role never writes policy.
                policy = conn.execute("SELECT policy_version,section_id,content_sha256,constants "
                                      "FROM grant_policy_state WHERE singleton").fetchone()
                now = commit_reference(conn, command)
                _validate_current(conn, command, subscription, now, policy)
                receipt = InternalGrantReceipt(action_id=command.action_id, tenant_id=tenant_id,
                    customer_id=command.customer_id, subscription_id=command.subscription_id,
                    canonical_args_hash=command.canonical_args_hash, before_version=subscription.business_version,
                    after_version=subscription.business_version+1, committed_business_at=now,
                    seats=command.seats, days=command.days, temporary_expires_at=now+timedelta(seconds=command.days*86400),
                    policy_version=command.policy_version, proposal_id=command.proposal_id,
                    authorization_id=command.authorization_id, authorized_at=command.authorized_at)
                conn.execute("UPDATE subscriptions SET temporary_seats=%s,temporary_expires_at=%s,business_version=%s "
                    "WHERE tenant_id=%s AND customer_id=%s AND subscription_id=%s",
                    (receipt.seats, receipt.temporary_expires_at, receipt.after_version,
                     tenant_id, command.customer_id, command.subscription_id))
                grant_hook("after_subscription_update_before_receipt_insert", conn, command)
                conn.execute("INSERT INTO action_receipts (" + ",".join(RECEIPT_COLUMNS) + ") VALUES (" +
                    ",".join(["%s"]*len(RECEIPT_COLUMNS)) + ")", tuple(getattr(receipt, key) for key in RECEIPT_COLUMNS))
            # Transaction context has committed before any response can escape.
            grant_hook("after_sandbox_commit_before_reply", conn, command)
            return receipt, True
    except psycopg.errors.UniqueViolation:
        # A different resource can race the global action PK. The failed context
        # has rolled back the entire subscription mutation before this lookup.
        try:
            receipt = get_receipt(command.action_id, tenant_id,
                ReceiptQuery(customer_id=command.customer_id, subscription_id=command.subscription_id,
                             canonical_args_hash=command.canonical_args_hash))
            return receipt, False
        except SandboxGrantError as error:
            if error.status == 503:
                raise SandboxGrantError(503, "DEPENDENCY_UNAVAILABLE") from None
            raise
    except psycopg.Error:
        raise SandboxGrantError(503, "DEPENDENCY_UNAVAILABLE") from None
