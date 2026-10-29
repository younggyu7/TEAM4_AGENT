"""API 오류: 화면은 모든 오류를 {"detail": "..."} 한 형태로 받는다 (FastAPI 기본 형태와 같음)."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("app")


class AppError(Exception):
    """화면에 그대로 보여 줄 오류. 메시지는 사용자가 읽을 문장으로 쓴다."""
    status_code = 400

    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class BadRequest(AppError):
    status_code = 400


class NotFound(AppError):
    status_code = 404


class Conflict(AppError):
    """같은 프로젝트에서 이미 작업이 도는 중 등"""
    status_code = 409


class PayloadTooLarge(AppError):
    status_code = 413


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 원인은 서버 로그에만 남기고 응답에는 내부 정보(경로·스택)를 싣지 않는다
        log.error("처리하지 못한 오류: %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "서버 오류가 났습니다. 서버 로그를 확인하세요."})
