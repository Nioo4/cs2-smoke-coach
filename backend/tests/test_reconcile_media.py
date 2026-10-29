import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.db.models import LineupStatus, VideoStatus
from app.scripts import reconcile_media
from app.services import media


def _video(root_name: str = "public") -> SimpleNamespace:
    lineup_id = uuid.uuid4()
    storage_key = uuid.uuid4()
    return SimpleNamespace(
        id=uuid.uuid4(),
        lineup_id=lineup_id,
        status=VideoStatus.ready,
        storage_key=storage_key,
        relative_path=f"{lineup_id}/primary-{storage_key}.mp4",
        poster_relative_path=f"{lineup_id}/primary-{storage_key}.webp",
        duration_ms=2_000,
        size_bytes=4,
        checksum_sha256="098f6bcd4621d373cade4e832627b4f6c8a3e3b4f2d8f6f1d5a3f6d4c5b6a7",
        failure_reason=None,
        root_name=root_name,
    )


def _video_probe(*, width: int = 1280, height: int = 720) -> dict[str, object]:
    return {
        "format": {"format_name": "mov,mp4", "duration": "2.000"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": width,
                "height": height,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
                "duration": "2.000",
                "nb_frames": "60",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
    }


def _poster_probe(*, width: int = 640, height: int = 360) -> dict[str, object]:
    return {
        "format": {"format_name": "webp"},
        "streams": [
            {"codec_type": "video", "codec_name": "webp", "width": width, "height": height}
        ],
    }


def _write_pair(root, video, side: str, *, video_bytes: bytes = b"test") -> None:
    directory = root / side / str(video.lineup_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"primary-{video.storage_key}.mp4").write_bytes(video_bytes)
    (directory / f"primary-{video.storage_key}.webp").write_bytes(b"poster")


@pytest.mark.asyncio
async def test_reconcile_merges_partial_pair_and_discards_duplicate(monkeypatch, tmp_path) -> None:
    video = _video()
    video.checksum_sha256 = "098f6bcd4621d373cade4e832627b4f6c8a3e3b4f2d8f6f1d5a3f6d4c5b6a7"
    # The fixture uses a real checksum rather than trusting a test-only value.
    import hashlib

    video.checksum_sha256 = hashlib.sha256(b"test").hexdigest()
    _write_pair(tmp_path, video, "public")
    archive_poster = tmp_path / "archive" / str(video.lineup_id)
    archive_poster.mkdir(parents=True)
    (archive_poster / f"primary-{video.storage_key}.webp").write_bytes(b"duplicate")

    async def probe(path):
        return _poster_probe() if path.suffix == ".webp" else _video_probe()

    monkeypatch.setattr(reconcile_media, "_probe", probe)
    journal: list[tuple[object, object]] = []
    result = await reconcile_media._reconcile_video(tmp_path, video, journal)

    assert result.healthy is True
    assert (tmp_path / "public" / video.relative_path).is_file()
    assert not (tmp_path / "archive" / video.poster_relative_path).exists()
    assert len(journal) == 1
    assert any((tmp_path / "trash").rglob("*.webp"))


@pytest.mark.asyncio
async def test_reconcile_moves_missing_side_from_other_root(monkeypatch, tmp_path) -> None:
    video = _video()
    import hashlib

    video.checksum_sha256 = hashlib.sha256(b"test").hexdigest()
    directory = tmp_path / "archive" / str(video.lineup_id)
    directory.mkdir(parents=True)
    (directory / f"primary-{video.storage_key}.mp4").write_bytes(b"test")
    (directory / f"primary-{video.storage_key}.webp").write_bytes(b"poster")

    async def probe(path):
        return _poster_probe() if path.suffix == ".webp" else _video_probe()

    monkeypatch.setattr(reconcile_media, "_probe", probe)
    journal: list[tuple[object, object]] = []
    result = await reconcile_media._reconcile_video(tmp_path, video, journal)

    assert result.healthy is True
    assert (tmp_path / "public" / video.relative_path).is_file()
    assert (tmp_path / "public" / video.poster_relative_path).is_file()
    assert not (tmp_path / "archive" / video.relative_path).exists()
    assert len(journal) == 2


@pytest.mark.asyncio
async def test_reconcile_quarantines_corrupt_pair_before_failure(monkeypatch, tmp_path) -> None:
    video = _video()
    video.checksum_sha256 = "0" * 64
    _write_pair(tmp_path, video, "public")

    async def probe(path):
        return _poster_probe() if path.suffix == ".webp" else _video_probe()

    monkeypatch.setattr(reconcile_media, "_probe", probe)
    journal: list[tuple[object, object]] = []
    result = await reconcile_media._reconcile_video(tmp_path, video, journal)

    assert result.healthy is False
    assert not (tmp_path / "public" / video.relative_path).exists()
    assert not (tmp_path / "public" / video.poster_relative_path).exists()
    assert len(list((tmp_path / "trash").rglob("*"))) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_suffix", [".mp4", ".webp"])
async def test_reconcile_quarantines_valid_half_pair_before_failure(
    monkeypatch, tmp_path, missing_suffix
) -> None:
    video = _video()
    import hashlib

    video.checksum_sha256 = hashlib.sha256(b"test").hexdigest()
    _write_pair(tmp_path, video, "public")
    (
        tmp_path / "public" / str(video.lineup_id) / f"primary-{video.storage_key}{missing_suffix}"
    ).unlink()

    async def probe(path):
        return _poster_probe() if path.suffix == ".webp" else _video_probe()

    monkeypatch.setattr(reconcile_media, "_probe", probe)
    journal: list[tuple[object, object]] = []
    result = await reconcile_media._reconcile_video(tmp_path, video, journal)

    assert result.healthy is False
    assert not (tmp_path / "public" / video.relative_path).exists()
    assert not (tmp_path / "public" / video.poster_relative_path).exists()
    trashed_files = list((tmp_path / "trash").rglob("*.mp4")) + list(
        (tmp_path / "trash").rglob("*.webp")
    )
    assert len(trashed_files) == 1


class _FakeResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class _FakeSession:
    def __init__(self, videos, lineups):
        self.videos = videos
        self.lineups = lineups

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement, *_args, **_kwargs):
        if statement.__class__.__name__ == "TextClause":
            return _FakeResult([])
        entity = statement.column_descriptions[0]["entity"]
        from app.db.models import Lineup, TutorialVideo

        if entity is TutorialVideo:
            return _FakeResult(self.videos)
        if entity is Lineup:
            where = str(statement.whereclause)
            if "target_id" in where and "origin_id" in where:
                first = self.lineups[0]
                return _FakeResult(
                    [
                        lineup
                        for lineup in self.lineups
                        if lineup.target_id == first.target_id
                        and lineup.origin_id == first.origin_id
                    ]
                )
            return _FakeResult(self.lineups)
        return _FakeResult([])

    async def get(self, model, identifier, **_kwargs):
        from app.db.models import TutorialVideo

        values = self.videos if model is TutorialVideo else self.lineups
        return next((item for item in values if item.id == identifier), None)

    async def flush(self):
        return None

    @asynccontextmanager
    async def begin(self):
        yield self


