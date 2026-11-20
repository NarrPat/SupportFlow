CREATE TABLE invitation_policies (
    tenant_id text NOT NULL,
    customer_id text NOT NULL,
    allowed_domains jsonb CHECK (allowed_domains IS NULL OR
        (jsonb_typeof(allowed_domains) = 'array' AND jsonb_array_length(allowed_domains) <= 50)),
    allow_subdomains boolean,
    invitation_permission boolean,
    PRIMARY KEY (tenant_id, customer_id)
);

CREATE TABLE invitations (
    tenant_id text NOT NULL,
    customer_id text NOT NULL,
    invitation_id varchar(128) NOT NULL,
    email varchar(254) NOT NULL,
    email_domain varchar(253) NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
    status varchar(64) NOT NULL CHECK (length(status) > 0),
    reason_code varchar(128),
    PRIMARY KEY (tenant_id, invitation_id)
);
CREATE INDEX invitations_latest ON invitations (tenant_id,customer_id,email,created_at DESC,invitation_id DESC);

CREATE TABLE service_statuses (
    tenant_id text NOT NULL,
    customer_id text NOT NULL,
    component text NOT NULL CHECK (component = 'invitations'),
    status varchar(64) NOT NULL CHECK (length(status) > 0),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id,customer_id,component)
);

CREATE TABLE grant_policy_state (
    singleton boolean PRIMARY KEY CHECK (singleton),
    policy_version varchar(128) NOT NULL,
    section_id varchar(128) NOT NULL,
    content_sha256 varchar(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    constants jsonb NOT NULL CHECK (jsonb_typeof(constants) = 'object')
);
CREATE INDEX receipt_history ON action_receipts (tenant_id,customer_id,subscription_id,committed_business_at DESC,action_id);

GRANT SELECT ON invitation_policies, invitations, service_statuses, grant_policy_state TO sf_java_runtime, sf_java_oracle;
GRANT UPDATE (temporary_seats, temporary_expires_at, business_version) ON subscriptions TO sf_java_runtime;
GRANT INSERT ON action_receipts TO sf_java_runtime;
