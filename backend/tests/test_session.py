import pytest

from app.db.session import check_database


class ConnectionContext:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    async def __aenter__(self):
        if self.should_fail:
            raise RuntimeError("database unavailable")
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def execute(self, _statement) -> None:
        return None


class FakeEngine:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    def connect(self) -> ConnectionContext:
        return ConnectionContext(self.should_fail)


@pytest.mark.asyncio
async def test_check_database_reports_success_and_failure() -> None:
    assert await check_database(FakeEngine()) is True
    assert await check_database(FakeEngine(should_fail=True)) is False
