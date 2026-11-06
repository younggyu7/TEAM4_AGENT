"""API 공통 입력 검사: 로그인한 회원과 권한, 프로젝트·산출물 값이 저장소 안을 가리키는지(담당자가 아니면 없는 것처럼),
진행 중인 작업이 있는지."""
from __future__ import annotations

from contextvars import ContextVar

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from app.agents.storage import get_store
from app.core.auth import allowed, denied_message
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound, Unauthorized
from app.services import jobs, output_store, project_access, users

SESSION_COOKIE = "reqagent_session"
HIDDEN = "프로젝트가 없거나 이 프로젝트의 담당자가 아닙니다 (PM이 프로젝트 화면 '담당자'에서 추가)."

# 이 요청을 보낸 회원: _project·_file·visible_job이 담당자가 아닌 프로젝트를 막는 데 쓴다 (요청마다 따로, 요청 밖이면 None)
_viewer: ContextVar[users.Member | None] = ContextVar("viewer", default=None)


def _signed_in(request: Request) -> users.Member:
    member = users.member_by_token(request.cookies.get(SESSION_COOKIE, ""))
    if member is None:
        raise Unauthorized("로그인이 필요합니다.")
    return member


async def current_user(request: Request) -> users.Member:
    """로그인한 회원 (쿠키 세션). 없거나 끝났으면 401 — 화면은 로그인 화면으로 보낸다.
    이 요청의 회원으로 남겨 두어 프로젝트를 받는 API가 담당자인지 본다 (async: 남긴 값이 엔드포인트까지 이어지게)."""
    member = await run_in_threadpool(_signed_in, request)
    _viewer.set(member)
    return member


def _can_see(project: str) -> bool:
    viewer = _viewer.get()
    return viewer is None or project_access.can_see(viewer, project)


def ensure(member: users.Member, permission: str) -> None:
    """역할 권한 검사 (core/auth.PERMISSIONS). 없으면 403과 누가 할 수 있는지."""
    if not allowed(member.role, permission):
        raise Forbidden(denied_message(permission))


def admin_user(request: Request) -> users.Member:
    member = _signed_in(request)
    if not member.is_admin:
        raise Forbidden("회원 관리는 관리자만 할 수 있습니다.")
    return member


def _project(project: str) -> str:
    """저장소 안에 있는 프로젝트 ID (밖을 가리키거나 없으면 400). 요청한 회원이 담당자가 아니면(PM 밖) 404."""
    if not output_store.has_project(project):
        raise BadRequest("project 값이 올바르지 않습니다.")
    if not _can_see(project):
        raise NotFound(HIDDEN)
    return project


def visible_job(job: jobs.Job | None) -> jobs.Job | None:
    """작업의 프로젝트를 볼 수 있는 회원에게만 그 작업 (아니면 None = 없는 작업처럼)"""
    return job if job is not None and _can_see(job.project) else None


def _file(project: str, version: str, name: str) -> str:
    """산출물 원문 (허용 목록·버전 형식이 틀리면 400, 없으면 404)."""
    if not output_store.valid_file(project, version, name):
        raise BadRequest("project·version·name 값이 올바르지 않습니다.")
    if not _can_see(project):
        raise NotFound(HIDDEN)
    try:
        text = get_store().read(project, version, name)
    except ValueError as e:   # 저장소 밖을 가리키는 프로젝트
        raise BadRequest("project·version·name 값이 올바르지 않습니다.") from e
    if text is None:
        raise NotFound(f"{name} 파일이 없습니다.")
    return text


def _no_running_job(project: str) -> None:
    if jobs.running_job(project):
        raise Conflict("이 프로젝트에서 이미 작업이 진행 중입니다.")
