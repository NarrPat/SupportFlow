"""Versioned knowledge, atomic dual-index publication and retrieval audits."""
from alembic import op


revision = "sf_app_0007"
down_revision = "sf_app_0006"


def upgrade():
    op.execute("""
        CREATE TABLE knowledge_state (
            tenant_id text PRIMARY KEY REFERENCES tenants,
            active_generation integer,
            next_generation integer NOT NULL DEFAULT 1 CHECK(next_generation > 0)
        );
        CREATE TABLE documents (
            document_id text PRIMARY KEY,
            tenant_id text NOT NULL REFERENCES tenants,
            title text NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
            visibility text NOT NULL DEFAULT 'tenant' CHECK(visibility IN ('tenant','private')),
            active_version_id text,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE(tenant_id,document_id)
        );
        CREATE TABLE document_acl (
            tenant_id text NOT NULL,
            document_id text NOT NULL,
            user_id text NOT NULL,
            PRIMARY KEY(tenant_id,document_id,user_id),
            FOREIGN KEY(tenant_id,document_id) REFERENCES documents(tenant_id,document_id),
            FOREIGN KEY(tenant_id,user_id) REFERENCES memberships(tenant_id,user_id)
        );
        CREATE TABLE document_versions (
            version_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            document_id text NOT NULL,
            raw_sha256 text NOT NULL CHECK(raw_sha256 ~ '^[0-9a-f]{64}$'),
            content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
            normalized_text text NOT NULL,
            original_path text NOT NULL,
            media_type text NOT NULL CHECK(media_type IN ('md','txt')),
            parse_status text NOT NULL DEFAULT 'staged' CHECK(parse_status IN ('staged','parsed','failed')),
            corpus_origin text NOT NULL CHECK(corpus_origin IN ('uploaded','synthetic_business')),
            policy_version text,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE(tenant_id,document_id,version_id),
            UNIQUE(tenant_id,document_id,raw_sha256),
            FOREIGN KEY(tenant_id,document_id) REFERENCES documents(tenant_id,document_id)
        );
        ALTER TABLE documents ADD CONSTRAINT document_active_version_fk
            FOREIGN KEY(tenant_id,document_id,active_version_id)
            REFERENCES document_versions(tenant_id,document_id,version_id);
        CREATE TABLE document_requests (
            tenant_id text NOT NULL,
            actor_id text NOT NULL,
            client_request_id text NOT NULL CHECK(length(client_request_id) BETWEEN 1 AND 128
                AND client_request_id ~ '^[ -~]+$' AND length(btrim(client_request_id)) > 0),
            request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
            document_id text NOT NULL,
            version_id text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,actor_id,client_request_id),
            FOREIGN KEY(tenant_id,actor_id) REFERENCES memberships(tenant_id,user_id),
            FOREIGN KEY(tenant_id,document_id,version_id)
                REFERENCES document_versions(tenant_id,document_id,version_id)
        );
        CREATE TABLE chunks (
            evidence_id text PRIMARY KEY,
            tenant_id text NOT NULL,
            document_id text NOT NULL,
            version_id text NOT NULL,
            section_id text NOT NULL,
            title text NOT NULL,
            start_line integer NOT NULL CHECK(start_line > 0),
            end_line integer NOT NULL CHECK(end_line >= start_line),
            start_offset integer NOT NULL CHECK(start_offset >= 0),
            end_offset integer NOT NULL CHECK(end_offset > start_offset),
            text text NOT NULL,
            content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
            section_content_sha256 text NOT NULL CHECK(section_content_sha256 ~ '^[0-9a-f]{64}$'),
            token_count integer NOT NULL CHECK(token_count BETWEEN 1 AND 450),
            tokenizer_version text NOT NULL,
            chunker_version text NOT NULL,
            UNIQUE(tenant_id,evidence_id),
            UNIQUE(version_id,section_id,start_offset,end_offset,tokenizer_version,chunker_version),
            FOREIGN KEY(tenant_id,document_id,version_id)
                REFERENCES document_versions(tenant_id,document_id,version_id)
        );
        CREATE TABLE index_generations (
            tenant_id text NOT NULL REFERENCES tenants,
            generation integer NOT NULL CHECK(generation > 0),
            expected_generation integer,
            manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object'),
            corpus_manifest_hash text NOT NULL CHECK(corpus_manifest_hash ~ '^[0-9a-f]{64}$'),
            embedding_model text NOT NULL,
            embedding_revision text NOT NULL,
            dimension integer NOT NULL CHECK(dimension=512),
            tokenizer_version text NOT NULL,
            chunker_version text NOT NULL,
            lexical_version text NOT NULL,
            vector_origin text NOT NULL CHECK(vector_origin IN ('live_local','synthetic_vector')),
            vector_ready boolean NOT NULL DEFAULT false,
            lexical_ready boolean NOT NULL DEFAULT false,
            snapshot_checksum text CHECK(snapshot_checksum ~ '^[0-9a-f]{64}$'),
            snapshot_path text,
            state text NOT NULL DEFAULT 'building' CHECK(state IN ('building','ready','active','retired','failed')),
            failure_code text,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            published_at timestamptz,
            PRIMARY KEY(tenant_id,generation),
            CHECK(state NOT IN ('ready','active','retired') OR
                (vector_ready AND lexical_ready AND snapshot_checksum IS NOT NULL AND snapshot_path IS NOT NULL))
        );
        CREATE UNIQUE INDEX one_active_knowledge_generation ON index_generations(tenant_id) WHERE state='active';
        ALTER TABLE knowledge_state ADD CONSTRAINT knowledge_active_generation_fk
            FOREIGN KEY(tenant_id,active_generation) REFERENCES index_generations(tenant_id,generation);
        CREATE TABLE index_vectors (
            tenant_id text NOT NULL,
            generation integer NOT NULL,
            evidence_id text NOT NULL,
            embedding vector(512) NOT NULL,
            PRIMARY KEY(tenant_id,generation,evidence_id),
            FOREIGN KEY(tenant_id,generation) REFERENCES index_generations(tenant_id,generation),
            FOREIGN KEY(tenant_id,evidence_id) REFERENCES chunks(tenant_id,evidence_id)
        );
        CREATE TABLE run_retrievals (
            run_id text NOT NULL REFERENCES runs,
            execution_generation integer NOT NULL CHECK(execution_generation > 0),
            ordinal integer NOT NULL CHECK(ordinal BETWEEN 1 AND 6),
            tenant_id text NOT NULL REFERENCES tenants,
            index_generation integer,
            query_sha256 text NOT NULL CHECK(query_sha256 ~ '^[0-9a-f]{64}$'),
            vector_ids jsonb NOT NULL,
            lexical_ids jsonb NOT NULL,
            fused_ids jsonb NOT NULL,
            refs jsonb NOT NULL,
            raw_bytes integer CHECK(raw_bytes BETWEEN 0 AND 65536),
            projection_tokens integer CHECK(projection_tokens >= 0),
            error_code text,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(run_id,execution_generation,ordinal),
            FOREIGN KEY(tenant_id,index_generation) REFERENCES index_generations(tenant_id,generation),
            FOREIGN KEY(tenant_id,run_id) REFERENCES runs(tenant_id,run_id)
        );
        REVOKE ALL ON chunks,index_vectors,run_retrievals FROM sf_app_runtime;
        GRANT SELECT,INSERT ON chunks,index_vectors,run_retrievals TO sf_app_runtime;
    """)
