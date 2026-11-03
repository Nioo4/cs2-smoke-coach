"""Share-copy filesystem/OS-lock tests; fresh tmp_path only, no DB/HTTP/service access."""

import asyncio
import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.services import tactics_shares as shares

BACKEND = Path(__file__).resolve().parents[1]
CHILD = """
import asyncio, os, sys
from pathlib import Path
from app.core.errors import AppError
from app.services.tactics_shares import _share_copy_batch
async def main():
    try:
        async with _share_copy_batch(Path(sys.argv[1])) as directory:
            (directory / '0.state').write_bytes(b'private test fixture')
            if sys.argv[2] == 'crash':
                os._exit(17)
        print('ACQUIRED')
    except AppError as error:
        print(error.code)
        return 3
    return 0
sys.exit(asyncio.run(main()))
"""


def child(root, mode="probe"):
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(root), mode], cwd=BACKEND,
        capture_output=True, text=True, timeout=20,
    )


def marked(private):
    identity = str(uuid.uuid4())
    directory = private / f"batch-{identity}"
    directory.mkdir(mode=0o700)
    (directory / shares._SPOOL_MARKER).write_text(json.dumps(
        {"kind": shares._SPOOL_KIND, "version": 1, "batch_id": identity}
    ))
    (directory / "0.state").write_bytes(b"private test fixture")
    return directory


def remaining_batches(root):
    return list((root / shares._SPOOL_DIRECTORY).glob("batch-*"))


