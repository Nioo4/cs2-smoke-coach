"""Allow the Phase 5B route-list response in usage events.

The migration only widens the historical response-type check.  It does not
rewrite usage rows or import the mutable runtime ORM metadata.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_route_matches_response"
down_revision = "0004_hd_media_output"
branch_labels = None
depends_on = None


_RESPONSE_TYPES = (
    "'match', 'route_matches', 'target_matches', 'clarification', "
    "'external_candidates', 'not_found', 'out_of_scope'"
)
_LEGACY_RESPONSE_TYPES = (
    "'match', 'target_matches', 'clarification', 'external_candidates', "
    "'not_found', 'out_of_scope'"
)


def _lock_usage_events() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('LOCK TABLE "usage_events" IN ACCESS EXCLUSIVE MODE')


def _response_check(values: str) -> str:
    return f"response_type IS NULL OR response_type IN ({values})"


def upgrade() -> None:
    _lock_usage_events()
    op.drop_constraint("ck_usage_response_type", "usage_events", type_="check")
    op.create_check_constraint(
        "ck_usage_response_type",
        "usage_events",
        _response_check(_RESPONSE_TYPES),
    )


def downgrade() -> None:
    _lock_usage_events()
    blocked = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM usage_events "
            "WHERE response_type = 'route_matches'"
            ")"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError("ROUTE_MATCHES_RESPONSE_DOWNGRADE_BLOCKED")

    op.drop_constraint("ck_usage_response_type", "usage_events", type_="check")
    op.create_check_constraint(
        "ck_usage_response_type",
        "usage_events",
        _response_check(_LEGACY_RESPONSE_TYPES),
    )
