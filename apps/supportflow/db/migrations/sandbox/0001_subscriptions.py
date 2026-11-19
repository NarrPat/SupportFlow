"""Subscriptions owned only by the business sandbox."""
from alembic import op


revision = "sf_sandbox_0001"
down_revision = None


def upgrade():
    op.execute("""
        CREATE TABLE public.subscriptions (
            tenant_id text NOT NULL,
            customer_id text NOT NULL,
            subscription_id text NOT NULL,
            base_seats integer NOT NULL CHECK (base_seats >= 0),
            used_seats integer NOT NULL CHECK (used_seats >= 0),
            temporary_seats integer NOT NULL CHECK (temporary_seats >= 0),
            temporary_expires_at timestamptz,
            plan_status text NOT NULL,
            business_version bigint NOT NULL CHECK (business_version > 0),
            PRIMARY KEY (tenant_id, subscription_id),
            CHECK (temporary_seats = 0 OR temporary_expires_at IS NOT NULL)
        )
    """)
    op.execute("""
        CREATE TABLE public.seed_history (
            fixture_id text NOT NULL,
            schema_version integer NOT NULL,
            fixture_sha256 text NOT NULL,
            PRIMARY KEY (fixture_id, schema_version)
        )
    """)
    op.execute("REVOKE ALL ON public.alembic_version, public.seed_history FROM sf_sandbox_runtime")
