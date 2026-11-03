"""Account security against an explicitly provisioned, isolated real PostgreSQL database."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import uuid

import httpx
import psycopg
import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.api import features
from app.api import tactics_auth as api
from app.core import config
from app.core.errors import install_error_handlers
from app.db.tactics_models import PersonalNotebook, TacticsSession, TacticsUser
from app.services import tactics_accounts as accounts

pytestmark = pytest.mark.integration
ORIGIN = "http://accounts.test"
PASSWORD = "account-test-password-2026"


@pytest.fixture(scope="module")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="module")
def database_url():
    raw = os.environ.get("TACTICS_ACCOUNTS_TEST_DATABASE_URL", "")
    if not raw:
        pytest.fail("TACTICS_ACCOUNTS_TEST_DATABASE_URL must name the isolated account test DB")
    parsed = make_url(raw)
    assert parsed.host == "127.0.0.1" and parsed.database in {
        "tactics_accounts_selftest", "tactics_notebooks_selftest", "tactics_formal_selftest"
    }
    assert parsed.port != 55433, "existing prototype database must never be used"
    settings = config.Settings(_env_file=None, app_env="test", database_url=raw)
    original = config.get_settings
    config.get_settings = lambda: settings
    try:
        target = {
            "tactics_accounts_selftest": "0006_tactics_accounts",
            "tactics_notebooks_selftest": "0007_tactics_documents",
        }.get(parsed.database, "head")
        command.upgrade(Config("alembic.ini"), target)
    finally:
        config.get_settings = original
    return raw


@pytest.fixture
async def app_client(database_url, monkeypatch):
    engine = create_async_engine(database_url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = config.Settings(
        _env_file=None, app_env="test", database_url=database_url,
        tactics_enabled=True, public_base_url=ORIGIN,
    )
    monkeypatch.setattr(api, "get_session_factory", lambda: factory)
    monkeypatch.setattr(accounts, "get_session_factory", lambda: factory)
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(features, "get_settings", lambda: settings)
    accounts._RATE_WINDOWS.clear()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(api.router)
    app.include_router(features.router)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        yield client, factory, settings, transport
    await engine.dispose()


async def register(client, username=None, password=PASSWORD):
    username = username or "test_" + uuid.uuid4().hex[:16]
    result = await client.post("/api/v1/auth/register", headers={"origin": ORIGIN}, json={
        "username": username, "display_name": "测试用户", "password": password,
    })
    assert result.status_code == 201, result.text
    cookie_header = result.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=lax" in cookie_header
    assert "path=/" in cookie_header and "domain=" not in cookie_header
    return result.json(), username


def write_headers(data):
    return {"origin": ORIGIN, "x-csrf-token": data["csrf_token"]}


async def test_register_personal_notebook_csrf_and_account_isolation(app_client):
    client, factory, _, transport = app_client
    first, username = await register(client)
    cookie = client.cookies[accounts.COOKIE]
    session = await client.get("/api/v1/auth/session")
    assert session.json()["csrf_token"] == first["csrf_token"]
    notebook = (await client.get("/api/v1/notebooks/personal")).json()["notebook"]
    assert notebook["user_id"] == first["user"]["id"]
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as other:
        second, _ = await register(other)
        other_notebook = (await other.get("/api/v1/notebooks/personal")).json()["notebook"]
        assert other_notebook["id"] != notebook["id"]
        cross = await other.patch("/api/v1/auth/profile", headers=write_headers(first),
                                  json={"display_name": "不得修改"})
        assert cross.status_code == 403
        assert second["user"]["id"] != first["user"]["id"]
    for headers in ({"origin": "https://evil.test"}, {"origin": ORIGIN}, {}):
        denied = await client.patch("/api/v1/auth/profile", headers=headers,
                                   json={"display_name": "不得修改"})
        assert denied.status_code == 403
    updated = await client.patch("/api/v1/auth/profile", headers=write_headers(first),
                                 json={"display_name": "新昵称"})
    assert updated.json()["user"]["display_name"] == "新昵称"
    duplicate = await client.post("/api/v1/auth/register", headers=write_headers(first), json={
        "username": username.upper(), "display_name": "重复", "password": PASSWORD,
    })
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "USERNAME_TAKEN"
    async with factory() as db:
        user = await db.get(TacticsUser, uuid.UUID(first["user"]["id"]))
        stored = await db.scalar(sa.select(TacticsSession).where(
            TacticsSession.session_digest == accounts.digest(cookie)
        ))
        assert user.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=2$")
        assert accounts.HASHER.verify(user.password_hash, PASSWORD)
        assert user.recovery_digest == accounts.digest(first["recovery_code"])
        assert stored.csrf_digest == accounts.digest(first["csrf_token"])
        assert (stored.expires_at - stored.created_at).days == 7
        assert cookie.encode() != stored.session_digest
    assert session.headers["cache-control"] == "no-store"


async def test_password_and_recovery_atomically_revoke_all_sessions(app_client):
    client, _, _, transport = app_client
    first, username = await register(client)
    old_cookie = client.cookies[accounts.COOKIE]
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as other:
        login = await other.post("/api/v1/auth/login", headers={"origin": ORIGIN}, json={
            "username": username, "password": PASSWORD,
        })
        assert login.status_code == 200
        changed = await client.post("/api/v1/auth/password", headers=write_headers(first), json={
            "current_password": PASSWORD, "new_password": "  exact spaced password  ",
        })
        assert changed.status_code == 200
        assert client.cookies[accounts.COOKIE] != old_cookie
        assert (await other.get("/api/v1/auth/session")).status_code == 401
    trimmed = await client.post("/api/v1/auth/login", headers=write_headers(changed.json()), json={
        "username": username, "password": "exact spaced password",
    })
    assert trimmed.status_code == 401
    exact = await client.post("/api/v1/auth/login", headers=write_headers(changed.json()), json={
        "username": username, "password": "  exact spaced password  ",
    })
    assert exact.status_code == 200
    recovered = await client.post(
        "/api/v1/auth/recover", headers=write_headers(exact.json()), json={
        "username": username, "recovery_code": first["recovery_code"],
        "new_password": "recovered-password-2026",
    })
    assert recovered.status_code == 200
    assert (await client.get("/api/v1/auth/session")).status_code == 401
    again = await client.post("/api/v1/auth/recover", headers={"origin": ORIGIN}, json={
        "username": username, "recovery_code": first["recovery_code"], "new_password": PASSWORD,
    })
    assert again.status_code == 401
    assert recovered.json()["recovery_code"] != first["recovery_code"]
    login = await client.post("/api/v1/auth/login", headers={"origin": ORIGIN}, json={
        "username": username.upper(), "password": "recovered-password-2026",
    })
    assert login.status_code == 200
    logout = await client.post("/api/v1/auth/logout", headers=write_headers(login.json()), json={})
    assert logout.json() == {"ok": True}
    assert (await client.get("/api/v1/auth/session")).status_code == 401
    repeated_logout = await client.post(
        "/api/v1/auth/logout", headers={"origin": ORIGIN}, json={}
    )
    assert repeated_logout.json() == {"ok": True}


async def test_recovery_code_rotation_invalidates_old_code(app_client):
    client, _, _, _ = app_client
    data, username = await register(client)
    rotated = await client.post("/api/v1/auth/recovery-code", headers=write_headers(data),
                                json={"current_password": PASSWORD})
    assert rotated.status_code == 200
    for code, expected in [(data["recovery_code"], 401), (rotated.json()["recovery_code"], 200)]:
        result = await client.post("/api/v1/auth/recover", headers=write_headers(data), json={
            "username": username, "recovery_code": code, "new_password": PASSWORD,
        })
        assert result.status_code == expected


async def test_strict_safe_errors_feature_flag_and_uniform_login(app_client):
    client, _, settings, _ = app_client
    for body in ({"username": "valid_user", "password": PASSWORD, "extra_secret": PASSWORD},
                 {"username": 123, "password": PASSWORD}):
        result = await client.post("/api/v1/auth/login", headers={"origin": ORIGIN}, json=body)
        assert result.status_code == 422
        assert PASSWORD not in result.text
        assert result.headers["cache-control"] == "no-store"
    query = await client.get("/api/v1/notebooks/personal?user_id=someone-else")
    assert query.status_code == 422
    assert (await client.get("/api/v1/notebooks/personal")).status_code == 401
    huge = await client.post("/api/v1/auth/login", headers={"origin": ORIGIN}, content=b"x" * 9000)
    assert huge.status_code == 413
    for _ in range(5):
        result = await client.post("/api/v1/auth/login", headers={"origin": ORIGIN}, json={
            "username": "unknown_user", "password": PASSWORD,
        })
        assert result.status_code == 401 and result.json()["error"]["code"] == "USER_AUTH_INVALID"
    limited = await client.post("/api/v1/auth/login", headers={"origin": ORIGIN}, json={
        "username": "unknown_user", "password": PASSWORD,
    })
    assert limited.status_code == 429
    assert len(accounts._RATE_WINDOWS) <= 2 and all(
        isinstance(key, bytes) and len(key) == 32 for key in accounts._RATE_WINDOWS
    )
    settings.tactics_enabled = False
    assert (await client.get("/api/v1/auth/session")).status_code == 404
    capability = await client.get("/api/v1/features")
    assert capability.json() == {"tactics_enabled": False}
    assert capability.headers["cache-control"] == "no-store"


async def test_concurrent_registration_creates_one_user_and_notebook(app_client):
    client, factory, _, transport = app_client
    username = "race_" + uuid.uuid4().hex[:16]
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as other:
        async def attempt(active):
            return await active.post("/api/v1/auth/register", headers={"origin": ORIGIN}, json={
                "username": username, "display_name": "并发", "password": PASSWORD,
            })
        results = await asyncio.gather(attempt(client), attempt(other))
    assert sorted(result.status_code for result in results) == [201, 409]
    async with factory() as db:
        user = await db.scalar(sa.select(TacticsUser).where(
            TacticsUser.username_normalized == username
        ))
        assert await db.scalar(sa.select(sa.func.count()).select_from(PersonalNotebook).where(
            PersonalNotebook.user_id == user.id
        )) == 1


async def test_expired_session_is_not_authenticated(app_client):
    client, factory, _, _ = app_client
    data, _ = await register(client)
    async with factory() as db, db.begin():
        session = await db.scalar(sa.select(TacticsSession).where(
            TacticsSession.session_digest == accounts.digest(client.cookies[accounts.COOKIE])
        ))
        session.created_at -= accounts.timedelta(days=9)
        session.expires_at -= accounts.timedelta(days=8)
    assert (await client.get("/api/v1/auth/session")).status_code == 401
    assert "recovery_code" not in (await client.get("/api/v1/notebooks/personal")).text
    assert data["recovery_code"]


async def test_real_database_failure_rolls_back_registration(app_client):
    client, factory, _, _ = app_client
    username = "fail_" + uuid.uuid4().hex[:16]
    async with factory() as db, db.begin():
        await db.execute(sa.text("""CREATE OR REPLACE FUNCTION account_test_reject_notebook()
          RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test failure'; END $$"""))
        await db.execute(sa.text("""CREATE TRIGGER account_test_notebook_failure
          BEFORE INSERT ON notebooks FOR EACH ROW
          EXECUTE FUNCTION account_test_reject_notebook()"""))
    try:
        result = await client.post("/api/v1/auth/register", headers={"origin": ORIGIN}, json={
            "username": username, "display_name": "失败回滚", "password": PASSWORD,
        })
        assert result.status_code == 500
        assert PASSWORD not in result.text and "INSERT" not in result.text
        async with factory() as db:
            assert await db.scalar(sa.select(TacticsUser).where(
                TacticsUser.username_normalized == username
            )) is None
    finally:
        async with factory() as db, db.begin():
            await db.execute(sa.text("DROP TRIGGER account_test_notebook_failure ON notebooks"))
            await db.execute(sa.text("DROP FUNCTION account_test_reject_notebook()"))


