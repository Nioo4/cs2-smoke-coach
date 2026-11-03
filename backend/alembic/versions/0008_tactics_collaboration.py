"""Single-use collaboration tickets, live grants, operation deduplication and recovery."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_tactics_collaboration"
down_revision = "0007_tactics_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collaboration_tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticket_digest", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("user_sessions.id"), nullable=False),
        sa.Column("tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("octet_length(ticket_digest)=32", name="ck_collab_ticket_digest"),
    )
    op.create_index("ix_collab_ticket_expires", "collaboration_tickets", ["expires_at"])
    op.create_table(
        "collaboration_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("collaboration_tickets.id"), nullable=False, unique=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("user_sessions.id"), nullable=False),
        sa.Column("tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id"), nullable=False),
        sa.Column("node_instance_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "tactic_operations",
        sa.Column("tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id"), primary_key=True),
        sa.Column("epoch", sa.Uuid(), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tactic_id", "epoch", "revision", name="uq_tactic_operation_revision"),
        sa.CheckConstraint("octet_length(fingerprint)=32", name="ck_tactic_operation_fingerprint"),
        sa.CheckConstraint("revision BETWEEN 1 AND 9007199254740991", name="ck_tactic_operation_revision"),
    )
    op.create_table(
        "tactic_recovery_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id"), nullable=False),
        sa.Column("source_epoch", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("texts", JSONB(), nullable=False),
        sa.Column("deleted_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tactic_id", "source_epoch", "entity_id", name="uq_tactic_recovery_entity"),
    )
    op.create_index("ix_tactic_recovery_expires", "tactic_recovery_items", ["tactic_id", "expires_at"])


def downgrade():
    raise RuntimeError("Collaboration data must be exported before an explicit destructive downgrade.")
