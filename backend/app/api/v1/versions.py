"""새 설계 버전 만들기 (작업).

  POST /projects/versions   기능 명세 내보내기·ERD 저장·확인 질문 답 반영 → {job}
  GET  /capabilities        백엔드 rerun이 기능 명세 수정본·추천안 확인 규칙을 받는지 {design, spec_md, confirm_rules}
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1.deps import _no_running_job, _project, current_user, ensure
from app.core import guards
from app.core.exceptions import NotFound
from app.schemas.version import VersionCreate
from app.services import drafts, jobs, output_store, users

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.post("/projects/versions", status_code=202, summary="새 설계 버전 만들기 (작업)")
def create_version(body: VersionCreate, member: users.Member = Depends(current_user)):
    # 기능 명세 내보내기(확정) = 기획 파트장·팀장, 기능 명세 새 버전(답·수정·의견) = 기획 파트, ERD 저장 = 데이터 파트
    ensure(member, "spec_confirm" if body.source == "spec" and not body.answers
           else "spec_edit" if body.source in ("spec", "answers") else "erd_edit")
    project = _project(body.project)
    base = guards.version(body.base, "base")
    if not output_store.has_version(project, base):
        raise NotFound(f"기준 버전 v{base}이 없습니다.")
    _no_running_job(body.project)
    if body.source == "spec" and not body.answers:   # 기능 명세 확정: 파트원의 승인 요청이 있어야 (팀장은 바로)
        drafts.require_request(project, base, member, "기능 명세를 확정(내보내기)")
        drafts.note_event(project, base, member, "확정", "기능 명세 확정 → ERD 만들기")
    title = (body.title or "").strip()
    job = jobs.start_rerun(
        output_dir=output_store.output_root(), project=body.project, base=base, source=body.source,
        data_model=body.data_model, design=body.design, spec_md=body.functions_md, spec_summary=body.spec_summary,
        answers=body.answers, confirm_rules=body.confirm_rules, dispositions=body.dispositions,
        functions=body.functions, feedback=body.feedback,
        on_done=lambda v: output_store.record_maker(project, v, member, title),
    )
    return {"job": job.to_dict()}


@router.get("/capabilities", summary="백엔드 rerun이 기능 명세 수정본을 받는지")
def get_capabilities():
    return jobs.capabilities()
