"""Same-origin ordinary-account endpoints; no shared administrator credentials."""

from __future__ import annotations

import hmac
import secrets

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.routing import JsonBodyRoute
from app.db.session import get_session_factory
from app.db.tactics_models import PersonalNotebook, TacticsUser
from app.schemas.tactics_auth import (
    EmptyBody,
    LoginBody,
    PasswordBody,
    ProfileBody,
    RecoverBody,
    RecoveryCodeBody,
    RegisterBody,
)
from app.services import tactics_accounts as accounts
from app.services import tactics_audit as audit


class AccountRoute(JsonBodyRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def checked(request: Request):
            if not get_settings().tactics_enabled:
                raise AppError(404, "ENDPOINT_NOT_FOUND", "接口不存在。")
            allowed_queries = {field.alias for field in self.dependant.query_params}
            if set(request.query_params) - allowed_queries or len(
                request.query_params.multi_items()
            ) != len(request.query_params):
                raise AppError(422, "VALIDATION_ERROR", "请求参数不合法。")
            if request.method not in {"GET", "HEAD"}:
                if request.headers.get("origin") != get_settings().public_base_url.rstrip("/"):
                    raise AppError(403, "ORIGIN_INVALID", "不允许此来源的写入请求。")
                if request.url.path in {
                    "/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/recover"
                } and request.cookies.get(accounts.COOKIE):
                    try:
                        async with accounts.authenticated(request, write=True):
                            pass
                    except AppError as exc:
                        if exc.status_code != 401:
                            raise
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 8192:
                        raise AppError(413, "REQUEST_TOO_LARGE", "请求内容过大。")
                request._body = bytes(body)
            return await original(request)

        return checked


router = APIRouter(prefix="/api/v1", route_class=AccountRoute, tags=["tactics-accounts"])


def response(body: dict, token: str | None = None, *, status: int = 200, clear=False):
    result = JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})
    secure = get_settings().app_env == "production"
    if clear:
        result.delete_cookie(
            accounts.COOKIE, path="/", secure=secure, httponly=True, samesite="lax"
        )
    elif token:
        result.set_cookie(accounts.COOKIE, token, max_age=7 * 86400, path="/", secure=secure,
                          httponly=True, samesite="lax")
    return result


@router.post("/auth/register", status_code=201)
async def register(body: RegisterBody, request: Request):
    accounts.rate_limit(request, "register", body.username)
    password_hash = await accounts.password_work(accounts.HASHER.hash, body.password)
    recovery = secrets.token_urlsafe(32)
    try:
        async with get_session_factory()() as db, db.begin():
            user = TacticsUser(username=body.username, username_normalized=body.username.lower(),
                               display_name=body.display_name, password_hash=password_hash,
                               recovery_digest=accounts.digest(recovery), credential_version=1)
            db.add(user)
            await db.flush()
            db.add(PersonalNotebook(user_id=user.id))
            token, csrf = accounts.add_session(db, user)
            view = accounts.user_view(user)
            await audit.append(db, "account.register", user.id, request=request,
                               resource_id=user.id)
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", "") == (
            "users_username_normalized_key"
        ):
            raise AppError(409, "USERNAME_TAKEN", "用户名已被使用。") from None
        raise
    return response(
        {"user": view, "recovery_code": recovery, "csrf_token": csrf}, token, status=201
    )


@router.post("/auth/login")
async def login(body: LoginBody, request: Request):
    accounts.rate_limit(request, "login", body.username)
    snapshot = await accounts.user_by_name(body.username)
    if not await accounts.verify_password(
        snapshot.password_hash if snapshot else None, body.password
    ):
        raise accounts.auth_error()
    async with get_session_factory()() as db, db.begin():
        user = await db.scalar(sa.select(TacticsUser).where(
            TacticsUser.id == snapshot.id
        ).with_for_update(key_share=True))
        if user.credential_version != snapshot.credential_version:
            raise accounts.auth_error()
        token, csrf = accounts.add_session(db, user)
        view = accounts.user_view(user)
        await audit.append(db, "account.login", user.id, request=request, resource_id=user.id)
    return response({"user": view, "csrf_token": csrf}, token)


@router.get("/auth/session")
async def session_info(request: Request):
    async with accounts.authenticated(request) as (_, user, session, token):
        return response({"user": accounts.user_view(user),
                         "csrf_token": accounts.csrf_value(token, session.csrf_nonce)})


