"""Minimal successful business events, no payload or credential columns."""

import sqlalchemy as sa

from alembic import op

revision = "0011_tactics_audit_events"
down_revision = "0010_tactics_shares_history"
branch_labels = None
depends_on = None

ACTIONS = (
    "account.register",
    "account.login",
    "account.logout",
    "account.profile.update",
    "account.password.change",
    "account.recovery.rotate",
    "account.recover",
    "team.create",
    "team.rename",
    "team.transfer",
    "team.archive",
    "team.restore",
    "team.invite.create",
    "team.invite.revoke",
    "team.invite.accept",
    "team.member.role",
    "team.member.remove",
    "team.member.leave",
    "tactic.create",
    "tactic.archive",
    "tactic.restore",
    "tactic.copy",
    "tactic.restore_copy",
    "share.create",
    "share.publish",
    "share.revoke",
    "tactic.copy.share",
    "history.checkpoint",
    "document.text.update",
    "document.role.add",
    "document.stage.add",
    "document.stage.copy",
    "document.stage.delete",
    "document.stage.reorder",
    "document.stage.reinherit",
    "document.stage.lineups.set",
    "document.position.set",
    "document.action.add",
    "document.action.update",
    "document.action.group",
    "document.action.reorder",
    "document.action.delete",
    "document.recovery.restore",
    "document.tactic.side.set",
    "document.object.undo",
)


def upgrade():
    op.create_table("tactic_audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("tactic_id", sa.Uuid(), sa.ForeignKey("tactics.id", ondelete="SET NULL")),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id", ondelete="SET NULL")),
        sa.Column("resource_id", sa.Uuid()),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("result_code", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("action IN (" + ",".join(repr(a) for a in ACTIONS) + ")",
                           name="ck_tactic_audit_action"),
        sa.CheckConstraint("result_code='SUCCESS'", name="ck_tactic_audit_result"),
        sa.CheckConstraint("request_id ~ '^[0-9a-f]{32}$'", name="ck_tactic_audit_request"),
    )
    op.create_index("ix_tactic_audit_time", "tactic_audit_events", ["created_at", "id"])
    op.create_index("ix_tactic_audit_tactic", "tactic_audit_events",
                    ["tactic_id", "created_at", "id"])
    op.create_index("ix_tactic_audit_team", "tactic_audit_events", ["team_id", "created_at", "id"])


def downgrade():
    op.drop_table("tactic_audit_events")
