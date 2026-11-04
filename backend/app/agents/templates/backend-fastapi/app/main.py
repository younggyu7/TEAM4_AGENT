"""서버 시작점: 업무 API(/api/v1)·화면 보조 API(/api/ui)·로그인(/api/auth)과 화면(static, SPA).

uvicorn app.main:app --port 8080. 환경 변수: DB_URL, DEMO_PASSWORD, RESET_PASSWORDS, SYSTEM_API_KEY, LOCAL_DIR,
ACCESS_LOG_DIR(요청 한 줄씩 기록, 실행 미리보기가 쓰는 중인지 본다), STATIC_DIR(빌드한 화면, 기본 ./static).
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from starlette.exceptions import HTTPException

from app.api import ROUTERS
from app.auth import accounts
from app.auth.routes import router as auth_router, token
from app.common.errors import ApiError
from app.common.user import CurrentUser, system_call

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")
STATIC = Path(os.getenv("STATIC_DIR", Path(__file__).resolve().parent.parent / "static")).resolve()


@asynccontextmanager
async def lifespan(_: FastAPI):
    accounts.setup()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


@app.exception_handler(ApiError)
async def api_error(_: Request, e: ApiError):
    return _error(e.status, e.code, e.message)


@app.exception_handler(RequestValidationError)
async def unreadable(_: Request, e: RequestValidationError):
    return _error(400, "VALIDATION_FAILED", "요청 형식이 올바르지 않습니다.")


@app.exception_handler(IntegrityError)
@app.exception_handler(DataError)
async def integrity(_: Request, e: Exception):
    return _error(400, "VALIDATION_FAILED", "필수 값이나 값의 조건이 맞지 않습니다.")


@app.exception_handler(HTTPException)
async def http_error(_: Request, e: HTTPException):
    known = {404: ("NOT_FOUND", "대상을 찾을 수 없습니다."), 405: ("METHOD_NOT_ALLOWED", "허용되지 않은 메서드입니다.")}
    code, message = known.get(e.status_code, ("HTTP_ERROR", str(e.detail)))
    return _error(e.status_code, code, message)


# MySQL: 값 조건(CHECK 3819)·맞지 않는 값(1366·1292·1265)·너무 긴 값(1406)·범위(1264)는 드라이버가 따로 분류하지 않기도 한다
MYSQL_VALUE_ERRORS = {3819, 1366, 1292, 1265, 1406, 1264}


@app.exception_handler(DBAPIError)
async def db_error(request: Request, e: DBAPIError):
    code = e.orig.args[0] if e.orig is not None and getattr(e.orig, "args", None) else None
    if code in MYSQL_VALUE_ERRORS:
        return await integrity(request, e)
    return await unexpected(request, e)


@app.exception_handler(Exception)
async def unexpected(_: Request, e: Exception):
    log.exception("처리하지 못한 오류")   # 내부 메시지는 응답에 넣지 않는다
    return _error(500, "INTERNAL_ERROR", "서버 오류가 발생했습니다.")


@app.middleware("http")
async def session(request: Request, call_next):
    """요청의 로그인 사용자를 붙인다. 막는 일은 하지 않는다: 각 API가 user.require…로 확인한다 (외부 시스템은 X-Api-Key)."""
    found = accounts.user(token(request))
    system = system_call(request)
    request.state.user = CurrentUser(found.id, found.role, system) if found else CurrentUser(None, "", system)
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        _access(request, status)


def _access(request: Request, status: int) -> None:
    """ACCESS_LOG_DIR이 있으면 요청 한 줄을 남긴다. 기록하지 못해도 요청은 처리한다."""
    folder = os.getenv("ACCESS_LOG_DIR")
    if not folder:
        return
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        with open(Path(folder) / "access.log", "a", encoding="utf-8") as out:
            out.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {request.method} {request.url.path} {status}\n")
    except OSError:
        pass


app.include_router(auth_router)
for router in ROUTERS:
    app.include_router(router)


def _specific(route) -> tuple:
    """같은 모양의 경로는 글자 경로를 변수 경로보다 먼저 (Spring처럼 /orders/summary가 /orders/{id}보다 먼저)."""
    if not isinstance(route, APIRoute):
        return (1,)
    return (0, *(1 if part.startswith("{") else 0 for part in route.path.strip("/").split("/")))


app.router.routes.sort(key=_specific)


@app.get("/{path:path}", include_in_schema=False)
def web(path: str):
    """빌드한 화면. 없는 파일은 index.html (화면 안의 주소). /api 아래는 404 (경로는 있고 메서드가 다르면 405)."""
    if path == "api" or path.startswith("api/"):
        if any(isinstance(r, APIRoute) and r.endpoint is not web and r.path_regex.match("/" + path) for r in app.router.routes):
            raise ApiError(405, "METHOD_NOT_ALLOWED", "허용되지 않은 메서드입니다.")
        raise ApiError.not_found()
    target = (STATIC / path).resolve()
    if path and STATIC in target.parents and target.is_file():
        return FileResponse(target)
    if (STATIC / "index.html").is_file():
        return FileResponse(STATIC / "index.html")
    raise ApiError.not_found()
