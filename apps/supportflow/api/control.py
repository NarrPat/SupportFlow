"""Idempotent cancellation control. No external write or reconciliation dispatch."""
import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import ActorView, Db, check_csrf, check_request, live_actor, unauthorized
from .clarifications import Key, authorized_run, event
from .objects import Identifier, principal
from .runtime import ApiError
from .runs import RunView, TERMINAL_STATUSES, enrich_run, resolve_request_key
from apps.supportflow.jobs.waiting import waiting_milliseconds

router = APIRouter()


async def cancel_actor(request: Request, db: Db):
    # Match the worker's limits -> run -> membership order, while retaining
    # the session lock that serializes this write with logout.
    actor = await live_actor(request, db, writing=True, lock_membership=False)
    if actor is None:
        raise unauthorized()
    check_request(request)
    check_csrf(request, 'session-csrf', request.cookies[request.app.state.cookies.session_name])
    return actor


CancelWriteActor = Annotated[ActorView, Depends(cancel_actor)]


class CancelRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    expected_version: int = Field(ge=1, le=2**63-1)
    client_request_id: Key | None = None


class CancelView(RunView):
    already_terminal: bool
    cancel_requested: bool


def cancel_hash(run_id, expected_version):
    text = json.dumps({"schema_version": 1, "operation": "cancel_run", "run_id": run_id,
                       "expected_version": expected_version},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@router.post("/runs/{run_id}/cancel", response_model=CancelView, response_model_exclude_unset=True)
async def cancel(run_id: Identifier, body: CancelRequest, request: Request, response: Response, actor: CancelWriteActor, db: Db):
    actor.require("runs:create")
    key = resolve_request_key(body, request)
    await db.execute("SET LOCAL lock_timeout='5s'")
    await db.execute("SET LOCAL statement_timeout='15s'")
    await authorized_run(db, actor, run_id, lock=False)
    await db.execute("SELECT app FROM runtime_limits WHERE app='supportflow' FOR UPDATE")
    run = await authorized_run(db, actor, run_id, lock=True)
    membership = await (await db.execute("SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s FOR SHARE",
        (actor.tenant_id, actor.actor_id))).fetchone()
    if membership is None:
        raise unauthorized()
    actor = actor.model_copy(update={'role': membership['role']})
    params = {**principal(actor), "key": key, "run": run_id, "hash": cancel_hash(run_id, body.expected_version)}
    previous = await (await db.execute("""SELECT request_hash FROM request_idempotency
        WHERE app='supportflow' AND tenant_id=%(tenant)s AND actor_id=%(actor)s
          AND operation='cancel_run' AND client_request_id=%(key)s""", params)).fetchone()
    if previous is not None and previous["request_hash"] != params["hash"]:
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "此请求键已用于其他请求。")
    already_terminal = run["status"] in TERMINAL_STATUSES
    # A successful key replays the current authorized snapshot, even after the
    # first mutation advanced its version. A failed CAS never consumes the key.
    if previous is None and not already_terminal and run["version"] != body.expected_version:
        raise ApiError(409, "VERSION_CONFLICT", "运行状态已更新，请刷新后重试。")
    if not already_terminal and not run["cancel_requested"]:
        from apps.supportflow.jobs.action_effects import read_cancel_effect
        effect = await read_cancel_effect(db, run)
        if run["status"] in {"running", "needs_reconciliation"}:
            run = await (await db.execute("""UPDATE runs SET cancel_requested=true,
                version=version+1,last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *""", (run_id,))).fetchone()
        elif effect["phase"] in {"potential", "committed"}:
            # Defensive recovery of an effect-bearing queued row: no graph
            # resume or cancellation may erase the remote outcome. Scanner is
            # the sole trigger for the existing bounded receipt-read path.
            await db.execute("""UPDATE runtime_limits SET logical_queued=logical_queued-%s,active_reserved=active_reserved-%s
                WHERE app='supportflow'""", (int(run["admission_state"] == "queued"), int(run["admission_state"] == "active")))
            await db.execute("UPDATE outbox SET status='cancelled' WHERE run_id=%s AND status='pending'", (run_id,))
            run = await (await db.execute("""UPDATE runs SET cancel_requested=true,
                status='needs_reconciliation',reason_code='UNKNOWN_COMMIT',admission_state='none',
                execution_generation=execution_generation+1,version=version+1,last_event_seq=last_event_seq+1
                WHERE run_id=%s RETURNING *""", (run_id,))).fetchone()
        else:
            now = (await (await db.execute("SELECT clock_timestamp() AS now")).fetchone())["now"]
            elapsed = 0
            pending = await (await db.execute("""SELECT * FROM pending_inputs
                WHERE run_id=%s AND status='pending' FOR UPDATE""", (run_id,))).fetchone()
            if pending is not None:
                start = pending["wait_started_at"]
                end = max(now, start) if start is not None else None
                elapsed += waiting_milliseconds(start, end) if end is not None else 0
                await db.execute("""UPDATE pending_inputs SET status='cancelled',version=version+1,wait_ended_at=%s
                    WHERE input_id=%s""", (end, pending["input_id"]))
            proposal_wait = await (await db.execute("SELECT * FROM proposal_waits WHERE run_id=%s AND status='pending' FOR UPDATE", (run_id,))).fetchone()
            if proposal_wait is not None:
                end = max(now, proposal_wait["wait_started_at"])
                elapsed += waiting_milliseconds(proposal_wait["wait_started_at"], end)
                await db.execute("UPDATE proposal_waits SET status='cancelled',wait_ended_at=%s WHERE proposal_id=%s",
                                 (end, proposal_wait["proposal_id"]))
            await db.execute("UPDATE approvals SET status='cancelled',version=version+1 WHERE run_id=%s AND status='pending'", (run_id,))
            await db.execute("UPDATE actions SET status='cancelled' WHERE run_id=%s AND status='proposed'", (run_id,))
            await db.execute("""UPDATE runtime_limits SET logical_queued=logical_queued-%s,active_reserved=active_reserved-%s
                WHERE app='supportflow'""", (int(run["admission_state"] == "queued"), int(run["admission_state"] == "active")))
            await db.execute("UPDATE outbox SET status='cancelled' WHERE run_id=%s AND status='pending'", (run_id,))
            run = await (await db.execute("""UPDATE runs SET status='cancelled',reason_code='CANCEL_REQUESTED',
                cancel_requested=true,admission_state='none',execution_generation=execution_generation+1,
                version=version+1,last_event_seq=last_event_seq+1,waiting_since=NULL,wait_expires_at=NULL,
                accumulated_wait_ms=accumulated_wait_ms+%s WHERE run_id=%s RETURNING *""", (elapsed, run_id))).fetchone()
        await event(db, run, "run.status_changed", {"status": run["status"], "reason_code": "CANCEL_REQUESTED"})
    if previous is None:
        await db.execute("""INSERT INTO request_idempotency(tenant_id,actor_id,operation,client_request_id,request_hash,run_id)
            VALUES(%(tenant)s,%(actor)s,'cancel_run',%(key)s,%(hash)s,%(run)s)""", params)
    response.status_code = 202 if run["status"] in {"running", "needs_reconciliation"} else 200
    row = await enrich_run(db, actor, run)
    return {**row, "cancel_requested": run["cancel_requested"], "already_terminal": already_terminal}
