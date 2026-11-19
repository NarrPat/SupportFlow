"""Immutable proposal storage only; approvals and business commands are separate."""
from alembic import op

revision = "sf_app_0009"
down_revision = "sf_app_0008"


def upgrade():
    op.execute("""
        CREATE TABLE public.proposals (
            proposal_id text PRIMARY KEY,
            action_id text NOT NULL UNIQUE,
            tenant_id text NOT NULL,
            run_id text NOT NULL,
            proposal_seq integer NOT NULL CHECK (proposal_seq > 0),
            canonical_args jsonb NOT NULL CHECK (jsonb_typeof(canonical_args)='object'),
            canonical_args_hash text NOT NULL CHECK (canonical_args_hash ~ '^[a-f0-9]{64}$'),
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL CHECK (expires_at=created_at+interval '900 seconds'),
            evidence_refs jsonb NOT NULL CHECK (jsonb_typeof(evidence_refs)='array' AND jsonb_array_length(evidence_refs) BETWEEN 1 AND 5),
            observation_refs jsonb NOT NULL CHECK (jsonb_typeof(observation_refs)='array' AND jsonb_array_length(observation_refs) BETWEEN 1 AND 6),
            UNIQUE (run_id,proposal_seq),
            UNIQUE (tenant_id,proposal_id),
            FOREIGN KEY (tenant_id,run_id) REFERENCES public.runs(tenant_id,run_id)
        )
    """)
    op.execute("""
        REVOKE ALL ON public.proposals FROM sf_app_runtime;
        GRANT SELECT,INSERT ON public.proposals TO sf_app_runtime;
        CREATE FUNCTION public.reject_proposal_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Proposals are immutable' USING ERRCODE='23514';
        END $$;
        CREATE TRIGGER proposals_immutable BEFORE UPDATE ON public.proposals
            FOR EACH ROW EXECUTE FUNCTION public.reject_proposal_update()
    """)
