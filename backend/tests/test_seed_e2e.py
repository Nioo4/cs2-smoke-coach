"""Contract tests for the explicit, fixed E2E seed operation."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.core.config import Settings
from app.db.models import Lineup, Origin, OriginAlias, Target, TargetAlias, TutorialVideo
from app.db.session import get_session_factory
from app.scripts import seed_e2e


def _settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline",
        postgres_password="smokeline",
        media_store_root="/data/media-store",
        ai_provider="fake",
    )


def _environment(**overrides: str) -> dict[str, str]:
    environment = {
        "APP_ENV": "test",
        "E2E_SEED": "1",
        "AI_PROVIDER": "fake",
        "MEDIA_STORE_ROOT": "/data/media-store",
        "DATABASE_URL": "postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline",
    }
    environment.update(overrides)
    return environment


@pytest.mark.parametrize(
    ("settings", "environment"),
    [
        (_settings().model_copy(update={"app_env": "development"}), _environment()),
        (_settings(), _environment(E2E_SEED="0")),
        (_settings().model_copy(update={"ai_provider": "disabled"}), _environment()),
        (_settings(), _environment(DATABASE_URL="not a database URL")),
        (_settings(), _environment(MEDIA_STORE_ROOT="/tmp/media-store")),
    ],
)
def test_seed_guards_reject_before_any_database_or_file_work(
    settings: Settings, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_work() -> None:
        raise AssertionError("guard returned after work started")

    monkeypatch.setattr(seed_e2e, "_snapshot", unexpected_work)
    with pytest.raises(seed_e2e.SeedGuardError):
        seed_e2e.validate_seed_environment(settings, environment)


def test_fixed_catalog_and_routes_match_spec() -> None:
    targets = seed_e2e._target_specs()
    origins = seed_e2e._origin_specs()
    specs = seed_e2e._lineup_specs()
    assert set(targets) == {"vip", "ct-spawn", "bench"}
    assert targets["vip"]["aliases"] == ["窗口", "中路窗口"]
    assert "中路看人的窗口" not in targets["vip"]["aliases"]
    assert origins["t-spawn"]["aliases"] == ["匪家"]
    assert origins["t-spawn"]["sort_order"] == 10
    assert origins["a-ramp"]["sort_order"] == 20
    assert len(specs) == 20
    assert not [spec for spec in specs if spec.target_slug == "bench"]
    assert [(spec.origin_slug, spec.target_slug) for spec in specs[:4]] == [
        ("t-spawn", "vip"),
        ("t-spawn", "vip"),
        ("a-ramp", "vip"),
        ("t-spawn", "ct-spawn"),
    ]
    t_vip = [spec for spec in specs if (spec.origin_slug, spec.target_slug) == ("t-spawn", "vip")]
    assert len(t_vip) == 2 and [spec.is_default for spec in t_vip] == [True, False]
    assert "测试路线一" in t_vip[0].setup and "原地跳投" in t_vip[0].throw
    assert "测试路线二" in t_vip[1].aim and "蹲着跳投" in t_vip[1].throw
    groups: dict[tuple[str, str], int] = {}
    for spec in specs:
        group = (spec.origin_slug, spec.target_slug)
        groups[group] = groups.get(group, 0) + int(spec.is_default)
        assert all(
            seed_e2e.FIXTURE_NOTE in value
            for value in (spec.setup, spec.aim, spec.throw, spec.result)
        )
    assert all(count == 1 for count in groups.values())


def test_alias_comparison_accepts_explicit_empty_owner_sets() -> None:
    owner_without_aliases = seed_e2e._target_id("ct-spawn")
    assert seed_e2e._assert_aliases((), {owner_without_aliases: []})


def test_media_commands_are_shell_free_and_wav_contract_is_real() -> None:
    output = Path("/tmp/test;not-shell.mp4")
    video = seed_e2e.video_command("0xff0000", output)
    poster = seed_e2e.poster_command(output, Path("/tmp/poster.webp"))
    wav = seed_e2e.wav_command(Path("/tmp/recording.wav"))
    assert video[0] == poster[0] == wav[0] == "ffmpeg"
    assert "shell=True" not in video + poster + wav
    assert "-pix_fmt" in video and "yuv420p" in video
    assert "-r" in video and "30" in video
    assert "-c:v" in video and "libx264" in video
    assert "pcm_s16le" in wav and "16000" in wav and "-ac" in wav and "1" in wav
    with pytest.raises(seed_e2e.SeedError):
        seed_e2e._safe_child(Path("/tmp/media-store"), "../outside.mp4")
    with pytest.raises(seed_e2e.SeedError):
        seed_e2e._safe_child(Path("/tmp/media-store"), "nested\\outside.mp4")
    seed_e2e._assert_wav_probe(
        {
            "format": {"format_name": "wav", "duration": "0.5"},
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "pcm_s16le",
                    "sample_rate": "16000",
                    "channels": 1,
                }
            ],
        }
    )
    with pytest.raises(seed_e2e.SeedError):
        seed_e2e._assert_wav_probe({"format": {"format_name": "mp3"}, "streams": []})


def test_generated_media_probe_contracts_fail_closed() -> None:
    valid_video = {
        "format": {"duration": "1.0"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
                "width": 1920,
                "height": 1080,
                "duration": "1.0",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
    }
    assert seed_e2e._assert_video_probe(valid_video) == 1000
    with pytest.raises(seed_e2e.SeedError, match="incomplete"):
        seed_e2e._assert_video_probe({"format": {}, "streams": "not-a-list"})
    with pytest.raises(seed_e2e.SeedError, match="exactly one"):
        seed_e2e._assert_video_probe({"format": {}, "streams": []})
    malformed_video = {
        **valid_video,
        "streams": [{"codec_type": "video"}, valid_video["streams"][1]],
    }
    with pytest.raises(seed_e2e.SeedError, match="metadata is invalid"):
        seed_e2e._assert_video_probe(malformed_video)
    wrong_size = {
        **valid_video,
        "streams": [{**valid_video["streams"][0], "width": 1280}, valid_video["streams"][1]],
    }
    with pytest.raises(seed_e2e.SeedError, match="30fps contract"):
        seed_e2e._assert_video_probe(wrong_size)

    valid_poster = {
        "format": {"format_name": "webp_pipe"},
        "streams": [{"codec_type": "video", "codec_name": "webp", "width": 1280, "height": 720}],
    }
    seed_e2e._assert_poster_probe(valid_poster)
    with pytest.raises(seed_e2e.SeedError, match="incomplete"):
        seed_e2e._assert_poster_probe({"format": {}, "streams": None})
    with pytest.raises(seed_e2e.SeedError, match="not a WebP"):
        seed_e2e._assert_poster_probe({"format": {"format_name": "png"}, "streams": []})
    wrong_poster = {
        **valid_poster,
        "streams": [{**valid_poster["streams"][0], "width": 640}],
    }
    with pytest.raises(seed_e2e.SeedError, match="dimensions"):
        seed_e2e._assert_poster_probe(wrong_poster)

    invalid_wav = {
        "format": {"format_name": "wav", "duration": "0.5"},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "sample_rate": "16000",
                "channels": 2,
            }
        ],
    }
    with pytest.raises(seed_e2e.SeedError, match="short recording contract"):
        seed_e2e._assert_wav_probe(invalid_wav)


def test_media_tool_and_probe_errors_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError("missing")

    monkeypatch.setattr(seed_e2e.subprocess, "run", unavailable)
    with pytest.raises(seed_e2e.SeedError, match="command unavailable"):
        seed_e2e._run_tool(["ffmpeg"])

    monkeypatch.setattr(
        seed_e2e.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["ffmpeg"], 1, b"", b"failed"),
    )
    with pytest.raises(seed_e2e.SeedError, match="command failed"):
        seed_e2e._run_tool(["ffmpeg"])

    monkeypatch.setattr(seed_e2e, "_run_tool", lambda _arguments: b"not-json")
    with pytest.raises(seed_e2e.SeedError, match="invalid JSON"):
        seed_e2e._probe(Path("probe.mp4"))
    monkeypatch.setattr(seed_e2e, "_run_tool", lambda _arguments: b"[]")
    with pytest.raises(seed_e2e.SeedError, match="invalid payload"):
        seed_e2e._probe(Path("probe.mp4"))


def test_media_root_preparation_and_nonempty_guard(tmp_path: Path) -> None:
    seed_e2e._prepare_media_root(tmp_path)
    seed_e2e._assert_media_areas_empty(tmp_path)
    (tmp_path / "public" / "occupied").write_text("fixture", encoding="utf-8")
    with pytest.raises(seed_e2e.SeedError, match="existing media"):
        seed_e2e._assert_media_areas_empty(tmp_path)

    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("fixture", encoding="utf-8")
    with pytest.raises(seed_e2e.SeedError, match="not a directory"):
        seed_e2e._assert_real_directory(regular_file)


def test_move_rollback_restores_sources_without_touching_unmoved_entries(tmp_path: Path) -> None:
    untouched_source = tmp_path / "untouched" / "source.mp4"
    source_already_present = tmp_path / "present" / "source.mp4"
    source_already_present.parent.mkdir()
    source_already_present.write_bytes(b"original")
    conflicting_destination = tmp_path / "published-conflict.mp4"
    conflicting_destination.write_bytes(b"published")
    restored_source = tmp_path / "restored" / "source.mp4"
    published_destination = tmp_path / "published.mp4"
    published_destination.write_bytes(b"published")

    seed_e2e._rollback_moves(
        [
            (untouched_source, tmp_path / "never-published.mp4"),
            (source_already_present, conflicting_destination),
            (restored_source, published_destination),
        ]
    )

    assert not untouched_source.exists()
    assert source_already_present.read_bytes() == b"original"
    assert not conflicting_destination.exists()
    assert restored_source.read_bytes() == b"published"
    assert not published_destination.exists()


def test_atomic_media_move_publishes_and_rejects_an_occupied_destination(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    staged_wav = staging / "recording.wav"
    staged_wav.write_bytes(b"wav")
    (tmp_path / "e2e").mkdir()

    journal = seed_e2e._move_files(tmp_path, [], staging)
    published_wav = tmp_path / seed_e2e.WAV_RELATIVE_PATH
    assert published_wav.read_bytes() == b"wav"
    assert not staged_wav.exists()

    seed_e2e._rollback_moves(journal)
    assert staged_wav.read_bytes() == b"wav"
    assert not published_wav.exists()

    published_wav.write_bytes(b"occupied")
    with pytest.raises(seed_e2e.SeedError, match="occupied or unsafe"):
        seed_e2e._move_files(tmp_path, [], staging)
    assert staged_wav.read_bytes() == b"wav"
    assert published_wav.read_bytes() == b"occupied"


def test_seed_cli_main_reports_success_and_bounded_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = object()
    monkeypatch.setattr(seed_e2e, "get_settings", lambda: sentinel)
    monkeypatch.setattr(seed_e2e, "seed", lambda settings: ("seed", settings))
    monkeypatch.setattr(
        seed_e2e.asyncio,
        "run",
        lambda operation: (
            "E2E_SEED_EXISTING lineups=20 videos=20 wav=1"
            if operation == ("seed", sentinel)
            else "unexpected"
        ),
    )
    assert seed_e2e.main() == 0
    assert capsys.readouterr().out.strip() == "E2E_SEED_EXISTING lineups=20 videos=20 wav=1"

    monkeypatch.setattr(
        seed_e2e,
        "get_settings",
        lambda: (_ for _ in ()).throw(seed_e2e.SeedGuardError("guarded")),
    )
    assert seed_e2e.main() == 1
    assert "E2E_SEED_REJECTED: guarded" in capsys.readouterr().err

    monkeypatch.setattr(
        seed_e2e,
        "get_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("private detail")),
    )
    assert seed_e2e.main() == 1
    error = capsys.readouterr().err
    assert error.strip() == "E2E_SEED_REJECTED: seed failed"
    assert "private detail" not in error


@pytest.mark.asyncio
async def test_partial_rows_are_rejected_without_media_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    partial = seed_e2e.FixtureSnapshot(
        targets=(Target(id=seed_e2e._target_id("vip")),),
        target_aliases=(),
        origins=(),
        origin_aliases=(),
        lineups=(),
        videos=(),
    )
    monkeypatch.setattr(seed_e2e, "_prepare_media_root", lambda _root: None)
    monkeypatch.setattr(seed_e2e, "_snapshot", lambda: _async_value(partial))
    monkeypatch.setattr(
        seed_e2e, "_generate_media", lambda *_args: (_ for _ in ()).throw(AssertionError())
    )
    with pytest.raises(seed_e2e.SeedError, match="partial"):
        await seed_e2e.seed(settings, _environment())


@pytest.mark.asyncio
async def test_complete_rows_report_idempotently_without_regenerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = object()
    monkeypatch.setattr(seed_e2e, "_prepare_media_root", lambda _root: None)
    monkeypatch.setattr(seed_e2e, "_snapshot", lambda: _async_value(snapshot))
    monkeypatch.setattr(seed_e2e, "_matches_fixture_rows", lambda _snapshot: True)
    monkeypatch.setattr(seed_e2e, "_verify_complete_media", lambda _root, _snapshot: True)
    monkeypatch.setattr(
        seed_e2e, "_generate_media", lambda *_args: (_ for _ in ()).throw(AssertionError())
    )
    assert (
        await seed_e2e.seed(_settings(), _environment())
        == "E2E_SEED_EXISTING lineups=20 videos=20 wav=1"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_media_seed_and_idempotence_are_isolated() -> None:
    if os.getenv("PHASE2_INTEGRATION_REQUIRED") != "1":
        pytest.skip("requires the explicitly provisioned PostgreSQL and media test volumes")

    settings = _settings()
    root = Path(settings.media_store_root)
    target_ids = [item["id"] for item in seed_e2e._target_specs().values()]
    origin_ids = [item["id"] for item in seed_e2e._origin_specs().values()]
    lineup_ids = [seed_e2e._lineup_id(index) for index in range(20)]

    async def cleanup_fixture() -> None:
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(
                    delete(TutorialVideo).where(TutorialVideo.lineup_id.in_(lineup_ids))
                )
                await session.execute(delete(Lineup).where(Lineup.id.in_(lineup_ids)))
                await session.execute(
                    delete(TargetAlias).where(TargetAlias.target_id.in_(target_ids))
                )
                await session.execute(
                    delete(OriginAlias).where(OriginAlias.origin_id.in_(origin_ids))
                )
                await session.execute(delete(Target).where(Target.id.in_(target_ids)))
                await session.execute(delete(Origin).where(Origin.id.in_(origin_ids)))
        for lineup_id in lineup_ids:
            directory = seed_e2e._safe_child(root, f"public/{lineup_id}")
            if directory.is_symlink():
                raise AssertionError("fixture cleanup refused a symlinked lineup directory")
            if directory.exists():
                shutil.rmtree(directory)
        wav = seed_e2e._safe_child(root, seed_e2e.WAV_RELATIVE_PATH)
        if wav.is_symlink():
            raise AssertionError("fixture cleanup refused a symlinked WAV")
        wav.unlink(missing_ok=True)

    assert seed_e2e._snapshot_empty(await seed_e2e._snapshot())
    try:
        assert (
            await seed_e2e.seed(settings, _environment())
            == "E2E_SEED_CREATED lineups=20 videos=20 wav=1"
        )
        assert (
            await seed_e2e.seed(settings, _environment())
            == "E2E_SEED_EXISTING lineups=20 videos=20 wav=1"
        )
        snapshot = await seed_e2e._snapshot()
        assert seed_e2e._matches_fixture_rows(snapshot)
        assert seed_e2e._verify_complete_media(root, snapshot)
    finally:
        await cleanup_fixture()

    assert seed_e2e._snapshot_empty(await seed_e2e._snapshot())
    assert not [path for path in (root / "public").iterdir()]
    assert not seed_e2e._safe_child(root, seed_e2e.WAV_RELATIVE_PATH).exists()


async def _async_value(value: object) -> object:
    return value
