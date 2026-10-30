"""버전별 산출물 파일 내용 (세션 안에서)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models import Artifact
from app.repositories import version_repository


def read(session: Session, project: str, version: str, name: str) -> str | None:
    row = session.get(Artifact, (project, version, name))
    return None if row is None else row.content


def write(session: Session, project: str, version: str, name: str, text: str) -> None:
    """산출물을 쓴다 (같은 이름이면 바꾼다). 버전 행이 없으면 만든다."""
    version_repository.ensure(session, project, version)
    session.execute(insert(Artifact).values(project_id=project, version_label=version, name=name, content=text)
                    .on_conflict_do_update(index_elements=["project_id", "version_label", "name"], set_={"content": text, "updated_at": utcnow()}))


def exists(session: Session, project: str, version: str, name: str) -> bool:
    return session.execute(select(Artifact.name).where(Artifact.project_id == project, Artifact.version_label == version,
                                                       Artifact.name == name)).first() is not None
