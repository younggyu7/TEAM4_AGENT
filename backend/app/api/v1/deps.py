"""API 공통 입력 검사: 프로젝트·산출물 값이 저장소 안을 가리키는지, 이 프로젝트에 진행 중인 작업이 있는지."""
from __future__ import annotations

from app.agents.storage import get_store
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.services import jobs, output_store


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
