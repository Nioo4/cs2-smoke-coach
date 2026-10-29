"""Allow a Target or Origin to omit its map coordinate pair.

This migration is deliberately standalone: historical migrations must not
import the runtime ORM because later model changes would otherwise rewrite
their meaning.  The pair checks keep the two nullable columns all-or-nothing,
while the guarded downgrade refuses to make an existing NULL row invalid.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_optional_coordinates"
down_revision = "0002_descriptive_throws"
branch_labels = None
depends_on = None


_POINT_TABLES = ("targets", "origins")


def _lock_point_tables() -> None:
    # Use a fixed order so concurrent upgrade/downgrade attempts cannot
    # deadlock while changing the two catalog tables.
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in _POINT_TABLES:
        op.execute(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE')


def upgrade() -> None:
    _lock_point_tables()
    for table in _POINT_TABLES:
        op.alter_column(
            table,
            "map_x",
            existing_type=sa.Numeric(5, 2),
            nullable=True,
        )
        op.alter_column(
            table,
            "map_y",
            existing_type=sa.Numeric(5, 2),
            nullable=True,
        )
        op.create_check_constraint(
            f"ck_{table}_coordinate_pair",
            table,
            "(map_x IS NULL) = (map_y IS NULL)",
        )


def downgrade() -> None:
    _lock_point_tables()
    bind = op.get_bind()
    for table in _POINT_TABLES:
        has_null = bind.execute(
            sa.text(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" '
                "WHERE map_x IS NULL OR map_y IS NULL)"
            )
        ).scalar_one()
        if has_null:
            raise RuntimeError("OPTIONAL_COORDINATES_DOWNGRADE_BLOCKED")

    for table in _POINT_TABLES:
        op.drop_constraint(f"ck_{table}_coordinate_pair", table, type_="check")
        op.alter_column(
            table,
            "map_x",
            existing_type=sa.Numeric(5, 2),
            nullable=False,
        )
        op.alter_column(
            table,
            "map_y",
            existing_type=sa.Numeric(5, 2),
            nullable=False,
        )
