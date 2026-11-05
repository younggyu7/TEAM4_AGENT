"""프로젝트·버전 목록과 버전 제목.

  GET  /projects          프로젝트와 설계 버전 목록 (버전 번호는 '4', '4.1' 같은 문자열). 내가 담당자인 프로젝트만 (팀장은 모두)
  POST /projects/title    버전 제목 저장
  GET  /tech-stacks       개발 스펙의 기술 스택 선택지 (화면·서버·DB, 개발 틀이 있는지)
  GET  /projects/stack    버전의 기술 스택 상태 (원문·확정·미정, 개발 틀 없는 층 경고)
  POST /projects/tech-stack  최종본의 기술 스택 확정 → 개발 스펙 10개 다시 쓰기 (LLM 없음)
  POST /projects/dev-spec/fill  예전 최종본의 빠진 개발 스펙 채우기 (업무 동작 설계 LLM 1회 → BEHAVIOR.md 등 다시 쓰기)
  DELETE /projects        프로젝트 삭제 (팀장만, confirm에 프로젝트 이름을 그대로 적어야 함, 되돌릴 수 없음)
  GET  /projects/members  담당자(이 프로젝트를 보는 회원)와 Slack 채널 (채널이 아직 없거나 Slack이 꺼져 있으면 slack: null)
  POST /projects/members  담당자 바꾸기 (팀장만): 뺀 회원은 이 프로젝트를 더는 못 본다. Slack 채널이 있으면 더한 회원은 초대, 뺀 회원은 내보냄
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents.roles.api_designer import stack_choices
from app.agents.storage import get_store
from app.agents.tools import stacks
from app.agents.llm.client import LLMError
from app.agents.tools.exporter import confirm_tech_stack, fill_dev_spec
from app.api.v1.deps import _no_running_job, _project, current_user, ensure
from app.core import guards
from app.core.auth import ROLES
from app.core.exceptions import AppError, BadRequest, Forbidden, NotFound
from app.schemas.version import TechStackUpdate, TitleUpdate, VersionRef
from app.services import output_store, project_access, project_delete, slack, users

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


class ProjectMembers(BaseModel):
    project: str
    members: list[int] = Field(default_factory=list, max_length=200)   # 새 담당자 회원 ID 전체 (GET /members, 팀장은 늘 포함)


@router.get("/projects", summary="프로젝트와 설계 버전 목록 (내가 담당자인 프로젝트만, 팀장은 모두)")
def list_projects(member: users.Member = Depends(current_user)):
    projects = output_store.find_projects()
    mine = set(project_access.visible(member, [p["id"] for p in projects]))
    return {"output_dir": get_store().location, "projects": [p for p in projects if p["id"] in mine]}


@router.delete("/projects", summary="프로젝트 삭제 (팀장만, 되돌릴 수 없음)")
def delete_project(project: str, confirm: str, member: users.Member = Depends(current_user)):
    ensure(member, "project_delete")
    project = _project(project)
    if confirm != project:
        raise BadRequest("확인 칸에 프로젝트 이름을 그대로 적어 주세요.")
    return project_delete.delete_project(project, member)


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


@router.post("/projects/dev-spec/fill", summary="예전 최종본의 빠진 개발 스펙 채우기 (업무 동작 설계 LLM 1회)")
def fill_spec(body: VersionRef, member: users.Member = Depends(current_user)):
    ensure(member, "erd_confirm")   # 개발 스펙은 'ERD 최종 확정'이 만든다 → 같은 권한 (데이터 파트장·팀장)
    project, version = _project(body.project), guards.version(body.version)
    if not output_store.has_version(project, version):
        raise NotFound(f"v{version}이 없습니다.")
    _no_running_job(body.project)
    try:
        return {"dev_spec": fill_dev_spec(project, version)}
    except ValueError as e:
        raise BadRequest(str(e)) from e
    except LLMError as e:   # 실패를 빈 문서로 숨기지 않는다: 파일은 그대로 두고 알린다
        raise AppError(f"업무 동작 설계 요청이 실패해 채우지 못했습니다: {e}", 502) from e


def _members_view(project: str) -> dict:
    """담당자 카드: 담당자(Slack 채널에 들어 있는지)와 채널. 팀장은 고르지 않아도 늘 담당자"""
    channel = slack.project_config(project) if slack.enabled() else None
    in_channel = set(channel["members"]) if channel and channel["channel_id"] else set()
    return {"members": [{"id": m.id, "name": m.name, "role": m.role, "role_label": ROLES.get(m.role, "역할 없음"),
                         "has_slack": bool(m.slack_id), "in_channel": m.id in in_channel, "always": m.role == project_access.ALWAYS}
                        for m in project_access.assignees(project)],
            "slack": slack.project_info(project), "slack_enabled": slack.enabled()}


@router.get("/projects/members", summary="프로젝트 담당자와 Slack 채널")
def get_project_members(project: str):
    return _members_view(_project(project))


@router.post("/projects/members", summary="담당자 바꾸기 (팀장만)")
def set_project_members(body: ProjectMembers, member: users.Member = Depends(current_user)):
    if member.role != "lead":
        raise Forbidden("담당자는 팀장만 바꿀 수 있습니다.")
    project = _project(body.project)
    before = {m.id: m for m in project_access.assignees(project)}
    after = {m.id: m for m in project_access.set_members(project, body.members)}
    result = {"added": [m.name for i, m in after.items() if i not in before],
              "removed": [m.name for i, m in before.items() if i not in after], "failed": []}
    # Slack 채널이 있으면 채널도 담당자에 맞춘다 (Slack이 실패해도 담당자는 이미 바뀌었다)
    if slack.enabled() and slack.project_info(project):
        try:
            result["failed"] = slack.update_members(project, list(after), member)["failed"]
        except slack.SlackError as e:
            result["failed"] = [{"name": "Slack 채널", "reason": str(e)}]
    return {"result": result, **_members_view(project)}
