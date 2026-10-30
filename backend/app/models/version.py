"""설계 버전: 번호(1, 1.1, 1.1.1)·버전 기록·화면에서 붙인 제목.

번호를 잡으면(claim) 행이 먼저 생기고 record는 비어 있다. 산출물을 다 쓰고 기록을 남기면 record가 채워진다
(로컬 폴더 어댑터의 'v<번호> 폴더 + versions.json 항목'과 같은 뜻).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class DesignVersion(Base):
    __tablename__ = "design_versions"

    project_id: Mapped[str] = mapped_column(String(200), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    label: Mapped[str] = mapped_column(String(50), primary_key=True)
    record: Mapped[dict | None] = mapped_column(JSONB)     # versions.json의 버전 항목 (부모·단계·실행 ID·DB 검증·확정 …)
    title: Mapped[str | None] = mapped_column(Text)       # 화면에서 붙인 제목 (titles.json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
