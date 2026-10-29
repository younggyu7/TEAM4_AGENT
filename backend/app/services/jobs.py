"""새 버전 만들기 작업: 화면의 요청을 CLI(`python -m app …`) 하위 프로세스로 실행하고 진행 상황을 기록한다.

- 문서 업로드: `run <문서> --defer --until function_designer` → 기능 명세 초안 첫 버전 (ERD는 사람이 확정한 뒤)
- 기능 명세 화면에서 확인 질문에 새로 답함: `rerun function_designer --answers` (추천과 다른 답이 바꾼 것만 다시 설계)
- 기능 명세 내보내기 / ERD 저장: `rerun data_modeler` (확정한 기능 명세는 --design, 고친 데이터 모델은 --data-model, 의견은 --feedback)
- ERD 최종 확정: `review <프로젝트> v<버전>` → 그 버전 폴더에 의미 검토 결과 (새 버전 없음)
- 확인 질문에서 멈추면(종료 코드 3) `resume --defer`로 보류(가정) 처리하고 이어 간다.

LLM 작업은 몇 분씩 걸리므로 서버 안에서 돌리지 않고 하위 프로세스로 띄운다. 작업 기록은 메모리에만 있어 서버를 다시 띄우면 사라진다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path

from app.core.security import redact
from app.services.output_store import version_key, version_labels

BACKEND_ROOT = Path(__file__).resolve().parents[2]
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


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def running_job(project: str) -> Job | None:
    return next((j for j in _jobs.values() if j.project == project and j.status == "running"), None)


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "app", *args]


def _path_arg(path: Path) -> str:
    """backend 안의 경로는 상대 경로로 넘긴다 (versions.json의 output_dir가 폴더를 옮겨도 맞게)"""
    path = path.resolve()
    return str(path.relative_to(BACKEND_ROOT)) if path.is_relative_to(BACKEND_ROOT) else str(path)


@cache
def capabilities() -> dict:
    """백엔드 rerun이 기능 명세 수정본을 받는지 (옵션이 추가되면 자동으로 그걸 쓴다)"""
    out = subprocess.run(_cli("rerun", "-h"), cwd=BACKEND_ROOT, capture_output=True, text=True).stdout
    return {"design": bool(re.search(r"--design\b", out)), "spec_md": bool(re.search(r"--spec-md\b", out))}


class _Runner:
    """작업 하나: CLI를 실행하고, 확인 질문에서 멈추면 resume --defer로 이어 가고, 끝나면 새 버전을 찾는다."""

    def __init__(self, job: Job, project_dir: Path, output_dir: Path, *, submitted_md: str | None = None,
                 pre: list[str] | None = None, temp: list[Path] | None = None,
                 on_done: Callable[[str], None] | None = None, result: Path | None = None):
        self.job, self.project_dir, self.output_dir = job, project_dir, output_dir
        self.submitted_md, self.pre, self.temp, self.on_done = submitted_md, pre or [], temp or [], on_done
        self.result = result     # 새 버전 대신 이 파일을 만드는 작업 (의미 검토)
        self.before = version_labels(project_dir)

    def start(self, args: list[str]) -> None:
        threading.Thread(target=self._run, args=(args,), daemon=True).start()

    def _push(self, line: str) -> None:
        if line.strip():
            self.job.log.append(redact(line.rstrip()))
            del self.job.log[:-MAX_LOG]

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
                    code = self._exec(["resume", run_id, "--defer", "--output-dir", _path_arg(self.output_dir)])
            if code == 0:
                self._reviewed() if self.result else self._done()
            else:
                self.job.warnings = [*_llm_errors(self.job.log), *self.pre]
                reason = EXIT_MESSAGE.get(code, f"실행이 실패했습니다 (종료 코드 {code}).")
                if self.result and code == 2:
                    reason = "의미 검토를 끝내지 못했습니다. 아래 기록의 마지막 줄을 확인하세요."
                self._finish("failed", reason)
        except Exception as e:  # noqa: BLE001 - 작업 스레드의 오류는 화면에 보여 준다
            self._push(repr(e))
            self._finish("failed", f"작업 중 오류가 났습니다: {e}")

    def _done(self) -> None:
        created = version_labels(self.project_dir) - self.before
        if not created:
            return self._finish("failed", "실행은 끝났지만 새 버전 폴더를 찾지 못했습니다.")
        newest = max(created, key=version_key)
        folder = self.project_dir / f"v{newest}"
        self.job.new_version = newest
        # Agent에 넘긴 기능 명세를 새 버전 폴더에 남긴다 (어떤 명세로 만든 ERD인지 확인용)
        if self.submitted_md:
            (folder / "functions.submitted.md").write_text(self.submitted_md, encoding="utf-8")
        self.job.warnings = [*_llm_errors(self.job.log), *self.pre, *_run_warnings(folder / "design.json")]
        if self.on_done:
            self.on_done(newest)
        self._finish("done", f"v{newest}을 만들었지만 Agent 단계 일부가 실패했습니다." if self.job.warnings
                     else f"v{newest}을 만들었습니다.")

    def _reviewed(self) -> None:
        try:
            data = json.loads(self.result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._finish("failed", "검토는 끝났지만 결과 파일(semantic_review.json)을 찾지 못했습니다.")
        self.job.warnings = _llm_errors(self.job.log)
        self._finish("done", f"의미 검토를 마쳤습니다: 지적 {len(data.get('remarks') or [])}건 (최종 산출물에서 확인)")

    def _finish(self, status: str, message: str) -> None:
        self.job.status, self.job.message = status, message
        self.job.finished_at = datetime.now().isoformat(timespec="seconds")
        for f in self.temp:
            f.unlink(missing_ok=True)


def _new_job(**kw) -> Job:
    job = Job(id=f"job-{uuid.uuid4().hex[:10]}", status="running", **kw)
    with _lock:
        _jobs[job.id] = job
    return job


def _temp_file(job: Job, name: str, body: str, temp: list[Path]) -> Path:
    f = Path(tempfile.gettempdir()) / f"req-agent-{job.id}-{name}"
    f.write_text(body, encoding="utf-8")
    temp.append(f)
    return f


def start_rerun(*, output_dir: Path, project: str, base: str, source: str, data_model=None, design=None,
                spec_md: str | None = None, spec_summary: str | None = None, answers: dict | None = None,
                feedback: str | None = None, on_done: Callable[[str], None] | None = None) -> Job:
    """기준 버전에서 한 단계만 다시 만들어 새 버전(v4.1 …)을 만든다."""
    project_dir = (output_dir / project).resolve()
    # rerun은 '--output-dir 아래의 프로젝트 이름'으로 찾으므로 한 단계 위 폴더와 이름으로 나눈다
    rerun_root, name = project_dir.parent, project_dir.name
    if source == "spec" and answers:
        source = "answers"
    job = _new_job(project=project, base=base, source=source,
                   message=f"기능 명세 v{base}에 확인 질문 답 {len(answers or {})}건을 반영하는 중" if source == "answers"
                   else f"기능 명세 v{base}로 ERD를 만드는 중" if source == "spec" else f"ERD v{base}에서 새 버전을 만드는 중")
    temp: list[Path] = []
    pre: list[str] = []   # 시작 전에 정해지는 안내 (완료 화면 경고에 합친다)
    model_file = _temp_file(job, "data_model.json", json.dumps(data_model, ensure_ascii=False, indent=2), temp) \
        if data_model else None
    feedback = (feedback or "").strip()
    summary = (spec_summary or "").strip()
    spec_args: list[str] = []
    if source == "answers":
        # 기능 명세 수정은 rerun function_designer가 파일로 받지 않으므로 변경 요약을 의견으로 함께 넘긴다
        spec_args += ["--answers", str(_temp_file(job, "answers.json", json.dumps(answers, ensure_ascii=False), temp))]
        if summary:
            feedback = "\n\n".join(x for x in (summary, feedback) if x)
            pre.append("기능 명세 수정 내용은 Agent 의견으로 넘겼습니다 (그 수정과 관련된 기능만 다시 설계).")
    elif source == "spec":
        caps = capabilities()
        if caps["design"] and design:
            spec_args += ["--design", str(_temp_file(job, "design.json", json.dumps(design, ensure_ascii=False), temp))]
        if caps["spec_md"] and spec_md:
            spec_args += ["--spec-md", str(_temp_file(job, "functions.md", spec_md, temp))]
        if not caps["design"] and not caps["spec_md"] and summary:
            feedback = "\n\n".join(x for x in (summary, feedback) if x)
            pre.append("백엔드 rerun에 기능 명세 수정본 옵션(--design·--spec-md)이 아직 없어 수정 내용을 Agent 의견으로 넘겼습니다.")
        # 수정도 의견도 없으면 이 기능 명세로 ERD를 처음부터 설계한다
        if not spec_args and not feedback and not model_file:
            spec_args.append("--full")

    args = ["rerun", "function_designer" if source == "answers" else "data_modeler", name, "--from", f"v{base}",
            "--output-dir", _path_arg(rerun_root),
            *(["--data-model", str(model_file)] if model_file else []),
            *spec_args,
            *(["--feedback", feedback] if feedback else [])]
    _Runner(job, project_dir, rerun_root, submitted_md=spec_md if source == "spec" else None, pre=pre, temp=temp,
            on_done=on_done).start(args)
    return job


def start_run(*, output_dir: Path, document: Path, project: str, title: str = "", validate_db: bool = False) -> Job:
    """업로드한 문서로 기능 명세 초안까지 실행한다 (확인 질문은 추천안으로 가정). ERD는 사람이 기능 명세를 확정하고
    '내보내기 → ERD 만들기'를 할 때 만든다. validate_db는 그때 이어받는다."""
    job = _new_job(project=project, base="", source="run", message=f"{document.name}을(를) 분석하는 중")
    args = ["run", _path_arg(document), "--project", project, "--defer", "--until", "function_designer",
            "--output-dir", _path_arg(output_dir),
            *(["--title", title] if title else []), *(["--validate-db"] if validate_db else [])]
    _Runner(job, output_dir / project, output_dir).start(args)
    return job


def start_review(*, output_dir: Path, project: str, version: str) -> Job:
    """ERD 최종 확정: 그 버전의 데이터 모델을 기능 명세와 대조하는 의미 검토를 한 번 한다 (결과는 그 버전 폴더)."""
    project_dir = (output_dir / project).resolve()
    root, name = project_dir.parent, project_dir.name
    job = _new_job(project=project, base=version, source="review", message=f"ERD v{version}을 기능 명세와 대조해 의미 검토하는 중")
    _Runner(job, project_dir, root, result=project_dir / f"v{version}" / "semantic_review.json").start(
        ["review", name, f"v{version}", "--output-dir", _path_arg(root)])
    return job


def _run_warnings(design_file: Path) -> list[str]:
    """실행은 끝났어도 Agent가 단계를 반복 실패하면 남은 결과로 '현재 상태로 제공'한다.
    그런 경우를 화면에 알리도록 판단 기록의 실패 결과와 충족 못 한 완료 조건을 모은다."""
    try:
        d = json.loads(design_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
