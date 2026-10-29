from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.api.admin import _mark_failed_video
from app.db.models import LineupStatus, VideoStatus
from app.services import media


def _input_probe(*, audio: bool = True, duration: str = "2.000") -> dict[str, object]:
    streams: list[dict[str, object]] = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30/1",
            "duration": duration,
        }
    ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"format": {"format_name": "mov,mp4", "duration": duration}, "streams": streams}


def _fixed_output_probe(
    *, duration: str = "2.000000", nb_frames: str = "60", width: int = 1920, height: int = 1080
) -> dict[str, object]:
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": width,
                "height": height,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
                "duration": duration,
                "nb_frames": nb_frames,
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
        "format": {"format_name": "webp_pipe"},
        "streams": [
            {"codec_type": "video", "codec_name": "webp", "width": width, "height": height}
        ],
    }


@pytest.mark.parametrize(
    ("payload", "mime", "code"),
    [
        ({}, "video/mp4", "INVALID_VIDEO"),
        (_input_probe(), "video/webm", "UNSUPPORTED_VIDEO_TYPE"),
        (_input_probe(duration="0"), "video/mp4", "INVALID_VIDEO"),
        (_input_probe(duration="20.1"), "video/mp4", "VIDEO_TOO_LONG"),
        (_input_probe(), "video/mp4", "INVALID_VIDEO"),
    ],
)
def test_validate_probe_rejects_invalid_metadata(payload, mime, code) -> None:
    if code == "INVALID_VIDEO" and payload == _input_probe():
        payload = _input_probe()
        payload["streams"] = [{"codec_type": "audio", "codec_name": "aac"}]
    with pytest.raises(media.MediaError) as error:
        media._validate_probe(payload, mime)
    assert error.value.code == code


def test_validate_probe_accepts_mp4_and_detects_audio() -> None:
    result = media._validate_probe(_input_probe(), "video/mp4")
    assert result.duration == 2.0
    assert result.width == 1920
    assert result.height == 1080
    assert result.fps == 30.0
    assert result.has_audio is True
    assert media._validate_probe(_input_probe(audio=False), "video/quicktime").has_audio is False


@pytest.mark.parametrize(
    ("width", "height"),
    [(1440, 1080), (3840, 2160), (320, 180), (1439, 1081)],
)
def test_validate_probe_accepts_hd_input_boundaries(width: int, height: int) -> None:
    payload = _input_probe()
    payload["streams"][0].update({"width": width, "height": height})
    result = media._validate_probe(payload, "video/mp4")
    assert (result.width, result.height) == (width, height)


@pytest.mark.parametrize(
    ("width", "height"),
    [(1, 720), (720, 1), (0, 720), (720, 0)],
)
def test_validate_probe_rejects_sub_two_pixel_input(width: int, height: int) -> None:
    payload = _input_probe()
    payload["streams"][0].update({"width": width, "height": height})
    with pytest.raises(media.MediaError) as error:
        media._validate_probe(payload, "video/mp4")
    assert error.value.code == "INVALID_VIDEO"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload["streams"][0].update({"width": 3841}),
        lambda payload: payload["streams"][0].update({"height": 2161}),
        lambda payload: payload["streams"][0].update({"avg_frame_rate": "121/1"}),
    ],
)
def test_validate_probe_rejects_dimensions_and_frame_rate(mutator) -> None:
    payload = _input_probe()
    mutator(payload)
    with pytest.raises(media.MediaError) as error:
        media._validate_probe(payload, "video/mp4")
    assert error.value.code == "VIDEO_DIMENSIONS_TOO_LARGE"


