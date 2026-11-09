"""최종 개발하기·미리보기·git 올리기 (최종 승인한 최종본에서).

  GET  /develop/plan?project=&version=   화면·파일 수, 서버를 만드는지, 틀 없는 층 경고, AI 업무 기능 구현 예상 토큰 (LLM 없음)
  POST /develop                           최종 개발하기 → 코드(+AI 구현) + 예시 데이터 화면 + 자동 확인(Docker) → {job}  (개발자)
  POST /develop/check                     자동 확인 다시 하기 {project, version} → 지금 dev를 Docker로 켜고 조회 API 검사, 기록 갱신 → {job}  (개발자, LLM 없음)
  GET  /preview/{프로젝트}/                 미리보기 안내 페이지: 앱을 Docker로 켜고 준비되면 앱 주소(http://<서버>:<포트>)로 (로그인한 회원)
  GET  /preview/{프로젝트}/mock/…           예시 데이터 화면 (서버 없이)
  GET  /preview/{프로젝트}/accounts         앱 시연 계정 (admin·user 비밀번호, PM·개발자)
  GET  /preview/{프로젝트}/status · POST /preview/{프로젝트}/start · POST /preview/{프로젝트}/stop (끄기는 PM·개발자)
  GET·POST /develop/git?project=          원격 설정 하나 {url, branch, token} (토큰은 다시 보내지 않음, 개발자)
  POST /develop/git/push                  원격에 고른 브랜치(dev·main)와 태그 올리기 {target} → {job} (개발자)
  GET  /develop/files?project=&branch=    코드 파일 목록 (dev 또는 브랜치 작업 폴더, 빈 업무 로직·사람이 고침·커밋 안 한 변경 표시)
  GET·PUT /develop/file?project=&path=&branch=  파일 내용 보기 · 브랜치에서 고쳐 저장 (dev·main 거부, 개발자)
  브랜치·저장하기(커밋)·합치기 요청·이력은 api/v1/repo.py
  GET  /preview/{프로젝트}/terminal         실행 미리보기 터미널: 빌드·실행 기록 + 앱 서버 로그
"""
from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.api.v1.runs import llm_options
from app.core.config import EFFORTS
from app.api.v1.deps import _no_running_job, _project, current_user, ensure
from app.core import guards
from app.core.exceptions import BadRequest, NotFound
from app.services import appfiles, develop, gitpush, preview, users

router = APIRouter(tags=["develop"])


class DevelopBody(BaseModel):
    project: str
    version: str
    build: bool = False      # 업무 기능 전체를 AI가 구현 (--build, 토큰 사용)
    logic: bool = False      # 빈 업무 로직 자리만 AI가 채움 (--logic, 토큰 사용). build와 함께 켜지 않는다
    theme: str = ""          # 화면 스타일 id (tools/themes.py, 비우면 지난번·기본)
    model: str = ""          # build일 때 모델 (비우면 .env 기본)
    effort: str = ""         # build일 때 생각 수준 low·medium·high (비우면 설정 dev_effort)


class CheckBody(BaseModel):
    project: str
    version: str


class GitSettings(BaseModel):
    project: str
    url: str = ""
    branch: str = ""         # 비우면 main
    token: str = ""          # 비우면 저장된 토큰 그대로


class PushBody(BaseModel):
    project: str
    version: str
    target: str = "dev"      # 올릴 브랜치: dev(테스트 배포) · main(릴리스한 고객 납품본)


class FileBody(BaseModel):
    project: str
    path: str
    content: str
    branch: str = ""         # 고칠 브랜치 (main·빈 값은 거부)


@router.get("/develop/themes", summary="개발한 앱의 화면 스타일 선택지 (미리보기 색)")
def list_themes():
    from app.agents.tools import themes
    return {"themes": themes.choices(), "default": themes.DEFAULT}


