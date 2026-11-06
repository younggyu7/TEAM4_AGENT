"""작업 진행 상황.

  GET /jobs?id=<job>   작업 진행 상황 {status, message, log, new_version, warnings} (담당자가 아닌 프로젝트의 작업은 404)
  GET /jobs/running?project=   그 프로젝트에서 진행 중인 작업 {job} (없으면 null). 로그아웃·새로고침 뒤 화면이 진행 상황을 이어 본다
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.deps import _project, visible_job
from app.core.exceptions import NotFound
from app.services import jobs

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.get("/jobs", summary="작업 진행 상황")
def get_job(id: str):
    job = visible_job(jobs.get_job(id))
    if job is None:
        raise NotFound("작업을 찾을 수 없습니다. 서버를 다시 띄우면 작업 기록이 사라집니다.")
    return {"job": job.to_dict()}


@router.get("/jobs/running", summary="프로젝트에서 진행 중인 작업 (화면을 다시 열어도 이어 보기)")
def get_running_job(project: str):
    job = jobs.running_job(_project(project))
    return {"job": job.to_dict() if job else None}
