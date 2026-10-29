from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import UploadFile

from app.core.config import Settings
from app.core.errors import AppError


def test_asr_quota_uses_its_own_limit_and_error() -> None:
    from app.services.providers import _quota_error, _quota_limit

    settings = Settings(daily_asr_seconds_limit=37)

    assert _quota_limit(settings, "asr") == 37
    assert _quota_error("asr").code == "DAILY_ASR_QUOTA_EXHAUSTED"


def test_transcription_response_has_only_text_and_duration() -> None:
    from app.schemas.public import TranscriptionResponse

    response = TranscriptionResponse(text="我在 Mirage 的 T 出生点", duration_seconds=4.2)
    assert response.model_dump() == {
        "text": "我在 Mirage 的 T 出生点",
        "duration_seconds": 4.2,
    }
    with pytest.raises(ValueError):
        TranscriptionResponse(text="ok", duration_seconds=-1)


@pytest.mark.asyncio
async def test_fake_speech_transcriber_is_fixed_and_network_free(tmp_path: Path) -> None:
    from app.services.providers import speech_transcriber_factory

    async def should_not_reserve(*_args, **_kwargs):
        raise AssertionError("fake ASR must not reserve quota")

    provider = speech_transcriber_factory(
        settings=Settings(app_env="test", ai_provider="fake"),
        request_id=str(uuid4()),
        duration_seconds=4.6,
        reservation_fn=should_not_reserve,
    )
    result = await provider.transcribe(tmp_path / "recording.wav", [])

    assert result.text == "我在 Mirage 的 T 出生点，想封 VIP 烟"
    assert result.usage_duration == 4.6


@pytest.mark.asyncio
async def test_disabled_speech_transcriber_fails_closed_without_reservation(tmp_path: Path) -> None:
    from app.services.providers import speech_transcriber_factory

    async def should_not_reserve(*_args, **_kwargs):
        raise AssertionError("disabled ASR must not reserve quota")

    provider = speech_transcriber_factory(
        settings=Settings(ai_provider="disabled"),
        request_id=str(uuid4()),
        duration_seconds=1,
        reservation_fn=should_not_reserve,
    )
    with pytest.raises(AppError) as error:
        await provider.transcribe(tmp_path / "recording.wav", [])

    assert error.value.status_code == 503
    assert error.value.code == "ASR_UNAVAILABLE"


def test_hotwords_keep_first_spelling_and_dedupe_normalized_values() -> None:
    from app.services.audio import build_hotwords

    target = SimpleNamespace(
        name_zh="VIP",
        name_en="VIP Site",
        aliases=[" vip ", "贵宾"],
        sort_order=1,
        id="target-1",
    )
    origin = SimpleNamespace(
        name_zh="T 出生点",
        name_en="T Spawn",
        aliases=["t-spawn"],
        sort_order=1,
        id="origin-1",
    )

    words = build_hotwords([target], [origin])
    assert words["Mirage"] == 5
    assert words["VIP"] == 5
    assert " vip " not in words
    assert words["贵宾"] == 4
    assert words["T Spawn"] == 5
    assert list(words)[:6] == ["Mirage", "荒漠迷城", "米垃圾", "VIP", "烟", "烟雾弹"]
    assert list(words).index("VIP Site") < list(words).index("贵宾")


