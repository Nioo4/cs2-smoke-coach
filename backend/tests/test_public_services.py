from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.db.models import LineupStatus, VideoStatus
from app.services import local_search
from app.services.local_search import PublicRow


def _entities():
    target = SimpleNamespace(
        id=uuid.uuid4(),
        slug="vip",
        name_zh="VIP",
        name_en="Window",
        sort_order=20,
        is_active=True,
        map_x=50,
        map_y=25,
    )
    origin = SimpleNamespace(
        id=uuid.uuid4(),
        slug="t-spawn",
        name_zh="T 出生点",
        name_en=None,
        sort_order=10,
        is_active=True,
        map_x=20,
        map_y=80,
    )
    return target, origin


def _row(
    *,
    default: bool = True,
    sort_order: int = 0,
    setup_text: str = "站到箱子旁",
    aim_text: str = "瞄准墙角",
    throw_text: str = "按住左键跳投",
    result_text: str = "烟雾封住窗口",
) -> PublicRow:
    target, origin = _entities()
    lineup = SimpleNamespace(
        id=uuid.uuid4(),
        target_id=target.id,
        origin_id=origin.id,
        status=LineupStatus.published,
        is_default=default,
        sort_order=sort_order,
        setup_text=setup_text,
        aim_text=aim_text,
        throw_text=throw_text,
        result_text=result_text,
        last_checked_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        game_version_note="2.1",
    )
    video = SimpleNamespace(
        id=uuid.uuid4(),
        status=VideoStatus.ready,
        storage_key=uuid.uuid4(),
        relative_path=f"{lineup.id}/primary-{uuid.uuid4()}.mp4",
        poster_relative_path=f"{lineup.id}/primary-{uuid.uuid4()}.webp",
        duration_ms=2_000,
        checksum_sha256="a" * 64,
    )
    return PublicRow(lineup, target, origin, video)


def test_public_helpers_and_group_selection() -> None:
    row = _row(sort_order=20)
    assert local_search.media_url(row).startswith("/media/")
    assert local_search.media_url(row, poster=True).endswith(".webp?v=aaaaaaaaaaaa")
    assert local_search.point_summary(row.target) == {
        "slug": "vip",
        "name_zh": "VIP",
        "map_x": 50.0,
        "map_y": 25.0,
    }
    assert local_search.primary_video(row)["duration_ms"] == 2_000
    summary = local_search.lineup_summary(row)
    assert summary["id"] == str(row.lineup.id)
    assert summary["variant_hint"].startswith("站位：")
    assert local_search.group_rows([row]) == {(row.target.id, row.origin.id): [row]}
    assert local_search.choose_group([row]) == row

    preferred = _row(default=False, sort_order=2)
    # The default is the only selection signal; descriptive throw text is not.
    assert local_search.choose_group([row, preferred]) == row

    with pytest.raises(LookupError):
        local_search.choose_group([])
    broken = _row(default=False)
    with pytest.raises(AppError) as error:
        local_search.choose_group([broken])
    assert error.value.code == "CONTENT_CONFIGURATION_ERROR"


def test_point_summary_preserves_an_unlocated_point_pair() -> None:
    target, _origin = _entities()
    target.map_x = None
    target.map_y = None
    assert local_search.point_summary(target) == {
        "slug": "vip",
        "name_zh": "VIP",
        "map_x": None,
        "map_y": None,
    }


def test_detail_and_other_lineup_sorting() -> None:
    current = _row(default=True, sort_order=30)
    other = _row(default=False, sort_order=10)
    detail = local_search.detail_from_row(current, [current, other])
    assert detail["source"] == "local_curated"
    assert detail["check_info"]["last_checked_at"].endswith("Z")
    assert detail["other_lineups"][0]["id"] == str(other.lineup.id)
    assert detail["steps"]["result"] == "烟雾封住窗口"


