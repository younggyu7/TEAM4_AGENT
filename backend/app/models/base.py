"""ORM 공통: 모든 테이블의 Base와 시각 컬럼 기본값. Alembic은 Base.metadata로 테이블을 비교한다."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
