"""Persist only the opaque session digest and the login proof's replay marker."""
from alembic import op


revision = "sf_app_0002"
down_revision = "sf_app_0001"


def upgrade():
    # Create once per application database. All API workers must sign with the same key.
    op.execute("""
        CREATE TABLE public.auth_key (
            singleton boolean PRIMARY KEY CHECK (singleton),
            secret bytea NOT NULL CHECK (octet_length(secret) = 32)
        )
    """)
    op.execute("""
        INSERT INTO public.auth_key VALUES (
            true, uuid_send(gen_random_uuid()) || uuid_send(gen_random_uuid())
        )
    """)
    op.execute("REVOKE ALL ON public.auth_key FROM sf_app_runtime")
    op.execute("GRANT SELECT ON public.auth_key TO sf_app_runtime")
    op.execute("""
        CREATE TABLE public.sessions (
            token_hash text PRIMARY KEY CHECK (token_hash ~ '^[a-f0-9]{64}$'),
            login_proof_hash text NOT NULL UNIQUE CHECK (login_proof_hash ~ '^[a-f0-9]{64}$'),
            app text NOT NULL CHECK (app = 'supportflow'),
            user_id text NOT NULL REFERENCES public.users,
            tenant_id text NOT NULL REFERENCES public.tenants,
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
            revoked_at timestamptz
        )
    """)
    op.execute("REVOKE UPDATE, DELETE ON public.sessions FROM sf_app_runtime")
    op.execute("GRANT UPDATE (revoked_at) ON public.sessions TO sf_app_runtime")
    # Keep the consumed login proof and audit row even if membership is later restored.
    op.execute("""
        CREATE FUNCTION public.revoke_membership_sessions() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE public.sessions SET revoked_at = clock_timestamp()
            WHERE tenant_id = OLD.tenant_id AND user_id = OLD.user_id AND revoked_at IS NULL;
            RETURN OLD;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER revoke_deleted_membership AFTER DELETE ON public.memberships
        FOR EACH ROW EXECUTE FUNCTION public.revoke_membership_sessions()
    """)
