"""Read-only raw observations for an explicitly idle ordinary proposal startup.

There are no verdict booleans, provider requests or configurable database names.
The two setup containers each keep their existing migrator secret; the API mode
reads only selected nonsecret configuration from that actual container.
"""
import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

PHASES = ("before_migration", "after_migration", "after_ensure", "live", "final")
RELATIONS = {
    "app": ("alembic_version", "runs", "pending_inputs", "proposals", "approvals", "actions", "proposal_waits",
            "request_idempotency", "knowledge_state", "index_generations", "chunks", "documents", "index_vectors",
            "approval_decision_requests", "dispatch_authorizations", "action_dispatches", "ticket_resolution_audits"),
    "sandbox": ("alembic_version", "subscriptions", "invitation_policies", "invitations", "service_statuses",
                "action_receipts", "grant_policy_state"),
}
BUSINESS_ORDER = {
    "subscriptions": "tenant_id,customer_id,subscription_id",
    "invitation_policies": "tenant_id,customer_id",
    "invitations": "tenant_id,customer_id,invitation_id",
    "service_statuses": "tenant_id,customer_id,component",
    "action_receipts": "tenant_id,customer_id,subscription_id,after_version",
}
ROW_LIMIT = 1024


def ordinary_database(target, environment=None):
    env = os.environ if environment is None else environment
    if target not in ("app", "sandbox", "api"):
        raise ValueError("Unknown startup observation target")
    expected = "sf_sandbox" if target == "sandbox" else "sf_app"
    if env.get("PGDATABASE", expected) != expected:
        raise ValueError("Proposal startup observation requires its ordinary database")
    if env.get("SF_RUNTIME_PROFILE") == "eval" or env.get("SF_EVAL_REFERENCE_TIME") is not None:
        raise ValueError("Ordinary startup observation cannot inherit an evaluation clock")
    return expected