def test_variant_hint_is_unicode_whitespace_safe_and_literal() -> None:
    exact = "字" * 60
    over = "字" * 61
    row = _row(
        setup_text="  站位\t\u3000<em>不要解析</em>\n\r  ",
        aim_text="瞄点 😀",
    )
    assert local_search._hint_fragment(exact) == exact
    assert local_search._hint_fragment(over) == f"{exact}…"
    assert local_search._hint_fragment("😀" * 61) == f"{'😀' * 60}…"
    assert local_search.variant_hint(row.lineup) == ("站位：站位 <em>不要解析</em>；瞄点：瞄点 😀")
    assert len(local_search.variant_hint(_row(setup_text=exact, aim_text=exact).lineup)) == 127
    assert len(local_search.variant_hint(_row(setup_text=over, aim_text=over).lineup)) == 129


def test_other_lineups_sort_by_default_sort_order_and_id_without_merging_hints() -> None:
    current = _row(default=False, sort_order=50)
    default_other = _row(default=True, sort_order=90, setup_text="相同站位", aim_text="相同瞄点")
    same_score_late = _row(default=False, sort_order=10, setup_text="相同站位", aim_text="相同瞄点")
    same_score_early = _row(
        default=False, sort_order=10, setup_text="相同站位", aim_text="相同瞄点"
    )
    current.lineup.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    default_other.lineup.id = uuid.UUID("00000000-0000-0000-0000-000000000004")
    same_score_late.lineup.id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    same_score_early.lineup.id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    rows = [current, same_score_late, default_other, same_score_early]

    detail = local_search.detail_from_row(current, rows)
    ordered = detail["other_lineups"]
    assert [item["id"] for item in ordered] == [
        str(default_other.lineup.id),
        str(same_score_early.lineup.id),
        str(same_score_late.lineup.id),
    ]
    assert len(ordered) == 3
    assert (
        ordered[1]["variant_hint"] == local_search.lineup_summary(same_score_early)["variant_hint"]
    )
    assert ordered[1]["variant_hint"] == ordered[2]["variant_hint"]


class _Result:
    def __init__(self, values=(), rows=()):
        self._values = list(values)
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows if self._rows else self._values

    def __iter__(self):
        return iter(self._rows)


class _CatalogSession:
    def __init__(self, targets, origins, target_aliases, origin_aliases):
        self.targets = targets
        self.origins = origins
        self.target_aliases = target_aliases
        self.origin_aliases = origin_aliases

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity.__name__ == "Target":
            return _Result(self.targets)
        if entity.__name__ == "Origin":
            return _Result(self.origins)
        if entity.__name__ == "TargetAlias":
            return _Result(self.target_aliases)
        if entity.__name__ == "OriginAlias":
            return _Result(self.origin_aliases)
        raise AssertionError(f"unexpected select: {entity}")


@pytest.mark.asyncio
async def test_active_catalog_includes_names_aliases_and_sort_order() -> None:
    target, origin = _entities()
    target_alias = SimpleNamespace(target_id=target.id, alias="中路窗口")
    origin_alias = SimpleNamespace(origin_id=origin.id, alias="匪家")
    result = await local_search.active_catalog(
        _CatalogSession([target], [origin], [target_alias], [origin_alias])
    )
    targets, origins = result
    assert targets[0].sort_order == 20
    assert {term.normalized for term in targets[0].terms} == {"vip", "window", "中路窗口"}
    assert {term.normalized for term in origins[0].terms} == {"t 出生点", "匪家"}


@pytest.mark.asyncio
async def test_available_rows_applies_optional_filters() -> None:
    row = _row()

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return _Result(rows=[(row.lineup, row.target, row.origin, row.video)])

    session = Session()
    rows = await local_search.available_rows(
        session, target_id=row.target.id, origin_id=row.origin.id
    )
    assert rows == [row]
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_public_snapshot_maps_database_errors_to_safe_app_error(monkeypatch) -> None:
    class FailingBegin:
        async def __aenter__(self):
            import psycopg
            from sqlalchemy.exc import OperationalError

            raise OperationalError("read", {}, psycopg.OperationalError("test-private-marker"))

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return FailingBegin()

    monkeypatch.setattr(local_search, "get_session_factory", lambda: lambda: Session())
    with pytest.raises(AppError) as error:
        async with local_search.public_snapshot():
            pass
    assert error.value.code == "DATABASE_UNAVAILABLE"
