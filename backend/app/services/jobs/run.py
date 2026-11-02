"""문서 업로드 작업: 올린 고객 문서로 기능 명세 초안까지 실행한다 (`run <문서> --defer --until function_designer`)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.services.jobs.common import Job, _Runner, _new_job, _path_arg


def start_run(*, output_dir: Path, document: Path, project: str, title: str = "", model: str = "", effort: str = "",
              on_done: Callable[[str], None] | None = None) -> Job:
    """업로드한 문서로 기능 명세 초안까지 실행한다 (확인 질문은 추천안으로 가정). ERD는 사람이 기능 명세를 확정하고
    '내보내기 → ERD 만들기'를 할 때 만들고, DDL은 그때마다 검증한다.
    model·effort(화면에서 고른 값, 비우면 .env)는 실행 설정에 남아 이 프로젝트의 ERD·최종 확정까지 같이 쓴다."""
    job = _new_job(project=project, base="", source="run", message=f"{document.name}을(를) 분석하는 중")
    args = ["run", _path_arg(document), "--project", project, "--defer", "--until", "function_designer",
            "--output-dir", _path_arg(output_dir),
            *(["--title", title] if title else []), *(["--model", model] if model else []),
            *(["--effort", effort] if effort else [])]
    _Runner(job, project, output_dir, on_done=on_done).start(args)
    return job
