"""Slack 봇 토큰을 프로젝트마다 두지 않고 .env SLACK_BOT_TOKEN 하나로 (팀 워크스페이스 하나): slack_projects.token 삭제

Revision ID: d2f4b6a8c0e1
Revises: 9c3d5e7f1a20
Create Date: 2026-11-04 18:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd2f4b6a8c0e1'
down_revision = '9c3d5e7f1a20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('slack_projects', 'token')


def downgrade() -> None:
    op.add_column('slack_projects', sa.Column('token', sa.String(length=300), server_default='', nullable=False))
