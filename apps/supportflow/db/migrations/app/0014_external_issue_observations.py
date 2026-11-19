"""Allow the external read tool in the existing append-only observation journal."""
from alembic import op


revision = "sf_app_0014"
down_revision = "sf_app_0013"


def upgrade():
    op.execute("""ALTER TABLE public.run_tool_observations
        DROP CONSTRAINT run_tool_observations_tool_id_check,
        ADD CONSTRAINT run_tool_observations_tool_id_check CHECK (
            tool_id IN ('get_customer', 'get_subscription', 'get_invitation',
                        'get_service_status', 'list_external_issues'))""")
