"""Trusted grant-reading contracts, separate from the legacy read-only tool DTO.

An observation is evidence to evaluate, never permission to dispatch a write.
The existing get_subscription endpoint keeps its original Subscription shape.
"""
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import Identifier, StrictModel, Subscription


class GrantReceipt(StrictModel):
    action_id: Identifier
    tenant_id: Identifier
    customer_id: Identifier
    subscription_id: Identifier
    canonical_args_hash: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    before_version: Annotated[int, Field(ge=1, le=2**63-2)]
    after_version: Annotated[int, Field(ge=2, le=2**63-1)]
    committed_business_at: AwareDatetime
    seats: Annotated[int, Field(ge=1, le=20)]
    days: Annotated[int, Field(ge=1, le=7)]
    temporary_expires_at: AwareDatetime
    policy_version: Identifier


class GrantHistory(StrictModel):
    # False remains representable so the gate can distinguish unknown from empty.
    complete: bool
    receipts: Annotated[list[GrantReceipt], Field(max_length=50)]


class GrantPolicyConstants(StrictModel):
    action_type: str
    command_schema_version: int
    seats_min: int
    seats_max: int
    days_min: int
    days_max: int
    required_plan_status: str
    cooldown_seconds: int
    proposal_ttl_seconds: int
    day_seconds: int
    minimum_remaining_seats: int
    temporary_grant_mode: str
    approval_role: str
    self_approval_allowed: bool


class GrantPolicyBinding(StrictModel):
    policy_version: Identifier
    section_id: Identifier
    content_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    constants: GrantPolicyConstants


class GrantSubscriptionObservation(Subscription):
    grant_history: GrantHistory
    policy: GrantPolicyBinding


class GrantConfirmedFields(StrictModel):
    requested_action: Literal['grant_temporary_seats'] | None = None
    customer_id: Identifier | None = None
    invitation_email: Annotated[str, Field(max_length=254)] | None = None
    seats: Annotated[int, Field(ge=1, le=20)] | None = None
    days: Annotated[int, Field(ge=1, le=7)] | None = None

    @field_validator('invitation_email')
    @classmethod
    def normalized_email(cls, value):
        from .rules import normalize_email
        return None if value is None else normalize_email(value)

    @model_validator(mode='after')
    def action_required(self):
        if self.requested_action is None and any(value is not None for value in (
                self.invitation_email, self.seats, self.days)):
            raise ValueError('Structured grant fields require a requested action')
        return self
