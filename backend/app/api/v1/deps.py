"""API 공통 입력 검사: 로그인한 회원과 권한, 프로젝트·산출물 값이 저장소 안을 가리키는지, 진행 중인 작업이 있는지."""
from __future__ import annotations

from fastapi import Request

from app.agents.storage import get_store
from app.core.auth import allowed, denied_message
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound, Unauthorized
from app.services import jobs, output_store, users

SESSION_COOKIE = "reqagent_session"


def current_user(request: Request) -> users.Member:
    """로그인한 회원 (쿠키 세션). 없거나 끝났으면 401 — 화면은 로그인 화면으로 보낸다."""
    member = users.member_by_token(request.cookies.get(SESSION_COOKIE, ""))
    if member is None:
        raise Unauthorized("로그인이 필요합니다.")
    return member


def ensure(member: users.Member, permission: str) -> None:
    """역할 권한 검사 (core/auth.PERMISSIONS). 없으면 403과 누가 할 수 있는지."""
    if not allowed(member.role, permission):
        raise Forbidden(denied_message(permission))


def admin_user(request: Request) -> users.Member:
    member = current_user(request)
    if not member.is_admin:
        raise Forbidden("회원 관리는 관리자만 할 수 있습니다.")
    return member


def _project(project: str) -> str:
    """저장소 안에 있는 프로젝트 ID (밖을 가리키거나 없으면 400)."""
    if not output_store.has_project(project):
        raise BadRequest("project 값이 올바르지 않습니다.")
    return project


def _file(project: str, version: str, name: str) -> str:
    """산출물 원문 (허용 목록·버전 형식이 틀리면 400, 없으면 404)."""
    if not output_store.valid_file(project, version, name):
        raise BadRequest("project·version·name 값이 올바르지 않습니다.")
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