class _FakeFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


@pytest.mark.asyncio
async def test_reconcile_replaces_a_broken_default_once(monkeypatch, caplog) -> None:
    target_id, origin_id = uuid.uuid4(), uuid.uuid4()
    bad_lineup_id, healthy_lineup_id = uuid.uuid4(), uuid.uuid4()
    bad_video = SimpleNamespace(
        id=uuid.uuid4(),
        lineup_id=bad_lineup_id,
        status=VideoStatus.ready,
        storage_key=uuid.uuid4(),
        relative_path="bad.mp4",
        poster_relative_path="bad.webp",
        duration_ms=2_000,
        size_bytes=4,
        checksum_sha256="0" * 64,
        failure_reason=None,
    )
    healthy_video = SimpleNamespace(
        id=uuid.uuid4(),
        lineup_id=healthy_lineup_id,
        status=VideoStatus.ready,
        storage_key=uuid.uuid4(),
        relative_path="good.mp4",
        poster_relative_path="good.webp",
        duration_ms=2_000,
        size_bytes=4,
        checksum_sha256="1" * 64,
        failure_reason=None,
    )
    bad_lineup = SimpleNamespace(
        id=bad_lineup_id,
        target_id=target_id,
        origin_id=origin_id,
        status=LineupStatus.published,
        is_default=True,
        default_needs_review=False,
        sort_order=10,
    )
    healthy_lineup = SimpleNamespace(
        id=healthy_lineup_id,
        target_id=target_id,
        origin_id=origin_id,
        status=LineupStatus.published,
        is_default=False,
        default_needs_review=False,
        sort_order=20,
    )
    session = _FakeSession([bad_video, healthy_video], [bad_lineup, healthy_lineup])
    monkeypatch.setattr(reconcile_media, "get_session_factory", lambda: _FakeFactory(session))
    caplog.set_level("WARNING", logger="smokeline")
    checks = {
        bad_video.id: reconcile_media._VideoCheck(False, True),
        healthy_video.id: reconcile_media._VideoCheck(True, True),
    }

    await reconcile_media._apply_failures(
        [bad_video, healthy_video],
        [bad_lineup, healthy_lineup],
        checks,
        {bad_lineup_id},
    )

    assert bad_lineup.status == LineupStatus.needs_revalidation
    assert bad_lineup.is_default is False
    assert healthy_lineup.is_default is True
    assert healthy_lineup.default_needs_review is True
    assert [record.message for record in caplog.records].count("media default replaced") == 1


