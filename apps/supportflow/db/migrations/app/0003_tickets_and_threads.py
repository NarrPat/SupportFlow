"""Tenant-scoped tickets and actor-private threads."""
from alembic import op


revision = "sf_app_0003"
down_revision = "sf_app_0002"


def upgrade():
    op.execute("""
        CREATE TABLE public.tickets (
            tenant_id text NOT NULL REFERENCES public.tenants,
            ticket_id text NOT NULL,
            title varchar(200) NOT NULL CHECK (length(btrim(title)) > 0),
            customer_id text,
            assignee_id text NOT NULL REFERENCES public.users(user_id),
            status text NOT NULL DEFAULT 'open',
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, ticket_id),
            FOREIGN KEY (tenant_id, customer_id) REFERENCES public.customers
        )
    """)
    op.execute("""
        CREATE TABLE public.threads (
            tenant_id text NOT NULL,
            thread_id text NOT NULL,
            ticket_id text NOT NULL,
            actor_id text NOT NULL REFERENCES public.users(user_id),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, thread_id),
            FOREIGN KEY (tenant_id, ticket_id) REFERENCES public.tickets
        )
    """)
    op.execute("""
        CREATE INDEX threads_by_ticket_actor
            ON public.threads (tenant_id, ticket_id, actor_id, created_at DESC, thread_id DESC)
    """)
    # The future clarification/worker path must honor the same one-way binding.
    op.execute("""
        CREATE FUNCTION public.reject_customer_rebind() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Ticket already has a confirmed customer' USING ERRCODE = '23514';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER tickets_confirm_customer_once BEFORE UPDATE OF customer_id ON public.tickets
        FOR EACH ROW WHEN (OLD.customer_id IS NOT NULL AND NEW.customer_id IS DISTINCT FROM OLD.customer_id)
        EXECUTE FUNCTION public.reject_customer_rebind()
    """)
