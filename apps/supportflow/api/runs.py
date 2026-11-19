"""Create one authorized run and its initial dispatch intent atomically."""
from datetime import datetime
import hashlib
import json
import re
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

from .auth import ActorView, ApiError, Db, ReadActor, WriteActor
from .objects import Identifier, Page, PageQuery, VISIBLE_TICKET, lock_customer_scope, not_found, principal, read_page
from apps.supportflow.jobs import event_hints
from apps.supportflow.agent.models import snapshot_settings
from apps.supportflow.domain.contracts import DiagnosisResult, ObservationRef, RagDiagnosisResult
from apps.supportflow.domain.recovered_diagnosis import RecoveredRagDiagnosisResult
from apps.supportflow.domain.github_clues import (
    ExternalIssuesView, TOOL_ID, currently_allowed, freeze_repository, project_issue_observation, snapshot_repository,
)
from apps.supportflow.domain.grant_effects import GrantExecutionResult
from apps.supportflow.domain.knowledge_answers import KnowledgeAnswerResult
from apps.supportflow.domain.capacity_diagnosis import CapacityDiagnosisResult
from apps.supportflow.domain.grant_policy_reply import GrantPolicyReply
from apps.supportflow.domain.proposals import ProposalView, public_proposal
from apps.supportflow.domain.input_refusal import InputRefusalResult
from .clarifications import PendingInput


router = APIRouter()
RunStatus = Literal[
    "queued", "running", "awaiting_input", "awaiting_approval", "resume_queued", "succeeded",
    "handoff", "rejected", "expired", "failed", "cancelled", "needs_reconciliation",
]
RequestKey = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[ -~]+$")]
TERMINAL_STATUSES = ("succeeded", "handoff", "rejected", "expired", "failed", "cancelled")
RUN_COLUMNS = "r.run_id, r.thread_id, r.status, r.message, r.version, r.last_event_seq, r.created_at, r.reason_code, r.result, r.confirmed_fields, r.cancel_requested"


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=8000)
    client_request_id: RequestKey | None = None
    requested_action: Literal["grant_temporary_seats"] | None = None
    seats: int | None = Field(default=None, ge=1, le=20)
    days: int | None = Field(default=None, ge=1, le=7)
    invitation_email: str | None = Field(default=None, min_length=3, max_length=254)

    @field_validator("invitation_email")
    @classmethod
    def email_value(cls, value):
        from apps.supportflow.domain.rules import normalize_email
        return None if value is None else normalize_email(value)

    @model_validator(mode="after")
    def explicit_request(self):
        if self.requested_action is None and any(value is not None for value in (self.seats, self.days, self.invitation_email)):
            raise ValueError("Action parameters require an explicit requested action")
        return self

    @field_validator("message")
    @classmethod
    def valid_message(cls, value: str) -> str:
        if not value.strip() or "\0" in value:
            raise ValueError("Expected a nonempty message without NUL")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Expected valid UTF-8 text") from None
        return value


class RunAccepted(BaseModel):
    run_id: str
    status: RunStatus


class CustomerRunFacts(BaseModel):
    customer_id: str
    customer_code: str
    display_name: str


class CustomerRunResult(BaseModel):
    schema_version: Literal[1]
    resolution_code: str
    customer: CustomerRunFacts | None
    reply_text: str


class RunExecutionConfiguration(BaseModel):
    graph_version: str | None = None
    model_mode: Literal["live", "scripted"] | None = None
    model_id: str | None = None
    max_output_tokens: int | None = None
    tool_round_limit: int | None = None
    budget_ms: int


class RunSummary(RunAccepted):
    thread_id: str
    ticket_id: str
    ticket_title: str
    created_at: datetime