@pytest.mark.asyncio
async def test_reconcile_assumes_caller_holds_media_lock(monkeypatch, tmp_path) -> None:
    settings = SimpleNamespace(media_store_root=str(tmp_path))
    monkeypatch.setattr(reconcile_media, "get_settings", lambda: settings)
    monkeypatch.setattr(media, "get_settings", lambda: settings)

    async def empty_snapshot():
        return [], []

    monkeypatch.setattr(reconcile_media, "_read_snapshot", empty_snapshot)

    async with media.media_lock(timeout_seconds=1):
        await reconcile_media.reconcile_media()


@pytest.mark.parametrize(
    "relative",
    [None, "", "../escape.mp4", "nested\\escape.mp4", "/absolute.mp4", "nested//file.mp4"],
)
def test_reconcile_rejects_unsafe_or_missing_relative_media_paths(tmp_path, relative) -> None:
    if relative is None:
        assert reconcile_media._safe_media_path(tmp_path, relative) is None
        return
    with pytest.raises(reconcile_media.MediaReconcileError):
        reconcile_media._safe_media_path(tmp_path, relative)


def test_reconcile_probe_contract_accepts_fixed_pair_and_rejects_bad_metadata() -> None:
    video = _video()
    valid_video = _video_probe()
    assert reconcile_media._video_probe_is_valid(valid_video, video)

    wrong_duration = _video_probe()
    wrong_duration["streams"][0]["duration"] = "3.000"
    wrong_duration["format"]["duration"] = "3.000"
    assert not reconcile_media._video_probe_is_valid(wrong_duration, video)

    wrong_format = _video_probe()
    wrong_format["format"]["format_name"] = "webm"
    assert not reconcile_media._video_probe_is_valid(wrong_format, video)

    wrong_fps = _video_probe()
    wrong_fps["streams"][0]["avg_frame_rate"] = "60/1"
    wrong_fps["streams"][0]["r_frame_rate"] = "60/1"
    assert not reconcile_media._video_probe_is_valid(wrong_fps, video)

    wrong_expected_duration = _video_probe()
    video.duration_ms = 5_000
    assert not reconcile_media._video_probe_is_valid(wrong_expected_duration, video)


def test_reconcile_probe_contract_accepts_hd_pair_and_rejects_mismatched_poster() -> None:
    video = _video_probe(width=1440, height=1080)
    poster = _poster_probe(width=1280, height=960)
    assert reconcile_media._output_probe_is_fixed(video)
    assert reconcile_media._poster_probe_is_valid(poster, video)
    assert not reconcile_media._poster_probe_is_valid(_poster_probe(), video)
    assert not reconcile_media._poster_probe_is_valid(_poster_probe(width=1280, height=720), video)


def test_reconcile_probe_helpers_cover_fallback_and_poster_validation() -> None:
    assert (
        reconcile_media._probe_duration(
            {"streams": [{"codec_type": "video", "duration": "bad"}], "format": {"duration": "2"}}
        )
        == 2.0
    )
    assert reconcile_media._probe_duration({"streams": [], "format": {"duration": "bad"}}) == 0.0
    assert reconcile_media._fps({"streams": [{"codec_type": "audio"}]}) == 0.0
    assert (
        reconcile_media._fps({"streams": [{"codec_type": "video", "avg_frame_rate": "bad"}]}) == 0.0
    )
    assert reconcile_media._poster_probe_is_valid(_poster_probe())
    assert not reconcile_media._poster_probe_is_valid({})
    assert not reconcile_media._poster_probe_is_valid(
        {"format": {"format_name": "webp"}, "streams": [{"codec_type": "audio"}]}
    )
    assert not reconcile_media._poster_probe_is_valid(
        {
            "format": {"format_name": "webp"},
            "streams": [{"codec_type": "video", "codec_name": "webp", "width": "bad", "height": 1}],
        }
    )
