"""Immutable bounded share snapshots and unique confirmed history checkpoints."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0010_tactics_shares_history"
down_revision = "0009_tactics_teams"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_tactics_notebook_id", "tactics", ["notebook_id", "id"])
    op.execute("""DO $$ BEGIN IF EXISTS (
      SELECT 1 FROM tactic_versions a JOIN tactic_versions b
      ON a.tactic_id=b.tactic_id AND a.source_epoch=b.source_epoch
      AND a.source_revision=b.source_revision AND a.id<>b.id
      WHERE a.yjs_state<>b.yjs_state OR a.projection<>b.projection
    ) THEN RAISE EXCEPTION 'inconsistent history checkpoint'; END IF; END $$""")
    op.execute("""DELETE FROM tactic_versions WHERE id IN (
      SELECT id FROM (SELECT id,row_number() OVER(PARTITION BY tactic_id,source_epoch,
      source_revision ORDER BY created_at,id) AS position FROM tactic_versions) d
      WHERE position>1)""")
    op.create_unique_constraint(
        "uq_tactic_versions_revision",
        "tactic_versions",
        ["tactic_id", "source_epoch", "source_revision"],
    )
    op.create_check_constraint(
        "ck_tactic_versions_reason", "tactic_versions", "reason IN ('archive','publish','periodic')"
    )
    op.create_table(
        "share_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_notebook_id", sa.Uuid(), sa.ForeignKey("notebooks.id"), nullable=False),
        sa.Column("tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id")),
        sa.Column("token_digest", sa.LargeBinary(), unique=True, nullable=False),
        sa.Column("published_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("current_version_id", sa.Uuid()),
        sa.Column("allow_copy", sa.Boolean(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("octet_length(token_digest)=32", name="ck_share_token_digest"),
    )
    op.create_index(
        "ix_share_links_notebook_created", "share_links", ["owner_notebook_id", "created_at", "id"]
    )
    op.create_foreign_key(
        "fk_share_tactic_notebook",
        "share_links",
        "tactics",
        ["owner_notebook_id", "tactic_id"],
        ["notebook_id", "id"],
    )
    op.create_table(
        "share_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("share_link_id", sa.Uuid(), sa.ForeignKey("share_links.id"), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allow_copy", sa.Boolean(), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.UniqueConstraint("share_link_id", "id", name="uq_share_versions_owner"),
    )
    op.create_foreign_key(
        "fk_share_current_version",
        "share_links",
        "share_versions",
        ["id", "current_version_id"],
        ["share_link_id", "id"],
    )
    op.create_table(
        "share_version_items",
        sa.Column(
            "share_version_id", sa.Uuid(), sa.ForeignKey("share_versions.id"), primary_key=True
        ),
        sa.Column("source_tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id"), primary_key=True),
        sa.Column("item_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("order_index", sa.SmallInteger(), nullable=False),
        sa.Column("source_epoch", sa.Uuid(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("projection", JSONB(), nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=False),
        sa.Column("attribution", JSONB(), nullable=False),
        sa.UniqueConstraint("share_version_id", "order_index", name="uq_share_items_order"),
        sa.CheckConstraint("order_index BETWEEN 0 AND 99", name="ck_share_items_order"),
        sa.CheckConstraint(
            "octet_length(state) BETWEEN 1 AND 4194304", name="ck_share_items_state"
        ),
    )
    op.create_table(
        "share_read_grants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("grant_digest", sa.LargeBinary(), unique=True, nullable=False),
        sa.Column("share_link_id", sa.Uuid(), sa.ForeignKey("share_links.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("octet_length(grant_digest)=32", name="ck_share_grant_digest"),
    )
    op.create_index("ix_share_read_grants_expires", "share_read_grants", ["expires_at"])


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM share_links) THEN "
        "RAISE EXCEPTION 'export share data before downgrade'; END IF; END $$"
    )
    op.drop_table("share_read_grants")
    op.drop_table("share_version_items")
    op.drop_constraint("fk_share_current_version", "share_links", type_="foreignkey")
    op.drop_table("share_versions")
    op.drop_table("share_links")
    op.drop_constraint("uq_tactics_notebook_id", "tactics", type_="unique")
    op.drop_constraint("ck_tactic_versions_reason", "tactic_versions", type_="check")
    op.drop_constraint("uq_tactic_versions_revision", "tactic_versions", type_="unique")