@pytest.mark.asyncio
async def test_dashscope_asr_wire_contract_and_ceiling_reservation(tmp_path: Path) -> None:
    from app.services.providers import QuotaReservation, speech_transcriber_factory

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav-bytes")
    reservations: list[tuple[str, object]] = []
    outcomes: list[tuple[str, str]] = []

    async def reserve(provider_type: str, units: object) -> QuotaReservation:
        reservations.append((provider_type, units))
        return QuotaReservation(uuid4(), uuid4(), provider_type, units)  # type: ignore[arg-type]

    async def finish(reservation: QuotaReservation, outcome: str, **_kwargs: object) -> None:
        outcomes.append((reservation.provider_type, outcome))

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = httpx.Response  # keep the handler body visibly request-scoped
        del payload
        body = __import__("json").loads(request.content)
        assert str(request.url).endswith("/api/v1/services/aigc/multimodal-generation/generation")
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["x-dashscope-sse"] == "disable"
        assert body["input"]["messages"][0]["content"][0]["type"] == "input_audio"
        assert body["input"]["messages"][0]["content"][0]["input_audio"]["data"].startswith(
            "data:audio/wav;base64,"
        )
        assert body["parameters"]["format"] == "wav"
        assert body["parameters"]["sample_rate"] == "16000"
        assert body["parameters"]["vocabulary"] == {"VIP": 5}
        assert body["parameters"]["language_hints"] == ["zh", "en"]
        return httpx.Response(200, json={"output": {"text": " VIP "}, "usage": {"duration": 1.2}})

    settings = Settings(
        app_env="test",
        ai_provider="dashscope",
        dashscope_api_key="test-key",
        dashscope_api_host="dashscope.cn-beijing.maas.aliyuncs.com",
    )
    provider = speech_transcriber_factory(
        settings=settings,
        request_id=str(uuid4()),
        duration_seconds=2.1,
        reservation_fn=reserve,
        outcome_fn=finish,
        deadline=__import__("time").monotonic() + 5,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.transcribe(audio_path, {"VIP": 5})

    assert result.text == " VIP "
    assert result.usage_duration == 1.2
    assert reservations[0][0] == "asr"
    assert str(reservations[0][1]) == "3"
    assert outcomes == [("asr", "success")]


@pytest.mark.asyncio
async def test_dashscope_asr_logs_do_not_contain_audio_or_provider_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from app.services.providers import QuotaReservation, speech_transcriber_factory

    audio_bytes = b"known-audio-secret-fragment"
    transcript = "已知转写原文-不应进入日志"
    provider_body = "供应商正文-不应进入日志"
    authorization = "authorization-secret"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(audio_bytes)

    async def reserve(_provider_type: str, units: object) -> QuotaReservation:
        return QuotaReservation(uuid4(), uuid4(), "asr", units)  # type: ignore[arg-type]

    async def finish(_reservation: QuotaReservation, _outcome: str, **_kwargs: object) -> None:
        return None

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": {"text": transcript},
                "usage": {"duration": 1},
                "provider_body": provider_body,
            },
        )

    provider = speech_transcriber_factory(
        settings=Settings(
            app_env="test",
            ai_provider="dashscope",
            dashscope_api_key=authorization,
            dashscope_api_host="dashscope.cn-beijing.maas.aliyuncs.com",
        ),
        duration_seconds=1,
        reservation_fn=reserve,
        outcome_fn=finish,
        deadline=time.monotonic() + 5,
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level("DEBUG", logger="smokeline"):
        result = await provider.transcribe(audio_path, {})

    assert result.text == transcript
    for secret in (
        transcript,
        base64.b64encode(audio_bytes).decode("ascii"),
        authorization,
        f"Bearer {authorization}",
        provider_body,
    ):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_dashscope_asr_retries_only_first_retryable_attempt(tmp_path: Path) -> None:
    from app.services.providers import QuotaReservation, speech_transcriber_factory

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav-bytes")
    statuses = iter((503, 200))
    reservations: list[object] = []
    outcomes: list[str] = []

    async def reserve(_provider_type: str, units: object) -> QuotaReservation:
        reservations.append(units)
        return QuotaReservation(uuid4(), uuid4(), "asr", units)  # type: ignore[arg-type]

    async def finish(_reservation: QuotaReservation, outcome: str, **_kwargs: object) -> None:
        outcomes.append(outcome)

    async def handler(_request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 503:
            return httpx.Response(status)
        return httpx.Response(200, json={"output": {"text": "ok"}, "usage": {"duration": 1}})

    provider = speech_transcriber_factory(
        settings=Settings(
            app_env="test",
            ai_provider="dashscope",
            dashscope_api_key="test-key",
            dashscope_api_host="dashscope.cn-beijing.maas.aliyuncs.com",
        ),
        duration_seconds=1.1,
        reservation_fn=reserve,
        outcome_fn=finish,
        deadline=__import__("time").monotonic() + 5,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.transcribe(audio_path, {})

    assert result.text == "ok"
    assert len(reservations) == 2
    assert outcomes == ["failed", "success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["429", "connect"])
async def test_dashscope_asr_retries_429_or_connection_once_then_succeeds(
    tmp_path: Path, first_failure: str
) -> None:
    from app.services.providers import QuotaReservation, speech_transcriber_factory

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav-bytes")
    calls = 0
    reservations: list[QuotaReservation] = []
    outcomes: list[str] = []

    async def reserve(_provider_type: str, units: object) -> QuotaReservation:
        reservation = QuotaReservation(uuid4(), uuid4(), "asr", units)  # type: ignore[arg-type]
        reservations.append(reservation)
        return reservation

    async def finish(_reservation: QuotaReservation, outcome: str, **_kwargs: object) -> None:
        outcomes.append(outcome)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1 and first_failure == "429":
            return httpx.Response(429)
        if calls == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"output": {"text": "ok"}, "usage": {"duration": 1}})

    provider = speech_transcriber_factory(
        settings=Settings(
            app_env="test",
            ai_provider="dashscope",
            dashscope_api_key="test-key",
            dashscope_api_host="dashscope.cn-beijing.maas.aliyuncs.com",
        ),
        duration_seconds=1,
        reservation_fn=reserve,
        outcome_fn=finish,
        deadline=time.monotonic() + 5,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.transcribe(audio_path, {})

    assert result.text == "ok"
    assert calls == 2
    assert len(reservations) == 2
    assert outcomes == ["failed", "success"]


@pytest.mark.asyncio
async def test_transcribe_upload_uses_real_probe_contract_and_cleans_temp_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import audio

    original_probe = {
        "format": {"format_name": "webm", "duration": "1.234"},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "opus",
                "duration": "1.234",
                "sample_rate": "48000",
                "channels": 2,
            }
        ],
    }
    converted_probe = {
        "format": {"format_name": "wav", "duration": "1.234"},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "duration": "1.234",
                "sample_rate": "16000",
                "channels": 1,
            }
        ],
    }
    commands: list[list[str]] = []

    async def run_process(args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        commands.append(args)
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"converted-wav")
            return b"", b""
        payload = converted_probe if Path(args[-1]).name == "audio.wav" else original_probe
        return json.dumps(payload).encode(), b""

    monkeypatch.setattr(audio, "_run_process", run_process)
    monkeypatch.setattr(audio, "_catalog_hotwords", lambda: asyncio.sleep(0, result={"VIP": 5}))
    settings = Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path))
    upload = UploadFile(BytesIO(b"recording"), filename="client-name.webm", headers=None)
    upload.headers = httpx.Headers({"content-type": "audio/webm"})  # type: ignore[attr-defined]

    result, duration = await audio.transcribe_upload(
        upload,
        request_id=str(uuid4()),
        settings=settings,
        deadline=time.monotonic() + 25,
    )

    assert result.text == "我在 Mirage 的 T 出生点，想封 VIP 烟"
    assert duration == 1.234
    ffmpeg_command = next(command for command in commands if command[0] == "ffmpeg")
    assert ffmpeg_command[ffmpeg_command.index("-map") + 1] == "0:a:0"
    assert ffmpeg_command[ffmpeg_command.index("-vn") + 1] == "-ac"
    assert ffmpeg_command[ffmpeg_command.index("-ac") + 1] == "1"
    assert ffmpeg_command[ffmpeg_command.index("-ar") + 1] == "16000"
    assert ffmpeg_command[ffmpeg_command.index("-c:a") + 1] == "pcm_s16le"
    assert ffmpeg_command[ffmpeg_command.index("-sample_fmt") + 1] == "s16"
    assert list((tmp_path / "transcriptions").rglob("*")) == []


