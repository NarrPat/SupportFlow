"""Versioned ten-round execution budget; old snapshots and send bindings stay strict."""
from alembic import op
revision='sf_app_0016'
down_revision='sf_app_0015'

def upgrade():
    op.execute("""
        CREATE FUNCTION supportflow_execution_budget(config jsonb) RETURNS integer
        LANGUAGE sql IMMUTABLE AS $$
          SELECT CASE WHEN config @> '{"schema_version": 1,"contract_version":"dispatch-v1",
              "graph_version":"grant-execution-v1","grant_contract_version": 1,"approval_contract_version": 1}'::jsonb
            AND config->>'schema_version'='1' AND config->>'grant_contract_version'='1'
            AND config->>'approval_contract_version'='1'
          THEN CASE WHEN config @> '{"execution_contract_version": 1,"tool_round_limit": 9}'::jsonb
                  AND config->>'execution_contract_version'='1' AND config->>'tool_round_limit'='9' THEN 9
                WHEN config @> '{"execution_contract_version": 2,"tool_round_limit": 10}'::jsonb
                  AND config->>'execution_contract_version'='2' AND config->>'tool_round_limit'='10' THEN 10
                ELSE 0 END ELSE 0 END
        $$;
        ALTER TABLE runs DROP CONSTRAINT runs_tool_rounds_check;
        ALTER TABLE runs ADD CONSTRAINT runs_tool_rounds_check CHECK(tool_rounds BETWEEN 0 AND 6
            OR (tool_rounds=7 AND config_snapshot @> '{"schema_version": 1,"contract_version":"dispatch-v1",
                "graph_version":"grant-approval-v1","grant_contract_version": 1,"approval_contract_version": 1,"tool_round_limit": 7}'::jsonb
                AND config_snapshot->>'schema_version'='1' AND config_snapshot->>'grant_contract_version'='1'
                AND config_snapshot->>'approval_contract_version'='1' AND config_snapshot->>'tool_round_limit'='7')
            OR (tool_rounds BETWEEN 7 AND supportflow_execution_budget(config_snapshot)));
        ALTER TABLE run_tool_observations DROP CONSTRAINT run_tool_observations_ordinal_check;
        ALTER TABLE run_tool_observations ADD CONSTRAINT run_tool_observations_ordinal_check CHECK(ordinal BETWEEN 1 AND 10);
        CREATE OR REPLACE FUNCTION validate_approval_refresh_ordinal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.ordinal>6 AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id
                AND r.execution_generation=NEW.execution_generation AND r.tool_rounds>=NEW.ordinal
                AND NEW.tool_id='get_subscription' AND (
                    (NEW.ordinal=7 AND r.config_snapshot @> '{"schema_version": 1,"contract_version":"dispatch-v1",
                        "graph_version":"grant-approval-v1","grant_contract_version": 1,"approval_contract_version": 1,"tool_round_limit": 7}'::jsonb
                        AND r.config_snapshot->>'schema_version'='1' AND r.config_snapshot->>'grant_contract_version'='1'
                        AND r.config_snapshot->>'approval_contract_version'='1' AND r.config_snapshot->>'tool_round_limit'='7')
                    OR (NEW.ordinal IN (7,9) AND supportflow_execution_budget(r.config_snapshot)=9)
                    OR (NEW.ordinal IN (7,8,10) AND supportflow_execution_budget(r.config_snapshot)=10))) THEN
                RAISE EXCEPTION 'Observation requires its frozen graph budget' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        ALTER TABLE action_dispatches DROP CONSTRAINT action_dispatches_tool_ordinal_check;
        ALTER TABLE action_dispatches ADD CONSTRAINT action_dispatches_tool_ordinal_check CHECK(tool_ordinal IN (8,9));
        CREATE OR REPLACE FUNCTION validate_dispatch_intent() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status<>'sending' OR NEW.receipt IS NOT NULL OR NOT EXISTS(
                SELECT 1 FROM runs r JOIN dispatch_authorizations a USING(tenant_id,run_id)
                WHERE r.tenant_id=NEW.tenant_id AND r.run_id=NEW.run_id AND a.authorization_id=NEW.authorization_id
                    AND a.action_id=NEW.action_id AND a.canonical_args_hash=NEW.canonical_args_hash
                    AND r.status='running' AND r.admission_state='active' AND r.tool_rounds=NEW.tool_ordinal
                    AND r.execution_generation=NEW.execution_generation AND r.claimed_generation=NEW.execution_generation
                    AND ((NEW.tool_ordinal=8 AND supportflow_execution_budget(r.config_snapshot)=9)
                        OR (NEW.tool_ordinal IN (8,9) AND supportflow_execution_budget(r.config_snapshot)=10))
                    AND NEW.command->>'action_id'=a.action_id AND NEW.command->>'tenant_id'=a.tenant_id
                    AND NEW.command->>'proposal_id'=a.proposal_id AND NEW.command->>'authorization_id'=a.authorization_id
                    AND NEW.command->>'canonical_args_hash'=a.canonical_args_hash
                    AND (NEW.command->>'authorized_at')::timestamptz=a.authorized_at) THEN
                RAISE EXCEPTION 'Send intent requires the frozen execution contract and authorization' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
