CREATE TABLE subscriptions (
    tenant_id varchar(128) NOT NULL,
    customer_id varchar(128) NOT NULL,
    subscription_id varchar(128) NOT NULL,
    base_seats integer NOT NULL CHECK (base_seats >= 0),
    used_seats integer NOT NULL CHECK (used_seats >= 0),
    temporary_seats integer NOT NULL DEFAULT 0 CHECK (temporary_seats >= 0),
    temporary_expires_at timestamptz,
    plan_status varchar(64) NOT NULL CHECK (length(plan_status) > 0),
    business_version bigint NOT NULL CHECK (business_version >= 1),
    PRIMARY KEY (tenant_id, customer_id, subscription_id),
    UNIQUE (tenant_id, customer_id),
    CHECK (temporary_seats = 0 OR temporary_expires_at IS NOT NULL)
);

CREATE TABLE action_receipts (
    action_id varchar(128) PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    customer_id varchar(128) NOT NULL,
    subscription_id varchar(128) NOT NULL,
    canonical_args_hash varchar(64) NOT NULL CHECK (canonical_args_hash ~ '^[0-9a-f]{64}$'),
    before_version bigint NOT NULL CHECK (before_version >= 1),
    after_version bigint NOT NULL CHECK (after_version = before_version + 1),
    committed_business_at timestamptz NOT NULL,
    seats integer NOT NULL CHECK (seats BETWEEN 1 AND 20),
    days integer NOT NULL CHECK (days BETWEEN 1 AND 7),
    temporary_expires_at timestamptz NOT NULL CHECK (temporary_expires_at = committed_business_at + days * interval '24 hours'),
    policy_version varchar(128) NOT NULL,
    proposal_id varchar(128) NOT NULL,
    authorization_id varchar(128) NOT NULL,
    authorized_at timestamptz NOT NULL,
    FOREIGN KEY (tenant_id, customer_id, subscription_id) REFERENCES subscriptions,
    UNIQUE (tenant_id, customer_id, subscription_id, after_version)
);

GRANT SELECT ON subscriptions, action_receipts TO sf_java_runtime, sf_java_oracle;
