"""Авторизация админки: один общий admin-токен из env ADMIN_TOKEN.

Принимается как `Authorization: Bearer <токен>` (для API и fetch),
так и cookie `admin_token` (её ставит страница /login, так удобно htmx)."""
from __future__ import annotations

import os
import secrets

from fastapi import Cookie, Header, HTTPException, Request, status

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "changeme")


def check_token(raw: str | None) -> str | None:
    if raw and secrets.compare_digest(raw, ADMIN_TOKEN):
        return raw
    return None


def _from_request(
    request: Request | None,
    authorization: str | None,
    admin_token: str | None,
) -> tuple[str | None, str | None]:
    # Прямой вызов (не через FastAPI DI) подсовывает объекты Header/Cookie
    # вместо строк — такое отбрасываем, берём из request напрямую.
    if not isinstance(authorization, str):
        authorization = None
    if not isinstance(admin_token, str):
        admin_token = None
    if request is not None:
        if not authorization:
            authorization = request.headers.get("authorization")
        if not admin_token:
            admin_token = request.cookies.get("admin_token")
    return authorization, admin_token


def _pick(authorization: str | None, admin_token: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if admin_token:
        return admin_token
    return None


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    admin_token: str | None = Cookie(default=None),
) -> str:
    authorization, admin_token = _from_request(request, authorization, admin_token)
    token = _pick(authorization, admin_token)
    if not check_token(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужен admin-токен")
    return token  # type: ignore[return-value]


def optional_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    admin_token: str | None = Cookie(default=None),
) -> str | None:
    authorization, admin_token = _from_request(request, authorization, admin_token)
    return check_token(_pick(authorization, admin_token))
