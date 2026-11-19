"""Current evidence/fact rechecks and checkpoint-backed pending approval.

Only the app ledger changes. There is no approval decision, grant dispatch or
business receipt writer here. Network reads occur in graph nodes, never while
these short app transactions hold the admission/run locks.
"""
from datetime import timedelta
import json

from .contracts import StaleFragment, StopFragment
from .waiting import waiting_milliseconds


def prepared(conn, run, *, proposal_id=None):
    suffix, values = ("p.proposal_id=%s", (proposal_id,)) if proposal_id is not None else ("p.proposal_seq=1", ())
    return conn.execute("""SELECT p.*,a.approval_id,a.status AS approval_status,x.status AS action_status
        FROM proposals p JOIN approvals a USING(tenant_id,run_id,proposal_id)
        JOIN actions x USING(tenant_id,run_id,proposal_id)
        WHERE p.tenant_id=%s AND p.run_id=%s AND """ + suffix,
        (run["tenant_id"], run["run_id"], *values)).fetchone()


def candidate_context(conn, run, *, evidence_refs, observation_refs):
    """Re-read authoritative app rows and current code policy in caller's txn."""
    from apps.supportflow.domain.clock import business_now
    from apps.supportflow.domain.contracts import EvidenceBundle, ToolEnvelope
    from apps.supportflow.retrieval.service import KnowledgeError, validate_refs
    from apps.supportflow.retrieval.contracts import reference
    try:
        rows = validate_refs(conn, run, evidence_refs, current_generation=True, lock=True)
    except KnowledgeError:
        raise StopFragment("EVIDENCE_REQUIRED") from None
    # Only exact, bounded, already-authorized section text enters this local
    # evaluation. No embedding/model/tokenizer call is made in this transaction.
    generation = evidence_refs[0]["generation"] if evidence_refs else None
    evidence = EvidenceBundle.model_validate_json(json.dumps({"schema_version": 1, "ok": generation is not None,
        "error_code": None if generation is not None else "EVIDENCE_REQUIRED", "generation": generation,
        "refs": [reference(row, generation) for row in rows],
        "context": [{"evidence_id": row["evidence_id"], "text": row["text"]} for row in rows],
        "supported_topics": [], "supported_codes": [], "source": "supportflow.knowledge", "quality_eligible": False}))
    observations, latest, recorded = {}, {}, {}
    seen = set()
    for ref in observation_refs:
        key = ref["execution_generation"], ref["ordinal"]
        if (ref["run_id"] != run["run_id"] or ref["execution_generation"] > run["execution_generation"] or key in seen):
            raise StopFragment("CHECKPOINT_INCOMPATIBLE")
        seen.add(key)
        row = conn.execute("""SELECT tool_id,envelope FROM run_tool_observations
            WHERE run_id=%s AND execution_generation=%s AND ordinal=%s""",
            (run["run_id"], *key)).fetchone()
        if row is None or row["tool_id"] != ref["tool_id"]:
            raise StopFragment("CHECKPOINT_INCOMPATIBLE")
        envelope = ToolEnvelope.model_validate_json(json.dumps(row["envelope"]))
        expected = {"get_customer": "app_db.customers", "get_subscription": "sandbox.subscription_grant",
                    "get_invitation": "sandbox.invitations", "get_service_status": "sandbox.service_statuses"}
        if envelope.source != expected.get(row["tool_id"]):
            raise StopFragment("FACTS_INCOMPLETE")
        recorded[key] = (row["tool_id"], envelope)
    for key, (tool_id, envelope) in recorded.items():
        if not envelope.ok:
            # Keep every failed attempt in the ledger. Only the immediate,
            # same-generation successful retry can resolve a network failure.
            # An arbitrary later success cannot hide authorization/DTO errors.
            retry = recorded.get((key[0], key[1] + 1))
            if (not envelope.retryable or envelope.error_code not in {"TIMEOUT", "UPSTREAM_UNAVAILABLE"}
                    or retry is None or retry[0] != tool_id or not retry[1].ok):
                raise StopFragment("FACTS_INCOMPLETE")
            continue
        # A budgeted final subscription read supersedes its initial observation.
        if key > latest.get(tool_id, (-1, -1)):
            observations[tool_id] = envelope.data
            latest[tool_id] = key
    customer = run.get("confirmed_fields", {}).get("customer_id") or run.get("customer_id")
    if observations.get("get_customer", {}).get("customer_id") != customer:
        raise StopFragment("CUSTOMER_BINDING_CHANGED")
    return dict(tenant_id=run["tenant_id"], customer_id=customer, confirmed=run.get("confirmed_fields", {}),
        subscription=observations.get("get_subscription"), invitation=observations.get("get_invitation"),
        service_status=observations.get("get_service_status"), evidence=evidence.model_dump(mode='json'), now=business_now())


def evaluate_candidate(conn, run, *, evidence_refs, observation_refs):
    from apps.supportflow.domain.grant_policy import evaluate_grant
    return evaluate_grant(**candidate_context(conn, run, evidence_refs=evidence_refs, observation_refs=observation_refs))


