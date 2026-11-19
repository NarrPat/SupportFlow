"""Allow audited no-effect answers without inventing a grant action."""
from alembic import op

revision = 'sf_app_0015'
down_revision = 'sf_app_0014'


def upgrade():
    op.execute("""
        ALTER TABLE ticket_resolution_audits ALTER COLUMN action_id DROP NOT NULL;
        ALTER TABLE ticket_resolution_audits ADD CONSTRAINT readonly_resolution_without_action CHECK (
            action_id IS NOT NULL OR COALESCE(
                status='succeeded' AND result->'schema_version' IN ('5'::jsonb,'7'::jsonb)
                AND result->'action_receipts'='[]'::jsonb, false)
        );
    """)
