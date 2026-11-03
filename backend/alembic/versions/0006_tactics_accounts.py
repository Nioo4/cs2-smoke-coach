"""Ordinary accounts and their unique personal notebook, without prototype import."""

from alembic import op
import sqlalchemy as sa

revision = "0006_tactics_accounts"
down_revision = "0005_route_matches_response"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("username_normalized", sa.String(32), nullable=False, unique=True),
        sa.Column("display_name", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("recovery_digest", sa.LargeBinary(), nullable=False),
        sa.Column("credential_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("username ~ '^[A-Za-z0-9_]{3,32}$'", name="ck_users_username"),
        sa.CheckConstraint("username_normalized = lower(username)", name="ck_users_normalized"),
        sa.CheckConstraint("char_length(display_name) BETWEEN 1 AND 32", name="ck_users_display"),
        sa.CheckConstraint("octet_length(recovery_digest) = 32", name="ck_users_recovery"),
        sa.CheckConstraint("credential_version >= 1", name="ck_users_credentials"),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("session_digest", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("csrf_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("csrf_digest", sa.LargeBinary(), nullable=False),
        sa.Column("credential_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("octet_length(session_digest) = 32", name="ck_user_sessions_digest"),
        sa.CheckConstraint("octet_length(csrf_nonce) = 32", name="ck_user_sessions_nonce"),
        sa.CheckConstraint("octet_length(csrf_digest) = 32", name="ck_user_sessions_csrf"),
        sa.CheckConstraint("expires_at > created_at", name="ck_user_sessions_expiry"),
        sa.CheckConstraint("credential_version >= 1", name="ck_user_sessions_credentials"),
    )
    op.create_index("ix_user_sessions_user_revoked", "user_sessions", ["user_id", "revoked_at"])
    op.create_index("ix_user_sessions_expires", "user_sessions", ["expires_at"])
    op.create_table(
        "notebooks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind = 'personal'", name="ck_notebooks_kind"),
    )


def downgrade():
    raise RuntimeError("Account data is retained; downgrade requires an explicit export/removal plan.")
