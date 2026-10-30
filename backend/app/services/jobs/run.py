"""문서 업로드 작업: 올린 고객 문서로 기능 명세 초안까지 실행한다 (`run <문서> --defer --until function_designer`)."""
from __future__ import annotations

from pathlib import Path

from app.services.jobs.common import Job, _Runner, _new_job, _path_arg


def start_run(*, output_dir: Path, document: Path, project: str, title: str = "", validate_db: bool = False) -> Job:
    """업로드한 문서로 기능 명세 초안까지 실행한다 (확인 질문은 추천안으로 가정). ERD는 사람이 기능 명세를 확정하고
    '내보내기 → ERD 만들기'를 할 때 만든다. validate_db는 그때 이어받는다."""
    job = _new_job(project=project, base="", source="run", message=f"{document.name}을(를) 분석하는 중")
    args = ["run", _path_arg(document), "--project", project, "--defer", "--until", "function_designer",
            "--output-dir", _path_arg(output_dir),
            *(["--title", title] if title else []), *(["--validate-db"] if validate_db else [])]
    _Runner(job, project, output_dir).start(args)
    return job
