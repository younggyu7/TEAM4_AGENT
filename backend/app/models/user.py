"""회원과 로그인 세션 (화면 로그인·권한). 서비스 DB가 없으면 OUTPUT_DIR의 service.sqlite에 같은 테이블을 둔다."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)   # 소문자로 저장
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(30), default="")             # core/auth.ROLES (승인 전에는 빈 값)
    requested_role: Mapped[str] = mapped_column(String(30), default="")   # 가입할 때 적은 희망 역할 (관리자 참고용)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)         # 회원 승인·역할 지정
    status: Mapped[str] = mapped_column(String(20), default="pending")    # pending / active / disabled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())


class UserSession(Base):
    """로그인 세션: 쿠키에는 토큰 원문, DB에는 해시만 둔다."""
    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