@pytest.mark.asyncio
async def test_conversion_rejects_non_pcm_s16le_even_when_rate_and_channels_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import audio

    source_probe = {
        "format": {"format_name": "webm", "duration": "1"},
        "streams": [{"codec_type": "audio", "codec_name": "opus", "duration": "1"}],
    }
    wrong_codec_probe = {
        "format": {"format_name": "wav", "duration": "1"},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_mulaw",
                "duration": "1",
                "sample_rate": "16000",
                "channels": 1,
            }
        ],
    }
    commands: list[list[str]] = []

    async def run_process(args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        commands.append(args)
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"converted-wav")
            return b"", b""
        payload = wrong_codec_probe if Path(args[-1]).name == "output.wav" else source_probe
        return json.dumps(payload).encode(), b""

    monkeypatch.setattr(audio, "_run_process", run_process)
    with pytest.raises(AppError) as error:
        await audio._convert_to_wav(
            tmp_path / "input.webm", tmp_path / "output.wav", time.monotonic() + 5
        )

    assert (error.value.status_code, error.value.code) == (415, "UNSUPPORTED_AUDIO_TYPE")
    ffmpeg_command = next(command for command in commands if command[0] == "ffmpeg")
    assert ffmpeg_command[ffmpeg_command.index("-c:a") + 1] == "pcm_s16le"


@pytest.mark.asyncio
async def test_transcribe_upload_stops_streaming_at_audio_size_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import audio

    probe_called = False

    async def probe(*_args: object, **_kwargs: object):
        nonlocal probe_called
        probe_called = True
        raise AssertionError("oversized input must stop before ffprobe")

    monkeypatch.setattr(audio, "_probe_audio", probe)
    upload = UploadFile(
        BytesIO(b"x" * (audio.MAX_AUDIO_BYTES + 1)), filename="too-large.webm", headers=None
    )
    upload.headers = httpx.Headers({"content-type": "audio/webm"})  # type: ignore[attr-defined]
    with pytest.raises(AppError) as error:
        await audio.transcribe_upload(
            upload,
            request_id=str(uuid4()),
            settings=Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path)),
        )

    assert error.value.status_code == 413
    assert error.value.code == "AUDIO_TOO_LARGE"
    assert not probe_called
    assert list((tmp_path / "transcriptions").rglob("*")) == []