def test_fraction_and_output_probe_contract() -> None:
    assert media._fraction("30000/1001") == pytest.approx(29.97003, rel=1e-5)
    assert media._fraction("N/A") == 0.0
    assert media._fraction("bad") == 0.0
    valid = _fixed_output_probe()
    assert media._output_probe_is_fixed(valid)
    invalid = _fixed_output_probe()
    invalid["streams"] = invalid["streams"][:1]
    assert not media._output_probe_is_fixed(invalid)
    invalid = _fixed_output_probe()
    invalid["streams"][0]["pix_fmt"] = "yuv444p"
    assert not media._output_probe_is_fixed(invalid)
    invalid = _fixed_output_probe()
    invalid["streams"][0]["duration"] = "20.001"
    assert not media._output_probe_is_fixed(invalid)
    invalid = _fixed_output_probe()
    invalid["streams"][0]["duration"] = "nan"
    assert not media._output_probe_is_fixed(invalid)
    invalid = _fixed_output_probe()
    invalid["streams"][0]["avg_frame_rate"] = "29/1"
    assert not media._output_probe_is_fixed(invalid)
    invalid = _fixed_output_probe()
    invalid["streams"][1]["channels"] = 1
    assert not media._output_probe_is_fixed(invalid)
    invalid = _fixed_output_probe()
    invalid["streams"][0].pop("nb_frames")
    assert not media._output_probe_is_fixed(invalid)
    invalid = _fixed_output_probe()
    invalid["streams"][0]["nb_frames"] = "0"
    assert not media._output_probe_is_fixed(invalid)
    assert media._output_duration_ms(_fixed_output_probe()) == 2_000
    assert media._output_frame_count(_fixed_output_probe()) == 60
    assert media._new_output_probe_is_valid(_fixed_output_probe())
    assert not media._new_output_probe_is_valid(_fixed_output_probe(width=1440, height=1080))
    assert media._poster_frame_index(_fixed_output_probe()) == 30
    assert media._poster_frame_index(_fixed_output_probe(duration="1.000000", nb_frames="30")) == 29
    assert media._poster_frame_index(_fixed_output_probe(duration="0.500000", nb_frames="15")) == 7
    assert media._poster_frame_index(_fixed_output_probe(duration="1.500000", nb_frames="45")) == 30
    assert media._poster_frame_index(_fixed_output_probe(duration="0.033333", nb_frames="1")) == 0
    assert media._poster_probe_is_valid(_poster_probe())
    image2_poster = _poster_probe()
    image2_poster["format"]["format_name"] = "image2"
    assert media._poster_probe_is_valid(image2_poster)
    invalid_poster = _poster_probe()
    invalid_poster["streams"][0]["width"] = 639
    assert not media._poster_probe_is_valid(invalid_poster)


@pytest.mark.parametrize(
    ("width", "height", "poster_width", "poster_height"),
    [
        (1440, 1080, 1280, 960),
        (1920, 1080, 1280, 720),
        (640, 360, 640, 360),
        (1920, 2, 1280, 2),
        (1280, 720, 640, 360),  # legacy complete pair
    ],
)
def test_output_contract_accepts_new_and_legacy_complete_pairs(
    width: int, height: int, poster_width: int, poster_height: int
) -> None:
    output = _fixed_output_probe(width=width, height=height)
    poster = _poster_probe(width=poster_width, height=poster_height)
    assert media._output_probe_is_fixed(output)
    assert media._poster_probe_is_valid(poster)
    assert media._poster_dimensions_match_video(poster, output)
    if (width, height) == (1280, 720) and poster_width == 640:
        assert not media._poster_dimensions_match_video(poster, output, allow_legacy=False)


@pytest.mark.parametrize(
    ("width", "height"),
    [(1439, 1080), (1440, 1079), (1922, 1080), (1920, 1082), (0, 720)],
)
def test_output_contract_rejects_odd_or_out_of_bounds_dimensions(width: int, height: int) -> None:
    assert not media._output_probe_is_fixed(_fixed_output_probe(width=width, height=height))


def test_new_poster_contract_rejects_legacy_width_for_non_720p_output() -> None:
    output = _fixed_output_probe(width=1440, height=1080)
    legacy_poster = _poster_probe(width=640, height=480)
    assert not media._poster_dimensions_match_video(legacy_poster, output, allow_legacy=False)


