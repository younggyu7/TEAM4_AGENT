"""작업 진행 상황.

  GET /jobs?id=<job>   작업 진행 상황 {status, message, log, new_version, warnings}
"""
from __future__ import annotations

from fastapi import APIRouter

from app.core.exceptions import NotFound
from app.services import jobs

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.get("/jobs", summary="작업 진행 상황")
def get_job(id: str):
    job = jobs.get_job(id)
    if job is None:
        raise NotFound("작업을 찾을 수 없습니다. 서버를 다시 띄우면 작업 기록이 사라집니다.")
    return {"job": job.to_dict()}
