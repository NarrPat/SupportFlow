"""Strict runtime contracts for the four read-only diagnosis tools."""
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Count = Annotated[int, Field(ge=0, le=2**31 - 1)]
ErrorCode = Literal["NOT_FOUND", "FORBIDDEN", "VALIDATION", "TIMEOUT", "RATE_LIMIT",
                    "UPSTREAM_UNAVAILABLE", "UNKNOWN_OUTCOME", "RESULT_TOO_LARGE"]
BlockingCode = Literal["CAPACITY_EXHAUSTED", "PLAN_NOT_ACTIVE", "DOMAIN_NOT_ALLOWED", "INVITATION_EXPIRED",
    "INVITATION_NOT_YET_VALID", "INVITATION_PERMISSION_DENIED", "SERVICE_UNAVAILABLE", "ALREADY_JOINED", "INVITATION_REVOKED"]
UnknownCode = Literal["KNOWLEDGE_EVIDENCE_MISSING", "DOMAIN_POLICY_UNKNOWN", "INVITATION_PERMISSION_UNKNOWN",
    "INVITATION_NOT_FOUND", "INVITATION_UNAVAILABLE", "INVITATION_STATUS_UNKNOWN", "SUBSCRIPTION_NOT_FOUND",
    "SUBSCRIPTION_UNAVAILABLE", "SERVICE_STATUS_UNKNOWN", "PLAN_STATUS_UNKNOWN", "CUSTOMER_UNAVAILABLE"]
ResolutionCode = Literal["NO_ACTION_REQUIRED", "EVIDENCE_REQUIRED", "FACTS_INCOMPLETE", "CUSTOMER_REQUIRED", "INPUT_REQUIRED"]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class Customer(StrictModel):
    customer_id: Identifier
    customer_code: Identifier
    display_name: Annotated[str, Field(min_length=1, max_length=256)]


class Subscription(StrictModel):
    subscription_id: Identifier
    base_seats: Count
    used_seats: Count
    temporary_seats: Count
    temporary_expires_at: AwareDatetime | None
    plan_status: Annotated[str, Field(min_length=1, max_length=64)]
    business_version: Annotated[int, Field(ge=1, le=2**63 - 1)]

    @model_validator(mode="after")
    def temporary_expiry(self):
        if self.temporary_seats and self.temporary_expires_at is None:
            raise ValueError("Temporary seats require an expiry")
        return self


class InvitationPolicy(StrictModel):
    allowed_domains: Annotated[list[str], Field(max_length=50)] | None
    allow_subdomains: bool | None
    invitation_permission: bool | None

    @field_validator("allowed_domains")
    @classmethod
    def valid_domains(cls, value):
        from .rules import normalize_domain
        return None if value is None else [normalize_domain(domain) for domain in value]


class Invitation(StrictModel):
    invitation_id: Identifier
    email: Annotated[str, Field(min_length=3, max_length=254)]
    email_domain: Annotated[str, Field(min_length=1, max_length=253)]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: Annotated[str, Field(min_length=1, max_length=64)]
    reason_code: Annotated[str, Field(max_length=128)] | None

    @model_validator(mode="after")
    def valid_record(self):
        from .rules import normalize_domain, normalize_email
        if self.email != normalize_email(self.email) or self.email_domain != normalize_domain(self.email_domain):
            raise ValueError("Invitation identity must be normalized")
        if self.email.rsplit("@", 1)[1] != self.email_domain or self.expires_at <= self.created_at:
            raise ValueError("Invitation identity or validity interval is inconsistent")
        return self


class InvitationObservation(StrictModel):
    invitation: Invitation
    policy: InvitationPolicy
    history: Annotated[list[Invitation], Field(max_length=5)] = Field(default_factory=list)

    @model_validator(mode="after")
    def scoped_history(self):
        ids = [self.invitation.invitation_id, *(r.invitation_id for r in self.history)]
        if len(ids) != len(set(ids)) or any(r.email != self.invitation.email or
                (r.created_at, r.invitation_id) > (self.invitation.created_at, self.invitation.invitation_id)
                for r in self.history):
            raise ValueError("History must be distinct older records for the same normalized email")
        return self


class ServiceStatus(StrictModel):
    component: Literal["invitations"]
    status: Annotated[str, Field(min_length=1, max_length=64)]
    updated_at: AwareDatetime


class ToolEnvelope(StrictModel):
    ok: bool
    data: dict | None
    error_code: ErrorCode | None
    retryable: bool
    source: Annotated[str, Field(min_length=1, max_length=128)]
    observed_at: AwareDatetime
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value):
        # Literal equality alone considers True equal to 1, even in strict mode.
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.ok and (self.data is None or self.error_code is not None or self.retryable):
            raise ValueError("Success envelope is inconsistent")
        if not self.ok and (self.data is not None or self.error_code is None):
            raise ValueError("Failure envelope is inconsistent")
        return self


class Intent(StrictModel):
    intent: Literal["subscription_status", "invitation_diagnosis"]
    invitation_email: Annotated[str, Field(max_length=254)] | None

    @field_validator("invitation_email")
    @classmethod
    def valid_email(cls, value):
        from .rules import normalize_email
        return None if value is None else normalize_email(value)


class Summary(StrictModel):
    resolution_code: Literal["NO_ACTION_REQUIRED", "EVIDENCE_REQUIRED", "FACTS_INCOMPLETE"]
    blocking_codes: Annotated[list[BlockingCode], Field(max_length=20)]
    unknown_codes: Annotated[list[UnknownCode], Field(max_length=20)]