def validate_prepared(conn, run, proposal):
    from apps.supportflow.domain.proposals import command_value, public_proposal
    if proposal is None or proposal["approval_status"] != "pending" or proposal["action_status"] != "proposed":
        raise StopFragment("PROPOSAL_IMMUTABLE")
    public_proposal(proposal)
    decision = evaluate_candidate(conn, run, evidence_refs=proposal["evidence_refs"], observation_refs=proposal["observation_refs"])
    if decision.decision != "proposal":
        raise StopFragment(decision.reason_code or "EVIDENCE_REQUIRED")
    original = command_value(proposal["canonical_args"])
    if (original.model_dump(exclude={"created_at", "expires_at"})
            != decision.command.model_dump(exclude={"created_at", "expires_at"})):
        raise StopFragment("PROPOSAL_IMMUTABLE")
    return original


def project_wait(conn, run, *, proposal_id, checkpoint_id, checkpoint_ns="", checkpoint_created_at):
    from apps.supportflow.domain.clock import business_now
    from .core import append_event, owns_lock
    if not owns_lock(conn, run["run_id"]):
        raise StaleFragment("Run lock missing")
    proposal = prepared(conn, run, proposal_id=proposal_id)
    command = validate_prepared(conn, run, proposal)
    if run["status"] != "running" or run["admission_state"] != "active" or checkpoint_ns != "":
        raise StaleFragment("Proposal wait no longer owns active execution")
    exists = conn.execute("SELECT 1 FROM checkpoints WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s",
                          (run["run_id"], checkpoint_ns, checkpoint_id)).fetchone()
    if exists is None:
        raise ValueError("Proposal wait requires a durable checkpoint")
    now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    # Ordinary processing and proposal expiry share this one DB timestamp.
    # Eval may use its separately declared fixed business reference instead.
    remaining = command.expires_at-business_now(real_now=now)
    if remaining <= timedelta(0):
        raise StopFragment("PROPOSAL_EXPIRED", "expired")
    # Business reference time can be fixed in eval; real wait TTL remains bounded.
    deadline = now + min(timedelta(seconds=900), remaining)
    conn.execute("""INSERT INTO proposal_waits(proposal_id,tenant_id,run_id,checkpoint_id,checkpoint_ns,checkpoint_at,
        checkpoint_generation,wait_started_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
        (proposal_id, run["tenant_id"], run["run_id"], checkpoint_id, checkpoint_ns, checkpoint_created_at,
         run["execution_generation"], now))
    conn.execute("UPDATE runtime_limits SET active_reserved=active_reserved-1 WHERE app='supportflow'")
    updated = conn.execute("""UPDATE runs SET status='awaiting_approval',reason_code='APPROVAL_REQUIRED',result=NULL,
        admission_state='none',waiting_since=%s,wait_expires_at=%s,version=version+1,last_event_seq=last_event_seq+1
        WHERE run_id=%s AND execution_generation=%s RETURNING *""",
        (now, deadline, run["run_id"], run["execution_generation"])).fetchone()
    append_event(conn, updated, "approval.required", {"proposal_id": proposal_id, "approval_id": proposal["approval_id"],
        "action_id": proposal["action_id"], "expires_at": command.expires_at.isoformat()})
    updated = conn.execute("UPDATE runs SET last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *", (run["run_id"],)).fetchone()
    append_event(conn, updated, "run.status_changed", {"status": "awaiting_approval", "reason_code": "APPROVAL_REQUIRED"})
    return updated


def close_pending(conn, run, *, status="cancelled", now=None):
    if status not in {"expired", "cancelled"}:
        raise ValueError("Invalid proposal closure")
    row = conn.execute("SELECT * FROM proposal_waits WHERE run_id=%s AND status='pending' FOR UPDATE", (run["run_id"],)).fetchone()
    if row is not None:
        now = now or conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        end = max(now, row["wait_started_at"])
        conn.execute("UPDATE proposal_waits SET status=%s,wait_ended_at=%s WHERE proposal_id=%s", (status, end, row["proposal_id"]))
        conn.execute("UPDATE runs SET accumulated_wait_ms=accumulated_wait_ms+%s,waiting_since=NULL,wait_expires_at=NULL WHERE run_id=%s",
                     (waiting_milliseconds(row["wait_started_at"], end), run["run_id"]))
    conn.execute("UPDATE approvals SET status=%s,version=version+1 WHERE run_id=%s AND status='pending'", (status, run["run_id"]))
    conn.execute("UPDATE actions SET status=%s WHERE run_id=%s AND status='proposed'", (status, run["run_id"]))


def expire_wait(conn, run):
    if run["status"] != "awaiting_approval":
        return None
    from apps.supportflow.domain.clock import business_now
    from .core import append_event
    row = prepared(conn, run)
    now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    if row is None or (now < run["wait_expires_at"] and business_now() < row["expires_at"]):
        return None
    close_pending(conn, run, status="expired", now=now)
    updated = conn.execute("""UPDATE runs SET status='expired',reason_code='PROPOSAL_EXPIRED',result=NULL,
        version=version+1,last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *""", (run["run_id"],)).fetchone()
    append_event(conn, updated, "run.status_changed", {"status": "expired", "reason_code": "PROPOSAL_EXPIRED"})
    return updated
