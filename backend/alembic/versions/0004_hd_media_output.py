"""Raise the tutorial video output limit for the v1.4 media contract.

This migration deliberately changes only the historical size check.  It does
not import the mutable ORM metadata or rewrite existing media rows.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_hd_media_output"
down_revision = "0003_optional_coordinates"
branch_labels = None
depends_on = None


_LEGACY_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_BYTES = 12 * 1024 * 1024


def _lock_videos() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('LOCK TABLE "tutorial_videos" IN ACCESS EXCLUSIVE MODE')


def _size_check(max_bytes: int) -> str:
    return f"size_bytes IS NULL OR size_bytes BETWEEN 1 AND {max_bytes}"


def upgrade() -> None:
    _lock_videos()
    op.drop_constraint("ck_video_size", "tutorial_videos", type_="check")
    op.create_check_constraint(
        "ck_video_size",
        "tutorial_videos",
        _size_check(_MAX_OUTPUT_BYTES),
    )


def downgrade() -> None:
    _lock_videos()
    blocked = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM tutorial_videos "
            "WHERE size_bytes IS NOT NULL AND size_bytes > :legacy_max"
            ")"
        ),
        {"legacy_max": _LEGACY_MAX_OUTPUT_BYTES},
    ).scalar_one()
    if blocked:
        raise RuntimeError("HD_MEDIA_DOWNGRADE_BLOCKED")

    op.drop_constraint("ck_video_size", "tutorial_videos", type_="check")
    op.create_check_constraint(
        "ck_video_size",
        "tutorial_videos",
        _size_check(_LEGACY_MAX_OUTPUT_BYTES),
    )
