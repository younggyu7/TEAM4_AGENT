"""작업 공통: 작업 기록(Job), CLI 하위 프로세스 실행기(_Runner), 종료 코드·LLM 오류 안내.

서비스별 작업 시작은 run.py(문서 업로드)·rerun.py(새 버전 만들기)·review.py(ERD 최종 확정)에 있다.
LLM 작업은 몇 분씩 걸리므로 서버 안에서 돌리지 않고 하위 프로세스로 띄운다. 작업 기록은 메모리에만 있어 서버를 다시 띄우면 사라진다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path

from app.agents.storage import get_store
from app.core.database import database_enabled, session_scope
from app.repositories import job_repository
from app.core.security import redact
from app.services.output_store import version_key, version_labels

# backend 폴더 (CLI를 이 폴더에서 실행한다). 이 파일은 app/services/jobs/ 안에 있다
BACKEND_ROOT = Path(__file__).resolve().parents[3]
log = logging.getLogger(__name__)
MAX_LOG = 300

# 종료 코드 → 화면에 보여 줄 이유 (app/cli.py의 _drive·_rerun과 같은 뜻)
EXIT_MESSAGE = {
    2: "다시 만들 수 없습니다. 아래 기록의 마지막 줄을 확인하세요.",
    4: "LLM 설정 오류로 멈췄습니다. .env의 키·크레딧·모델 설정을 확인하세요.",
    5: "LLM 응답 대기(manual 공급자)에서 멈췄습니다. 응답 파일을 쓴 뒤 CLI로 resume 하세요.",
}


@dataclass
class Job:
    id: str
    project: str
    base: str
    source: str              # run(문서 업로드) / spec(기능 명세 → ERD) / erd(ERD 저장) / answers(확인 질문 답 → 기능 명세) / review(ERD 최종 확정 → 의미 검토)
    status: str              # running / done / failed
    message: str
    log: list[str] = field(default_factory=list)
    new_version: str | None = None
    warnings: list[str] = field(default_factory=list)   # 새 버전은 만들었지만 Agent 단계가 실패한 경우
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


_jobs: dict[str, Job] = {}       # 이 서버가 띄운 작업 (진행 중 상태는 여기서 바뀐다)
_lock = threading.Lock()
SAVE_EVERY = 2.0                 # 서비스 DB가 있으면 실행 기록을 이 간격(초)으로 저장한다 (상태가 바뀔 때는 바로)


def _persist(job: Job) -> None:
    """서비스 DB가 있으면 작업 기록을 저장한다 (서버를 다시 띄워도 /jobs로 볼 수 있게). 저장 실패는 작업을 멈추지 않는다."""
    if not database_enabled():
        return
    try:
        with session_scope() as s:
            job_repository.save(s, job.to_dict())
    except Exception as e:  # noqa: BLE001 - 기록 저장 실패로 작업을 멈추지 않는다
        log.warning("작업 기록 저장 실패 %s: %s", job.id, e)


def get_job(job_id: str) -> Job | None:
    job = _jobs.get(job_id)
    if job is None and database_enabled():
        with session_scope() as s:
            saved = job_repository.get(s, job_id)
        job = Job(**saved) if saved else None
    return job


def running_job(project: str) -> Job | None:
    return next((j for j in _jobs.values() if j.project == project and j.status == "running"), None)


def close_orphaned_jobs() -> int:
    """서버를 다시 띄웠을 때: 서비스 DB에 '진행 중'으로 남은 작업은 더 추적할 수 없으므로 실패로 닫는다. 닫은 수."""
    if not database_enabled():
        return 0
    with session_scope() as s:
        return job_repository.fail_running(
            s, "서버가 다시 시작되어 진행 상황을 더 추적할 수 없습니다. 만들어진 버전은 버전 목록에서 확인하세요.",
            datetime.now().isoformat(timespec="seconds"))


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "app", *args]


def _path_arg(path: Path) -> str:
    """backend 안의 경로는 상대 경로로 넘긴다 (versions.json의 output_dir가 폴더를 옮겨도 맞게)"""
    path = path.resolve()
    return str(path.relative_to(BACKEND_ROOT)) if path.is_relative_to(BACKEND_ROOT) else str(path)


@cache
def capabilities() -> dict:
    """백엔드 rerun이 기능 명세 수정본·추천안 확인 규칙을 받는지 (옵션이 추가되면 자동으로 그걸 쓴다)"""
    out = subprocess.run(_cli("rerun", "-h"), cwd=BACKEND_ROOT, capture_output=True, text=True).stdout
    return {"design": bool(re.search(r"--design\b", out)), "spec_md": bool(re.search(r"--spec-md\b", out)),
            "confirm_rules": bool(re.search(r"--confirm-rules\b", out))}


class _Runner:
    """작업 하나: CLI를 실행하고, 확인 질문에서 멈추면 resume --defer로 이어 가고, 끝나면 새 버전을 찾는다."""

    def __init__(self, job: Job, project: str, output_dir: Path, *, submitted_md: str | None = None,
                 pre: list[str] | None = None, temp: list[Path] | None = None,
                 on_done: Callable[[str], None] | None = None, result: str | None = None):
        # project: 산출물 저장소의 프로젝트 ID (새 버전 찾기·결과 읽기) / output_dir: CLI에 --output-dir로 넘길 폴더
        self.job, self.project, self.output_dir = job, project, output_dir
        self.submitted_md, self.pre, self.temp, self.on_done = submitted_md, pre or [], temp or [], on_done
        self.result = result     # 최종 확정: 새 최종본 폴더에서 읽을 결과 파일 이름 (semantic_review.json)
        self.before = version_labels(project)
        self._saved = time.monotonic()

    def start(self, args: list[str]) -> None:
        threading.Thread(target=self._run, args=(args,), daemon=True).start()

    def _push(self, line: str) -> None:
        if line.strip():
            self.job.log.append(redact(line.rstrip()))
            del self.job.log[:-MAX_LOG]
            if time.monotonic() - self._saved >= SAVE_EVERY:
                self._save()

    def _save(self) -> None:
        self._saved = time.monotonic()
        _persist(self.job)

    def _exec(self, args: list[str]) -> int:
        self._push(f"$ python -m app {' '.join(args)}")
        try:
            # 터미널이 아니면 CLI는 확인 질문에서 멈추고 코드 3으로 끝난다
            proc = subprocess.Popen(_cli(*args), cwd=BACKEND_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except OSError as e:
            self._push(str(e))
            return -1
        for line in proc.stdout:
            self._push(line)
        return proc.wait()

    def _run(self, args: list[str]) -> None:
        try:
            code = self._exec(args)
            if code == 3:
                run_id = next((m[1] for line in self.job.log if (m := re.search(r"\(실행 (\S+?)\)", line))), None)
                if run_id:
                    self.job.message = "확인 질문을 보류(가정)로 처리하고 계속하는 중"
                    self._save()
                    code = self._exec(["resume", run_id, "--defer", "--output-dir", _path_arg(self.output_dir)])
            if code == 0:
                self._reviewed() if self.result else self._done()
            else:
                self.job.warnings = [*_llm_errors(self.job.log), *self.pre]
                reason = EXIT_MESSAGE.get(code, f"실행이 실패했습니다 (종료 코드 {code}).")
                if self.result and code == 2:
                    reason = "최종 확정(의미 검토)을 끝내지 못했습니다. 아래 기록의 마지막 줄을 확인하세요."
                self._finish("failed", reason)
        except Exception as e:  # noqa: BLE001 - 작업 스레드의 오류는 화면에 보여 준다
            self._push(repr(e))
            self._finish("failed", f"작업 중 오류가 났습니다: {e}")

    def _done(self) -> None:
        created = version_labels(self.project) - self.before
        if not created:
            return self._finish("failed", "실행은 끝났지만 새 버전을 찾지 못했습니다.")
        newest = max(created, key=version_key)
        self.job.new_version = newest
        # Agent에 넘긴 기능 명세를 새 버전에 남긴다 (어떤 명세로 만든 ERD인지 확인용)
        if self.submitted_md:
            get_store().write(self.project, newest, "functions.submitted.md", self.submitted_md)
        self.job.warnings = [*_llm_errors(self.job.log), *self.pre, *_run_warnings(self.project, newest)]
        if self.on_done:
            self.on_done(newest)
        self._finish("done", f"v{newest}을 만들었지만 Agent 단계 일부가 실패했습니다." if self.job.warnings
                     else f"v{newest}을 만들었습니다.")

    def _reviewed(self) -> None:
        """ERD 최종 확정: 새로 생긴 최종본(v1.1.1 …)을 찾아 그 의미 검토 결과로 끝낸다."""
        created = version_labels(self.project) - self.before
        if not created:
            return self._finish("failed", "최종 확정은 끝났지만 최종본을 찾지 못했습니다.")
        newest = max(created, key=version_key)
        self.job.new_version = newest
        try:
            data = json.loads(get_store().read(self.project, newest, self.result) or "{}")
        except ValueError:
            data = {}
        failed = data.get("failed") or []
        self.job.warnings = [*_llm_errors(self.job.log), *([f"의미 검토 실패(미검증): {', '.join(failed)}"] if failed else [])]
        self._finish("done", f"최종본 v{newest}을 만들었습니다: 의미 검토 지적 {len(data.get('remarks') or [])}건")

    def _finish(self, status: str, message: str) -> None:
        self.job.status, self.job.message = status, message
        self.job.finished_at = datetime.now().isoformat(timespec="seconds")
        self._save()
        for f in self.temp:
            f.unlink(missing_ok=True)


def _new_job(**kw) -> Job:
    job = Job(id=f"job-{uuid.uuid4().hex[:10]}", status="running", **kw)
    with _lock:
        _jobs[job.id] = job
    _persist(job)
    return job


def _temp_file(job: Job, name: str, body: str, temp: list[Path]) -> Path:
    f = Path(tempfile.gettempdir()) / f"req-agent-{job.id}-{name}"
    f.write_text(body, encoding="utf-8")
    temp.append(f)
    return f


def _run_warnings(project: str, version: str) -> list[str]:
    """실행은 끝났어도 Agent가 단계를 반복 실패하면 남은 결과로 '현재 상태로 제공'한다.
    그런 경우를 화면에 알리도록 판단 기록의 실패 결과와 충족 못 한 완료 조건을 모은다."""
    try:
        d = json.loads(get_store().read(project, version, "design.json") or "")
    except ValueError:
        return []
    failed = [x for x in d.get("decisions") or [] if re.search(r"실패|검토 못 함", x.get("result") or "")]
    checks = [c for c in (d.get("completion") or {}).get("checks") or [] if not c.get("passed")]
    return [*(f"#{x['step']} {x['action']}: {x['result']}" for x in failed),
            *(f"완료 조건 미충족 — {c['name']}: {c['detail']}" for c in checks)]


def _llm_errors(log: list[str]) -> list[str]:
    """판단 기록에는 예외 이름만 남으므로 실행 기록에서 LLM 오류 메시지를 찾아 앞에 보여준다. 자주 나오는 원인은 할 일을 같이 적는다."""
    messages: list[str] = []
    for line in log:
        m = re.search(r"Error code: (\d+) - .*?'message': '([^']+)'", line) or re.search(r"Error code: (\d+) - (.+)$", line)
        if m and (msg := f"{m[1]} {m[2]}") not in messages:
            messages.append(msg)
    out = []
    for m in messages[:3]:
        if re.search(r"usage limits", m, re.I):
            out.append(f"LLM 사용 한도 초과: {m} → Anthropic Console에서 워크스페이스 사용 한도를 올리거나 다시 열리는 날까지 기다리세요.")
        elif re.search(r"credit balance", m, re.I):
            out.append(f"LLM 크레딧 부족: {m} → 결제(크레딧)를 충전하세요.")
        elif re.search(r"^401|api[_ ]key", m, re.I):
            out.append(f"LLM 키 오류: {m} → .env의 API 키를 확인하세요.")
        else:
            out.append(f"LLM 오류: {m}")
    return out
