"""Staged MD/TXT uploads, index activation and authorized citation reads."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile

from .auth import ActorView, ReadActor, WriteActor, check_csrf, check_request, live_actor, unauthorized
from .objects import Identifier, not_found, principal
from .runtime import ApiError, Db
from apps.supportflow.domain.contracts import EvidenceRef


router = APIRouter()
UPLOAD_LIMIT = 2 * 1024 * 1024
VISIBLE_DOCUMENT = """d.tenant_id=%(tenant)s
    AND EXISTS(SELECT 1 FROM memberships m WHERE m.tenant_id=d.tenant_id AND m.user_id=%(actor)s)
    AND (d.visibility='tenant' OR EXISTS(SELECT 1 FROM document_acl a
        WHERE a.tenant_id=d.tenant_id AND a.document_id=d.document_id AND a.user_id=%(actor)s))"""


class CitationView(EvidenceRef):
    content: str
    document_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class DocumentView(BaseModel):
    document_id: str
    title: str
    visibility: Literal["tenant", "private"]
    active_version_id: str | None
    created_at: datetime


class DocumentPage(BaseModel):
    items: list[DocumentView]


class VersionView(BaseModel):
    version_id: str
    raw_sha256: str
    content_sha256: str
    parse_status: Literal["staged", "parsed", "failed"]
    corpus_origin: Literal["uploaded", "synthetic_business"]
    created_at: datetime
    index_state: Literal["staged", "building", "ready", "active", "retired", "failed", "interrupted"]
    index_generation: int | None
    failure_code: str | None


class VersionPage(BaseModel):
    items: list[VersionView]
    active_generation: int | None


class DocumentStaged(BaseModel):
    document_id: str
    version_id: str
    parse_status: Literal["staged", "parsed", "failed"]
    raw_sha256: str
    content_sha256: str
    created: bool


class DocumentActivationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_generation: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)] | None


class DocumentActivationResult(BaseModel):
    document_id: str
    version_id: str
    status: Literal["active", "building"]
    active_generation: int | None


async def upload_actor(request: Request, db: Db) -> ActorView:
    actor = await live_actor(request, db, writing=True)
    if actor is None:
        raise unauthorized()
    check_request(request, media_type="multipart/form-data")
    check_csrf(request, "session-csrf", request.cookies[request.app.state.cookies.session_name])
    return actor


UploadActor = Annotated[ActorView, Depends(upload_actor)]


async def lock_visible_document(db: Db, actor: ActorView, document_id: str):
    row = await (await db.execute(f"""SELECT d.document_id,d.visibility FROM documents d
        WHERE {VISIBLE_DOCUMENT} AND d.document_id=%(document)s FOR SHARE OF d""",
        {**principal(actor), "document": document_id})).fetchone()
    if row is None:
        raise not_found()
    if row["visibility"] == "private":
        acl = await (await db.execute("""SELECT user_id FROM document_acl
            WHERE tenant_id=%(tenant)s AND document_id=%(document)s AND user_id=%(actor)s FOR SHARE""",
            {**principal(actor), "document": document_id})).fetchone()
        if acl is None:
            raise not_found()


@router.get("/citations/{evidence_id}", response_model=CitationView)
async def get_citation(evidence_id: Identifier, request: Request, actor: ReadActor, db: Db,
                       generation: Annotated[int | None, Query(ge=1, le=2**31-1)] = None):
    if set(request.query_params) - {"generation"} or len(request.query_params.getlist("generation")) > 1:
        raise ApiError(422, "VALIDATION_ERROR", "引用参数无效。")
    # A published generation proves that this exact chunk once belonged to a
    # public index; staged uploads are not readable merely by guessing their ID.
    row = await (await db.execute(f"""SELECT c.*,g.generation,g.manifest,g.corpus_manifest_hash,
        v.content_sha256 AS document_sha256,v.normalized_text FROM chunks c
        JOIN documents d USING(tenant_id,document_id)
        JOIN document_versions v USING(tenant_id,document_id,version_id)
        JOIN index_vectors iv USING(tenant_id,evidence_id)
        JOIN index_generations g ON g.tenant_id=iv.tenant_id AND g.generation=iv.generation
        WHERE {VISIBLE_DOCUMENT} AND c.evidence_id=%(evidence)s
        AND g.state IN ('active','retired') AND (%(generation)s::integer IS NULL OR g.generation=%(generation)s)
        ORDER BY g.generation DESC LIMIT 1""",
        {**principal(actor), "evidence": evidence_id, "generation": generation})).fetchone()
    if row is None:
        raise not_found()
    await lock_visible_document(db, actor, row["document_id"])
    if hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != row["content_sha256"]:
        raise ApiError(503, "INVALID_REFERENCE", "引用内容校验失败。")
    normalized, start, end = row["normalized_text"], row["start_offset"], row["end_offset"]
    manifest_bytes = json.dumps(row["manifest"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    entry = {name: row[name] for name in ("evidence_id", "version_id", "section_id", "content_sha256")}
    if (not 0 <= start < end <= len(normalized) or normalized[start:end] != row["text"]
            or hashlib.sha256(normalized.encode("utf-8")).hexdigest() != row["document_sha256"]
            or normalized.count("\n", 0, start)+1 != row["start_line"]
            or normalized.count("\n", 0, end-1)+1 != row["end_line"]
            or hashlib.sha256(manifest_bytes).hexdigest() != row["corpus_manifest_hash"]
            or entry not in row["manifest"].get("chunks", [])):
        raise ApiError(503, "INVALID_REFERENCE", "引用位置或版本校验失败。")
    fields = ("evidence_id", "document_id", "version_id", "section_id", "generation", "title",
              "start_line", "end_line", "content_sha256", "document_sha256")
    return {**{name: row[name] for name in fields}, "source": "supportflow.knowledge", "content": row["text"]}


@router.get("/documents", response_model=DocumentPage)
async def list_documents(actor: ReadActor, db: Db):
    rows = await (await db.execute(f"""SELECT d.document_id,d.title,d.visibility,d.active_version_id,d.created_at
        FROM documents d WHERE {VISIBLE_DOCUMENT} ORDER BY d.created_at DESC,d.document_id LIMIT 50""",
        principal(actor))).fetchall()
    # Current document locks also cover visibility changes before projection.
    for row in rows:
        await lock_visible_document(db, actor, row["document_id"])
    return {"items": rows}


@router.get("/documents/{document_id}/versions", response_model=VersionPage)
async def list_versions(document_id: Identifier, actor: ReadActor, db: Db):
    await lock_visible_document(db, actor, document_id)
    rows = await (await db.execute(VERSION_SNAPSHOT + " ORDER BY v.created_at DESC,v.version_id LIMIT 50",
                                  version_parameters(actor, document_id))).fetchall()
    return {"items": [project_version(row) for row in rows],
            "active_generation": rows[0]["active_generation"] if rows else None}


VERSION_SNAPSHOT = """SELECT v.version_id,v.raw_sha256,v.content_sha256,v.parse_status,v.corpus_origin,v.created_at,
    d.active_version_id,s.active_generation,g.generation,g.state AS generation_state,g.failure_code,
    EXISTS(SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1
        AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND l.classid::bigint=%(lock_high)s AND l.objid::bigint=%(lock_low)s) AS builder_running
    FROM document_versions v JOIN documents d USING(tenant_id,document_id)
    LEFT JOIN knowledge_state s USING(tenant_id)
    LEFT JOIN LATERAL (
        SELECT i.generation,i.state,i.failure_code FROM index_generations i
        WHERE i.tenant_id=v.tenant_id AND i.manifest @> jsonb_build_object('documents',jsonb_build_array(
            jsonb_build_object('document_id',v.document_id,'version_id',v.version_id)))
        ORDER BY i.generation DESC LIMIT 1
    ) g ON true
    WHERE v.tenant_id=%(tenant)s AND v.document_id=%(document)s"""


BUILD_FAILURES = {
    "DUPLICATE_SECTION", "EMPTY_INDEX", "INVALID_TOKENIZATION", "INVALID_EMBEDDING",
    "EMBEDDING_UNAVAILABLE", "INDEX_BUILD_FAILED", "INDEX_NOT_READY", "GENERATION_CONFLICT",
}


def version_parameters(actor, document_id):
    from apps.supportflow.retrieval.ingest import ingest_lock

    key = ingest_lock(actor.tenant_id)
    return {**principal(actor), "document": document_id,
            "lock_high": (key >> 32) & 0xffffffff, "lock_low": key & 0xffffffff}


def project_version(row):
    state, generation, failure = row["generation_state"], row["generation"], None
    if row["version_id"] == row["active_version_id"] and row["active_generation"] is not None:
        state, generation = "active", row["active_generation"]
    elif row["parse_status"] == "failed":
        state, failure = "failed", "DOCUMENT_PARSE_FAILED"
    elif state == "failed":
        failure = row["failure_code"] if row["failure_code"] in BUILD_FAILURES else "INDEX_BUILD_FAILED"
    elif state in {"building", "ready"} and not row["builder_running"]:
        state, failure = "interrupted", "INDEX_BUILD_INTERRUPTED"
    return {**row, "index_state": state or "staged", "index_generation": generation, "failure_code": failure}


def document_connection(application_name):
    database = os.environ.get("PGDATABASE", "sf_app")
    if database != "sf_app":
        import re
        if not re.fullmatch(r"sf_eval_[a-z0-9_]+_app", database):
            raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "知识存储配置无效。")
    password = Path(os.environ["PGPASSWORD_FILE"]).read_text(encoding="utf-8").rstrip("\r\n")
    return psycopg.connect(host=os.environ.get("PGHOST", "postgres"), port=int(os.environ.get("PGPORT", "5432")),
        dbname=database, user="sf_app_runtime", password=password, connect_timeout=5,
        row_factory=dict_row, autocommit=True, application_name=application_name)


def activate_version(actor, *, document_id, version_id, expected_generation):
    from apps.supportflow.retrieval.contracts import KnowledgeError
    from apps.supportflow.retrieval.ingest import IndexBuilder, visible_version

    def read_snapshot(conn):
        with conn.transaction():
            visible_version(conn, actor.tenant_id, actor.actor_id, document_id, version_id)
            return project_version(conn.execute(VERSION_SNAPSHOT + " AND v.version_id=%(version)s",
                {**version_parameters(actor, document_id), "version": version_id}).fetchone())

    def result(status, active_generation):
        return {"document_id": document_id, "version_id": version_id,
                "status": status, "active_generation": active_generation}

    try:
        with document_connection("supportflow-document-activation") as conn:
            conn.execute("SET statement_timeout='30s'")
            conn.execute("SET lock_timeout='3s'")
            current = read_snapshot(conn)
            if current["index_state"] == "active":
                return result("active", current["active_generation"])
            if current["active_generation"] != expected_generation:
                raise KnowledgeError("GENERATION_CONFLICT")
            try:
                published = IndexBuilder(conn, actor_id=actor.actor_id).build(actor.tenant_id,
                    overrides={document_id: version_id}, expected_generation=expected_generation,
                    visible_target=(document_id, version_id))
                return result("active", published["generation"])
            except KnowledgeError as error:
                if error.code in {"INGEST_BUSY", "GENERATION_CONFLICT"}:
                    current = read_snapshot(conn)
                    if current["index_state"] == "active":
                        return result("active", current["active_generation"])
                    if (error.code == "INGEST_BUSY" and current["active_generation"] == expected_generation
                            and current["index_state"] in {"building", "ready"}):
                        return result("building", current["active_generation"])
                raise
    except KnowledgeError as error:
        if error.code == "NOT_FOUND":
            raise not_found() from None
        if error.code == "FORBIDDEN":
            raise ApiError(403, error.code, "只有当前管理员可以发布知识版本。") from None
        if error.code in {"GENERATION_CONFLICT", "INGEST_BUSY"}:
            raise ApiError(409, error.code, "索引状态已变更或正在构建，请刷新后重试。") from None
        if error.code in {"DUPLICATE_SECTION", "EMPTY_INDEX", "INVALID_TOKENIZATION"}:
            raise ApiError(422, error.code, "此知识版本解析失败。") from None
        code = error.code if error.code in BUILD_FAILURES else "INDEX_BUILD_FAILED"
        raise ApiError(503, code, "知识索引暂时无法发布。") from None
    except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled):
        raise ApiError(503, "INDEX_BUILD_BUSY", "索引存储暂时繁忙，请刷新后重试。") from None


@router.post("/documents/{document_id}/versions/{version_id}/activate", response_model=DocumentActivationResult,
             responses={202: {"model": DocumentActivationResult}})
async def activate_document(document_id: Identifier, version_id: Identifier, body: DocumentActivationInput,
                            request: Request, response: Response, actor: WriteActor):
    if actor.role != "admin":
        raise ApiError(403, "FORBIDDEN", "只有管理员可以发布知识版本。")
    result = await anyio.to_thread.run_sync(lambda: activate_version(actor, document_id=document_id,
        version_id=version_id, expected_generation=body.expected_generation),
        limiter=request.app.state.knowledge_upload_slots)
    if result["status"] == "building":
        response.status_code = 202
    return result


def stage_upload(actor, *, filename, content, title, document_id, visibility, key):
    """One authenticated actor, one short ledger/staging transaction; no embedding."""
    from apps.supportflow.retrieval.contracts import KnowledgeError
    from apps.supportflow.retrieval.ingest import stage_document

    def authorize_document(conn, selected):
        visible = conn.execute(f"""SELECT d.document_id,d.visibility FROM documents d
            WHERE {VISIBLE_DOCUMENT} AND d.document_id=%(document)s FOR SHARE OF d""",
            {**principal(actor), "document": selected}).fetchone()
        if visible is None:
            raise not_found()
        if visible["visibility"] == "private":
            grant = conn.execute("""SELECT user_id FROM document_acl
                WHERE tenant_id=%(tenant)s AND document_id=%(document)s AND user_id=%(actor)s FOR SHARE""",
                {**principal(actor), "document": selected}).fetchone()
            if grant is None:
                raise not_found()

    payload = {"schema_version": 1, "operation": "stage_document", "filename": filename,
               "raw_sha256": hashlib.sha256(content).hexdigest(), "title": title,
               "document_id": document_id, "visibility": visibility}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with document_connection("supportflow-document-staging") as conn:
        try:
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout='5s'")
                conn.execute("SET LOCAL lock_timeout='3s'")
                current = conn.execute("SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s FOR SHARE",
                    (actor.tenant_id, actor.actor_id)).fetchone()
                if current is None or current["role"] != "admin":
                    raise ApiError(403, "FORBIDDEN", "只有当前管理员可以导入知识。")
                conn.execute("INSERT INTO knowledge_state(tenant_id) VALUES(%s) ON CONFLICT DO NOTHING", (actor.tenant_id,))
                conn.execute("SELECT tenant_id FROM knowledge_state WHERE tenant_id=%s FOR UPDATE", (actor.tenant_id,)).fetchone()
                if document_id is not None:
                    authorize_document(conn, document_id)
                prior = conn.execute("""SELECT document_id FROM document_requests
                    WHERE tenant_id=%s AND actor_id=%s AND client_request_id=%s""",
                    (actor.tenant_id, actor.actor_id, key)).fetchone()
                if prior is not None:
                    authorize_document(conn, prior["document_id"])
                previous = conn.execute("""SELECT r.request_hash,v.* FROM document_requests r
                    JOIN document_versions v USING(tenant_id,document_id,version_id)
                    WHERE r.tenant_id=%s AND r.actor_id=%s AND r.client_request_id=%s""",
                    (actor.tenant_id, actor.actor_id, key)).fetchone()
                if previous is not None:
                    if previous["request_hash"] != digest:
                        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "相同请求键对应不同上传内容。")
                    return {name: previous[name] for name in ("document_id", "version_id", "parse_status", "raw_sha256", "content_sha256")} | {"created": False}
                staged = stage_document(conn, actor.tenant_id, actor.actor_id, filename=filename,
                    content=content, title=title, document_id=document_id, visibility=visibility)
                conn.execute("""INSERT INTO document_requests
                    (tenant_id,actor_id,client_request_id,request_hash,document_id,version_id) VALUES(%s,%s,%s,%s,%s,%s)""",
                    (actor.tenant_id, actor.actor_id, key, digest, staged["document_id"], staged["version_id"]))
                return staged
        except KnowledgeError as error:
            if error.code in ("RESULT_TOO_LARGE", "UPLOAD_TOO_LARGE"):
                raise ApiError(413, error.code, "文件超过2MiB上限。") from None
            if error.code == "FORBIDDEN":
                raise ApiError(403, error.code, "只有当前管理员可以导入知识。") from None
            if error.code == "NOT_FOUND":
                raise not_found() from None
            if error.code == "DOCUMENT_CONFLICT":
                raise ApiError(409, error.code, "上传版本与当前文档配置不一致。") from None
            if error.code == "DOCUMENT_STORAGE_CORRUPT":
                raise ApiError(503, error.code, "知识存储校验失败。") from None
            raise ApiError(422, error.code, "知识文件无法导入。") from None


@router.post("/documents", response_model=DocumentStaged, status_code=201, responses={200: {"model": DocumentStaged}}, openapi_extra={
    "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128}}],
    "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "additionalProperties": False, "required": ["file", "title"],
        "properties": {"file": {"type": "string", "format": "binary", "description": "UTF-8 MD/TXT, at most 2MiB"},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "document_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "visibility": {"type": "string", "enum": ["tenant", "private"], "default": "tenant"}}}}}}
})
async def upload_document(request: Request, response: Response, actor: UploadActor):
    if actor.role != "admin":
        raise ApiError(403, "FORBIDDEN", "只有管理员可以导入知识。")
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or not keys[0].strip() or not 1 <= len(keys[0]) <= 128 or any(not 32 <= ord(c) <= 126 for c in keys[0]):
        raise ApiError(422, "VALIDATION_ERROR", "上传需要有效的请求键。")
    async with request.form(max_files=1, max_fields=3, max_part_size=4096) as form:
        if set(form) - {"file", "title", "document_id", "visibility"} or any(len(form.getlist(k)) != 1 for k in form):
            raise ApiError(422, "VALIDATION_ERROR", "上传字段无效。")
        upload, title = form.get("file"), form.get("title")
        document_id, visibility = form.get("document_id"), form.get("visibility", "tenant")
        if (not isinstance(upload, UploadFile) or not isinstance(title, str) or not 1 <= len(title.strip()) <= 200
            or "\0" in title or visibility not in ("tenant", "private")
            or (document_id is not None and (not isinstance(document_id, str) or not 1 <= len(document_id) <= 128 or "\0" in document_id))):
            raise ApiError(422, "VALIDATION_ERROR", "上传字段无效。")
        content = await upload.read(UPLOAD_LIMIT + 1)
        if len(content) > UPLOAD_LIMIT:
            raise ApiError(413, "RESULT_TOO_LARGE", "文件超过2MiB上限。")
        # Parameters are all bounded before crossing into the synchronous worker.
        result = await anyio.to_thread.run_sync(lambda: stage_upload(actor, filename=upload.filename or "",
            content=content, title=title, document_id=document_id, visibility=visibility, key=keys[0]),
            limiter=request.app.state.knowledge_upload_slots)
    if not result["created"]:
        response.status_code = 200
    return result


async def mask_run_evidence(db: Db, actor: ActorView, row: dict):
    """Deny a history projection containing revoked knowledge metadata.

    Do not rewrite the historical resolution into a different business outcome.
    The result DTO remains strict: completed diagnoses have actual evidence.
    """
    result = row.get("result")
    if not isinstance(result, dict) or result.get("schema_version") not in {3, 4, 5, 6, 7, 8, 9}:
        return row
    references = result.get("evidence_refs", [])
    for ref in references:
        visible = await (await db.execute(f"""SELECT d.document_id FROM documents d
            WHERE {VISIBLE_DOCUMENT} AND d.document_id=%(document)s""",
            {**principal(actor), "document": ref["document_id"]})).fetchone()
        if visible is None:
            raise not_found()
        await lock_visible_document(db, actor, ref["document_id"])
    return row