@pytest.mark.asyncio
async def test_write_audio_upload_accepts_exact_maximum_size_and_cleans_file(
    tmp_path: Path,
) -> None:
    from app.services import audio

    destination = tmp_path / "input"
    upload = UploadFile(
        BytesIO(b"x" * audio.MAX_AUDIO_BYTES), filename="recording.webm", headers=None
    )
    try:
        count = await audio._write_audio_upload(upload, destination)
        assert count == audio.MAX_AUDIO_BYTES
        assert destination.stat().st_size == audio.MAX_AUDIO_BYTES
    finally:
        await upload.close()
        if destination.exists():
            destination.unlink()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_code"),
    [("   ", "EMPTY_TRANSCRIPTION"), ("x" * 201, "TRANSCRIPTION_TOO_LONG")],
)
async def test_transcription_text_boundaries_are_rejected_without_truncation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text: str,
    expected_code: str,
) -> None:
    from app.services import audio
    from app.services.providers import Transcription

    async def probe(*_args: object, **_kwargs: object) -> audio.AudioProbe:
        return audio.AudioProbe(frozenset({"webm"}), 2.3456, 1, 0, 48000, 2, "opus")

    async def convert(*_args: object, **_kwargs: object) -> audio.AudioProbe:
        return audio.AudioProbe(frozenset({"wav"}), 2.3456, 1, 0, 16000, 1, "pcm_s16le")

    class Provider:
        async def transcribe(self, _path: Path, _hotwords: dict[str, int]) -> Transcription:
            return Transcription(text, 0.5)

    monkeypatch.setattr(audio, "_probe_audio", probe)
    monkeypatch.setattr(audio, "_convert_to_wav", convert)
    monkeypatch.setattr(audio, "_catalog_hotwords", lambda: asyncio.sleep(0, result={}))
    monkeypatch.setattr(audio, "speech_transcriber_factory", lambda **_kwargs: Provider())
    upload = UploadFile(BytesIO(b"recording"), filename="recording.webm", headers=None)
    upload.headers = httpx.Headers({"content-type": "audio/webm"})  # type: ignore[attr-defined]

    with pytest.raises(AppError) as error:
        await audio.transcribe_upload(
            upload,
            request_id=str(uuid4()),
            settings=Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path)),
        )

    assert error.value.code == expected_code
    assert list((tmp_path / "transcriptions").rglob("*")) == []


@pytest.mark.asyncio
async def test_invalid_declared_mime_closes_upload_without_creating_temp_files(
    tmp_path: Path,
) -> None:
    from app.services import audio

    upload = UploadFile(BytesIO(b"not-an-audio"), filename="recording.mp4", headers=None)
    upload.headers = httpx.Headers({"content-type": "video/mp4"})  # type: ignore[attr-defined]

    with pytest.raises(AppError) as error:
        await audio.transcribe_upload(
            upload,
            request_id=str(uuid4()),
            settings=Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path)),
        )

    assert (error.value.status_code, error.value.code) == (415, "UNSUPPORTED_AUDIO_TYPE")
    assert upload.file.closed
    assert not (tmp_path / "transcriptions").exists()


@pytest.mark.asyncio
async def test_dashscope_asr_does_not_call_upstream_after_reservation_budget_expires(
    tmp_path: Path,
) -> None:
    from app.services.providers import QuotaReservation, speech_transcriber_factory

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav-bytes")
    reservations: list[object] = []
    outcomes: list[tuple[str, str]] = []
    deadline = time.monotonic() + 1.5

    async def reserve(_provider_type: str, units: object) -> QuotaReservation:
        await asyncio.sleep(0.7)
        reservation = QuotaReservation(uuid4(), uuid4(), "asr", units)  # type: ignore[arg-type]
        reservations.append(reservation)
        return reservation

    async def finish(reservation: QuotaReservation, outcome: str, **_kwargs: object) -> None:
        outcomes.append((str(reservation.id), outcome))

    async def upstream_must_not_be_called(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an ASR request is forbidden with <=1 second remaining")

    provider = speech_transcriber_factory(
        settings=Settings(
            app_env="test",
            ai_provider="dashscope",
            dashscope_api_key="test-key",
            dashscope_api_host="dashscope.cn-beijing.maas.aliyuncs.com",
        ),
        duration_seconds=1.0,
        reservation_fn=reserve,
        outcome_fn=finish,
        deadline=deadline,
        transport=httpx.MockTransport(upstream_must_not_be_called),
    )

    with pytest.raises(AppError) as error:
        await provider.transcribe(audio_path, {})

    assert (error.value.status_code, error.value.code) == (504, "ASR_TIMEOUT")
    assert len(reservations) == 1
    assert outcomes == [(str(reservations[0].id), "failed")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(400), "ASR_UPSTREAM_ERROR"),
        (httpx.Response(200, content=b"not-json"), "ASR_BAD_RESPONSE"),
        (
            httpx.Response(200, json={"output": {}, "usage": {"duration": 1}}),
            "ASR_BAD_RESPONSE",
        ),
        (
            httpx.Response(200, json={"output": {"text": "ok"}, "usage": {"duration": -1}}),
            "ASR_BAD_RESPONSE",
        ),
        (
            httpx.Response(200, json={"output": {"text": "ok"}, "usage": {"duration": True}}),
            "ASR_BAD_RESPONSE",
        ),
        (
            httpx.Response(
                200,
                content=b'{"output":{"text":"ok"},"usage":{"duration":NaN}}',
            ),
            "ASR_BAD_RESPONSE",
        ),
        (
            httpx.Response(
                200,
                content=b'{"output":{"text":"ok"},"usage":{"duration":Infinity}}',
            ),
            "ASR_BAD_RESPONSE",
        ),
    ],
)
async def test_dashscope_asr_non_retryable_failures_use_one_attempt(
    tmp_path: Path, response: httpx.Response, expected_code: str
) -> None:
    from app.services.providers import QuotaReservation, speech_transcriber_factory

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav-bytes")
    calls = 0
    reservations: list[QuotaReservation] = []
    outcomes: list[str] = []

    async def reserve(_provider_type: str, units: object) -> QuotaReservation:
        reservation = QuotaReservation(uuid4(), uuid4(), "asr", units)  # type: ignore[arg-type]
        reservations.append(reservation)
        return reservation

    async def finish(_reservation: QuotaReservation, outcome: str, **_kwargs: object) -> None:
        outcomes.append(outcome)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    provider = speech_transcriber_factory(
        settings=Settings(
            app_env="test",
            ai_provider="dashscope",
            dashscope_api_key="test-key",
            dashscope_api_host="dashscope.cn-beijing.maas.aliyuncs.com",
        ),
        duration_seconds=1,
        reservation_fn=reserve,
        outcome_fn=finish,
        deadline=time.monotonic() + 5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AppError) as error:
        await provider.transcribe(audio_path, {})

    assert error.value.code == expected_code
    assert calls == 1
    assert len(reservations) == 1
    assert outcomes == ["failed"]


