"""Application identities and tenant-scoped customer access."""
from alembic import op


revision = "sf_app_0001"
down_revision = None


def upgrade():
    op.execute("""
        CREATE TABLE public.tenants (
            tenant_id text PRIMARY KEY
        )
    """)
    op.execute("""
        CREATE TABLE public.users (
            user_id text PRIMARY KEY,
            username varchar(128) NOT NULL UNIQUE,
            password_hash text NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE public.memberships (
            tenant_id text NOT NULL REFERENCES public.tenants,
            user_id text NOT NULL REFERENCES public.users,
            role text NOT NULL CHECK (role IN ('support','supervisor','admin')),
            PRIMARY KEY (tenant_id, user_id)
        )
    """)
    op.execute("""
        CREATE TABLE public.customers (
            tenant_id text NOT NULL REFERENCES public.tenants,
            customer_id text NOT NULL,
            customer_code text NOT NULL,
            display_name text NOT NULL,
            PRIMARY KEY (tenant_id, customer_id),
            UNIQUE (tenant_id, customer_code)
        )
    """)
    op.execute("""
        CREATE TABLE public.customer_scopes (
            tenant_id text NOT NULL,
            user_id text NOT NULL,
            customer_id text NOT NULL,
            PRIMARY KEY (tenant_id, user_id, customer_id),
            FOREIGN KEY (tenant_id, user_id) REFERENCES public.memberships,
            FOREIGN KEY (tenant_id, customer_id) REFERENCES public.customers
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
    op.execute("REVOKE ALL ON public.alembic_version, public.seed_history FROM sf_app_runtime")
