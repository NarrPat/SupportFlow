"""One send intent, immutable receipts and effect-aware app finalization."""
from alembic import op

revision = 'sf_app_0012'
down_revision = 'sf_app_0011'


def upgrade():
    op.execute("""
        ALTER TABLE runs DROP CONSTRAINT runs_tool_rounds_check;
        ALTER TABLE runs ADD CONSTRAINT runs_tool_rounds_check CHECK(tool_rounds BETWEEN 0 AND 6
            OR (tool_rounds=7 AND config_snapshot @> '{"schema_version": 1,"contract_version": "dispatch-v1",
                "graph_version": "grant-approval-v1","grant_contract_version": 1,
                "approval_contract_version": 1,"tool_round_limit": 7}'::jsonb
                AND config_snapshot->>'schema_version'='1' AND config_snapshot->>'grant_contract_version'='1'
                AND config_snapshot->>'approval_contract_version'='1' AND config_snapshot->>'tool_round_limit'='7')
            OR (tool_rounds BETWEEN 7 AND 9 AND config_snapshot @> '{"schema_version": 1,"contract_version": "dispatch-v1",
                "graph_version": "grant-execution-v1","grant_contract_version": 1,"approval_contract_version": 1,
                "execution_contract_version": 1,"tool_round_limit": 9}'::jsonb
                AND config_snapshot->>'schema_version'='1' AND config_snapshot->>'grant_contract_version'='1'
                AND config_snapshot->>'approval_contract_version'='1' AND config_snapshot->>'execution_contract_version'='1'
                AND config_snapshot->>'tool_round_limit'='9'));
        ALTER TABLE run_tool_observations DROP CONSTRAINT run_tool_observations_ordinal_check;
        ALTER TABLE run_tool_observations ADD CONSTRAINT run_tool_observations_ordinal_check CHECK(ordinal BETWEEN 1 AND 9);
        CREATE OR REPLACE FUNCTION validate_approval_refresh_ordinal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.ordinal>6 AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id
                AND r.execution_generation=NEW.execution_generation AND r.tool_rounds>=NEW.ordinal
                AND NEW.tool_id='get_subscription' AND (
                    (NEW.ordinal=7 AND r.config_snapshot @> '{"schema_version": 1,"contract_version": "dispatch-v1",
                        "graph_version": "grant-approval-v1","grant_contract_version": 1,"approval_contract_version": 1,
                        "tool_round_limit": 7}'::jsonb AND r.config_snapshot->>'schema_version'='1'
                        AND r.config_snapshot->>'grant_contract_version'='1' AND r.config_snapshot->>'approval_contract_version'='1'
                        AND r.config_snapshot->>'tool_round_limit'='7')
                    OR (NEW.ordinal IN (7,9) AND r.config_snapshot @> '{"schema_version": 1,"contract_version": "dispatch-v1",
                        "graph_version": "grant-execution-v1","grant_contract_version": 1,"approval_contract_version": 1,
                        "execution_contract_version": 1,"tool_round_limit": 9}'::jsonb
                        AND r.config_snapshot->>'schema_version'='1' AND r.config_snapshot->>'grant_contract_version'='1'
                        AND r.config_snapshot->>'approval_contract_version'='1' AND r.config_snapshot->>'execution_contract_version'='1'
                        AND r.config_snapshot->>'tool_round_limit'='9'))) THEN
                RAISE EXCEPTION 'Observation requires its frozen graph budget' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        ALTER TABLE outbox DROP CONSTRAINT outbox_status_check;
        ALTER TABLE outbox ADD CONSTRAINT outbox_status_check CHECK(status IN ('pending','sent','cancelled'));
        ALTER TABLE dispatch_authorizations ADD UNIQUE(tenant_id,run_id,action_id,authorization_id,canonical_args_hash);
        CREATE TABLE action_dispatches (
            action_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            run_id text NOT NULL UNIQUE,
            authorization_id text NOT NULL,
            canonical_args_hash text NOT NULL CHECK(canonical_args_hash ~ '^[a-f0-9]{64}$'),
            execution_generation integer NOT NULL CHECK(execution_generation>1),
            command jsonb NOT NULL CHECK(jsonb_typeof(command)='object'),
            status text NOT NULL CHECK(status IN ('sending','unknown','committed','rejected')),
            transport_attempts integer NOT NULL DEFAULT 1 CHECK(transport_attempts=1),
            tool_ordinal integer NOT NULL DEFAULT 8 CHECK(tool_ordinal=8),
            intent_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            receipt jsonb CHECK(receipt IS NULL OR jsonb_typeof(receipt)='object'),
            receipt_recorded_at timestamptz,
            verification jsonb CHECK(verification IS NULL OR jsonb_typeof(verification)='object'),
            last_error_code text CHECK(last_error_code IS NULL OR last_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
            reconciliation_rounds integer NOT NULL DEFAULT 0 CHECK(reconciliation_rounds>=0),
            reconciliation_requests integer NOT NULL DEFAULT 0 CHECK(reconciliation_requests>=0),
            next_reconcile_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            finalized_at timestamptz,
            FOREIGN KEY(tenant_id,run_id,action_id,authorization_id,canonical_args_hash)
                REFERENCES dispatch_authorizations(tenant_id,run_id,action_id,authorization_id,canonical_args_hash),
            CHECK((status='committed')=(receipt IS NOT NULL)),
            CHECK((receipt IS NULL)=(receipt_recorded_at IS NULL))
        );
        CREATE FUNCTION protect_action_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW)-ARRAY['status','receipt','receipt_recorded_at','verification','last_error_code',
                'reconciliation_rounds','reconciliation_requests','next_reconcile_at','finalized_at']) IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['status','receipt','receipt_recorded_at','verification','last_error_code',
                'reconciliation_rounds','reconciliation_requests','next_reconcile_at','finalized_at'])
                OR (OLD.receipt IS NOT NULL AND (NEW.receipt IS DISTINCT FROM OLD.receipt
                    OR NEW.receipt_recorded_at IS DISTINCT FROM OLD.receipt_recorded_at))
                OR (OLD.status='committed' AND NEW.status<>'committed')
                OR (OLD.status='rejected' AND NEW.status<>'rejected')
                OR (OLD.status='unknown' AND NEW.status NOT IN ('unknown','committed'))
                OR NEW.reconciliation_rounds<OLD.reconciliation_rounds
                OR NEW.reconciliation_requests<OLD.reconciliation_requests
                OR (OLD.finalized_at IS NOT NULL AND NEW.finalized_at IS DISTINCT FROM OLD.finalized_at) THEN
                RAISE EXCEPTION 'Dispatch identity, receipt and terminal facts are immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER action_dispatch_binding BEFORE UPDATE ON action_dispatches
            FOR EACH ROW EXECUTE FUNCTION protect_action_dispatch();
        CREATE FUNCTION validate_dispatch_intent() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status<>'sending' OR NEW.receipt IS NOT NULL OR NOT EXISTS(
                SELECT 1 FROM runs r JOIN dispatch_authorizations a USING(tenant_id,run_id)
                WHERE r.tenant_id=NEW.tenant_id AND r.run_id=NEW.run_id AND a.authorization_id=NEW.authorization_id
                    AND a.action_id=NEW.action_id AND a.canonical_args_hash=NEW.canonical_args_hash
                    AND r.status='running' AND r.admission_state='active' AND r.tool_rounds=8
                    AND r.execution_generation=NEW.execution_generation AND r.claimed_generation=NEW.execution_generation
                    AND r.config_snapshot @> '{"schema_version": 1,"contract_version": "dispatch-v1",
                        "graph_version": "grant-execution-v1","grant_contract_version": 1,"approval_contract_version": 1,
                        "execution_contract_version": 1,"tool_round_limit": 9}'::jsonb
                    AND r.config_snapshot->>'schema_version'='1' AND r.config_snapshot->>'grant_contract_version'='1'
                    AND r.config_snapshot->>'approval_contract_version'='1' AND r.config_snapshot->>'execution_contract_version'='1'
                    AND r.config_snapshot->>'tool_round_limit'='9'
                    AND NEW.command->>'action_id'=a.action_id AND NEW.command->>'tenant_id'=a.tenant_id
                    AND NEW.command->>'proposal_id'=a.proposal_id AND NEW.command->>'authorization_id'=a.authorization_id
                    AND NEW.command->>'canonical_args_hash'=a.canonical_args_hash
                    AND (NEW.command->>'authorized_at')::timestamptz=a.authorized_at) THEN
                RAISE EXCEPTION 'Send intent requires the frozen execution contract and authorization' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER action_dispatch_intent BEFORE INSERT ON action_dispatches
            FOR EACH ROW EXECUTE FUNCTION validate_dispatch_intent();
        REVOKE ALL ON action_dispatches FROM sf_app_runtime;
        GRANT SELECT,INSERT,UPDATE ON action_dispatches TO sf_app_runtime;
        CREATE INDEX action_dispatches_reconcile ON action_dispatches(next_reconcile_at,run_id)
            WHERE status IN ('sending','unknown','committed') AND finalized_at IS NULL;
        ALTER TABLE tickets ADD COLUMN agent_resolution_code text,
            ADD COLUMN agent_action_id text,
            ADD COLUMN agent_effect_status text CHECK(agent_effect_status IS NULL OR agent_effect_status IN ('none','unknown','committed')),
            ADD COLUMN agent_resolved_at timestamptz;
        CREATE TABLE ticket_resolution_audits (
            run_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            ticket_id text NOT NULL,
            action_id text NOT NULL UNIQUE REFERENCES action_dispatches(action_id),
            status text NOT NULL CHECK(status IN ('succeeded','cancelled')),
            resolution_code text NOT NULL,
            result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY(tenant_id,run_id) REFERENCES runs(tenant_id,run_id),
            FOREIGN KEY(tenant_id,ticket_id) REFERENCES tickets(tenant_id,ticket_id)
        );
        CREATE TRIGGER ticket_resolution_audit_immutable BEFORE UPDATE ON ticket_resolution_audits
            FOR EACH ROW EXECUTE FUNCTION reject_approval_audit_update();
        REVOKE ALL ON ticket_resolution_audits FROM sf_app_runtime;
        GRANT SELECT,INSERT ON ticket_resolution_audits TO sf_app_runtime;
    """)
