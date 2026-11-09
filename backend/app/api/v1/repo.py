"""최종 개발물 코드 관리 (git, services/repo.py). 보기는 로그인한 회원 누구나.

  GET  /develop/repo?project=                       브랜치(앞섬·뒤처짐·커밋 안 한 변경·열린 요청)·합치기 요청·최종본 버전
  GET  /develop/repo/log?project=&limit=80          모든 브랜치의 커밋 (이력 그래프)
  GET  /develop/repo/commit?project=&sha=           커밋 하나의 바뀐 파일과 diff
  GET  /develop/repo/diff?project=&branch=          dev...브랜치 차이 (합치기 요청 차이 보기, main이면 릴리스할 차이)
  POST /develop/repo/branch                         브랜치 만들기 {project, name, from} (개발자)
  DELETE /develop/repo/branch?project=&name=        브랜치 지우기 (만든 사람·개발자, 열린 요청 있으면 거부)
  POST /develop/repo/commit                         저장하기 {project, branch, message, paths?} (개발자, dev·main 거부)
  POST /develop/repo/update-branch                  dev 가져오기 {project, branch} (충돌이면 409 + conflicts)
  POST /develop/repo/request                        합치기 요청 {project, branch, note} → 개발자 알림
  POST /develop/repo/merge                          합치기 {project, request_id} (개발자, 충돌이면 409 + conflicts)
  POST /develop/repo/reject                         반려 {project, request_id, reason} (개발자)
  POST /develop/repo/release                        릴리스: dev를 main으로 앞으로 감고 태그 release-<날짜>-<n> (개발자)
  POST /develop/repo/fetch                          원격 확인: 원격 브랜치를 받아 앞섬·뒤처짐 (개발자)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.v1.deps import _no_running_job, _project, current_user, ensure
from app.services import gitpush, repo, users

router = APIRouter(tags=["develop"])


class BranchBody(BaseModel):
    project: str
    name: str
    from_: str = Field(repo.DEV, alias="from")


class CommitBody(BaseModel):
    project: str
    branch: str
    message: str
    paths: list[str] | None = None


class BranchOnly(BaseModel):
    project: str
    branch: str


class RequestBody(BaseModel):
    project: str
    branch: str
    note: str = ""


class DecideBody(BaseModel):
    project: str
    request_id: int
    reason: str = ""


class ProjectBody(BaseModel):
    project: str


@router.get("/develop/repo", summary="코드 저장소 브랜치·합치기 요청")
def overview(project: str):
    return repo.overview(_project(project))


@router.get("/develop/repo/log", summary="모든 브랜치의 커밋 (이력 그래프)")
def get_log(project: str, limit: int = 80):
    return {"commits": repo.log(_project(project), limit)}


@router.get("/develop/repo/commit", summary="커밋 하나의 바뀐 파일과 diff")
def get_commit(project: str, sha: str):
    return repo.show(_project(project), sha)


@router.get("/develop/repo/diff", summary="main...브랜치 차이")
def get_diff(project: str, branch: str):
    return repo.branch_diff(_project(project), branch)


@router.post("/develop/repo/branch", summary="브랜치 만들기 (개발자)")
def create_branch(body: BranchBody, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    return repo.create_branch(_project(body.project), body.name, member, body.from_ or repo.DEV)


@router.delete("/develop/repo/branch", summary="브랜치 지우기 (만든 사람·개발자)")
def delete_branch(project: str, name: str, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    repo.delete_branch(_project(project), name, member)
    return {"ok": True}


@router.post("/develop/repo/commit", summary="저장하기: 브랜치의 변경을 커밋 (개발자)")
def commit(body: CommitBody, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    return repo.commit(_project(body.project), body.branch, body.message, member, body.paths)


@router.post("/develop/repo/update-branch", summary="dev 가져오기: dev를 브랜치에 합침")
def update_branch(body: BranchOnly, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    from app.services import work
    return repo.update_branch(_project(body.project), body.branch, member, work.base_of(_project(body.project), body.branch))


@router.post("/develop/repo/request", summary="합치기 요청 (개발자)")
def request_merge(body: RequestBody, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    from app.services import work
    return repo.request_merge(_project(body.project), body.branch, body.note, member, work.base_of(_project(body.project), body.branch))


@router.post("/develop/repo/merge", summary="합치기 (개발자)")
def merge(body: DecideBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    _no_running_job(body.project)   # 다시 개발·올리는 중에는 main을 바꾸지 않는다
    return repo.merge(_project(body.project), body.request_id, member)


@router.post("/develop/repo/reject", summary="합치기 요청 반려 (개발자)")
def reject(body: DecideBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    return repo.reject(_project(body.project), body.request_id, body.reason, member)


@router.post("/develop/repo/release", summary="릴리스: dev → main + 태그 (개발자)")
def release(body: ProjectBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    _no_running_job(body.project)
    return repo.release(_project(body.project), member)


@router.post("/develop/repo/fetch", summary="원격 확인 (개발자)")
def fetch(body: ProjectBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    return gitpush.check_remote(_project(body.project))
