"""Stable pending inputs and atomic, non-cached-on-rejection resume decisions."""
from alembic import op

revision = "sf_app_0008"
down_revision = "sf_app_0007"


def upgrade():
    op.execute("""
        ALTER TABLE runs ADD COLUMN confirmed_fields jsonb NOT NULL DEFAULT '{}'
            CHECK (jsonb_typeof(confirmed_fields)='object');
        CREATE TABLE pending_inputs (
            input_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            run_id text NOT NULL,
            input_seq integer NOT NULL CHECK(input_seq > 0),
            version integer NOT NULL DEFAULT 1 CHECK(version > 0),
            status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','answered','expired','cancelled')),
            required_fields text[] NOT NULL CHECK(cardinality(required_fields) BETWEEN 1 AND 4
                AND required_fields <@ ARRAY['customer_id','invitation_email','seats','days']::text[]
                AND array_position(required_fields,NULL) IS NULL),
            prompt text NOT NULL CHECK(length(prompt) BETWEEN 1 AND 1000 AND length(btrim(prompt)) > 0),
            created_generation integer NOT NULL CHECK(created_generation > 0),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            expires_at timestamptz NOT NULL,
            checkpoint_id text,
            checkpoint_ns text NOT NULL DEFAULT '',
            checkpoint_at timestamptz,
            checkpoint_generation integer,
            wait_started_at timestamptz,
            wait_ended_at timestamptz,
            answers jsonb CHECK(answers IS NULL OR jsonb_typeof(answers)='object'),
            answer_hash text CHECK(answer_hash IS NULL OR answer_hash ~ '^[a-f0-9]{64}$'),
            answered_at timestamptz,
            answered_by text REFERENCES users(user_id),
            resume_generation integer,
            UNIQUE(run_id,input_seq),
            UNIQUE(tenant_id,run_id,input_id),
            FOREIGN KEY(tenant_id,run_id) REFERENCES runs(tenant_id,run_id),
            CHECK(expires_at=created_at+interval '900 seconds'),
            CHECK((checkpoint_id IS NULL AND checkpoint_at IS NULL AND checkpoint_generation IS NULL
                   AND wait_started_at IS NULL)
               OR (checkpoint_id IS NOT NULL AND checkpoint_at IS NOT NULL
                   AND checkpoint_generation IS NOT NULL AND checkpoint_generation>=created_generation
                   AND wait_started_at IS NOT NULL)),
            CHECK(wait_ended_at IS NULL OR (wait_started_at IS NOT NULL AND wait_ended_at>=wait_started_at)),
            CHECK((status='answered' AND answers IS NOT NULL AND answer_hash IS NOT NULL
                   AND answered_at IS NOT NULL AND answered_by IS NOT NULL
                   AND resume_generation IS NOT NULL AND checkpoint_generation IS NOT NULL
                   AND resume_generation>checkpoint_generation AND wait_ended_at IS NOT NULL)
               OR (status<>'answered' AND answers IS NULL AND answer_hash IS NULL
                   AND answered_at IS NULL AND answered_by IS NULL AND resume_generation IS NULL))
        );
        CREATE UNIQUE INDEX inputs_one_pending_run ON pending_inputs(run_id) WHERE status='pending';
        CREATE INDEX inputs_wait_expiry ON pending_inputs(expires_at,run_id) WHERE status='pending';
        ALTER TABLE request_idempotency DROP CONSTRAINT request_idempotency_operation_check;
        ALTER TABLE request_idempotency
            ADD COLUMN input_id text,
            ADD COLUMN accepted_generation integer,
            ADD CONSTRAINT request_idempotency_operation_check CHECK(operation IN ('create_run','submit_clarification')),
            ADD CONSTRAINT request_idempotency_input_fk FOREIGN KEY(tenant_id,run_id,input_id)
                REFERENCES pending_inputs(tenant_id,run_id,input_id),
            ADD CONSTRAINT request_idempotency_result_kind CHECK(
                (operation='create_run' AND input_id IS NULL AND accepted_generation IS NULL)
                OR (operation='submit_clarification' AND input_id IS NOT NULL
                    AND accepted_generation IS NOT NULL AND accepted_generation>1));
    """)
    op.execute("""
        CREATE FUNCTION protect_pending_input() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.input_id,NEW.tenant_id,NEW.run_id,NEW.input_seq,NEW.required_fields,NEW.prompt,
                NEW.created_generation,NEW.created_at,NEW.expires_at) IS DISTINCT FROM
               (OLD.input_id,OLD.tenant_id,OLD.run_id,OLD.input_seq,OLD.required_fields,OLD.prompt,
                OLD.created_generation,OLD.created_at,OLD.expires_at) THEN
                RAISE EXCEPTION 'Pending input identity is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.status<>'pending' AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Closed input is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.checkpoint_id IS NOT NULL AND
               (NEW.checkpoint_id,NEW.checkpoint_ns,NEW.checkpoint_at,NEW.checkpoint_generation,NEW.wait_started_at)
               IS DISTINCT FROM
               (OLD.checkpoint_id,OLD.checkpoint_ns,OLD.checkpoint_at,OLD.checkpoint_generation,OLD.wait_started_at) THEN
                RAISE EXCEPTION 'Projected wait is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER pending_input_immutable BEFORE UPDATE ON pending_inputs
            FOR EACH ROW EXECUTE FUNCTION protect_pending_input();
        REVOKE ALL ON pending_inputs FROM sf_app_runtime;
        GRANT SELECT,INSERT,UPDATE ON pending_inputs TO sf_app_runtime;
    """)
