"""Durable generation claims, dispatch scheduling and non-resetting call budgets."""
from alembic import op


revision = "sf_app_0005"
down_revision = "sf_app_0004"


def upgrade():
    op.execute("""
        ALTER TABLE public.runs
            ADD COLUMN claimed_generation integer,
            ADD COLUMN claimed_at timestamptz,
            ADD COLUMN recovery_count integer NOT NULL DEFAULT 0 CHECK (recovery_count BETWEEN 0 AND 2),
            ADD COLUMN reason_code text,
            ADD COLUMN heartbeat_at timestamptz,
            ADD COLUMN result jsonb CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
            ADD COLUMN model_attempts integer NOT NULL DEFAULT 0 CHECK (model_attempts BETWEEN 0 AND 8),
            ADD COLUMN tool_rounds integer NOT NULL DEFAULT 0 CHECK (tool_rounds BETWEEN 0 AND 6),
            ADD CONSTRAINT runs_claim_pair CHECK (
                (claimed_generation IS NULL AND claimed_at IS NULL)
                OR (claimed_generation IS NOT NULL AND claimed_at IS NOT NULL
                    AND claimed_generation BETWEEN 1 AND execution_generation)
            )
    """)
    op.execute("""
        ALTER TABLE public.outbox
            ADD COLUMN available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            ADD COLUMN last_attempt_at timestamptz,
            ADD COLUMN dispatch_version bigint NOT NULL DEFAULT 0 CHECK (dispatch_version >= 0)
    """)
    op.execute("""
        CREATE INDEX outbox_dispatch_due ON public.outbox (available_at, created_at)
        WHERE status = 'pending'
    """)
    op.execute("""
        ALTER TABLE public.run_events ADD COLUMN execution_generation integer
            NOT NULL DEFAULT 1 CHECK (execution_generation > 0)
    """)
    op.execute("""
        CREATE TABLE public.run_model_attempts (
            run_id text NOT NULL REFERENCES public.runs,
            attempt integer NOT NULL CHECK (attempt BETWEEN 1 AND 8),
            execution_generation integer NOT NULL CHECK (execution_generation > 0),
            started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            ended_at timestamptz,
            status text NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'completed', 'error', 'unknown')),
            provider_response_id text,
            usage jsonb CHECK (usage IS NULL OR jsonb_typeof(usage) = 'object'),
            error_code text,
            PRIMARY KEY (run_id, attempt)
        )
    """)
