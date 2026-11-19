"""Store human notes separately from agent-owned resolution fields."""
from alembic import op


revision = "sf_app_0013"
down_revision = "sf_app_0012"


def upgrade():
    op.execute("ALTER TABLE public.tickets ADD COLUMN human_note varchar(2000)")
