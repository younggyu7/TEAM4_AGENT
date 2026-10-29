"""FastAPI 앱: 프론트(TEAM4_AGENT_FRONT)가 쓰는 /api/v1 API.

설계 Agent는 CLI(`python -m app …`)와 같은 코드이고, 오래 걸리는 실행은 app/services/jobs.py가 하위 프로세스로 띄운다.
산출물은 OUTPUT_DIR(기본 output/)에 버전 폴더로 저장한다. 서버는 backend/ 폴더에서 띄운다.
"""
from fastapi import FastAPI

from app.api.v1 import health, projects
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.core.security import add_cors


def create_app() -> FastAPI:
    setup_logging(get_settings().log_level)
    app = FastAPI(
        title="요구사항 → 설계 산출물 Agent",
        description="고객 요구사항 문서로 요구사항 정의서·기능 명세서·ERD·DDL·추적표를 만드는 백엔드 API입니다.",
        openapi_tags=[
            {"name": "projects", "description": "프로젝트·설계 버전·산출물·새 버전 만들기"},
            {"name": "health", "description": "서버 상태 확인"},
        ],
    )
    add_cors(app)
    register_exception_handlers(app)
    app.include_router(projects.router, prefix="/api/v1")
    app.include_router(health.router)
    return app


app = create_app()