def test_poster_height_uses_positive_half_away_from_zero_rounding() -> None:
    # 8 * 10 / (16 * 2) is exactly 2.5; FFmpeg's av_rescale rounds this to 3.
    assert media._poster_height_for_width(16, 10, 8) == 6


@pytest.mark.asyncio
async def test_run_process_handles_success_failure_and_timeout() -> None:
    output, error = await media._run_process([sys.executable, "-c", "print('ok')"], timeout=2)
    assert output.strip() == b"ok"
    assert error == b""
    with pytest.raises(subprocess.CalledProcessError):
        await media._run_process([sys.executable, "-c", "raise SystemExit(3)"], timeout=2)
    with pytest.raises(TimeoutError):
        await media._run_process([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.01)


class _Chunks:
    def __init__(self, chunks: list[bytes]):
        self.chunks = iter(chunks)

    async def read(self, _size: int) -> bytes:
        return next(self.chunks, b"")


class _ReadyVideoFailureSession:
    def __init__(self, lineup, video):
        self.lineup = lineup
        self.video = video

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    def begin(self):
        return self._Transaction()

    async def get(self, model, identifier, **_kwargs):
        from app.db.models import Lineup

        if model is Lineup and identifier == self.lineup.id:
            return self.lineup
        return None

    async def execute(self, statement, *_args, **_kwargs):
        if statement.__class__.__name__ == "TextClause":
            return None

        class Result:
            def scalar_one_or_none(inner_self):
                return self.video

        return Result()


@pytest.mark.asyncio
async def test_mark_failed_video_preserves_existing_ready_row() -> None:
    lineup = SimpleNamespace(id=uuid.uuid4(), status=LineupStatus.draft)
    video = SimpleNamespace(
        status=VideoStatus.ready,
        storage_key=uuid.uuid4(),
        relative_path="lineup/video.mp4",
        poster_relative_path="lineup/video.webp",
        duration_ms=2_000,
        size_bytes=123,
        checksum_sha256="a" * 64,
        rights_note="原有授权",
        failure_reason=None,
    )
    before = video.__dict__.copy()
    session = _ReadyVideoFailureSession(lineup, video)
    await _mark_failed_video(session, lineup.id, "新授权", "output_too_large", "a" * 32)
    assert video.__dict__ == before


@pytest.mark.asyncio
async def test_write_upload_streams_and_enforces_input_limit(tmp_path) -> None:
    destination = tmp_path / "input.bin"
    assert await media._write_upload(_Chunks([b"one", b"two"]), destination) == 6
    assert destination.read_bytes() == b"onetwo"
    with pytest.raises(media.MediaError) as error:
        await media._write_upload(
            _Chunks([b"x" * (media.MAX_INPUT_BYTES + 1)]), tmp_path / "big.bin"
        )
    assert error.value.code == "VIDEO_TOO_LARGE"


@pytest.mark.asyncio
async def test_media_lock_and_public_artifact_validation(monkeypatch, tmp_path) -> None:
    lock_path = tmp_path / ".media.lock"
    first = media._MediaLock(lock_path)
    async with first:
        second = media._MediaLock(lock_path, timeout_seconds=0)
        with pytest.raises(media.MediaLockTimeout):
            await second.__aenter__()

    root = tmp_path / "media"
    public = root / "public" / "lineup"
    public.mkdir(parents=True)
    video_bytes = b"video"
    video_path = public / "video.mp4"
    poster_path = public / "video.webp"
    video_path.write_bytes(video_bytes)
    poster_path.write_bytes(b"poster")
    video = SimpleNamespace(
        status=VideoStatus.ready,
        relative_path="lineup/video.mp4",
        poster_relative_path="lineup/video.webp",
        size_bytes=len(video_bytes),
        duration_ms=2_000,
        checksum_sha256=hashlib.sha256(video_bytes).hexdigest(),
    )
    monkeypatch.setattr(media, "media_root", lambda: root)
    assert media.public_artifacts_look_valid(video)
    video.checksum_sha256 = "0" * 64
    assert not media.public_artifacts_look_valid(video)
    video.status = VideoStatus.failed
    assert not media.public_artifacts_look_valid(video)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("video_width", "video_height", "poster_width", "poster_height", "valid"),
    [
        (1280, 720, 640, 360, True),
        (1440, 1080, 1280, 960, True),
        (1440, 1080, 640, 480, False),
    ],
)
async def test_archived_media_validation_accepts_complete_contract_pairs(
    monkeypatch,
    tmp_path,
    video_width: int,
    video_height: int,
    poster_width: int,
    poster_height: int,
    valid: bool,
) -> None:
    root = tmp_path / "media"
    lineup_id = uuid.uuid4()
    storage_key = uuid.uuid4()
    relative_video = f"{lineup_id}/primary-{storage_key}.mp4"
    relative_poster = f"{lineup_id}/primary-{storage_key}.webp"
    archive_dir = root / "archive" / str(lineup_id)
    archive_dir.mkdir(parents=True)
    video_path = archive_dir / f"primary-{storage_key}.mp4"
    poster_path = archive_dir / f"primary-{storage_key}.webp"
    video_path.write_bytes(b"video")
    poster_path.write_bytes(b"poster")
    video = SimpleNamespace(
        relative_path=relative_video,
        poster_relative_path=relative_poster,
        duration_ms=2_000,
        size_bytes=video_path.stat().st_size,
        checksum_sha256=hashlib.sha256(b"video").hexdigest(),
    )

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe(width=video_width, height=video_height)
        return _poster_probe(width=poster_width, height=poster_height)

    monkeypatch.setattr(media, "media_root", lambda: root)
    monkeypatch.setattr(media, "_probe", probe)
    assert await media.archived_artifacts_look_valid(video) is valid


