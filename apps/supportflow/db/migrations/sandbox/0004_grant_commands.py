"""Grant-only runtime writes and immutable receipts with optional legacy metadata."""
from alembic import op

revision = "sf_sandbox_0004"
down_revision = "sf_sandbox_0003"


def upgrade():
    op.execute("""
        ALTER TABLE action_receipts ADD COLUMN proposal_id text,
            ADD COLUMN authorization_id text, ADD COLUMN authorized_at timestamptz;
        ALTER TABLE action_receipts ADD CONSTRAINT receipt_authorization_metadata CHECK (
            (proposal_id IS NULL AND authorization_id IS NULL AND authorized_at IS NULL)
            OR (proposal_id IS NOT NULL AND length(btrim(proposal_id)) BETWEEN 1 AND 128
                AND authorization_id IS NOT NULL AND length(btrim(authorization_id)) BETWEEN 1 AND 128
                AND authorized_at IS NOT NULL));
        CREATE FUNCTION reject_grant_receipt_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Committed grant receipts are immutable' USING ERRCODE='55000';
        END $$;
        CREATE TRIGGER immutable_grant_receipt BEFORE UPDATE OR DELETE ON action_receipts
            FOR EACH ROW EXECUTE FUNCTION reject_grant_receipt_mutation();
        REVOKE ALL ON subscriptions FROM sf_sandbox_runtime;
        GRANT SELECT ON subscriptions TO sf_sandbox_runtime;
        GRANT UPDATE(temporary_seats,temporary_expires_at,business_version) ON subscriptions TO sf_sandbox_runtime;
        REVOKE ALL ON action_receipts FROM sf_sandbox_runtime;
        GRANT SELECT,INSERT ON action_receipts TO sf_sandbox_runtime;
        REVOKE ALL ON grant_policy_state FROM sf_sandbox_runtime;
        GRANT SELECT ON grant_policy_state TO sf_sandbox_runtime;
    """)
