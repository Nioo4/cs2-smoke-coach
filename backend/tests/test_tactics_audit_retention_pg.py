"""Actual 0011 metadata and exact retention helper check; no maintenance scope bypass."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
import test_tactics_audit_pg as core

from app.db.tactics_models import TacticAuditEvent
from app.services import tactics_maintenance as maintenance

database_url, event_loop_policy = core.database_url, core.event_loop_policy
pytestmark = pytest.mark.integration


async def test_0011_real_schema_and_30_day_helper_before_after(database_url):
    engine = maintenance.create_async_engine(database_url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                assert await maintenance.formal_schema(connection) == "0011_tactics_audit_events"
                at = datetime.now(UTC)
                old, fresh = uuid.uuid4(), uuid.uuid4()
                for identity, created in [
                    (old, at - timedelta(days=30)),
                    (fresh, at - timedelta(days=30) + timedelta(microseconds=1)),
                ]:
                    await connection.execute(
                        TacticAuditEvent.__table__.insert().values(
                            id=identity,
                            action="account.login",
                            result_code="SUCCESS",
                            request_id=uuid.uuid4().hex,
                            created_at=created,
                        )
                    )
                before = await maintenance.cleanup_plan(connection, at, with_audit=True)
                assert before["tactic_audit_events"] == 1
                selected = (
                    (
                        await connection.execute(
                            sa.text(
                                "SELECT id FROM tactic_audit_events "
                                "WHERE created_at<=:at - INTERVAL '30 days' "
                                "ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"
                            ),
                            {"at": at},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert selected == [old]
                assert (
                    await maintenance._delete_ids(connection, "tactic_audit_events", selected) == 1
                )
                after = await maintenance.cleanup_plan(connection, at, with_audit=True)
                assert after["tactic_audit_events"] == 0
                assert (
                    await connection.scalar(
                        sa.select(TacticAuditEvent.id).where(TacticAuditEvent.id == fresh)
                    )
                    == fresh
                )
            finally:
                # Roll back only this test transaction; keep all preexisting fixture rows.
                await transaction.rollback()
    finally:
        await engine.dispose()
