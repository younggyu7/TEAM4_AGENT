"""수정 초안 공유와 팀장 결정.

  GET  /drafts               버전의 수정 초안 (파트원이 함께 고치는 편집·체크·답·지적 처리)
  POST /drafts               수정 초안 저장 (rev가 다르면 409: 다른 사람이 먼저 저장)
  POST /projects/approval    최종본 승인 / 수정 지시 (팀장)

초안은 산출물 저장소의 프로젝트 파일 draft-v<버전>.json에 둔다 (서비스 DB가 있으면 project_files 테이블).
기능 명세 버전의 초안은 기획 파트, ERD 버전의 초안은 데이터 파트가 고친다 (예전 전체 실행 버전은 둘 다).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents.storage import get_store
from app.agents.tools.versions import find_version, update_version
from app.api.v1.deps import _project, current_user, ensure
from app.core import guards
from app.core.auth import allowed, denied_message
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound
from app.services import output_store, users

router = APIRouter(tags=["projects"])
MAX_DRAFT = 4 * 1024 * 1024   # 초안 하나의 최대 크기 (JSON 글자 수)


class DraftSave(BaseModel):
    project: str
    version: str
    state: dict
    rev: int = 0               # 불러온 초안의 rev (처음이면 0)


class Approval(BaseModel):
    project: str
    version: str               # 최종본 (v1.1.1 …)
    decision: Literal["approve", "changes"]
    target: Literal["spec", "erd"] | None = None   # 수정 지시: 어느 단계로 돌려보낼지
    comment: str = Field("", max_length=2000)


def _name(version: str) -> str:
    return f"draft-v{version}.json"


def _record(project: str, version: str) -> dict:
    """버전 기록 (예전 폴더만 있는 버전은 기록이 없어 빈 값 = 전체 실행으로 본다)"""
    if not output_store.has_version(project, version):
        raise NotFound(f"v{version}이 없습니다.")
    return find_version(project, version) or {}


@router.get("/drafts", summary="버전의 수정 초안")
def get_draft(project: str, version: str, _: users.Member = Depends(current_user)):
    project, version = _project(project), guards.version(version)
    _record(project, version)
    text = get_store().read_project_file(project, _name(version))
    return json.loads(text) if text else {"state": None, "rev": 0}


@router.post("/drafts", summary="수정 초안 저장 (함께 고치기)")
def save_draft(body: DraftSave, member: users.Member = Depends(current_user)):
    project, version = _project(body.project), guards.version(body.version)
    stage = _record(project, version).get("stage")
    if stage == "final":
        raise BadRequest("최종본은 고치지 않습니다. 팀장이 '수정 지시'로 기능 명세·ERD 단계에 돌려보냅니다.")
    if stage == "data_modeler":
        ensure(member, "erd_edit")
    elif stage in (None, "full") and not allowed(member.role, "spec_edit"):
        ensure(member, "erd_edit")   # 예전 전체 실행 버전은 기능 명세와 ERD 초안이 함께 있어 두 파트 모두
    elif stage not in (None, "full"):
        ensure(member, "spec_edit")
    text = json.dumps(body.state, ensure_ascii=False)
    if len(text) > MAX_DRAFT:
        raise BadRequest("초안이 너무 큽니다.")
    store = get_store()
    old = store.read_project_file(project, _name(version))
    current = json.loads(old) if old else {"rev": 0}
    if current.get("rev", 0) != body.rev:
        by = (current.get("updated_by") or {}).get("name", "다른 사람")
        raise Conflict(f"{by}님이 먼저 저장했습니다 ({current.get('updated_at', '')}). 새로 불러온 내용에서 다시 고치세요.")
    saved = {"state": body.state, "rev": body.rev + 1, "updated_at": datetime.now().isoformat(timespec="seconds"),
             "updated_by": {"id": member.id, "name": member.name, "role": member.role}}
    store.write_project_file(project, _name(version), json.dumps(saved, ensure_ascii=False))
    return {"rev": saved["rev"], "updated_at": saved["updated_at"], "updated_by": saved["updated_by"]}


@router.post("/projects/approval", summary="최종본 승인 / 수정 지시 (팀장)")
def decide(body: Approval, member: users.Member = Depends(current_user)):
    if not allowed(member.role, "final_decide"):
        raise Forbidden(denied_message("final_decide"))
    project, version = _project(body.project), guards.version(body.version)
    record = _record(project, version)
    if record.get("stage") != "final":
        raise BadRequest("승인·수정 지시는 최종본(ERD 최종 확정으로 만든 버전)에만 합니다.")
    if body.decision == "changes" and (not body.target or not body.comment.strip()):
        raise BadRequest("수정 지시는 돌려보낼 단계(기능 명세·ERD)와 지시 내용을 적어야 합니다.")
    decision = {"decision": body.decision, "target": body.target if body.decision == "changes" else None,
                "comment": body.comment.strip(), "at": datetime.now().isoformat(timespec="seconds"),
                "by": {"id": member.id, "name": member.name, "role": member.role}}
    update_version(project, version, approval=decision, approvals=[*(record.get("approvals") or []), decision])
    return {"approval": decision}
