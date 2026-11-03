"""Bounded password work and transactional ordinary-account authentication."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request

from app.core.errors import AppError
from app.db.session import get_session_factory
from app.db.tactics_models import TacticsSession, TacticsUser

COOKIE = "smokeline_session"
HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
AUTH_SLOTS = asyncio.Semaphore(2)
_DUMMY_HASH = HASHER.hash("ordinary-account-dummy-password")
_RATE_SALT = secrets.token_bytes(32)
_RATE_WINDOWS: dict[bytes, deque[float]] = {}


def now() -> datetime:
    return datetime.now(UTC)


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def auth_error() -> AppError:
    return AppError(401, "USER_AUTH_INVALID", "用户名或密码不正确。")


def recovery_error() -> AppError:
    return AppError(401, "RECOVERY_INVALID", "恢复信息无效。")


def rate_limit(request: Request, action: str, username: str) -> None:
    """Single-process, 60-second bounded windows; only salted digests survive the call."""
    current = time.monotonic()
    for key in list(_RATE_WINDOWS):
        window = _RATE_WINDOWS[key]
        while window and window[0] <= current - 60:
            window.popleft()
        if not window:
            del _RATE_WINDOWS[key]
    identities = [f"user:{username.lower()}", f"ip:{request.client.host if request.client else ''}"]
    keys = [hmac.digest(_RATE_SALT, f"{action}:{value}".encode(), "sha256") for value in identities]
    if len(_RATE_WINDOWS) + len(keys) > 10000 or any(
        len(_RATE_WINDOWS.get(key, ())) >= 5 for key in keys
    ):
        raise AppError(429, "USER_AUTH_RATE_LIMITED", "请求过于频繁，请稍后重试。")
    for key in keys:
        _RATE_WINDOWS.setdefault(key, deque()).append(current)


async def password_work(function, *args):
    try:
        await asyncio.wait_for(AUTH_SLOTS.acquire(), timeout=1)
    except TimeoutError:
        raise AppError(429, "USER_AUTH_BUSY", "账户认证繁忙，请稍后重试。") from None
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
    finally:
        AUTH_SLOTS.release()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _verify(encoded_hash: str, password: str) -> bool:
    try:
        return HASHER.verify(encoded_hash, password)
    except (InvalidHashError, VerificationError):
        return False


async def verify_password(encoded_hash: str | None, password: str) -> bool:
    verified = await password_work(_verify, encoded_hash or _DUMMY_HASH, password)
    return bool(encoded_hash and verified)


def csrf_value(token: str, nonce: bytes) -> str:
    return hmac.new(token.encode("ascii"), nonce, hashlib.sha256).hexdigest()


def add_session(db, user: TacticsUser) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    nonce = secrets.token_bytes(32)
    csrf = csrf_value(token, nonce)
    created = now()
    db.add(TacticsSession(
        user_id=user.id, session_digest=digest(token), csrf_nonce=nonce, csrf_digest=digest(csrf),
        credential_version=user.credential_version, created_at=created,
        expires_at=created + timedelta(days=7),
    ))
    return token, csrf


async def user_by_name(username: str) -> TacticsUser | None:
    async with get_session_factory()() as db:
        return await db.scalar(sa.select(TacticsUser).where(
            TacticsUser.username_normalized == username.lower()
        ))


def read_token(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    if len(token) != 43 or any(char not in
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in token
    ):
        raise AppError(401, "USER_AUTH_REQUIRED", "请先登录。")
    return token


@asynccontextmanager
async def authenticated(request: Request, *, write: bool = False):
    token = read_token(request)
    async with get_session_factory()() as db, db.begin():
        # Read the owner first, then lock user -> session consistently with credential revocation.
        user_id = await db.scalar(sa.select(TacticsSession.user_id).where(
            TacticsSession.session_digest == digest(token)
        ))
        user = await db.scalar(sa.select(TacticsUser).where(
            TacticsUser.id == user_id
        ).with_for_update(key_share=True)) if user_id else None
        session = await db.scalar(sa.select(TacticsSession).where(
            TacticsSession.session_digest == digest(token)
        ).with_for_update()) if user else None
        if not session or session.revoked_at or session.expires_at <= now() or (
            session.credential_version != user.credential_version
        ):
            raise AppError(401, "USER_AUTH_REQUIRED", "请先登录。")
        if write and not hmac.compare_digest(
            digest(request.headers.get("x-csrf-token", "")), session.csrf_digest
        ):
            raise AppError(403, "CSRF_INVALID", "页面验证已失效，请刷新后重试。")
        yield db, user, session, token


async def revoke_sessions(db, user: TacticsUser) -> None:
    await db.execute(sa.update(TacticsSession).where(
        TacticsSession.user_id == user.id, TacticsSession.revoked_at.is_(None)
    ).values(revoked_at=now()))


def user_view(user: TacticsUser) -> dict:
    return {"id": str(user.id), "username": user.username, "display_name": user.display_name,
            "created_at": user.created_at.isoformat()}
