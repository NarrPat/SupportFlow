"""Canonical, immutable proposal preparation with a trusted graph-specific gate.

The grant graph rechecks current policy, scope and stored observations here.
Legacy private storage tests exercise identity only and confer no authority.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Annotated, Literal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import EvidenceRef, Identifier, ObservationRef, StrictModel


class ProposalCommand(StrictModel):
    schema_version: Literal[1] = 1
    action_type: Literal["grant_temporary_seats"] = "grant_temporary_seats"
    command_schema_version: Literal[1] = 1
    tenant_id: Identifier
    customer_id: Identifier
    subscription_id: Identifier
    seats: Annotated[int, Field(ge=1, le=20)]
    days: Annotated[int, Field(ge=1, le=7)]
    policy_version: Identifier
    business_version: Annotated[int, Field(ge=1, le=2**63-1)]
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("schema_version", "command_schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("Expected a strict integer version")
        return value

    @field_validator("tenant_id", "customer_id", "subscription_id", "policy_version")
    @classmethod
    def db_text(cls, value):
        if not value.strip() or "\0" in value:
            raise ValueError("Expected a nonempty database identifier")
        return value

    @model_validator(mode="after")
    def bounded_lifetime(self):
        if self.expires_at - self.created_at != timedelta(seconds=900):
            raise ValueError("Expected a fixed 900-second proposal lifetime")
        return self


def utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Expected an aware server timestamp")
    value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds" if value.microsecond else "seconds").replace("+00:00", "Z")


def command_value(value: ProposalCommand | dict) -> ProposalCommand:
    if isinstance(value, ProposalCommand):
        # Revalidate even a model_construct result at the storage boundary.
        value = value.model_dump(mode="json")
    return ProposalCommand.model_validate_json(json.dumps(value, ensure_ascii=False, allow_nan=False))


def canonical_action_json(value: dict) -> str:
    """Bind every fixed command field, without authorizing an action/version.

    Useful when rejecting a modified command against a known hash. Storage and
    execution must separately call command_value and their policy gates.
    """
    if not isinstance(value, dict) or set(value) != set(ProposalCommand.model_fields):
        raise ValueError("Unexpected canonical command fields")
    payload = dict(value)
    for name in ("schema_version", "command_schema_version", "seats", "days", "business_version"):
        if type(payload[name]) is not int:
            raise ValueError("Expected strict JSON integers")
    for name in ("action_type", "tenant_id", "customer_id", "subscription_id", "policy_version"):
        if not isinstance(payload[name], str) or not payload[name].strip() or "\0" in payload[name]:
            raise ValueError("Expected a nonempty identifier")
    for name in ("created_at", "expires_at"):
        stamp = payload[name]
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        payload[name] = utc_text(stamp)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def hash_raw_action(value: dict) -> str:
    return hashlib.sha256(canonical_action_json(value).encode("utf-8")).hexdigest()


def canonical_command(value: ProposalCommand | dict) -> str:
    command = command_value(value)
    payload = command.model_dump(mode="json")
    payload.update(created_at=utc_text(command.created_at), expires_at=utc_text(command.expires_at))
    return canonical_action_json(payload)


def command_hash(value: ProposalCommand | dict) -> str:
    return hashlib.sha256(canonical_command(value).encode("utf-8")).hexdigest()


class ProposalObservationRef(StrictModel):
    """Grant preparation references; does not widen readonly diagnosis DTOs."""
    run_id: Identifier
    execution_generation: Annotated[int, Field(ge=1)]
    ordinal: Annotated[int, Field(ge=1, le=8)]
    tool_id: Literal['get_customer', 'get_subscription', 'get_invitation', 'get_service_status']


class ProposalView(StrictModel):
    proposal_id: Identifier
    action_id: Identifier
    approval_id: Identifier | None = None
    proposal_seq: Annotated[int, Field(ge=1)]
    schema_version: Literal[1]
    action_type: Literal["grant_temporary_seats"]
    command_schema_version: Literal[1]
    customer_id: Identifier
    subscription_id: Identifier
    seats: Annotated[int, Field(ge=1, le=20)]
    days: Annotated[int, Field(ge=1, le=7)]
    canonical_args_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    policy_version: Identifier
    business_version: Annotated[int, Field(ge=1)]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    evidence_refs: Annotated[list[EvidenceRef], Field(min_length=1, max_length=5)]
    observation_refs: Annotated[list[ProposalObservationRef], Field(min_length=1, max_length=8)]


class ProposalReferences(StrictModel):
    evidence_refs: Annotated[list[EvidenceRef], Field(min_length=1, max_length=5)]
    observation_refs: Annotated[list[ProposalObservationRef], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def distinct(self):
        if len({ref.evidence_id for ref in self.evidence_refs}) != len(self.evidence_refs):
            raise ValueError("Duplicate evidence reference")
        keys = {(ref.run_id, ref.execution_generation, ref.ordinal) for ref in self.observation_refs}
        if len(keys) != len(self.observation_refs):
            raise ValueError("Duplicate observation reference")
        return self


def public_proposal(row: dict) -> dict:
    command = command_value(row["canonical_args"])
    if command.tenant_id != row["tenant_id"]:
        raise ValueError("Stored proposal tenant mismatch")
    if command_hash(command) != row["canonical_args_hash"]:
        raise ValueError("Stored proposal hash mismatch")
    if (utc_text(command.created_at) != utc_text(row["created_at"])
            or utc_text(command.expires_at) != utc_text(row["expires_at"])):
        raise ValueError("Stored proposal timestamps mismatch")
    payload = command.model_dump(mode="json", exclude={"tenant_id"})
    payload.update(**{key: row[key] for key in ("proposal_id", "action_id", "proposal_seq", "canonical_args_hash",
                                               "evidence_refs", "observation_refs")})
    if row.get("approval_id") is not None:
        payload["approval_id"] = row["approval_id"]
    return ProposalView.model_validate_json(json.dumps(payload, ensure_ascii=False)).model_dump(mode="json")


def persist_prepared_proposal(conn, run: dict, command: ProposalCommand | dict, *,
                              evidence_refs: list[dict], observation_refs: list[dict], proposal_seq: int = 1) -> dict:
    """Private storage only. It does not certify proposal eligibility or approve.

    Must run on the fixed worker connection after the caller's current grant
    gate, repeated here for the grant graph. Stable identity
    is read before comparing non-time command fields, so re-entry cannot renew
    an existing proposal's creation/expiry, or allocate a second action.
    """
    from apps.supportflow.jobs.core import Gate, lock_final_scope
    from apps.supportflow.jobs.contracts import StopFragment
    if type(proposal_seq) is not int or not 1 <= proposal_seq <= 2**31-1:
        raise ValueError("Expected a positive proposal sequence")
    command = command_value(command)
    refs = ProposalReferences.model_validate_json(json.dumps({"evidence_refs": evidence_refs, "observation_refs": observation_refs}))
    evidence = [ref.model_dump(mode="json") for ref in refs.evidence_refs]
    observations = [ref.model_dump(mode="json") for ref in refs.observation_refs]
    with conn.transaction():
        current = Gate(conn, run["run_id"], run["execution_generation"])._current()
        if not lock_final_scope(conn, current):
            raise StopFragment("AUTHORIZATION_REVOKED")
        if current["config_snapshot"].get("graph_version") in {"grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}:
            from apps.supportflow.jobs.proposal_waiting import evaluate_candidate
            decision = evaluate_candidate(conn, current, evidence_refs=evidence, observation_refs=observations)
            if decision.decision != "proposal":
                raise StopFragment(decision.reason_code or "EVIDENCE_REQUIRED")
            if (command.model_dump(exclude={"created_at", "expires_at"})
                    != decision.command.model_dump(exclude={"created_at", "expires_at"})):
                raise StopFragment("PROPOSAL_BINDING_MISMATCH")
            command = decision.command
        customer = current.get("confirmed_fields", {}).get("customer_id") or current.get("customer_id")
        if command.tenant_id != current["tenant_id"] or command.customer_id != customer:
            raise StopFragment("PROPOSAL_BINDING_MISMATCH")
        if any(ref["run_id"] != current["run_id"] or ref["execution_generation"] > current["execution_generation"]
               for ref in observations):
            raise StopFragment("PROPOSAL_BINDING_MISMATCH")
        previous = conn.execute("""
            SELECT p.*,a.approval_id FROM public.proposals p LEFT JOIN public.approvals a USING(tenant_id,run_id,proposal_id)
            WHERE p.tenant_id=%s AND p.run_id=%s AND p.proposal_seq=%s
        """, (current["tenant_id"], current["run_id"], proposal_seq)).fetchone()
        if previous is not None:
            old = command_value(previous["canonical_args"])
            if (old.model_dump(exclude={"created_at", "expires_at"})
                    != command.model_dump(exclude={"created_at", "expires_at"})):
                raise StopFragment("PROPOSAL_IMMUTABLE")
            public_proposal(previous)  # Never return a corrupt stored binding.
            return previous
        # References were validated from app rows above for the grant graph.
        row = conn.execute("""
            INSERT INTO public.proposals
                (proposal_id,action_id,tenant_id,run_id,proposal_seq,canonical_args,canonical_args_hash,
                 created_at,expires_at,evidence_refs,observation_refs)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (str(uuid4()), str(uuid4()), current["tenant_id"], current["run_id"], proposal_seq,
              Jsonb(json.loads(canonical_command(command))), command_hash(command), command.created_at, command.expires_at,
              Jsonb(evidence), Jsonb(observations))).fetchone()
        approval_id = str(uuid4())
        conn.execute("""INSERT INTO actions(action_id,tenant_id,run_id,proposal_id,canonical_args_hash)
            VALUES(%s,%s,%s,%s,%s)""", (row["action_id"], row["tenant_id"], row["run_id"], row["proposal_id"], row["canonical_args_hash"]))
        conn.execute("""INSERT INTO approvals(approval_id,tenant_id,run_id,proposal_id) VALUES(%s,%s,%s,%s)""",
                     (approval_id, row["tenant_id"], row["run_id"], row["proposal_id"]))
        return {**row, "approval_id": approval_id}
