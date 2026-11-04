"""프로젝트별 Slack 채널과 이미 올린 알림 (services/slack.py). 봇 토큰은 .env SLACK_BOT_TOKEN 하나 (팀 워크스페이스).

  slack_projects       프로젝트마다 만든 비공개 채널·담당자 (채널을 아직 못 만들었으면 channel_id가 빈 값)
  slack_notifications  채널에 이미 올린 '나에게 온 알림' 키 (같은 알림을 두 번 올리지 않게)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class SlackProject(Base):
    __tablename__ = "slack_projects"

    project: Mapped[str] = mapped_column(String(300), primary_key=True)    # 프로젝트 ID (산출물 저장소의 이름)
    team: Mapped[str] = mapped_column(String(200), default="", server_default="")          # 워크스페이스 이름
    channel_id: Mapped[str] = mapped_column(String(50), default="", server_default="")     # 만든 비공개 채널
    channel_name: Mapped[str] = mapped_column(String(100), default="", server_default="")
    members: Mapped[list] = mapped_column(JSON, default=list)              # 담당자 = 채널에 초대한 회원 ID (users.id)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())


class SlackNotification(Base):
    __tablename__ = "slack_notifications"

    key: Mapped[str] = mapped_column(String(400), primary_key=True)   # 종류|프로젝트|화면|버전|시각 (services/slack.notice_key)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True)
