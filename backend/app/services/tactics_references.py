"""Tactic references reuse the teaching catalog's single public-visibility query."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.core.errors import AppError
from app.schemas.public import LineupDetail
from app.services.local_search import available_rows, detail_from_row

# Existing admin/reconcile writers take the exclusive advisory lock with this key.
CONTENT_WRITE_LOCK_KEY = 1397575499


def referenced_ids(projection):
    return {identity for stage in projection["structure"]["stages"]
            for identity in stage["lineup_ids"]}


async def check_new_references(db, old_projection, next_projection, recovery=None):
    restored = set()
    if recovery is not None and recovery.kind == "stage":
        restored = set(recovery.snapshot["stage"]["lineup_ids"])
    added = referenced_ids(next_projection) - referenced_ids(old_projection) - restored
    if not added:
        return
    # Serialize this visibility check with publication/unpublication, without writing catalog rows.
    await db.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    await db.execute(sa.text("SELECT pg_advisory_xact_lock_shared(:key)"),
                     {"key": CONTENT_WRITE_LOCK_KEY})
    visible = {str(row.lineup.id) for row in await available_rows(db)}
    if not added <= visible:
        raise AppError(422, "TACTIC_LINEUP_UNAVAILABLE", "部分教学引用暂不可用，请重新选择。")


async def resolve_reference(db, lineup_id: uuid.UUID):
    rows = await available_rows(db)
    current = next((row for row in rows if row.lineup.id == lineup_id), None)
    if current is None:
        return {"id": str(lineup_id), "available": False, "lineup": None}
    group = [row for row in rows if row.target.id == current.target.id
             and row.origin.id == current.origin.id]
    lineup = LineupDetail.model_validate(detail_from_row(current, group)).model_dump(mode="json")
    return {"id": str(lineup_id), "available": True, "lineup": lineup}
