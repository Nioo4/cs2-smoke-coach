"""Immutable published snapshots with source-domain authorization and bounded copy spool."""

import asyncio
import base64
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa

from app.core.errors import AppError
from app.db.tactics_models import (
    PersonalNotebook,
    ShareLink,
    ShareReadGrant,
    ShareVersion,
    ShareVersionItem,
    Tactic,
    TacticDocument,
    TacticsUser,
    Team,
)
from app.services import tactics_accounts as accounts
from app.services import tactics_documents as documents
from app.services import tactics_history as history
from app.services.media import MediaLockTimeout, _MediaLock, _prepare_safe_directory

_COPY_SLOT = asyncio.Semaphore(1)
_SPOOL_DIRECTORY = "tactics-share-copy-v1"
_SPOOL_MARKER = ".owner.json"
_SPOOL_KIND = "smokeline-share-copy-v1"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_BATCH_NAME = re.compile(
    r"^batch-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_SPOOL_FILE = re.compile(r"^(?:[0-9]|[1-9][0-9])\.(?:state|projection)$")


def _spool_error(code="SHARE_COPY_SPOOL_UNVERIFIED"):
    return AppError(503, code, "复制临时目录需要人工核查，请联系维护人员。")


def _spool_info(path, *, directory):
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    if stat.S_ISLNK(info.st_mode) or reparse or not (
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    ):
        raise _spool_error("SHARE_COPY_SPOOL_UNSAFE")
    if os.name == "posix" and info.st_uid != os.geteuid():
        raise _spool_error("SHARE_COPY_SPOOL_UNSAFE")
    if not directory and info.st_nlink != 1:
        raise _spool_error("SHARE_COPY_SPOOL_UNSAFE")
    return info


def _share_copy_root(root):
    base = Path(root).absolute()
    if ".." in base.parts or not _prepare_safe_directory(base, create=False):
        raise _spool_error("SHARE_COPY_SPOOL_UNSAFE")
    # Reuse the media directory check, additionally rejecting Windows junction/reparse points.
    for component in [*reversed(base.parents), base]:
        if getattr(component.lstat(), "st_file_attributes", 0) & _REPARSE_POINT:
            raise _spool_error("SHARE_COPY_SPOOL_UNSAFE")
    private = base / _SPOOL_DIRECTORY
    try:
        private.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = _spool_info(private, directory=True)
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700:
        raise _spool_error("SHARE_COPY_SPOOL_UNSAFE")
    return private


def _verified_batch(directory):
    match = _BATCH_NAME.fullmatch(directory.name)
    if not match:
        raise _spool_error()
    info = _spool_info(directory, directory=True)
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700:
        raise _spool_error("SHARE_COPY_SPOOL_UNSAFE")
    children = list(directory.iterdir())
    for child in children:
        if child.name != _SPOOL_MARKER and not _SPOOL_FILE.fullmatch(child.name):
            raise _spool_error()
        _spool_info(child, directory=False)
    marker = directory / _SPOOL_MARKER
    try:
        if _spool_info(marker, directory=False).st_size > 256:
            raise _spool_error()
        expected = {"kind": _SPOOL_KIND, "version": 1, "batch_id": match[1]}
        payload = json.loads(marker.read_bytes())
        if payload != expected or type(payload.get("version")) is not int:
            raise _spool_error()
    except (FileNotFoundError, ValueError):
        raise _spool_error() from None
    return children


def _remove_verified_batch(directory):
    # No recursive deletion: every direct entry was checked as an owned regular file.
    for child in _verified_batch(directory):
        if child.name != _SPOOL_MARKER:
            child.unlink()
    # Keep proof until all payload files are gone, including across an interrupted cleanup.
    (directory / _SPOOL_MARKER).unlink()
    directory.rmdir()


@asynccontextmanager
async def _share_copy_batch(root):
    try:
        private = _share_copy_root(root)
        lock_path = private / ".copy.lock"
        try:
            _spool_info(lock_path, directory=False)
        except FileNotFoundError:
            pass
        async with _MediaLock(lock_path, timeout_seconds=0):
            _spool_info(lock_path, directory=False)
            # _MediaLock only checks/creates parents; it never widens existing 0700 modes.
            _share_copy_root(root)
            stale = [entry for entry in private.iterdir() if entry.name != ".copy.lock"]
            for directory in stale:
                _verified_batch(directory)  # Validate all candidates before removing any.
            for directory in stale:
                _remove_verified_batch(directory)
            batch_id = str(uuid.uuid4())
            directory = private / f"batch-{batch_id}"
            directory.mkdir(mode=0o700)
            marker = directory / _SPOOL_MARKER
            marker.write_text(
                json.dumps({"kind": _SPOOL_KIND, "version": 1, "batch_id": batch_id}),
                encoding="utf-8",
            )
            os.chmod(marker, 0o600)
            try:
                yield directory
            finally:
                _remove_verified_batch(directory)
    except MediaLockTimeout:
        raise AppError(503, "COLLAB_UNAVAILABLE", "复制任务正忙，请稍后重试。") from None
    except OSError:
        raise _spool_error("SHARE_COPY_SPOOL_IO_FAILED") from None


@asynccontextmanager
async def copy_spool_context(factory, version_id, root):
    if _COPY_SLOT.locked():
        raise AppError(503, "COLLAB_UNAVAILABLE", "复制任务正忙，请稍后重试。")
    await _COPY_SLOT.acquire()
    try:
        async with _share_copy_batch(root) as directory:
            async with factory() as db:
                count = await db.scalar(
                    sa.select(sa.func.count())
                    .select_from(ShareVersionItem)
                    .where(ShareVersionItem.share_version_id == version_id)
                )
            if not 1 <= count <= 100:
                raise unavailable()
            if shutil.disk_usage(directory).free < count * 6 * 1024 * 1024 + 16 * 1024 * 1024:
                raise AppError(503, "COLLAB_UNAVAILABLE", "临时空间不足，请稍后重试。")
            yield await spool_copies(factory, version_id, directory)
    finally:
        _COPY_SLOT.release()


def unavailable():
    return AppError(404, "SHARE_UNAVAILABLE", "分享已失效或暂不可访问。")


def changed():
    return AppError(409, "SHARE_VERSION_CHANGED", "分享已更新，请刷新确认后重试。")


def summary(link):
    return {
        "id": str(link.id),
        "notebook_id": str(link.owner_notebook_id),
        "tactic_id": str(link.tactic_id) if link.tactic_id else None,
        "version_id": str(link.current_version_id),
        "allow_copy": link.allow_copy,
        "created_at": link.created_at.isoformat(),
        "updated_at": link.updated_at.isoformat(),
        "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
    }


async def lock_notebooks(db, ids):
    rows = (
        await db.scalars(sa.select(PersonalNotebook).where(PersonalNotebook.id.in_(set(ids))))
    ).all()
    if len(rows) != len(set(ids)):
        raise unavailable()
    for team_id in sorted({row.team_id for row in rows if row.team_id}):
        team = await db.scalar(sa.select(Team).where(Team.id == team_id).with_for_update())
        if team.status != "active":
            raise unavailable()
    result = {}
    for row in sorted(rows, key=lambda item: item.id):
        result[row.id] = await db.scalar(
            sa.select(PersonalNotebook).where(PersonalNotebook.id == row.id).with_for_update()
        )
    return result


async def lock_link(
    db, *, identity=None, token=None, grant=None, target_id=None, allow_revoked=False
):
    read_grant = None
    if grant is not None:
        read_grant = await db.scalar(
            sa.select(ShareReadGrant).where(ShareReadGrant.grant_digest == accounts.digest(grant))
        )
        if read_grant is None or read_grant.expires_at <= accounts.now():
            raise unavailable()
        identity = read_grant.share_link_id
    query = sa.select(ShareLink)
    query = (
        query.where(ShareLink.token_digest == accounts.digest(token))
        if token
        else (query.where(ShareLink.id == identity))
    )
    located = await db.scalar(query)
    if located is None:
        raise unavailable()
    await lock_notebooks(db, [located.owner_notebook_id] + ([target_id] if target_id else []))
    # Pointer writers require the notebook lock; refresh before discovering the source set.
    await db.refresh(located)
    if located.current_version_id is None:
        raise unavailable()
    source_ids = (
        await db.scalars(
            sa.select(ShareVersionItem.source_tactic_id).where(
                ShareVersionItem.share_version_id == located.current_version_id
            )
        )
    ).all()
    sources = (
        await db.scalars(
            sa.select(Tactic).where(Tactic.id.in_(source_ids)).order_by(Tactic.id).with_for_update()
        )
    ).all()
    if not allow_revoked and (
        len(sources) != len(source_ids) or any(item.status != "active" for item in sources)
    ):
        raise unavailable()
    link = await db.scalar(
        sa.select(ShareLink)
        .where(ShareLink.id == located.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not allow_revoked and link.revoked_at:
        raise unavailable()
    if read_grant is not None and read_grant.expires_at <= accounts.now():
        raise unavailable()
    return link


async def publish(db, link, actor_id, expected, allow_copy):
    query = sa.select(Tactic).where(
        Tactic.notebook_id == link.owner_notebook_id, Tactic.status == "active"
    )
    if link.tactic_id:
        query = query.where(Tactic.id == link.tactic_id)
    tactics = (await db.scalars(query.order_by(Tactic.id).with_for_update())).all()
    if not tactics:
        if link.tactic_id:
            raise documents.missing()
        raise AppError(422, "SHARE_EMPTY_NOTEBOOK", "没有可发布的已保存战术。")
    revisions = (
        await db.execute(
            sa.select(TacticDocument.tactic_id, TacticDocument.revision)
            .where(TacticDocument.tactic_id.in_([item.id for item in tactics]))
            .order_by(TacticDocument.tactic_id)
            .with_for_update()
        )
    ).all()
    if {str(identity): revision for identity, revision in revisions} != {
        str(item.id): item.revision for item in expected
    }:
        raise AppError(409, "SHARE_SOURCE_CHANGED", "已保存集合已变化，请重新确认。")
    # Fixed order is source UUID order, independent of later draft titles and directory updates.
    version = ShareVersion(
        share_link_id=link.id, created_by=actor_id, allow_copy=allow_copy, manifest=[]
    )
    db.add(version)
    await db.flush()
    manifest = []
    for order, tactic in enumerate(tactics):
        doc = await db.get(TacticDocument, tactic.id)
        await history.save_history(db, doc, actor_id, "publish")
        creator = await db.get(TacticsUser, tactic.created_by)
        identity = uuid.uuid4()
        attribution = {
            "tactic_id": str(tactic.id),
            "title": tactic.title,
            "author_display_name": creator.display_name,
            "copied_at": accounts.now().isoformat(),
        }
        item = ShareVersionItem(
            share_version_id=version.id,
            source_tactic_id=tactic.id,
            item_id=identity,
            order_index=order,
            source_epoch=doc.epoch,
            source_revision=doc.revision,
            projection=doc.projection,
            state=doc.yjs_state,
            attribution=attribution,
        )
        db.add(item)
        manifest.append(
            {
                "item_id": str(identity),
                "title": tactic.title,
                "side": tactic.side,
                "map": tactic.map,
                "source_revision": doc.revision,
            }
        )
        await db.flush()
        db.expunge(item)
        db.expunge(doc)
    version.manifest = manifest
    link.current_version_id, link.allow_copy, link.updated_at = (
        version.id,
        allow_copy,
        accounts.now(),
    )
    await db.flush()
    return version


async def revoke_sources(db, notebook_id, tactic_id=None):
    query = sa.select(ShareLink).where(
        ShareLink.owner_notebook_id == notebook_id, ShareLink.revoked_at.is_(None)
    )
    if tactic_id:
        query = query.where(
            sa.or_(
                ShareLink.tactic_id == tactic_id,
                sa.exists(
                    sa.select(1).where(
                        ShareVersionItem.share_version_id == ShareLink.current_version_id,
                        ShareVersionItem.source_tactic_id == tactic_id,
                    )
                ),
            )
        )
    links = (await db.scalars(query.order_by(ShareLink.id).with_for_update())).all()
    for link in links:
        link.revoked_at = link.updated_at = accounts.now()


def item_cursor(link, version, index):
    return (
        base64.urlsafe_b64encode(
            documents.json_bytes(
                {
                    "s": str(link.id),
                    "v": str(version),
                    "i": index,
                }
            )
        )
        .decode()
        .rstrip("=")
    )


def cursor_index(cursor, link, version):
    if cursor is None:
        return 0
    try:
        data = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            set(data) != {"s", "v", "i"}
            or data["s"] != str(link.id)
            or (data["v"] != str(version) or type(data["i"]) is not int or not 0 <= data["i"] < 100)
        ):
            raise ValueError
        return data["i"]
    except (ValueError, TypeError, KeyError):
        raise documents.bad_input() from None


async def spool_copies(db_factory, version_id, directory):
    """One source projection and one Node result in memory; only immutable IDs span the calls."""
    async with db_factory() as db:
        rows = (
            await db.execute(
                sa.select(ShareVersionItem.item_id, ShareVersionItem.attribution)
                .where(ShareVersionItem.share_version_id == version_id)
                .order_by(ShareVersionItem.order_index)
            )
        ).all()
    if not 1 <= len(rows) <= 100:
        raise unavailable()
    result, total_state, total_projection = [], 0, 0
    for index, (item_id, attribution) in enumerate(rows):
        async with db_factory() as db:
            projection = await db.scalar(
                sa.select(ShareVersionItem.projection).where(ShareVersionItem.item_id == item_id)
            )
        if projection is None:
            raise unavailable()
        identity = uuid.uuid4()
        state, copied, digest, pdigest = await documents.request_copy(identity, projection)
        encoded = documents.json_bytes(copied)
        total_state += len(state)
        total_projection += len(encoded)
        if (
            len(state) > 4194304
            or len(encoded) > 2097152
            or total_state > 419430400
            or (total_projection > 209715200)
        ):
            raise AppError(413, "REQUEST_TOO_LARGE", "复制内容超出安全大小。")
        base = Path(directory) / str(index)
        base.with_suffix(".state").write_bytes(state)
        base.with_suffix(".projection").write_bytes(encoded)
        result.append((identity, base, digest, pdigest, attribution))
    return result


async def insert_spooled(db, notebook, actor_id, copies):
    for identity, base, digest, pdigest, attribution in copies:
        projection = json.loads(base.with_suffix(".projection").read_bytes())
        tactic = Tactic(
            id=identity,
            notebook_id=notebook.id,
            created_by=actor_id,
            title=projection["fields"]["tactic.title"],
            side=projection["structure"]["side"],
            source_attribution={**attribution, "copied_at": accounts.now().isoformat()},
        )
        db.add(tactic)
        await db.flush()
        doc = TacticDocument(
            tactic_id=identity,
            yjs_state=base.with_suffix(".state").read_bytes(),
            projection=projection,
            state_digest=digest,
            projection_digest=pdigest,
        )
        db.add(doc)
        await db.flush()
        # Expunge large rows once inserted; summaries are small and the transaction stays atomic.
        db.expunge(doc)
        tactic._access_role = notebook._access_role
        yield documents.summary(tactic, doc)
        db.expunge(tactic)