@pytest.mark.asyncio
async def test_dashscope_asr_timeout_is_not_retried(tmp_path: Path) -> None:
    from app.services.providers import QuotaReservation, speech_transcriber_factory

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav-bytes")
    calls = 0
    reservations: list[QuotaReservation] = []
    outcomes: list[str] = []

    async def reserve(_provider_type: str, units: object) -> QuotaReservation:
        reservation = QuotaReservation(uuid4(), uuid4(), "asr", units)  # type: ignore[arg-type]
        reservations.append(reservation)
        return reservation

    async def finish(_reservation: QuotaReservation, outcome: str, **_kwargs: object) -> None:
        outcomes.append(outcome)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("test timeout")

    provider = speech_transcriber_factory(
        settings=Settings(
            app_env="test",
            ai_provider="dashscope",
            dashscope_api_key="test-key",
            dashscope_api_host="dashscope.cn-beijing.maas.aliyuncs.com",
        ),
        duration_seconds=1,
        reservation_fn=reserve,
        outcome_fn=finish,
        deadline=time.monotonic() + 5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AppError) as error:
        await provider.transcribe(audio_path, {})

    assert (error.value.status_code, error.value.code) == (504, "ASR_TIMEOUT")
    assert calls == 1
    assert len(reservations) == 1
    assert outcomes == ["failed"]


