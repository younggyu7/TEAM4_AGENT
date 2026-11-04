"""포털 로그인 (서비스 틀). 스펙의 업무 API(/api/v1)와 따로 둔다."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response

from app.auth import accounts
from app.common.errors import ApiError

router = APIRouter(prefix="/api/auth")
NO_STORE = {"Cache-Control": "no-store"}


def token(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    return header[7:].strip() if header and header.startswith("Bearer ") else None


@router.post("/login")
def login(body: Any = Body(None)):
    if not isinstance(body, dict) or any(v is not None and not isinstance(v, str) for v in (body.get("loginId"), body.get("password"))):
        raise ApiError(400, "VALIDATION_FAILED", "요청 형식이 올바르지 않습니다.")
    return JSONResponse(accounts.login(body.get("loginId"), body.get("password")), headers=NO_STORE)


@router.get("/me")
def me(request: Request):
    found = accounts.me(token(request))
    if found is None:
        raise ApiError(401, "UNAUTHORIZED", "다시 로그인하세요.")
    return JSONResponse(found, headers=NO_STORE)


@router.post("/logout", status_code=204)
def logout(request: Request):
    accounts.logout(token(request))
    return Response(status_code=204)
