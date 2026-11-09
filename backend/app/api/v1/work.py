"""최종 개발물 화면 구성·작업 묶음·배정·제출·검토와 VS Code 확장용 API (services/layout.py · work.py).

  GET  /version                                   API 버전 {api} (확장이 접속할 때 맞는지 본다)
  GET  /develop/layout?project=                   화면 구성 {layout, menu, screens, path}
  PUT  /develop/layout                            화면 구성 저장 → dev 커밋 {project, layout} (개발자, 진행 중인 영역의 메뉴 이동은 400)
  POST /develop/layout/suggest                    업무 영역 다시 묶기 제안 {project, mode: rules|ai, layout?} → {layout, usage} (저장 안 함, 개발자)
  GET  /develop/work?project=                     작업 묶음·상태·진행 현황 {items, counts, people, developers}
  GET  /develop/work/item?project=&key=           작업 하나 + 작업 설명(TASK.md)
  POST /develop/work/brief                        작업 설명 고치기 {project, key, text, base} (개발자, 웹·확장 공유, 비우면 자동 설명)
  POST /develop/work/assign                       배정 {project, key, user_id} → 작업 브랜치·알림 (개발자)
  POST /develop/work/assign-many                  일괄 배정 {project, keys[], user_id} → 묶음별 결과 {results, assigned, failed} (개발자)
  POST /develop/work/submit                       제출 {project, key, note} → 합치기 요청 (담당자)
  POST /develop/work/approve                      승인 {project, key} → dev에 합침 (다른 개발자, 개발자 1명이면 본인)
  POST /develop/work/reject                       반려 {project, key, reason} (다른 개발자, 개발자 1명이면 본인)
  GET  /develop/work/mine                         내 작업·내가 검토할 작업 (볼 수 있는 프로젝트 전체, 확장 사이드바)
  GET  /develop/work/archive?project=&key=        작업 코드 받기: 최신 dev를 넣은 작업 브랜치 파일 전체(base64) + .task 파일 (담당자·개발자)
  POST /develop/work/upload                       바뀐 파일 올리기 {project, key, base, message, files[{path, content|null}], submit, note} (담당자)
  GET  /develop/work/review?project=&key=         검토 비교: 바뀐 파일의 dev 쪽·작업 쪽 내용
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.v1.deps import _no_running_job, _project, current_user, ensure
from app.services import layout, users, work

API_VERSION = 1
router = APIRouter(tags=["develop"])


class LayoutBody(BaseModel):
    project: str
    layout: dict


class SuggestBody(BaseModel):
    project: str
    mode: str = "rules"             # rules: 규칙(LLM 없음) · ai: LLM 한 번
    layout: dict | None = None      # 지금 화면에서 고치던 구성 (없으면 저장된 것)


class KeyBody(BaseModel):
    project: str
    key: str


class BriefBody(KeyBody):
    text: str = Field("", max_length=100_000)
    base: str | None = None   # 고치기 시작한 내용 (그 사이 다른 사람이 고쳤으면 409)


class AssignBody(KeyBody):
    user_id: int
    base: str = ""                  # 보낼 브랜치 (비우면 dev): 받을 때 이 브랜치의 서버 최신 내용, 승인하면 여기로 합침


class AssignManyBody(BaseModel):
    project: str
    keys: list[str]
    user_id: int
    base: str = ""


class NoteBody(KeyBody):
    note: str = ""


class ReasonBody(KeyBody):
    reason: str = ""


class FileChange(BaseModel):
    path: str
    content: str | None = None      # base64, null이면 지움


class UploadBody(KeyBody):
    base: str = ""                  # 받을 때의 작업 브랜치 커밋 (다르면 409)
    message: str = ""
    files: list[FileChange] = Field(default_factory=list)
    submit: bool = False
    note: str = ""


@router.get("/version", summary="API 버전 (확장 호환 확인)")
def version():
    return {"api": API_VERSION}


@router.get("/develop/layout", summary="화면 구성 보기")
def get_layout(project: str):
    return layout.get(_project(project))


@router.put("/develop/layout", summary="화면 구성 저장 (dev 커밋, 개발자)")
def put_layout(body: LayoutBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    _no_running_job(body.project)
    return layout.save(_project(body.project), body.layout, member)


@router.post("/develop/layout/suggest", summary="업무 영역 다시 묶기 제안 (저장 안 함, 개발자)")
def suggest_layout(body: SuggestBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    return layout.suggest(_project(body.project), body.mode, body.layout)


@router.get("/develop/work", summary="작업 묶음·진행 현황")
def get_work(project: str):
    return work.overview(_project(project))


@router.get("/develop/work/item", summary="작업 하나 + 작업 설명")
def get_item(project: str, key: str):
    project = _project(project)
    item = next((i for i in work.overview(project)["items"] if i["key"] == key), None)
    if item is None:
        from app.core.exceptions import NotFound
        raise NotFound(f"작업 묶음이 없습니다: {key}")
    return {"item": item, "brief": work.brief(project, key), "brief_edit": work.brief_edit(project, key), "rules": work.RULES}


@router.post("/develop/work/brief", summary="작업 설명 고치기 (개발자, 비우면 자동 설명)")
def save_brief(body: BriefBody, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    return work.save_brief(_project(body.project), body.key, body.text, body.base, member)


@router.post("/develop/work/assign", summary="담당자 배정 (개발자)")
def assign(body: AssignBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    return work.assign(_project(body.project), body.key, body.user_id, member, base=body.base or None)


@router.post("/develop/work/assign-many", summary="일괄 배정 (개발자)")
def assign_many(body: AssignManyBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    return work.assign_many(_project(body.project), body.keys, body.user_id, member, base=body.base or None)


@router.post("/develop/work/submit", summary="작업 제출 (담당자)")
def submit(body: NoteBody, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    return work.submit(_project(body.project), body.key, body.note, member)


@router.post("/develop/work/approve", summary="작업 승인 (다른 개발자)")
def approve(body: KeyBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    _no_running_job(body.project)
    return work.approve(_project(body.project), body.key, member)


@router.post("/develop/work/reject", summary="작업 반려 (다른 개발자)")
def reject(body: ReasonBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    return work.reject(_project(body.project), body.key, body.reason, member)


@router.get("/develop/work/mine", summary="내 작업·검토할 작업 (확장)")
def mine(member: users.Member = Depends(current_user)):
    return work.mine(member)


@router.get("/develop/work/archive", summary="작업 코드 받기 (확장)")
def archive(project: str, key: str, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    return work.archive(_project(project), key, member)


@router.post("/develop/work/upload", summary="바뀐 파일 올리기 (확장, 담당자)")
def upload(body: UploadBody, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    return work.upload(_project(body.project), body.key, body.base, body.message, [f.model_dump() for f in body.files], member,
                       body.submit, body.note)


@router.get("/develop/work/review", summary="검토 비교 (바뀐 파일의 dev·작업 내용)")
def review(project: str, key: str):
    return work.review_files(_project(project), key)
