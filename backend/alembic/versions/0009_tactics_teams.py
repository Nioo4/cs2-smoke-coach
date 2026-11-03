"""Team ownership domains, bounded invitations and preserved personal notebooks."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0009_tactics_teams"
down_revision = "0008_tactics_collaboration"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "teams", sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="ck_teams_name"),
        sa.CheckConstraint("(status='active' AND archived_at IS NULL) OR "
                           "(status='archived' AND archived_at IS NOT NULL)",
                           name="ck_teams_archive"),
    )
    op.create_table(
        "team_members",
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('editor','viewer')", name="ck_members_role"),
    )
    op.create_table(
        "team_invites", sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("token_digest", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.SmallInteger(), nullable=False),
        sa.Column("uses", sa.SmallInteger(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("octet_length(token_digest)=32", name="ck_invites_digest"),
        sa.CheckConstraint("max_uses BETWEEN 1 AND 20 AND uses BETWEEN 0 AND max_uses",
                           name="ck_invites_uses"),
    )
    op.create_index("ix_team_invites_team_created", "team_invites", ["team_id", "created_at", "id"])
    op.create_table(
        "team_invite_acceptances",
        sa.Column("invite_id", sa.Uuid(), sa.ForeignKey("team_invites.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.drop_constraint("ck_notebooks_kind", "notebooks", type_="check")
    op.alter_column("notebooks", "user_id", nullable=True)
    op.add_column("notebooks", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_notebooks_team", "notebooks", "teams", ["team_id"], ["id"],
                          ondelete="RESTRICT")
    op.create_unique_constraint("uq_notebooks_team", "notebooks", ["team_id"])
    op.create_check_constraint("ck_notebooks_kind", "notebooks",
                               "(kind='personal' AND user_id IS NOT NULL AND team_id IS NULL) OR "
                               "(kind='team' AND user_id IS NULL AND team_id IS NOT NULL)")
    op.add_column("tactics", sa.Column("source_attribution", JSONB(), nullable=True))
    # Deferred so creation, transfer and membership updates can remain one atomic transaction.
    op.execute("""
      CREATE FUNCTION tactics_require_owner_member() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE affected uuid[];
      BEGIN
        IF TG_TABLE_NAME='teams' THEN affected := ARRAY[NEW.id];
        ELSIF TG_OP='DELETE' THEN affected := ARRAY[OLD.team_id];
        ELSE affected := ARRAY[OLD.team_id,NEW.team_id]; END IF;
        IF EXISTS (SELECT 1 FROM teams t WHERE t.id=ANY(affected) AND NOT EXISTS
          (SELECT 1 FROM team_members m WHERE m.team_id=t.id AND m.user_id=t.owner_id)) THEN
          RAISE EXCEPTION 'team owner must be a member' USING ERRCODE='23514';
        END IF;
        RETURN NULL;
      END $$;
      CREATE CONSTRAINT TRIGGER teams_owner_member AFTER INSERT OR UPDATE ON teams
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION tactics_require_owner_member();
      CREATE CONSTRAINT TRIGGER members_owner_member AFTER DELETE OR UPDATE ON team_members
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION tactics_require_owner_member();
    """)


def downgrade():
    # No destructive implicit downgrade of real team notebooks.
    op.execute("DO $$ BEGIN IF EXISTS(SELECT 1 FROM notebooks WHERE kind='team') THEN "
               "RAISE EXCEPTION 'archive/export team data before downgrade'; END IF; END $$")
    op.execute("DROP TRIGGER members_owner_member ON team_members")
    op.execute("DROP TRIGGER teams_owner_member ON teams")
    op.execute("DROP FUNCTION tactics_require_owner_member()")
    op.drop_column("tactics", "source_attribution")
    op.drop_constraint("ck_notebooks_kind", "notebooks", type_="check")
    op.drop_constraint("uq_notebooks_team", "notebooks", type_="unique")
    op.drop_constraint("fk_notebooks_team", "notebooks", type_="foreignkey")
    op.drop_column("notebooks", "team_id")
    op.alter_column("notebooks", "user_id", nullable=False)
    op.create_check_constraint("ck_notebooks_kind", "notebooks", "kind='personal'")
    for name in ["team_invite_acceptances", "team_invites", "team_members", "teams"]:
        op.drop_table(name)
