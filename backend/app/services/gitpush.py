"""최종 개발물 git 올리기: 서버의 코드 폴더(OUTPUT_DIR/<프로젝트>/app)를 화면·백엔드 저장소에 따로 커밋·push한다.

- 저장소 설정은 프로젝트마다 (프로젝트 파일 git.json): 화면·백엔드 각각 {url, branch, token}. 처음 올릴 때 화면에서 받는다.
  토큰은 암호화해 저장하고(Fernet, 키는 .env GIT_SECRET_KEY, 없으면 OUTPUT_DIR/.git-secret를 만들어 씀) 화면에는 다시 보내지 않는다.
- 올리는 내용
    백엔드 저장소: backend/ · db/ · spec/(개발 스펙 전부) · README.md · docker-compose.yml · .gitignore
    화면 저장소:   frontend/ · spec/SCREENS.md · spec/API.md · spec/openapi.yaml · .gitignore
  빌드 결과·node_modules·미리보기(.preview)·생성기 기록(.req-agent)은 올리지 않는다.
- 저장소 작업 폴더는 app/.preview/git-<쪽>. 브랜치가 원격에 있으면 받아서 그 위에 커밋한다 (이력 유지, 강제 push 없음).
- 토큰은 명령줄·기록에 남지 않게 http.extraHeader로만 넘기고, 기록의 토큰 글자는 가린다.
- 올리는 일은 정해진 절차(코드)이고 LLM은 쓰지 않는다.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from app.agents.storage import get_store
from app.agents.tools.versions import find_version, update_version
from app.core.config import get_settings
from app.core.exceptions import BadRequest
from app.services.develop import PREVIEW, app_dir
from app.services.jobs.common import Job, _new_job, _Runner

SIDES = {"frontend": "화면", "backend": "백엔드"}
CONTENT = {
    "backend": ["backend", "db", "spec", "README.md", "docker-compose.yml", ".gitignore"],
    "frontend": ["frontend", "spec/SCREENS.md", "spec/API.md", "spec/openapi.yaml", ".gitignore"],
}
SKIP = {"node_modules", "dist", "target", ".env", PREVIEW, ".req-agent", ".git", "local"}   # local: 앱 시연 계정 비밀번호
SETTINGS = "git.json"


def _fernet():
    from cryptography.fernet import Fernet
    key = os.getenv("GIT_SECRET_KEY", "").strip()
    if not key:
        path = Path(get_settings().output_dir) / ".git-secret"
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(Fernet.generate_key().decode(), encoding="utf-8")
            path.chmod(0o600)
        key = path.read_text(encoding="utf-8").strip()
    return Fernet(key.encode())


def _read(project: str) -> dict:
    text = get_store().read_project_file(project, SETTINGS)
    return json.loads(text) if text else {}


def settings(project: str) -> dict:
    """화면에 보낼 설정 (토큰은 있는지만)"""
    saved = _read(project)
    return {side: {"url": (saved.get(side) or {}).get("url", ""), "branch": (saved.get(side) or {}).get("branch", ""),
                   "has_token": bool((saved.get(side) or {}).get("token"))} for side in SIDES}


def save_settings(project: str, body: dict) -> dict:
    """{frontend|backend: {url, branch, token}} — 토큰이 비면 저장된 것을 그대로 둔다. url이 비면 그쪽은 올리지 않는다"""
    saved = _read(project)
    for side in SIDES:
        given = body.get(side) or {}
        url = (given.get("url") or "").strip()
        if url and not url.startswith(("https://", "http://")):
            raise BadRequest(f"{SIDES[side]} 저장소 주소는 https://로 시작해야 합니다 (예: https://github.com/팀/저장소.git).")
        old = saved.get(side) or {}
        token = (given.get("token") or "").strip()
        saved[side] = {"url": url, "branch": (given.get("branch") or "").strip(),
                       "token": _fernet().encrypt(token.encode()).decode() if token else old.get("token", "")}
    get_store().write_project_file(project, SETTINGS, json.dumps(saved, ensure_ascii=False))
    return settings(project)


def _token(project: str, side: str) -> str:
    enc = (_read(project).get(side) or {}).get("token")
    return _fernet().decrypt(enc.encode()).decode() if enc else os.getenv("GIT_TOKEN", "").strip()


def _copy(app: Path, work: Path, items: list[str]) -> None:
    """작업 폴더를 비우고(.git 빼고) 올릴 내용을 복사한다"""
    for child in work.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    for item in items:
        src = app / item
        if not src.exists():
            continue
        dest = work / item
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, ignore=lambda _d, names: [n for n in names if n in SKIP])
        else:
            shutil.copy2(src, dest)


class _GitRunner(_Runner):
    def __init__(self, job: Job, project: str, version: str, member):
        super().__init__(job, project, Path("."))
        self.version, self.member = version, member
        self.secrets: list[str] = []

    def _push(self, line: str) -> None:
        for s in self.secrets:
            line = line.replace(s, "***")
        super()._push(line)

    def _git(self, args: list[str], cwd: Path, token: str = "") -> int:
        cmd = ["git"]
        if token:
            header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            self.secrets += [token, header]
            cmd += ["-c", f"http.extraHeader=Authorization: Basic {header}"]
        cmd += args
        self._push("$ git " + " ".join(args))
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        for line in (proc.stdout + proc.stderr).splitlines():
            self._push(line)
        return proc.returncode

    def _side(self, side: str, conf: dict, approver: str) -> str:
        app = app_dir(self.project)
        work = app / PREVIEW / f"git-{side}"
        token = _token(self.project, side)
        branch = conf.get("branch") or f"req-agent/v{self.version}"
        url = conf["url"]
        if not (work / ".git").is_dir():
            work.mkdir(parents=True, exist_ok=True)
            self._git(["init", "-q"], work)
            self._git(["remote", "add", "origin", url], work)
        else:
            self._git(["remote", "set-url", "origin", url], work)
        # 원격에 브랜치가 있으면 그 위에, 없으면 새 브랜치
        if self._git(["fetch", "-q", "--depth", "1", "origin", branch], work, token) == 0:
            self._git(["checkout", "-q", "-B", branch, "FETCH_HEAD"], work)
        else:
            self._git(["checkout", "-q", "-B", branch], work)
        _copy(app, work, CONTENT[side])
        self._git(["add", "-A"], work)
        if self._git(["diff", "--cached", "--quiet"], work) == 0:
            self._push(f"{SIDES[side]}: 바뀐 파일이 없어 커밋하지 않음")
        else:
            develop = (find_version(self.project, self.version) or {}).get("develop") or {}
            message = (f"최종본 v{self.version} 개발물 ({SIDES[side]})\n\n승인: {approver}\n"
                       f"개발: {(develop.get('by') or {}).get('name', '')} · 업무 로직 AI: {'사용' if develop.get('logic') else '안 씀'}\n"
                       f"올림: {self.member.name} · 설계 도구 req-agent")
            author = ["-c", "user.name=req-agent", "-c", f"user.email=req-agent@{self.member.email.split('@')[-1]}"]
            if self._git([*author, "commit", "-q", "-m", message], work) != 0:
                raise RuntimeError(f"{SIDES[side]} 커밋을 만들지 못했습니다.")
        if self._git(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], work, token) != 0:
            raise RuntimeError(f"{SIDES[side]} 저장소에 올리지 못했습니다 (주소·브랜치·토큰 권한을 확인하세요).")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()
        return f"{branch}@{sha[:10]}"

    def _run(self, args: list[str]) -> None:
        try:
            conf = _read(self.project)
            sides = [s for s in SIDES if (conf.get(s) or {}).get("url")]
            if not sides:
                return self._finish("failed", "저장소 주소가 없습니다. 'Git에 올리기' 설정에서 화면·백엔드 저장소를 적으세요.")
            record = find_version(self.project, self.version) or {}
            approver = ((record.get("approval") or {}).get("by") or {}).get("name", "")
            pushed = {}
            for side in sides:
                self.job.message = f"{SIDES[side]} 저장소에 올리는 중"
                self._save()
                pushed[side] = self._side(side, conf[side], approver)
            git = {"at": datetime.now().isoformat(timespec="seconds"), "by": {"id": self.member.id, "name": self.member.name},
                   "pushed": pushed, "urls": {s: conf[s]["url"] for s in sides}}
            update_version(self.project, self.version, git=git, gits=[*(record.get("gits") or []), git])
            self.job.new_version = self.version
            self._finish("done", "올렸습니다: " + ", ".join(f"{SIDES[s]} {v}" for s, v in pushed.items()))
        except Exception as e:  # noqa: BLE001 - 작업 스레드의 오류는 화면에 보여 준다
            self._push(str(e))
            self._finish("failed", str(e))


def start_push(*, project: str, version: str, member) -> Job:
    if not (app_dir(project) / "README.md").is_file():
        raise BadRequest("먼저 '최종 개발하기'로 코드를 만드세요.")
    job = _new_job(project=project, base=version, source="git", message="git에 올리는 중")
    _GitRunner(job, project, version, member).start([])
    return job
