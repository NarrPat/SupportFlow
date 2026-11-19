"""Verify seed credentials and reconstruct authority from live database records."""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from argon2.low_level import Type
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runtime import ApiError, CookiePolicy, Db


router = APIRouter()
SESSION_TTL = timedelta(hours=8)
BOOTSTRAP_TTL = timedelta(minutes=15)
HASHER = PasswordHasher(type=Type.ID)
READ_CAPABILITIES = {"customers:read", "tickets:read", "threads:read", "runs:read"}
WRITE_CAPABILITIES = {"tickets:create", "threads:create", "runs:create"}


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024, repr=False)

    @field_validator("username")
    @classmethod
    def valid_db_text(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("Invalid username")
        return value

    @field_validator("password")
    @classmethod
    def bounded_password(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024:
            raise ValueError("Password exceeds UTF-8 byte limit")
        return value


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CsrfToken(BaseModel):
    csrf_token: str


class ActorView(BaseModel):
    actor_id: str
    username: str
    tenant_id: str
    role: Literal["support", "supervisor", "admin"]
    capabilities: list[str]

    def require(self, capability: str):
        if capability not in self.capabilities:
            raise ApiError(403, "FORBIDDEN", "当前角色无权执行此操作。")


def verify_password(encoded: str, password: str) -> bool:
    try:
        return HASHER.verify(encoded, password)
    except VerificationError:
        return False


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mac(key: bytes, purpose: str, value: str) -> str:
    return hmac.digest(key, f"supportflow:{purpose}:{value}".encode("utf-8"), "sha256").hex()


def csrf_failed():
    return ApiError(403, "CSRF_FAILED", "请求来源或 CSRF 令牌无效。")


def unauthorized():
    return ApiError(401, "UNAUTHENTICATED", "请重新登录。")


def check_request(request: Request, *, media_type: str = "application/json"):
    trusted = request.app.state.cookies.origin
    origins = request.headers.getlist("origin")
    if origins:
        if origins != [trusted]:
            raise csrf_failed()
    else:
        refs = request.headers.getlist("referer")
        if len(refs) != 1:
            raise csrf_failed()
        try:
            url = urlsplit(refs[0])
        except ValueError:
            raise csrf_failed() from None
        if url.username is not None or f"{url.scheme}://{url.netloc}" != trusted:
            raise csrf_failed()
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != media_type:
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "请求内容类型无效。")


def check_csrf(request: Request, purpose: str, cookie: str):
    headers = request.headers.getlist("x-csrf-token")
    expected = mac(request.app.state.key, purpose, cookie)
    if len(headers) != 1 or not hmac.compare_digest(expected.encode(), headers[0].encode()):
        raise csrf_failed()


def valid_bootstrap(cookie: str, key: bytes, now: datetime) -> bool:
    # Authenticate the issue timestamp before using this client-supplied lifetime.
    match = re.fullmatch(r"([0-9]{1,12}\.[A-Za-z0-9_-]{43})\.([0-9a-f]{64})", cookie)
    if not match or not hmac.compare_digest(mac(key, "issued", match[1]), match[2]):
        return False
    age = now.timestamp() - int(match[1].split(".")[0])
    return 0 <= age < BOOTSTRAP_TTL.total_seconds()


def cookie_args(policy: CookiePolicy):
    return dict(secure=policy.secure, httponly=True, samesite="lax", path="/")


async def live_actor(request: Request, db: Db, *, writing: bool, lock_membership: bool = True) -> ActorView | None:
    raw = request.cookies.get(request.app.state.cookies.session_name, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw):
        return None
    query = """
        SELECT u.user_id AS actor_id, u.username, m.tenant_id, m.role
        FROM public.sessions s
        JOIN public.memberships m USING (tenant_id, user_id)
        JOIN public.users u ON u.user_id = s.user_id
        WHERE s.token_hash = %s AND s.app = 'supportflow'
          AND s.revoked_at IS NULL AND s.expires_at > %s
    """
    # Serialize writes from this session with logout and keep membership current
    # throughout this request's authorized reads/writes.
    locks = (" FOR UPDATE OF s" + (" FOR SHARE OF m" if lock_membership else "")) if writing else " FOR SHARE OF s, m"
    row = await (await db.execute(query + locks, (digest(raw), request.app.state.clock()))).fetchone()
    if row is None:
        return None
    # SF-D08 allows all three roles to initiate work inside their current scope.
    capabilities = READ_CAPABILITIES | WRITE_CAPABILITIES
    return ActorView(**row, capabilities=sorted(capabilities))


async def read_actor(request: Request, db: Db) -> ActorView:
    actor = await live_actor(request, db, writing=False)
    if actor is None:
        raise unauthorized()
    return actor


async def write_actor(request: Request, db: Db) -> ActorView:
    actor = await live_actor(request, db, writing=True)
    if actor is None:
        raise unauthorized()
    check_request(request)
    check_csrf(request, "session-csrf", request.cookies[request.app.state.cookies.session_name])
    return actor


ReadActor = Annotated[ActorView, Depends(read_actor)]
WriteActor = Annotated[ActorView, Depends(write_actor)]


@router.get("/auth/csrf")
async def bootstrap(request: Request, response: Response, db: Db) -> CsrfToken:
    policy = request.app.state.cookies
    key = request.app.state.key
    if await live_actor(request, db, writing=False):
        return CsrfToken(csrf_token=mac(key, "session-csrf", request.cookies[policy.session_name]))

    now = request.app.state.clock()
    anon = request.cookies.get(policy.anonymous_name, "")
    if valid_bootstrap(anon, key, now):
        used = await (await db.execute(
            "SELECT 1 FROM public.sessions WHERE login_proof_hash = %s", (digest(anon),)
        )).fetchone()
        if used is None:
            return CsrfToken(csrf_token=mac(key, "anon-csrf", anon))
    nonce = f"{int(now.timestamp())}.{secrets.token_urlsafe(32)}"
    anon = f"{nonce}.{mac(key, 'issued', nonce)}"
    response.set_cookie(policy.anonymous_name, anon, max_age=int(BOOTSTRAP_TTL.total_seconds()), **cookie_args(policy))
    return CsrfToken(csrf_token=mac(key, "anon-csrf", anon))


@router.post("/auth/login")
async def login(body: Credentials, request: Request, response: Response, db: Db) -> CsrfToken:
    check_request(request)
    state = request.app.state
    policy = state.cookies
    old_session = await live_actor(request, db, writing=True)
    if old_session:
        proof = request.cookies[policy.session_name]
        check_csrf(request, "session-csrf", proof)
    else:
        proof = request.cookies.get(policy.anonymous_name, "")
        if not valid_bootstrap(proof, state.key, state.clock()):
            raise csrf_failed()
        check_csrf(request, "anon-csrf", proof)
    proof_hash = digest(proof)
    used = await (await db.execute(
        "SELECT 1 FROM public.sessions WHERE login_proof_hash = %s", (proof_hash,)
    )).fetchone()
    if used:
        raise csrf_failed()

    row = await (await db.execute(
        "SELECT user_id, password_hash FROM public.users WHERE username = %s", (body.username,)
    )).fetchone()
    encoded = row["password_hash"] if row and row["password_hash"].startswith("$argon2id$") else state.dummy_hash
    try:
        state.hash_slots.acquire_nowait()
    except anyio.WouldBlock:
        raise ApiError(429, "LOGIN_BUSY", "登录验证繁忙，请稍后重试。") from None
    try:
        matched = await anyio.to_thread.run_sync(state.verify_password, encoded, body.password)
    finally:
        state.hash_slots.release()
    denied = ApiError(401, "INVALID_CREDENTIALS", "用户名或密码错误。")
    if not matched or row is None or encoded == state.dummy_hash:
        raise denied

    # Scope comes only from the DB. P0 accounts belong to one tenant; ambiguous
    # memberships fail closed. Recheck the hash after the CPU-bound verification.
    scopes = await (await db.execute("""
        SELECT m.tenant_id FROM public.memberships m JOIN public.users u USING (user_id)
        WHERE m.user_id = %s AND u.password_hash = %s
        ORDER BY m.tenant_id LIMIT 2 FOR SHARE OF m, u
    """, (row["user_id"], encoded))).fetchall()
    if len(scopes) != 1:
        raise denied
    now = state.clock()
    if not old_session and not valid_bootstrap(proof, state.key, now):
        raise csrf_failed()
    raw = secrets.token_urlsafe(32)
    issued = await db.execute("""
        INSERT INTO public.sessions
            (token_hash, login_proof_hash, app, user_id, tenant_id, created_at, expires_at)
        VALUES (%s, %s, 'supportflow', %s, %s, %s, %s)
        ON CONFLICT (login_proof_hash) DO NOTHING
    """, (digest(raw), proof_hash, row["user_id"], scopes[0]["tenant_id"], now, now + SESSION_TTL))
    if issued.rowcount != 1:
        raise csrf_failed()
    if old_session:
        revoked = await db.execute("""
            UPDATE public.sessions SET revoked_at = %s WHERE token_hash = %s AND expires_at > %s
        """, (now, proof_hash, now))
        if revoked.rowcount != 1:
            raise unauthorized()
    response.delete_cookie(policy.anonymous_name, **cookie_args(policy))
    response.set_cookie(policy.session_name, raw, max_age=int(SESSION_TTL.total_seconds()), **cookie_args(policy))
    return CsrfToken(csrf_token=mac(state.key, "session-csrf", raw))


@router.get("/me")
async def who_am_i(actor: ReadActor) -> ActorView:
    return actor


@router.post("/auth/logout", status_code=204)
async def logout(body: EmptyBody, actor: WriteActor, request: Request, db: Db) -> Response:
    policy = request.app.state.cookies
    await db.execute("UPDATE public.sessions SET revoked_at = %s WHERE token_hash = %s",
                     (request.app.state.clock(), digest(request.cookies[policy.session_name])))
    response = Response(status_code=204)
    response.delete_cookie(policy.session_name, **cookie_args(policy))
    response.delete_cookie(policy.anonymous_name, **cookie_args(policy))
    return response
