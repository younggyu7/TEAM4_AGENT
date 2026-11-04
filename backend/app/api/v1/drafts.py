"""수정 초안 공유와 팀장 결정.

  GET  /drafts               버전의 수정 초안 (편집·체크·답·지적 처리) + 변경 내역(누가·언제·무엇을) + 승인 요청
  POST /drafts               수정 초안 저장 (rev가 다르면 409: 다른 사람이 먼저 저장)
  POST /drafts/request       파트원의 수정 완료 승인 요청 (파트장의 확정은 이 요청이 있어야 한다)
  POST /projects/approval    최종본 승인 / 수정 지시 (팀장)
  GET  /projects/directives  끝나지 않은 수정 지시와 진행 (어느 버전에서 처리할지, 옛 ERD 잠금, 항목 처리)
  POST /projects/directives/resolve  지시 항목 처리: 반영함 / 그대로 둠(이유) — LLM 없음
  GET  /notices              나에게 온 알림 (역할별 수정 지시·승인 요청·승인 대기·개발 대기 + 업로드 분석 완료·ERD 초안·개발 초안)
  POST /notices/dismiss      알림 확인 (나에게서만 지운다)

초안은 산출물 저장소의 프로젝트 파일 draft-v<버전>.json에 둔다 (서비스 DB가 있으면 project_files 테이블, 형식은
services/drafts.py).
기능 명세 버전의 초안은 기획 파트, ERD 버전의 초안은 데이터 파트가 고친다 (예전 전체 실행 버전은 둘 다).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agents.tools.versions import find_version, update_version
from app.api.v1.deps import _project, current_user, ensure
from app.core import guards
from app.core.auth import allowed, denied_message
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound
from app.services import directives, drafts, output_store, users

router = APIRouter(tags=["projects"])
MAX_DRAFT = 4 * 1024 * 1024   # 초안 하나의 최대 크기 (JSON 글자 수)


class DraftSave(BaseModel):
    project: str
    version: str
    state: dict
    rev: int = 0               # 불러온 초안의 rev (처음이면 0)


class ReviewRequest(BaseModel):
    project: str
    version: str
    note: str = Field("", max_length=500)


class ChangeItem(BaseModel):
    """수정 지시에서 고른 항목: 어느 단계의 어느 기능·테이블을 어떻게 고칠지"""
    stage: Literal["spec", "erd"]
    target_id: str = Field(max_length=200)     # 기능 ID 또는 테이블 ID
    label: str = Field("", max_length=200)
    note: str = Field("", max_length=1000)


class Resolve(BaseModel):
    project: str
    final: str                 # 수정 지시를 받은 최종본
    stage: Literal["spec", "erd"]
    target_id: str
    choice: Literal["applied", "kept", ""]   # 빈 값 = 처리 취소
    note: str = Field("", max_length=500)


class Approval(BaseModel):
    project: str
    version: str               # 최종본 (v1.1.1 …)
    decision: Literal["approve", "changes"]
    target: Literal["spec", "erd"] | None = None   # 예전 화면: 돌려보낼 단계 하나
    targets: list[Literal["spec", "erd"]] = []     # 수정 지시: 돌려보낼 단계 (둘 다 가능)
    items: list[ChangeItem] = Field(default_factory=list, max_length=200)   # 고칠 기능·테이블과 항목별 메모
    comment: str = Field("", max_length=2000)


def _record(project: str, version: str) -> dict:
    """버전 기록 (예전 폴더만 있는 버전은 기록이 없어 빈 값 = 전체 실행으로 본다)"""
    if not output_store.has_version(project, version):
        raise NotFound(f"v{version}이 없습니다.")
    return find_version(project, version) or {}


@router.get("/drafts", summary="버전의 수정 초안·변경 내역·승인 요청")
def get_draft(project: str, version: str, _: users.Member = Depends(current_user)):
    project, version = _project(project), guards.version(version)
    _record(project, version)
    return drafts.read(project, version)


def _can_edit(project: str, version: str, member: users.Member) -> None:
    """그 버전 단계를 고칠 권한: 기능 명세 버전은 기획 파트, ERD 버전은 데이터 파트 (예전 전체 실행 버전은 둘 다)"""
    stage = _record(project, version).get("stage")
    if stage == "final":
        raise BadRequest("최종본은 고치지 않습니다. 팀장이 '수정 지시'로 기능 명세·ERD 단계에 돌려보냅니다.")
    if stage == "data_modeler":
        ensure(member, "erd_edit")
        directives.ensure_unlocked(project, version)   # 기능 명세 수정 지시가 진행 중인 옛 ERD
    elif stage in (None, "full") and not allowed(member.role, "spec_edit"):
        ensure(member, "erd_edit")   # 예전 전체 실행 버전은 기능 명세와 ERD 초안이 함께 있어 두 파트 모두
    elif stage not in (None, "full"):
        ensure(member, "spec_edit")


@router.post("/drafts", summary="수정 초안 저장 (함께 고치기, 변경 내역을 남김)")
def save_draft(body: DraftSave, member: users.Member = Depends(current_user)):
    project, version = _project(body.project), guards.version(body.version)
    _can_edit(project, version, member)
    if len(str(body.state)) > MAX_DRAFT:
        raise BadRequest("초안이 너무 큽니다.")
    doc = drafts.save(project, version, body.state, body.rev, member)
    if doc is None:
        current = drafts.read(project, version)
        by = (current.get("updated_by") or {}).get("name", "다른 사람")
        raise Conflict(f"{by}님이 먼저 저장했습니다 ({current.get('updated_at', '')}). 새로 불러온 내용에서 다시 고치세요.")
    return {"rev": doc["rev"], "updated_at": doc["updated_at"], "updated_by": doc["updated_by"],
            "log": doc["log"], "request": doc["request"]}


@router.post("/drafts/request", summary="수정 완료 승인 요청 (파트원 → 파트장)")
def request_review(body: ReviewRequest, member: users.Member = Depends(current_user)):
    project, version = _project(body.project), guards.version(body.version)
    _can_edit(project, version, member)
    return {"request": drafts.request(project, version, member, body.note), "log": drafts.read(project, version)["log"]}


@router.post("/projects/approval", summary="최종본 승인 / 수정 지시 (팀장)")
def decide(body: Approval, member: users.Member = Depends(current_user)):
    if not allowed(member.role, "final_decide"):
        raise Forbidden(denied_message("final_decide"))
    project, version = _project(body.project), guards.version(body.version)
    record = _record(project, version)
    if record.get("stage") != "final":
        raise BadRequest("승인·수정 지시는 최종본(ERD 최종 확정으로 만든 버전)에만 합니다.")
    if body.decision == "approve" and (record.get("approval") or {}).get("decision") == "approve":
        raise BadRequest("이미 최종 승인한 최종본입니다.")
    changes = body.decision == "changes"
    items = [i for i in body.items if i.target_id.strip()] if changes else []
    # 돌려보낼 단계: 고른 단계 + 고른 항목의 단계 (화면이 단계를 따로 안 골라도 항목으로 정해진다)
    targets = list(dict.fromkeys([*body.targets, *([body.target] if body.target else []), *(i.stage for i in items)])) if changes else []
    if changes and (not targets or not (body.comment.strip() or any(i.note.strip() for i in items))):
        raise BadRequest("수정 지시는 돌려보낼 단계(기능 명세·ERD)와 지시 내용(전체 의견 또는 항목별 메모)을 적어야 합니다.")
    decision = {"decision": body.decision, "targets": targets, "target": targets[0] if len(targets) == 1 else None,
                "items": [i.model_dump() for i in items], "comment": body.comment.strip(),
                "at": datetime.now().isoformat(timespec="seconds"),
                "by": {"id": member.id, "name": member.name, "role": member.role}}
    update_version(project, version, approval=decision, approvals=[*(record.get("approvals") or []), decision])
    return {"approval": decision}


@router.get("/projects/directives", summary="끝나지 않은 수정 지시와 진행")
def list_directives(project: str, _: users.Member = Depends(current_user)):
    return {"directives": directives.open_directives(_project(project))}


@router.post("/projects/directives/resolve", summary="수정 지시 항목 처리 (반영함 / 그대로 둠)")
def resolve_directive(body: Resolve, member: users.Member = Depends(current_user)):
    project = _project(body.project)
    return {"resolution": directives.resolve(project, guards.version(body.final), body.stage, body.target_id,
                                             body.choice, body.note, member)}


@router.get("/notices", summary="나에게 온 알림")
def get_notices(member: users.Member = Depends(current_user)):
    return {"notices": directives.notices(member)}


class DismissBody(BaseModel):
    project: str
    id: str


@router.post("/notices/dismiss", summary="알림 확인 (나에게서만 지운다)")
def dismiss_notice(body: DismissBody, member: users.Member = Depends(current_user)):
    directives.dismiss(_project(body.project), body.id[:200], member)
    return {"ok": True}
