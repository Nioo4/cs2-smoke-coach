"""Make throw instructions descriptive data, without rewriting history.

The first migration is intentionally immutable.  This migration only changes
the historical ``lineups.throw_method_id`` column and the old identity
constraint; the old tables, columns, foreign keys, and rows remain in place
for legacy data and a guarded downgrade.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_descriptive_throws"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _lock_lineups() -> None:
    # The lock is acquired before either DDL or downgrade validation.  A
    # blocked migration therefore fails within the documented bounded window.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE lineups IN ACCESS EXCLUSIVE MODE")


def upgrade() -> None:
    _lock_lineups()
    op.drop_constraint("uq_lineup_identity", "lineups", type_="unique")
    op.alter_column(
        "lineups",
        "throw_method_id",
        existing_type=sa.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    _lock_lineups()
    bind = op.get_bind()
    blocked = bind.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM lineups
                WHERE throw_method_id IS NULL
            )
            OR EXISTS (
                SELECT 1
                FROM lineups
                GROUP BY target_id, origin_id, throw_method_id
                HAVING COUNT(*) > 1
            )
            """
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError("DESCRIPTIVE_THROW_DOWNGRADE_BLOCKED")
    op.alter_column(
        "lineups",
        "throw_method_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_lineup_identity", "lineups", ["target_id", "origin_id", "throw_method_id"]
    )
