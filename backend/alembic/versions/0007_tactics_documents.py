"""Personal tactic documents, directory idempotency and archive snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_tactics_documents"
down_revision = "0006_tactics_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tactics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("notebook_id", sa.Uuid(), sa.ForeignKey("notebooks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("side", sa.String(2), nullable=False),
        sa.Column("map", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 100", name="ck_tactics_title"),
        sa.CheckConstraint("side IN ('T','CT')", name="ck_tactics_side"),
        sa.CheckConstraint("map = 'mirage'", name="ck_tactics_map"),
        sa.CheckConstraint("(status='active' AND archived_at IS NULL) OR (status='archived' AND archived_at IS NOT NULL)", name="ck_tactics_archive"),
    )
    op.create_index("ix_tactics_notebook_status_updated", "tactics", ["notebook_id", "status", "updated_at", "id"])
    op.create_table(
        "tactic_documents",
        sa.Column("tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("yjs_state", sa.LargeBinary(), nullable=False),
        sa.Column("projection", JSONB(), nullable=False),
        sa.Column("state_digest", sa.LargeBinary(), nullable=False),
        sa.Column("projection_digest", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version=2", name="ck_tactic_documents_schema"),
        sa.CheckConstraint("revision BETWEEN 0 AND 9007199254740991", name="ck_tactic_revision"),
        sa.CheckConstraint("octet_length(yjs_state) BETWEEN 1 AND 4194304", name="ck_tactic_documents_size"),
        sa.CheckConstraint("octet_length(state_digest)=32 AND octet_length(projection_digest)=32", name="ck_tactic_documents_digests"),
    )
    op.create_table(
        "tactic_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_epoch", sa.Uuid(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("yjs_state", sa.LargeBinary(), nullable=False),
        sa.Column("projection", JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tactic_versions_tactic_created", "tactic_versions", ["tactic_id", "created_at"])
    op.create_index("ix_tactic_versions_expires", "tactic_versions", ["expires_at"])
    op.create_table(
        "api_idempotency",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("result", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "scope", "key", name="uq_api_idempotency_actor_scope_key"),
        sa.CheckConstraint("octet_length(request_digest)=32", name="ck_api_idempotency_digest"),
    )
    op.create_index("ix_api_idempotency_expires", "api_idempotency", ["expires_at"])


def downgrade():
    raise RuntimeError("Tactic data is retained; explicit export/removal is required before downgrade.")
