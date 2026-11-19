"""Honor frozen execution budgets for optional, untrusted external reads."""
from alembic import op
revision="sf_app_0017"
down_revision="sf_app_0016"

def upgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_approval_refresh_ordinal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.ordinal>6 AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id
                AND r.execution_generation=NEW.execution_generation AND r.tool_rounds>=NEW.ordinal
                AND ((NEW.tool_id='list_external_issues'
                    AND NEW.ordinal BETWEEN 7 AND 9
                    AND supportflow_execution_budget(r.config_snapshot)>=NEW.ordinal
                    AND r.config_snapshot->'external_issues' @> '{"schema_version": 1}'::jsonb
                    AND r.config_snapshot->'external_issues'->>'schema_version'='1'
                    AND jsonb_typeof(r.config_snapshot->'external_issues'->'repository')='string'
                    AND COALESCE(r.confirmed_fields->>'requested_action','')='')
                    OR (NEW.tool_id='get_subscription' AND (
                    (NEW.ordinal=7 AND r.config_snapshot @> '{"schema_version": 1,"contract_version":"dispatch-v1",
                        "graph_version":"grant-approval-v1","grant_contract_version": 1,"approval_contract_version": 1,"tool_round_limit": 7}'::jsonb
                        AND r.config_snapshot->>'schema_version'='1' AND r.config_snapshot->>'grant_contract_version'='1'
                        AND r.config_snapshot->>'approval_contract_version'='1' AND r.config_snapshot->>'tool_round_limit'='7')
                    OR (NEW.ordinal IN (7,9) AND supportflow_execution_budget(r.config_snapshot)=9)
                    OR (NEW.ordinal IN (7,8,10) AND supportflow_execution_budget(r.config_snapshot)=10))))) THEN
                RAISE EXCEPTION 'Observation requires its frozen graph budget' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
