"""최종 개발물 실행 미리보기: 생성된 앱을 그 앱의 docker-compose.yml(앱 폴더)로 띄운다. 우리 서버를 거치지 않는 독립 앱이다.

- 앱마다 compose 프로젝트 reqagent-app-<키>로 따로 띄운다 (우리 서비스 컨테이너와 분리, DB 볼륨도 앱마다).
  Dockerfile이 화면·서버를 이미지 안에서 빌드하므로 서버 PC에는 Docker만 있으면 된다.
- 앱 포트는 18080~18199 중 빈 것. PREVIEW_BIND(기본 0.0.0.0)에 열어 팀원 PC에서도 http://<서버 주소>:<포트>로 연다.
  앱 DB 포트는 이 PC 안(127.0.0.1)에서만, 앱마다 다른 빈 포트.
- 쓰는 동안은 켜 두고 IDLE_SECONDS(10분) 동안 요청이 없으면 끈다. 요청은 앱의 접근 기록(ACCESS_LOG_DIR,
  Spring은 Tomcat access log)이 바뀐 시각으로 안다. 동시에 MAX_RUNNING(3)개까지 켠다 (넘으면 가장 오래 안 쓴 것을 끈다).
- 다시 개발하면 다음에 켤 때 새 코드로 다시 빌드한다. 스키마가 바뀌면 DB를 새로 만든다 (예시 데이터로 다시 채움).
- 시연 계정 비밀번호는 앱이 처음 뜰 때 만들어 앱 폴더 local/accounts.txt에만 적는다 (accounts()로 개발자에 보여 준다).
상태는 메모리와 app/.preview/run.json에 둔다 (서버를 다시 띄우면 컨테이너 상태를 docker에 다시 묻는다).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from app.core import exe
from app.services.develop import PREVIEW, app_dir

log = logging.getLogger(__name__)
IDLE_SECONDS = 10 * 60
MAX_RUNNING = 3
LOG_LINES = 2000      # 터미널에 남기는 빌드·실행 기록 줄 수
PORTS = range(18080, 18200)
DB_PORTS = range(18300, 18420)
_lock = threading.Lock()
_state: dict[str, dict] = {}       # 프로젝트 → 상태 (status, port, message, log, last_access …)

# 앱의 compose에 더하는 것: 요청 기록(쓰는 중인지 보려고). Spring은 Tomcat access log, 다른 틀은 ACCESS_LOG_DIR을 따른다
OVERRIDE = """# 실행 미리보기가 더하는 설정 (설계 도구가 만듦, git에 올리지 않음)
services:
  app:
    environment:
      ACCESS_LOG_DIR: /app/access
      SERVER_TOMCAT_ACCESSLOG_ENABLED: "true"
      SERVER_TOMCAT_ACCESSLOG_DIRECTORY: /app/access
      SERVER_TOMCAT_ACCESSLOG_BUFFERED: "false"
    volumes:
      - ./.preview/access:/app/access   # 경로는 앱 폴더(첫 compose 파일) 기준