@router.post("/auth/logout")
async def logout(body: EmptyBody, request: Request):
    try:
        async with accounts.authenticated(request, write=True) as (db, user, session, _):
            session.revoked_at = accounts.now()
            await audit.append(db, "account.logout", user.id, request=request, resource_id=user.id)
    except AppError as exc:
        if exc.status_code != 401:
            raise
    return response({"ok": True}, clear=True)


@router.patch("/auth/profile")
async def profile(body: ProfileBody, request: Request):
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        if user.display_name != body.display_name:
            user.display_name = body.display_name
            user.updated_at = accounts.now()
            await audit.append(db, "account.profile.update", user.id, request=request,
                               resource_id=user.id)
        view = accounts.user_view(user)
    return response({"user": view})


async def checked_password(request: Request, password: str) -> tuple:
    async with accounts.authenticated(request, write=True) as (_, user, _, _):
        snapshot = (user.id, user.password_hash, user.credential_version, user.username)
    accounts.rate_limit(request, "credential", snapshot[3])
    if not await accounts.verify_password(snapshot[1], password):
        raise accounts.auth_error()
    return snapshot


@router.post("/auth/password")
async def password(body: PasswordBody, request: Request):
    snapshot = await checked_password(request, body.current_password)
    password_hash = await accounts.password_work(accounts.HASHER.hash, body.new_password)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        if user.credential_version != snapshot[2]:
            raise accounts.auth_error()
        user.password_hash = password_hash
        user.credential_version += 1
        user.updated_at = accounts.now()
        await accounts.revoke_sessions(db, user)
        token, csrf = accounts.add_session(db, user)
        view = accounts.user_view(user)
        await audit.append(db, "account.password.change", user.id, request=request,
                           resource_id=user.id)
    return response({"user": view, "csrf_token": csrf}, token)


@router.post("/auth/recovery-code")
async def recovery_code(body: RecoveryCodeBody, request: Request):
    snapshot = await checked_password(request, body.current_password)
    code = secrets.token_urlsafe(32)
    async with accounts.authenticated(request, write=True) as (db, user, _, _):
        if user.credential_version != snapshot[2]:
            raise accounts.auth_error()
        user.recovery_digest = accounts.digest(code)
        user.updated_at = accounts.now()
        await audit.append(db, "account.recovery.rotate", user.id, request=request,
                           resource_id=user.id)
    return response({"recovery_code": code})


@router.post("/auth/recover")
async def recover(body: RecoverBody, request: Request):
    accounts.rate_limit(request, "recover", body.username)
    snapshot = await accounts.user_by_name(body.username)
    valid = hmac.compare_digest(
        accounts.digest(body.recovery_code), snapshot.recovery_digest if snapshot else bytes(32)
    )
    if not snapshot or not valid:
        raise accounts.recovery_error()
    password_hash = await accounts.password_work(accounts.HASHER.hash, body.new_password)
    code = secrets.token_urlsafe(32)
    async with get_session_factory()() as db, db.begin():
        user = await db.scalar(sa.select(TacticsUser).where(
            TacticsUser.id == snapshot.id
        ).with_for_update(key_share=True))
        if user.credential_version != snapshot.credential_version or not hmac.compare_digest(
            user.recovery_digest, snapshot.recovery_digest
        ):
            raise accounts.recovery_error()
        user.password_hash = password_hash
        user.recovery_digest = accounts.digest(code)
        user.credential_version += 1
        user.updated_at = accounts.now()
        await accounts.revoke_sessions(db, user)
        await audit.append(db, "account.recover", user.id, request=request, resource_id=user.id)
    return response({"recovery_code": code}, clear=True)


@router.get("/notebooks/personal")
async def personal_notebook(request: Request):
    async with accounts.authenticated(request) as (db, user, _, _):
        notebook = await db.scalar(sa.select(PersonalNotebook).where(
            PersonalNotebook.user_id == user.id
        ))
        if notebook is None:
            raise AppError(500, "INTERNAL_ERROR", "个人本暂时不可用。")
        view = {"id": str(notebook.id), "kind": "personal",
                "user_id": str(user.id), "team_id": None}
    return response({"notebook": view})