@router.get("/develop/plan", summary="개발 전 예상 (화면·파일·AI 비용)")
def get_plan(project: str, version: str, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    project, version = _project(project), guards.version(version)
    develop.approved_final(project, version)
    return develop.plan(project, version)


@router.post("/develop", status_code=202, summary="최종 개발하기 (작업)")
def start(body: DevelopBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    project, version = _project(body.project), guards.version(body.version)
    develop.approved_final(project, version)
    if blocked := develop.plan(project, version)["blocked"]:   # 최종 개발하기를 하지 않는 스택 (예: Oracle)
        raise BadRequest(blocked)
    if body.build and body.logic:
        raise BadRequest("AI 방식은 하나만 고릅니다 (기능 전체 AI 또는 빈 로직만 AI).")
    from app.agents.tools import themes
    if body.theme and body.theme not in themes.BY_ID:
        raise BadRequest(f"없는 화면 스타일입니다: {body.theme}")
    if body.effort and body.effort not in EFFORTS:
        raise BadRequest(f"effort는 {', '.join(EFFORTS)} 중 하나입니다.")
    if body.model and body.model not in {m["id"] for m in llm_options()["models"]}:
        raise BadRequest(f"고를 수 없는 모델입니다: {body.model}")
    _no_running_job(body.project)
    return {"job": develop.start_develop(project=project, version=version, build=body.build, logic=body.logic, theme=body.theme,
                                              model=body.model, effort=body.effort, member=member).to_dict()}


@router.post("/develop/check", status_code=202, summary="자동 확인 다시 하기 (작업)")
def recheck(body: CheckBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    project, version = _project(body.project), guards.version(body.version)
    develop.approved_final(project, version)
    _no_running_job(body.project)
    return {"job": develop.start_check(project=project, version=version).to_dict()}


def _inside(root: Path, rest: str) -> Path:
    """빌드 폴더 안의 파일 (밖을 가리키면 404). 없는 경로는 index.html (화면은 해시 주소라 거의 없음)"""
    root = root.resolve()
    target = (root / (rest or "index.html")).resolve()
    if root not in target.parents and target != root:
        raise NotFound("파일이 없습니다.")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        target = root / "index.html"
    if not target.is_file():
        raise NotFound("미리보기 화면이 아직 없습니다. '최종 개발하기'를 먼저 하세요.")
    return target


@router.get("/preview/{project}/mock/{rest:path}", summary="예시 데이터 화면", include_in_schema=False)
def mock_file(project: str, rest: str = ""):
    project = _project(project)
    return FileResponse(_inside(develop.app_dir(project) / develop.PREVIEW / "mock", rest))


@router.get("/preview/{project}/status", summary="실행 미리보기 상태")
def run_status(project: str):
    return preview.status(_project(project))


@router.post("/preview/{project}/start", summary="실행 미리보기 켜기")
def run_start(project: str):
    return preview.start(_project(project))


@router.post("/preview/{project}/stop", summary="실행 미리보기 끄기 (PM·개발자)")
def run_stop(project: str, member: users.Member = Depends(current_user)):
    ensure(member, "preview_stop")
    return preview.stop(_project(project), f"{member.name}님이 껐습니다.")


@router.get("/preview/{project}/accounts", summary="앱 시연 계정 (PM·개발자)")
def run_accounts(project: str, member: users.Member = Depends(current_user)):
    ensure(member, "app_accounts")
    return {"accounts": preview.accounts(_project(project))}


@router.get("/preview/{project}/terminal", summary="실행 미리보기 터미널 (빌드·실행 기록 + 앱 서버 로그)")
def run_terminal(project: str):
    return preview.terminal(_project(project))


@router.get("/preview/{project}/", response_class=HTMLResponse, summary="미리보기 안내 페이지", include_in_schema=False)
def landing(project: str):
    """실행 미리보기를 켜고(백엔드가 있으면) 진행을 보여 주다가 준비되면 앱 화면으로 넘긴다. 백엔드가 없으면 예시 데이터 화면"""
    project = _project(project)
    name = html.escape(project)
    return HTMLResponse(LANDING.replace("{{name}}", name))


LANDING = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{name}} · 최종 개발물 미리보기</title>
<style>body{font-family:system-ui,-apple-system,'Apple SD Gothic Neo',sans-serif;max-width:720px;margin:48px auto;padding:0 16px;color:#111}
h1{font-size:20px}p{color:#444}.bar{height:6px;background:#eee;border-radius:3px;overflow:hidden;margin:16px 0}.bar i{display:block;height:100%;width:30%;background:#111;animation:m 1.2s infinite ease-in-out}
@keyframes m{0%{margin-left:-30%}100%{margin-left:100%}}pre{background:#f5f5f5;padding:12px;border-radius:6px;font-size:12px;max-height:260px;overflow:auto;white-space:pre-wrap}
a.btn{display:inline-block;margin-right:8px;padding:8px 12px;border:1px solid #111;border-radius:6px;color:#111;text-decoration:none}</style></head>
<body><h1>{{name}} · 최종 개발물 미리보기</h1><p id="msg">상태를 확인하는 중…</p><div class="bar" id="bar"><i></i></div>
<p><a class="btn" href="./mock/">예시 데이터 화면 바로 보기 (서버 없이)</a></p><pre id="log" hidden></pre>
<script>
const msg=document.getElementById('msg'),bar=document.getElementById('bar'),logEl=document.getElementById('log');
async function call(p,m){const r=await fetch(p,{method:m||'GET',credentials:'include'});if(r.status===401){msg.textContent='로그인이 필요합니다. 설계 도구에서 로그인한 뒤 다시 여세요.';throw 0}return r.json()}
const appUrl=p=>location.protocol+'//'+location.hostname+':'+p+'/';
function show(s){msg.textContent=s.message||s.status;if(s.log&&s.log.length){logEl.hidden=false;logEl.textContent=s.log.join('\\n');logEl.scrollTop=1e9}}
async function go(){let s=await call('./status');
 if(!s.backend){msg.textContent='서버 코드가 없어(서버 틀이 없는 기술 스택) 예시 데이터 화면을 엽니다.';bar.hidden=true;setTimeout(()=>location.href='./mock/',1200);return}
 if(s.status!=='running'||s.stale){s=await call('./start','POST')}
 while(s.status==='building'||s.status==='starting'){show(s);await new Promise(r=>setTimeout(r,2000));s=await call('./status')}
 if(s.status==='running'){msg.textContent='실행 중 — 앱을 엽니다 (로그인 화면).';location.href=s.url||appUrl(s.port);return}
 bar.hidden=true;show(s)}
go().catch(()=>{bar.hidden=true})
</script></body></html>"""


@router.get("/develop/git", summary="원격 설정과 앞섬·뒤처짐 (토큰은 있는지만, fetch 안 함)")
def get_git(project: str):
    return gitpush.remote_status(_project(project))


@router.post("/develop/git", summary="git 저장소 설정 저장")
def set_git(body: GitSettings, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    return gitpush.save_settings(_project(body.project), body.model_dump())


@router.post("/develop/git/push", status_code=202, summary="원격에 올리기 (작업)")
def push(body: PushBody, member: users.Member = Depends(current_user)):
    ensure(member, "develop")
    project, version = _project(body.project), guards.version(body.version)
    develop.approved_final(project, version)
    _no_running_job(body.project)
    return {"job": gitpush.start_push(project=project, version=version, member=member, target=body.target).to_dict()}


@router.get("/develop/files", summary="최종 개발물 파일 목록 (main 또는 브랜치)")
def list_files(project: str, branch: str = ""):
    return appfiles.tree(_project(project), branch or None)


@router.get("/develop/file", summary="최종 개발물 파일 내용")
def get_file(project: str, path: str, branch: str = ""):
    return appfiles.read(_project(project), path, branch or None)


@router.put("/develop/file", summary="브랜치에서 파일 고쳐 저장 (개발자, main 거부)")
def put_file(body: FileBody, member: users.Member = Depends(current_user)):
    ensure(member, "repo_write")
    return appfiles.save(_project(body.project), body.path, body.content, body.branch or None)