def test_hotwords_exclude_inactive_entities_and_truncate_after_two_hundred_terms() -> None:
    from app.services.audio import build_hotwords

    inactive = SimpleNamespace(
        name_zh="已归档目标",
        name_en="Archived Target",
        aliases=["archived-alias"],
        is_active=False,
        sort_order=0,
        id="inactive",
    )
    active = [
        SimpleNamespace(
            name_zh=f"目标{i:03d}",
            name_en=None,
            aliases=[f"别名{i:03d}", f"alt{i:03d}"],
            is_active=True,
            sort_order=i,
            id=f"active-{i:03d}",
        )
        for i in range(100)
    ]

    words = build_hotwords([inactive, *active], [])

    assert len(words) == 200
    assert "已归档目标" not in words
    assert "archived-alias" not in words
    assert list(words)[:6] == ["Mirage", "荒漠迷城", "米垃圾", "VIP", "烟", "烟雾弹"]
    assert words["目标000"] == 5
    assert words["别名000"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared_mime", "format_name"),
    [
        ("audio/webm", "webm"),
        ("audio/ogg", "ogg"),
        ("audio/wav", "wav"),
        ("audio/mpeg", "mp3"),
    ],
)
async def test_probe_requires_declared_mime_to_match_real_audio_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    declared_mime: str,
    format_name: str,
) -> None:
    from app.services import audio

    async def probe(_args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        return (
            json.dumps(
                {
                    "format": {"format_name": format_name, "duration": "15.000"},
                    "streams": [
                        {
                            "codec_type": "audio",
                            "codec_name": "pcm_s16le",
                            "duration": "15.000",
                        }
                    ],
                }
            ).encode(),
            b"",
        )

    monkeypatch.setattr(audio, "_run_process", probe)
    result = await audio._probe_audio(tmp_path / "client-name.bin", declared_mime)

    assert result.duration == 15.0
    assert result.audio_streams == 1
    assert result.video_streams == 0


@pytest.mark.asyncio
async def test_probe_uses_packet_timeline_when_mobile_webm_omits_header_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services import audio

    metadata = {
        "format": {"format_name": "webm"},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "opus",
                "sample_rate": "48000",
                "channels": 1,
            }
        ],
    }
    packet_timeline = {
        "packets": [
            {"pts_time": "-0.007", "dts_time": "-0.007", "duration_time": "0.020"},
            {"pts_time": "1.000", "dts_time": "1.000", "duration_time": "0.020"},
            {"pts_time": "2.980", "dts_time": "2.980", "duration_time": "0.020"},
        ]
    }
    commands: list[list[str]] = []

    async def probe(args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        commands.append(args)
        payload = packet_timeline if "-show_packets" in args else metadata
        return json.dumps(payload).encode(), b""

    monkeypatch.setattr(audio, "_run_process", probe)

    result = await audio._probe_audio(tmp_path / "mobile-recording.webm", "audio/webm")

    assert result.duration == pytest.approx(3.0)
    assert len(commands) == 2
    assert commands[1][commands[1].index("-select_streams") + 1] == "a:0"
    assert commands[1][commands[1].index("-show_entries") + 1] == (
        "packet=pts_time,dts_time,duration_time"
    )


@pytest.mark.asyncio
async def test_packet_timeline_still_rejects_mobile_audio_over_15_seconds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services import audio

    metadata = {
        "format": {"format_name": "webm"},
        "streams": [{"codec_type": "audio", "codec_name": "opus"}],
    }
    packet_timeline = {
        "packets": [
            {"pts_time": "0.000", "duration_time": "0.020"},
            {"pts_time": "15.000", "duration_time": "0.020"},
        ]
    }

    async def probe(args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        payload = packet_timeline if "-show_packets" in args else metadata
        return json.dumps(payload).encode(), b""

    monkeypatch.setattr(audio, "_run_process", probe)

    with pytest.raises(AppError) as error:
        await audio._probe_audio(tmp_path / "too-long-mobile.webm", "audio/webm")

    assert (error.value.status_code, error.value.code) == (413, "AUDIO_TOO_LONG")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_ffmpeg_streamed_webm_without_header_duration_is_accepted(
    tmp_path: Path,
) -> None:
    if os.environ.get("PHASE2_INTEGRATION_REQUIRED") != "1":
        pytest.skip("real audio FFmpeg coverage requires the isolated test stack")
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            pytest.fail(f"real audio FFmpeg coverage requires {executable}", pytrace=False)

    from app.services import audio

    path = tmp_path / "streamed-mobile-like.webm"
    generated = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=1.2:sample_rate=48000",
            "-c:a",
            "libopus",
            "-f",
            "webm",
            "pipe:1",
        ],
        check=False,
        capture_output=True,
    )
    assert generated.returncode == 0, generated.stderr.decode(errors="replace")
    path.write_bytes(generated.stdout)

    metadata = subprocess.run(
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
    payload = json.loads(metadata.stdout)
    assert audio._probe_duration(payload["format"], payload["streams"]) == 0

    result = await audio._probe_audio(path, "audio/webm")

    assert 1.1 < result.duration < 1.3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared_mime", "payload"),
    [
        (
            "audio/webm",
            json.dumps(
                {
                    "format": {"format_name": "wav", "duration": "1"},
                    "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
                }
            ).encode(),
        ),
        ("audio/webm", b"{ffprobe output was truncated"),
    ],
)
async def test_probe_rejects_declared_container_mismatch_and_corrupt_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    declared_mime: str,
    payload: bytes,
) -> None:
    from app.services import audio

    async def probe(_args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        return payload, b""

    monkeypatch.setattr(audio, "_run_process", probe)
    with pytest.raises(AppError) as error:
        await audio._probe_audio(tmp_path / "input", declared_mime)

    assert (error.value.status_code, error.value.code) == (415, "UNSUPPORTED_AUDIO_TYPE")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("streams", "duration", "expected_code"),
    [
        ([], "1", "UNSUPPORTED_AUDIO_TYPE"),
        (
            [
                {"codec_type": "audio", "codec_name": "opus", "duration": "1"},
                {"codec_type": "video", "codec_name": "h264", "duration": "1"},
            ],
            "1",
            "UNSUPPORTED_AUDIO_TYPE",
        ),
        (
            [{"codec_type": "audio", "codec_name": "opus", "duration": "0"}],
            "0",
            "UNSUPPORTED_AUDIO_TYPE",
        ),
        (
            [{"codec_type": "audio", "codec_name": "opus", "duration": "15.001"}],
            "15.001",
            "AUDIO_TOO_LONG",
        ),
        (
            [
                {"codec_type": "audio", "codec_name": "opus", "duration": "15.001"},
                {"codec_type": "audio", "codec_name": "opus", "duration": "1"},
            ],
            "1",
            "AUDIO_TOO_LONG",
        ),
    ],
)
async def test_probe_rejects_missing_video_or_duration_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    streams: list[dict[str, str]],
    duration: str,
    expected_code: str,
) -> None:
    from app.services import audio

    async def probe(_args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        return (
            json.dumps(
                {"format": {"format_name": "webm", "duration": duration}, "streams": streams}
            ).encode(),
            b"",
        )

    monkeypatch.setattr(audio, "_run_process", probe)
    with pytest.raises(AppError) as error:
        await audio._probe_audio(tmp_path / "input", "audio/webm")

    assert error.value.code == expected_code


@pytest.mark.asyncio
async def test_probe_timeout_uses_request_timeout_when_deadline_is_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import audio

    async def timeout(_args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        raise TimeoutError

    monkeypatch.setattr(audio, "_run_process", timeout)
    with pytest.raises(AppError) as error:
        await audio._probe_audio(
            tmp_path / "input",
            "audio/webm",
            timeout_seconds=0.01,
            deadline=time.monotonic() - 1,
        )

    assert (error.value.status_code, error.value.code) == (504, "ASR_TIMEOUT")


@pytest.mark.asyncio
async def test_conversion_budget_exhaustion_is_not_reported_as_bad_audio(
    tmp_path: Path,
) -> None:
    from app.services import audio

    with pytest.raises(AppError) as error:
        await audio._convert_to_wav(
            tmp_path / "input",
            tmp_path / "output.wav",
            time.monotonic() - 1,
        )

    assert (error.value.status_code, error.value.code) == (504, "ASR_TIMEOUT")


@pytest.mark.asyncio
async def test_audio_processing_busy_returns_without_running_ffprobe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import audio

    class BusySlot:
        async def acquire(self) -> bool:
            return False

        def release(self) -> None:
            raise AssertionError("busy slot must not be released when acquire failed")

    async def probe(*_args: object, **_kwargs: object):
        raise AssertionError("busy request must not run ffprobe")

    monkeypatch.setattr(audio, "_AUDIO_PROCESSING_SLOT", BusySlot())
    monkeypatch.setattr(audio, "_probe_audio", probe)
    upload = UploadFile(BytesIO(b"recording"), filename="recording.webm", headers=None)
    upload.headers = httpx.Headers({"content-type": "audio/webm"})  # type: ignore[attr-defined]

    with pytest.raises(AppError) as error:
        await audio.transcribe_upload(
            upload,
            request_id=str(uuid4()),
            settings=Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path)),
        )

    assert (error.value.status_code, error.value.code) == (429, "AUDIO_PROCESSING_BUSY")
    assert list((tmp_path / "transcriptions").rglob("*")) == []