class RunView(RunAccepted):
    thread_id: str
    message: str
    version: int
    last_event_seq: int
    created_at: datetime
    reason_code: str | None
    result: CustomerRunResult | DiagnosisResult | RagDiagnosisResult | RecoveredRagDiagnosisResult | GrantExecutionResult | KnowledgeAnswerResult | CapacityDiagnosisResult | GrantPolicyReply | InputRefusalResult | None
    cancel_requested: bool = False
    pending_input: PendingInput | None = None
    proposal: ProposalView | None = None
    execution_configuration: RunExecutionConfiguration | None = None
    external_issues: ExternalIssuesView | None = None

    @field_validator("proposal", mode="before")
    @classmethod
    def json_proposal(cls, value):
        # Canonical proposal projection is JSON; retain strict numeric fields
        # while parsing its RFC3339 dates through the JSON DTO boundary.
        if isinstance(value, dict):
            return ProposalView.model_validate_json(json.dumps(value, ensure_ascii=False))
        return value

    @field_validator("result", mode="before")
    @classmethod
    def json_result(cls, value):
        # JSONB retains ISO timestamps as JSON strings. Strict domain contracts
        # parse these through the JSON boundary, rather than coercing Python values.
        if isinstance(value, dict) and value.get("schema_version") == 2:
            return DiagnosisResult.model_validate_json(json.dumps(value, ensure_ascii=False))
        if isinstance(value, dict) and value.get("schema_version") == 3:
            return RagDiagnosisResult.model_validate_json(json.dumps(value, ensure_ascii=False))
        if isinstance(value, dict) and value.get("schema_version") == 8:
            return RecoveredRagDiagnosisResult.model_validate_json(json.dumps(value, ensure_ascii=False))
        if isinstance(value, dict) and value.get("schema_version") == 4:
            return GrantExecutionResult.model_validate_json(json.dumps(value, ensure_ascii=False))
        if isinstance(value, dict) and value.get("schema_version") == 5:
            return KnowledgeAnswerResult.model_validate_json(json.dumps(value, ensure_ascii=False))
        if isinstance(value, dict) and value.get("schema_version") == 6:
            return CapacityDiagnosisResult.model_validate_json(json.dumps(value, ensure_ascii=False))
        if isinstance(value, dict) and value.get("schema_version") == 7:
            return GrantPolicyReply.model_validate_json(json.dumps(value, ensure_ascii=False))
        return value


def public_execution_configuration(snapshot: dict, budget_ms: int) -> RunExecutionConfiguration:
    """Project the frozen configuration through the browser's small allowlist."""
    fields = {"budget_ms": budget_ms}
    for name in ("graph_version", "model_id"):
        value = snapshot.get(name)
        if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value)
                and not value.lower().startswith("sk-")):
            fields[name] = value
    if snapshot.get("model_mode") in ("live", "scripted"):
        fields["model_mode"] = snapshot["model_mode"]
    for name in ("max_output_tokens", "tool_round_limit"):
        value = snapshot.get(name)
        if type(value) is int and 0 < value <= 2**31 - 1:
            fields[name] = value
    return RunExecutionConfiguration(**fields)


def resolve_request_key(body: CreateRun, request: Request) -> str:
    headers = request.headers.getlist("idempotency-key")
    if len(headers) > 1:
        raise ApiError(422, "VALIDATION_ERROR", "请求键无效。")
    header = headers[0] if headers else None
    if header is not None and body.client_request_id is not None and header != body.client_request_id:
        raise ApiError(422, "VALIDATION_ERROR", "请求头与正文的请求键不一致。")
    key = body.client_request_id if body.client_request_id is not None else header
    if key is None or not 1 <= len(key) <= 128 or not key.strip() or any(not 32 <= ord(char) <= 126 for char in key):
        raise ApiError(422, "VALIDATION_ERROR", "请求键无效。")
    return key


def canonical_request_hash(thread_id: str, message: str, *, confirmed_request: dict | None = None) -> str:
    payload = {"schema_version": 1, "operation": "create_run", "thread_id": thread_id, "message": message}
    if confirmed_request:
        payload["requested_action"] = confirmed_request["requested_action"]
        for name in ("seats", "days", "invitation_email"):
            if confirmed_request.get(name) is not None:
                payload[name] = confirmed_request[name]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def confirmed_request(body: CreateRun, customer_id: str | None) -> dict:
    """User-authenticated input, never a model/document statement of intent."""
    if body.requested_action is None:
        return {}
    result = body.model_dump(include={"requested_action", "seats", "days", "invitation_email"}, exclude_none=True)
    if customer_id is not None:
        result["customer_id"] = customer_id
    return result


