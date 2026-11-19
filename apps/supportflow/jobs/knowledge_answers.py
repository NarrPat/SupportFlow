"""Final answer publication inside the caller's locked run transaction."""
from apps.supportflow.domain.contracts import EvidenceBundle
from apps.supportflow.domain.knowledge_answers import enabled, roles_enabled, search_enabled, verify_projection
from apps.supportflow.retrieval.contracts import empty_bundle
from apps.supportflow.retrieval.service import KnowledgeError, reviewed_support, validate_refs
from .contracts import FragmentOutcome


def validate_publication(conn, run, outcome):
    try:
        if not enabled(run["config_snapshot"]):
            raise ValueError("Knowledge contract is absent")
        result = outcome.result
        document_search=result.get('knowledge_topic')=='document_search'
        if document_search and not search_enabled(run['config_snapshot']):
            raise ValueError('Document search contract is absent')
        if result.get("knowledge_topic") == "invitation_roles" and not roles_enabled(run["config_snapshot"]):
            raise ValueError("Knowledge roles contract is absent")
        customer_id = run.get("confirmed_fields", {}).get("customer_id") or run.get("customer_id")
        customer = conn.execute("SELECT customer_id,customer_code,display_name FROM customers "
            "WHERE tenant_id=%s AND customer_id=%s FOR SHARE", (run["tenant_id"], customer_id)).fetchone()
        if result.get("customer") is not None and result["customer"] != customer:
            raise ValueError("Saved customer facts differ from the bound customer")
        if any(ref["run_id"] != run["run_id"] or ref["execution_generation"] > run["execution_generation"]
               for ref in result["observation_refs"]):
            raise ValueError("Saved observations differ from this run")
        rows = validate_refs(conn, run, result["evidence_refs"], current_generation=True, lock=True)
        data = empty_bundle("EVIDENCE_REQUIRED")
        if rows:
            topics, codes = ([],[]) if document_search else reviewed_support(rows)
            if not document_search and any(row["section_content_sha256"] != row["content_sha256"] for row in rows):
                raise KnowledgeError("INVALID_REFERENCE")
            data.update(ok=True, error_code=None, generation=result["evidence_refs"][0]["generation"], refs=result["evidence_refs"],
                context=[{"evidence_id": row["evidence_id"], "text": row["text"]} for row in rows],
                supported_topics=topics, supported_codes=codes)
        verified = verify_projection(result, EvidenceBundle.model_validate(data), customer_id=customer_id)
        expected_status = "succeeded" if verified.resolution_code == "KNOWLEDGE_ANSWERED" else "handoff"
        if (outcome.status, outcome.reason_code) != (expected_status, verified.resolution_code):
            raise ValueError("Saved terminal status differs from verified answer")
        return outcome
    except (KnowledgeError, ValueError, TypeError, KeyError):
        # Do not publish a changed checkpoint reply, stale text or stale citations.
        return FragmentOutcome("handoff", "EVIDENCE_REQUIRED")