def json_default(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected an aware SQL timestamp")
        return value.isoformat()
    raise TypeError("Unexpected startup SQL value")


def relation(conn, table):
    if table not in {item for items in RELATIONS.values() for item in items}:
        raise ValueError("Unknown startup relation")
    return conn.execute("SELECT to_regclass(%s)::text AS relation", ("public." + table,)).fetchone()["relation"]


def bounded_rows(conn, table, order, present):
    """A missing or over-limit relation remains explicit, never an empty table."""
    allowed_order = {**BUSINESS_ORDER, "grant_policy_state": "singleton"}
    if table not in allowed_order or order != allowed_order[table]:
        raise ValueError("Unknown startup table projection")
    if present is None:
        return {"relation": None, "count": None, "rows": None}
    count = conn.execute(f"SELECT count(*) AS count FROM public.{table}").fetchone()["count"]
    if count > ROW_LIMIT:
        return {"relation": present, "count": count, "rows": None, "row_limit_exceeded": ROW_LIMIT}
    return {"relation": present, "count": count,
            "rows": conn.execute(f"SELECT * FROM public.{table} ORDER BY {order}").fetchall()}


def collect_database(conn, target, phase):
    if target not in RELATIONS or phase not in PHASES:
        raise ValueError("Invalid startup observation")
    identity = conn.execute("""SELECT current_database() AS database,current_user AS actor,
        pg_backend_pid() AS backend_pid,current_setting('transaction_read_only') AS transaction_read_only,
        clock_timestamp() AS observed_at""").fetchone()
    present = {table: relation(conn, table) for table in RELATIONS[target]}
    result = {"schema_version": 1, "target": target, "phase": phase, "identity": identity,
              "relations": present,
              "revision_rows": conn.execute("SELECT version_num FROM public.alembic_version ORDER BY version_num").fetchall()
                               if present["alembic_version"] is not None else None}
    if target == "app":
        result["counters"] = conn.execute("""SELECT count(*) AS total_runs,
            count(*) FILTER (WHERE status IN ('queued','running','resume_queued')) AS runnable,
            coalesce(sum(model_attempts),0)::bigint AS model_attempts FROM public.runs""").fetchone() if present["runs"] else None
        result["entity_counts"] = {table: conn.execute(f"SELECT count(*) AS count FROM public.{table}").fetchone()["count"]
            if present[table] is not None else None for table in ("pending_inputs", "proposals", "approvals", "actions", "proposal_waits", "approval_decision_requests", "dispatch_authorizations", "action_dispatches", "ticket_resolution_audits")}
        result["knowledge"] = conn.execute("""SELECT s.tenant_id,s.active_generation,g.generation,g.state,g.vector_origin,
            g.vector_ready,g.lexical_ready,g.embedding_model,g.embedding_revision,g.dimension,
            g.corpus_manifest_hash,g.snapshot_checksum FROM knowledge_state s LEFT JOIN index_generations g
            ON g.tenant_id=s.tenant_id AND g.generation=s.active_generation ORDER BY s.tenant_id""").fetchall() if present["knowledge_state"] and present["index_generations"] else None
        result["policy_sections"] = conn.execute("""SELECT c.tenant_id,c.evidence_id,c.document_id,c.version_id,c.section_id,
            c.title,c.text,c.content_sha256,c.start_line,c.end_line,d.active_version_id,s.active_generation AS generation
            FROM knowledge_state s JOIN index_vectors iv ON iv.tenant_id=s.tenant_id AND iv.generation=s.active_generation
            JOIN chunks c ON c.tenant_id=iv.tenant_id AND c.evidence_id=iv.evidence_id
            JOIN documents d ON d.tenant_id=c.tenant_id AND d.document_id=c.document_id
            WHERE c.section_id LIKE 'policy.%' ORDER BY c.tenant_id,c.evidence_id""").fetchall() if all(present.get(key) for key in ("knowledge_state", "index_vectors", "chunks", "documents")) else None
    else:
        result["business"] = {table: bounded_rows(conn, table, order, present[table]) for table, order in BUSINESS_ORDER.items()}
        result["policy_state"] = bounded_rows(conn, "grant_policy_state", "singleton", present["grant_policy_state"])
    return result


def collect_api(phase):
    ordinary_database("api")
    if phase not in ("live", "final"):
        raise ValueError("The API configuration can only be observed after startup")
    from apps.supportflow.agent.models import snapshot_settings
    value = snapshot_settings()
    keys = ("schema_version", "contract_version", "graph_version", "grant_contract_version", "model_mode", "retrieval")
    if value.get("graph_version") in {"grant-approval-v1", "grant-execution-v1"}:
        keys += ("approval_contract_version", "tool_round_limit")
    if value.get("graph_version") == "grant-execution-v1":
        keys += ("execution_contract_version",)
    return {"schema_version": 1, "target": "api", "phase": phase,
            "observed_at": datetime.now(UTC).isoformat(),
            "graph_environment": os.environ.get("SF_GRAPH_VERSION"),
            "snapshot": {key: value.get(key) for key in keys}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("app", "sandbox", "api"), required=True)
    parser.add_argument("--phase", choices=PHASES, required=True)
    args = parser.parse_args()
    database = ordinary_database(args.target)
    if args.target == "api":
        value = collect_api(args.phase)
    else:
        secret = Path("/run/secrets/" + args.target + "_migrator_password").read_text(encoding="utf-8").strip()
        if not secret:
            raise ValueError("Missing startup migrator credential")
        with psycopg.connect(host=os.environ.get("PGHOST", "postgres"), dbname=database,
            user="sf_" + args.target + "_migrator", password=secret, connect_timeout=5,
            autocommit=True, row_factory=dict_row, options="-c statement_timeout=5000 -c lock_timeout=1000",
            application_name="supportflow-proposal-startup-probe") as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                value = collect_database(conn, args.target, args.phase)
    print("SF_PROPOSAL_PROBE " + json.dumps(value, default=json_default, ensure_ascii=False, sort_keys=True, allow_nan=False))


if __name__ == "__main__": main()
