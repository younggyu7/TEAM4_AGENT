"""최종 개발물 코드 관리 (git): 서버의 코드 폴더 하나를 로컬 저장소로 두고 브랜치·커밋·합치기 요청으로 고친다.

- 코드 폴더(OUTPUT_DIR/<프로젝트>/app)가 저장소이고 그 작업 폴더는 dev 브랜치(통합·테스트 배포)다. gen·dev·main은 화면에서 직접 고치거나 커밋하지 않는다.
  gen(AI 생성 결과)은 최종 개발하기만 커밋한다 (작성자 req-agent, 태그 final-v<최종본>). 생성기는 gen 작업 폴더(.preview/worktrees/gen)에 쓴다.
  처음 개발하면 dev를 gen과 같은 커밋으로 만든다(자동 병합). 다시 개발하면 gen → dev 합치기 요청을 자동으로 낸다 (개발자가 승인).
  dev가 바뀌는 길: ① 합치기 요청을 합침 (gen 또는 작업 브랜치) ② 화면 구성 저장.
  main(고객 납품)은 첫 '릴리스' 때 만들고, 그 뒤로도 릴리스로만 dev까지 앞으로 감긴다(태그 release-<날짜>-<n>).
  예전 저장소는 처음 열 때 main에서 dev를, dev에서 gen을 만든다.
- 합치기 요청을 낸 사람은 자기 요청을 합치거나 반려하지 않는다 (그 프로젝트의 개발자 담당자가 한 명이면 허용하고 '본인 승인'으로 기록).
- 브랜치마다 작업 폴더(git worktree)를 app/.preview/worktrees/<이름>에 둔다. 파일 고치기·저장하기(커밋)는 그 폴더에서 한다.
- 합치기 요청은 프로젝트 파일 repo.json에 둔다 (최신이 앞). 요청·합침·반려는 알림(services/directives.add_event)으로 알린다.
- 생성기 기록(.req-agent)·미리보기(.preview)·시연 계정(local)·패키지는 커밋하지 않는다 (.gitignore + .git/info/exclude).
- 원격 올리기는 services/gitpush.py (원격 하나, 고른 브랜치 dev·main과 태그). LLM은 쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from app.agents.storage import get_store
from app.core.auth import DEVELOPER, allowed
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound
from app.services.develop import PREVIEW, app_dir

MAIN = "main"       # 고객 납품 (첫 릴리스 때 생기고 릴리스로만 바뀜)
DEV = "dev"         # 통합·테스트 배포 (코드 폴더의 작업 브랜치)
GEN = "gen"         # AI 생성 결과 (최종 개발하기만 커밋)
PROTECTED = (MAIN, DEV, GEN)
AI_NAME, AI_EMAIL = "req-agent", "req-agent@req-agent.local"
REPO_FILE = "repo.json"
EXCLUDE = ".preview/\n.req-agent/\nlocal/\nnode_modules/\ndist/\ntarget/\n.env\n"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DIFF_BUDGET = 300_000      # 커밋·요청 차이 보기에 싣는 diff 글자 수 (넘으면 파일 목록만)
FILE_DIFF_MAX = 60_000     # 파일 하나의 diff 글자 수
_lock = threading.RLock()  # 저장소를 바꾸는 일은 한 번에 하나


class GitError(BadRequest):
    pass


class MergeConflict(Conflict):
    """합치기 충돌 (409): 충돌한 파일 목록을 같이 보낸다"""

    def __init__(self, detail: str, files: list[str]):
        super().__init__(detail)
        self.extra = {"conflicts": files}


def git(cwd: Path, *args: str, check: bool = True, who: tuple[str, str] | None = None, header: str = "") -> str:
    """git 명령 (사용자 전역 설정의 서명·한글 경로 따옴표를 끈다). check면 실패 시 GitError"""
    cmd = ["git", "-c", "core.quotepath=false", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
    if who:
        cmd += ["-c", f"user.name={who[0]}", "-c", f"user.email={who[1]}"]
    if header:
        cmd += ["-c", f"http.extraHeader={header}"]
    proc = subprocess.run([*cmd, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}, timeout=120)
    if check and proc.returncode != 0:
        raise GitError(f"git {args[0]} 실패: {(proc.stderr or proc.stdout).strip()[-400:]}")
    return proc.stdout if check else f"{proc.returncode}\n{proc.stdout}{proc.stderr}"


def _ok(cwd: Path, *args: str) -> bool:
    return git(cwd, *args, check=False).split("\n", 1)[0] == "0"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _who(member) -> dict:
    return {"id": member.id, "name": member.name}


# ---------------------------------------------------------------- 저장소 만들기 · 최종 개발하기 커밋

def ready(project: str) -> bool:
    return (app_dir(project) / ".git").exists()


def ensure_repo(project: str, first_commit: bool = True) -> Path:
    """코드 폴더를 저장소로 (처음이면 git init -b dev, first_commit이면 지금 파일을 req-agent 커밋으로). 코드가 없으면 400.
    이 기능 전에 개발한 프로젝트는 처음 볼 때 '가져옴' 커밋 하나로 시작한다"""
    app = app_dir(project)
    if not (app / "README.md").is_file():
        raise BadRequest("아직 개발한 코드가 없습니다. 산출물 보기에서 '최종 개발하기'를 먼저 하세요.")
    with _lock:
        if not (app / ".git").exists():
            _init(app, DEV)
        has = lambda b: _ok(app, "rev-parse", "--verify", "-q", f"refs/heads/{b}")   # noqa: E731
        if first_commit and not has(DEV) and not has(MAIN):
            git(app, "add", "-A")
            git(app, "commit", "-q", "--allow-empty", "-m", "최종 개발물 가져옴 (저장소 시작)", who=(AI_NAME, AI_EMAIL))
        _ensure_dev(app, has)
    return app


def _init(app: Path, branch: str) -> None:
    git(app, "init", "-q", "-b", branch)
    (app / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (app / ".git" / "info" / "exclude").write_text(EXCLUDE, encoding="utf-8")


def _ensure_dev(app: Path, has) -> None:
    """코드 폴더의 작업 브랜치를 dev로: 예전 저장소(main만)면 main에서 dev를, gen이 없으면 dev에서 gen을 만든다 (main은 만들지 않는다)"""
    if not has(DEV) and has(MAIN):
        git(app, "branch", DEV, MAIN)
    if has(DEV) and not has(GEN):
        git(app, "branch", GEN, DEV)
    current = git(app, "symbolic-ref", "-q", "--short", "HEAD", check=False).split("\n")
    if has(DEV) and (current[0] != "0" or current[1].strip() != DEV):
        git(app, "checkout", "-q", DEV)   # 같은 커밋이면 커밋 안 한 변경도 그대로 옮겨진다


def gen_tree(project: str) -> Path:
    """생성기가 쓰는 gen 작업 폴더 (없으면 만든다). 생성기 기록(.req-agent)이 코드 폴더에만 있으면(예전 프로젝트) 복사해 온다"""
    app = ensure_repo(project)
    path = worktree(project, GEN)
    if not (path / ".req-agent").exists() and (app / ".req-agent").is_dir():
        shutil.copytree(app / ".req-agent", path / ".req-agent")
    return path


def commit_develop(project: str, version: str, message: str, member) -> dict:
    """최종 개발하기 뒤: 생성 결과를 gen에 req-agent 커밋으로, 태그 final-v<최종본>을 그 커밋에 (다시 개발하면 옮김).
    처음(저장소가 없을 때: 생성기가 코드 폴더에 썼다)이면 dev를 gen과 같은 커밋으로 둔다 (자동 병합).
    다시 개발해 바뀐 것이 있으면 gen → dev 합치기 요청을 낸다 (요청한 사람 = 개발을 시작한 회원).
    → {commit: sha|None, first: bool, request: 요청 ID|None}"""
    app = app_dir(project)
    if not (app / ".git").exists():
        with _lock:
            _init(app, GEN)
            git(app, "add", "-A")
            git(app, "commit", "-q", "--allow-empty", "-m", message, who=(AI_NAME, AI_EMAIL))
            git(app, "branch", DEV, GEN)
            git(app, "checkout", "-q", DEV)
            git(app, "tag", "-f", f"final-v{version}", GEN)
            sha = git(app, "rev-parse", GEN).strip()
        gen_tree(project)
        return {"commit": sha, "first": True, "request": None}
    tree = gen_tree(project)
    with _lock:
        git(tree, "add", "-A")
        sha = None
        if not _ok(tree, "diff", "--cached", "--quiet"):
            git(tree, "commit", "-q", "-m", message, who=(AI_NAME, AI_EMAIL))
            sha = git(tree, "rev-parse", "HEAD").strip()
        git(app, "tag", "-f", f"final-v{version}", GEN)
    req = request_merge(project, GEN, message, member, generated=True) if sha else None
    return {"commit": sha, "first": False, "request": req["id"] if req else None}


def develop_message(version: str, ai: dict, member_name: str) -> str:
    """'최종본 v1.1.1 개발 · 기능 전체 AI · claude-haiku-5-5 · medium (개발: 한개발)'"""
    mode = ai.get("mode")
    how = "기능 전체 AI" if mode == "build" else "빈 로직만 AI" if mode == "logic" else "AI 안 씀"
    parts = [f"최종본 v{version} 개발", how] + ([p for p in (ai.get("model"), ai.get("effort")) if p] if mode else [])
    return " · ".join(parts) + f" (개발: {member_name})"


def final_version(project: str) -> str:
    """gen에서 가장 가까운 최종본 태그의 버전 (알림의 버전). 없으면 빈 값"""
    out = git(app_dir(project), "describe", "--tags", "--match", "final-v*", "--abbrev=0", GEN, check=False).split("\n")
    return out[1].strip().removeprefix("final-v") if out[0] == "0" and len(out) > 1 else ""


# ---------------------------------------------------------------- 브랜치 · 작업 폴더

def clean_name(name: str) -> str:
    """브랜치 이름 정리: 공백→-, 글자·숫자·. _ - /만, '..'·'//' 없이. git이 받지 않으면 400"""
    name = re.sub(r"\s+", "-", name.strip())
    name = re.sub(r"[^\w./-]", "", name)
    name = re.sub(r"\.{2,}", ".", re.sub(r"/{2,}", "/", name)).strip("/.-")
    if name.endswith(".lock"):
        name = name[:-5]
    if not name or name in (*PROTECTED, "HEAD") or len(name) > 100:
        raise BadRequest("브랜치 이름이 올바르지 않습니다 (예: feature/홍길동-로그인).")
    if not _ok(Path.cwd(), "check-ref-format", "--branch", name):
        raise BadRequest(f"git이 받지 않는 브랜치 이름입니다: {name}")
    return name


def _exists(app: Path, branch: str) -> bool:
    return _ok(app, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")


def _worktrees(app: Path) -> dict[str, Path]:
    """브랜치 → 작업 폴더 (git worktree list)"""
    out, path = {}, None
    for line in git(app, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[9:])
        elif line.startswith("branch refs/heads/") and path is not None:
            out[line[18:]] = path
    return out


def _slug(branch: str) -> str:
    return branch.replace("/", "__")


def worktree(project: str, branch: str | None = None) -> Path:
    """그 브랜치의 작업 폴더 (dev면 코드 폴더). 브랜치가 있는데 폴더가 없으면 다시 만든다 (main·gen은 사람이 고치지 않는 작업 폴더)"""
    app = app_dir(project)
    if not branch or branch == DEV:
        return app
    if not ready(project) or not _exists(app, branch):
        raise NotFound(f"브랜치가 없습니다: {branch}")
    with _lock:
        found = _worktrees(app).get(branch)
        if found is not None and found.is_dir():
            return found
        git(app, "worktree", "prune")
        path = app / PREVIEW / "worktrees" / _slug(branch)
        git(app, "worktree", "add", "-q", str(path), branch)
        return path


def changes(root: Path) -> dict[str, str]:
    """커밋하지 않은 파일 {경로: 'M'|'A'|'D'} (저장소가 아니면 빈 값)"""
    if not (root / ".git").exists():
        return {}
    out = {}
    entries = git(root, "status", "--porcelain", "-z", "-uall", "--no-renames").split("\0")
    for e in entries:
        if len(e) < 4:
            continue
        code, path = e[:2], e[3:]
        out[path] = "A" if code == "??" or "A" in code else "D" if "D" in code else "M"
    return out


def _counts(app: Path, base: str, head: str) -> tuple[int, int]:
    """(behind, ahead): base에만 있는 커밋 수, head에만 있는 커밋 수"""
    left, right = git(app, "rev-list", "--left-right", "--count", f"{base}...{head}").split()
    return int(left), int(right)


def branches(project: str) -> list[dict]:
    """dev·gen·main + 브랜치들: 마지막 커밋·작성자, dev보다 앞섬·뒤처짐(main은 릴리스할 dev 커밋 수가 behind), 커밋 안 한 변경 수, 열린 합치기 요청"""
    app = ensure_repo(project)
    data = _data(project)
    trees = _worktrees(app)
    out = []
    fmt = "%(refname:short)%00%(objectname)%00%(authorname)%00%(committerdate:iso-strict)%00%(subject)"
    for line in git(app, "for-each-ref", "refs/heads", f"--format={fmt}").splitlines():
        name, sha, author, date, subject = line.split("\0")
        behind, ahead = (0, 0) if name == DEV else _counts(app, DEV, name)
        tree = app if name == DEV else trees.get(name)
        req = next((r for r in data["requests"] if r["branch"] == name and r["status"] == "open"), None)
        out.append({"name": name, "head": sha, "author": author, "date": date, "subject": subject, "ahead": ahead, "behind": behind,
                    "changes": len(changes(tree)) if tree is not None and tree.is_dir() else 0, "protected": name in PROTECTED,
                    "owner": (data["branches"].get(name) or {}).get("owner"), "request": req["id"] if req else None})
    return sorted(out, key=lambda b: (b["name"] != DEV, b["name"] != GEN, b["name"] != MAIN, b["name"]))


def create_branch(project: str, name: str, member, base: str = DEV) -> dict:
    app = ensure_repo(project)
    name = clean_name(name)
    with _lock:
        if _exists(app, name):
            raise BadRequest(f"이미 있는 브랜치입니다: {name}")
        if not _exists(app, base):
            raise NotFound(f"브랜치가 없습니다: {base}")
        path = app / PREVIEW / "worktrees" / _slug(name)
        git(app, "worktree", "prune")
        git(app, "worktree", "add", "-q", "-b", name, str(path), base)
        data = _data(project)
        data["branches"][name] = {"owner": _who(member), "at": _now(), "from": base}
        _write(project, data)
    return next(b for b in branches(project) if b["name"] == name)


def delete_branch(project: str, name: str, member) -> None:
    """브랜치 지우기 (만든 사람 또는 개발자). 열린 합치기 요청이 있으면 거부"""
    app = ensure_repo(project)
    if name in PROTECTED or not _exists(app, name):
        raise NotFound(f"지울 수 있는 브랜치가 없습니다: {name}")
    data = _data(project)
    owner = (data["branches"].get(name) or {}).get("owner") or {}
    if not allowed(member.role, "develop") and owner.get("id") != member.id:
        raise Forbidden("브랜치는 만든 사람이나 개발자만 지웁니다.")
    if any(r["branch"] == name and r["status"] == "open" for r in data["requests"]):
        raise BadRequest("열린 합치기 요청이 있는 브랜치는 지우지 않습니다 (반려되거나 합친 뒤 지우세요).")
    with _lock:
        if (path := _worktrees(app).get(name)) is not None:
            git(app, "worktree", "remove", "--force", str(path))
        git(app, "branch", "-D", name)
        data["branches"].pop(name, None)
        _write(project, data)


# ---------------------------------------------------------------- 저장하기 (커밋)

def commit(project: str, branch: str, message: str, member, paths: list[str] | None = None) -> dict:
    """그 브랜치 작업 폴더의 변경을 로그인한 회원 이름으로 커밋한다 (dev·main·gen은 거부). paths가 있으면 그 파일만"""
    if not branch or branch in PROTECTED:
        raise BadRequest("dev·main·gen은 직접 고칠 수 없습니다 — 브랜치를 만들어 고치세요.")
    message = message.strip()
    if not message:
        raise BadRequest("저장 메시지를 적으세요 (무엇을 고쳤는지).")
    root = worktree(project, branch)
    with _lock:
        changed = changes(root)
        picked = [p for p in (paths or changed) if p in changed]
        if not picked:
            raise BadRequest("저장할 변경이 없습니다.")
        git(root, "add", "-A", "--", *picked)
        git(root, "commit", "-q", "-m", message[:2000], "--", *picked, who=(member.name, member.email))
        sha = git(root, "rev-parse", "HEAD").strip()
    return {"commit": sha, "files": picked, "branch": branch}


def update_branch(project: str, branch: str, member) -> dict:
    """dev 가져오기: dev를 그 브랜치에 합친다 (커밋 안 한 변경이 있으면 거부, 충돌이면 되돌리고 409).
    브랜치에 dev에 없는 커밋이 없으면 합치기 커밋 없이 dev로 앞으로 감는다"""
    if not branch or branch in PROTECTED:
        raise BadRequest("브랜치를 고르세요.")
    root = worktree(project, branch)
    with _lock:
        if changes(root):
            raise BadRequest("커밋하지 않은 변경이 있습니다. 먼저 '저장하기'를 하세요.")
        app = app_dir(project)
        behind, ahead = _counts(app, DEV, branch)
        if not behind:
            return {"merged": False, "message": "이미 dev의 최신 내용이 들어 있습니다."}
        if not ahead:
            git(root, "merge", "-q", "--ff-only", DEV)
            return {"merged": True, "message": f"dev의 커밋 {behind}개를 가져왔습니다.", "commit": git(root, "rev-parse", "HEAD").strip()}
        result = git(root, "merge", "--no-ff", "--no-edit", "-m", f"dev 가져오기: {DEV} → {branch}", DEV,
                     check=False, who=(member.name, member.email))
        if not result.startswith("0\n"):
            files = _conflicts(root)
            git(root, "merge", "--abort", check=False)
            raise MergeConflict(f"dev와 충돌해 가져오지 않았습니다: {', '.join(files) or '알 수 없음'}", files)
    return {"merged": True, "message": f"dev의 커밋 {behind}개를 가져왔습니다.", "commit": git(root, "rev-parse", "HEAD").strip()}


def _conflicts(root: Path) -> list[str]:
    return [p for p in git(root, "diff", "--name-only", "--diff-filter=U", "-z", check=False).split("\n", 1)[-1].split("\0") if p]


# ---------------------------------------------------------------- 합치기 요청 (repo.json)

def _data(project: str) -> dict:
    text = get_store().read_project_file(project, REPO_FILE)
    data = json.loads(text) if text else {}
    return {"requests": data.get("requests") or [], "branches": data.get("branches") or {}, "remote": data.get("remote") or {}}


def _write(project: str, data: dict) -> None:
    get_store().write_project_file(project, REPO_FILE, json.dumps(data, ensure_ascii=False))


def requests(project: str) -> list[dict]:
    return _data(project)["requests"]


def _request(data: dict, request_id: int) -> dict:
    found = next((r for r in data["requests"] if r["id"] == request_id), None)
    if found is None:
        raise NotFound("합치기 요청이 없습니다.")
    if found["status"] != "open":
        raise BadRequest("이미 처리한 합치기 요청입니다.")
    return found


def request_merge(project: str, branch: str, note: str, member, generated: bool = False) -> dict:
    """합치기 요청: dev보다 앞선 커밋이 있고 커밋 안 한 변경이 없어야 한다. 개발자에게 알림.
    generated: 다시 개발한 결과(gen → dev) — 최종 개발하기만 낸다. 열린 gen 요청이 있으면 새로 내지 않고 그 요청을 최신 gen으로 바꾼다"""
    from app.services import directives

    if not branch or (branch in PROTECTED and not (generated and branch == GEN)):
        raise BadRequest("dev·main·gen은 합치기 요청을 하지 않습니다.")
    root = worktree(project, branch)
    if changes(root):
        raise BadRequest("커밋하지 않은 변경이 있습니다. 먼저 '저장하기'를 하세요.")
    _, ahead = _counts(app_dir(project), DEV, branch)
    if not ahead:
        raise BadRequest("dev에 없는 커밋이 없습니다 (합칠 것이 없음).")
    with _lock:
        data = _data(project)
        head = git(root, "rev-parse", "HEAD").strip()
        req = next((r for r in data["requests"] if r["branch"] == branch and r["status"] == "open"), None)
        if req is not None and not generated:
            raise BadRequest("이 브랜치에는 이미 열린 합치기 요청이 있습니다.")
        if req is not None:
            req.update(note=note.strip()[:1000], by=_who(member), at=_now(), head=head, ahead=ahead, conflicts=[])
        else:
            req = {"id": max([r["id"] for r in data["requests"]] or [0]) + 1, "branch": branch, "note": note.strip()[:1000],
                   "by": _who(member), "at": _now(), "status": "open", "head": head, "ahead": ahead, "conflicts": []}
            data["requests"].insert(0, req)
        if generated:
            req["generated"] = True
        _write(project, data)
    title = f"AI 재생성 결과를 dev에 합치기 요청 ({member.name}님 개발)" if generated else f"{member.name}님의 합치기 요청 ({branch})"
    directives.add_event(project, "merge_request", title, req["note"] or f"커밋 {ahead}개", "develop", final_version(project), {DEVELOPER},
                         by=member.id)
    return req


def merge(project: str, request_id: int, member) -> dict:
    """개발자가 합친다: dev 작업 폴더(코드 폴더)에서 git merge --no-ff. 충돌이면 되돌리고 409(요청은 열린 채 conflicts 표시).
    요청한 사람은 자기 요청을 합치지 않는다 (개발자 담당자가 한 명이면 허용·'본인 승인' 기록)"""
    from app.services import directives, preview

    app = ensure_repo(project)
    with _lock:
        data = _data(project)
        req = _request(data, request_id)
        alone = check_reviewer(project, req, member)
        branch = req["branch"]
        if not _exists(app, branch):
            raise NotFound(f"브랜치가 없습니다: {branch}")
        if changes(app):
            raise Conflict("dev 작업 폴더에 커밋하지 않은 변경이 있어 합치지 않습니다 (서버 코드 폴더를 확인하세요).")
        message = f"합치기: {branch} (요청 {req['by']['name']}, 승인 {member.name}{' · 본인 승인' if alone else ''})"
        result = git(app, "merge", "--no-ff", "-m", message, branch, check=False, who=(member.name, member.email))
        if not result.startswith("0\n"):
            files = _conflicts(app)
            git(app, "merge", "--abort", check=False)
            req["conflicts"] = files
            _write(project, data)
            how = ("AI가 바꾼 파일을 사람도 dev에서 고쳤습니다. 반려한 뒤 그 파일은 작업 브랜치에서 사람이 맞추세요."
                   if branch == GEN else "요청한 사람이 'dev 가져오기'로 충돌을 풀고 다시 저장해야 합니다.")
            raise MergeConflict(f"dev와 충돌해 합치지 않았습니다: {', '.join(files) or '알 수 없음'}. {how}", files)
        req.update(status="merged", conflicts=[], commit=git(app, "rev-parse", "HEAD").strip(), done_by=_who(member), done_at=_now(),
                   self_approved=alone)
        _write(project, data)
    directives.add_event(project, "merged", f"{member.name}님이 합치기 요청을 반영했습니다 ({branch})", req["note"],
                         "develop", final_version(project), None, member_ids={req["by"]["id"]}, by=member.id)
    preview.mark_stale(project)
    return req


def reject(project: str, request_id: int, reason: str, member) -> dict:
    from app.services import directives

    if not reason.strip():
        raise BadRequest("반려 사유를 적으세요.")
    with _lock:
        data = _data(project)
        req = _request(data, request_id)
        check_reviewer(project, req, member)
        req.update(status="rejected", reason=reason.strip()[:1000], done_by=_who(member), done_at=_now())
        _write(project, data)
    directives.add_event(project, "merge_rejected", f"{member.name}님이 합치기 요청을 반려했습니다 ({req['branch']})",
                         f"사유: {req['reason']}", "develop", final_version(project), None, member_ids={req["by"]["id"]}, by=member.id)
    return req


def developers(project: str) -> list:
    """그 프로젝트의 개발자 담당자 (승인할 다른 사람이 있는지 볼 때)"""
    from app.services import project_access

    return [m for m in project_access.assignees(project) if allowed(m.role, "develop")]


def check_reviewer(project: str, req: dict, member) -> bool:
    """요청한 사람이 자기 요청을 승인·반려하면 403. 개발자 담당자가 그 사람 한 명뿐이면 허용하고 True(본인 승인)"""
    if (req.get("by") or {}).get("id") != member.id:
        return False
    if any(m.id != member.id for m in developers(project)):
        raise Forbidden("자기가 낸 요청은 다른 개발자가 승인·반려합니다 (개발자 담당자가 한 명이면 본인 승인 가능).")
    return True


# ---------------------------------------------------------------- 릴리스 (dev → main)

def pending_release(project: str) -> int:
    """릴리스할 dev 커밋 수 (main이 없으면 dev의 모든 커밋)"""
    app = app_dir(project)
    if not _exists(app, MAIN):
        return int(git(app, "rev-list", "--count", DEV).strip())
    return _counts(app, DEV, MAIN)[0]


def release(project: str, member) -> dict:
    """dev를 main으로 앞으로 감고(첫 릴리스면 main을 만든다) 태그 release-<날짜>-<n>을 단다. main에 dev에 없는 커밋이 있으면 400"""
    from app.services import directives

    app = ensure_repo(project)
    with _lock:
        dev = git(app, "rev-parse", DEV).strip()
        first = not _exists(app, MAIN)
        old = "" if first else git(app, "rev-parse", MAIN).strip()
        if old == dev:
            raise BadRequest("main이 이미 dev와 같습니다 (릴리스할 커밋 없음).")
        if not first and not _ok(app, "merge-base", "--is-ancestor", MAIN, DEV):
            raise BadRequest("main에 dev에 없는 커밋이 있어 릴리스하지 않습니다 (서버 저장소를 확인하세요).")
        if (path := _worktrees(app).get(MAIN)) is not None:   # 보기용 main 작업 폴더는 지우고 다음에 다시 만든다
            git(app, "worktree", "remove", "--force", str(path))
        count = int(git(app, "rev-list", "--count", dev if first else f"{old}..{dev}").strip())
        git(app, "update-ref", f"refs/heads/{MAIN}", dev, old or "0" * 40)
        day = datetime.now().strftime("%Y%m%d")
        n = 1 + sum(1 for t in git(app, "tag", "--list", f"release-{day}-*").split() if t)
        tag = f"release-{day}-{n}"
        git(app, "tag", tag, dev)
    directives.add_event(project, "released", f"{member.name}님이 릴리스했습니다 ({tag})", f"dev의 커밋 {count}개를 main에 반영",
                         "develop", final_version(project), {DEVELOPER}, by=member.id)
    return {"tag": tag, "commit": dev, "commits": count}


# ---------------------------------------------------------------- 이력 그래프 · 차이 보기

def log(project: str, limit: int = 80) -> list[dict]:
    """모든 브랜치의 커밋 (시간 순, 최신이 먼저): sha·부모·작성자·날짜·제목·ref(브랜치·태그·원격·HEAD)·ai(req-agent)"""
    app = ensure_repo(project)
    fmt = "%H%x00%P%x00%an%x00%ae%x00%aI%x00%s%x00%D%x1e"
    out = []
    for rec in git(app, "log", "--all", "--date-order", f"-n{max(1, min(limit, 500))}", f"--format={fmt}").split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, name, email, date, subject, refs = rec.split("\0")
        out.append({"sha": sha, "parents": parents.split(), "author": name, "email": email, "date": date, "subject": subject,
                    "refs": _refs(refs), "ai": name == AI_NAME})
    return out


def _refs(text: str) -> list[dict]:
    out = []
    for r in filter(None, (x.strip() for x in text.split(","))):
        if r.startswith("HEAD -> "):
            out += [{"type": "head", "name": "HEAD"}, {"type": "branch", "name": r[8:]}]
        elif r.startswith("tag: "):
            out.append({"type": "tag", "name": r[5:]})
        elif r.startswith("origin/"):
            out.append({"type": "remote", "name": r})
        elif r != "HEAD":
            out.append({"type": "branch", "name": r})
    return out


def _diff(cwd: Path, base: str, head: str) -> list[dict]:
    """base→head 바뀐 파일 [{path, status, diff, truncated}] (diff는 글자 수 한도 안에서만)"""
    files = []
    parts = git(cwd, "diff", "--name-status", "-z", "--no-renames", base, head).split("\0")
    for i in range(0, len(parts) - 1, 2):
        files.append({"status": parts[i][:1], "path": parts[i + 1], "diff": "", "truncated": False})
    budget = DIFF_BUDGET
    text = git(cwd, "diff", "--no-renames", "--no-color", base, head) if files else ""
    chunks = {}
    for chunk in re.split(r"(?m)^(?=diff --git )", text):
        if m := re.match(r"diff --git a/(.*?) b/", chunk):
            chunks[m.group(1)] = chunk
    for f in files:
        chunk = chunks.get(f["path"], "")
        if len(chunk) > FILE_DIFF_MAX or len(chunk) > budget:
            f["truncated"] = True
            chunk = chunk[: min(FILE_DIFF_MAX, max(budget, 0))]
        budget -= len(chunk)
        f["diff"] = chunk
    return files


def show(project: str, sha: str) -> dict:
    """커밋 하나: 정보 + 바뀐 파일과 diff (합친 커밋은 첫 부모와 비교, 첫 커밋은 빈 상태와)"""
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha or ""):
        raise BadRequest("커밋 sha가 올바르지 않습니다.")
    app = ensure_repo(project)
    if not _ok(app, "cat-file", "-e", f"{sha}^{{commit}}"):
        raise NotFound(f"커밋이 없습니다: {sha}")
    sha, parents, name, email, date, subject, body = git(
        app, "show", "-s", "--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%s%x00%b", sha).rstrip("\n").split("\0")
    parents_l = parents.split()
    return {"sha": sha, "parents": parents_l, "author": name, "email": email, "date": date, "subject": subject, "body": body.strip(),
            "ai": name == AI_NAME, "files": _diff(app, parents_l[0] if parents_l else EMPTY_TREE, sha)}


def branch_diff(project: str, branch: str) -> dict:
    """합치기 요청 차이 보기: dev...브랜치 (갈라진 뒤 브랜치에서 바뀐 것). main이면 릴리스할 차이(main...dev, 첫 릴리스 전이면 dev 전체)"""
    app = ensure_repo(project)
    if branch == MAIN and not _exists(app, MAIN):
        return {"branch": MAIN, "ahead": pending_release(project), "behind": 0, "files": _diff(app, EMPTY_TREE, DEV)}
    if branch == MAIN:
        base, head = git(app, "merge-base", MAIN, DEV).strip(), DEV
        behind, ahead = _counts(app, DEV, MAIN)
        return {"branch": MAIN, "ahead": behind, "behind": ahead, "files": _diff(app, base, head)}
    if branch == DEV or not _exists(app, branch):
        raise NotFound(f"브랜치가 없습니다: {branch}")
    base = git(app, "merge-base", DEV, branch).strip()
    behind, ahead = _counts(app, DEV, branch)
    return {"branch": branch, "ahead": ahead, "behind": behind, "files": _diff(app, base, branch)}


def overview(project: str) -> dict:
    """화면 오른쪽 저장소 카드: 브랜치·합치기 요청·최종본 버전·릴리스 대기 커밋 수 (main이 없으면 released=False)"""
    found = branches(project)
    return {"branches": found, "requests": requests(project)[:30], "version": final_version(project),
            "main": DEV, "gen": GEN, "release": MAIN, "released": any(b["name"] == MAIN for b in found),
            "pending_release": pending_release(project), "developers": len(developers(project))}
