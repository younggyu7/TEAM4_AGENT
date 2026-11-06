"""최종 개발물 코드 관리 (git): 서버의 코드 폴더 하나를 로컬 저장소로 두고 브랜치·커밋·합치기 요청으로 고친다.

- 코드 폴더(OUTPUT_DIR/<프로젝트>/app)가 저장소이고 그 작업 폴더는 main 브랜치다. main은 화면에서 직접 고치거나 커밋하지 않는다.
  main이 바뀌는 길은 둘: ① 최종 개발하기(작성자 req-agent, 태그 final-v<최종본>) ② 개발자가 합치기 요청을 합침.
- 브랜치마다 작업 폴더(git worktree)를 app/.preview/worktrees/<이름>에 둔다. 파일 고치기·저장하기(커밋)는 그 폴더에서 한다.
- 합치기 요청은 프로젝트 파일 repo.json에 둔다 (최신이 앞). 요청·합침·반려는 알림(services/directives.add_event)으로 알린다.
- 생성기 기록(.req-agent)·미리보기(.preview)·시연 계정(local)·패키지는 커밋하지 않는다 (.gitignore + .git/info/exclude).
- 원격 올리기는 services/gitpush.py (원격 하나, main과 태그만). LLM은 쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from app.agents.storage import get_store
from app.core.auth import DEVELOPER, allowed
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound
from app.services.develop import PREVIEW, app_dir

MAIN = "main"
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
    """코드 폴더를 저장소로 (처음이면 git init -b main, first_commit이면 지금 파일을 req-agent 커밋으로). 코드가 없으면 400.
    이 기능 전에 개발한 프로젝트는 처음 볼 때 '가져옴' 커밋 하나로 시작한다"""
    app = app_dir(project)
    if not (app / "README.md").is_file():
        raise BadRequest("아직 개발한 코드가 없습니다. 산출물 보기에서 '최종 개발하기'를 먼저 하세요.")
    with _lock:
        if not (app / ".git").exists():
            git(app, "init", "-q", "-b", MAIN)
            (app / ".git" / "info").mkdir(parents=True, exist_ok=True)
            (app / ".git" / "info" / "exclude").write_text(EXCLUDE, encoding="utf-8")
        if first_commit and not _ok(app, "rev-parse", "--verify", "-q", f"refs/heads/{MAIN}"):
            git(app, "add", "-A")
            git(app, "commit", "-q", "--allow-empty", "-m", "최종 개발물 가져옴 (저장소 시작)", who=(AI_NAME, AI_EMAIL))
    return app


def commit_develop(project: str, version: str, message: str) -> str | None:
    """최종 개발하기 뒤: main의 바뀐 파일을 req-agent 커밋으로, 태그 final-v<최종본>을 그 커밋에 (다시 개발하면 옮김).
    바뀐 것이 없으면 커밋하지 않고 None"""
    app = ensure_repo(project, first_commit=False)
    with _lock:
        git(app, "add", "-A")
        sha = None
        if not _ok(app, "diff", "--cached", "--quiet"):
            git(app, "commit", "-q", "-m", message, who=(AI_NAME, AI_EMAIL))
            sha = git(app, "rev-parse", "HEAD").strip()
        git(app, "tag", "-f", f"final-v{version}", MAIN)
    return sha


def develop_message(version: str, ai: dict, member_name: str) -> str:
    """'최종본 v1.1.1 개발 · 기능 전체 AI · claude-haiku-5-5 · medium (개발: 한개발)'"""
    mode = ai.get("mode")
    how = "기능 전체 AI" if mode == "build" else "빈 로직만 AI" if mode == "logic" else "AI 안 씀"
    parts = [f"최종본 v{version} 개발", how] + ([p for p in (ai.get("model"), ai.get("effort")) if p] if mode else [])
    return " · ".join(parts) + f" (개발: {member_name})"


def final_version(project: str) -> str:
    """main에서 가장 가까운 최종본 태그의 버전 (알림의 버전). 없으면 빈 값"""
    out = git(app_dir(project), "describe", "--tags", "--match", "final-v*", "--abbrev=0", MAIN, check=False).split("\n")
    return out[1].strip().removeprefix("final-v") if out[0] == "0" and len(out) > 1 else ""


# ---------------------------------------------------------------- 브랜치 · 작업 폴더

def clean_name(name: str) -> str:
    """브랜치 이름 정리: 공백→-, 글자·숫자·. _ - /만, '..'·'//' 없이. git이 받지 않으면 400"""
    name = re.sub(r"\s+", "-", name.strip())
    name = re.sub(r"[^\w./-]", "", name)
    name = re.sub(r"\.{2,}", ".", re.sub(r"/{2,}", "/", name)).strip("/.-")
    if name.endswith(".lock"):
        name = name[:-5]
    if not name or name in (MAIN, "HEAD") or len(name) > 100:
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
    """그 브랜치의 작업 폴더 (main이면 코드 폴더). 브랜치가 있는데 폴더가 없으면 다시 만든다"""
    app = app_dir(project)
    if not branch or branch == MAIN:
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
    """main + 브랜치들: 마지막 커밋·작성자, main보다 앞섬·뒤처짐, 커밋 안 한 변경 수, 열린 합치기 요청"""
    app = ensure_repo(project)
    data = _data(project)
    trees = _worktrees(app)
    out = []
    fmt = "%(refname:short)%00%(objectname)%00%(authorname)%00%(committerdate:iso-strict)%00%(subject)"
    for line in git(app, "for-each-ref", "refs/heads", f"--format={fmt}").splitlines():
        name, sha, author, date, subject = line.split("\0")
        behind, ahead = (0, 0) if name == MAIN else _counts(app, MAIN, name)
        tree = app if name == MAIN else trees.get(name)
        req = next((r for r in data["requests"] if r["branch"] == name and r["status"] == "open"), None)
        out.append({"name": name, "head": sha, "author": author, "date": date, "subject": subject, "ahead": ahead, "behind": behind,
                    "changes": len(changes(tree)) if tree is not None and tree.is_dir() else 0, "protected": name == MAIN,
                    "owner": (data["branches"].get(name) or {}).get("owner"), "request": req["id"] if req else None})
    return sorted(out, key=lambda b: (b["name"] != MAIN, b["name"]))


def create_branch(project: str, name: str, member, base: str = MAIN) -> dict:
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
    if name == MAIN or not _exists(app, name):
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
    """그 브랜치 작업 폴더의 변경을 로그인한 회원 이름으로 커밋한다 (main은 거부). paths가 있으면 그 파일만"""
    if not branch or branch == MAIN:
        raise BadRequest("main은 직접 고칠 수 없습니다 — 브랜치를 만들어 고치세요.")
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
    """메인 가져오기: main을 그 브랜치에 합친다 (커밋 안 한 변경이 있으면 거부, 충돌이면 되돌리고 409)"""
    if not branch or branch == MAIN:
        raise BadRequest("브랜치를 고르세요.")
    root = worktree(project, branch)
    with _lock:
        if changes(root):
            raise BadRequest("커밋하지 않은 변경이 있습니다. 먼저 '저장하기'를 하세요.")
        app = app_dir(project)
        behind, _ = _counts(app, MAIN, branch)
        if not behind:
            return {"merged": False, "message": "이미 main의 최신 내용이 들어 있습니다."}
        result = git(root, "merge", "--no-ff", "--no-edit", "-m", f"메인 가져오기: {MAIN} → {branch}", MAIN,
                     check=False, who=(member.name, member.email))
        if not result.startswith("0\n"):
            files = _conflicts(root)
            git(root, "merge", "--abort", check=False)
            raise MergeConflict(f"main과 충돌해 가져오지 않았습니다: {', '.join(files) or '알 수 없음'}", files)
    return {"merged": True, "message": f"main의 커밋 {behind}개를 가져왔습니다.", "commit": git(root, "rev-parse", "HEAD").strip()}


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


def request_merge(project: str, branch: str, note: str, member) -> dict:
    """합치기 요청: main보다 앞선 커밋이 있고 커밋 안 한 변경이 없어야 한다. 개발자에게 알림"""
    from app.services import directives

    if not branch or branch == MAIN:
        raise BadRequest("main은 합치기 요청을 하지 않습니다.")
    root = worktree(project, branch)
    if changes(root):
        raise BadRequest("커밋하지 않은 변경이 있습니다. 먼저 '저장하기'를 하세요.")
    _, ahead = _counts(app_dir(project), MAIN, branch)
    if not ahead:
        raise BadRequest("main에 없는 커밋이 없습니다 (합칠 것이 없음).")
    with _lock:
        data = _data(project)
        if any(r["branch"] == branch and r["status"] == "open" for r in data["requests"]):
            raise BadRequest("이 브랜치에는 이미 열린 합치기 요청이 있습니다.")
        req = {"id": max([r["id"] for r in data["requests"]] or [0]) + 1, "branch": branch, "note": note.strip()[:1000],
               "by": _who(member), "at": _now(), "status": "open", "head": git(root, "rev-parse", "HEAD").strip(),
               "ahead": ahead, "conflicts": []}
        data["requests"].insert(0, req)
        _write(project, data)
    directives.add_event(project, "merge_request", f"{member.name}님의 합치기 요청 ({branch})", req["note"] or f"커밋 {ahead}개",
                         "develop", final_version(project), {DEVELOPER})
    return req


def merge(project: str, request_id: int, member) -> dict:
    """개발자가 합친다: main 작업 폴더에서 git merge --no-ff. 충돌이면 되돌리고 409(요청은 열린 채 conflicts 표시)"""
    from app.services import directives, preview

    app = ensure_repo(project)
    with _lock:
        data = _data(project)
        req = _request(data, request_id)
        branch = req["branch"]
        if not _exists(app, branch):
            raise NotFound(f"브랜치가 없습니다: {branch}")
        if changes(app):
            raise Conflict("main 작업 폴더에 커밋하지 않은 변경이 있어 합치지 않습니다 (서버 코드 폴더를 확인하세요).")
        message = f"합치기: {branch} (요청 {req['by']['name']}, 승인 {member.name})"
        result = git(app, "merge", "--no-ff", "-m", message, branch, check=False, who=(member.name, member.email))
        if not result.startswith("0\n"):
            files = _conflicts(app)
            git(app, "merge", "--abort", check=False)
            req["conflicts"] = files
            _write(project, data)
            raise MergeConflict(f"main과 충돌해 합치지 않았습니다: {', '.join(files) or '알 수 없음'}. "
                                "요청한 사람이 '메인 가져오기'로 충돌을 풀고 다시 저장해야 합니다.", files)
        req.update(status="merged", conflicts=[], commit=git(app, "rev-parse", "HEAD").strip(), done_by=_who(member), done_at=_now())
        _write(project, data)
    directives.add_event(project, "merged", f"{member.name}님이 합치기 요청을 반영했습니다 ({branch})", req["note"],
                         "develop", final_version(project), None, member_ids={req["by"]["id"]})
    preview.mark_stale(project)
    return req


def reject(project: str, request_id: int, reason: str, member) -> dict:
    from app.services import directives

    if not reason.strip():
        raise BadRequest("반려 사유를 적으세요.")
    with _lock:
        data = _data(project)
        req = _request(data, request_id)
        req.update(status="rejected", reason=reason.strip()[:1000], done_by=_who(member), done_at=_now())
        _write(project, data)
    directives.add_event(project, "merge_rejected", f"{member.name}님이 합치기 요청을 반려했습니다 ({req['branch']})",
                         f"사유: {req['reason']}", "develop", final_version(project), None, member_ids={req["by"]["id"]})
    return req


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
    """합치기 요청 차이 보기: main...브랜치 (갈라진 뒤 브랜치에서 바뀐 것)"""
    app = ensure_repo(project)
    if branch == MAIN or not _exists(app, branch):
        raise NotFound(f"브랜치가 없습니다: {branch}")
    base = git(app, "merge-base", MAIN, branch).strip()
    behind, ahead = _counts(app, MAIN, branch)
    return {"branch": branch, "ahead": ahead, "behind": behind, "files": _diff(app, base, branch)}


def overview(project: str) -> dict:
    """화면 오른쪽 저장소 카드: 브랜치·합치기 요청·최종본 버전"""
    return {"branches": branches(project), "requests": requests(project)[:30], "version": final_version(project), "main": MAIN}
