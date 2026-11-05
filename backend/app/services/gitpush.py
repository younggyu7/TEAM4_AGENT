"""최종 개발물 원격 올리기: 서버의 코드 저장소(OUTPUT_DIR/<프로젝트>/app, services/repo.py)의 main과 최종본 태그를 원격 하나에 push한다.

- 원격 설정은 프로젝트마다 (프로젝트 파일 git.json): {url, branch(기본 main), token}. 화면·백엔드·DB는 한 저장소에 같이 올린다.
  토큰은 암호화해 저장하고(Fernet, 키는 .env GIT_SECRET_KEY, 없으면 OUTPUT_DIR/.git-secret를 만들어 씀) 화면에는 다시 보내지 않는다.
  예전 설정(화면·백엔드 저장소 두 개 {frontend, backend})은 읽을 때 백엔드(없으면 화면) 저장소 하나로 바꿔 읽는다 (토큰 유지).
- 올리기(작업, 개발 파트장): 원격 브랜치를 받아(fetch) 원격에 main에 없는 커밋이 있으면 거부하고(강제 push 없음),
  `git push origin main:<branch> + final-v* 태그`. 빌드 결과·미리보기·생성기 기록은 .gitignore로 커밋되지 않는다.
- 원격 확인(check_remote)은 화면이 '원격 확인'을 누를 때와 올릴 때만 fetch한다. 앞섬·뒤처짐은 받아 둔 origin/<branch>로 센다.
- 토큰은 명령줄·기록에 남지 않게 http.extraHeader로만 넘기고, 기록의 토큰 글자는 가린다. LLM은 쓰지 않는다.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path

from app.agents.storage import get_store
from app.agents.tools.versions import find_version, update_version
from app.core.config import get_settings
from app.core.exceptions import BadRequest
from app.services import repo
from app.services.develop import app_dir
from app.services.jobs.common import Job, _new_job, _Runner

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
    """{url, branch, token(암호문)}. 예전 두 저장소 설정이면 백엔드(없으면 화면) 쪽 하나로"""
    text = get_store().read_project_file(project, SETTINGS)
    saved = json.loads(text) if text else {}
    if "url" not in saved:
        old = next((saved[s] for s in ("backend", "frontend") if (saved.get(s) or {}).get("url")), {})
        saved = {"url": old.get("url", ""), "branch": old.get("branch") or "", "token": old.get("token", "")}
    return {"url": saved.get("url", ""), "branch": saved.get("branch") or repo.MAIN, "token": saved.get("token", "")}


def settings(project: str) -> dict:
    """화면에 보낼 설정 (토큰은 있는지만)"""
    saved = _read(project)
    return {"url": saved["url"], "branch": saved["branch"], "has_token": bool(saved["token"])}


def save_settings(project: str, body: dict) -> dict:
    """{url, branch, token} — 토큰이 비면 저장된 것을 그대로 둔다"""
    saved = _read(project)
    url = (body.get("url") or "").strip()
    if url and not url.startswith(("https://", "http://")):
        raise BadRequest("저장소 주소는 https://로 시작해야 합니다 (예: https://github.com/팀/저장소.git).")
    branch = (body.get("branch") or "").strip() or repo.MAIN
    if branch != repo.MAIN:
        branch = repo.clean_name(branch)
    token = (body.get("token") or "").strip()
    saved = {"url": url, "branch": branch, "token": _fernet().encrypt(token.encode()).decode() if token else saved["token"]}
    get_store().write_project_file(project, SETTINGS, json.dumps(saved, ensure_ascii=False))
    return settings(project)


def _token(project: str) -> str:
    enc = _read(project)["token"]
    return _fernet().decrypt(enc.encode()).decode() if enc else os.getenv("GIT_TOKEN", "").strip()


def _header(token: str) -> str:
    return "Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode() if token else ""


def _set_remote(app: Path, url: str) -> None:
    if repo.git(app, "remote", "get-url", "origin", check=False).startswith("0\n"):
        repo.git(app, "remote", "set-url", "origin", url)
    else:
        repo.git(app, "remote", "add", "origin", url)


def remote_status(project: str) -> dict:
    """원격 설정 + 받아 둔 origin/<branch> 기준 main의 앞섬·뒤처짐 (fetch하지 않는다)"""
    conf = settings(project)
    out = {**conf, "ahead": None, "behind": None, "exists": False, **repo._data(project)["remote"]}
    if conf["url"] and repo.ready(project):
        app = app_dir(project)
        ref = f"refs/remotes/origin/{conf['branch']}"
        if repo.git(app, "rev-parse", "--verify", "-q", ref, check=False).startswith("0\n"):
            out["behind"], out["ahead"] = repo._counts(app, ref, repo.MAIN)
            out["exists"] = True
        elif out.get("checked_at"):
            out["ahead"], out["behind"] = int(repo.git(app, "rev-list", "--count", repo.MAIN).strip()), 0
    return out


def _fetch(project: str, app: Path, conf: dict, token: str) -> tuple[bool, str]:
    """원격 브랜치를 origin/<branch>로 받는다. (원격에 브랜치가 있는지, 오류 메시지)"""
    _set_remote(app, conf["url"])
    header = _header(token)
    listed = repo.git(app, "ls-remote", "--heads", "origin", conf["branch"], check=False, header=header)
    code, out = listed.split("\n", 1)
    if code != "0":
        return False, _redact(out.strip()[-300:], token, header) or "원격에 연결하지 못했습니다."
    if not out.strip():
        return False, ""
    fetched = repo.git(app, "fetch", "-q", "origin", f"+refs/heads/{conf['branch']}:refs/remotes/origin/{conf['branch']}",
                       check=False, header=header)
    if not fetched.startswith("0\n"):
        return False, _redact(fetched.split("\n", 1)[1].strip()[-300:], token, header)
    return True, ""


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def check_remote(project: str) -> dict:
    """'원격 확인': 원격 브랜치를 받아 앞섬·뒤처짐을 다시 센다 (개발 파트장)"""
    conf = _read(project)
    if not conf["url"]:
        raise BadRequest("원격 저장소 주소가 없습니다. 원격 설정에서 주소를 적으세요.")
    app = repo.ensure_repo(project)
    _, error = _fetch(project, app, conf, _token(project))
    with repo._lock:
        data = repo._data(project)
        data["remote"] = {"checked_at": datetime.now().isoformat(timespec="seconds"), "error": error}
        repo._write(project, data)
    return remote_status(project)


class _GitRunner(_Runner):
    def __init__(self, job: Job, project: str, version: str, member):
        super().__init__(job, project, Path("."))
        self.version, self.member = version, member
        self.secrets: list[str] = []

    def _push(self, line: str) -> None:
        for s in self.secrets:
            line = line.replace(s, "***")
        super()._push(line)

    def _run(self, args: list[str]) -> None:
        try:
            conf = _read(self.project)
            if not conf["url"]:
                return self._finish("failed", "원격 저장소 주소가 없습니다. 'Git' 단계의 원격 설정에서 주소를 적으세요.")
            app = repo.ensure_repo(self.project)
            token = _token(self.project)
            self.secrets += [s for s in (token, _header(token).split(" ")[-1] if token else "") if s]
            branch = conf["branch"]
            self.job.message = f"원격 {branch}를 확인하는 중"
            self._save()
            self._push(f"$ git fetch origin {branch}")
            exists, error = _fetch(self.project, app, conf, token)
            if error:
                raise RuntimeError(f"원격을 받지 못했습니다 (주소·토큰 권한을 확인하세요): {error}")
            if exists and not repo._ok(app, "merge-base", "--is-ancestor", f"refs/remotes/origin/{branch}", repo.MAIN):
                raise RuntimeError(f"원격 {branch}에 서버에 없는 커밋이 있어 올리지 않았습니다 (강제로 덮어쓰지 않음). "
                                   "원격의 변경을 서버 밖에서 정리한 뒤 다시 올리세요.")
            self.job.message = f"원격 {branch}에 올리는 중"
            self._save()
            self._push(f"$ git push origin {repo.MAIN}:{branch} +refs/tags/final-v*")
            pushed = repo.git(app, "push", "--porcelain", "origin", f"refs/heads/{repo.MAIN}:refs/heads/{branch}",
                              "+refs/tags/final-v*:refs/tags/final-v*", check=False, header=_header(token))
            for line in pushed.split("\n")[1:]:
                if line.strip():
                    self._push(line)
            if not pushed.startswith("0\n"):
                raise RuntimeError("원격에 올리지 못했습니다 (주소·브랜치·토큰 권한을 확인하세요).")
            sha = repo.git(app, "rev-parse", repo.MAIN).strip()
            with repo._lock:
                data = repo._data(self.project)
                data["remote"] = {"checked_at": datetime.now().isoformat(timespec="seconds"), "error": ""}
                repo._write(self.project, data)
            record = find_version(self.project, self.version) or {}
            git = {"at": datetime.now().isoformat(timespec="seconds"), "by": {"id": self.member.id, "name": self.member.name},
                   "url": conf["url"], "branch": branch, "commit": sha}
            update_version(self.project, self.version, git=git, gits=[*(record.get("gits") or []), git])
            self.job.new_version = self.version
            self._finish("done", f"올렸습니다: {branch}@{sha[:10]}")
        except Exception as e:  # noqa: BLE001 - 작업 스레드의 오류는 화면에 보여 준다
            self._push(str(e))
            self._finish("failed", str(e))


def start_push(*, project: str, version: str, member) -> Job:
    if not (app_dir(project) / "README.md").is_file():
        raise BadRequest("먼저 '최종 개발하기'로 코드를 만드세요.")
    job = _new_job(project=project, base=version, source="git", message="원격에 올리는 중")
    _GitRunner(job, project, version, member).start([])
    return job