@pytest.mark.asyncio
async def test_cancelled_media_lock_wait_does_not_retain_handle(tmp_path) -> None:
    lock_path = tmp_path / ".media.lock"
    async with media._MediaLock(lock_path):
        waiter = asyncio.create_task(media._MediaLock(lock_path).__aenter__())
        await asyncio.sleep(0.1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    async with media._MediaLock(lock_path, timeout_seconds=0.2):
        pass


def test_media_write_roots_reject_symlink_components(tmp_path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this runner")
    assert not media._prepare_safe_directory(link, create=True)
    assert not media._prepare_safe_directory(link / "child", create=True)
    with pytest.raises(OSError):
        lock = media._MediaLock(link / ".media.lock")
        asyncio.run(lock.__aenter__())


@pytest.mark.asyncio
async def test_process_upload_success_adds_silent_audio_and_commits_atomically(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )
    commands: list[list[str]] = []

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe(duration="1.000000", nb_frames="30", width=1920, height=1080)
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe(audio=False, duration="1.000")

    async def run_process(args, timeout):
        commands.append(args)
        destination = args[-1]
        if destination.endswith(".mp4"):
            with open(destination, "wb") as handle:
                handle.write(b"encoded")
        elif destination.endswith(".webp"):
            with open(destination, "wb") as handle:
                handle.write(b"poster")
        return b"", b""

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    committed = []

    async def commit(value):
        committed.append(value)

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: Lock())
    lineup_id = uuid.uuid4()
    processed = await media.process_upload(
        _Chunks([b"input"]),
        content_type="video/mp4",
        rights_note="自动化测试夹具，非真实教学",
        lineup_id=lineup_id,
        request_id="a" * 32,
        commit=commit,
    )
    assert committed == [processed]
    assert processed.relative_path.startswith(f"{lineup_id}/primary-")
    assert (root / "public" / processed.relative_path).is_file()
    assert (root / "public" / processed.poster_relative_path).is_file()
    assert any("anullsrc" in command for command in commands[0])
    video_command = commands[0]
    assert video_command[video_command.index("-preset") + 1] == "medium"
    assert video_command[video_command.index("-threads") + 1] == "2"
    assert video_command[video_command.index("-crf") + 1] == "20"
    assert video_command[video_command.index("-maxrate") + 1] == "4000k"
    assert video_command[video_command.index("-bufsize") + 1] == "8000k"
    assert [command[command.index("-crf") + 1] for command in commands if "-crf" in command] == [
        "20"
    ]
    assert video_command[video_command.index("-vf") + 1] == (
        "split=2[bg][fg];"
        "[bg]scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1920:1080,boxblur=40:2,"
        "eq=brightness=-0.18:saturation=0.75[bgfill];"
        "[fg]scale=w='min(1920,iw)':h='min(1080,ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos[fgfit];"
        "[bgfill][fgfit]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30"
    )
    assert "boxblur=40:2" in video_command[video_command.index("-vf") + 1]
    poster_command = next(command for command in commands if "libwebp" in command)
    assert "-ss" not in poster_command
    assert poster_command[poster_command.index("-vf") + 1] == (
        r"select=eq(n\,29),scale=w='min(1280,iw)':h=-2:flags=lanczos"
    )
    assert poster_command[poster_command.index("-q:v") + 1] == "85"
    assert not (root / ".staging" / ("a" * 32)).exists()


@pytest.mark.asyncio
async def test_process_upload_accepts_exact_output_limit_without_retry(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )
    commands: list[list[str]] = []

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe(duration="1.000000", nb_frames="30", width=1920, height=1080)
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe(audio=False, duration="1.000")

    async def run_process(args, timeout):
        commands.append(args)
        destination = args[-1]
        if destination.endswith(".mp4"):
            with open(destination, "wb") as handle:
                handle.write(b"x" * media.MAX_OUTPUT_BYTES)
        elif destination.endswith(".webp"):
            with open(destination, "wb") as handle:
                handle.write(b"poster")
        return b"", b""

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: Lock())
    processed = await media.process_upload(
        _Chunks([b"input"]),
        content_type="video/mp4",
        rights_note="自动化测试夹具，非真实教学",
        lineup_id=uuid.uuid4(),
        request_id="m" * 32,
    )

    transcode_commands = [command for command in commands if command[-1].endswith(".mp4")]
    assert processed.size_bytes == media.MAX_OUTPUT_BYTES
    assert len(transcode_commands) == 1
    assert transcode_commands[0][transcode_commands[0].index("-crf") + 1] == "20"
    assert not list((root / ".staging").rglob("*"))