async def authorized_thread(db: Db, actor: ActorView, thread_id: str, *, lock: bool) -> dict:
    suffix = " FOR SHARE OF h, t" if lock else ""
    cursor = await db.execute(f"""
        SELECT h.thread_id, t.customer_id FROM public.threads h
        JOIN public.tickets t USING (tenant_id, ticket_id)
        WHERE {VISIBLE_TICKET} AND h.actor_id = %(actor)s AND h.thread_id = %(thread)s
    """ + suffix, {**principal(actor), "thread": thread_id})
    thread = await cursor.fetchone()
    if thread is None:
        raise not_found()
    if lock and thread["customer_id"] is not None:
        await lock_customer_scope(db, actor, thread["customer_id"])
    return thread


async def read_external_clues(db: Db, actor: ActorView, row: dict) -> ExternalIssuesView | None:
    result = row.get("result")
    if not isinstance(result, dict) or result.get("schema_version") != 3 or result.get("intent") != "invitation_diagnosis":
        return None
    repository = snapshot_repository(row.get("config_snapshot") or {})
    if repository is None or not currently_allowed(actor.tenant_id, repository):
        return None
    refs = result.get("observation_refs", [])
    if not isinstance(refs, list):
        return None
    matches = [ref for ref in refs if isinstance(ref, dict) and ref.get("tool_id") == TOOL_ID]
    if len(matches) != 1:
        return None
    try:
        reference = ObservationRef.model_validate(matches[0])
    except ValidationError:
        return None
    if reference.run_id != row["run_id"]:
        return None
    customer = result.get("customer")
    if not isinstance(customer, dict) or not isinstance(customer.get("customer_id"), str):
        return None
    journal = await (await db.execute(f"""SELECT o.envelope FROM run_tool_observations o
        JOIN runs r USING(run_id)
        JOIN threads h USING(tenant_id,thread_id,actor_id)
        JOIN tickets t USING(tenant_id,ticket_id)
        WHERE {VISIBLE_TICKET} AND r.actor_id=%(actor)s AND o.run_id=%(run)s
          AND COALESCE(r.confirmed_fields->>'customer_id',t.customer_id)=%(customer)s
          AND o.execution_generation=%(generation)s AND o.execution_generation<=r.execution_generation
          AND o.ordinal=%(ordinal)s AND o.tool_id=%(tool)s""", {
        **principal(actor), "run": reference.run_id, "customer": customer["customer_id"],
        "generation": reference.execution_generation, "ordinal": reference.ordinal, "tool": TOOL_ID,
    })).fetchone()
    return None if journal is None else project_issue_observation(journal["envelope"], repository)


async def enrich_run(db: Db, actor: ActorView, row: dict) -> dict:
    """Current customer and knowledge access precede every history projection."""
    from .documents import mask_run_evidence
    confirmed = row.get("confirmed_fields", {}).get("customer_id")
    if confirmed is not None:
        await lock_customer_scope(db, actor, confirmed)
    row = await mask_run_evidence(db, actor, row)
    clues = await read_external_clues(db, actor, row)
    if clues is not None:
        row["external_issues"] = clues
    if row["status"] == "awaiting_input":
        pending = await (await db.execute("""
            SELECT input_id,version,prompt,required_fields,expires_at FROM pending_inputs
            WHERE tenant_id=%s AND run_id=%s AND status='pending' AND checkpoint_id IS NOT NULL
        """, (actor.tenant_id, row["run_id"]))).fetchone()
        if pending is not None:
            row["pending_input"] = pending
    proposal = await (await db.execute("""SELECT p.*,a.approval_id FROM proposals p LEFT JOIN approvals a USING(tenant_id,run_id,proposal_id)
        WHERE p.tenant_id=%s AND p.run_id=%s ORDER BY p.proposal_seq DESC LIMIT 1""", (actor.tenant_id, row["run_id"]))).fetchone()
    if proposal is not None:
        await mask_run_evidence(db, actor, {"result": {"schema_version": 3, "evidence_refs": proposal["evidence_refs"]}})
        row["proposal"] = public_proposal(proposal)
    return row


