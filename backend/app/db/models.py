"""The complete PostgreSQL schema defined by Spec section 9."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class LineupStatus(StrEnum):
    draft = "draft"
    published = "published"
    needs_revalidation = "needs_revalidation"
    archived = "archived"


class VideoStatus(StrEnum):
    ready = "ready"
    failed = "failed"
    archived = "archived"


LINEUP_STATUS = sa.Enum(
    LineupStatus,
    name="lineup_status_enum",
    native_enum=True,
    create_type=False,
    values_callable=lambda values: [item.value for item in values],
)
VIDEO_STATUS = sa.Enum(
    VideoStatus,
    name="video_status_enum",
    native_enum=True,
    create_type=False,
    values_callable=lambda values: [item.value for item in values],
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=utc_now,
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class PointFields:
    name_zh: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name_en: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    description: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, default="", server_default=sa.text("''")
    )
    map_x: Mapped[float | None] = mapped_column(sa.Numeric(5, 2), nullable=True)
    map_y: Mapped[float | None] = mapped_column(sa.Numeric(5, 2), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default=sa.text("true")
    )


class Target(PointFields, TimestampMixin, Base):
    __tablename__ = "targets"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)

    __table_args__ = (
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_targets_slug"),
        sa.CheckConstraint("char_length(name_zh) BETWEEN 1 AND 64", name="ck_targets_name_zh"),
        sa.CheckConstraint(
            "name_en IS NULL OR char_length(name_en) BETWEEN 1 AND 64",
            name="ck_targets_name_en",
        ),
        sa.CheckConstraint("char_length(description) <= 500", name="ck_targets_description"),
        sa.CheckConstraint("map_x >= 0 AND map_x <= 100", name="ck_targets_map_x"),
        sa.CheckConstraint("map_y >= 0 AND map_y <= 100", name="ck_targets_map_y"),
        sa.CheckConstraint("(map_x IS NULL) = (map_y IS NULL)", name="ck_targets_coordinate_pair"),
        sa.CheckConstraint("sort_order BETWEEN 0 AND 9999", name="ck_targets_sort_order"),
    )


class TargetAlias(Base):
    __tablename__ = "target_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("target_id", "normalized_alias", name="uq_target_alias_normalized"),
        sa.Index("ix_target_aliases_normalized_alias", "normalized_alias"),
        sa.CheckConstraint("char_length(alias) BETWEEN 1 AND 64", name="ck_target_alias_alias"),
        sa.CheckConstraint(
            "char_length(normalized_alias) BETWEEN 1 AND 64", name="ck_target_alias_normalized"
        ),
    )


class Origin(PointFields, TimestampMixin, Base):
    __tablename__ = "origins"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)

    __table_args__ = (
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_origins_slug"),
        sa.CheckConstraint("char_length(name_zh) BETWEEN 1 AND 64", name="ck_origins_name_zh"),
        sa.CheckConstraint(
            "name_en IS NULL OR char_length(name_en) BETWEEN 1 AND 64",
            name="ck_origins_name_en",
        ),
        sa.CheckConstraint("char_length(description) <= 500", name="ck_origins_description"),
        sa.CheckConstraint("map_x >= 0 AND map_x <= 100", name="ck_origins_map_x"),
        sa.CheckConstraint("map_y >= 0 AND map_y <= 100", name="ck_origins_map_y"),
        sa.CheckConstraint("(map_x IS NULL) = (map_y IS NULL)", name="ck_origins_coordinate_pair"),
        sa.CheckConstraint("sort_order BETWEEN 0 AND 9999", name="ck_origins_sort_order"),
    )


class OriginAlias(Base):
    __tablename__ = "origin_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    origin_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("origins.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("origin_id", "normalized_alias", name="uq_origin_alias_normalized"),
        sa.Index("ix_origin_aliases_normalized_alias", "normalized_alias"),
        sa.CheckConstraint("char_length(alias) BETWEEN 1 AND 64", name="ck_origin_alias_alias"),
        sa.CheckConstraint(
            "char_length(normalized_alias) BETWEEN 1 AND 64", name="ck_origin_alias_normalized"
        ),
    )


class Lineup(TimestampMixin, Base):
    __tablename__ = "lineups"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("targets.id", ondelete="RESTRICT"), nullable=False
    )
    origin_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("origins.id", ondelete="RESTRICT"), nullable=False
    )
    setup_text: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="")
    aim_text: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="")
    throw_text: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="")
    result_text: Mapped[str] = mapped_column(sa.Text(), nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.text("false")
    )
    default_needs_review: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.text("false")
    )
    sort_order: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    status: Mapped[LineupStatus] = mapped_column(
        LINEUP_STATUS, nullable=False, default=LineupStatus.draft, server_default="draft"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    game_version_note: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("char_length(setup_text) <= 1000", name="ck_lineups_setup"),
        sa.CheckConstraint("char_length(aim_text) <= 1000", name="ck_lineups_aim"),
        sa.CheckConstraint("char_length(throw_text) <= 1000", name="ck_lineups_throw"),
        sa.CheckConstraint("char_length(result_text) <= 1000", name="ck_lineups_result"),
        sa.CheckConstraint("sort_order BETWEEN 0 AND 9999", name="ck_lineups_sort_order"),
        sa.CheckConstraint(
            "NOT is_default OR status = 'published'", name="ck_lineups_default_status"
        ),
        sa.CheckConstraint(
            "NOT default_needs_review OR is_default", name="ck_lineups_default_review"
        ),
        sa.Index("ix_lineups_target_id", "target_id"),
        sa.Index("ix_lineups_origin_id", "origin_id"),
        sa.Index("ix_lineups_status", "status"),
        sa.Index(
            "uq_lineups_published_default",
            "target_id",
            "origin_id",
            unique=True,
            postgresql_where=sa.text("status = 'published' AND is_default = true"),
        ),
    )


class TutorialVideo(TimestampMixin, Base):
    __tablename__ = "tutorial_videos"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lineup_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("lineups.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[VideoStatus] = mapped_column(VIDEO_STATUS, nullable=False)
    storage_key: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(as_uuid=True), unique=True)
    relative_path: Mapped[str | None] = mapped_column(sa.Text())
    poster_relative_path: Mapped[str | None] = mapped_column(sa.Text())
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer())
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger())
    checksum_sha256: Mapped[str | None] = mapped_column(sa.CHAR(64))
    rights_note: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(sa.Text())

    __table_args__ = (
        sa.Index("ix_tutorial_videos_lineup_id", "lineup_id"),
        sa.Index("ix_tutorial_videos_status", "status"),
        sa.CheckConstraint(
            "char_length(btrim(rights_note)) BETWEEN 1 AND 500", name="ck_video_rights"
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms BETWEEN 1 AND 20000", name="ck_video_duration"
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes BETWEEN 1 AND 12582912", name="ck_video_size"
        ),
        sa.CheckConstraint(
            "checksum_sha256 IS NULL OR checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_video_checksum",
        ),
        sa.CheckConstraint(
            "("
            "status IN ('ready', 'archived') "
            "AND storage_key IS NOT NULL "
            "AND relative_path IS NOT NULL "
            "AND poster_relative_path IS NOT NULL "
            "AND duration_ms IS NOT NULL "
            "AND size_bytes IS NOT NULL "
            "AND checksum_sha256 IS NOT NULL "
            "AND failure_reason IS NULL"
            ") OR ("
            "status = 'failed' "
            "AND storage_key IS NULL "
            "AND relative_path IS NULL "
            "AND poster_relative_path IS NULL "
            "AND duration_ms IS NULL "
            "AND size_bytes IS NULL "
            "AND checksum_sha256 IS NULL "
            "AND failure_reason IS NOT NULL "
            "AND failure_reason IN ("
            "'processing_failed', 'processing_timeout', 'output_too_large', "
            "'media_missing', 'media_corrupt'"
            ")"
            ")",
            name="ck_video_state",
        ),
    )


class ConversationState(Base):
    __tablename__ = "conversation_states"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    map: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    grenade: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    target_slug: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    origin_slug: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    pending_field: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    pending_options: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        sa.Index("ix_conversation_states_expires_at", "expires_at"),
        sa.CheckConstraint("map IS NULL OR map = 'mirage'", name="ck_conversation_map"),
        sa.CheckConstraint("grenade IS NULL OR grenade = 'smoke'", name="ck_conversation_grenade"),
        sa.CheckConstraint(
            "(pending_field IS NULL AND pending_options IS NULL) OR "
            "(pending_field IS NOT NULL AND pending_field = 'target' "
            "AND pending_options IS NOT NULL "
            "AND CASE WHEN jsonb_typeof(pending_options) = 'array' "
            "THEN jsonb_array_length(pending_options) BETWEEN 1 AND 6 ELSE false END "
            "AND pending_options::text ~ "
            r"'^\[\"[a-z0-9]+(?:-[a-z0-9]+)*\"(?:, \"[a-z0-9]+(?:-[a-z0-9]+)*\")*\]$'",
            name="ck_conversation_pending",
        ),
    )


class ExternalSearchCache(Base):
    __tablename__ = "external_search_cache"

    cache_key: Mapped[str] = mapped_column(sa.String(256), primary_key=True)
    payload: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    is_empty: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=utc_now,
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        sa.Index("ix_external_search_cache_expires_at", "expires_at"),
        sa.CheckConstraint("char_length(cache_key) BETWEEN 1 AND 256", name="ck_search_cache_key"),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(payload) = 'array' "
            "THEN jsonb_array_length(payload) BETWEEN 0 AND 2 ELSE false END",
            name="ck_search_cache_payload",
        ),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(payload) = 'array' "
            "THEN is_empty = (jsonb_array_length(payload) = 0) ELSE false END",
            name="ck_search_cache_empty",
        ),
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    input_mode: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    response_type: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    target_slug: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    origin_slug: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    provider_type: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    reserved_units: Mapped[float] = mapped_column(
        sa.Numeric(10, 2), nullable=False, default=0, server_default=sa.text("0")
    )
    provider_outcome: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    input_tokens: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default=sa.text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, default=0, server_default=sa.text("0")
    )
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=utc_now,
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        sa.Index("ix_usage_events_request_id", "request_id"),
        sa.Index("ix_usage_events_created_at", "created_at"),
        sa.Index("ix_usage_events_event_type", "event_type"),
        sa.CheckConstraint(
            "event_type IN ('public_request', 'provider_reservation', 'admin_action')",
            name="ck_usage_event_type",
        ),
        sa.CheckConstraint(
            "input_mode IN ('map', 'text', 'voice', 'admin')", name="ck_usage_input_mode"
        ),
        sa.CheckConstraint(
            "response_type IS NULL OR response_type IN ("
            "'match', 'route_matches', 'target_matches', 'clarification', 'external_candidates', "
            "'not_found', 'out_of_scope'"
            ")",
            name="ck_usage_response_type",
        ),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_usage_latency"),
        sa.CheckConstraint(
            "provider_type IS NULL OR provider_type IN ('intent', 'web_search', 'asr')",
            name="ck_usage_provider_type",
        ),
        sa.CheckConstraint(
            "provider_outcome IS NULL OR provider_outcome IN ('reserved', 'success', 'failed')",
            name="ck_usage_provider_outcome",
        ),
        sa.CheckConstraint("reserved_units >= 0", name="ck_usage_reserved_units"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="ck_usage_tokens"),
        sa.CheckConstraint(
            "(event_type = 'provider_reservation' "
            "AND provider_type IS NOT NULL "
            "AND provider_outcome IS NOT NULL "
            "AND reserved_units > 0) OR ("
            "event_type <> 'provider_reservation' "
            "AND provider_type IS NULL "
            "AND provider_outcome IS NULL "
            "AND reserved_units = 0 "
            "AND input_tokens = 0 "
            "AND output_tokens = 0"
            ")",
            name="ck_usage_provider_fields",
        ),
    )
