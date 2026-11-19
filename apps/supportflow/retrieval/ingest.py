"""Staged upload and explicit, per-tenant serialized dual-index construction."""
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from psycopg.types.json import Jsonb

from .contracts import CHUNKER_VERSION, DIMENSION, KnowledgeError, compact, sha256
from .embedding import default_embedding, require_embedding_profile, validate_vectors
from .lexical import SnapshotCache, lexical_version, term_document, write_snapshot
from .parsing import chunk_document, normalize


def stage_document(conn, tenant_id, actor_id, *, filename, content, title, document_id=None,
                   visibility="tenant", corpus_origin="uploaded"):
    """Can participate in the caller's transaction; never commits that transaction."""
    if (not isinstance(title, str) or not title.strip() or not 1 <= len(title) <= 200 or "\0" in title
            or visibility not in {"tenant", "private"} or corpus_origin not in {"uploaded", "synthetic_business"}):
        raise KnowledgeError("VALIDATION")
    parsed = normalize(content, filename)
    with conn.transaction():
        actor = conn.execute("SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s FOR SHARE",
                             (tenant_id, actor_id)).fetchone()
        if actor is None or actor["role"] != "admin":
            raise KnowledgeError("FORBIDDEN")
        if document_id is None:
            document_id = str(uuid4())
            conn.execute("INSERT INTO documents(document_id,tenant_id,title,visibility) VALUES(%s,%s,%s,%s)",
                         (document_id, tenant_id, title, visibility))
            if visibility == "private":
                conn.execute("INSERT INTO document_acl(tenant_id,document_id,user_id) VALUES(%s,%s,%s)",
                             (tenant_id, document_id, actor_id))
        else:
            document = conn.execute("""SELECT d.* FROM documents d
                WHERE d.tenant_id=%s AND d.document_id=%s AND (d.visibility='tenant' OR EXISTS(
                    SELECT 1 FROM document_acl a WHERE a.tenant_id=d.tenant_id
                    AND a.document_id=d.document_id AND a.user_id=%s)) FOR UPDATE OF d""",
                                    (tenant_id, document_id, actor_id)).fetchone()
            if document is None:
                raise KnowledgeError("NOT_FOUND")
            if document["visibility"] == "private" and not conn.execute("""SELECT user_id FROM document_acl
                WHERE tenant_id=%s AND document_id=%s AND user_id=%s FOR SHARE""",
                (tenant_id, document_id, actor_id)).fetchone():
                raise KnowledgeError("NOT_FOUND")
            # Version upload cannot covertly broaden an existing document ACL.
            if document["visibility"] != visibility:
                raise KnowledgeError("DOCUMENT_CONFLICT")
        previous = conn.execute("SELECT * FROM document_versions WHERE tenant_id=%s AND document_id=%s AND raw_sha256=%s",
                                (tenant_id, document_id, parsed["raw_sha256"])).fetchone()
        if previous:
            return {**previous, "created": False}
        version_id = str(uuid5(NAMESPACE_URL, "supportflow:version:"+document_id+":"+parsed["raw_sha256"]))
        root = Path(os.environ.get("SF_KB_UPLOAD_DIR", "/data/knowledge/uploads")).resolve()
        relative = sha256(tenant_id)[:32] + "/" + document_id + "/" + version_id + "." + parsed["media_type"]
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise KnowledgeError("INVALID_DOCUMENT")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256(target.read_bytes()) != parsed["raw_sha256"]:
                raise KnowledgeError("DOCUMENT_STORAGE_CORRUPT")
        else:
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        row = conn.execute("""INSERT INTO document_versions(version_id,tenant_id,document_id,raw_sha256,
            content_sha256,normalized_text,original_path,media_type,corpus_origin)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (version_id, tenant_id, document_id, parsed["raw_sha256"], parsed["content_sha256"], parsed["text"],
             relative, parsed["media_type"], corpus_origin)).fetchone()
        return {**row, "created": True}


def ingest_lock(tenant_id):
    return int.from_bytes(bytes.fromhex(sha256("supportflow:knowledge:"+tenant_id))[:8], "big", signed=True)


def validate_overrides(overrides):
    if not isinstance(overrides, dict) or any(
        not isinstance(key, str) or not key or (value is not None and (not isinstance(value, str) or not value))
        for key, value in overrides.items()
    ):
        raise KnowledgeError("VALIDATION")
    return overrides


def generation_manifest(versions, chunks):
    return {"schema_version": 1,
            "documents": [{"document_id": row["document_id"], "version_id": row["version_id"],
                           "content_sha256": row["content_sha256"], "policy_version": row["policy_version"]}
                          for row in sorted(versions, key=lambda row: row["document_id"])],
            "chunks": [{key: row[key] for key in ("evidence_id", "version_id", "section_id", "content_sha256")}
                       for row in sorted(chunks, key=lambda row: row["evidence_id"])]}


def visible_version(conn, tenant_id, actor_id, document_id, version_id):
    """Hold current document and private ACL access through publication."""
    document = conn.execute("""SELECT d.visibility,d.active_version_id FROM documents d
        JOIN document_versions v USING(tenant_id,document_id)
        WHERE d.tenant_id=%s AND d.document_id=%s AND v.version_id=%s
        AND (d.visibility='tenant' OR EXISTS(SELECT 1 FROM document_acl a
            WHERE a.tenant_id=d.tenant_id AND a.document_id=d.document_id AND a.user_id=%s))
        FOR SHARE OF d,v""", (tenant_id, document_id, version_id, actor_id)).fetchone()
    if document is None:
        raise KnowledgeError("NOT_FOUND")
    if document["visibility"] == "private" and not conn.execute("""SELECT user_id FROM document_acl
        WHERE tenant_id=%s AND document_id=%s AND user_id=%s FOR SHARE""",
        (tenant_id, document_id, actor_id)).fetchone():
        raise KnowledgeError("NOT_FOUND")
    return document


def publish_generation(conn, tenant_id, generation, *, expected_generation, actor_id=None, visible_target=None):
    """Only this CAS transaction changes current versions and the active index."""
    with conn.transaction():
        if actor_id is not None:
            actor = conn.execute("SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s FOR SHARE",
                                 (tenant_id, actor_id)).fetchone()
            if not actor or actor["role"] != "admin":
                raise KnowledgeError("FORBIDDEN")
        state = conn.execute("SELECT * FROM knowledge_state WHERE tenant_id=%s FOR UPDATE", (tenant_id,)).fetchone()
        candidate = conn.execute("SELECT * FROM index_generations WHERE tenant_id=%s AND generation=%s FOR UPDATE",
                                 (tenant_id, generation)).fetchone()
        if not state or not candidate:
            raise KnowledgeError("INDEX_NOT_READY")
        if visible_target is not None:
            visible_version(conn, tenant_id, actor_id, *visible_target)
        if state["active_generation"] == generation and candidate["state"] == "active":
            return candidate
        if state["active_generation"] != expected_generation or candidate["expected_generation"] != expected_generation:
            raise KnowledgeError("GENERATION_CONFLICT")
        if not candidate["vector_ready"] or not candidate["lexical_ready"] or candidate["state"] != "ready":
            raise KnowledgeError("INDEX_NOT_READY")
        snapshot = SnapshotCache().load(candidate)
        manifest = candidate["manifest"]
        if sha256(compact(manifest)) != candidate["corpus_manifest_hash"]:
            raise KnowledgeError("INDEX_NOT_READY")
        expected_ids = sorted(item["evidence_id"] for item in manifest["chunks"])
        vector_rows = conn.execute("""SELECT c.evidence_id,c.version_id,c.section_id,c.content_sha256
            FROM index_vectors v JOIN chunks c USING(tenant_id,evidence_id)
            WHERE v.tenant_id=%s AND v.generation=%s ORDER BY c.evidence_id""", (tenant_id, generation)).fetchall()
        if (vector_rows != sorted(manifest["chunks"], key=lambda row: row["evidence_id"])
                or sorted(item["evidence_id"] for item in snapshot["documents"]) != expected_ids):
            raise KnowledgeError("INDEX_NOT_READY")
        conn.execute("UPDATE index_generations SET state='retired' WHERE tenant_id=%s AND state='active'", (tenant_id,))
        # The manifest defines the complete active corpus. Explicit null
        # overrides omit a document from it, while immutable versions/vectors
        # and current ACL remain available for authorized historical citations.
        conn.execute("""UPDATE documents SET active_version_id=NULL WHERE tenant_id=%s
            AND active_version_id IS NOT NULL AND NOT(document_id=ANY(%s::text[]))""",
            (tenant_id, [document["document_id"] for document in manifest["documents"]]))
        for document in manifest["documents"]:
            conn.execute("UPDATE documents SET active_version_id=%s WHERE tenant_id=%s AND document_id=%s",
                         (document["version_id"], tenant_id, document["document_id"]))
        conn.execute("UPDATE knowledge_state SET active_generation=%s WHERE tenant_id=%s", (generation, tenant_id))
        return conn.execute("""UPDATE index_generations SET state='active',published_at=clock_timestamp()
            WHERE tenant_id=%s AND generation=%s RETURNING *""", (tenant_id, generation)).fetchone()


class IndexBuilder:
    def __init__(self, conn, *, embedding=None, snapshot_writer=None, actor_id=None):
        self.conn, self.embedding = conn, embedding
        self.snapshot_writer = snapshot_writer or write_snapshot
        self.actor_id = actor_id

    def build(self, tenant_id, *, overrides, expected_generation, resume_generation=None, visible_target=None):
        validate_overrides(overrides)
        if visible_target is not None and (self.actor_id is None or overrides != dict([visible_target])):
            raise KnowledgeError("VALIDATION")
        if not self.conn.autocommit:
            raise KnowledgeError("INGEST_REQUIRES_AUTOCOMMIT")
        key = ingest_lock(tenant_id)
        acquired = self.conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()["acquired"]
        if not acquired:
            raise KnowledgeError("INGEST_BUSY")
        generation = None
        parse_failed_version = None
        try:
            embedding = self.embedding or default_embedding()
            require_embedding_profile(embedding)
            lex_version = lexical_version()
            with self.conn.transaction():
                if self.actor_id is not None:
                    actor = self.conn.execute("SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s FOR SHARE",
                                              (tenant_id, self.actor_id)).fetchone()
                    if not actor or actor["role"] != "admin":
                        raise KnowledgeError("FORBIDDEN")
                self.conn.execute("INSERT INTO knowledge_state(tenant_id) VALUES(%s) ON CONFLICT DO NOTHING", (tenant_id,))
                state = self.conn.execute("SELECT * FROM knowledge_state WHERE tenant_id=%s FOR UPDATE", (tenant_id,)).fetchone()
                if state["active_generation"] != expected_generation:
                    raise KnowledgeError("GENERATION_CONFLICT")
                if visible_target is not None:
                    visible_version(self.conn, tenant_id, self.actor_id, *visible_target)
                documents = self.conn.execute("SELECT * FROM documents WHERE tenant_id=%s ORDER BY document_id FOR SHARE", (tenant_id,)).fetchall()
                if set(overrides) - {row["document_id"] for row in documents}:
                    raise KnowledgeError("NOT_FOUND")
                selected = {row["document_id"]: overrides.get(row["document_id"], row["active_version_id"])
                            for row in documents}
                versions, chunks = [], []
                for document_id, version_id in selected.items():
                    if version_id is None:
                        continue
                    version = self.conn.execute("""SELECT * FROM document_versions
                        WHERE tenant_id=%s AND document_id=%s AND version_id=%s FOR SHARE""",
                        (tenant_id, document_id, version_id)).fetchone()
                    if version is None:
                        raise KnowledgeError("NOT_FOUND")
                    versions.append(version)
                    try:
                        parsed = chunk_document(version["normalized_text"], version["media_type"],
                            document_id=document_id, version_id=version_id, tokenizer=embedding.tokenizer)
                    except KnowledgeError:
                        parse_failed_version = version_id
                        raise
                    for chunk in parsed:
                        columns = list(chunk)
                        self.conn.execute("INSERT INTO chunks(tenant_id,"+",".join(columns)+") VALUES("+
                            ",".join(["%s"]*(len(columns)+1))+") ON CONFLICT(evidence_id) DO NOTHING",
                            (tenant_id, *(chunk[column] for column in columns)))
                    chunks.extend(parsed)
                    self.conn.execute("UPDATE document_versions SET parse_status='parsed' WHERE version_id=%s", (version_id,))
                if not chunks:
                    raise KnowledgeError("EMPTY_INDEX")
                manifest = generation_manifest(versions, chunks)
                manifest_hash = sha256(compact(manifest))
                if resume_generation is None:
                    generation = state["next_generation"]
                    self.conn.execute("UPDATE knowledge_state SET next_generation=next_generation+1 WHERE tenant_id=%s", (tenant_id,))
                    self.conn.execute("""INSERT INTO index_generations(tenant_id,generation,expected_generation,manifest,
                        corpus_manifest_hash,embedding_model,embedding_revision,dimension,tokenizer_version,chunker_version,
                        lexical_version,vector_origin) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, generation, expected_generation, Jsonb(manifest), manifest_hash, embedding.model_id,
                         embedding.revision, DIMENSION, embedding.tokenizer.version, CHUNKER_VERSION, lex_version, embedding.vector_origin))
                else:
                    generation = resume_generation
                    existing = self.conn.execute("SELECT * FROM index_generations WHERE tenant_id=%s AND generation=%s FOR UPDATE",
                                                 (tenant_id, generation)).fetchone()
                    if (not existing or existing["corpus_manifest_hash"] != manifest_hash
                            or existing["expected_generation"] != expected_generation
                            or existing["embedding_revision"] != embedding.revision
                            or existing["vector_origin"] != embedding.vector_origin
                            or existing["tokenizer_version"] != embedding.tokenizer.version
                            or existing["lexical_version"] != lex_version):
                        generation = None  # A rejected resume must not alter the unrelated existing generation.
                        raise KnowledgeError("GENERATION_CONFLICT")
                    self.conn.execute("UPDATE index_generations SET state='building',failure_code=NULL WHERE tenant_id=%s AND generation=%s",
                                      (tenant_id, generation))
            already = {row["evidence_id"] for row in self.conn.execute(
                "SELECT evidence_id FROM index_vectors WHERE tenant_id=%s AND generation=%s", (tenant_id, generation)).fetchall()}
            pending = [row for row in chunks if row["evidence_id"] not in already]
            for offset in range(0, len(pending), 16):
                batch = pending[offset:offset+16]
                try:
                    vectors = validate_vectors(embedding.passages([row["text"] for row in batch]), len(batch))
                except KnowledgeError as error:
                    raise KnowledgeError(error.code, details={"generation": generation, "batch": offset//16,
                        "evidence_ids": [row["evidence_id"] for row in batch]}) from None
                with self.conn.transaction():
                    for row, vector in zip(batch, vectors, strict=True):
                        self.conn.execute("""INSERT INTO index_vectors(tenant_id,generation,evidence_id,embedding)
                            VALUES(%s,%s,%s,%s::vector) ON CONFLICT DO NOTHING""",
                            (tenant_id, generation, row["evidence_id"], compact(vector).decode("ascii")))
            with self.conn.transaction():
                count = self.conn.execute("SELECT count(*) AS n FROM index_vectors WHERE tenant_id=%s AND generation=%s",
                                          (tenant_id, generation)).fetchone()["n"]
                if count != len(chunks):
                    raise KnowledgeError("INDEX_NOT_READY")
                self.conn.execute("UPDATE index_generations SET vector_ready=true WHERE tenant_id=%s AND generation=%s",
                                  (tenant_id, generation))
            snapshot = {"schema_version": 1, "tenant_id": tenant_id, "generation": generation,
                        "corpus_manifest_hash": manifest_hash, "tokenizer_version": embedding.tokenizer.version,
                        "chunker_version": CHUNKER_VERSION, "lexical_version": lex_version,
                        "documents": [term_document(row["evidence_id"], row["text"])
                                      for row in sorted(chunks, key=lambda row: row["evidence_id"])]}
            relative, checksum = self.snapshot_writer(snapshot)
            with self.conn.transaction():
                self.conn.execute("""UPDATE index_generations SET lexical_ready=true,snapshot_path=%s,
                    snapshot_checksum=%s,state='ready' WHERE tenant_id=%s AND generation=%s""",
                    (relative, checksum, tenant_id, generation))
            return publish_generation(self.conn, tenant_id, generation, expected_generation=expected_generation,
                                      actor_id=self.actor_id, visible_target=visible_target)
        except KnowledgeError as error:
            if parse_failed_version is not None:
                with self.conn.transaction():
                    self.conn.execute("""UPDATE document_versions v SET parse_status='failed'
                        FROM documents d WHERE v.tenant_id=%s AND v.version_id=%s
                        AND d.tenant_id=v.tenant_id AND d.document_id=v.document_id
                        AND d.active_version_id IS DISTINCT FROM v.version_id""",
                        (tenant_id, parse_failed_version))
            if generation is not None:
                with self.conn.transaction():
                    self.conn.execute("""UPDATE index_generations SET state='failed',failure_code=%s
                        WHERE tenant_id=%s AND generation=%s AND state NOT IN ('active','retired')""",
                        (error.code, tenant_id, generation))
            raise
        except (OSError, ValueError) as error:
            if generation is not None:
                with self.conn.transaction():
                    self.conn.execute("""UPDATE index_generations SET state='failed',failure_code='INDEX_BUILD_FAILED'
                        WHERE tenant_id=%s AND generation=%s AND state NOT IN ('active','retired')""", (tenant_id, generation))
            raise KnowledgeError("INDEX_BUILD_FAILED") from None
        finally:
            if not self.conn.closed and not self.conn.broken:
                self.conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
