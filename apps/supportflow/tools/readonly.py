"""Bounded HTTP reads and append-only observations on the worker's locked session."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
from time import monotonic, sleep

import httpx
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from apps.supportflow.domain.contracts import Customer, InvitationObservation, ServiceStatus, Subscription, ToolEnvelope
from apps.supportflow.domain.rules import normalize_email
from apps.supportflow.jobs.contracts import StopFragment


MAX_BYTES = 65536
MAX_PROJECTION_BYTES = 6000
TIMEOUT_SECONDS = 8
SANDBOX_URL = "http://sandbox:8001"
ENVELOPE_FIELDS = {"ok", "data", "error_code", "retryable", "source", "observed_at", "schema_version"}
log = logging.getLogger(__name__)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def failure(code, source, retryable=False):
    return ToolEnvelope(ok=False, data=None, error_code=code, retryable=retryable, source=source,
                        observed_at=datetime.now(timezone.utc))


@dataclass(frozen=True)
class ToolDefinition:
    tool_id: str
    endpoint: str | None
    output_model: type
    source: str
    required_capability: str = "customer:read"
    timeout_seconds: int = TIMEOUT_SECONDS
    result_limit_bytes: int = MAX_BYTES


TOOLS = {
    "get_customer": ToolDefinition("get_customer", None, Customer, "app_db.customers"),
    "get_subscription": ToolDefinition("get_subscription", "subscription", Subscription, "sandbox.subscriptions"),
    "get_invitation": ToolDefinition("get_invitation", "invitation", InvitationObservation, "sandbox.invitations"),
    "get_service_status": ToolDefinition("get_service_status", "service-status", ServiceStatus, "sandbox.service_statuses"),
}


@dataclass(frozen=True)
class ReadAttempt:
    envelope: ToolEnvelope
    raw_bytes: int
    duration_ms: float
    ordinal: int


class SandboxClient:
    """A local async HTTP deadline; no async client survives the synchronous read."""
    def __init__(self, *, token=None, base_url=None, transport=None):
        self.base_url = base_url if base_url is not None else os.environ.get("SF_SANDBOX_BASE_URL", SANDBOX_URL)
        if self.base_url != SANDBOX_URL:
            raise ValueError("Sandbox reads require the fixed internal service URL")
        try:
            self._token = token if token is not None else Path(os.environ.get(
                "SF_SANDBOX_TOKEN_FILE", "/run/secrets/sandbox_service_token")).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise ValueError("Sandbox service credential is unavailable") from None
        if not isinstance(self._token, str) or re.fullmatch(r"[A-Za-z0-9._~+/-]{32,512}={0,2}", self._token) is None:
            raise ValueError("Sandbox service credential is missing or invalid")
        self.transport = transport

    async def _request(self, definition, context, email, timeout):
        source = definition.source
        path = self.base_url.rstrip("/") + "/api/v1/customers/" + context["customer_id"] + "/" + definition.endpoint
        headers = {"Authorization": "Bearer " + self._token, "X-SupportFlow-Tenant": context["tenant_id"],
                   "X-SupportFlow-Actor": context["actor_id"], "X-SupportFlow-Run": context["run_id"],
                   "Accept": "application/json", "Accept-Encoding": "identity"}
        parameters = {"email": email} if definition.tool_id == "get_invitation" else None
        try:
            async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(3, timeout)),
                    transport=self.transport, trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", path, params=parameters, headers=headers) as response:
                    if (response.headers.get("content-encoding", "identity").lower() != "identity"
                            or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"):
                        return failure("UPSTREAM_UNAVAILABLE", source), False
                    data = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=8192):
                        if len(data) + len(chunk) > MAX_BYTES:
                            return failure("RESULT_TOO_LARGE", source), False
                        data.extend(chunk)
                    if self._token.encode() in data:
                        return failure("UPSTREAM_UNAVAILABLE", source), False
                    # JSON validation occurs in JSON mode so RFC3339 timestamp
                    # strings are accepted without weakening strict integer/bool DTOs.
                    decoded = json.loads(bytes(data), object_pairs_hook=unique_object,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-JSON number")))
                    if (not isinstance(decoded, dict) or set(decoded) != ENVELOPE_FIELDS
                            or type(decoded["schema_version"]) is not int or decoded["schema_version"] != 1):
                        return failure("UPSTREAM_UNAVAILABLE", source), False
                    normalized = compact(decoded)
                    # JSON escaping must not hide an echoed credential from the
                    # audit boundary. The upstream body is never included in errors.
                    if self._token.encode() in normalized:
                        return failure("UPSTREAM_UNAVAILABLE", source), False
                    envelope = ToolEnvelope.model_validate_json(normalized)
                    if response.status_code == 200:
                        if not envelope.ok or envelope.source != source:
                            return failure("UPSTREAM_UNAVAILABLE", source), False
                        validated = definition.output_model.model_validate_json(compact(envelope.data))
                        if definition.tool_id == "get_invitation" and validated.invitation.email != email:
                            return failure("UPSTREAM_UNAVAILABLE", source), False
                    else:
                        allowed = {400: "VALIDATION", 401: "FORBIDDEN", 403: "FORBIDDEN", 404: "NOT_FOUND",
                                   408: "TIMEOUT", 422: "VALIDATION", 429: "RATE_LIMIT", 504: "TIMEOUT",
                                   500: "UPSTREAM_UNAVAILABLE", 503: "UPSTREAM_UNAVAILABLE", 502: "UPSTREAM_UNAVAILABLE"}
                        if (envelope.ok or allowed.get(response.status_code) != envelope.error_code or
                                envelope.source not in {"sandbox", source} or
                                envelope.retryable and envelope.error_code in {"FORBIDDEN", "NOT_FOUND", "VALIDATION"}):
                            return failure("UPSTREAM_UNAVAILABLE", source), False
                        envelope = envelope.model_copy(update={"source": source})
                    return envelope, False
        except (httpx.TimeoutException, TimeoutError):
            return failure("TIMEOUT", source, True), True
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            return failure("UPSTREAM_UNAVAILABLE", source, True), True
        except (httpx.HTTPError, ValueError, TypeError, RecursionError, ValidationError):
            return failure("UPSTREAM_UNAVAILABLE", source), False

    def read(self, tool_id, context, *, invitation_email=None, timeout=TIMEOUT_SECONDS, grant_subscription=False,
             start_attempt=None, finish_attempt=None):
        definition = TOOLS.get(tool_id)
        if type(grant_subscription) is not bool or grant_subscription and tool_id != "get_subscription":
            raise ValueError("Invalid subscription observation contract")
        if grant_subscription:
            from apps.supportflow.domain.grant_contracts import GrantSubscriptionObservation
            definition = ToolDefinition("get_subscription", "subscription-grant", GrantSubscriptionObservation, "sandbox.subscription_grant")
        if definition is None or definition.endpoint is None:
            raise ValueError("HTTP read tool is not registered")
        if not isinstance(context, dict) or any(
            not isinstance(context.get(field), str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", context[field]) is None
            for field in ("tenant_id", "actor_id", "customer_id", "run_id")
        ):
            raise ValueError("Trusted sandbox identity is missing or invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
            raise ValueError("Expected a finite remaining tool budget")
        if invitation_email is not None:
            invitation_email = normalize_email(invitation_email)
        if tool_id == "get_invitation" and invitation_email is None:
            raise ValueError("Invitation email is required")
        deadline = monotonic() + max(0, min(TIMEOUT_SECONDS, timeout))
        results = []
        for index in range(2):
            ordinal = start_attempt() if start_attempt else index + 1
            started = monotonic()
            remaining = deadline - started
            if remaining <= 0:
                envelope, retry = failure("TIMEOUT", definition.source, True), False
            else:
                envelope, retry = asyncio.run(self._request(definition, context, invitation_email, remaining))
            # A stored raw result is the full validated envelope, including a
            # sanitized envelope for rejected upstream data; no oversized body is stored.
            raw_bytes = len(compact(envelope.model_dump(mode="json")))
            if raw_bytes > MAX_BYTES:
                envelope = failure("RESULT_TOO_LARGE", definition.source)
                raw_bytes = len(compact(envelope.model_dump(mode="json")))
                retry = False
            result = ReadAttempt(envelope, raw_bytes, (monotonic()-started)*1000, ordinal)
            if finish_attempt:
                finish_attempt(result)
            results.append(result)
            if not retry or index == 1 or deadline - monotonic() <= .05:
                break
            sleep(.05)
        return results


class ToolExecutor:
    def __init__(self, conn, run, gate, *, client=None, grant_subscription=False):
        self.conn, self.run, self.gate = conn, run, gate
        self.client = client
        if type(grant_subscription) is not bool:
            raise ValueError("Invalid subscription observation contract")
        self.grant_subscription = grant_subscription

    def progress(self, event, payload):
        from apps.supportflow.jobs.core import append_progress
        append_progress(self.conn, self.run["run_id"], self.run["execution_generation"], event, payload)

    def authorize(self):
        # This transaction is complete before HTTP dispatch. Membership and scope
        # are read from current backend rows, never from messages/checkpoint claims.
        with self.conn.transaction():
            self.gate.check("tool.authorization")
            row = self.conn.execute("""SELECT r.run_id,r.tenant_id,r.actor_id,c.customer_id,c.customer_code,c.display_name
                FROM runs r JOIN threads h USING(tenant_id,thread_id,actor_id)
                JOIN tickets t USING(tenant_id,ticket_id)
                JOIN customers c ON c.tenant_id=t.tenant_id
                    AND c.customer_id=COALESCE(r.confirmed_fields->>'customer_id',t.customer_id)
                JOIN memberships m ON m.tenant_id=r.tenant_id AND m.user_id=r.actor_id
                JOIN customer_scopes s ON s.tenant_id=r.tenant_id AND s.user_id=r.actor_id AND s.customer_id=c.customer_id
                WHERE r.run_id=%s AND r.execution_generation=%s AND m.role IN ('support','supervisor','admin')
                FOR SHARE OF m,s,c,t,h""", (self.run["run_id"], self.run["execution_generation"])).fetchone()
            if row is None:
                raise StopFragment("AUTHORIZATION_REVOKED")
            if [row[k] for k in ("tenant_id", "actor_id", "customer_id")] != [self.run[k] for k in ("tenant_id", "actor_id", "customer_id")]:
                raise StopFragment("CUSTOMER_BINDING_CHANGED")
            return row

    def persist(self, tool_id, attempt, *, model_visible=True):
        raw = attempt.envelope.model_dump(mode="json")
        projection_bytes = len(compact(raw)) if model_visible else 0
        if model_visible and projection_bytes > MAX_PROJECTION_BYTES:
            # Preserve the raw bounded record, but refuse to silently drop facts
            # when this projection cannot fit the documented observation budget.
            projection_bytes = 0
        reference = {"run_id": self.run["run_id"], "execution_generation": self.run["execution_generation"],
                     "ordinal": attempt.ordinal, "tool_id": tool_id}
        with self.conn.transaction():
            self.gate.check("tool.persist")
            self.conn.execute("""INSERT INTO run_tool_observations
                (run_id,execution_generation,ordinal,tool_id,envelope,observed_at,raw_bytes,projection_bytes,duration_ms)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (reference["run_id"], reference["execution_generation"],
                reference["ordinal"], tool_id, Jsonb(raw), attempt.envelope.observed_at, attempt.raw_bytes,
                projection_bytes, attempt.duration_ms))
        self.progress("tool.finished", {"tool_id": tool_id, "ok": attempt.envelope.ok})
        if model_visible and projection_bytes == 0:
            raise StopFragment("RESULT_TOO_LARGE")
        return reference

    def execute(self, tool_id, *, invitation_email=None):
        if tool_id not in TOOLS:
            raise StopFragment("MODEL_PROTOCOL_ERROR")
        context = self.authorize()
        references = []
        def started():
            self.authorize()
            self.progress("tool.started", {"tool_id": tool_id})
            return self.gate.reserve_tool()
        def finished(attempt):
            self.authorize()
            references.append(self.persist(tool_id, attempt))
        if tool_id == "get_customer":
            ordinal = started()
            start = monotonic()
            context = self.authorize()
            data = Customer.model_validate({key: context[key] for key in Customer.model_fields}).model_dump(mode="json")
            envelope = ToolEnvelope(ok=True, data=data, error_code=None, retryable=False,
                                    source=TOOLS[tool_id].source, observed_at=datetime.now(timezone.utc))
            finished(ReadAttempt(envelope, len(compact(envelope.model_dump(mode="json"))), (monotonic()-start)*1000, ordinal))
        else:
            client = self.client or SandboxClient()
            timeout = min(TIMEOUT_SECONDS, self.gate.check("tool.http.before"))
            options = {"grant_subscription": True} if tool_id == "get_subscription" and self.grant_subscription else {}
            attempts = client.read(tool_id, context, invitation_email=invitation_email, timeout=timeout,
                                   start_attempt=started, finish_attempt=finished, **options)
            envelope = attempts[-1].envelope
        return envelope, references

    def execute_external(self, repository):
        """Journal a read-only clue separately from internal facts and model observations."""
        from apps.supportflow.domain.github_clues import TOOL_ID, currently_allowed
        from apps.supportflow.tools.github import ExternalIssuesInput, GitHubIssuesClient

        context = self.authorize()
        if not currently_allowed(context["tenant_id"], repository):
            return None
        recorded = self.conn.execute("""SELECT run_id,execution_generation,ordinal,tool_id
            FROM run_tool_observations WHERE run_id=%s AND execution_generation<=%s AND tool_id=%s
            ORDER BY execution_generation DESC,ordinal DESC LIMIT 1""",
            (self.run["run_id"], self.run["execution_generation"], TOOL_ID)).fetchone()
        if recorded is not None:
            return dict(recorded)
        remaining = self.gate.check("external_issues.before")
        if remaining <= 1:
            return None
        try:
            ordinal = self.gate.reserve_tool()
        except StopFragment as error:
            if error.reason_code == "TOOL_ROUNDS_EXHAUSTED":
                return None
            raise
        self.progress("tool.started", {"tool_id": TOOL_ID})
        started = monotonic()
        owner, repo = repository.split("/")
        try:
            client = GitHubIssuesClient.from_environment()
        except ValueError:
            envelope = failure("UPSTREAM_UNAVAILABLE", "github")
        else:
            def observe(row):
                log.info("github_issue_request run_id=%s ordinal=%d observation=%s",
                         self.run["run_id"], ordinal, compact(row).decode("utf-8"))

            # No customer details or user prose become outbound query parameters.
            query = ExternalIssuesInput(owner=owner, repo=repo, state="all")
            envelope = client.list_issues(query, self.authorize(),
                                          timeout=min(TIMEOUT_SECONDS, remaining - .5), observe=observe,
                                          before_request=self.authorize)
        self.authorize()
        size = len(compact(envelope.model_dump(mode="json")))
        if size > MAX_BYTES:
            envelope = failure("RESULT_TOO_LARGE", "github")
            size = len(compact(envelope.model_dump(mode="json")))
        attempt = ReadAttempt(envelope, size, (monotonic() - started) * 1000, ordinal)
        return self.persist(TOOL_ID, attempt, model_visible=False)

    def observations(self, references):
        self.authorize()
        observations = {}
        for ref in references:
            if ref["run_id"] != self.run["run_id"]:
                raise StopFragment("CHECKPOINT_INVALID")
            if ref["tool_id"] == "list_external_issues":
                # An external clue is never eligible as a trusted diagnostic fact.
                continue
            row = self.conn.execute("""SELECT tool_id,envelope FROM run_tool_observations
                WHERE run_id=%s AND execution_generation=%s AND ordinal=%s""",
                (ref["run_id"], ref["execution_generation"], ref["ordinal"])).fetchone()
            if row is None or row["tool_id"] != ref["tool_id"]:
                raise StopFragment("CHECKPOINT_INVALID")
            observations[row["tool_id"]] = ToolEnvelope.model_validate_json(compact(row["envelope"]))
        return observations
