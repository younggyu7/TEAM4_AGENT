"""작업 기록: 화면 요청으로 띄운 CLI 하위 프로세스 작업의 진행 상황 (서버를 다시 띄워도 남는다)."""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project: Mapped[str] = mapped_column(String(200), index=True)
    base: Mapped[str] = mapped_column(String(50), default="")
    source: Mapped[str] = mapped_column(String(20))           # run / spec / erd / answers / review
    status: Mapped[str] = mapped_column(String(20), index=True)   # running / done / failed
    message: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSONB, default=list)
    new_version: Mapped[str | None] = mapped_column(String(50))
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    started_at: Mapped[str] = mapped_column(String(40))       # 화면과 같은 ISO 문자열 (작업 기록 dataclass 그대로)
    finished_at: Mapped[str | None] = mapped_column(String(40))
