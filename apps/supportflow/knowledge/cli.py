"""Explicit knowledge staging/building. No graph task imports this CLI."""
import argparse
import json
import os
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.rows import dict_row

from apps.supportflow.retrieval.contracts import CHUNKER_VERSION, DIMENSION, MODEL_ID, MODEL_REVISION, KnowledgeError, compact, sha256
from apps.supportflow.retrieval.embedding import default_tokenizer, verified_directory
from apps.supportflow.retrieval.ingest import IndexBuilder, stage_document, validate_overrides
from apps.supportflow.retrieval.lexical import SnapshotCache, lexical_version


HERE = Path(__file__).resolve().parent


def connect():
    database = os.environ.get("PGDATABASE", "sf_app")
    if database != "sf_app" and not re.fullmatch(r"sf_eval_[a-z0-9_]+_app", database):
        raise KnowledgeError("INVALID_DATABASE")
    role = os.environ.get("PGUSER", "sf_app_runtime")
    if role not in {"sf_app_runtime", "sf_app_migrator"}:
        raise KnowledgeError("INVALID_DATABASE_ROLE")
    password = Path(os.environ.get("PGPASSWORD_FILE", "/run/secrets/app_runtime_password")).read_text().strip()
    return psycopg.connect(host=os.environ.get("PGHOST", "postgres"), port=int(os.environ.get("PGPORT", "5432")),
        dbname=database, user=role, password=password, autocommit=True, row_factory=dict_row, connect_timeout=8,
        application_name="supportflow-knowledge", options="-c statement_timeout=30000 -c lock_timeout=5000")


def require_admin(conn, tenant, actor, *, lock=False):
    row = conn.execute("SELECT role FROM memberships WHERE tenant_id=%s AND user_id=%s" + (" FOR SHARE" if lock else ""),
                       (tenant, actor)).fetchone()
    if row is None or row["role"] != "admin":
        raise KnowledgeError("FORBIDDEN")


def seed(conn, tenant, actor, *, corpus_tenant="primary"):
    """Only authored product files. Evaluation fixtures are never imported here."""
    require_admin(conn, tenant, actor)
    catalog = json.loads((HERE / "corpus.json").read_text(encoding="utf-8"))
    overrides, staged = {}, []
    for entry in catalog["documents"]:
        if entry["tenant"] != corpus_tenant:
            continue
        document_id = str(uuid5(NAMESPACE_URL, "supportflow:product-knowledge:"+tenant+":"+entry["slug"]))
        with conn.transaction():
            require_admin(conn, tenant, actor, lock=True)
            conn.execute("INSERT INTO knowledge_state(tenant_id) VALUES(%s) ON CONFLICT DO NOTHING", (tenant,))
            conn.execute("SELECT tenant_id FROM knowledge_state WHERE tenant_id=%s FOR UPDATE", (tenant,))
            created = conn.execute("""INSERT INTO documents(document_id,tenant_id,title,visibility)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING document_id""",
                (document_id, tenant, entry["title"], entry["visibility"])).fetchone()
            if created and entry["visibility"] == "private":
                conn.execute("INSERT INTO document_acl(tenant_id,document_id,user_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                             (tenant, document_id, actor))
            row = stage_document(conn, tenant, actor, filename=entry["file"],
                content=(HERE / "documents" / entry["file"]).read_bytes(), title=entry["title"],
                document_id=document_id, visibility=entry["visibility"], corpus_origin="synthetic_business")
        staged.append({"document_id": document_id, "version_id": row["version_id"], "slug": entry["slug"]})
        if entry["active"]:
            overrides[document_id] = row["version_id"]
    return {"corpus_origin": "synthetic_business", "staged": staged, "overrides": overrides}


def ensure(conn, tenant, actor, *, corpus_tenant="primary"):
    """Idempotent ordinary-deployment bootstrap, with no background scheduler."""
    staged = seed(conn, tenant, actor, corpus_tenant=corpus_tenant)
    overrides = staged["overrides"]
    verified_directory()
    with conn.transaction():
        require_admin(conn, tenant, actor, lock=True)
        state = conn.execute("SELECT * FROM knowledge_state WHERE tenant_id=%s FOR SHARE", (tenant,)).fetchone()
        current = state["active_generation"]
        generation = conn.execute("SELECT * FROM index_generations WHERE tenant_id=%s AND generation=%s",
                                  (tenant, current)).fetchone() if current else None
        active = {row["document_id"]: row["active_version_id"] for row in conn.execute(
            "SELECT document_id,active_version_id FROM documents WHERE tenant_id=%s FOR SHARE", (tenant,)).fetchall()
            if row["active_version_id"] is not None}
        reusable = (generation and generation["state"] == "active" and generation["vector_ready"]
            and generation["lexical_ready"] and generation["embedding_model"] == MODEL_ID
            and generation["embedding_revision"] == MODEL_REVISION and generation["dimension"] == DIMENSION
            and generation["vector_origin"] == "live_local" and generation["chunker_version"] == CHUNKER_VERSION
            and generation["tokenizer_version"] == default_tokenizer().version
            and generation["lexical_version"] == lexical_version()
            and sha256(compact(generation["manifest"])) == generation["corpus_manifest_hash"]
            and all(active.get(document_id) == version_id for document_id,version_id in overrides.items())
            and active == {row["document_id"]: row["version_id"] for row in generation["manifest"]["documents"]})
        if reusable:
            try:
                SnapshotCache().load(generation)
            except KnowledgeError:
                reusable = False
        if reusable:
            return {**generation, "bootstrap": "already_active"}
    return {**IndexBuilder(conn, actor_id=actor).build(tenant, overrides=overrides, expected_generation=current),
            "bootstrap": "built"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("seed", "build", "ensure"))
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--corpus-tenant", choices=("primary", "secondary"), default="primary")
    parser.add_argument("--overrides-file", type=Path)
    parser.add_argument("--expected-generation", type=int)
    parser.add_argument("--resume-generation", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        with connect() as conn:
            require_admin(conn, args.tenant, args.actor)
            if args.operation == "seed":
                result = seed(conn, args.tenant, args.actor, corpus_tenant=args.corpus_tenant)
            elif args.operation == "ensure":
                row = ensure(conn, args.tenant, args.actor, corpus_tenant=args.corpus_tenant)
                result = {key: row[key] for key in ("tenant_id", "generation", "state", "vector_origin", "corpus_manifest_hash", "bootstrap")}
            else:
                if args.overrides_file is None:
                    raise KnowledgeError("OVERRIDES_REQUIRED")
                overrides = json.loads(args.overrides_file.read_text(encoding="utf-8"))
                if isinstance(overrides, dict) and "overrides" in overrides:
                    overrides = overrides["overrides"]
                validate_overrides(overrides)
                row = IndexBuilder(conn, actor_id=args.actor).build(args.tenant, overrides=overrides,
                    expected_generation=args.expected_generation, resume_generation=args.resume_generation)
                result = {key: row[key] for key in ("tenant_id", "generation", "state", "vector_origin", "corpus_manifest_hash")}
        if args.output:
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({"operation": args.operation, "status": "completed",
                          "count": len(result.get("staged", [])), "generation": result.get("generation"),
                          "bootstrap": result.get("bootstrap")}))
    except (KnowledgeError, psycopg.Error, OSError, ValueError) as error:
        print(json.dumps({"operation": args.operation, "status": "failed", "error_type": type(error).__name__,
                          "code": error.code if isinstance(error, KnowledgeError) else "DEPENDENCY_FAILURE"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
