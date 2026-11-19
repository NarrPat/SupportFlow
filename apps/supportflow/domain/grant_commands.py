"""Authenticated internal grant wire types; none of these are model tools.

The wire preserves the frozen proposal hash. Authorization metadata is a trusted
backend reference, not an independently verified cross-database capability.
"""
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import Identifier, StrictModel
from .grant_contracts import GrantReceipt
from .proposals import ProposalCommand, command_hash, command_value

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _identifier(value: str) -> str:
    if not value.strip() or "\0" in value:
        raise ValueError("Expected a nonempty database identifier")
    return value


class GrantCommand(StrictModel):
    action_id: Identifier
    command_schema_version: Literal[1]
    proposal_id: Identifier
    tenant_id: Identifier
    customer_id: Identifier
    subscription_id: Identifier
    seats: Annotated[int, Field(ge=1, le=20)]
    days: Annotated[int, Field(ge=1, le=7)]
    expected_business_version: Annotated[int, Field(ge=1, le=2**63-1)]
    policy_version: Identifier
    proposal_expires_at: AwareDatetime
    canonical_args_hash: Hash
    authorization_id: Identifier
    authorized_at: AwareDatetime

    @field_validator("command_schema_version", mode="before")
    @classmethod
    def integer_schema(cls, value):
        if type(value) is not int:
            raise ValueError("Expected a strict integer schema version")
        return value

    @field_validator("action_id", "proposal_id", "tenant_id", "customer_id", "subscription_id",
                     "policy_version", "authorization_id")
    @classmethod
    def db_text(cls, value):
        return _identifier(value)

    def proposal_command(self) -> ProposalCommand:
        return ProposalCommand(tenant_id=self.tenant_id, customer_id=self.customer_id,
            subscription_id=self.subscription_id, seats=self.seats, days=self.days,
            policy_version=self.policy_version, business_version=self.expected_business_version,
            created_at=self.proposal_expires_at-timedelta(seconds=900), expires_at=self.proposal_expires_at)

    def recomputed_hash(self) -> str:
        return command_hash(self.proposal_command())

    def validate_canonical_hash(self) -> None:
        if self.recomputed_hash() != self.canonical_args_hash:
            raise ValueError("Canonical grant command hash mismatch")

    @classmethod
    def from_proposal(cls, proposal: ProposalCommand | dict, *, action_id: str, proposal_id: str,
                      authorization_id: str, authorized_at: datetime):
        value = command_value(proposal)
        return cls(action_id=action_id, command_schema_version=value.command_schema_version,
            proposal_id=proposal_id, tenant_id=value.tenant_id, customer_id=value.customer_id,
            subscription_id=value.subscription_id, seats=value.seats, days=value.days,
            expected_business_version=value.business_version, policy_version=value.policy_version,
            proposal_expires_at=value.expires_at, canonical_args_hash=command_hash(value),
            authorization_id=authorization_id, authorized_at=authorized_at)


class InternalGrantReceipt(GrantReceipt):
    proposal_id: Identifier
    authorization_id: Identifier
    authorized_at: AwareDatetime

    @field_validator("action_id", "tenant_id", "customer_id", "subscription_id", "policy_version",
                     "proposal_id", "authorization_id")
    @classmethod
    def db_text(cls, value):
        return _identifier(value)

    @model_validator(mode="after")
    def exact_business_effect(self):
        if self.after_version != self.before_version+1:
            raise ValueError("Receipt must advance the version exactly once")
        if self.temporary_expires_at != self.committed_business_at+timedelta(seconds=self.days*86400):
            raise ValueError("Receipt expiry must bind to its original business commit reference")
        return self

    def validate_binding(self, command: GrantCommand) -> None:
        command.validate_canonical_hash()
        for name in ("action_id", "tenant_id", "customer_id", "subscription_id", "canonical_args_hash",
                     "seats", "days", "policy_version"):
            if getattr(self, name) != getattr(command, name):
                raise ValueError("Receipt business command binding mismatch")
        if self.before_version != command.expected_business_version:
            raise ValueError("Receipt original business version mismatch")


class ReceiptQuery(StrictModel):
    customer_id: Identifier
    subscription_id: Identifier
    canonical_args_hash: Hash

    @field_validator("customer_id", "subscription_id")
    @classmethod
    def db_text(cls, value):
        return _identifier(value)


class GrantCommandEnvelope(StrictModel):
    ok: bool
    data: InternalGrantReceipt | None
    error_code: str | None
    retryable: bool
    source: Literal["sandbox.temporary_grants", "sandbox.action_receipts"]
    observed_at: AwareDatetime
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema(cls, value):
        if type(value) is not int:
            raise ValueError("Expected a strict integer schema version")
        return value

    @model_validator(mode="after")
    def consistent_outcome(self):
        if self.ok:
            if self.data is None or self.error_code is not None or self.retryable:
                raise ValueError("Success must contain exactly one committed receipt")
        elif self.data is not None or not self.error_code or not self.error_code.strip():
            raise ValueError("Failure must contain an error and no receipt")
        if not self.ok and self.source == "sandbox.temporary_grants" and self.retryable:
            raise ValueError("Grant POST errors must not request automatic retries")
        return self
