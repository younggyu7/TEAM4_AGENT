"""ERD 최종 확정 작업: `review <프로젝트> v<버전> [--dispositions]` → 최종본 v<버전>.1 (DDL 검증·추적 검토·API 설계)."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from app.services.jobs.common import Job, _Runner, _new_job, _path_arg, _temp_file


def start_review(*, output_dir: Path, project: str, version: str, dispositions: dict | None = None,
                 on_done: Callable[[str], None] | None = None) -> Job:
    """ERD 최종 확정: 사람이 정한 검토 지적 처리를 남기고 그 ERD 아래 최종본(v1.1 → v1.1.1)을 만든다 (의미 검토는 다시 하지 않음)."""
    project_dir = (output_dir / project).resolve()
    root, name = project_dir.parent, project_dir.name
    job = _new_job(project=project, base=version, source="review", message=f"ERD v{version} 최종 확정: DDL을 검증하고 최종본을 만드는 중")
    temp: list[Path] = []
    extra = ["--dispositions", str(_temp_file(job, "dispositions.json", json.dumps(dispositions, ensure_ascii=False), temp))] \
        if dispositions else []
    _Runner(job, project, root, result="semantic_review.json", temp=temp, on_done=on_done).start(
        ["review", name, f"v{version}", "--output-dir", _path_arg(root), *extra])
    return job
