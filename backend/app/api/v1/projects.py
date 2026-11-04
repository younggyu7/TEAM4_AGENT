"""프로젝트·버전 목록과 버전 제목.

  GET  /projects          프로젝트와 설계 버전 목록 (버전 번호는 '4', '4.1' 같은 문자열)
  POST /projects/title    버전 제목 저장
  GET  /tech-stacks       개발 스펙의 기술 스택 선택지 (화면·서버·DB, 개발 틀이 있는지)
  GET  /projects/stack    버전의 기술 스택 상태 (원문·확정·미정, 개발 틀 없는 층 경고)
  POST /projects/tech-stack  최종본의 기술 스택 확정 → 개발 스펙 10개 다시 쓰기 (LLM 없음)
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from app.agents.roles.api_designer import stack_choices
from app.agents.storage import get_store
from app.agents.tools import stacks
from app.agents.tools.exporter import confirm_tech_stack
from app.api.v1.deps import _no_running_job, _project, current_user, ensure
from app.core import guards
from app.core.exceptions import BadRequest, NotFound
from app.schemas.version import TechStackUpdate, TitleUpdate
from app.services import output_store, users

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.get("/projects", summary="프로젝트와 설계 버전 목록")
def list_projects():
    return {"output_dir": get_store().location, "projects": output_store.find_projects()}


@router.post("/projects/title", summary="버전 제목 저장")
def set_title(body: TitleUpdate):
    output_store.save_title(_project(body.project), guards.version(body.version), body.title)
    return {"ok": True}


@router.get("/tech-stacks", summary="개발 스펙의 기술 스택 선택지")
def list_tech_stacks():
    # catalog: 화면 / 서버 / DB를 나눠 고르는 칸 (supported=False는 개발 틀 없음: 고를 수 있지만 개발 때 AI가 뼈대부터 → 비용 더 듦)
    return {"choices": stack_choices(), "catalog": stacks.catalog(), "cost_note": stacks.COST_NOTE}


@router.get("/projects/stack", summary="버전의 기술 스택 상태 (원문·확정·미정, 개발 틀이 없는 층 경고)")
def stack_status(project: str, version: str):
    project, version = _project(project), guards.version(version)
    text = get_store().read(project, version, "design.json")
    if text is None:
        raise NotFound(f"v{version}이 없습니다.")
    return stacks.status(json.loads(text))


@router.post("/projects/tech-stack", summary="최종본의 기술 스택 확정 (개발 스펙 다시 쓰기, LLM 없음)")
def set_tech_stack(body: TechStackUpdate, member: users.Member = Depends(current_user)):
    ensure(member, "final_decide")
    project, version = _project(body.project), guards.version(body.version)
    try:
        stack = stacks.compose(body.parts) if body.parts else " ".join(body.stack.split())
    except ValueError as e:
        raise BadRequest(str(e)) from e
    if not stack or len(stack) > 200:
        raise BadRequest("기술 스택은 1~200자로 적어 주세요.")
    if not output_store.has_version(project, version):
        raise NotFound(f"v{version}이 없습니다.")
    _no_running_job(body.project)
    try:
        return {"dev_spec": confirm_tech_stack(project, version, stack, body.parts)}
    except ValueError as e:
        raise BadRequest(str(e)) from e
