"""산출물: 설계 버전마다 이름으로 읽고 쓰는 텍스트 파일 (design.json, schema.sql, functions.md …)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "version_label"], ["design_versions.project_id", "design_versions.label"],
                             ondelete="CASCADE"),
    )

    project_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    version_label: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now())
