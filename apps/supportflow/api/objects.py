"""HTTP resources for authorized customers, tickets, and actor-private threads."""
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .auth import ActorView, ApiError, Db, ReadActor, WriteActor


router = APIRouter()
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[^\x00]+$")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00]+$")]


class CreateTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: Title
    customer_id: Identifier | None = None


class CreateThread(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket_id: Identifier
    customer_id: Identifier | None = None


class Customer(BaseModel):
    customer_id: str
    customer_code: str
    display_name: str


class Ticket(BaseModel):
    ticket_id: str
    title: str
    status: str
    customer_id: str | None
    assignee_id: str
    version: int


class TicketDetail(Ticket):
    latest_thread_id: str | None


class Thread(BaseModel):
    thread_id: str
    ticket_id: str
    customer_id: str | None


class Page[T](BaseModel):
    items: list[T]
    limit: int
    offset: int
    has_more: bool


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=2_147_483_647)

    @field_validator("limit", "offset", mode="before")
    @classmethod
    def decimal_query_parameter(cls, value):
        if isinstance(value, str) and not (value.isascii() and value.isdecimal()):
            raise ValueError("Expected a nonnegative integer")
        return value


class CustomerQuery(PageQuery):
    customer_code: Identifier | None = None


class TicketQuery(PageQuery):
    customer_id: Identifier | None = None


class ThreadQuery(PageQuery):
    ticket_id: Identifier | None = None


# Every ticket query uses this predicate, including reads through a thread.
# An unbound ticket is private. Bound tickets follow current customer grants
# even when the assignee is the person who originally created the ticket.
VISIBLE_TICKET = """
    t.tenant_id = %(tenant)s AND (
        (t.customer_id IS NULL AND t.assignee_id = %(actor)s)
        OR EXISTS (
            SELECT 1 FROM public.customer_scopes s
            WHERE s.tenant_id = t.tenant_id AND s.customer_id = t.customer_id
              AND s.user_id = %(actor)s
        )
    )
"""
TICKET_COLUMNS = "t.ticket_id, t.title, t.status, t.customer_id, t.assignee_id, t.version"


def principal(actor: ActorView) -> dict:
    return {"tenant": actor.tenant_id, "actor": actor.actor_id}


def not_found() -> ApiError:
    return ApiError(404, "object_not_found", "资源不存在或不可见")


async def read_page(db: Db, statement: str, params: dict, query: PageQuery) -> dict:
    cursor = await db.execute(
        statement, {**params, "fetch_limit": query.limit + 1, "offset": query.offset},
    )
    rows = await cursor.fetchall()
    return {
        "items": rows[:query.limit], "limit": query.limit, "offset": query.offset,
        "has_more": len(rows) > query.limit,
    }


async def lock_customer_scope(db: Db, actor: ActorView, customer_id: str) -> None:
    # Serialize an in-flight write with deletion of its authorizing grant.
    cursor = await db.execute("""
        SELECT 1 FROM public.customer_scopes
        WHERE tenant_id = %(tenant)s AND user_id = %(actor)s AND customer_id = %(customer)s
        FOR SHARE
    """, {**principal(actor), "customer": customer_id})
    if await cursor.fetchone() is None:
        raise not_found()


@router.get("/customers", response_model=Page[Customer])
async def list_customers(actor: ReadActor, db: Db, query: Annotated[CustomerQuery, Query()]):
    actor.require("customers:read")
    return await read_page(db, """
        SELECT c.customer_id, c.customer_code, c.display_name
        FROM public.customers c JOIN public.customer_scopes s USING (tenant_id, customer_id)
        WHERE c.tenant_id = %(tenant)s AND s.user_id = %(actor)s
          AND (%(code)s::text IS NULL OR c.customer_code = %(code)s)
        ORDER BY c.customer_code, c.customer_id LIMIT %(fetch_limit)s OFFSET %(offset)s
    """, {**principal(actor), "code": query.customer_code}, query)


@router.get("/customers/{customer_id}", response_model=Customer)
async def get_customer(customer_id: Identifier, actor: ReadActor, db: Db):
    actor.require("customers:read")
    cursor = await db.execute("""
        SELECT c.customer_id, c.customer_code, c.display_name
        FROM public.customers c JOIN public.customer_scopes s USING (tenant_id, customer_id)
        WHERE c.tenant_id = %(tenant)s AND s.user_id = %(actor)s AND c.customer_id = %(customer)s
    """, {**principal(actor), "customer": customer_id})
    row = await cursor.fetchone()
    if row is None:
        raise not_found()
    return row


