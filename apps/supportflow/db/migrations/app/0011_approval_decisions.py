"""Atomic approval decisions and immutable pre-dispatch authorization audit; no grant."""
from alembic import op

revision='sf_app_0011'
down_revision='sf_app_0010'


def upgrade():
    op.execute("""
        ALTER TABLE runs DROP CONSTRAINT runs_tool_rounds_check;
        ALTER TABLE runs ADD CONSTRAINT runs_tool_rounds_check CHECK(tool_rounds BETWEEN 0 AND 6
            OR (tool_rounds=7 AND config_snapshot @> '{"schema_version": 1,"contract_version": "dispatch-v1",
                "graph_version": "grant-approval-v1","grant_contract_version": 1,
                "approval_contract_version": 1,"tool_round_limit": 7}'::jsonb
                AND config_snapshot->>'schema_version'='1' AND config_snapshot->>'grant_contract_version'='1'
                AND config_snapshot->>'approval_contract_version'='1' AND config_snapshot->>'tool_round_limit'='7'));
        ALTER TABLE run_tool_observations DROP CONSTRAINT run_tool_observations_ordinal_check;
        ALTER TABLE run_tool_observations ADD CONSTRAINT run_tool_observations_ordinal_check CHECK(ordinal BETWEEN 1 AND 7);
        CREATE FUNCTION validate_approval_refresh_ordinal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.ordinal=7 AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id
                AND r.execution_generation=NEW.execution_generation AND r.tool_rounds=7
                AND r.config_snapshot @> '{"schema_version": 1,"contract_version": "dispatch-v1",
                    "graph_version": "grant-approval-v1","grant_contract_version": 1,
                    "approval_contract_version": 1,"tool_round_limit": 7}'::jsonb
                AND r.config_snapshot->>'schema_version'='1' AND r.config_snapshot->>'grant_contract_version'='1'
                AND r.config_snapshot->>'approval_contract_version'='1' AND r.config_snapshot->>'tool_round_limit'='7') THEN
                RAISE EXCEPTION 'Seventh observation requires the frozen approval contract' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER approval_refresh_budget BEFORE INSERT ON run_tool_observations
            FOR EACH ROW EXECUTE FUNCTION validate_approval_refresh_ordinal();
        ALTER TABLE approvals DROP CONSTRAINT approvals_status_check;
        ALTER TABLE approvals DROP CONSTRAINT approvals_decision_check;
        ALTER TABLE approvals ADD COLUMN decided_by text REFERENCES users(user_id),
            ADD COLUMN decided_at timestamptz,
            ADD COLUMN reason text,
            ADD COLUMN resume_generation integer;
        ALTER TABLE approvals ADD CONSTRAINT approvals_status_check
            CHECK(status IN ('pending','approved','rejected','expired','cancelled'));
        ALTER TABLE approvals ADD CONSTRAINT approvals_decision_metadata CHECK(
            (status IN ('approved','rejected') AND decision IS NOT NULL
                AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND reason IS NOT NULL
                AND length(reason)<=1000 AND
                ((status='approved' AND decision='approve' AND resume_generation IS NOT NULL AND resume_generation>1)
                 OR (status='rejected' AND decision='reject' AND resume_generation IS NULL)))
            OR (status IN ('pending','expired','cancelled') AND decision IS NULL
                AND decided_by IS NULL AND decided_at IS NULL AND reason IS NULL AND resume_generation IS NULL));
        ALTER TABLE approvals ADD UNIQUE(tenant_id,run_id,approval_id),
            ADD UNIQUE(tenant_id,run_id,proposal_id,approval_id), ADD UNIQUE(approval_id,decided_by);
        ALTER TABLE actions DROP CONSTRAINT actions_status_check;
        ALTER TABLE actions ADD CONSTRAINT actions_status_check
            CHECK(status IN ('proposed','expired','cancelled','rejected','dispatch_authorized'));
        ALTER TABLE actions ADD UNIQUE(tenant_id,run_id,proposal_id,action_id,canonical_args_hash);
        ALTER TABLE proposal_waits DROP CONSTRAINT proposal_waits_status_check;
        ALTER TABLE proposal_waits ADD CONSTRAINT proposal_waits_status_check
            CHECK(status IN ('pending','approved','rejected','expired','cancelled'));
        DROP TRIGGER approvals_binding_immutable ON approvals;
        DROP TRIGGER actions_binding_immutable ON actions;
        CREATE FUNCTION protect_approval_decision() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW)-ARRAY['status','version','decision','decided_by','decided_at','reason','resume_generation'])
               IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['status','version','decision','decided_by','decided_at','reason','resume_generation'])
               OR (OLD.status<>'pending' AND NEW IS DISTINCT FROM OLD)
               OR (NEW IS DISTINCT FROM OLD AND (NEW.status=OLD.status OR NEW.version<>OLD.version+1)) THEN
                RAISE EXCEPTION 'Approval binding and recorded decision are immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER approvals_binding_immutable BEFORE UPDATE ON approvals
            FOR EACH ROW EXECUTE FUNCTION protect_approval_decision();
        CREATE FUNCTION protect_action_authorization() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW)-'status') IS DISTINCT FROM (to_jsonb(OLD)-'status')
               OR (OLD.status<>'proposed' AND NEW IS DISTINCT FROM OLD) THEN
                RAISE EXCEPTION 'Action reference and authorization are immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER actions_binding_immutable BEFORE UPDATE ON actions
            FOR EACH ROW EXECUTE FUNCTION protect_action_authorization();
        GRANT UPDATE(status,version,decision,decided_by,decided_at,reason,resume_generation) ON approvals TO sf_app_runtime;
        REVOKE INSERT ON approvals,actions FROM sf_app_runtime;
        GRANT INSERT(approval_id,tenant_id,run_id,proposal_id) ON approvals TO sf_app_runtime;
        GRANT INSERT(action_id,tenant_id,run_id,proposal_id,canonical_args_hash) ON actions TO sf_app_runtime;
        CREATE TABLE approval_decision_requests (
            app text NOT NULL DEFAULT 'supportflow' CHECK(app='supportflow'),
            tenant_id text NOT NULL,
            actor_id text NOT NULL REFERENCES users(user_id),
            operation text NOT NULL DEFAULT 'decide_approval' CHECK(operation='decide_approval'),
            client_request_id text NOT NULL CHECK(length(client_request_id) BETWEEN 1 AND 128
                AND client_request_id ~ '^[ -~]+$' AND length(btrim(client_request_id))>0),
            request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{64}$'),
            run_id text NOT NULL,
            approval_id text NOT NULL,
            decision text NOT NULL CHECK(decision IN ('approve','reject')),
            approval_version integer NOT NULL CHECK(approval_version>1),
            accepted_generation integer,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(app,tenant_id,actor_id,operation,client_request_id),
            FOREIGN KEY(tenant_id,run_id,approval_id) REFERENCES approvals(tenant_id,run_id,approval_id),
            CHECK((decision='approve' AND accepted_generation IS NOT NULL AND accepted_generation>1)
                OR (decision='reject' AND accepted_generation IS NULL))
        );
        CREATE TABLE dispatch_authorizations (
            authorization_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            run_id text NOT NULL,
            proposal_id text NOT NULL,
            approval_id text NOT NULL,
            action_id text NOT NULL UNIQUE,
            canonical_args_hash text NOT NULL CHECK(canonical_args_hash ~ '^[a-f0-9]{64}$'),
            execution_generation integer NOT NULL CHECK(execution_generation>1),
            requested_by text NOT NULL,
            approved_by text NOT NULL,
            authorized_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            policy_version text NOT NULL,
            business_version bigint NOT NULL CHECK(business_version>0),
            CHECK(requested_by<>approved_by),
            FOREIGN KEY(tenant_id,run_id,proposal_id,approval_id)
                REFERENCES approvals(tenant_id,run_id,proposal_id,approval_id),
            FOREIGN KEY(tenant_id,run_id,proposal_id,action_id,canonical_args_hash)
                REFERENCES actions(tenant_id,run_id,proposal_id,action_id,canonical_args_hash),
            FOREIGN KEY(approval_id,approved_by) REFERENCES approvals(approval_id,decided_by),
            FOREIGN KEY(tenant_id,requested_by,run_id) REFERENCES runs(tenant_id,actor_id,run_id)
        );
        CREATE FUNCTION validate_dispatch_approval() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS(SELECT 1 FROM approvals a WHERE a.approval_id=NEW.approval_id
                AND a.status='approved' AND a.decision='approve' AND a.decided_by=NEW.approved_by
                AND a.resume_generation<=NEW.execution_generation) THEN
                RAISE EXCEPTION 'Dispatch requires a stored approved decision' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER dispatch_approval_binding BEFORE INSERT ON dispatch_authorizations
            FOR EACH ROW EXECUTE FUNCTION validate_dispatch_approval();
        CREATE FUNCTION reject_approval_audit_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Approval audit rows are immutable' USING ERRCODE='23514';
        END $$;
        CREATE TRIGGER decision_request_immutable BEFORE UPDATE ON approval_decision_requests
            FOR EACH ROW EXECUTE FUNCTION reject_approval_audit_update();
        CREATE TRIGGER dispatch_authorization_immutable BEFORE UPDATE ON dispatch_authorizations
            FOR EACH ROW EXECUTE FUNCTION reject_approval_audit_update();
        REVOKE ALL ON approval_decision_requests,dispatch_authorizations FROM sf_app_runtime;
        GRANT SELECT,INSERT ON approval_decision_requests,dispatch_authorizations TO sf_app_runtime;
        CREATE INDEX approvals_pending_run ON approvals(tenant_id,status,run_id,approval_id);
    """)
