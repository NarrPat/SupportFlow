"""Authorize and accept one durable clarification without executing the graph."""
from datetime import datetime
from typing import Annotated, Literal

from apps.supportflow.jobs import event_hints

from fastapi import APIRouter, Request
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .auth import ActorView, Db, WriteActor
from .objects import Identifier, VISIBLE_TICKET, lock_customer_scope, not_found, principal
from .runtime import ApiError
from apps.supportflow.jobs.waiting import (
    canonical_answer, compatible_config, normalize_answers, valid_text, validate_required_fields, waiting_milliseconds, wait_has_expired,
)

router = APIRouter()
Key = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[ -~]+$")]


class PendingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    input_id: Identifier
    version: int = Field(ge=1, le=2**31-1)
    prompt: str = Field(min_length=1, max_length=1000)
    required_fields: list[Literal["customer_id", "invitation_email", "seats", "days"]]
    expires_at: AwareDatetime

    @field_validator("required_fields")
    @classmethod
    def necessary_fields(cls, value):
        return validate_required_fields(value)


class ClarificationSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    input_id: Identifier
    answers: dict
    refusal_reason: str | None = Field(default=None, min_length=1, max_length=500)
    expected_version: int = Field(ge=1, le=2**31-1)
    client_request_id: Key | None = None

    @field_validator("input_id")
    @classmethod
    def valid_input(cls, value):
        return valid_text(value, maximum=128)

    @model_validator(mode="after")
    def valid_response(self):
        if self.refusal_reason is not None:
            valid_text(self.refusal_reason, maximum=500)
            if self.answers:raise ValueError("Refusal cannot include confirmed answers")
        else:self.answers = normalize_answers(self.answers)
        return self


class ClarificationAccepted(BaseModel):
    run_id: str
    status: Literal["queued", "running", "awaiting_input", "awaiting_approval", "resume_queued",
                    "succeeded", "handoff", "rejected", "expired", "failed", "cancelled", "needs_reconciliation"]


async def authorized_run(db, actor: ActorView, run_id: str, *, lock: bool):
    suffix = " FOR UPDATE OF r FOR SHARE OF h,t" if lock else ""
    row = await (await db.execute("""SELECT r.*, t.customer_id AS ticket_customer_id,
        COALESCE(r.confirmed_fields->>'customer_id',t.customer_id) AS customer_id
        FROM runs r JOIN threads h USING(tenant_id,thread_id,actor_id)
        JOIN tickets t USING(tenant_id,ticket_id)
        WHERE """ + VISIBLE_TICKET + " AND r.run_id=%(run)s AND r.actor_id=%(actor)s" + suffix,
        {**principal(actor), "run": run_id})).fetchone()
    if row is None:
        raise not_found()
    confirmed = row["confirmed_fields"].get("customer_id")
    if confirmed is not None and row["ticket_customer_id"] not in (None, confirmed):
        raise not_found()
    if row["customer_id"] is not None:
        await visible_customer(db, actor, row["customer_id"], lock=lock)
    return row


async def visible_customer(db, actor, customer_id, *, lock):
    if lock:
        await lock_customer_scope(db, actor, customer_id)
    else:
        found = await (await db.execute("""SELECT 1 FROM customer_scopes
            WHERE tenant_id=%s AND user_id=%s AND customer_id=%s""",
            (actor.tenant_id, actor.actor_id, customer_id))).fetchone()
        if found is None:
            raise not_found()


async def event(db, run, event_type, payload):
    await db.execute("""INSERT INTO run_events
        (run_id,tenant_id,seq,status_version,execution_generation,type,payload)
        VALUES(%s,%s,%s,%s,%s,%s,%s)""", (run["run_id"], run["tenant_id"], run["last_event_seq"],
        run["version"], run["execution_generation"], event_type, Jsonb(payload)))
    await event_hints.mark_async(db, run)


