"""Frozen initial PostgreSQL schema and the ten initial throw methods.

This migration intentionally owns its DDL. It must not import the mutable ORM
metadata: a later model change is a new migration, never a rewrite of history.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


LINEUP_STATUS = postgresql.ENUM(
    "draft",
    "published",
    "needs_revalidation",
    "archived",
    name="lineup_status_enum",
    create_type=False,
)
VIDEO_STATUS = postgresql.ENUM(
    "ready", "failed", "archived", name="video_status_enum", create_type=False
)


def _normalize_seed(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        char if (char.isalnum() or char.isspace()) else " " for char in normalized
    )
    return re.sub(r"\s+", " ", normalized).strip()


def upgrade() -> None:
    op.execute(
        "CREATE TYPE lineup_status_enum AS ENUM "
        "('draft', 'published', 'needs_revalidation', 'archived')"
    )
    op.execute("CREATE TYPE video_status_enum AS ENUM ('ready', 'failed', 'archived')")

    op.create_table(
        "targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name_zh", sa.String(64), nullable=False),
        sa.Column("name_en", sa.String(64)),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("map_x", sa.Numeric(5, 2), nullable=False),
        sa.Column("map_y", sa.Numeric(5, 2), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("slug", name="uq_targets_slug"),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_targets_slug"),
        sa.CheckConstraint("char_length(name_zh) BETWEEN 1 AND 64", name="ck_targets_name_zh"),
        sa.CheckConstraint(
            "name_en IS NULL OR char_length(name_en) BETWEEN 1 AND 64", name="ck_targets_name_en"
        ),
        sa.CheckConstraint("char_length(description) <= 500", name="ck_targets_description"),
        sa.CheckConstraint("map_x >= 0 AND map_x <= 100", name="ck_targets_map_x"),
        sa.CheckConstraint("map_y >= 0 AND map_y <= 100", name="ck_targets_map_y"),
        sa.CheckConstraint("sort_order BETWEEN 0 AND 9999", name="ck_targets_sort_order"),
    )
    op.create_table(
        "target_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "target_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("targets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("normalized_alias", sa.String(64), nullable=False),
        sa.UniqueConstraint("target_id", "normalized_alias", name="uq_target_alias_normalized"),
        sa.CheckConstraint("char_length(alias) BETWEEN 1 AND 64", name="ck_target_alias_alias"),
        sa.CheckConstraint(
            "char_length(normalized_alias) BETWEEN 1 AND 64", name="ck_target_alias_normalized"
        ),
    )
    op.create_index("ix_target_aliases_normalized_alias", "target_aliases", ["normalized_alias"])

    op.create_table(
        "origins",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name_zh", sa.String(64), nullable=False),
        sa.Column("name_en", sa.String(64)),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("map_x", sa.Numeric(5, 2), nullable=False),
        sa.Column("map_y", sa.Numeric(5, 2), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("slug", name="uq_origins_slug"),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_origins_slug"),
        sa.CheckConstraint("char_length(name_zh) BETWEEN 1 AND 64", name="ck_origins_name_zh"),
        sa.CheckConstraint(
            "name_en IS NULL OR char_length(name_en) BETWEEN 1 AND 64", name="ck_origins_name_en"
        ),
        sa.CheckConstraint("char_length(description) <= 500", name="ck_origins_description"),
        sa.CheckConstraint("map_x >= 0 AND map_x <= 100", name="ck_origins_map_x"),
        sa.CheckConstraint("map_y >= 0 AND map_y <= 100", name="ck_origins_map_y"),
        sa.CheckConstraint("sort_order BETWEEN 0 AND 9999", name="ck_origins_sort_order"),
    )
    op.create_table(
        "origin_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "origin_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("origins.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("normalized_alias", sa.String(64), nullable=False),
        sa.UniqueConstraint("origin_id", "normalized_alias", name="uq_origin_alias_normalized"),
        sa.CheckConstraint("char_length(alias) BETWEEN 1 AND 64", name="ck_origin_alias_alias"),
        sa.CheckConstraint(
            "char_length(normalized_alias) BETWEEN 1 AND 64", name="ck_origin_alias_normalized"
        ),
    )
    op.create_index("ix_origin_aliases_normalized_alias", "origin_aliases", ["normalized_alias"])

    op.create_table(
        "throw_methods",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name_zh", sa.String(64), nullable=False),
        sa.Column("search_term_en", sa.String(128), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("slug", name="uq_throw_methods_slug"),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_throw_methods_slug"
        ),
        sa.CheckConstraint(
            "char_length(name_zh) BETWEEN 1 AND 64", name="ck_throw_methods_name_zh"
        ),
        sa.CheckConstraint(
            "char_length(search_term_en) BETWEEN 1 AND 128", name="ck_throw_methods_search_en"
        ),
        sa.CheckConstraint("sort_order BETWEEN 0 AND 9999", name="ck_throw_methods_sort_order"),
    )
    op.create_table(
        "throw_method_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "throw_method_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("throw_methods.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("normalized_alias", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "throw_method_id", "normalized_alias", name="uq_throw_method_alias_normalized"
        ),
        sa.CheckConstraint(
            "char_length(alias) BETWEEN 1 AND 64", name="ck_throw_method_alias_alias"
        ),
        sa.CheckConstraint(
            "char_length(normalized_alias) BETWEEN 1 AND 64",
            name="ck_throw_method_alias_normalized",
        ),
    )
    op.create_index(
        "ix_throw_method_aliases_normalized_alias", "throw_method_aliases", ["normalized_alias"]
    )

    op.create_table(
        "lineups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "target_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("targets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "origin_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("origins.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "throw_method_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("throw_methods.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("setup_text", sa.Text(), nullable=False),
        sa.Column("aim_text", sa.Text(), nullable=False),
        sa.Column("throw_text", sa.Text(), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "default_needs_review", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", LINEUP_STATUS, nullable=False, server_default=sa.text("'draft'")),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("game_version_note", sa.String(128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("target_id", "origin_id", "throw_method_id", name="uq_lineup_identity"),
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
    )
    op.create_index("ix_lineups_target_id", "lineups", ["target_id"])
    op.create_index("ix_lineups_origin_id", "lineups", ["origin_id"])
    op.create_index("ix_lineups_throw_method_id", "lineups", ["throw_method_id"])
    op.create_index("ix_lineups_status", "lineups", ["status"])
    op.create_index(
        "uq_lineups_published_default",
        "lineups",
        ["target_id", "origin_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published' AND is_default = true"),
    )

    op.create_table(
        "tutorial_videos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lineup_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("lineups.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", VIDEO_STATUS, nullable=False),
        sa.Column("storage_key", postgresql.UUID(as_uuid=True), unique=True),
        sa.Column("relative_path", sa.Text()),
        sa.Column("poster_relative_path", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("checksum_sha256", sa.CHAR(64)),
        sa.Column("rights_note", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("lineup_id", name="uq_tutorial_videos_lineup_id"),
        sa.CheckConstraint(
            "char_length(btrim(rights_note)) BETWEEN 1 AND 500", name="ck_video_rights"
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms BETWEEN 1 AND 20000", name="ck_video_duration"
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes BETWEEN 1 AND 8388608", name="ck_video_size"
        ),
        sa.CheckConstraint(
            "checksum_sha256 IS NULL OR checksum_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_video_checksum",
        ),
        sa.CheckConstraint(
            "(status IN ('ready', 'archived') AND storage_key IS NOT NULL AND relative_path IS NOT NULL "
            "AND poster_relative_path IS NOT NULL AND duration_ms IS NOT NULL AND size_bytes IS NOT NULL "
            "AND checksum_sha256 IS NOT NULL AND failure_reason IS NULL) OR "
            "(status = 'failed' AND storage_key IS NULL AND relative_path IS NULL "
            "AND poster_relative_path IS NULL AND duration_ms IS NULL AND size_bytes IS NULL "
            "AND checksum_sha256 IS NULL AND failure_reason IS NOT NULL AND failure_reason IN "
            "('processing_failed', 'processing_timeout', 'output_too_large', 'media_missing', 'media_corrupt'))",
            name="ck_video_state",
        ),
    )
    op.create_index("ix_tutorial_videos_lineup_id", "tutorial_videos", ["lineup_id"])
    op.create_index("ix_tutorial_videos_status", "tutorial_videos", ["status"])

    op.create_table(
        "conversation_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("map", sa.String(32)),
        sa.Column("grenade", sa.String(32)),
        sa.Column("target_slug", sa.String(64)),
        sa.Column("origin_slug", sa.String(64)),
        sa.Column("throw_method_slug", sa.String(64)),
        sa.Column("pending_field", sa.String(16)),
        sa.Column("pending_options", postgresql.JSONB(none_as_null=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("map IS NULL OR map = 'mirage'", name="ck_conversation_map"),
        sa.CheckConstraint("grenade IS NULL OR grenade = 'smoke'", name="ck_conversation_grenade"),
        sa.CheckConstraint(
            "(pending_field IS NULL AND pending_options IS NULL) OR "
            "(pending_field IS NOT NULL AND pending_field = 'target' AND pending_options IS NOT NULL "
            "AND CASE WHEN jsonb_typeof(pending_options) = 'array' "
            "THEN jsonb_array_length(pending_options) BETWEEN 1 AND 6 ELSE false END "
            "AND pending_options::text ~ '^\\[\"[a-z0-9]+(?:-[a-z0-9]+)*\"(?:, \"[a-z0-9]+(?:-[a-z0-9]+)*\")*\\]$')",
            name="ck_conversation_pending",
        ),
    )
    op.create_index("ix_conversation_states_expires_at", "conversation_states", ["expires_at"])

    op.create_table(
        "external_search_cache",
        sa.Column("cache_key", sa.String(256), primary_key=True),
        sa.Column("payload", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("is_empty", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
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
    op.create_index("ix_external_search_cache_expires_at", "external_search_cache", ["expires_at"])

    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("input_mode", sa.String(16), nullable=False),
        sa.Column("response_type", sa.String(32)),
        sa.Column("target_slug", sa.String(64)),
        sa.Column("origin_slug", sa.String(64)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("provider_type", sa.String(16)),
        sa.Column("reserved_units", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("provider_outcome", sa.String(16)),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "event_type IN ('public_request', 'provider_reservation', 'admin_action')",
            name="ck_usage_event_type",
        ),
        sa.CheckConstraint(
            "input_mode IN ('map', 'text', 'voice', 'admin')", name="ck_usage_input_mode"
        ),
        sa.CheckConstraint(
            "response_type IS NULL OR response_type IN ('match', 'target_matches', 'clarification', 'external_candidates', 'not_found', 'out_of_scope')",
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
            "(event_type = 'provider_reservation' AND provider_type IS NOT NULL AND provider_outcome IS NOT NULL AND reserved_units > 0) OR "
            "(event_type <> 'provider_reservation' AND provider_type IS NULL AND provider_outcome IS NULL AND reserved_units = 0 AND input_tokens = 0 AND output_tokens = 0)",
            name="ck_usage_provider_fields",
        ),
    )
    op.create_index("ix_usage_events_request_id", "usage_events", ["request_id"])
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"])
    op.create_index("ix_usage_events_event_type", "usage_events", ["event_type"])

    now = datetime.now(timezone.utc)
    method_table = sa.table(
        "throw_methods",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("slug", sa.String(64)),
        sa.column("name_zh", sa.String(64)),
        sa.column("search_term_en", sa.String(128)),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    alias_table = sa.table(
        "throw_method_aliases",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("throw_method_id", postgresql.UUID(as_uuid=True)),
        sa.column("alias", sa.String(64)),
        sa.column("normalized_alias", sa.String(64)),
    )
    seeds = (
        (10, "left-click", "左键站投", "left click throw", ("左键", "站投", "普通投掷", "left click")),
        (20, "right-click", "右键轻抛", "right click throw", ("右键", "轻抛", "right click")),
        (30, "both-click", "双键投掷", "both mouse buttons throw", ("双键", "左右键", "both buttons")),
        (40, "jump-throw", "原地跳投", "standing jump throw", ("跳投", "跳扔", "jump throw", "jumpthrow")),
        (50, "crouch-jump-throw", "蹲着跳投", "crouch jump throw", ("蹲跳投", "蹲跳", "crouch jump")),
        (60, "w-jump-throw", "W+跳投", "forward jump throw", ("W跳投", "W 跳投", "前进跳投", "前跳投")),
        (70, "a-jump-throw", "A跳投", "left strafe jump throw", ("A 跳投", "左移跳投")),
        (80, "d-jump-throw", "D跳投", "right strafe jump throw", ("D 跳投", "右移跳投")),
        (90, "run-throw", "跑投", "run throw", ("跑扔", "助跑投", "run throw")),
        (100, "walk-throw", "走投", "walk throw", ("走扔", "静步投", "walk throw")),
    )
    method_rows = []
    alias_rows = []
    for sort_order, slug, name_zh, search_term_en, aliases in seeds:
        method_id = uuid.uuid4()
        method_rows.append(
            {
                "id": method_id,
                "slug": slug,
                "name_zh": name_zh,
                "search_term_en": search_term_en,
                "sort_order": sort_order,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        alias_rows.extend(
            {
                "id": uuid.uuid4(),
                "throw_method_id": method_id,
                "alias": alias,
                "normalized_alias": _normalize_seed(alias),
            }
            for alias in aliases
        )
    op.bulk_insert(method_table, method_rows)
    op.bulk_insert(alias_table, alias_rows)


def downgrade() -> None:
    for name in (
        "usage_events",
        "external_search_cache",
        "conversation_states",
        "tutorial_videos",
        "lineups",
        "throw_method_aliases",
        "throw_methods",
        "origin_aliases",
        "origins",
        "target_aliases",
        "targets",
    ):
        op.drop_table(name)
    op.execute("DROP TYPE video_status_enum")
    op.execute("DROP TYPE lineup_status_enum")
