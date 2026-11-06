"""ERD 최종 확정 (작업).

  POST /projects/review   ERD 최종 확정 → 최종본 v<ERD>.1 (추적·의미 검토) → {job}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1.deps import _no_running_job, _project, current_user, ensure
from app.core import guards
from app.core.exceptions import BadRequest, NotFound
from app.schemas.version import ReviewCreate
from app.services import directives, drafts, jobs, output_store, slack, users

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.post("/projects/review", status_code=202, summary="ERD 최종 확정 → 최종본 만들기 (작업)")
def create_review(body: ReviewCreate, member: users.Member = Depends(current_user)):
    ensure(member, "erd_confirm")
    project = _project(body.project)
    version = guards.version(body.version)
    design = output_store.read_design(project, version)
    if design is None:
        raise NotFound(f"버전 v{version}이 없습니다.")
    if not (design.get("data_model") or {}).get("entities"):
        raise BadRequest(f"v{version}에는 ERD(데이터 모델)가 없어 최종 확정할 수 없습니다.")
    _no_running_job(body.project)
    directives.ensure_unlocked(project, version)
    approved = drafts.note_event(project, version, member, "확정", "ERD 최종 확정 → 최종본 만들기")
    slack.request_decided(project, version, "erd", approved, member, approved=True)   # (예전) 승인 요청이 남아 있었으면 요청한 개발자에게 '승인'
    return {"job": jobs.start_review(output_dir=output_store.output_root(), project=body.project, version=version,
                                     dispositions=body.dispositions,
                                     on_done=lambda v: output_store.record_maker(project, v, member)).to_dict()}
