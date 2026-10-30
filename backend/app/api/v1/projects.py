"""프로젝트·버전 목록과 버전 제목.

  GET  /projects          프로젝트와 설계 버전 목록 (버전 번호는 '4', '4.1' 같은 문자열)
  POST /projects/title    버전 제목 저장
"""
from __future__ import annotations

from fastapi import APIRouter

from app.agents.storage import get_store
from app.api.v1.deps import _project
from app.core import guards
from app.schemas.version import TitleUpdate
from app.services import output_store

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.get("/projects", summary="프로젝트와 설계 버전 목록")
def list_projects():
    return {"output_dir": get_store().location, "projects": output_store.find_projects()}


@router.post("/projects/title", summary="버전 제목 저장")
def set_title(body: TitleUpdate):
    output_store.save_title(_project(body.project), guards.version(body.version), body.title)
    return {"ok": True}
