"""Version 1 input for the local demo/evaluation seed command."""
import json
from pathlib import Path
from typing import Annotated, Literal

from argon2 import PasswordHasher
from argon2.low_level import Type
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection
from psycopg.types.json import Jsonb

from apps.supportflow.domain.contracts import Invitation as InvitationContract
from apps.supportflow.domain.contracts import InvitationPolicy as PolicyContract
from apps.supportflow.domain.contracts import ServiceStatus as StatusContract


Name = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0, le=2**31 - 1)]


class Row(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class Tenant(Row):
    tenant_id: Name


class User(Row):
    user_id: Name
    username: Annotated[str, Field(min_length=1, max_length=128)]


class Membership(Row):
    tenant_id: Name
    user_id: Name
    role: Literal["support", "supervisor", "admin"]


class Customer(Row):
    tenant_id: Name
    customer_id: Name
    customer_code: Name
    display_name: Name


class CustomerScope(Row):
    tenant_id: Name
    user_id: Name
    customer_id: Name


class Subscription(Row):
    tenant_id: Name
    customer_id: Name
    subscription_id: Name
    base_seats: Count
    used_seats: Count
    temporary_seats: Count
    temporary_expires_at: AwareDatetime | None
    plan_status: Name
    business_version: Annotated[int, Field(ge=1, le=2**63 - 1)]


class InvitationPolicy(PolicyContract):
    tenant_id: Name
    customer_id: Name


class Invitation(InvitationContract):
    tenant_id: Name
    customer_id: Name


class ServiceStatus(StatusContract):
    tenant_id: Name
    customer_id: Name


def unique_keys(rows, *fields):
    keys = {tuple(getattr(row, name) for name in fields) for row in rows}
    if len(keys) != len(rows):
        raise ValueError(f"Duplicate seed key: {', '.join(fields)}")
    return keys


class SeedData(Row):
    fixture_id: Name
    schema_version: Literal[1]
    tenants: list[Tenant] = Field(min_length=1)
    users: list[User] = Field(min_length=1)
    memberships: list[Membership] = Field(min_length=1)
    customers: list[Customer] = Field(min_length=1)
    customer_scopes: list[CustomerScope]
    subscriptions: list[Subscription] = Field(min_length=1)
    invitation_policies: list[InvitationPolicy] = Field(default_factory=list)
    invitations: list[Invitation] = Field(default_factory=list)
    service_statuses: list[ServiceStatus] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_keys(self):
        # Cross-database references cannot have a PostgreSQL foreign key.
        tenants = unique_keys(self.tenants, "tenant_id")
        users = unique_keys(self.users, "user_id")
        memberships = unique_keys(self.memberships, "tenant_id", "user_id")
        customers = unique_keys(self.customers, "tenant_id", "customer_id")
        unique_keys(self.users, "username")
        unique_keys(self.customers, "tenant_id", "customer_code")
        unique_keys(self.customer_scopes, "tenant_id", "user_id", "customer_id")
        unique_keys(self.subscriptions, "tenant_id", "subscription_id")
        unique_keys(self.subscriptions, "tenant_id", "customer_id")
        unique_keys(self.invitation_policies, "tenant_id", "customer_id")
        unique_keys(self.invitations, "tenant_id", "invitation_id")
        unique_keys(self.invitations, "tenant_id", "customer_id", "email")
        unique_keys(self.service_statuses, "tenant_id", "customer_id", "component")
        for row in self.memberships:
            if (row.tenant_id,) not in tenants or (row.user_id,) not in users:
                raise ValueError("Membership refers to an unknown tenant or user")
        for row in self.customers:
            if (row.tenant_id,) not in tenants:
                raise ValueError("Customer refers to an unknown tenant")
        for row in self.customer_scopes:
            if (row.tenant_id, row.user_id) not in memberships or (row.tenant_id, row.customer_id) not in customers:
                raise ValueError("Customer scope refers outside its tenant or membership")
        for row in self.subscriptions:
            if (row.tenant_id, row.customer_id) not in customers:
                raise ValueError("Subscription refers outside its tenant")
            if row.temporary_seats and row.temporary_expires_at is None:
                raise ValueError("Temporary seats require an expiry")
        for row in [*self.invitation_policies, *self.invitations, *self.service_statuses]:
            if (row.tenant_id, row.customer_id) not in customers:
                raise ValueError("Diagnosis seed refers outside its tenant or customer")
        return self


def load_password_hashes(users: list[User], path: Path) -> list[dict]:
    credentials = json.loads(path.read_bytes())
    if not isinstance(credentials, dict) or set(credentials) != {u.username for u in users}:
        raise ValueError("Seed password file must contain exactly the local seed accounts")
    hasher = PasswordHasher(type=Type.ID)
    result = []
    for user in users:
        password = credentials[user.username]
        if not isinstance(password, str) or len(password) < 16 or len(password.encode("utf-8")) > 1024:
            raise ValueError("Seed passwords need at least 16 characters and at most 1024 UTF-8 bytes")
        result.append({**user.model_dump(), "password_hash": hasher.hash(password)})
    return result


def seed_app(conn: Connection, data: SeedData, password_file: Path):
    users = load_password_hashes(data.users, password_file)
    conn.execute(text("INSERT INTO public.tenants (tenant_id) VALUES (:tenant_id)"),
                 [row.model_dump() for row in data.tenants])
    conn.execute(text("""
        INSERT INTO public.users (user_id, username, password_hash)
        VALUES (:user_id, :username, :password_hash)
    """), users)
    conn.execute(text("""
        INSERT INTO public.memberships (tenant_id, user_id, role)
        VALUES (:tenant_id, :user_id, :role)
    """), [row.model_dump() for row in data.memberships])
    conn.execute(text("""
        INSERT INTO public.customers (tenant_id, customer_id, customer_code, display_name)
        VALUES (:tenant_id, :customer_id, :customer_code, :display_name)
    """), [row.model_dump() for row in data.customers])
    if data.customer_scopes:
        conn.execute(text("""
            INSERT INTO public.customer_scopes (tenant_id, user_id, customer_id)
            VALUES (:tenant_id, :user_id, :customer_id)
        """), [row.model_dump() for row in data.customer_scopes])


def seed_sandbox(conn: Connection, data: SeedData):
    conn.execute(text("""
        INSERT INTO public.subscriptions (
            tenant_id, customer_id, subscription_id, base_seats, used_seats,
            temporary_seats, temporary_expires_at, plan_status, business_version
        ) VALUES (
            :tenant_id, :customer_id, :subscription_id, :base_seats, :used_seats,
            :temporary_seats, :temporary_expires_at, :plan_status, :business_version
        )
    """), [row.model_dump() for row in data.subscriptions])
    seed_diagnosis(conn, data.invitation_policies, data.invitations, data.service_statuses)


def seed_diagnosis(conn: Connection, invitation_policies, invitations, service_statuses):
    if invitation_policies:
        rows = [{**row.model_dump(), "allowed_domains": Jsonb(row.allowed_domains) if row.allowed_domains is not None else None}
                for row in invitation_policies]
        conn.execute(text("""INSERT INTO public.invitation_policies
            (tenant_id,customer_id,allowed_domains,allow_subdomains,invitation_permission)
            VALUES(:tenant_id,:customer_id,:allowed_domains,:allow_subdomains,:invitation_permission)"""), rows)
    if invitations:
        conn.execute(text("""INSERT INTO public.invitations
            (tenant_id,customer_id,invitation_id,email,email_domain,created_at,expires_at,status,reason_code)
            VALUES(:tenant_id,:customer_id,:invitation_id,:email,:email_domain,:created_at,:expires_at,:status,:reason_code)"""),
            [row.model_dump() for row in invitations])
    if service_statuses:
        conn.execute(text("""INSERT INTO public.service_statuses(tenant_id,customer_id,component,status,updated_at)
            VALUES(:tenant_id,:customer_id,:component,:status,:updated_at)"""), [row.model_dump() for row in service_statuses])