@pytest.mark.asyncio
async def test_process_upload_retries_one_safe_oversized_candidate_at_crf22(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )
    commands: list[list[str]] = []
    timeouts: list[float] = []
    video_runs = 0

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe(duration="1.000000", nb_frames="30")
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe(audio=False, duration="1.000")

    async def run_process(args, timeout):
        nonlocal video_runs
        commands.append(args)
        timeouts.append(timeout)
        destination = args[-1]
        if destination.endswith(".mp4"):
            video_runs += 1
            data = b"x" * (media.MAX_OUTPUT_BYTES + 1) if video_runs == 1 else b"encoded"
            with open(destination, "wb") as handle:
                handle.write(data)
            if video_runs == 1:
                await asyncio.sleep(0.01)
        elif destination.endswith(".webp"):
            with open(destination, "wb") as handle:
                handle.write(b"poster")
        return b"", b""

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: Lock())
    processed = await media.process_upload(
        _Chunks([b"input"]),
        content_type="video/mp4",
        rights_note="自动化测试夹具，非真实教学",
        lineup_id=uuid.uuid4(),
        request_id="j" * 32,
    )

    transcode_commands = [command for command in commands if command[-1].endswith(".mp4")]
    transcode_timeouts = [
        timeout
        for command, timeout in zip(commands, timeouts, strict=True)
        if command[-1].endswith(".mp4")
    ]
    assert video_runs == 2
    assert [command[command.index("-crf") + 1] for command in transcode_commands] == ["20", "22"]

    def without_crf(command: list[str]) -> list[str]:
        crf_index = command.index("-crf")
        return command[:crf_index] + command[crf_index + 2 :]

    assert without_crf(transcode_commands[0]) == without_crf(transcode_commands[1])
    assert 0 < transcode_timeouts[1] < transcode_timeouts[0] <= media.PROCESSING_TIMEOUT_SECONDS
    assert processed.size_bytes == len(b"encoded")
    assert not list((root / ".staging").rglob("*"))


