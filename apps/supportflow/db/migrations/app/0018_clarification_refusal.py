"""Persist explicit refusals without pretending a resume generation exists."""
from alembic import op
revision='sf_app_0018'
down_revision='sf_app_0017'

def upgrade():
    op.execute("""
        ALTER TABLE request_idempotency DROP CONSTRAINT request_idempotency_operation_check;
        ALTER TABLE request_idempotency DROP CONSTRAINT request_idempotency_result_kind;
        ALTER TABLE request_idempotency ADD CONSTRAINT request_idempotency_operation_check
            CHECK(operation IN ('create_run','submit_clarification','cancel_run','refuse_clarification'));
        ALTER TABLE request_idempotency ADD CONSTRAINT request_idempotency_result_kind CHECK(
            (operation IN ('create_run','cancel_run') AND input_id IS NULL AND accepted_generation IS NULL)
            OR (operation='submit_clarification' AND input_id IS NOT NULL
                AND accepted_generation IS NOT NULL AND accepted_generation>1)
            OR (operation='refuse_clarification' AND input_id IS NOT NULL AND accepted_generation IS NULL));
    """)