async def close_wait(db, run, pending, *, now, status, reason_code, result=None):
    """The caller returns a Response after this, so the dependency commits it."""
    start = pending["wait_started_at"]
    end = max(start, now) if start is not None else None
    elapsed = waiting_milliseconds(start, end) if end is not None else 0
    await db.execute("""UPDATE pending_inputs SET status=%s,version=version+1,wait_ended_at=%s
        WHERE input_id=%s""", ("expired" if status == "expired" else "cancelled", end, pending["input_id"]))
    updated = await (await db.execute("""UPDATE runs SET status=%s,reason_code=%s,result=%s,
        version=version+1,last_event_seq=last_event_seq+1,waiting_since=NULL,wait_expires_at=NULL,
        accumulated_wait_ms=accumulated_wait_ms+%s WHERE run_id=%s RETURNING *""",
        (status, reason_code, Jsonb(result) if result is not None else None, elapsed, run["run_id"]))).fetchone()
    if result is not None:
        await event(db, updated, "message.final", {"message_id":run["run_id"]+":reply","text":result["reply_text"]})
        updated = await (await db.execute("UPDATE runs SET last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *",
            (run["run_id"],))).fetchone()
    await event(db, updated, "run.status_changed", {"status": status, "reason_code": reason_code})


async def wait_comparison_now(db, *, run_id: str, input_id: str, observed_now: datetime) -> datetime:
    """Production uses the real DB clock. Only an isolated test bootstrap replaces
    this function to hit an exact TTL boundary; it never replaces elapsed time.
    There is no client argument, environment flag or public fault control.
    """
    return observed_now


