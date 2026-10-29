"""Bounded, request-scoped audio validation and transcription orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.services.local_search import active_catalog, public_snapshot
from app.services.media import _prepare_safe_directory, _run_process
from app.services.normalization import normalize_text
from app.services.providers import (
    Transcription,
    speech_transcriber_factory,
)

MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 15.0
AUDIO_REQUEST_TIMEOUT_SECONDS = 25.0
AUDIO_CONVERSION_TIMEOUT_SECONDS = 5.0
AUDIO_PROBE_TIMEOUT_SECONDS = 5.0
AUDIO_PROCESSING_SLOTS = 2
_AUDIO_PROCESSING_SLOT = asyncio.Semaphore(AUDIO_PROCESSING_SLOTS)
_ALLOWED_AUDIO_MIMES = {"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg"}
_MIME_FORMATS = {
    "audio/webm": {"webm", "matroska"},
    "audio/ogg": {"ogg", "oga", "opus"},
    "audio/wav": {"wav", "wave", "wav_pipe"},
    "audio/mpeg": {"mp3", "mp2", "mpeg"},
}
_FIXED_HOTWORDS = ("Mirage", "荒漠迷城", "米垃圾", "VIP", "烟", "烟雾弹")
_logger = logging.getLogger("smokeline")


@dataclass(frozen=True, slots=True)
class AudioProbe:
    format_names: frozenset[str]
    duration: float
    audio_streams: int
    video_streams: int
    sample_rate: int | None = None
    channels: int | None = None
    codec_name: str | None = None


def _audio_error(code: str, message: str, status_code: int) -> AppError:
    return AppError(status_code, code, message)


def _entity_terms(entity: object) -> list[tuple[str, int, str]]:
    terms = getattr(entity, "terms", None)
    if terms is not None:
        values: list[tuple[str, int, str]] = []
        for term in terms:
            value = getattr(term, "value", None)
            normalized = getattr(term, "normalized", None)
            source = getattr(term, "source", "alias")
            if isinstance(value, str) and isinstance(normalized, str) and normalized:
                values.append((value, 4 if source == "alias" else 5, normalized))
        named = [item for item in values if item[1] == 5]
        named.sort(key=lambda item: 0 if item[0] == getattr(entity, "name_zh", None) else 1)
        aliases = sorted((item for item in values if item[1] == 4), key=lambda item: item[2])
        return named + aliases
    values = []
    for attribute in ("name_zh", "name_en"):
        value = getattr(entity, attribute, None)
        if isinstance(value, str) and value.strip():
            values.append((value, 5, normalize_text(value)))
    aliases = getattr(entity, "aliases", ())
    for value in sorted(
        (item for item in aliases if isinstance(item, str) and item.strip()),
        key=normalize_text,
    ):
        values.append((value, 4, normalize_text(value)))
    return values


def build_hotwords(targets: Iterable[object], origins: Iterable[object]) -> dict[str, int]:
    """Build a bounded provider vocabulary from fixed and active catalog terms."""

    candidates: list[tuple[str, int, str]] = [
        (value, 5, normalize_text(value)) for value in _FIXED_HOTWORDS
    ]

    def ordered(values: Iterable[object]) -> list[object]:
        return sorted(
            (item for item in values if getattr(item, "is_active", True)),
            key=lambda item: (
                int(getattr(item, "sort_order", 0)),
                str(getattr(item, "name_zh", "")),
                str(getattr(item, "id", "")),
            ),
        )

    target_entities = ordered(targets)
    origin_entities = ordered(origins)
    entities = [*target_entities, *origin_entities]
    for entity in entities:
        candidates.extend(item for item in _entity_terms(entity) if item[1] == 5)
    for entity in entities:
        candidates.extend(item for item in _entity_terms(entity) if item[1] == 4)

    result: dict[str, int] = {}
    seen: set[str] = set()
    for value, weight, normalized in candidates:
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result[value.strip()] = weight
        if len(result) >= 200:
            break
    return result


def _probe_duration(format_data: dict[str, Any], streams: list[Any]) -> float:
    first_stream = streams[0] if streams else None
    if isinstance(first_stream, dict):
        try:
            value = float(first_stream.get("duration"))
        except (TypeError, ValueError, OverflowError):
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    try:
        value = float(format_data.get("duration"))
    except (TypeError, ValueError, OverflowError):
        value = 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _packet_timeline_duration(payload: dict[str, Any]) -> float:
    packets = payload.get("packets")
    if not isinstance(packets, list):
        return 0.0

    earliest_start: float | None = None
    earliest_packet_duration = 0.0
    latest_end: float | None = None
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        timestamp: float | None = None
        for field in ("pts_time", "dts_time"):
            try:
                candidate = float(packet.get(field))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(candidate):
                timestamp = candidate
                break
        try:
            packet_duration = float(packet.get("duration_time"))
        except (TypeError, ValueError, OverflowError):
            packet_duration = 0.0
        if timestamp is None or not math.isfinite(packet_duration) or packet_duration <= 0:
            continue
        packet_end = timestamp + packet_duration
        if not math.isfinite(packet_end):
            continue
        if earliest_start is None or timestamp < earliest_start:
            earliest_start = timestamp
            earliest_packet_duration = packet_duration
        latest_end = packet_end if latest_end is None else max(latest_end, packet_end)

    if earliest_start is None or latest_end is None:
        return 0.0
    # Opus commonly exposes a small negative first PTS for codec pre-roll. Only
    # discard it when it fits inside that first packet; a large negative span
    # remains part of the measured timeline and cannot bypass the 15s limit.
    starts_with_codec_preroll = (
        earliest_start < 0 and abs(earliest_start) <= earliest_packet_duration + 1e-6
    )
    timeline_start = 0.0 if starts_with_codec_preroll else earliest_start
    duration = latest_end - timeline_start
    return duration if math.isfinite(duration) and duration > 0 else 0.0


async def _probe_packet_timeline(
    path: Path,
    timeout_seconds: float,
    *,
    deadline: float | None,
) -> float:
    try:
        stdout, _stderr = await _run_process(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_packets",
                "-show_entries",
                "packet=pts_time,dts_time,duration_time",
                "-of",
                "json",
                str(path),
            ],
            timeout_seconds,
        )
        payload = json.loads(stdout.decode("utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TimeoutError,
        subprocess.CalledProcessError,
    ):
        if deadline is not None and deadline - time.monotonic() <= 0:
            raise AppError(504, "ASR_TIMEOUT", "语音识别服务响应超时，请稍后重试。") from None
        return 0.0
    return _packet_timeline_duration(payload) if isinstance(payload, dict) else 0.0


async def _probe_audio(
    path: Path,
    declared_mime: str,
    timeout_seconds: float = AUDIO_PROBE_TIMEOUT_SECONDS,
    *,
    deadline: float | None = None,
) -> AudioProbe:
    probe_deadline = time.monotonic() + max(0.0, timeout_seconds)
    try:
        stdout, _stderr = await _run_process(
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
            timeout_seconds,
        )
        payload = json.loads(stdout.decode("utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TimeoutError,
        subprocess.CalledProcessError,
    ):
        if deadline is not None and deadline - time.monotonic() <= 0:
            raise AppError(504, "ASR_TIMEOUT", "语音识别服务响应超时，请稍后重试。") from None
        raise _audio_error("UNSUPPORTED_AUDIO_TYPE", "音频格式不受支持。", 415) from None
    if not isinstance(payload, dict):
        raise _audio_error("UNSUPPORTED_AUDIO_TYPE", "音频格式不受支持。", 415)
    format_data = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        raise _audio_error("UNSUPPORTED_AUDIO_TYPE", "音频格式不受支持。", 415)
    format_names = frozenset(
        part.strip().casefold()
        for part in str(format_data.get("format_name", "")).split(",")
        if part.strip()
    )
    if not format_names.intersection(_MIME_FORMATS.get(declared_mime, set())):
        raise _audio_error("UNSUPPORTED_AUDIO_TYPE", "音频格式与声明不一致。", 415)
    audios = [
        stream
        for stream in streams
        if isinstance(stream, dict)
        and stream.get("codec_type") == "audio"
        and isinstance(stream.get("codec_name"), str)
        and bool(stream.get("codec_name"))
    ]
    videos = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not audios or videos:
        raise _audio_error("UNSUPPORTED_AUDIO_TYPE", "音频流不受支持。", 415)
    duration = _probe_duration(format_data, audios)
    if duration <= 0:
        remaining = probe_deadline - time.monotonic()
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        if remaining > 0:
            duration = await _probe_packet_timeline(
                path,
                remaining,
                deadline=deadline,
            )
    if not math.isfinite(duration) or duration <= 0:
        raise _audio_error("UNSUPPORTED_AUDIO_TYPE", "音频时长无效。", 415)
    if duration > MAX_AUDIO_DURATION_SECONDS + 1e-6:
        raise _audio_error("AUDIO_TOO_LONG", "音频不能超过 15 秒。", 413)
    stream = audios[0]
    try:
        sample_rate = int(stream.get("sample_rate"))
    except (TypeError, ValueError, OverflowError):
        sample_rate = None
    try:
        channels = int(stream.get("channels"))
    except (TypeError, ValueError, OverflowError):
        channels = None
    return AudioProbe(
        format_names,
        duration,
        len(audios),
        len(videos),
        sample_rate,
        channels,
        str(stream.get("codec_name")),
    )


async def _write_audio_upload(upload: UploadFile, destination: Path) -> int:
    count = 0
    try:
        with destination.open("xb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_AUDIO_BYTES:
                    raise _audio_error("AUDIO_TOO_LARGE", "音频不能超过 5 MB。", 413)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except AppError:
        raise
    except OSError:
        raise AppError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。") from None
    return count


async def _convert_to_wav(source: Path, destination: Path, deadline: float) -> AudioProbe:
    remaining = min(AUDIO_CONVERSION_TIMEOUT_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        raise AppError(504, "ASR_TIMEOUT", "语音识别服务响应超时，请稍后重试。")
    try:
        await _run_process(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-sample_fmt",
                "s16",
                str(destination),
            ],
            remaining,
        )
    except (OSError, subprocess.CalledProcessError):
        raise AppError(415, "UNSUPPORTED_AUDIO_TYPE", "音频无法转换。") from None
    except TimeoutError:
        if deadline - time.monotonic() <= 0:
            raise AppError(504, "ASR_TIMEOUT", "语音识别服务响应超时，请稍后重试。") from None
        raise AppError(415, "UNSUPPORTED_AUDIO_TYPE", "音频无法转换。") from None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AppError(504, "ASR_TIMEOUT", "语音识别服务响应超时，请稍后重试。")
    converted = await _probe_audio(
        destination,
        "audio/wav",
        min(AUDIO_PROBE_TIMEOUT_SECONDS, remaining),
        deadline=deadline,
    )
    if (
        converted.sample_rate != 16000
        or converted.channels != 1
        or converted.codec_name != "pcm_s16le"
    ):
        raise AppError(415, "UNSUPPORTED_AUDIO_TYPE", "音频无法转换。")
    return converted


async def _catalog_hotwords() -> dict[str, int]:
    async with public_snapshot() as session:
        targets, origins = await active_catalog(session)
    return build_hotwords(targets, origins)


async def _close_upload_without_masking(upload: UploadFile) -> None:
    try:
        await upload.close()
    except Exception:
        _logger.error(
            "audio upload close failed",
            extra={"event": "AUDIO_TEMP_CLEANUP_FAILED", "error_code": "INTERNAL_ERROR"},
        )


async def transcribe_upload(
    upload: UploadFile,
    *,
    request_id: str,
    settings: Settings | None = None,
    deadline: float | None = None,
) -> tuple[Transcription, float]:
    """Validate, convert, transcribe, and remove one request's audio files."""

    active = settings or get_settings()
    declared_mime = (upload.content_type or "").split(";", 1)[0].strip().casefold()
    if declared_mime not in _ALLOWED_AUDIO_MIMES:
        await _close_upload_without_masking(upload)
        raise AppError(415, "UNSUPPORTED_AUDIO_TYPE", "音频格式不受支持。")
    request_deadline = deadline or (time.monotonic() + AUDIO_REQUEST_TIMEOUT_SECONDS)
    root = Path(active.temp_root) / "transcriptions"
    request_dir = root / uuid.uuid4().hex
    if not _prepare_safe_directory(request_dir, create=True):
        await _close_upload_without_masking(upload)
        raise AppError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。")
    raw_path = request_dir / "input"
    wav_path = request_dir / "audio.wav"
    acquired = False
    try:
        async with asyncio.timeout(max(0.001, request_deadline - time.monotonic())):
            await _write_audio_upload(upload, raw_path)
            try:
                acquired = await asyncio.wait_for(_AUDIO_PROCESSING_SLOT.acquire(), timeout=1.0)
            except TimeoutError:
                raise AppError(429, "AUDIO_PROCESSING_BUSY", "语音处理繁忙，请稍后重试。") from None
            if not acquired:
                raise AppError(429, "AUDIO_PROCESSING_BUSY", "语音处理繁忙，请稍后重试。")
            try:
                remaining = request_deadline - time.monotonic()
                if remaining <= 0:
                    raise AppError(504, "ASR_TIMEOUT", "语音识别服务响应超时，请稍后重试。")
                original = await _probe_audio(
                    raw_path,
                    declared_mime,
                    min(AUDIO_PROBE_TIMEOUT_SECONDS, remaining),
                    deadline=request_deadline,
                )
                await _convert_to_wav(raw_path, wav_path, request_deadline)
            finally:
                _AUDIO_PROCESSING_SLOT.release()
                acquired = False
            hotwords = await _catalog_hotwords()
            provider = speech_transcriber_factory(
                settings=active,
                request_id=request_id,
                input_mode="voice",
                deadline=request_deadline,
                duration_seconds=original.duration,
            )
            result = await provider.transcribe(wav_path, hotwords)
            text = result.text.strip()
            if not text:
                raise AppError(422, "EMPTY_TRANSCRIPTION", "未识别到有效语音内容。")
            if len(text) > 200:
                raise AppError(422, "TRANSCRIPTION_TOO_LONG", "识别结果不能超过 200 个字符。")
            # The public duration is the source recording's local probe, not
            # the provider usage or a potentially re-timed conversion.
            return Transcription(text, result.usage_duration), round(original.duration, 3)
    except TimeoutError:
        raise AppError(504, "ASR_TIMEOUT", "语音识别服务响应超时，请稍后重试。") from None
    finally:
        primary_exception = sys.exc_info()[1]
        cleanup_failed = False
        cleanup_cancelled: BaseException | None = None
        if acquired:
            _AUDIO_PROCESSING_SLOT.release()
        try:
            await upload.close()
        except asyncio.CancelledError as exc:
            cleanup_failed = True
            cleanup_cancelled = exc
        except Exception:
            cleanup_failed = True
        for path in (raw_path, wav_path):
            try:
                if path.exists() and not path.is_symlink():
                    path.unlink()
            except OSError:
                cleanup_failed = True
        try:
            if request_dir.is_dir() and not request_dir.is_symlink():
                request_dir.rmdir()
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            _logger.error(
                "audio temporary resource cleanup failed",
                extra={
                    "event": "AUDIO_TEMP_CLEANUP_FAILED",
                    "error_code": "INTERNAL_ERROR",
                },
            )
            if cleanup_cancelled is not None and primary_exception is None:
                raise cleanup_cancelled
            if primary_exception is None:
                raise AppError(500, "INTERNAL_ERROR", "服务暂时异常，请凭请求编号重试。")
