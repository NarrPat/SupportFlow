"""Accept runs, initial events and dispatch intent in one bounded transaction."""
from alembic import op


revision = "sf_app_0004"
down_revision = "sf_app_0003"


def upgrade():
    op.execute("""
        CREATE TABLE public.runtime_limits (
            app text PRIMARY KEY CHECK (app = 'supportflow'),
            logical_queued integer NOT NULL DEFAULT 0 CHECK (logical_queued BETWEEN 0 AND 10),
            active_reserved integer NOT NULL DEFAULT 0 CHECK (active_reserved BETWEEN 0 AND 2)
        )
    """)
    op.execute("INSERT INTO public.runtime_limits (app) VALUES ('supportflow')")
    op.execute("ALTER TABLE public.threads ADD UNIQUE (tenant_id, thread_id, actor_id)")
    op.execute("""
        CREATE TABLE public.runs (
            run_id text PRIMARY KEY,
            app text NOT NULL DEFAULT 'supportflow' REFERENCES public.runtime_limits,
            tenant_id text NOT NULL,
            actor_id text NOT NULL,
            thread_id text NOT NULL,
            message text NOT NULL CHECK (length(message) BETWEEN 1 AND 8000),
            request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
            status text NOT NULL CHECK (status IN (
                'queued', 'running', 'awaiting_input', 'awaiting_approval', 'resume_queued',
                'succeeded', 'handoff', 'rejected', 'expired', 'failed', 'cancelled', 'needs_reconciliation'
            )),
            version integer NOT NULL DEFAULT 1 CHECK (version > 0),
            execution_generation integer NOT NULL DEFAULT 1 CHECK (execution_generation > 0),
            admission_state text NOT NULL CHECK (admission_state IN ('none', 'queued', 'active')),
            config_snapshot jsonb NOT NULL CHECK (jsonb_typeof(config_snapshot) = 'object'),
            cancel_requested boolean NOT NULL DEFAULT false,
            api_received_at timestamptz NOT NULL,
            budget_origin_at timestamptz NOT NULL,
            budget_ms integer NOT NULL CHECK (budget_ms > 0),
            waiting_since timestamptz,
            wait_expires_at timestamptz,
            accumulated_wait_ms bigint NOT NULL DEFAULT 0 CHECK (accumulated_wait_ms >= 0),
            last_event_seq bigint NOT NULL DEFAULT 1 CHECK (last_event_seq > 0),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (tenant_id, run_id),
            UNIQUE (tenant_id, actor_id, run_id),
            FOREIGN KEY (tenant_id, thread_id, actor_id) REFERENCES public.threads (tenant_id, thread_id, actor_id),
            CHECK (admission_state = CASE
                WHEN status IN ('queued', 'resume_queued') THEN 'queued'
                WHEN status = 'running' THEN 'active'
                ELSE 'none'
            END)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX runs_one_unfinished_thread ON public.runs (tenant_id, thread_id)
        WHERE status NOT IN ('succeeded', 'handoff', 'rejected', 'expired', 'failed', 'cancelled')
    """)
    op.execute("CREATE INDEX runs_by_admission ON public.runs (app, admission_state)")
    op.execute("""
        CREATE INDEX runs_by_thread ON public.runs (tenant_id, thread_id, created_at DESC, run_id DESC)
    """)
    op.execute("""
        CREATE TABLE public.request_idempotency (
            app text NOT NULL DEFAULT 'supportflow' CHECK (app = 'supportflow'),
            tenant_id text NOT NULL,
            actor_id text NOT NULL,
            operation text NOT NULL CHECK (operation = 'create_run'),
            client_request_id text NOT NULL CHECK (
                length(client_request_id) BETWEEN 1 AND 128
                AND client_request_id ~ '^[ -~]+$' AND length(btrim(client_request_id)) > 0
            ),
            request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
            run_id text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (app, tenant_id, actor_id, operation, client_request_id),
            FOREIGN KEY (tenant_id, actor_id, run_id) REFERENCES public.runs (tenant_id, actor_id, run_id)
        )
    """)
    op.execute("""
        CREATE TABLE public.outbox (
            outbox_id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
            app text NOT NULL DEFAULT 'supportflow' CHECK (app = 'supportflow'),
            tenant_id text NOT NULL,
            run_id text NOT NULL,
            execution_generation integer NOT NULL CHECK (execution_generation > 0),
            kind text NOT NULL CHECK (kind IN ('create', 'resume', 'recover_fragment')),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent')),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (app, run_id, execution_generation, kind),
            FOREIGN KEY (tenant_id, run_id) REFERENCES public.runs (tenant_id, run_id)
        )
    """)
    op.execute("""
        CREATE TABLE public.run_events (
            run_id text NOT NULL,
            tenant_id text NOT NULL,
            seq bigint NOT NULL CHECK (seq > 0),
            schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
            status_version integer NOT NULL CHECK (status_version > 0),
            type text NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
            PRIMARY KEY (run_id, seq),
            FOREIGN KEY (tenant_id, run_id) REFERENCES public.runs (tenant_id, run_id)
        )
    """)
    # Admission mutations lock runtime_limits first. Deferred validation then sees
    # the transaction's final ledger and catches omitted counter changes at commit.
    op.execute("""
        CREATE FUNCTION public.verify_admission_totals() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE matches boolean;
        BEGIN
            SELECT l.logical_queued = totals.queued AND l.active_reserved = totals.active INTO matches
            FROM public.runtime_limits l CROSS JOIN (
                SELECT count(*) FILTER (WHERE admission_state = 'queued') AS queued,
                       count(*) FILTER (WHERE admission_state = 'active') AS active
                FROM public.runs WHERE app = 'supportflow'
            ) totals WHERE l.app = 'supportflow';
            IF matches IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'Run admission ledger is inconsistent' USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END
        $$
    """)
    for table in ("runtime_limits", "runs"):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER {table}_admission_consistency
            AFTER INSERT OR UPDATE OR DELETE ON public.{table}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION public.verify_admission_totals()
        """)
