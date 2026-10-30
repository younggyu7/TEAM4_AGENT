"""ERD 최종 확정 작업: `review <프로젝트> v<버전>` → 최종본 v<버전>.1 (추적·의미 검토 결과와 그 사용량)."""
from __future__ import annotations

from pathlib import Path

from app.services.jobs.common import Job, _Runner, _new_job, _path_arg


def start_review(*, output_dir: Path, project: str, version: str) -> Job:
    """ERD 최종 확정: 그 ERD 아래 최종본(v1.1 → v1.1.1)을 만들고 추적 검토·의미 검토(ERD ↔ 기능 명세)를 한 번 한다."""
    project_dir = (output_dir / project).resolve()
    root, name = project_dir.parent, project_dir.name
    job = _new_job(project=project, base=version, source="review", message=f"ERD v{version} 최종 확정: 최종본을 만들고 기능 명세와 대조해 검토하는 중")
    _Runner(job, project, root, result="semantic_review.json").start(
        ["review", name, f"v{version}", "--output-dir", _path_arg(root)])
    return job
