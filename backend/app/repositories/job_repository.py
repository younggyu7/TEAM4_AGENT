"""작업 진행 기록 (세션 안에서). 작업 실행기가 상태가 바뀔 때마다 저장한다."""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import JobRecord

FIELDS = ("project", "base", "source", "status", "message", "log", "new_version", "warnings", "started_at", "finished_at")


def save(session: Session, job: dict) -> None:
    """작업 기록을 넣거나 바꾼다 (job은 Job.to_dict()와 같은 dict)."""
    values = {k: job.get(k) for k in FIELDS}
    values["log"], values["warnings"] = values["log"] or [], values["warnings"] or []
    session.execute(insert(JobRecord).values(id=job["id"], **values)
                    .on_conflict_do_update(index_elements=["id"], set_=values))


def get(session: Session, job_id: str) -> dict | None:
    row = session.get(JobRecord, job_id)
    return None if row is None else {"id": row.id, **{k: getattr(row, k) for k in FIELDS}}


def running(session: Session, project: str) -> dict | None:
    row = session.execute(select(JobRecord).where(JobRecord.project == project, JobRecord.status == "running")).scalars().first()
    return None if row is None else get(session, row.id)


def fail_running(session: Session, message: str, finished_at: str) -> int:
    """서버가 다시 시작되어 추적을 놓친 진행 중 작업을 실패로 닫는다. 닫은 수."""
    result = session.execute(update(JobRecord).where(JobRecord.status == "running")
                             .values(status="failed", message=message, finished_at=finished_at))
    return result.rowcount or 0
