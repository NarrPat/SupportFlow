"""Explicit invitation policy, invitation records and service status; no write action."""
from alembic import op


revision = "sf_sandbox_0002"
down_revision = "sf_sandbox_0001"


def upgrade():
    op.execute("ALTER TABLE public.subscriptions ADD CONSTRAINT subscriptions_customer_unique UNIQUE(tenant_id,customer_id)")
    op.execute("""
        CREATE TABLE public.invitation_policies (
            tenant_id text NOT NULL, customer_id text NOT NULL,
            allowed_domains jsonb CHECK (allowed_domains IS NULL OR
                (jsonb_typeof(allowed_domains)='array' AND jsonb_array_length(allowed_domains)<=50)),
            allow_subdomains boolean, invitation_permission boolean,
            PRIMARY KEY(tenant_id,customer_id)
        )
    """)
    op.execute("""
        CREATE TABLE public.invitations (
            tenant_id text NOT NULL, invitation_id text NOT NULL, customer_id text NOT NULL,
            email varchar(254) NOT NULL, email_domain varchar(253) NOT NULL,
            created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
            status varchar(64) NOT NULL, reason_code varchar(128),
            PRIMARY KEY(tenant_id,invitation_id), UNIQUE(tenant_id,customer_id,email),
            CHECK(expires_at>created_at)
        )
    """)
    op.execute("""
        CREATE TABLE public.service_statuses (
            tenant_id text NOT NULL, customer_id text NOT NULL,
            component text NOT NULL CHECK(component='invitations'),
            status varchar(64) NOT NULL, updated_at timestamptz NOT NULL,
            PRIMARY KEY(tenant_id,customer_id,component)
        )
    """)
