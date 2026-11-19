"""Preserve historical invitations and deterministically read the newest record."""
from alembic import op

revision = 'sf_sandbox_0005'
down_revision = 'sf_sandbox_0004'


def upgrade():
    op.execute('ALTER TABLE public.invitations DROP CONSTRAINT invitations_tenant_id_customer_id_email_key')
    op.execute('CREATE INDEX invitations_latest ON public.invitations (tenant_id,customer_id,email,created_at DESC,invitation_id DESC)')
