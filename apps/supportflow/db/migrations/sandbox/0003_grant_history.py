"""Read-only committed-history/policy facts for proposal eligibility, no grant API."""
from alembic import op

revision = "sf_sandbox_0003"
down_revision = "sf_sandbox_0002"


def upgrade():
    op.execute("""
        ALTER TABLE subscriptions ADD UNIQUE(tenant_id,customer_id,subscription_id);
        CREATE TABLE action_receipts (
            action_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            customer_id text NOT NULL,
            subscription_id text NOT NULL,
            canonical_args_hash text NOT NULL CHECK(canonical_args_hash ~ '^[a-f0-9]{64}$'),
            before_version bigint NOT NULL CHECK(before_version>0),
            after_version bigint NOT NULL CHECK(after_version=before_version+1),
            committed_business_at timestamptz NOT NULL,
            seats integer NOT NULL CHECK(seats BETWEEN 1 AND 20),
            days integer NOT NULL CHECK(days BETWEEN 1 AND 7),
            temporary_expires_at timestamptz NOT NULL CHECK(temporary_expires_at=committed_business_at+days*interval '86400 seconds'),
            policy_version text NOT NULL,
            FOREIGN KEY(tenant_id,customer_id,subscription_id) REFERENCES subscriptions(tenant_id,customer_id,subscription_id),
            UNIQUE(tenant_id,subscription_id,after_version)
        );
        CREATE INDEX action_receipts_history ON action_receipts(tenant_id,customer_id,subscription_id,committed_business_at DESC,action_id);
        CREATE TABLE grant_policy_state (
            singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
            policy_version text NOT NULL,
            section_id text NOT NULL,
            content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[a-f0-9]{64}$'),
            constants jsonb NOT NULL CHECK(jsonb_typeof(constants)='object')
        );
        INSERT INTO grant_policy_state(policy_version,section_id,content_sha256,constants)
        VALUES('v1','policy.temporary_seats.v1','89310ab4bedb6b246ab9367e75082acb0d594c5b13ed374273a1209c057695e6',
          '{"action_type": "grant_temporary_seats","command_schema_version": 1,"seats_min": 1,"seats_max": 20,
            "days_min": 1,"days_max": 7,"required_plan_status": "active","cooldown_seconds": 2592000,
            "proposal_ttl_seconds": 900,"day_seconds": 86400,"minimum_remaining_seats": 1,
            "temporary_grant_mode": "replace_expired","approval_role": "supervisor","self_approval_allowed": false}'::jsonb);
        REVOKE ALL ON action_receipts,grant_policy_state FROM sf_sandbox_runtime;
        GRANT SELECT ON action_receipts,grant_policy_state TO sf_sandbox_runtime;
    """)
