"""Prepared TCP/PG runner, restricted to the new undo database."""

import asyncio
import os
import shutil
import subprocess

import pytest
import test_tactics_collaboration as tcp
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core import config

event_loop_policy, live = tcp.event_loop_policy, tcp.live
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_UNDO_TEST_DATABASE_URL", "")
    if not raw:
        pytest.skip("Explicit new tactics_undo_selftest database not arranged")
    target = make_url(raw)
    assert target.host == "127.0.0.1" and target.port == 65402
    assert target.username == "tactics_test" and target.database == "tactics_undo_selftest"
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    previous = config.get_settings
    config.get_settings = lambda: settings
    try:
        command.upgrade(Config("alembic.ini"), "0010_tactics_shares_history")
    finally:
        config.get_settings = previous
    return raw


async def test_real_object_undo_persistence_recovery_fault_and_duplicate(live, database_url):
    backend, seed, _, _ = live
    process = subprocess.Popen(
        [shutil.which("node"), "scripts/formal-object-undo-smoke.mjs"], cwd=tcp.ROOT,
        env={**os.environ, "FORMAL_TEST_BACKEND": backend,
             "FORMAL_TEST_WS": seed.replace("http://", "ws://") + "/ws/tactics",
             "FORMAL_TEST_ORIGIN": tcp.ORIGIN,
             "UNDO_TEST_DATABASE_URL": database_url.replace(
                 "postgresql+psycopg://", "postgresql://")},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=45)
        assert process.returncode == 0, stderr.decode()
        assert b"OBJECT_UNDO_REAL_HTTP_WS_PG_PASS" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, 5)
