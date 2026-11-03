"""Confirmed immutable history; periodic checkpoints belong to the backend lifecycle."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

import sqlalchemy as sa

from app.core.errors import AppError
from app.db.tactics_models import CollaborationGrant, TacticOperation, TacticVersion
from app.services import tactics_accounts as accounts
from app.services import tactics_audit as audit

logger = logging.getLogger("smokeline")


def version_view(version):
    return {
        "id": str(version.id),
        "source_epoch": str(version.source_epoch),
        "source_revision": version.source_revision,
        "reason": version.reason,
        "created_at": version.created_at.isoformat(),
        "expires_at": version.expires_at.isoformat(),
    }


async def save_history(db, document, actor_id, reason, *, audit_actor_id=None):
    await db.execute(
        sa.delete(TacticVersion).where(
            TacticVersion.tactic_id == document.tactic_id,
            TacticVersion.expires_at <= accounts.now(),
        )
    )
    prior = await db.scalar(
        sa.select(TacticVersion).where(
            TacticVersion.tactic_id == document.tactic_id,
            TacticVersion.source_epoch == document.epoch,
            TacticVersion.source_revision == document.revision,
        )
    )
    if prior:
        return prior
    at = accounts.now()
    version = TacticVersion(
        tactic_id=document.tactic_id,
        source_epoch=document.epoch,
        source_revision=document.revision,
        reason=reason,
        yjs_state=document.yjs_state,
        projection=document.projection,
        created_by=actor_id,
        expires_at=at + timedelta(days=30),
    )
    db.add(version)
    await db.flush()
    if reason == "periodic":
        await audit.append(db, "history.checkpoint", audit_actor_id,
                           tactic_id=document.tactic_id, resource_id=version.id)
    excess = (
        sa.select(TacticVersion.id)
        .where(TacticVersion.tactic_id == document.tactic_id)
        .order_by(TacticVersion.created_at.desc(), TacticVersion.id.desc())
        .offset(100)
    )
    await db.execute(
        sa.delete(TacticVersion).where(
            sa.or_(
                TacticVersion.id.in_(excess),
                sa.and_(
                    TacticVersion.tactic_id == document.tactic_id, TacticVersion.expires_at <= at
                ),
            )
        )
    )
    return version


async def checkpoint_active(*, clock=accounts.now):
    from app.services import tactics_collaboration as collaboration
    from app.services import tactics_documents as documents

    seen = set()
    count = 0
    after = None
    while documents.get_settings().tactics_enabled:
        async with accounts.get_session_factory()() as db:
            query = sa.select(CollaborationGrant.id).where(CollaborationGrant.closed_at.is_(None))
            if after is not None:
                query = query.where(CollaborationGrant.id > after)
            ids = (await db.scalars(query.order_by(CollaborationGrant.id).limit(100))).all()
        for identity in ids:
            if not documents.get_settings().tactics_enabled:
                return count
            try:
                async with collaboration.authorized_grant(identity) as (db, user, _, tactic, doc):
                    if tactic.id in seen or tactic._access_role == "viewer" or doc.revision == 0:
                        continue
                    seen.add(tactic.id)
                    latest = await db.scalar(
                        sa.select(TacticVersion)
                        .where(TacticVersion.tactic_id == tactic.id)
                        .order_by(TacticVersion.created_at.desc(), TacticVersion.id.desc())
                        .limit(1)
                    )
                    if (
                        latest
                        and latest.source_epoch == doc.epoch
                        and (latest.source_revision == doc.revision)
                    ):
                        continue
                    boundary = latest.created_at if latest else tactic.created_at
                    if clock() - boundary < timedelta(seconds=300):
                        continue
                    actor = await db.scalar(
                        sa.select(TacticOperation.actor_id).where(
                            TacticOperation.tactic_id == tactic.id,
                            TacticOperation.epoch == doc.epoch,
                            TacticOperation.revision == doc.revision,
                        )
                    )
                    await save_history(db, doc, actor or user.id, "periodic", audit_actor_id=actor)
                    count += 1
            except AppError as exc:
                if exc.status_code not in {401, 403, 404}:
                    raise
        if len(ids) < 100:
            return count
        after = ids[-1]
    return count


async def history_worker(settings, *, wait=asyncio.sleep):
    while True:
        await wait(15)
        if not settings.tactics_enabled:
            continue
        try:
            await checkpoint_active()
        except Exception:
            # No exception repr: database parameters, document text and secrets stay out of logs.
            logger.warning("tactics_history_checkpoint_failed")


async def stop_worker(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