@router.get("/tickets", response_model=Page[Ticket])
async def list_tickets(actor: ReadActor, db: Db, query: Annotated[TicketQuery, Query()]):
    actor.require("tickets:read")
    return await read_page(db, f"""
        SELECT {TICKET_COLUMNS} FROM public.tickets t
        WHERE {VISIBLE_TICKET}
          AND (%(customer)s::text IS NULL OR t.customer_id = %(customer)s)
        ORDER BY t.created_at DESC, t.ticket_id DESC LIMIT %(fetch_limit)s OFFSET %(offset)s
    """, {**principal(actor), "customer": query.customer_id}, query)


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def get_ticket(ticket_id: Identifier, actor: ReadActor, db: Db):
    actor.require("tickets:read")
    cursor = await db.execute(f"""
        SELECT {TICKET_COLUMNS}, (
            SELECT h.thread_id FROM public.threads h
            WHERE h.tenant_id = t.tenant_id AND h.ticket_id = t.ticket_id AND h.actor_id = %(actor)s
            ORDER BY h.created_at DESC, h.thread_id DESC LIMIT 1
        ) AS latest_thread_id
        FROM public.tickets t WHERE {VISIBLE_TICKET} AND t.ticket_id = %(ticket)s
    """, {**principal(actor), "ticket": ticket_id})
    row = await cursor.fetchone()
    if row is None:
        raise not_found()
    return row


@router.post("/tickets", status_code=201, response_model=Ticket)
async def create_ticket(body: CreateTicket, actor: WriteActor, db: Db):
    actor.require("tickets:create")
    if body.customer_id is not None:
        await lock_customer_scope(db, actor, body.customer_id)
    cursor = await db.execute(f"""
        INSERT INTO public.tickets AS t (tenant_id, ticket_id, title, customer_id, assignee_id)
        VALUES (%(tenant)s, %(ticket)s, %(title)s, %(customer)s, %(actor)s)
        RETURNING {TICKET_COLUMNS}
    """, {**principal(actor), "ticket": str(uuid4()), "title": body.title, "customer": body.customer_id})
    return await cursor.fetchone()


@router.get("/threads", response_model=Page[Thread])
async def list_threads(actor: ReadActor, db: Db, query: Annotated[ThreadQuery, Query()]):
    actor.require("threads:read")
    return await read_page(db, f"""
        SELECT h.thread_id, h.ticket_id, t.customer_id FROM public.threads h
        JOIN public.tickets t USING (tenant_id, ticket_id)
        WHERE {VISIBLE_TICKET} AND h.actor_id = %(actor)s
          AND (%(ticket)s::text IS NULL OR h.ticket_id = %(ticket)s)
        ORDER BY h.created_at DESC, h.thread_id DESC LIMIT %(fetch_limit)s OFFSET %(offset)s
    """, {**principal(actor), "ticket": query.ticket_id}, query)


@router.get("/threads/{thread_id}", response_model=Thread)
async def get_thread(thread_id: Identifier, actor: ReadActor, db: Db):
    actor.require("threads:read")
    cursor = await db.execute(f"""
        SELECT h.thread_id, h.ticket_id, t.customer_id FROM public.threads h
        JOIN public.tickets t USING (tenant_id, ticket_id)
        WHERE {VISIBLE_TICKET} AND h.actor_id = %(actor)s AND h.thread_id = %(thread)s
    """, {**principal(actor), "thread": thread_id})
    row = await cursor.fetchone()
    if row is None:
        raise not_found()
    return row


@router.post("/threads", status_code=201, response_model=Thread)
async def create_thread(body: CreateThread, actor: WriteActor, db: Db):
    actor.require("threads:create")
    params = {**principal(actor), "ticket": body.ticket_id}
    # Competing first confirmations must observe the committed winning binding.
    cursor = await db.execute(f"""
        SELECT t.customer_id FROM public.tickets t
        WHERE {VISIBLE_TICKET} AND t.ticket_id = %(ticket)s FOR UPDATE OF t
    """, params)
    ticket = await cursor.fetchone()
    if ticket is None:
        raise not_found()
    bound = ticket["customer_id"]
    if bound is not None:
        await lock_customer_scope(db, actor, bound)
    if body.customer_id is not None and body.customer_id != bound:
        await lock_customer_scope(db, actor, body.customer_id)
        if bound is not None:
            raise ApiError(409, "customer_binding_conflict", "工单已绑定其他客户，请另建工单")
        bound = body.customer_id
        await db.execute("""
            UPDATE public.tickets SET customer_id = %(customer)s, version = version + 1
            WHERE tenant_id = %(tenant)s AND ticket_id = %(ticket)s
        """, {**params, "customer": bound})
    thread_id = str(uuid4())
    await db.execute("""
        INSERT INTO public.threads (tenant_id, thread_id, ticket_id, actor_id)
        VALUES (%(tenant)s, %(thread)s, %(ticket)s, %(actor)s)
    """, {**params, "thread": thread_id})
    return {"thread_id": thread_id, "ticket_id": body.ticket_id, "customer_id": bound}