"""


def key(project: str) -> str:
    """docker 이름에 쓸 수 있는 프로젝트 키 (한글 이름도 되게 해시)"""
    return "p" + hashlib.sha1(project.encode("utf-8")).hexdigest()[:10]


def _name(project: str) -> str:
    return f"reqagent-app-{key(project)}"


def _tree_hash(*paths: Path) -> str:
    h = hashlib.sha256()
    for root in paths:
        files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else ([root] if root.is_file() else [])
        for p in files:
            h.update(str(p.relative_to(root.parent)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _file(project: str) -> Path:
    return app_dir(project) / PREVIEW / "run.json"


def _get(project: str) -> dict:
    with _lock:
        if project not in _state:
            f = _file(project)
            saved = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
            # 서버를 다시 띄웠으면 '빌드 중'은 끊긴 것이고, '실행 중'은 docker에 다시 묻는다
            status = saved.get("status", "stopped")
            if status in ("building", "starting"):
                status = "stopped"
            if status == "running" and not _container_up(project):
                status = "stopped"
            _state[project] = {**saved, "status": status, "log": saved.get("log", [])}
        return _state[project]


def _save(project: str) -> None:
    s = _state.get(project)
    if s is None:
        return
    f = _file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({k: v for k, v in s.items() if k != "log"} | {"log": s.get("log", [])[-500:]},
                            ensure_ascii=False), encoding="utf-8")


def _container_up(project: str) -> bool:
    try:
        out = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={_name(project)}"],
                             capture_output=True, text=True, timeout=15).stdout
        return bool(out.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def runnable(project: str) -> bool:
    """앱 폴더에 실행 파일(Dockerfile·docker-compose.yml)이 있나 (서버 틀이 있는 스택이면 만들어진다)"""
    app = app_dir(project)
    return (app / "Dockerfile").is_file() and (app / "docker-compose.yml").is_file()


def last_activity(project: str) -> float:
    """마지막으로 앱을 쓴 시각: 우리가 켠 시각과 앱 접근 기록이 바뀐 시각 중 늦은 것"""
    s = _get(project)
    access = app_dir(project) / PREVIEW / "access"
    stamps = [s.get("last_access") or 0]
    if access.is_dir():
        stamps += [p.stat().st_mtime for p in access.iterdir() if p.is_file()]
    return max(stamps)


def status(project: str) -> dict:
    s = _get(project)
    idle = int(time.time() - last_activity(project)) if s.get("status") == "running" else None
    return {"status": s.get("status", "stopped"), "message": s.get("message", ""), "port": s.get("port"),
            "stale": s.get("stale", False), "backend": runnable(project),
            "mock": (app_dir(project) / PREVIEW / "mock" / "index.html").is_file(),
            "docker": shutil.which("docker") is not None, "log": s.get("log", [])[-40:],
            "idle_seconds": idle, "idle_limit": IDLE_SECONDS, "url": app_url(s.get("port"))}


def app_url(port: int | None) -> str | None:
    """밖에서 여는 앱 주소. PREVIEW_PUBLIC_HOST(서버의 공인 IP·도메인)가 있을 때만: 화면을 다른 주소(Vercel 등)에서 띄우면
    화면 주소에 포트를 붙여서는 앱에 닿지 않는다. 없으면 None (화면이 자기 주소에 포트를 붙인다, 로컬 개발)"""
    host = os.getenv("PREVIEW_PUBLIC_HOST", "").strip().rstrip("/")
    if not host or not port:
        return None
    return f"{host if '://' in host else 'http://' + host}:{port}/"


def touch(project: str) -> None:
    s = _get(project)
    s["last_access"] = time.time()


def mark_stale(project: str) -> None:
    """다시 개발했다: 다음에 켤 때 새 코드로 다시 빌드한다"""
    s = _get(project)
    s["stale"] = True
    _save(project)


class _Log:
    """명령을 실행하고 출력을 미리보기 기록(터미널)에 남긴다"""

    def __init__(self, project: str):
        self.project = project

    def _push(self, line: str) -> None:
        if line.strip():
            s = _get(self.project)
            s.setdefault("log", []).append(line.rstrip()[:500])
            del s["log"][:-LOG_LINES]

    def _sh(self, cmd: list[str], cwd: Path, env: dict | None = None) -> int:
        self._push(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=exe.run_shell(cmd[0]),
                                    text=True, encoding="utf-8", errors="replace", env={**os.environ, **(env or {})})
        except OSError as e:
            self._push(f"{cmd[0]}을(를) 실행할 수 없습니다: {e}")
            return -1
        for line in proc.stdout:
            self._push(line)
        return proc.wait()


def _free_port(ports: range, taken: set[int]) -> int:
    for port in ports:
        if port in taken:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"미리보기에 쓸 빈 포트가 없습니다 ({ports.start}~{ports.stop - 1}).")


def _compose_cmd(project: str) -> list[str]:
    app = app_dir(project)
    return ["docker", "compose", "-p", _name(project), "-f", str(app / "docker-compose.yml"), "-f", str(app / PREVIEW / "compose.preview.yml")]


def _env(s: dict) -> dict:
    return {"APP_HOST": os.getenv("PREVIEW_BIND", "0.0.0.0"), "APP_PORT": str(s["port"]), "DB_PORT": str(s["db_port"])}


def start(project: str) -> dict:
    """실행 미리보기를 켠다 (이미 켜져 있고 코드가 그대로면 그대로). 빌드·실행은 백그라운드에서 하고 상태로 진행을 알린다"""
    s = _get(project)
    if s.get("status") in ("building", "starting"):
        return status(project)
    if s.get("status") == "running" and not s.get("stale") and _container_up(project):
        touch(project)
        return status(project)
    if not runnable(project):
        s.update(status="no_backend", message="서버 코드가 없어 실행할 수 없습니다 (서버 틀이 없는 기술 스택). 예시 데이터 화면만 볼 수 있습니다.")
        return status(project)
    if shutil.which("docker") is None:
        s.update(status="failed", message="서버 PC에 Docker가 없어 실행할 수 없습니다. 예시 데이터 화면은 볼 수 있습니다.")
        return status(project)
    s.update(status="building", message="실행 준비 중 (1/2 이미지 빌드 — 처음에는 몇 분 걸립니다)", log=[], last_access=time.time())
    threading.Thread(target=_bring_up, args=(project,), daemon=True).start()
    return status(project)


def _make_room(project: str) -> None:
    """동시에 MAX_RUNNING개까지: 넘으면 가장 오래 안 쓴 것을 끈다"""
    running = [(p, last_activity(p)) for p, st in list(_state.items()) if p != project and st.get("status") == "running"]
    for p, _ in sorted(running, key=lambda x: x[1])[: max(0, len(running) - MAX_RUNNING + 1)]:
        log.info("[미리보기] 동시 실행 %d개 한도: %s 끔", MAX_RUNNING, p)
        stop(p, "다른 프로젝트 미리보기를 켜느라 껐습니다 (동시 실행 3개까지).")


def _bring_up(project: str) -> None:
    s, out = _get(project), _Log(project)
    app = app_dir(project)
    work = app / PREVIEW
    try:
        work.mkdir(parents=True, exist_ok=True)
        (work / "access").mkdir(exist_ok=True)
        (work / "compose.preview.yml").write_text(OVERRIDE, encoding="utf-8")
        others = [st for p, st in _state.items() if p != project and st.get("status") in ("running", "starting", "building")]
        if not s.get("port") or not _container_up(project):
            s["port"] = _free_port(PORTS, {st.get("port") for st in others if st.get("port")})
            s["db_port"] = _free_port(DB_PORTS, {st.get("db_port") for st in others if st.get("db_port")})
        cmd, env = _compose_cmd(project), _env(s)
        if out._sh([*cmd, "build", "--progress=plain"], app, env) != 0:
            raise RuntimeError("앱 이미지를 빌드하지 못했습니다 (위 기록).")
        s.update(message="실행 준비 중 (2/2 DB·서버 시작)", status="starting")
        _make_room(project)
        schema = _tree_hash(app / "db")
        # DB를 새로 만든다 (예시 데이터로 다시 채움): 이 앱 폴더에서 처음 켤 때(같은 이름의 예전 볼륨·다른 DB 종류가 남았을 수 있다),
        # 스키마가 바뀌었을 때, 계정 비밀번호 파일이 없을 때(DB에만 계정이 있으면 로그인할 수 없다)
        if s.get("schema") != schema or not (app / "local" / "accounts.txt").is_file():
            out._sh([*cmd, "down", "-v"], app, env)
        if out._sh([*cmd, "up", "-d"], app, env) != 0:
            raise RuntimeError("컨테이너를 시작하지 못했습니다 (위 기록).")
        import httpx
        for _ in range(180):   # 서버가 답할 때까지 (최대 3분). 로그인 전이라 401이 정상
            try:
                if httpx.get(f"http://127.0.0.1:{s['port']}/api/auth/me", timeout=2).status_code < 500:
                    break
            except httpx.HTTPError:
                pass   # 아직 뜨는 중
            time.sleep(1)
        else:
            raise RuntimeError("서버가 3분 안에 답하지 않았습니다 (터미널의 앱 서버 로그를 보세요).")
        s.update(status="running", schema=schema, stale=False, last_access=time.time(),
                 started_at=datetime.now().isoformat(timespec="seconds"), message="실행 중")
    except Exception as e:  # noqa: BLE001
        out._push(str(e))
        s.update(status="failed", message=str(e))
    finally:
        _save(project)


def terminal(project: str) -> dict:
    """화면의 터미널: 우리가 실행한 명령과 출력(빌드·실행) + 앱 서버 컨테이너 로그(마지막 300줄)"""
    s = _get(project)
    app_log: list[str] = []
    if s.get("port") and s.get("status") in ("starting", "running", "failed") and shutil.which("docker") and runnable(project):
        try:
            proc = subprocess.run([*_compose_cmd(project), "logs", "--no-color", "--tail", "300", "app"], cwd=app_dir(project),
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, env={**os.environ, **_env(s)})
            app_log = [line[:500] for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            app_log = ["(앱 서버 로그를 읽지 못했습니다)"]
    return {"status": s.get("status", "stopped"), "message": s.get("message", ""), "log": s.get("log", []), "app_log": app_log}


def accounts(project: str) -> list[dict]:
    """앱이 처음 뜰 때 만든 시연 계정 (local/accounts.txt: 아이디·역할·비밀번호, 탭 구분)"""
    path = app_dir(project) / "local" / "accounts.txt"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if line.startswith("#") or len(parts) < 3:
            continue
        out.append({"loginId": parts[0], "role": parts[1], "password": parts[2]})
    return out


def wait(project: str, limit: int = 20 * 60) -> dict:
    """켜는 중이면 끝날 때까지 기다린다 (개발 작업의 자동 확인)"""
    t0 = time.time()
    while _get(project).get("status") in ("building", "starting") and time.time() - t0 < limit:
        time.sleep(3)
    return status(project)


def smoke(project: str) -> dict:
    """자동 확인: admin으로 로그인해 경로 변수 없는 모든 조회(GET) API를 한 번씩 부른다 (LLM 없음).
    501은 일부러 막아 둔 '구현 대기'(고객 답을 기다리는 기능), 그 밖의 5xx는 서버 오류로 센다."""
    import httpx
    import yaml
    s = _get(project)
    if s.get("status") != "running" or not s.get("port"):
        return {"ok": False, "message": s.get("message") or "앱이 켜지지 않았습니다."}
    base = f"http://127.0.0.1:{s['port']}"
    admin = next((a for a in accounts(project) if a["loginId"] == "admin"), None)
    if not admin:
        return {"ok": False, "message": "admin 계정을 찾지 못했습니다 (local/accounts.txt)."}
    try:
        r = httpx.post(base + "/api/auth/login", json={"loginId": admin["loginId"], "password": admin["password"]}, timeout=10)
        if not 200 <= r.status_code < 300:   # 프레임워크에 따라 201일 수 있다
            return {"ok": False, "message": f"admin 로그인 실패 ({r.status_code})"}
        headers = {"Authorization": f"Bearer {r.json().get('token', '')}"}
        spec = yaml.safe_load((app_dir(project) / "spec" / "openapi.yaml").read_text(encoding="utf-8")) or {}
        paths = [p for p, ops in (spec.get("paths") or {}).items() if "get" in ops and "{" not in p]
        result: dict[str, list[str]] = {"ok": [], "not_implemented": [], "errors": [], "other": []}
        for path in paths:
            url = base + (path if path.startswith("/api/") else "/api/v1" + path)
            code = httpx.get(url, headers=headers, timeout=30).status_code
            group = "ok" if code < 300 else "not_implemented" if code == 501 else "errors" if code >= 500 else "other"
            result[group].append(f"{path} {code}" if group != "ok" else path)
    except (httpx.HTTPError, OSError, ValueError) as e:
        return {"ok": False, "message": f"확인하지 못했습니다: {e}"}
    finally:
        touch(project)
    counts = {k: len(v) for k, v in result.items()}
    return {"ok": not result["errors"], "login": True, "apis": len(paths), "counts": counts,
            "errors": result["errors"][:20], "not_implemented": result["not_implemented"][:20], "other": result["other"][:20]}


def stop(project: str, reason: str = "껐습니다.") -> dict:
    s = _get(project)
    if runnable(project) and s.get("port") and shutil.which("docker"):
        subprocess.run([*_compose_cmd(project), "stop"], cwd=app_dir(project), capture_output=True, timeout=120, env={**os.environ, **_env(s)})
    s.update(status="stopped", message=reason)
    _save(project)
    return status(project)


def _reaper() -> None:
    """IDLE_SECONDS 동안 아무도 쓰지 않은 실행 미리보기를 끈다"""
    while True:
        time.sleep(30)
        now = time.time()
        for project, s in list(_state.items()):
            if s.get("status") == "running" and now - last_activity(project) > IDLE_SECONDS:
                log.info("[미리보기] %d분 동안 안 써서 끔: %s", IDLE_SECONDS // 60, project)
                try:
                    stop(project, f"{IDLE_SECONDS // 60}분 동안 아무도 쓰지 않아 껐습니다. 다시 열면 켜집니다.")
                except Exception as e:  # noqa: BLE001
                    log.warning("[미리보기] 끄지 못함 %s: %s", project, e)


def start_reaper() -> None:
    threading.Thread(target=_reaper, daemon=True, name="preview-reaper").start()