@pytest.mark.asyncio
async def test_audio_processing_semaphore_limits_probe_and_convert_to_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import audio
    from app.services.providers import Transcription

    monkeypatch.setattr(audio, "_AUDIO_PROCESSING_SLOT", asyncio.Semaphore(2))
    probe_release = asyncio.Event()
    convert_release = asyncio.Event()
    first_two_probes = asyncio.Event()
    first_two_converts = asyncio.Event()
    active = 0
    max_active = 0
    probe_calls = 0
    convert_calls = 0

    async def hold(release: asyncio.Event) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await release.wait()
        finally:
            active -= 1

    async def fake_probe(*_args: object, **_kwargs: object) -> audio.AudioProbe:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 2:
            first_two_probes.set()
        await hold(probe_release)
        return audio.AudioProbe(frozenset({"webm"}), 1.0, 1, 0, 48000, 2, "opus")

    async def fake_convert(*_args: object, **_kwargs: object) -> audio.AudioProbe:
        nonlocal convert_calls
        convert_calls += 1
        if convert_calls == 2:
            first_two_converts.set()
        await hold(convert_release)
        return audio.AudioProbe(frozenset({"wav"}), 1.0, 1, 0, 16000, 1, "pcm_s16le")

    class Provider:
        async def transcribe(self, _path: Path, _hotwords: dict[str, int]) -> Transcription:
            return Transcription("ok", 1.0)

    monkeypatch.setattr(audio, "_probe_audio", fake_probe)
    monkeypatch.setattr(audio, "_convert_to_wav", fake_convert)
    monkeypatch.setattr(audio, "_catalog_hotwords", lambda: asyncio.sleep(0, result={}))
    monkeypatch.setattr(audio, "speech_transcriber_factory", lambda **_kwargs: Provider())
    uploads = [
        UploadFile(BytesIO(b"recording"), filename=f"recording-{index}.webm", headers=None)
        for index in range(3)
    ]
    for upload in uploads:
        upload.headers = httpx.Headers({"content-type": "audio/webm"})  # type: ignore[attr-defined]
    settings = Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path))
    tasks = [
        asyncio.create_task(
            audio.transcribe_upload(upload, request_id=str(uuid4()), settings=settings)
        )
        for upload in uploads
    ]
    try:
        await asyncio.wait_for(first_two_probes.wait(), timeout=1)
        assert probe_calls == 2
        assert max_active == 2
        assert not first_two_converts.is_set()

        probe_release.set()
        await asyncio.wait_for(first_two_converts.wait(), timeout=1)
        assert convert_calls == 2
        assert max_active <= 2

        convert_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert probe_calls == 3
        assert convert_calls == 3
        assert max_active == 2
    finally:
        probe_release.set()
        convert_release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for upload in uploads:
            await upload.close()