@pytest.mark.asyncio
async def test_process_upload_second_candidate_timeout_uses_shared_deadline_and_cleans_up(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )
    commands: list[list[str]] = []
    timeouts: list[float] = []
    video_runs = 0

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe(duration="1.000000", nb_frames="30", width=1440, height=1080)
        return _input_probe(audio=False, duration="1.000")

    async def run_process(args, timeout):
        nonlocal video_runs
        commands.append(args)
        timeouts.append(timeout)
        if args[-1].endswith(".mp4"):
            video_runs += 1
            if video_runs == 1:
                with open(args[-1], "wb") as handle:
                    handle.write(b"x" * (media.MAX_OUTPUT_BYTES + 1))
                await asyncio.sleep(0.01)
            else:
                raise TimeoutError
        return b"", b""

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: Lock())
    with pytest.raises(media.MediaError) as error:
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=uuid.uuid4(),
            request_id="n" * 32,
        )

    transcode_commands = [command for command in commands if command[-1].endswith(".mp4")]
    assert error.value.code == "VIDEO_PROCESSING_TIMEOUT"
    assert error.value.failure_reason == "processing_timeout"
    assert video_runs == 2
    assert [command[command.index("-crf") + 1] for command in transcode_commands] == ["20", "22"]

    def without_crf(command: list[str]) -> list[str]:
        crf_index = command.index("-crf")
        return command[:crf_index] + command[crf_index + 2 :]

    assert without_crf(transcode_commands[0]) == without_crf(transcode_commands[1])
    assert 0 < timeouts[1] < timeouts[0] <= media.PROCESSING_TIMEOUT_SECONDS
    assert not list((root / ".staging").rglob("*"))


@pytest.mark.asyncio
async def test_process_upload_second_oversized_candidate_returns_output_error_once(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )
    commands: list[list[str]] = []

    async def probe(path, timeout=15.0):
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe(audio=False, duration="1.000")

    async def run_process(args, timeout):
        commands.append(args)
        if args[-1].endswith(".mp4"):
            with open(args[-1], "wb") as handle:
                handle.write(b"x" * (media.MAX_OUTPUT_BYTES + 1))
        return b"", b""

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: Lock())
    with pytest.raises(media.MediaError) as error:
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=uuid.uuid4(),
            request_id="k" * 32,
        )
    assert error.value.code == "VIDEO_OUTPUT_TOO_LARGE"
    assert error.value.message == "处理后的视频不能超过 12 MB。"
    transcode_commands = [command for command in commands if command[-1].endswith(".mp4")]
    assert [command[command.index("-crf") + 1] for command in transcode_commands] == ["20", "22"]
    assert not list((root / ".staging").rglob("*"))


