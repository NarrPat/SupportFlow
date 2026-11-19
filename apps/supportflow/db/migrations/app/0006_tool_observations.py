"""Append-only bounded read-tool observations, separate from graph checkpoints."""
from alembic import op


revision = "sf_app_0006"
down_revision = "sf_app_0005"


def upgrade():
    # Readers must be able to encounter a newer producer version and recover
    # safely. Current writers still emit only version 1; raw future payloads are
    # never projected to the browser by the event adapter.
    op.execute("ALTER TABLE public.run_events DROP CONSTRAINT run_events_schema_version_check")
    op.execute("ALTER TABLE public.run_events ADD CONSTRAINT run_events_schema_version_check CHECK(schema_version > 0)")
    op.execute("""
        CREATE TABLE public.run_tool_observations (
            run_id text NOT NULL REFERENCES public.runs,
            execution_generation integer NOT NULL CHECK (execution_generation > 0),
            ordinal integer NOT NULL CHECK (ordinal BETWEEN 1 AND 6),
            tool_id text NOT NULL CHECK (tool_id IN ('get_customer','get_subscription','get_invitation','get_service_status')),
            envelope jsonb NOT NULL CHECK (jsonb_typeof(envelope)='object'),
            observed_at timestamptz NOT NULL,
            raw_bytes integer NOT NULL CHECK (raw_bytes BETWEEN 0 AND 65536),
            projection_bytes integer NOT NULL CHECK (projection_bytes BETWEEN 0 AND 6000),
            duration_ms double precision NOT NULL CHECK (duration_ms >= 0 AND duration_ms < 'Infinity'::float8),
            PRIMARY KEY (run_id,execution_generation,ordinal)
        )
    """)
    op.execute("REVOKE ALL ON public.run_tool_observations FROM sf_app_runtime")
    op.execute("GRANT SELECT,INSERT ON public.run_tool_observations TO sf_app_runtime")
