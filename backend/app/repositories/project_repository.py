"""프로젝트·프로젝트 파일 읽기·쓰기 (세션 안에서, 커밋은 호출한 쪽의 session_scope가 한다)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models import Artifact, Project, ProjectFile


def ensure(session: Session, project: str) -> None:
    """프로젝트 행이 없으면 만든다 (동시에 만들어도 한 번만)."""
    session.execute(insert(Project).values(id=project).on_conflict_do_nothing(index_elements=["id"]))


def lock(session: Session, project: str) -> None:
    """프로젝트 행을 잠근다 (버전 번호 잡기처럼 같은 프로젝트에서 한 번에 하나만 해야 하는 일)."""
    ensure(session, project)
    session.execute(select(Project.id).where(Project.id == project).with_for_update())


def exists(session: Session, project: str) -> bool:
    return session.get(Project, project) is not None


def with_design(session: Session) -> list[str]:
    """design.json 산출물이 있는 버전을 가진 프로젝트 ID."""
    rows = session.execute(select(Artifact.project_id).where(Artifact.name == "design.json").distinct()).scalars()
    return sorted(rows)


def read_file(session: Session, project: str, name: str) -> str | None:
    row = session.get(ProjectFile, (project, name))
    return None if row is None else row.content


def write_file(session: Session, project: str, name: str, text: str) -> None:
    ensure(session, project)
    session.execute(insert(ProjectFile).values(project_id=project, name=name, content=text)
                    .on_conflict_do_update(index_elements=["project_id", "name"], set_={"content": text, "updated_at": utcnow()}))
