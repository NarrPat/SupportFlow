"""Public, replayable run events; every stream read owns only a short DB lease.

PostgreSQL rows are authoritative. Polling also covers events committed between
reads, without requiring a notification channel to be available or race-free.
"""
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal, get_args
from urllib.parse import quote

from apps.supportflow.jobs.event_hints import Listener

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .auth import live_actor, unauthorized
from .objects import Identifier, VISIBLE_TICKET, lock_customer_scope, not_found, principal
from .runs import RunStatus
from .runtime import ApiError


router = APIRouter()
log = logging.getLogger("supportflow.events")
MAX_CURSOR = 9_223_372_036_854_775_807
PAGE_LIMIT = 100
POLL_SECONDS = 1
HEARTBEAT_SECONDS = 15
STATUS_EVENTS = frozenset(("run.accepted", "run.started", "run.resumed", "run.recovering", "run.status_changed"))
PUBLIC_TYPES = STATUS_EVENTS | {"stage.changed", "tool.started", "tool.finished", "message.delta", "message.final", "clarification.required", "approval.required", "approval.decided", "action.completed"}
# Validation stays inside the authorized transaction. Document the same inputs
# explicitly, rather than having FastAPI reject them before object permission.
CURSOR_PARAMETERS = [
    {"name": "after", "in": "query", "required": False,
     "schema": {"type": "integer", "minimum": 0, "maximum": MAX_CURSOR},
     "description": "Exclusive event sequence cursor; defaults to zero if both cursor inputs are absent."},
    {"name": "Last-Event-ID", "in": "header", "required": False,
     "schema": {"type": "string", "pattern": "^[0-9]+$"},
     "description": "Must equal after numerically if both are supplied. Duplicate values are rejected."},
]


class PublicEvent(BaseModel):
    schema_version: int = Field(ge=1)
    app: Literal["supportflow"] = "supportflow"
    run_id: str
    seq: int = Field(ge=1)
    type: str
    status_version: int = Field(ge=1)
    spec_version: None = None
    occurred_at: datetime
    payload: dict


class ApprovalRequired(BaseModel):
    """Only stable review references; never proposal arguments or graph state."""
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    proposal_id: Identifier
    approval_id: Identifier
    action_id: Identifier
    expires_at: AwareDatetime