@router.post("/threads/{thread_id}/runs", status_code=202, response_model=RunAccepted)
async def create_run(thread_id: Identifier, body: CreateRun, request: Request, actor: WriteActor, db: Db):
    actor.require("runs:create")
    key = resolve_request_key(body, request)
    await db.execute("SET LOCAL lock_timeout = '5s'")
    await db.execute("SET LOCAL statement_timeout = '15s'")
    # This read deliberately precedes every lookup of a previously accepted key.
    await authorized_thread(db, actor, thread_id, lock=False)
    limits = await (await db.execute("""
        SELECT logical_queued FROM public.runtime_limits WHERE app = 'supportflow' FOR UPDATE
    """)).fetchone()
    if limits is None:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "运行准入尚未就绪。")
    # Recheck current grants after waiting, and hold them until this request commits.
    thread = await authorized_thread(db, actor, thread_id, lock=True)
    confirmed = confirmed_request(body, thread["customer_id"])
    request_hash = canonical_request_hash(thread_id, body.message, confirmed_request=confirmed)
    params = {**principal(actor), "thread": thread_id, "key": key, "hash": request_hash}
    previous = await (await db.execute("""
        SELECT i.request_hash, r.run_id, r.status
        FROM public.request_idempotency i JOIN public.runs r USING (tenant_id, actor_id, run_id)
        WHERE i.app = 'supportflow' AND i.tenant_id = %(tenant)s AND i.actor_id = %(actor)s
          AND i.operation = 'create_run' AND i.client_request_id = %(key)s
    """, params)).fetchone()
    if previous is not None:
        if previous["request_hash"] != request_hash:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "此请求键已用于其他请求。")
        from .clarifications import authorized_run
        await authorized_run(db, actor, previous["run_id"], lock=True)
        return {"run_id": previous["run_id"], "status": previous["status"]}

    unfinished = await (await db.execute("""
        SELECT 1 FROM public.runs WHERE tenant_id = %(tenant)s AND thread_id = %(thread)s
          AND status <> ALL(%(terminal)s)
    """, {**params, "terminal": list(TERMINAL_STATUSES)})).fetchone()
    if unfinished is not None:
        raise ApiError(409, "THREAD_BUSY", "此会话已有尚未结束的运行。")
    if limits["logical_queued"] >= 10:
        raise ApiError(429, "QUEUE_FULL", "待执行队列已满，请稍后重试。")

    run_id = str(uuid4())
    config = snapshot_settings()
    if body.requested_action is None:
        freeze_repository(config, actor.tenant_id)
    if body.requested_action is not None and config.get("graph_version") not in {"grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}:
        raise ApiError(503, "FEATURE_UNAVAILABLE", "临时席位申请暂未启用，请联系管理员。")
    params.update(run=run_id, message=body.message, received=request.state.api_received_at, config=Jsonb(config), confirmed=Jsonb(confirmed))
    await db.execute("UPDATE public.runtime_limits SET logical_queued = logical_queued + 1 WHERE app = 'supportflow'")
    await db.execute("""
        INSERT INTO public.runs (
            run_id, tenant_id, actor_id, thread_id, message, request_hash, status, admission_state,
            config_snapshot, api_received_at, budget_origin_at, budget_ms, confirmed_fields
        ) VALUES (
            %(run)s, %(tenant)s, %(actor)s, %(thread)s, %(message)s, %(hash)s, 'queued', 'queued',
            %(config)s, %(received)s, %(received)s, 90000, %(confirmed)s
        )
    """, params)
    await db.execute("""
        INSERT INTO public.request_idempotency (tenant_id, actor_id, operation, client_request_id, request_hash, run_id)
        VALUES (%(tenant)s, %(actor)s, 'create_run', %(key)s, %(hash)s, %(run)s)
    """, params)
    await db.execute("""
        INSERT INTO public.outbox (tenant_id, run_id, execution_generation, kind)
        VALUES (%(tenant)s, %(run)s, 1, 'create')
    """, params)
    await db.execute("""
        INSERT INTO public.run_events (run_id, tenant_id, seq, status_version, type, payload)
        VALUES (%(run)s, %(tenant)s, 1, 1, 'run.accepted', '{"status":"queued"}'::jsonb)
    """, params)
    await event_hints.mark_async(db, {"tenant_id": actor.tenant_id, "run_id": run_id, "last_event_seq": 1})
    return {"run_id": run_id, "status": "queued"}


@router.get("/runs", response_model=Page[RunSummary])
async def list_current_runs(actor: ReadActor, db: Db, query: Annotated[PageQuery, Query()]):
    actor.require("runs:read")
    from .documents import VISIBLE_DOCUMENT

    # The index returns only metadata. Filter revoked customer and knowledge
    # scope before paging so each page contains currently visible run links.
    return await read_page(db, f"""
        SELECT r.run_id, r.thread_id, t.ticket_id, t.title AS ticket_title, r.status, r.created_at
        FROM public.runs r
        JOIN public.threads h USING (tenant_id, thread_id, actor_id)
        JOIN public.tickets t USING (tenant_id, ticket_id)
        WHERE {VISIBLE_TICKET} AND r.actor_id = %(actor)s
          AND (r.confirmed_fields->>'customer_id' IS NULL OR EXISTS (
            SELECT 1 FROM public.customer_scopes s
            WHERE s.tenant_id = r.tenant_id AND s.user_id = %(actor)s
              AND s.customer_id = r.confirmed_fields->>'customer_id'
          ))
          AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(
              CASE WHEN r.result->'schema_version' IN ('3'::jsonb, '4'::jsonb, '5'::jsonb, '6'::jsonb, '7'::jsonb, '8'::jsonb)
                        AND jsonb_typeof(r.result->'evidence_refs') = 'array'
                   THEN r.result->'evidence_refs' ELSE '[]'::jsonb END
              || COALESCE((
                SELECT p.evidence_refs FROM public.proposals p
                WHERE p.tenant_id = r.tenant_id AND p.run_id = r.run_id
                ORDER BY p.proposal_seq DESC LIMIT 1
              ), '[]'::jsonb)
            ) AS ref(value)
            WHERE NOT EXISTS (
              SELECT 1 FROM public.documents d
              WHERE {VISIBLE_DOCUMENT} AND d.document_id = ref.value->>'document_id'
            )
          )
        ORDER BY r.created_at DESC, r.run_id DESC LIMIT %(fetch_limit)s OFFSET %(offset)s
    """, principal(actor), query)


class RunDetailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/runs/{run_id}", response_model=RunView, response_model_exclude_unset=True)
async def get_run(run_id: Identifier, actor: ReadActor, db: Db, query: Annotated[RunDetailQuery, Query()]):
    actor.require("runs:read")
    row = await (await db.execute(f"""
        SELECT {RUN_COLUMNS}, r.config_snapshot, r.budget_ms FROM public.runs r
        JOIN public.threads h USING (tenant_id, thread_id, actor_id)
        JOIN public.tickets t USING (tenant_id, ticket_id)
        WHERE {VISIBLE_TICKET} AND r.actor_id = %(actor)s AND r.run_id = %(run)s
    """, {**principal(actor), "run": run_id})).fetchone()
    if row is None:
        raise not_found()
    row = await enrich_run(db, actor, row)
    row["execution_configuration"] = public_execution_configuration(
        row.pop("config_snapshot"), row.pop("budget_ms"),
    )
    return row


@router.get("/threads/{thread_id}/runs", response_model=Page[RunView], response_model_exclude_unset=True)
async def list_runs(thread_id: Identifier, actor: ReadActor, db: Db, query: Annotated[PageQuery, Query()]):
    actor.require("runs:read")
    await authorized_thread(db, actor, thread_id, lock=True)
    page = await read_page(db, f"""
        SELECT {RUN_COLUMNS}, r.config_snapshot FROM public.runs r
        WHERE r.tenant_id = %(tenant)s AND r.actor_id = %(actor)s AND r.thread_id = %(thread)s
        ORDER BY r.created_at DESC, r.run_id DESC LIMIT %(fetch_limit)s OFFSET %(offset)s
    """, {**principal(actor), "thread": thread_id}, query)
    page["items"] = [await enrich_run(db, actor, item) for item in page["items"]]
    for item in page["items"]:
        item.pop("config_snapshot", None)
    return page