@pytest.mark.asyncio
async def test_process_upload_transcode_failure_is_not_retried(monkeypatch, tmp_path) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )
    commands: list[list[str]] = []

    async def probe(path, timeout=15.0):
        return _input_probe(audio=False, duration="1.000")

    async def run_process(args, timeout):
        commands.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    with pytest.raises(media.MediaError) as error:
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=uuid.uuid4(),
            request_id="l" * 32,
        )
    assert error.value.code == "VIDEO_PROCESSING_FAILED"
    assert len([command for command in commands if command[-1].endswith(".mp4")]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "rights_note", "code"),
    [("image/png", "ok", "UNSUPPORTED_VIDEO_TYPE"), ("video/mp4", "", "VALIDATION_ERROR")],
)
async def test_process_upload_rejects_input_before_processing(
    monkeypatch, tmp_path, content_type, rights_note, code
) -> None:
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(
            media_store_root=str(tmp_path / "media"), temp_root=str(tmp_path / "temp")
        ),
    )
    with pytest.raises(media.MediaError) as error:
        await media.process_upload(
            _Chunks([b"input"]),
            content_type=content_type,
            rights_note=rights_note,
            lineup_id=uuid.uuid4(),
            request_id="b" * 32,
        )
    assert error.value.code == code


@pytest.mark.asyncio
async def test_process_upload_processing_failures_do_not_leave_new_pair(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe()
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe()

    async def run_process(args, timeout):
        destination = args[-1]
        if destination.endswith(".mp4"):
            with open(destination, "wb") as handle:
                handle.write(b"encoded")
        elif destination.endswith(".webp"):
            with open(destination, "wb") as handle:
                handle.write(b"poster")
        return b"", b""

    class Lock:
        def __init__(self):
            self.locked = False

        async def __aenter__(self):
            self.locked = True
            return self

        async def __aexit__(self, *_args):
            self.locked = False
            return None

    locks = []

    def make_lock(**_kwargs):
        lock = Lock()
        locks.append(lock)
        return lock

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", make_lock)
    lineup_id = uuid.uuid4()

    async def rejected(_processed):
        raise RuntimeError("database callback rejected")

    with pytest.raises(RuntimeError):
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=lineup_id,
            request_id="c" * 32,
            commit=rejected,
        )
    public_files = list((root / "public").rglob("*")) if (root / "public").exists() else []
    assert not [path for path in public_files if path.is_file()]

    real_replace = media.os.replace
    calls = 0
    rollback_lock_states = []

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second rename failed")
        rollback_lock_states.append(locks[-1].locked)
        return real_replace(source, destination)

    monkeypatch.setattr(media.os, "replace", fail_second)
    with pytest.raises(OSError):
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=uuid.uuid4(),
            request_id="d" * 32,
        )
    assert not list((root / "public").rglob("*.mp4")) if (root / "public").exists() else True
    assert rollback_lock_states and all(rollback_lock_states)


@pytest.mark.asyncio
async def test_post_commit_cleanup_cancellation_keeps_new_pair(monkeypatch, tmp_path) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe()
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe()

    async def run_process(args, timeout):
        destination = args[-1]
        destination_path = destination
        if destination.endswith(".mp4"):
            with open(destination_path, "wb") as handle:
                handle.write(b"encoded")
        elif destination.endswith(".webp"):
            with open(destination_path, "wb") as handle:
                handle.write(b"poster")
        return b"", b""

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def commit(_processed):
        return ("old/video.mp4", "old/video.webp")

    async def cancelled_cleanup(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: Lock())
    monkeypatch.setattr(media, "move_public_pair_to_trash", cancelled_cleanup)
    lineup_id = uuid.uuid4()

    with pytest.raises(asyncio.CancelledError):
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=lineup_id,
            request_id="g" * 32,
            commit=commit,
        )
    assert list((root / "public" / str(lineup_id)).glob("*.mp4"))
    assert list((root / "public" / str(lineup_id)).glob("*.webp"))


@pytest.mark.asyncio
async def test_commit_cancellation_waits_for_transaction_result(monkeypatch, tmp_path) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe()
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe()

    async def run_process(args, timeout):
        destination = args[-1]
        if destination.endswith(".mp4"):
            with open(destination, "wb") as handle:
                handle.write(b"encoded")
        elif destination.endswith(".webp"):
            with open(destination, "wb") as handle:
                handle.write(b"poster")
        return b"", b""

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()

    async def commit(_processed):
        commit_started.set()
        await allow_commit.wait()

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: Lock())
    lineup_id = uuid.uuid4()
    operation = asyncio.create_task(
        media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=lineup_id,
            request_id="h" * 32,
            commit=commit,
        )
    )
    await asyncio.wait_for(commit_started.wait(), timeout=1)
    operation.cancel()
    await asyncio.sleep(0.05)
    assert not operation.done()
    allow_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert list((root / "public" / str(lineup_id)).glob("*.mp4"))
    assert list((root / "public" / str(lineup_id)).glob("*.webp"))


