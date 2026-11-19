"""Run with uvicorn apps.supportflow.api.main:app behind the same-origin proxy."""
import logging
import os
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import anyio
import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from . import approvals, auth, clarifications, control, documents, evaluation, events, objects, runs
from .runtime import ApiError, CookiePolicy, ErrorView, database_pool


log = logging.getLogger("supportflow.api")


def utc_now() -> datetime:
    return datetime.now(UTC)


def error_response(request: Request, error: ApiError) -> JSONResponse:
    if error.status in (401, 403, 404, 422):
        log.warning("sf_denied request_id=%s status=%d code=%s",
                    request.state.request_id, error.status, error.code)
    return JSONResponse(status_code=error.status, content=ErrorView(
        code=error.code, message=error.message, request_id=request.state.request_id,
        retryable=error.status in (429, 503), details=error.details,
    ).model_dump())


def create_app(*, clock: Callable[[], datetime] = utc_now,
               verify_password: Callable[[str, str], bool] = auth.verify_password) -> FastAPI:
    @asynccontextmanager
    async def lifespan(api: FastAPI):
        api.state.cookies = CookiePolicy.from_env()
        api.state.clock = clock
        api.state.verify_password = verify_password
        api.state.hash_slots = anyio.CapacityLimiter(2)
        api.state.knowledge_upload_slots = anyio.CapacityLimiter(2)
        if os.environ.get("SF_GRAPH_VERSION") in {"readonly-diagnosis-rag-v1", "clarification-diagnosis-v1", "grant-proposal-v1", "grant-approval-v1"}:
            # Provisioning and indexing are explicit operations. Never accept a
            # dependent run by silently downloading a model or faking vectors.
            from apps.supportflow.retrieval.embedding import verified_directory
            from apps.supportflow.knowledge.reviewed import reviewed_sections
            await anyio.to_thread.run_sync(verified_directory)
            await anyio.to_thread.run_sync(reviewed_sections)
            if os.environ.get("SF_GRAPH_VERSION") in {"grant-proposal-v1", "grant-approval-v1"}:
                from apps.supportflow.domain.grant_policy import load_policy_bundle
                await anyio.to_thread.run_sync(load_policy_bundle)
        api.state.dummy_hash = await anyio.to_thread.run_sync(auth.HASHER.hash, secrets.token_urlsafe(32))
        pool = database_pool()
        try:
            await pool.open(wait=True, timeout=10)
            async with pool.connection() as conn:
                row = await (await conn.execute("SELECT secret FROM public.auth_key WHERE singleton")).fetchone()
                if row is None:
                    raise RuntimeError("Apply the SupportFlow authentication migration first")
                api.state.key = bytes(row["secret"])
            api.state.pool = pool
            yield
        finally:
            await pool.close()

    api = FastAPI(
        title="SupportFlow", version="0.1.0", lifespan=lifespan,
        responses={status: {"model": ErrorView} for status in (401, 403, 404, 409, 410, 413, 415, 422, 429, 503)},
    )

    @api.middleware("http")
    async def safe_headers(request: Request, call_next):
        request.state.request_id = uuid4().hex
        request.state.api_received_at = request.app.state.clock()
        try:
            response = await call_next(request)
        except Exception as exc:
            # Untrusted values and SQL bindings must never enter the log or the response.
            log.error("request_id=%s error_type=%s", request.state.request_id, type(exc).__name__)
            if isinstance(exc, psycopg.OperationalError):
                error = ApiError(503, "DEPENDENCY_UNAVAILABLE", "数据库暂时不可用。")
            else:
                error = ApiError(500, "INTERNAL_ERROR", "请求处理失败。")
            response = error_response(request, error)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.exception_handler(ApiError)
    async def product_error(request: Request, exc: ApiError):
        return error_response(request, exc)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default errors can echo password/token input and unknown fields.
        return error_response(request, ApiError(422, "VALIDATION_ERROR", "请求字段无效。"))

    @api.exception_handler(HTTPException)
    async def routing_error(request: Request, exc: HTTPException):
        return error_response(request, ApiError(exc.status_code, "HTTP_ERROR", "请求无法处理。"))

    api.include_router(auth.router, prefix="/api/v1")
    api.include_router(objects.router, prefix="/api/v1")
    api.include_router(runs.router, prefix="/api/v1")
    api.include_router(clarifications.router, prefix="/api/v1")
    api.include_router(approvals.router, prefix="/api/v1")
    api.include_router(control.router, prefix="/api/v1")
    api.include_router(events.router, prefix="/api/v1")
    api.include_router(documents.router, prefix="/api/v1")
    api.include_router(evaluation.router, prefix="/api/v1")
    return api


app = create_app()
