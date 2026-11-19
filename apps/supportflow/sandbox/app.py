"""Authenticated internal HTTP reads over the separate business sandbox database."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from apps.supportflow.domain.clock import business_now
from apps.supportflow.domain.contracts import InvitationObservation, ServiceStatus, Subscription, ToolEnvelope
from apps.supportflow.domain.rules import normalize_email


def envelope(data=None, *, error=None, source="sandbox", retryable=False):
    return ToolEnvelope(ok=error is None, data=data, error_code=error, retryable=retryable,
                        source=source, observed_at=datetime.now(timezone.utc)).model_dump(mode="json")


class ReadError(Exception):
    def __init__(self, status, code, retryable=False):
        self.status, self.code, self.retryable = status, code, retryable


@asynccontextmanager
async def lifespan(app):
    database = os.environ.get("PGDATABASE", "sf_sandbox")
    if database != "sf_sandbox" and not re.fullmatch(r"sf_eval_[a-z0-9_]+_sandbox", database):
        raise ValueError("Expected the SupportFlow sandbox database")
    if os.environ.get("PGUSER", "sf_sandbox_runtime") != "sf_sandbox_runtime":
        raise ValueError("Expected the sandbox runtime role")
    token = Path(os.environ.get("SF_SANDBOX_TOKEN_FILE", "/run/secrets/sandbox_service_token")).read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise ValueError("Sandbox service credential is missing or too short")
    business_now()  # Validate any evaluation reference clock before serving requests.
    app.state.service_token = token
    yield


app = FastAPI(title="SupportFlow internal read sandbox", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(ReadError)
async def read_error(request, error):
    if request.url.path.startswith("/internal/v1/"):
        from apps.supportflow.sandbox.grants import grant_envelope
        source = "sandbox.temporary_grants" if request.method == "POST" else "sandbox.action_receipts"
        code = "UNAUTHORIZED" if error.status == 401 else "VALIDATION_ERROR"
        return JSONResponse(grant_envelope(error=code, source=source), status_code=401 if error.status == 401 else 422)
    return JSONResponse(envelope(error=error.code, retryable=error.retryable), status_code=error.status)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    if request.url.path.startswith("/internal/v1/"):
        from apps.supportflow.sandbox.grants import grant_envelope
        source = "sandbox.temporary_grants" if request.method == "POST" else "sandbox.action_receipts"
        return JSONResponse(grant_envelope(error="VALIDATION_ERROR", source=source), status_code=422)
    return JSONResponse(envelope(error="VALIDATION"), status_code=422)


from apps.supportflow.sandbox.grants import SandboxGrantError


@app.exception_handler(SandboxGrantError)
async def grant_error(request, error):
    from apps.supportflow.sandbox.grants import grant_envelope
    source = "sandbox.temporary_grants" if request.method == "POST" else "sandbox.action_receipts"
    return JSONResponse(grant_envelope(error=error.code, source=source,
        retryable=error.retryable if request.method == "GET" else False), status_code=error.status)


def service_identity(request: Request):
    expected = getattr(request.app.state, "service_token", "")
    supplied = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(supplied.encode(), ("Bearer " + expected).encode()):
        raise ReadError(401, "FORBIDDEN")
    tenant = request.headers.get("x-supportflow-tenant", "")
    actor = request.headers.get("x-supportflow-actor", "")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in (tenant, actor)):
        raise ReadError(400, "VALIDATION")
    # These headers are an authenticated backend assertion. The app rechecks
    # current membership/customer scope before every dispatch; the browser and
    # model cannot set them. The sandbox role cannot connect to the app database.
    return tenant, actor


def read_one(query, parameters):
    password = Path(os.environ.get("PGPASSWORD_FILE", "/run/secrets/sandbox_runtime_password")).read_text().strip()
    try:
        with psycopg.connect(host=os.environ.get("PGHOST", "postgres"), port=int(os.environ.get("PGPORT", "5432")),
            dbname=os.environ.get("PGDATABASE", "sf_sandbox"), user="sf_sandbox_runtime", password=password,
            autocommit=True, row_factory=dict_row, connect_timeout=3,
            options="-c statement_timeout=4000 -c lock_timeout=1000", application_name="supportflow-sandbox-read") as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                return conn.execute(query, parameters).fetchone()
    except psycopg.Error:
        raise ReadError(503, "UPSTREAM_UNAVAILABLE", True) from None


def safe_result(model, data, source):
    if data is None:
        raise ReadError(404, "NOT_FOUND")
    try:
        return envelope(model.model_validate(data).model_dump(mode="json"), source=source)
    except ValidationError:
        raise ReadError(502, "UPSTREAM_UNAVAILABLE") from None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/customers/{customer_id}/subscription")
def subscription(customer_id: str, identity=Depends(service_identity)):
    row = read_one("""SELECT subscription_id,base_seats,used_seats,temporary_seats,temporary_expires_at,
        plan_status,business_version FROM subscriptions WHERE tenant_id=%s AND customer_id=%s""", (identity[0], customer_id))
    return safe_result(Subscription, row, "sandbox.subscriptions")


@app.get("/api/v1/customers/{customer_id}/subscription-grant")
def subscription_grant(customer_id: str, identity=Depends(service_identity)):
    """One read-only transaction, no missing-history-as-empty fallback.

    The receipt table's rows represent committed business transactions. Legacy
    history deliberately projects its original fields, including old receipts.
    """
    from apps.supportflow.domain.grant_contracts import GrantSubscriptionObservation
    row = read_one("""SELECT s.subscription_id,s.base_seats,s.used_seats,s.temporary_seats,s.temporary_expires_at,
        s.plan_status,s.business_version,
        jsonb_build_object('complete',(SELECT count(*)<=50 FROM action_receipts r
            WHERE r.tenant_id=s.tenant_id AND r.customer_id=s.customer_id AND r.subscription_id=s.subscription_id),
            'receipts',COALESCE((SELECT jsonb_agg(to_jsonb(h)) FROM (
                SELECT r.action_id,r.tenant_id,r.customer_id,r.subscription_id,r.canonical_args_hash,
                    r.before_version,r.after_version,r.committed_business_at,r.seats,r.days,
                    r.temporary_expires_at,r.policy_version FROM action_receipts r
                    WHERE r.tenant_id=s.tenant_id AND r.customer_id=s.customer_id
                    AND r.subscription_id=s.subscription_id ORDER BY r.committed_business_at DESC,r.action_id LIMIT 51
            ) h),'[]'::jsonb)) AS grant_history,
        CASE WHEN p.singleton IS NULL THEN NULL ELSE jsonb_build_object('policy_version',p.policy_version,
            'section_id',p.section_id,'content_sha256',p.content_sha256,'constants',p.constants) END AS policy
        FROM subscriptions s LEFT JOIN grant_policy_state p ON p.singleton
        WHERE s.tenant_id=%s AND s.customer_id=%s""", (identity[0], customer_id))
    if row is None:
        raise ReadError(404, "NOT_FOUND")
    try:
        def encode(value):
            if isinstance(value, datetime):
                return value.isoformat()
            raise TypeError("Unsupported business observation value")
        text = json.dumps(row, ensure_ascii=False, default=encode, allow_nan=False)
        data = GrantSubscriptionObservation.model_validate_json(text).model_dump(mode="json")
        return envelope(data, source="sandbox.subscription_grant")
    except (ValueError, TypeError):
        raise ReadError(502, "UPSTREAM_UNAVAILABLE") from None


@app.get("/api/v1/customers/{customer_id}/invitation")
def invitation(customer_id: str, email: str, identity=Depends(service_identity)):
    try:
        email = normalize_email(email)
    except ValueError:
        raise ReadError(422, "VALIDATION") from None
    row = read_one("""SELECT i.invitation_id,i.email,i.email_domain,i.created_at,i.expires_at,i.status,i.reason_code,
        p.allowed_domains,p.allow_subdomains,p.invitation_permission,
        COALESCE((SELECT jsonb_agg(to_jsonb(h) ORDER BY h.created_at DESC,h.invitation_id DESC) FROM (
            SELECT old.invitation_id,old.email,old.email_domain,old.created_at,old.expires_at,old.status,old.reason_code
            FROM invitations old WHERE old.tenant_id=i.tenant_id AND old.customer_id=i.customer_id
              AND old.email=i.email AND (old.created_at,old.invitation_id)<(i.created_at,i.invitation_id)
            ORDER BY old.created_at DESC,old.invitation_id DESC LIMIT 5
        ) h),'[]'::jsonb) AS history FROM invitations i
        LEFT JOIN invitation_policies p USING(tenant_id,customer_id)
        WHERE i.tenant_id=%s AND i.customer_id=%s AND i.email=%s
        ORDER BY i.created_at DESC,i.invitation_id DESC LIMIT 1""", (identity[0], customer_id, email))
    if row is None:
        raise ReadError(404, "NOT_FOUND")
    policy = {name: row.pop(name) for name in ("allowed_domains", "allow_subdomains", "invitation_permission")}
    history = row.pop("history")
    def encode_history(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError("Unsupported invitation observation value")
    # jsonb timestamps are strings; validate the complete observation in JSON mode.
    try:
        data = InvitationObservation.model_validate_json(json.dumps({"invitation": row, "policy": policy, "history": history},
            default=encode_history)).model_dump(mode="json")
        return envelope(data, source="sandbox.invitations")
    except (ValueError, TypeError):
        raise ReadError(502, "UPSTREAM_UNAVAILABLE") from None


@app.get("/api/v1/customers/{customer_id}/service-status")
def service_status(customer_id: str, identity=Depends(service_identity)):
    row = read_one("""SELECT component,status,updated_at FROM service_statuses
        WHERE tenant_id=%s AND customer_id=%s AND component='invitations'""", (identity[0], customer_id))
    return safe_result(ServiceStatus, row, "sandbox.service_statuses")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


@app.post("/internal/v1/subscriptions/{subscription_id}/temporary-seat-grants")
async def temporary_seat_grant(subscription_id: str, request: Request):
    # Authenticate before parsing the body: malformed user input must not become
    # an unauthenticated schema oracle. There is no browser-cookie fallback.
    identity = service_identity(request)
    from apps.supportflow.domain.grant_commands import GrantCommand
    from apps.supportflow.sandbox.grants import grant, grant_envelope
    try:
        body = await request.body()
        if len(body) > 65536:
            raise ValueError("Command too large")
        value = json.loads(body, object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        command = GrantCommand.model_validate_json(json.dumps(value, ensure_ascii=False, allow_nan=False))
        if command.subscription_id != subscription_id or request.query_params:
            raise ValueError("Route/command mismatch or unexpected query")
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise SandboxGrantError(422, "VALIDATION_ERROR") from None
    receipt, created = await run_in_threadpool(grant, command, tenant_id=identity[0])
    return JSONResponse(grant_envelope(data=receipt), status_code=201 if created else 200)


@app.get("/internal/v1/actions/{action_id}")
async def action_receipt(action_id: str, request: Request):
    identity = service_identity(request)
    from apps.supportflow.domain.grant_commands import ReceiptQuery
    from apps.supportflow.sandbox.grants import get_receipt, grant_envelope
    try:
        pairs = list(request.query_params.multi_items())
        if (len(pairs) != len(ReceiptQuery.model_fields) or
                len({key for key, _ in pairs}) != len(pairs) or
                not action_id.strip() or "\0" in action_id or len(action_id)>128):
            raise ValueError("Missing, extra, or repeated receipt query field")
        query = ReceiptQuery.model_validate(dict(pairs))
    except (ValueError, TypeError):
        raise SandboxGrantError(422, "VALIDATION_ERROR") from None
    receipt = await run_in_threadpool(get_receipt, action_id, identity[0], query)
    return grant_envelope(data=receipt, source="sandbox.action_receipts")