@pytest.mark.asyncio
async def test_uncertain_db_commit_keeps_new_pair_for_reconciliation(
    monkeypatch, tmp_path, caplog
) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )

    async def probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe()
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe()

    async def run_process(args, timeout):
        destination = args[-1]
        if destination.endswith(".mp4"):
            with open(destination, "wb") as handle:
                handle.write(b"encoded")
        elif destination.endswith(".webp"):
            with open(destination, "wb") as handle:
                handle.write(b"poster")
        return b"", b""

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def uncertain_commit(_processed):
        raise OperationalError("commit", {}, RuntimeError("connection lost"))

    monkeypatch.setattr(media, "_probe", probe)
    monkeypatch.setattr(media, "_run_process", run_process)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: Lock())
    lineup_id = uuid.uuid4()
    with pytest.raises(media.MediaCommitUnknown):
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=lineup_id,
            request_id="i" * 32,
            commit=uncertain_commit,
        )
    assert list((root / "public" / str(lineup_id)).glob("*.mp4"))
    assert list((root / "public" / str(lineup_id)).glob("*.webp"))
    assert "media_commit_unknown" in caplog.text


@pytest.mark.asyncio
async def test_process_upload_maps_media_lock_timeout_and_output_errors(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "media"
    temp = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(root), temp_root=str(temp)),
    )

    async def input_probe(path, timeout=15.0):
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe()

    async def too_large_run(args, timeout):
        destination = args[-1]
        if destination == "-":
            return b"", b""
        with open(destination, "wb") as handle:
            if destination.endswith(".mp4"):
                handle.write(b"x" * (media.MAX_OUTPUT_BYTES + 1))
            elif destination.endswith(".webp"):
                handle.write(b"poster")
        return b"", b""

    monkeypatch.setattr(media, "_probe", input_probe)
    monkeypatch.setattr(media, "_run_process", too_large_run)
    monkeypatch.setattr(media, "_fsync_file", lambda _path: None)
    with pytest.raises(media.MediaError) as error:
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=uuid.uuid4(),
            request_id="e" * 32,
        )
    assert error.value.code == "VIDEO_OUTPUT_TOO_LARGE"

    class BusyLock:
        async def __aenter__(self):
            raise media.MediaLockTimeout("busy")

        async def __aexit__(self, *_args):
            return None

    async def fixed_probe(path, timeout=15.0):
        if path.suffix == ".mp4":
            return _fixed_output_probe()
        if path.suffix == ".webp":
            return _poster_probe(width=1280, height=720)
        return _input_probe()

    async def fixed_run(args, timeout):
        if args[-1].endswith(".mp4"):
            with open(args[-1], "wb") as handle:
                handle.write(b"encoded")
        elif args[-1].endswith(".webp"):
            with open(args[-1], "wb") as handle:
                handle.write(b"poster")
        return b"", b""

    monkeypatch.setattr(media, "_probe", fixed_probe)
    monkeypatch.setattr(media, "_run_process", fixed_run)
    monkeypatch.setattr(media, "media_lock", lambda **_kwargs: BusyLock())
    with pytest.raises(media.MediaBusy) as error:
        await media.process_upload(
            _Chunks([b"input"]),
            content_type="video/mp4",
            rights_note="自动化测试夹具，非真实教学",
            lineup_id=uuid.uuid4(),
            request_id="f" * 32,
        )
    assert error.value.code == "MEDIA_BUSY"