def test_migration_and_database_constraints(database_url):
    with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://")) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            {"tactics_accounts_selftest": "0006_tactics_accounts",
             "tactics_notebooks_selftest": "0007_tactics_documents"}.get(
                make_url(database_url).database,
                ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
            )
        )
        assert db.execute("SELECT count(*) FROM information_schema.tables "
                          "WHERE table_name = ANY(%s)",
                          (["users", "user_sessions", "notebooks"],)).fetchone()[0] == 3
        for username, normalized, recovery in [
            ("bad-name", "bad-name", bytes(32)),
            ("ValidName", "WRONG", bytes(32)),
            ("ValidName", "validname", b"too-short"),
        ]:
            db.execute("SAVEPOINT invalid_account")
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute("""INSERT INTO users (id, username, username_normalized, display_name,
                  password_hash, recovery_digest, credential_version, created_at, updated_at)
                  VALUES (%s,%s,%s,'test','not-a-password',%s,1,now(),now())""",
                           (uuid.uuid4(), username, normalized, recovery))
            db.execute("ROLLBACK TO SAVEPOINT invalid_account")
        assert db.execute("SELECT count(*) FROM notebooks n LEFT JOIN users u ON n.user_id=u.id "
                          "WHERE u.id IS NULL").fetchone()[0] == 0


