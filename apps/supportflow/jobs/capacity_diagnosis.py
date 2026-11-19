"""Rebuild a capacity result from durable tool observations at publication."""
import json

from apps.supportflow.domain.capacity_diagnosis import enabled, verify_projection
from apps.supportflow.domain.contracts import EvidenceBundle, ToolEnvelope
from apps.supportflow.retrieval.contracts import empty_bundle
from apps.supportflow.retrieval.service import KnowledgeError, reviewed_support, validate_refs
from .contracts import FragmentOutcome


def validate_publication(conn, run, outcome):
    try:
        if not enabled(run["config_snapshot"]):
            raise ValueError("Capacity contract is absent")
        result = outcome.result
        customer_id = run.get("confirmed_fields", {}).get("customer_id") or run.get("customer_id")
        customer = conn.execute("SELECT customer_id,customer_code,display_name FROM customers "
            "WHERE tenant_id=%s AND customer_id=%s FOR SHARE", (run["tenant_id"], customer_id)).fetchone()
        if result.get("customer") is not None and result["customer"] != customer:
            raise ValueError("Saved customer facts differ from the bound customer")
        observations = {}
        for ref in result["observation_refs"]:
            if (ref["run_id"] != run["run_id"] or ref["execution_generation"] > run["execution_generation"]
                    or ref["tool_id"] not in {"get_customer", "get_subscription"} or ref["tool_id"] in observations):
                raise ValueError("Unexpected capacity observation")
            row = conn.execute("SELECT tool_id,envelope FROM run_tool_observations "
                "WHERE run_id=%s AND execution_generation=%s AND ordinal=%s",
                (ref["run_id"], ref["execution_generation"], ref["ordinal"])).fetchone()
            if row is None or row["tool_id"] != ref["tool_id"]:
                raise ValueError("Capacity observation disappeared")
            observations[ref["tool_id"]] = ToolEnvelope.model_validate_json(json.dumps(row["envelope"]))
        if set(observations) != {"get_customer", "get_subscription"}:
            raise ValueError("Capacity result requires both observed tools")
        rows = validate_refs(conn, run, result["evidence_refs"], current_generation=True, lock=True)
        data = empty_bundle("EVIDENCE_REQUIRED")
        if rows:
            if any(row["section_content_sha256"] != row["content_sha256"] for row in rows):
                raise KnowledgeError("INVALID_REFERENCE")
            topics, codes = reviewed_support(rows)
            data.update(ok=True, error_code=None, generation=result["evidence_refs"][0]["generation"],
                refs=result["evidence_refs"], context=[{"evidence_id": row["evidence_id"], "text": row["text"]} for row in rows],
                supported_topics=topics, supported_codes=codes)
        verified = verify_projection(result, observations, EvidenceBundle.model_validate(data), customer_id=customer_id)
        expected = "succeeded" if verified.resolution_code == "DIAGNOSIS_COMPLETE" else "handoff"
        if (outcome.status, outcome.reason_code) != (expected, verified.resolution_code):
            raise ValueError("Saved terminal status differs from verified capacity")
        return outcome
    except (KnowledgeError, ValueError, TypeError, KeyError):
        return FragmentOutcome("handoff", "EVIDENCE_REQUIRED")
