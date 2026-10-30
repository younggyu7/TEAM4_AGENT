"""ERD 최종 확정 (작업).

  POST /projects/review   ERD 최종 확정 → 최종본 v<ERD>.1 (추적·의미 검토) → {job}
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.deps import _no_running_job, _project
from app.core import guards
from app.core.exceptions import BadRequest, NotFound
from app.schemas.version import ReviewCreate
from app.services import jobs, output_store

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.post("/projects/review", status_code=202, summary="ERD 최종 확정 → 최종본 만들기 (작업)")
def create_review(body: ReviewCreate):
    project = _project(body.project)
    version = guards.version(body.version)
    design = output_store.read_design(project, version)
    if design is None:
        raise NotFound(f"버전 v{version}이 없습니다.")
    if not (design.get("data_model") or {}).get("entities"):
        raise BadRequest(f"v{version}에는 ERD(데이터 모델)가 없어 의미 검토를 할 수 없습니다.")
    _no_running_job(body.project)
    return {"job": jobs.start_review(output_dir=output_store.output_root(), project=body.project, version=version).to_dict()}
