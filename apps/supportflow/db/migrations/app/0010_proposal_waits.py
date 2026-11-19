"""Stable pending approval/action references; no decision or grant executor."""
from alembic import op

revision = "sf_app_0010"
down_revision = "sf_app_0009"


def upgrade():
    op.execute("""
        ALTER TABLE request_idempotency DROP CONSTRAINT request_idempotency_operation_check;
        ALTER TABLE request_idempotency DROP CONSTRAINT request_idempotency_result_kind;
        ALTER TABLE request_idempotency ADD CONSTRAINT request_idempotency_operation_check
            CHECK(operation IN ('create_run','submit_clarification','cancel_run'));
        ALTER TABLE request_idempotency ADD CONSTRAINT request_idempotency_result_kind CHECK(
            (operation IN ('create_run','cancel_run') AND input_id IS NULL AND accepted_generation IS NULL)
            OR (operation='submit_clarification' AND input_id IS NOT NULL
                AND accepted_generation IS NOT NULL AND accepted_generation>1));
        ALTER TABLE proposals ADD UNIQUE(tenant_id,run_id,proposal_id);
        ALTER TABLE proposals ADD UNIQUE(proposal_id,action_id,canonical_args_hash);
        CREATE TABLE actions (
            action_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            run_id text NOT NULL,
            proposal_id text NOT NULL UNIQUE,
            canonical_args_hash text NOT NULL CHECK(canonical_args_hash ~ '^[a-f0-9]{64}$'),
            status text NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','expired','cancelled')),
            FOREIGN KEY(tenant_id,run_id,proposal_id) REFERENCES proposals(tenant_id,run_id,proposal_id),
            FOREIGN KEY(proposal_id,action_id,canonical_args_hash) REFERENCES proposals(proposal_id,action_id,canonical_args_hash)
        );
        CREATE TABLE approvals (
            approval_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            run_id text NOT NULL,
            proposal_id text NOT NULL UNIQUE,
            status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','expired','cancelled')),
            version integer NOT NULL DEFAULT 1 CHECK(version>0),
            decision text CHECK(decision IS NULL),
            FOREIGN KEY(tenant_id,run_id,proposal_id) REFERENCES proposals(tenant_id,run_id,proposal_id)
        );
        CREATE TABLE proposal_waits (
            proposal_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            run_id text NOT NULL,
            checkpoint_id text NOT NULL,
            checkpoint_ns text NOT NULL CHECK(checkpoint_ns=''),
            checkpoint_at timestamptz NOT NULL,
            checkpoint_generation integer NOT NULL CHECK(checkpoint_generation>0),
            wait_started_at timestamptz NOT NULL,
            wait_ended_at timestamptz CHECK(wait_ended_at IS NULL OR wait_ended_at>=wait_started_at),
            status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','expired','cancelled')),
            FOREIGN KEY(tenant_id,run_id,proposal_id) REFERENCES proposals(tenant_id,run_id,proposal_id),
            CHECK((status='pending')=(wait_ended_at IS NULL))
        );
        CREATE UNIQUE INDEX proposal_wait_one_pending_run ON proposal_waits(run_id) WHERE status='pending';
        INSERT INTO actions(action_id,tenant_id,run_id,proposal_id,canonical_args_hash)
            SELECT action_id,tenant_id,run_id,proposal_id,canonical_args_hash FROM proposals;
        INSERT INTO approvals(approval_id,tenant_id,run_id,proposal_id)
            SELECT md5('supportflow:approval:'||proposal_id)::uuid::text,tenant_id,run_id,proposal_id FROM proposals;
        REVOKE ALL ON actions,approvals,proposal_waits FROM sf_app_runtime;
        GRANT SELECT,INSERT ON actions,approvals TO sf_app_runtime;
        GRANT SELECT,INSERT,UPDATE ON proposal_waits TO sf_app_runtime;
        GRANT UPDATE(status) ON actions TO sf_app_runtime;
        GRANT UPDATE(status,version) ON approvals TO sf_app_runtime;
        CREATE FUNCTION protect_proposal_reference() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW)-ARRAY['status','version']) IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['status','version']) THEN
                RAISE EXCEPTION 'Proposal reference identity is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.status NOT IN ('pending','proposed') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Closed proposal reference is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER actions_binding_immutable BEFORE UPDATE ON actions
            FOR EACH ROW EXECUTE FUNCTION protect_proposal_reference();
        CREATE TRIGGER approvals_binding_immutable BEFORE UPDATE ON approvals
            FOR EACH ROW EXECUTE FUNCTION protect_proposal_reference();
        CREATE FUNCTION protect_proposal_wait() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW)-ARRAY['status','wait_ended_at']) IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['status','wait_ended_at']) OR
               (OLD.status<>'pending' AND NEW IS DISTINCT FROM OLD) THEN
                RAISE EXCEPTION 'Proposal wait identity is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER proposal_wait_identity BEFORE UPDATE ON proposal_waits
            FOR EACH ROW EXECUTE FUNCTION protect_proposal_wait();
    """)