@pytest.mark.asyncio
async def test_audio_cancellation_releases_slot_and_temp_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import audio

    probe = audio.AudioProbe(frozenset({"webm"}), 1.0, 1, 0, 48000, 2, "opus")
    converted = audio.AudioProbe(frozenset({"wav"}), 1.0, 1, 0, 16000, 1, "pcm_s16le")
    started = asyncio.Event()

    async def fake_probe(*_args: object, **_kwargs: object) -> audio.AudioProbe:
        return probe

    async def fake_convert(*_args: object, **_kwargs: object) -> audio.AudioProbe:
        return converted

    async def hotwords() -> dict[str, int]:
        return {"VIP": 5}

    class BlockingProvider:
        async def transcribe(self, _path: Path, _hotwords: dict[str, int]):
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(audio, "_probe_audio", fake_probe)
    monkeypatch.setattr(audio, "_convert_to_wav", fake_convert)
    monkeypatch.setattr(audio, "_catalog_hotwords", hotwords)
    monkeypatch.setattr(audio, "speech_transcriber_factory", lambda **_kwargs: BlockingProvider())
    upload = UploadFile(BytesIO(b"recording"), filename="recording.webm", headers=None)
    upload.headers = httpx.Headers({"content-type": "audio/webm"})  # type: ignore[attr-defined]

    task = asyncio.create_task(
        audio.transcribe_upload(
            upload,
            request_id=str(uuid4()),
            settings=Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path)),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert audio._AUDIO_PROCESSING_SLOT._value == audio.AUDIO_PROCESSING_SLOTS
    assert upload.file.closed
    assert list((tmp_path / "transcriptions").rglob("*")) == []


@pytest.mark.asyncio
async def test_successful_transcription_cleanup_failure_is_safe_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    from app.services import audio

    original_probe = {
        "format": {"format_name": "webm", "duration": "1"},
        "streams": [{"codec_type": "audio", "codec_name": "opus", "duration": "1"}],
    }
    converted_probe = {
        "format": {"format_name": "wav", "duration": "1"},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "duration": "1",
                "sample_rate": "16000",
                "channels": 1,
            }
        ],
    }

    async def run_process(args: list[str], _timeout: float) -> tuple[bytes, bytes]:
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"converted-wav")
            return b"", b""
        payload = converted_probe if Path(args[-1]).name == "audio.wav" else original_probe
        return json.dumps(payload).encode(), b""

    def unlink_failure(_self: Path, missing_ok: bool = False) -> None:
        del missing_ok
        raise OSError("test cleanup failure")

    monkeypatch.setattr(audio, "_run_process", run_process)
    monkeypatch.setattr(audio, "_catalog_hotwords", lambda: asyncio.sleep(0, result={"VIP": 5}))
    monkeypatch.setattr(Path, "unlink", unlink_failure)
    upload = UploadFile(BytesIO(b"recording"), filename="recording.webm", headers=None)
    upload.headers = httpx.Headers({"content-type": "audio/webm"})  # type: ignore[attr-defined]

    with caplog.at_level("ERROR", logger="smokeline"), pytest.raises(AppError) as error:
        await audio.transcribe_upload(
            upload,
            request_id=str(uuid4()),
            settings=Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path)),
        )

    assert (error.value.status_code, error.value.code) == (500, "INTERNAL_ERROR")
    assert any(
        getattr(record, "event", None) == "AUDIO_TEMP_CLEANUP_FAILED" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_primary_audio_error_is_not_masked_by_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.services import audio

    def unlink_failure(_self: Path, missing_ok: bool = False) -> None:
        del missing_ok
        raise OSError("test cleanup failure")

    monkeypatch.setattr(Path, "unlink", unlink_failure)
    upload = UploadFile(
        BytesIO(b"x" * (audio.MAX_AUDIO_BYTES + 1)), filename="too-large.webm", headers=None
    )
    upload.headers = httpx.Headers({"content-type": "audio/webm"})  # type: ignore[attr-defined]

    with pytest.raises(AppError) as error:
        await audio.transcribe_upload(
            upload,
            request_id=str(uuid4()),
            settings=Settings(app_env="test", ai_provider="fake", temp_root=str(tmp_path)),
        )

    assert (error.value.status_code, error.value.code) == (413, "AUDIO_TOO_LARGE")