async def test_cookie_flags_and_logged_in_auth_writes_require_csrf(app_client):
    client, _, settings, _ = app_client
    data, username = await register(client)
    for path, payload in [
        ("login", {"username": username, "password": PASSWORD}),
        ("recover", {"username": username, "recovery_code": data["recovery_code"],
                     "new_password": PASSWORD}),
    ]:
        result = await client.post("/api/v1/auth/" + path, headers={"origin": ORIGIN}, json=payload)
        assert result.status_code == 403 and result.json()["error"]["code"] == "CSRF_INVALID"
    settings.app_env = "production"
    cookie = api.response({}, token="public-test-cookie").headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "domain=" not in cookie


def test_tactics_config_default_off_and_safe_validation():
    assert config.Settings(_env_file=None).tactics_enabled is False
    with pytest.raises(config.ConfigError) as caught:
        config.Settings(_env_file=None, collab_internal_key="test-short-secret").validate_runtime()
    assert "test-short-secret" not in str(caught.value)
    assert "COLLAB_INTERNAL_KEY" in caught.value.fields


async def test_cancelled_password_worker_keeps_slot_until_thread_settles():
    started = threading.Event()
    release = threading.Event()

    def worker():
        started.set()
        release.wait(2)
        return True

    operation = asyncio.create_task(accounts.password_work(worker))
    while not started.is_set():
        await asyncio.sleep(0.01)
    operation.cancel()
    await asyncio.sleep(0.02)
    assert not operation.done() and accounts.AUTH_SLOTS._value == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert accounts.AUTH_SLOTS._value == 2


def test_harness_passes_explicit_selector_factory_to_installed_uvicorn(monkeypatch):
    from app.scripts import run_tactics_accounts_selftest as harness

    captured = {}
    monkeypatch.setattr(harness, "create_app", lambda: None)
    monkeypatch.setattr(harness.uvicorn, "run", lambda _app, **kwargs: captured.update(kwargs))
    monkeypatch.setenv("TACTICS_ACCOUNTS_TEST_PORT", "4184")
    harness.main()
    factory = harness.uvicorn.Config(app=None, loop=captured["loop"]).get_loop_factory()
    loop = factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
        assert captured["host"] == "127.0.0.1"
    finally:
        loop.close()
