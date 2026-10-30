"""설계 버전 번호·버전 기록·제목 (세션 안에서)."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import DesignVersion
from app.repositories import project_repository


def _key(label: str) -> tuple[int, ...]:
    return tuple(int(x) for x in label.split("."))


def labels(session: Session, project: str) -> set[str]:
    """번호를 잡은 버전 (기록 전 포함)."""
    return set(session.execute(select(DesignVersion.label).where(DesignVersion.project_id == project)).scalars())


def ensure(session: Session, project: str, label: str) -> None:
    """버전 행이 없으면 만든다 (산출물을 쓰거나 가져올 때)."""
    project_repository.ensure(session, project)
    session.execute(insert(DesignVersion).values(project_id=project, label=label)
                    .on_conflict_do_nothing(index_elements=["project_id", "label"]))


def claim(session: Session, project: str, base: str | None) -> str:
    """다음 번호를 잡는다: base 아래(base.1 …) 또는 다음 최상위. 프로젝트 행을 잠가 동시에 잡아도 겹치지 않는다."""
    project_repository.lock(session, project)
    prefix = f"{base}." if base else ""
    pattern = re.compile(rf"{re.escape(prefix)}(\d+)")
    taken = [int(m.group(1)) for label in labels(session, project) if (m := pattern.fullmatch(label))]
    label = f"{prefix}{max(taken, default=0) + 1}"
    session.add(DesignVersion(project_id=project, label=label))
    session.flush()
    return label


def load_records(session: Session, project: str) -> list[dict]:
    """버전 기록 (versions.json의 versions와 같은 항목, 번호 순)."""
    rows = session.execute(select(DesignVersion).where(DesignVersion.project_id == project,
                                                       DesignVersion.record.is_not(None))).scalars()
    return [r.record for r in sorted(rows, key=lambda r: _key(r.label))]


def save_records(session: Session, project: str, records: list[dict]) -> None:
    """버전 기록 전체를 바꾼다 (목록에 없는 버전은 기록을 지우고 번호만 남긴다)."""
    by_label = {str(r["design_version"]): r for r in records}
    for label in by_label:
        ensure(session, project, label)
    for row in session.execute(select(DesignVersion).where(DesignVersion.project_id == project)).scalars():
        row.record = by_label.get(row.label)


def load_titles(session: Session, project: str) -> dict[str, str]:
    rows = session.execute(select(DesignVersion.label, DesignVersion.title)
                           .where(DesignVersion.project_id == project, DesignVersion.title.is_not(None)))
    return {label: title for label, title in rows}


def save_titles(session: Session, project: str, titles: dict[str, str]) -> None:
    """화면에서 붙인 버전 제목 전체를 바꾼다."""
    for label in titles:
        ensure(session, project, label)
    for row in session.execute(select(DesignVersion).where(DesignVersion.project_id == project)).scalars():
        row.title = titles.get(row.label)
