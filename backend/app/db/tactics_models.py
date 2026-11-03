"""Ordinary accounts, sessions and personal notebooks; independent of admin auth."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

AUDIT_ACTIONS = frozenset({
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
})


class TacticAuditEvent(Base):
    __tablename__ = "tactic_audit_events"
    __table_args__ = (
        sa.CheckConstraint("action IN (" + ",".join(repr(a) for a in sorted(AUDIT_ACTIONS)) + ")",
                           name="ck_tactic_audit_action"),
        sa.CheckConstraint("result_code='SUCCESS'", name="ck_tactic_audit_result"),
        sa.CheckConstraint("request_id ~ '^[0-9a-f]{32}$'", name="ck_tactic_audit_request"),
        sa.Index("ix_tactic_audit_time", "created_at", "id"),
        sa.Index("ix_tactic_audit_tactic", "tactic_id", "created_at", "id"),
        sa.Index("ix_tactic_audit_team", "team_id", "created_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"))
    tactic_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("tactics.id", ondelete="SET NULL"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("teams.id", ondelete="SET NULL"))
    resource_id: Mapped[uuid.UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(sa.String(64))
    result_code: Mapped[str] = mapped_column(sa.String(64), default="SUCCESS")
    request_id: Mapped[str] = mapped_column(sa.String(32))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True),
                                               default=lambda: datetime.now(UTC),
                                               server_default=sa.func.now())



def utc_now() -> datetime:
    return datetime.now(UTC)


class TacticsUser(Base):
    __tablename__ = "users"
    __table_args__ = (
        sa.CheckConstraint("username ~ '^[A-Za-z0-9_]{3,32}$'", name="ck_users_username"),
        sa.CheckConstraint("username_normalized = lower(username)", name="ck_users_normalized"),
        sa.CheckConstraint("char_length(display_name) BETWEEN 1 AND 32", name="ck_users_display"),
        sa.CheckConstraint("octet_length(recovery_digest) = 32", name="ck_users_recovery"),
        sa.CheckConstraint("credential_version >= 1", name="ck_users_credentials"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(sa.String(32))
    username_normalized: Mapped[str] = mapped_column(sa.String(32), unique=True)
    display_name: Mapped[str] = mapped_column(sa.String(32))
    password_hash: Mapped[str] = mapped_column(sa.Text)
    recovery_digest: Mapped[bytes] = mapped_column(sa.LargeBinary)
    credential_version: Mapped[int] = mapped_column(sa.BigInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class TacticsSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        sa.CheckConstraint("octet_length(session_digest) = 32", name="ck_user_sessions_digest"),
        sa.CheckConstraint("octet_length(csrf_nonce) = 32", name="ck_user_sessions_nonce"),
        sa.CheckConstraint("octet_length(csrf_digest) = 32", name="ck_user_sessions_csrf"),
        sa.CheckConstraint("expires_at > created_at", name="ck_user_sessions_expiry"),
        sa.CheckConstraint("credential_version >= 1", name="ck_user_sessions_credentials"),
        sa.Index("ix_user_sessions_user_revoked", "user_id", "revoked_at"),
        sa.Index("ix_user_sessions_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="RESTRICT"))
    session_digest: Mapped[bytes] = mapped_column(sa.LargeBinary, unique=True)
    csrf_nonce: Mapped[bytes] = mapped_column(sa.LargeBinary)
    csrf_digest: Mapped[bytes] = mapped_column(sa.LargeBinary)
    credential_version: Mapped[int] = mapped_column(sa.BigInteger)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="ck_teams_name"),
        sa.CheckConstraint("(status='active' AND archived_at IS NULL) OR "
                           "(status='archived' AND archived_at IS NOT NULL)",
                           name="ck_teams_archive"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(100))
    owner_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(sa.String(16), default="active")
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (sa.CheckConstraint("role IN ('editor','viewer')", name="ck_members_role"),)
    team_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("teams.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(sa.String(16), default="viewer")
    joined_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class TeamInvite(Base):
    __tablename__ = "team_invites"
    __table_args__ = (
        sa.CheckConstraint("octet_length(token_digest)=32", name="ck_invites_digest"),
        sa.CheckConstraint("max_uses BETWEEN 1 AND 20 AND uses BETWEEN 0 AND max_uses",
                           name="ck_invites_uses"),
        sa.Index("ix_team_invites_team_created", "team_id", "created_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("teams.id"))
    token_digest: Mapped[bytes] = mapped_column(sa.LargeBinary, unique=True)
    created_by: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    max_uses: Mapped[int] = mapped_column(sa.SmallInteger, default=20)
    uses: Mapped[int] = mapped_column(sa.SmallInteger, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class TeamInviteAcceptance(Base):
    __tablename__ = "team_invite_acceptances"
    invite_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("team_invites.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), primary_key=True)
    accepted_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class PersonalNotebook(Base):
    __tablename__ = "notebooks"
    __table_args__ = (sa.CheckConstraint(
        "(kind='personal' AND user_id IS NOT NULL AND team_id IS NULL) OR "
        "(kind='team' AND user_id IS NULL AND team_id IS NOT NULL)", name="ck_notebooks_kind"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(16), default="personal")
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="RESTRICT"), unique=True, nullable=True
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("teams.id", ondelete="RESTRICT"), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class Tactic(Base):
    __tablename__ = "tactics"
    __table_args__ = (
        sa.UniqueConstraint("notebook_id", "id", name="uq_tactics_notebook_id"),
        sa.CheckConstraint("char_length(title) BETWEEN 1 AND 100", name="ck_tactics_title"),
        sa.CheckConstraint("side IN ('T','CT')", name="ck_tactics_side"),
        sa.CheckConstraint("map = 'mirage'", name="ck_tactics_map"),
        sa.CheckConstraint("(status='active' AND archived_at IS NULL) OR "
                           "(status='archived' AND archived_at IS NOT NULL)",
                           name="ck_tactics_archive"),
        sa.Index("ix_tactics_notebook_status_updated", "notebook_id", "status", "updated_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    notebook_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("notebooks.id", ondelete="RESTRICT")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="RESTRICT"))
    source_attribution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    title: Mapped[str] = mapped_column(sa.String(100))
    side: Mapped[str] = mapped_column(sa.String(2))
    map: Mapped[str] = mapped_column(sa.String(16), default="mirage")
    status: Mapped[str] = mapped_column(sa.String(16), default="active")
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class TacticDocument(Base):
    __tablename__ = "tactic_documents"
    __table_args__ = (
        sa.CheckConstraint("schema_version=2", name="ck_tactic_documents_schema"),
        sa.CheckConstraint("revision BETWEEN 0 AND 9007199254740991", name="ck_tactic_revision"),
        sa.CheckConstraint("octet_length(yjs_state) BETWEEN 1 AND 4194304",
                           name="ck_tactic_documents_size"),
        sa.CheckConstraint("octet_length(state_digest)=32 AND octet_length(projection_digest)=32",
                           name="ck_tactic_documents_digests"),
    )
    tactic_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("tactics.id", ondelete="RESTRICT"), primary_key=True
    )
    schema_version: Mapped[int] = mapped_column(sa.SmallInteger, default=2)
    epoch: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    revision: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    yjs_state: Mapped[bytes] = mapped_column(sa.LargeBinary)
    projection: Mapped[dict] = mapped_column(JSONB)
    state_digest: Mapped[bytes] = mapped_column(sa.LargeBinary)
    projection_digest: Mapped[bytes] = mapped_column(sa.LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class TacticVersion(Base):
    __tablename__ = "tactic_versions"
    __table_args__ = (
        sa.Index("ix_tactic_versions_tactic_created", "tactic_id", "created_at"),
        sa.Index("ix_tactic_versions_expires", "expires_at"),
        sa.UniqueConstraint("tactic_id", "source_epoch", "source_revision",
                            name="uq_tactic_versions_revision"),
        sa.CheckConstraint("reason IN ('archive','publish','periodic')",
                           name="ck_tactic_versions_reason"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tactic_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tactics.id", ondelete="RESTRICT"))
    source_epoch: Mapped[uuid.UUID] = mapped_column()
    source_revision: Mapped[int] = mapped_column(sa.BigInteger)
    reason: Mapped[str] = mapped_column(sa.String(32))
    yjs_state: Mapped[bytes] = mapped_column(sa.LargeBinary)
    projection: Mapped[dict] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class ShareLink(Base):
    __tablename__ = "share_links"
    __table_args__ = (
        sa.ForeignKeyConstraint(["owner_notebook_id", "tactic_id"],
            ["tactics.notebook_id", "tactics.id"], name="fk_share_tactic_notebook"),
        sa.CheckConstraint("octet_length(token_digest)=32", name="ck_share_token_digest"),
        sa.ForeignKeyConstraint(["id", "current_version_id"],
            ["share_versions.share_link_id", "share_versions.id"],
            name="fk_share_current_version", use_alter=True),
        sa.Index("ix_share_links_notebook_created", "owner_notebook_id", "created_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_notebook_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("notebooks.id"))
    tactic_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("tactics.id"), nullable=True)
    token_digest: Mapped[bytes] = mapped_column(sa.LargeBinary, unique=True)
    published_by: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    allow_copy: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class ShareVersion(Base):
    __tablename__ = "share_versions"
    __table_args__ = (sa.UniqueConstraint("share_link_id", "id", name="uq_share_versions_owner"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    share_link_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("share_links.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    allow_copy: Mapped[bool] = mapped_column(default=False)
    manifest: Mapped[list] = mapped_column(JSONB)


class ShareVersionItem(Base):
    __tablename__ = "share_version_items"
    __table_args__ = (
        sa.UniqueConstraint("share_version_id", "order_index", name="uq_share_items_order"),
        sa.CheckConstraint("order_index BETWEEN 0 AND 99", name="ck_share_items_order"),
        sa.CheckConstraint("octet_length(state) BETWEEN 1 AND 4194304",
                           name="ck_share_items_state"),
    )
    share_version_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("share_versions.id"),
                                                       primary_key=True)
    source_tactic_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tactics.id"),
                                                      primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(unique=True, default=uuid.uuid4)
    order_index: Mapped[int] = mapped_column(sa.SmallInteger)
    source_epoch: Mapped[uuid.UUID] = mapped_column()
    source_revision: Mapped[int] = mapped_column(sa.BigInteger)
    projection: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[bytes] = mapped_column(sa.LargeBinary)
    attribution: Mapped[dict] = mapped_column(JSONB)


class ShareReadGrant(Base):
    __tablename__ = "share_read_grants"
    __table_args__ = (
        sa.CheckConstraint("octet_length(grant_digest)=32", name="ck_share_grant_digest"),
        sa.Index("ix_share_read_grants_expires", "expires_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    grant_digest: Mapped[bytes] = mapped_column(sa.LargeBinary, unique=True)
    share_link_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("share_links.id"))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class ApiIdempotency(Base):
    __tablename__ = "api_idempotency"
    __table_args__ = (
        sa.UniqueConstraint("actor_id", "scope", "key", name="uq_api_idempotency_actor_scope_key"),
        sa.CheckConstraint("octet_length(request_digest)=32", name="ck_api_idempotency_digest"),
        sa.Index("ix_api_idempotency_expires", "expires_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="RESTRICT"))
    scope: Mapped[str] = mapped_column(sa.String(128))
    key: Mapped[uuid.UUID] = mapped_column()
    request_digest: Mapped[bytes] = mapped_column(sa.LargeBinary)
    resource_id: Mapped[uuid.UUID] = mapped_column()
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))


class CollaborationTicket(Base):
    __tablename__ = "collaboration_tickets"
    __table_args__ = (
        sa.CheckConstraint("octet_length(ticket_digest)=32", name="ck_collab_ticket_digest"),
        sa.Index("ix_collab_ticket_expires", "expires_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    ticket_digest: Mapped[bytes] = mapped_column(sa.LargeBinary, unique=True)
    session_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("user_sessions.id"))
    tactic_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tactics.id"))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class CollaborationGrant(Base):
    __tablename__ = "collaboration_grants"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("collaboration_tickets.id"), unique=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("user_sessions.id"))
    tactic_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tactics.id"))
    node_instance_id: Mapped[uuid.UUID] = mapped_column()
    connection_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class TacticOperation(Base):
    __tablename__ = "tactic_operations"
    __table_args__ = (
        sa.UniqueConstraint("tactic_id", "epoch", "revision", name="uq_tactic_operation_revision"),
        sa.CheckConstraint("octet_length(fingerprint)=32", name="ck_tactic_operation_fingerprint"),
        sa.CheckConstraint("revision BETWEEN 1 AND 9007199254740991",
                           name="ck_tactic_operation_revision"),
    )
    tactic_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tactics.id"), primary_key=True)
    epoch: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    type: Mapped[str] = mapped_column(sa.String(32))
    fingerprint: Mapped[bytes] = mapped_column(sa.LargeBinary)
    revision: Mapped[int] = mapped_column(sa.BigInteger)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)


class TacticRecoveryItem(Base):
    __tablename__ = "tactic_recovery_items"
    __table_args__ = (
        sa.UniqueConstraint(
            "tactic_id", "source_epoch", "entity_id", name="uq_tactic_recovery_entity"
        ),
        sa.Index("ix_tactic_recovery_expires", "tactic_id", "expires_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tactic_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("tactics.id"))
    source_epoch: Mapped[uuid.UUID] = mapped_column()
    entity_id: Mapped[uuid.UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(sa.String(16))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    texts: Mapped[dict] = mapped_column(JSONB)
    deleted_by: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