@pytest.mark.asyncio
async def test_os_lock_excludes_other_processes_and_stays_held_through_caller_yield(tmp_path):
    async with shares._share_copy_batch(tmp_path) as active:
        (active / "0.state").write_bytes(b"active private fixture")
        lock = active.parent / ".copy.lock"
        identity = lock.stat().st_ino
        for _ in range(2):
            competitor = child(tmp_path)
            assert competitor.returncode == 3 and competitor.stdout.strip() == "COLLAB_UNAVAILABLE"
            assert active.is_dir()
            assert (active / "0.state").read_bytes() == b"active private fixture"
        if os.name == "posix":
            assert stat.S_IMODE(active.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(active.stat().st_mode) == 0o700
    assert not remaining_batches(tmp_path)
    assert lock.is_file() and lock.stat().st_ino == identity
    after = child(tmp_path)
    assert after.returncode == 0 and after.stdout.strip() == "ACQUIRED"
    assert not remaining_batches(tmp_path)


@pytest.mark.asyncio
async def test_hard_process_exit_releases_lock_and_only_verified_orphans_are_reclaimed(tmp_path):
    unrelated = tmp_path / "upload-other.bin"
    unrelated.write_bytes(b"other feature")
    legacy = tmp_path / "smokeline-share-copy-legacy"
    legacy.mkdir()
    (legacy / "0.state").write_bytes(b"legacy unknown fixture")
    crashed = child(tmp_path, "crash")
    assert crashed.returncode == 17  # os._exit bypasses Python finally, releases OS locks.
    stale = remaining_batches(tmp_path)
    assert len(stale) == 1 and (stale[0] / "0.state").is_file()
    async with shares._share_copy_batch(tmp_path) as current:
        assert not stale[0].exists() and current.is_dir()
        assert unrelated.read_bytes() == b"other feature"
        assert (legacy / "0.state").read_bytes() == b"legacy unknown fixture"
    assert not remaining_batches(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["missing_marker", "bad_marker", "wrong_uuid", "bool_version",
                "unknown_root", "unknown_child"]
)
async def test_unknown_entries_fail_closed_without_deleting_verified_candidates(tmp_path, invalid):
    private = shares._share_copy_root(tmp_path)
    valid = marked(private)
    suspect = marked(private)
    marker = suspect / shares._SPOOL_MARKER
    if invalid == "missing_marker":
        marker.unlink()
    elif invalid == "bad_marker":
        marker.write_text('{"kind":"unknown"}')
    elif invalid in {"wrong_uuid", "bool_version"}:
        data = json.loads(marker.read_bytes())
        data["batch_id" if invalid == "wrong_uuid" else "version"] = (
            str(uuid.uuid4()) if invalid == "wrong_uuid" else True
        )
        marker.write_text(json.dumps(data))
    elif invalid == "unknown_root":
        (private / "operator-note").write_text("unknown")
    else:
        (suspect / "unknown-content").write_text("unknown")
    with pytest.raises(AppError) as failure:
        async with shares._share_copy_batch(tmp_path):
            pytest.fail("unverified spool must not open")
    assert failure.value.code == "SHARE_COPY_SPOOL_UNVERIFIED"
    assert valid.is_dir() and suspect.is_dir()
    assert (valid / "0.state").read_bytes() == b"private test fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["root", "lock", "batch", "marker", "file", "ancestor"])
async def test_symlink_paths_are_never_followed_or_deleted(tmp_path, location):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep outside")
    base = tmp_path / "base"
    base.mkdir()
    private = shares._share_copy_root(base)
    try:
        if location == "root":
            private.rmdir()
            private.symlink_to(outside, target_is_directory=True)
        elif location == "ancestor":
            alias = tmp_path / "alias"
            alias.symlink_to(base, target_is_directory=True)
            base = alias
        elif location == "lock":
            (private / ".copy.lock").symlink_to(sentinel)
        elif location == "batch":
            (private / f"batch-{uuid.uuid4()}").symlink_to(outside, target_is_directory=True)
        else:
            directory = marked(private)
            target = directory / (shares._SPOOL_MARKER if location == "marker" else "0.state")
            target.unlink()
            target.symlink_to(sentinel)
    except OSError:
        pytest.skip("OS does not permit test symlink creation; not a passing link-safety check")
    with pytest.raises(AppError) as failure:
        async with shares._share_copy_batch(base):
            pytest.fail("symlink must not open")
    assert failure.value.code == "SHARE_COPY_SPOOL_UNSAFE"
    assert sentinel.read_bytes() == b"keep outside"


def test_windows_reparse_attribute_and_hardlinks_are_rejected(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"test")
    original = Path.lstat
    info = original(candidate)
    fake = SimpleNamespace(
        st_mode=info.st_mode,
        st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
    )
    monkeypatch.setattr(Path, "lstat", lambda path: fake if path == candidate else original(path))
    with pytest.raises(AppError, match="人工核查"):
        shares._spool_info(candidate, directory=False)
    monkeypatch.undo()
    linked = tmp_path / "linked"
    os.link(candidate, linked)
    with pytest.raises(AppError) as failure:
        shares._spool_info(linked, directory=False)
    assert failure.value.code == "SHARE_COPY_SPOOL_UNSAFE" and candidate.read_bytes() == b"test"


class CountOnly:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def scalar(self, _query):
        return 2


@pytest.mark.asyncio
async def test_copy_context_holds_lock_through_second_transaction_and_cleans_failure_cancel(
    tmp_path, monkeypatch
):
    async def spool(_factory, _version, directory):
        (directory / "0.state").write_bytes(b"state")
        return directory

    monkeypatch.setattr(shares, "spool_copies", spool)
    monkeypatch.setattr(shares, "_COPY_SLOT", asyncio.Semaphore(1))
    for fail in [False, True]:
        try:
            async with shares.copy_spool_context(CountOnly, uuid.uuid4(), tmp_path) as active:
                assert child(tmp_path).returncode == 3  # Caller transaction is still protected.
                if fail:
                    raise RuntimeError("isolated caller failure")
                assert active.is_dir()
        except RuntimeError:
            assert fail
        assert not remaining_batches(tmp_path) and not shares._COPY_SLOT.locked()
    entered = asyncio.Event()

    async def cancelled():
        async with shares.copy_spool_context(CountOnly, uuid.uuid4(), tmp_path):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(cancelled())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not remaining_batches(tmp_path) and not shares._COPY_SLOT.locked()
    assert child(tmp_path).returncode == 0


@pytest.mark.asyncio
async def test_preflight_space_failure_cleans_batch_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(shares.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(AppError) as failure:
        async with shares.copy_spool_context(CountOnly, uuid.uuid4(), tmp_path):
            pytest.fail("zero free space must not yield")
    assert failure.value.code == "COLLAB_UNAVAILABLE"
    assert not remaining_batches(tmp_path) and not shares._COPY_SLOT.locked()
    assert child(tmp_path).returncode == 0


@pytest.mark.asyncio
async def test_write_enospc_cleans_partial_batch_and_releases_slot_and_os_lock(
    tmp_path, monkeypatch
):
    async def spool(_factory, _version, directory):
        (directory / "0.state").write_bytes(b"partial private fixture")
        raise OSError(28, "injected disk full")

    monkeypatch.setattr(shares, "spool_copies", spool)
    with pytest.raises(AppError) as failure:
        async with shares.copy_spool_context(CountOnly, uuid.uuid4(), tmp_path):
            pytest.fail("partial write must not yield")
    assert failure.value.code == "SHARE_COPY_SPOOL_IO_FAILED"
    assert not remaining_batches(tmp_path) and not shares._COPY_SLOT.locked()
    assert child(tmp_path).returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("enough", [False, True])
async def test_space_guard_uses_exact_count_times_six_mib_plus_sixteen(
    tmp_path, monkeypatch, enough
):
    required = (2 * 6 + 16) * 1024 * 1024
    monkeypatch.setattr(
        shares.shutil, "disk_usage",
        lambda _path: SimpleNamespace(free=required if enough else required - 1)
    )

    async def spool(_factory, _version, directory):
        return directory

    monkeypatch.setattr(shares, "spool_copies", spool)
    if enough:
        async with shares.copy_spool_context(CountOnly, uuid.uuid4(), tmp_path) as directory:
            assert directory.is_dir()
    else:
        with pytest.raises(AppError) as failure:
            async with shares.copy_spool_context(CountOnly, uuid.uuid4(), tmp_path):
                pytest.fail("required minus one must not yield")
        assert failure.value.code == "COLLAB_UNAVAILABLE"
    assert not remaining_batches(tmp_path)


@pytest.mark.asyncio
async def test_existing_private_root_mode_is_never_widened_or_silently_repaired(tmp_path):
    private = shares._share_copy_root(tmp_path)
    if os.name != "posix":
        async with shares._share_copy_batch(tmp_path):
            assert private.is_dir()
        return  # Windows mode bits do not certify ACLs; reparse safety has a separate test.
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    async with shares._share_copy_batch(tmp_path):
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
    private.chmod(0o755)
    with pytest.raises(AppError) as failure:
        async with shares._share_copy_batch(tmp_path):
            pytest.fail("broad permissions must be rejected")
    assert failure.value.code == "SHARE_COPY_SPOOL_UNSAFE"
    assert stat.S_IMODE(private.stat().st_mode) == 0o755


def test_cleanup_keeps_marker_until_all_payload_files_are_removed(tmp_path, monkeypatch):
    private = shares._share_copy_root(tmp_path)
    directory = marked(private)
    (directory / "0.projection").write_bytes(b"private projection fixture")
    order = []
    original = Path.unlink

    def tracked(path, *args, **kwargs):
        if path.parent == directory:
            order.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", tracked)
    shares._remove_verified_batch(directory)
    assert set(order[:-1]) == {"0.state", "0.projection"}
    assert order[-1] == shares._SPOOL_MARKER and not directory.exists()