@router.post("/runs/{run_id}/clarifications", status_code=202, response_model=ClarificationAccepted)
async def submit_clarification(run_id: Identifier, body: ClarificationSubmit,
                               request: Request, actor: WriteActor, db: Db):
    # Lazy imports avoid a cycle when runs exposes the PendingInput DTO.
    from .main import error_response
    from .runs import resolve_request_key
    actor.require("runs:create")
    key = resolve_request_key(body, request)
    await db.execute("SET LOCAL lock_timeout='5s'")
    await db.execute("SET LOCAL statement_timeout='15s'")
    await authorized_run(db, actor, run_id, lock=False)
    if "customer_id" in body.answers:
        await visible_customer(db, actor, body.answers["customer_id"], lock=False)
    limits = await (await db.execute(
        "SELECT logical_queued FROM runtime_limits WHERE app='supportflow' FOR UPDATE")).fetchone()
    if limits is None:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "运行准入尚未就绪。")
    run = await authorized_run(db, actor, run_id, lock=True)
    if "customer_id" in body.answers:
        await visible_customer(db, actor, body.answers["customer_id"], lock=True)
    _, request_hash = canonical_answer(run_id, body.input_id, body.expected_version, body.answers, refusal_reason=body.refusal_reason)
    params = {**principal(actor), "key": key, "hash": request_hash, "run": run_id}
    previous = await (await db.execute("""SELECT i.request_hash,r.run_id,r.status
        FROM request_idempotency i JOIN runs r USING(tenant_id,actor_id,run_id)
        WHERE i.app='supportflow' AND i.tenant_id=%(tenant)s AND i.actor_id=%(actor)s
          AND i.operation IN ('submit_clarification','refuse_clarification') AND i.client_request_id=%(key)s""", params)).fetchone()
    if previous is not None:
        if previous["request_hash"] != request_hash:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "此请求键已用于其他请求。")
        return {"run_id": previous["run_id"], "status": previous["status"]}
    pending = await (await db.execute("""SELECT * FROM pending_inputs
        WHERE tenant_id=%s AND run_id=%s AND input_id=%s FOR UPDATE""",
        (actor.tenant_id, run_id, body.input_id))).fetchone()
    if pending is None:
        raise not_found()
    if (run["status"] != "awaiting_input" or pending["status"] != "pending"
            or pending["version"] != body.expected_version):
        raise ApiError(409, "INPUT_CONFLICT", "澄清状态或版本已变化。")
    try:
        answers = {} if body.refusal_reason is not None else normalize_answers(body.answers, pending["required_fields"])
    except ValueError:
        raise ApiError(422, "VALIDATION_ERROR", "答案必须恰好补齐当前必要字段。") from None
    now = (await (await db.execute("SELECT clock_timestamp() AS now")).fetchone())["now"]
    if not compatible_config(run["config_snapshot"]):
        await close_wait(db, run, pending, now=now, status="handoff", reason_code="CONFIG_VERSION_UNSUPPORTED")
        return error_response(request, ApiError(409, "CONFIG_VERSION_UNSUPPORTED", "当前运行配置不支持恢复，请重新发起。"))
    comparison_now = await wait_comparison_now(db, run_id=run_id, input_id=body.input_id, observed_now=now)
    if wait_has_expired(pending["expires_at"], comparison_now):
        await close_wait(db, run, pending, now=now, status="expired", reason_code="CLARIFICATION_EXPIRED")
        return error_response(request, ApiError(409, "CLARIFICATION_EXPIRED", "此澄清等待已到期。"))
    proposal = await (await db.execute("SELECT 1 FROM proposals WHERE tenant_id=%s AND run_id=%s LIMIT 1",
                                     (actor.tenant_id, run_id))).fetchone()
    if proposal is not None:
        raise ApiError(409, "PROPOSAL_IMMUTABLE", "已有提案不能修改确认字段。")
    if "customer_id" in answers and run["customer_id"] is not None:
        raise ApiError(409, "CUSTOMER_ALREADY_CONFIRMED", "当前运行已有确认客户。")
    if pending["checkpoint_id"] is None or pending["wait_started_at"] is None:
        raise ApiError(409, "INPUT_NOT_READY", "澄清状态尚未持久就绪。")
    if body.refusal_reason is not None:
        # An explicit refusal terminates the same waiting run. No resume outbox,
        # generation change, model inference, or queue capacity is required.
        from .input_refusal import policy_result
        result = await policy_result(db, actor, run, request)
        await close_wait(db, run, pending, now=now, status="handoff", reason_code="INPUT_DECLINED", result=result)
        await db.execute("""INSERT INTO request_idempotency
            (tenant_id,actor_id,operation,client_request_id,request_hash,run_id,input_id,accepted_generation)
            VALUES(%s,%s,'refuse_clarification',%s,%s,%s,%s,NULL)""",
            (actor.tenant_id, actor.actor_id, key, request_hash, run_id, body.input_id))
        return {"run_id":run_id,"status":"handoff"}
    if limits["logical_queued"] >= 10:
        raise ApiError(429, "QUEUE_FULL", "待执行队列已满，请稍后重试。")
    end = max(now, pending["wait_started_at"])
    elapsed = waiting_milliseconds(pending["wait_started_at"], end)
    generation = run["execution_generation"]+1
    await db.execute("""UPDATE pending_inputs SET status='answered',version=version+1,answers=%s,
        answer_hash=%s,answered_at=%s,answered_by=%s,resume_generation=%s,wait_ended_at=%s WHERE input_id=%s""",
        (Jsonb(answers), request_hash, now, actor.actor_id, generation, end, body.input_id))
    await db.execute("UPDATE runtime_limits SET logical_queued=logical_queued+1 WHERE app='supportflow'")
    updated = await (await db.execute("""UPDATE runs SET status='resume_queued',reason_code='INPUT_ACCEPTED',
        admission_state='queued',execution_generation=%s,version=version+1,last_event_seq=last_event_seq+1,
        confirmed_fields=confirmed_fields || %s,waiting_since=NULL,wait_expires_at=NULL,
        accumulated_wait_ms=accumulated_wait_ms+%s WHERE run_id=%s RETURNING *""",
        (generation, Jsonb(answers), elapsed, run_id))).fetchone()
    await db.execute("""INSERT INTO request_idempotency
        (tenant_id,actor_id,operation,client_request_id,request_hash,run_id,input_id,accepted_generation)
        VALUES(%s,%s,'submit_clarification',%s,%s,%s,%s,%s)""",
        (actor.tenant_id, actor.actor_id, key, request_hash, run_id, body.input_id, generation))
    await db.execute("INSERT INTO outbox(tenant_id,run_id,execution_generation,kind) VALUES(%s,%s,%s,'resume')",
                     (actor.tenant_id, run_id, generation))
    await event(db, updated, "run.status_changed", {"status": "resume_queued", "reason_code": "INPUT_ACCEPTED"})
    return {"run_id": run_id, "status": "resume_queued"}