class SubscriptionFacts(Subscription):
    subscription_active: bool
    effective_temporary: Count
    capacity: Annotated[int, Field(ge=0, le=2**32 - 2)]
    remaining: Annotated[int, Field(ge=0, le=2**32 - 2)]
    over_capacity: bool


class InvitationFacts(Invitation):
    history: Annotated[list[Invitation], Field(max_length=5)] = Field(default_factory=list)
    within_validity: bool
    domain_allowed: bool | None
    invitation_permission: bool | None


class Facts(StrictModel):
    as_of: AwareDatetime
    subscription: SubscriptionFacts | None
    invitation: InvitationFacts | None
    service_status: ServiceStatus | None


class ObservationRef(StrictModel):
    run_id: Identifier
    execution_generation: Annotated[int, Field(ge=1)]
    ordinal: Annotated[int, Field(ge=1, le=9)]
    tool_id: Literal["get_customer", "get_subscription", "get_invitation", "get_service_status", "list_external_issues"]

    @model_validator(mode="after")
    def retain_internal_ordinal_limit(self):
        if self.tool_id != "list_external_issues" and self.ordinal > 6:
            raise ValueError("Internal diagnosis observation is outside its tool budget")
        return self


class DiagnosisResult(StrictModel):
    schema_version: Literal[2] = 2
    resolution_code: ResolutionCode
    customer: Customer | None
    intent: Literal["subscription_status", "invitation_diagnosis"] | None
    facts: Facts
    blocking_codes: Annotated[list[BlockingCode], Field(max_length=20)]
    unknown_codes: Annotated[list[UnknownCode], Field(max_length=20)]
    evidence_refs: Annotated[list, Field(max_length=0)]
    action_receipts: Annotated[list, Field(max_length=0)]
    observation_refs: Annotated[list[ObservationRef], Field(max_length=6)]
    reply_text: Annotated[str, Field(min_length=1, max_length=4000)]


class EvidenceRef(StrictModel):
    """Immutable location metadata; document text requires a fresh authorized read."""
    evidence_id: Identifier
    document_id: Identifier
    version_id: Identifier
    section_id: Identifier
    generation: Annotated[int, Field(ge=1)]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    source: Literal["supportflow.knowledge"]
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def ordered_span(self):
        if self.end_line < self.start_line:
            raise ValueError("Invalid citation span")
        return self


class EvidenceContext(StrictModel):
    evidence_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=65536)]


class EvidenceBundle(StrictModel):
    schema_version: Literal[1]
    ok: bool
    error_code: Annotated[str, Field(min_length=1, max_length=80)] | None
    generation: Annotated[int, Field(ge=1)] | None
    refs: Annotated[list[EvidenceRef], Field(max_length=5)]
    context: Annotated[list[EvidenceContext], Field(max_length=5)]
    supported_topics: Annotated[list[Identifier], Field(max_length=20)]
    supported_codes: Annotated[list[BlockingCode], Field(max_length=20)]
    source: Literal["supportflow.knowledge"]
    quality_eligible: bool

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("Expected integer schema version")
        return value

    @model_validator(mode="after")
    def bound_context(self):
        import hashlib
        if not self.ok:
            if self.error_code is None or self.refs or self.context or self.supported_topics or self.supported_codes:
                raise ValueError("Failed retrieval must not retain evidence")
            return self
        if self.error_code is not None or self.generation is None:
            raise ValueError("Successful retrieval requires a generation")
        ids = [ref.evidence_id for ref in self.refs]
        if len(ids) != len(set(ids)) or ids != [item.evidence_id for item in self.context]:
            raise ValueError("Evidence references and context must match exactly")
        for ref, item in zip(self.refs, self.context):
            if ref.generation != self.generation or hashlib.sha256(item.text.encode("utf-8")).hexdigest() != ref.content_sha256:
                raise ValueError("Evidence generation or text hash changed")
        if len(self.supported_topics) != len(set(self.supported_topics)) or len(self.supported_codes) != len(set(self.supported_codes)):
            raise ValueError("Duplicate support declarations")
        if not self.refs and (self.supported_topics or self.supported_codes):
            raise ValueError("Support declarations require evidence")
        return self


class RagSummary(Summary):
    resolution_code: Literal["NO_ACTION_REQUIRED", "DIAGNOSIS_COMPLETE", "EVIDENCE_REQUIRED", "FACTS_INCOMPLETE"]


class RagDiagnosisResult(DiagnosisResult):
    schema_version: Literal[3] = 3
    resolution_code: ResolutionCode | Literal["DIAGNOSIS_COMPLETE"]
    evidence_refs: Annotated[list[EvidenceRef], Field(max_length=5)]

    @model_validator(mode="after")
    def diagnosis_boundary(self):
        refs = self.evidence_refs
        if len({ref.evidence_id for ref in refs}) != len(refs) or len({ref.generation for ref in refs}) > 1:
            raise ValueError("Expected unique evidence from one generation")
        if self.intent == "subscription_status" and refs:
            raise ValueError("A status-only result has no retrieval evidence")
        if self.resolution_code == "DIAGNOSIS_COMPLETE" and (
                self.intent != "invitation_diagnosis" or not refs or self.unknown_codes):
            raise ValueError("A completed diagnosis requires knowledge and complete facts")
        return self
