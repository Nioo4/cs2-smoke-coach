"""Opt-in real-FFmpeg coverage for the Full HD background-fill media contract.

The fixtures are generated under pytest's isolated temporary directory.  This
test never reads the repository's user-media directory and is intentionally
opt-in so a normal unit run cannot pretend that FFmpeg was exercised.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import media


class _BytesUpload:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    async def read(self, size: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _ffprobe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _make_source(path: Path, width: int, height: int, *, odd_container: bool) -> None:
    if odd_container:
        arguments = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate=30",
            "-frames:v",
            "12",
            "-an",
            "-c:v",
            "ffv1",
            "-pix_fmt",
            "yuv444p",
            "-f",
            "matroska",
            str(path),
        ]
    else:
        arguments = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s={width}x{height}:r=30",
            "-t",
            "0.4",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ]
    completed = subprocess.run(arguments, check=False, capture_output=True)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert path.is_file() and path.stat().st_size > 0


def _video_stream(payload: dict[str, object]) -> dict[str, object]:
    streams = payload.get("streams")
    assert isinstance(streams, list)
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    assert len(videos) == 1
    return videos[0]


@pytest.fixture()
def ffmpeg_environment() -> None:
    if os.environ.get("PHASE2_INTEGRATION_REQUIRED") != "1":
        pytest.skip("real HD media FFmpeg coverage requires an explicit isolated test stack")
    if os.environ.get("APP_ENV") != "test":
        pytest.fail("real HD media FFmpeg coverage must run with APP_ENV=test", pytrace=False)
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            pytest.fail(f"real HD media FFmpeg coverage requires {executable}", pytrace=False)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("width", "height", "odd_container"),
    [
        (1440, 1080, False),
        (3840, 2160, False),
        (320, 180, False),
        (1439, 1081, True),
    ],
)
@pytest.mark.asyncio
async def test_process_upload_real_ffmpeg_hd_contract(
    ffmpeg_environment: None,
    monkeypatch,
    tmp_path: Path,
    width: int,
    height: int,
    odd_container: bool,
) -> None:
    media_root = tmp_path / "media-store"
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(
        media,
        "get_settings",
        lambda: SimpleNamespace(media_store_root=str(media_root), temp_root=str(temp_root)),
    )
    source_path = tmp_path / ("odd-source.mkv" if odd_container else "source.mp4")
    _make_source(source_path, width, height, odd_container=odd_container)
    source_payload = _ffprobe(source_path)
    source_video = _video_stream(source_payload)
    assert (source_video["width"], source_video["height"]) == (width, height)
    assert not [
        stream for stream in source_payload["streams"] if stream.get("codec_type") == "audio"
    ]

    processed = await media.process_upload(
        _BytesUpload(source_path.read_bytes()),
        content_type="video/webm" if odd_container else "video/mp4",
        rights_note="容器内真实 FFmpeg 合同测试夹具，非用户素材",
        lineup_id=uuid.uuid4(),
        request_id=uuid.uuid4().hex,
    )
    output_path = media_root / "public" / processed.relative_path
    poster_path = media_root / "public" / processed.poster_relative_path
    output_payload = _ffprobe(output_path)
    output_video = _video_stream(output_payload)
    output_streams = output_payload["streams"]
    assert isinstance(output_streams, list)
    output_audio = [stream for stream in output_streams if stream.get("codec_type") == "audio"]
    assert len(output_streams) == 2 and len(output_audio) == 1
    assert output_video["codec_name"] == "h264"
    assert output_video["pix_fmt"] == "yuv420p"
    assert output_video["avg_frame_rate"] == "30/1"
    assert output_video["r_frame_rate"] == "30/1"
    assert output_audio[0]["codec_name"] == "aac"
    assert output_audio[0]["sample_rate"] == "48000"
    assert output_audio[0]["channels"] == 2
    output_width = int(output_video["width"])
    output_height = int(output_video["height"])
    assert (output_width, output_height) == (1920, 1080)

    poster_payload = _ffprobe(poster_path)
    poster_video = _video_stream(poster_payload)
    assert poster_payload["format"]["format_name"] in {"webp", "webp_pipe", "image2"}
    assert poster_video["codec_name"] == "webp"
    poster_width = int(poster_video["width"])
    poster_height = int(poster_video["height"])
    assert poster_width == 1280
    expected_poster_height = media._poster_height_for_width(
        output_width, output_height, poster_width
    )
    assert poster_height == expected_poster_height
    assert poster_height >= 2 and poster_height % 2 == 0
    assert (poster_width, poster_height) == (1280, 720)