class ApprovalDecided(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    approval_id: Identifier
    proposal_id: Identifier
    action_id: Identifier
    decision: Literal['approve','reject']
    version: int = Field(ge=1,le=2**31-1)


class ActionCompleted(BaseModel):
    """Effect references only. Internal commands and credentials stay private."""
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    action_id: Identifier
    effect_status: Literal['committed']
    receipt_action_id: Identifier


class EventPage(BaseModel):
    items: list[PublicEvent]
    next_after: int
    has_more: bool
    min_seq: int
    last_seq: int
    snapshot_url: str


def invalid_query() -> ApiError:
    return ApiError(422, "VALIDATION_ERROR", "事件游标或分页参数无效。")


def decimal_parameter(raw: str, *, maximum: int = MAX_CURSOR) -> int:
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise invalid_query()
    # Bound conversion itself, including inputs exceeding Python's digit limit.
    significant = raw.lstrip("0") or "0"
    if len(significant) > len(str(maximum)):
        raise invalid_query()
    value = int(significant)
    if value > maximum:
        raise invalid_query()
    return value


def resolve_cursor(after: list[str], last_event_id: list[str]) -> int:
    if len(after) > 1 or len(last_event_id) > 1:
        raise invalid_query()
    query = decimal_parameter(after[0]) if after else None
    header = decimal_parameter(last_event_id[0]) if last_event_id else None
    if query is not None and header is not None and query != header:
        raise ApiError(409, "CURSOR_CONFLICT", "查询游标与 Last-Event-ID 不一致。")
    return query if query is not None else header if header is not None else 0


def snapshot_url(run_id: str) -> str:
    return f"/api/v1/runs/{quote(run_id, safe='')}"


def validate_bounds(after: int, *, min_seq: int, last_seq: int, run_id: str) -> None:
    if after > last_seq:
        raise ApiError(409, "CURSOR_AHEAD", "事件游标超出当前运行记录。")
    if after < min_seq - 1:
        raise ApiError(410, "CURSOR_EXPIRED", "事件记录已过期，请重新读取运行快照。", {
            "reset_required": True, "snapshot_url": snapshot_url(run_id),
        })


def check_source(request: Request) -> None:
    origins = request.headers.getlist("origin")
    sites = request.headers.getlist("sec-fetch-site")
    if ((origins and origins != [request.app.state.cookies.origin])
            or (sites and sites not in (["same-origin"], ["none"]))):
        raise ApiError(403, "FORBIDDEN", "事件读取需要同源请求。")


def request_parameters(request: Request, *, history: bool) -> tuple[int, int]:
    allowed = {"after", "limit"} if history else {"after"}
    if any(key not in allowed for key in request.query_params):
        raise invalid_query()
    limits = request.query_params.getlist("limit")
    if len(limits) > 1:
        raise invalid_query()
    limit = decimal_parameter(limits[0], maximum=PAGE_LIMIT) if limits else PAGE_LIMIT
    if limit < 1:
        raise invalid_query()
    return resolve_cursor(request.query_params.getlist("after"), request.headers.getlist("last-event-id")), limit


def projection_error() -> ApiError:
    return ApiError(503, "EVENT_UNAVAILABLE", "事件记录暂时无法读取。")


def public_text(payload: dict, key: str, *, maximum: int, allow_empty: bool = False) -> str:
    value = payload.get(key)
    if (not isinstance(value, str) or len(value) > maximum or "\0" in value
            or (not allow_empty and not value)):
        raise projection_error()
    return value


def project_event(row: dict, *, run_id: str) -> PublicEvent:
    """Construct fields explicitly: neither State nor unknown payloads pass through."""
    kind = row["type"]
    raw = row["payload"]
    payload = {}
    if row["schema_version"] != 1 or kind not in PUBLIC_TYPES:
        kind = "event.unsupported"
    elif not isinstance(raw, dict):
        raise projection_error()
    elif kind in STATUS_EVENTS:
        status = raw.get("status")
        if status not in get_args(RunStatus):
            raise projection_error()
        payload["status"] = status
        if raw.get("reason_code") is not None:
            reason = public_text(raw, "reason_code", maximum=80)
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", reason) is None:
                raise projection_error()
            payload["reason_code"] = reason
    elif kind == "stage.changed":
        payload["stage"] = public_text(raw, "stage", maximum=128)
    elif kind == "clarification.required":
        from .clarifications import PendingInput
        try:
            # Rebuild only the documented projection; never forward State.
            payload = PendingInput.model_validate_json(json.dumps(raw)).model_dump(mode="json")
        except ValueError:
            raise projection_error() from None
    elif kind == "approval.required":
        try:
            payload = ApprovalRequired.model_validate_json(json.dumps(raw)).model_dump(mode="json")
        except (ValueError, TypeError):
            raise projection_error() from None
    elif kind == "approval.decided":
        try:
            payload = ApprovalDecided.model_validate_json(json.dumps(raw)).model_dump(mode="json")
        except (ValueError, TypeError):
            raise projection_error() from None
    elif kind == "action.completed":
        try:
            payload = ActionCompleted.model_validate_json(json.dumps(raw)).model_dump(mode="json")
            if payload['action_id'] != payload['receipt_action_id']:
                raise ValueError('Receipt action differs')
        except (ValueError, TypeError):
            raise projection_error() from None
    elif kind in ("tool.started", "tool.finished"):
        payload["tool_id"] = public_text(raw, "tool_id", maximum=128)
        if "ok" in raw:
            if type(raw["ok"]) is not bool:
                raise projection_error()
            payload["ok"] = raw["ok"]
    else:
        payload["message_id"] = public_text(raw, "message_id", maximum=256)
        field = "delta" if kind == "message.delta" else "text"
        payload[field] = public_text(raw, field, maximum=32_768, allow_empty=True)
    return PublicEvent(
        schema_version=row["schema_version"], run_id=run_id, seq=row["seq"], type=kind,
        status_version=row["status_version"], occurred_at=row["occurred_at"], payload=payload,
    )


async def read_batch(request: Request, run_id: str, *, after: int | None = None,
                     history: bool = False, observe: bool = False) -> EventPage:
    # No generator yield or sleep is allowed in this lease. It commits before
    # the caller publishes a row, and logout/grant deletion can proceed at idle.
    async with request.app.state.pool.connection() as conn, conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '2s'")
        await conn.execute("SET LOCAL statement_timeout = '5s'")
        actor = await live_actor(request, conn, writing=False)
        if actor is None:
            raise unauthorized()
        actor.require("runs:read")
        row = await (await conn.execute(f"""
            SELECT r.last_event_seq, r.result, COALESCE(r.confirmed_fields->>'customer_id',t.customer_id) AS customer_id FROM public.runs r
            JOIN public.threads h USING (tenant_id, thread_id, actor_id)
            JOIN public.tickets t USING (tenant_id, ticket_id)
            WHERE {VISIBLE_TICKET} AND r.actor_id = %(actor)s AND r.run_id = %(run)s
            FOR SHARE OF r, h, t
        """, {**principal(actor), "run": run_id})).fetchone()
        if row is None:
            raise not_found()
        if row["customer_id"] is not None:
            await lock_customer_scope(conn, actor, row["customer_id"])
        # Final knowledge text is durable event data. Reauthorize its source on
        # each history/page read, including the next poll of an open SSE stream.
        from .documents import mask_run_evidence
        await mask_run_evidence(conn, actor, row)
        request.state.event_tenant_id = actor.tenant_id
        # Authenticate and authorize before inspecting any cursor/boundary.
        initial_after, limit = request_parameters(request, history=history)
        cursor = initial_after if after is None else after
        params = {"run": run_id, "tenant": actor.tenant_id, "after": cursor, "limit": limit + 1}
        minimum = await (await conn.execute("""
            SELECT min(seq) AS min_seq FROM public.run_events
            WHERE run_id = %(run)s AND tenant_id = %(tenant)s
        """, params)).fetchone()
        last_seq = row["last_event_seq"]
        min_seq = minimum["min_seq"] if minimum["min_seq"] is not None else last_seq + 1
        validate_bounds(cursor, min_seq=min_seq, last_seq=last_seq, run_id=run_id)
        rows = await (await conn.execute("""
            SELECT schema_version, seq, type, status_version, occurred_at, payload
            FROM public.run_events WHERE run_id = %(run)s AND tenant_id = %(tenant)s
              AND seq > %(after)s AND seq <= %(last)s ORDER BY seq LIMIT %(limit)s
        """, {**params, "last": last_seq})).fetchall()
        # A middle gap is corrupt history, not a reason to silently advance.
        if ([item["seq"] for item in rows] != list(range(cursor + 1, cursor + len(rows) + 1))
                or (len(rows) < limit + 1 and (rows[-1]["seq"] if rows else cursor) != last_seq)):
            raise projection_error()
        items = [project_event(item, run_id=run_id) for item in rows[:limit]]
        page = EventPage(items=items, next_after=items[-1].seq if items else cursor,
                         has_more=len(rows) > limit, min_seq=min_seq, last_seq=last_seq,
                         snapshot_url=snapshot_url(run_id))
    # Optional embedding observer runs only after transaction + pool release.
    # Production installs none; this is not an HTTP-controlled diagnostic path.
    observer = getattr(request.app.state, "event_poll_observer", None) if observe else None
    if observer is not None:
        await observer(run_id, cursor, last_seq)
    return page


def business_frame(event: PublicEvent) -> str:
    # Default message events also deliver unknown schema versions to onmessage.
    return f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"


def control_frame(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def event_stream(request: Request, run_id: str, initial: EventPage) -> AsyncIterator[str]:
    observer = getattr(request.app.state, 'event_hint_observer', None)
    async with Listener(request.state.event_tenant_id, run_id, observer=observer) as listener:
        page = initial
        first_poll = True
        heartbeat_at = anyio.current_time() + HEARTBEAT_SECONDS
        while True:
            if await request.is_disconnected():
                return
            for event in page.items:
                yield business_frame(event)
            if anyio.current_time() >= heartbeat_at:
                yield ": heartbeat\n\n"
                heartbeat_at = anyio.current_time() + HEARTBEAT_SECONDS
            await listener._observe('after_batch', {'channel': listener.name,
                'cursor': page.next_after, 'has_more': page.has_more, 'items': len(page.items)})
            # SUBSCRIBE cannot recover a hint published in the read/subscribe
            # gap. Always re-read PostgreSQL once before waiting for any hint.
            if not page.has_more and not first_poll:
                await listener.wait(POLL_SECONDS)
            first_poll = False
            if await request.is_disconnected():
                return
            try:
                page = await read_batch(request, run_id, after=page.next_after, observe=True)
            except ApiError as exc:
                if exc.status in (401, 403, 404):
                    yield control_frame("access.revoked", {})
                elif exc.status == 410:
                    yield control_frame("reset.required", exc.details)
                else:
                    yield control_frame("stream.error", {"code": exc.code, "retryable": exc.status == 503})
                return
            except Exception as exc:
                # Never log exception messages, SQL parameters, tokens or payloads.
                log.warning("event_stream_error request_id=%s error_type=%s",
                            request.state.request_id, type(exc).__name__)
                yield control_frame("stream.error", {"code": "DEPENDENCY_UNAVAILABLE", "retryable": True})
                return


@router.get("/runs/{run_id}/events/history", response_model=EventPage,
            openapi_extra={"parameters": [*CURSOR_PARAMETERS, {
                "name": "limit", "in": "query", "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": PAGE_LIMIT, "default": PAGE_LIMIT},
            }]})
async def history(run_id: Identifier, request: Request):
    check_source(request)
    return await read_batch(request, run_id, history=True)


@router.get("/runs/{run_id}/events", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
            openapi_extra={"parameters": CURSOR_PARAMETERS})
async def subscribe(run_id: Identifier, request: Request):
    check_source(request)
    # Fail authentication and cursor validation before HTTP headers are sent.
    initial = await read_batch(request, run_id, observe=True)
    return StreamingResponse(event_stream(request, run_id, initial), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})
