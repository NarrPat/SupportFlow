"""Insert the opt-in synthetic invitation facts without changing business rows."""
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection

from apps.supportflow.domain.contracts import Identifier, InvitationPolicy, StrictModel

from .seed_data import Invitation, InvitationPolicy as SeedPolicy, ServiceStatus, seed_diagnosis, unique_keys


class DemoSeedError(ValueError):
    """A safe summary for the database setup CLI."""


class DemoInvitation(StrictModel):
    invitation_id: Identifier
    email: Annotated[str, Field(min_length=3, max_length=254)]
    email_domain: Annotated[str, Field(min_length=1, max_length=253)]
    status: Literal["pending"]
    reason_code: None
    created_seconds_ago: Annotated[int, Field(ge=0, le=86_400)]
    lifetime_seconds: Annotated[int, Field(gt=0, le=2_592_000)]

    @model_validator(mode="after")
    def valid_at_seed(self):
        if self.created_seconds_ago >= self.lifetime_seconds:
            raise ValueError("The demo invitation must still be valid at seed time")
        return self


class DemoService(StrictModel):
    component: Literal["invitations"]
    status: Literal["operational"]
    updated_seconds_ago: Annotated[int, Field(ge=0, le=86_400)]


class DemoScenario(StrictModel):
    tenant_id: Identifier
    customer_id: Identifier
    subscription_id: Identifier
    invitation_policy: InvitationPolicy
    invitation: DemoInvitation
    service_status: DemoService


class DemoSeedData(StrictModel):
    fixture_id: Literal["SF-DEMO-INVITATIONS"]
    schema_version: Literal[1]
    scenarios: Annotated[list[DemoScenario], Field(min_length=1, max_length=8)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @model_validator(mode="after")
    def check_keys(self):
        unique_keys(self.scenarios, "tenant_id", "customer_id")
        unique_keys(self.scenarios, "tenant_id", "subscription_id")
        keys = {(row.tenant_id, row.invitation.invitation_id) for row in self.scenarios}
        if len(keys) != len(self.scenarios):
            raise ValueError("Duplicate demo invitation ID")
        return self


def materialize(data: DemoSeedData, at: datetime):
    policies, invitations, statuses = [], [], []
    for scenario in data.scenarios:
        scope = {"tenant_id": scenario.tenant_id, "customer_id": scenario.customer_id}
        spec = scenario.invitation
        created_at = at - timedelta(seconds=spec.created_seconds_ago)
        policies.append(SeedPolicy(**scope, **scenario.invitation_policy.model_dump()))
        invitations.append(Invitation(**scope, created_at=created_at,
            expires_at=created_at + timedelta(seconds=spec.lifetime_seconds),
            **spec.model_dump(exclude={"created_seconds_ago", "lifetime_seconds"})))
        service = scenario.service_status
        statuses.append(ServiceStatus(**scope, component=service.component, status=service.status,
                                      updated_at=at - timedelta(seconds=service.updated_seconds_ago)))
    return policies, invitations, statuses


def seed_demo_sandbox(conn: Connection, data: DemoSeedData):
    # The caller already holds the normal migration/seed lock. Bind all relative
    # times to this PostgreSQL transaction, never a test clock or host time.
    at = conn.execute(text("SELECT transaction_timestamp()")).scalar_one()
    rows = materialize(data, at)
    for scenario in data.scenarios:
        binding = scenario.model_dump(include={"tenant_id", "customer_id", "subscription_id"})
        subscription = conn.execute(text("""
            SELECT subscription_id FROM public.subscriptions
            WHERE tenant_id = :tenant_id AND customer_id = :customer_id
              AND subscription_id = :subscription_id FOR KEY SHARE
        """), binding).scalar_one_or_none()
        if subscription is None:
            raise DemoSeedError("Apply FX-BASE to the sandbox before seeding demo invitation data")
    # Ordinary INSERT deliberately lets any existing conflicting fact abort the
    # entire seed, including the seed_history marker. Existing rows stay intact.
    seed_diagnosis(conn, *rows)
