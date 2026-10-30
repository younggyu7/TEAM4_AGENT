"""FastAPI 앱: 프론트(TEAM4_AGENT_FRONT)가 쓰는 /api/v1 API.

설계 Agent는 CLI(`python -m app …`)와 같은 코드이고, 오래 걸리는 실행은 app/services/jobs/가 하위 프로세스로 띄운다.
산출물은 산출물 저장소(DATABASE_URL이 있으면 PostgreSQL, 없으면 OUTPUT_DIR 폴더)에 버전별로 저장한다. 서버는 backend/ 폴더에서 띄운다.
기능별 API는 app/api/v1/의 파일마다 라우터 하나이고, 여기서 /api/v1 아래에 연결한다.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import artifacts, jobs, projects, review, runs, versions
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.core.security import add_cors
from app.services import jobs as job_service

API_PREFIX = "/api/v1"
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 서비스 DB가 있으면: 지난번 서버가 추적하던 '진행 중' 작업은 더 볼 수 없으므로 실패로 닫는다
    if closed := job_service.close_orphaned_jobs():
        log.info("서버 시작: 추적이 끊긴 진행 중 작업 %d건을 실패로 닫음", closed)
    yield


setup_logging(get_settings().log_level)
app = FastAPI(
    lifespan=lifespan,
    title="요구사항 → 설계 산출물 Agent",
    description="고객 요구사항 문서로 요구사항 정의서·기능 명세서·ERD·DDL·추적표를 만드는 백엔드 API입니다.",
    openapi_tags=[
        {"name": "projects", "description": "프로젝트·설계 버전·산출물·새 버전 만들기"},
        {"name": "health", "description": "서버 상태 확인"},
    ],
)
add_cors(app)
register_exception_handlers(app)


# 서버 상태 확인용 API
@app.get("/health", tags=["health"], summary="서버 상태 확인")
def health() -> dict:
    return {"status": "ok"}


# 라우터 연결 (/api/v1 아래, 기능별 파일 하나에 라우터 하나)
app.include_router(projects.router, prefix=API_PREFIX)    # 프로젝트·버전 목록, 버전 제목
app.include_router(artifacts.router, prefix=API_PREFIX)   # design.json·산출물 원문
app.include_router(versions.router, prefix=API_PREFIX)    # 새 설계 버전 만들기 (작업)
app.include_router(review.router, prefix=API_PREFIX)      # ERD 최종 확정 → 최종본 (작업)
app.include_router(runs.router, prefix=API_PREFIX)        # 문서 업로드 → 기능 명세 초안 (작업)
app.include_router(jobs.router, prefix=API_PREFIX)        # 작업 진행 상황
