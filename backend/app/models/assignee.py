"""프로젝트 담당자 (services/project_access.py): 담당자로 고른 회원만 그 프로젝트를 보고 쓴다 (PM도 고른 PM만, 최소 한 명)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class ProjectMember(Base):
    __tablename__ = "project_members"

    project: Mapped[str] = mapped_column(String(300), primary_key=True)    # 프로젝트 ID (산출물 저장소의 이름)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
