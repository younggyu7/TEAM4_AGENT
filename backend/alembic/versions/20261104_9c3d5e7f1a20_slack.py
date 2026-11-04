"""Slack 알림: 회원 Slack 멤버 ID, 프로젝트별 Slack 연결(봇 토큰·채널·담당자), 채널에 올린 알림 기록

Revision ID: 9c3d5e7f1a20
Revises: 7b1e4c2a9d10
Create Date: 2026-11-04 17:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '9c3d5e7f1a20'
down_revision = '7b1e4c2a9d10'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('slack_id', sa.String(length=50), server_default='', nullable=False))
    op.create_table('slack_projects',
    sa.Column('project', sa.String(length=300), nullable=False),
    sa.Column('token', sa.String(length=300), nullable=False),
    sa.Column('team', sa.String(length=200), server_default='', nullable=False),
    sa.Column('channel_id', sa.String(length=50), server_default='', nullable=False),
    sa.Column('channel_name', sa.String(length=100), server_default='', nullable=False),
    sa.Column('members', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('project')
    )
    op.create_table('slack_notifications',
    sa.Column('key', sa.String(length=400), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_index(op.f('ix_slack_notifications_sent_at'), 'slack_notifications', ['sent_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_slack_notifications_sent_at'), table_name='slack_notifications')
    op.drop_table('slack_notifications')
    op.drop_table('slack_projects')
    op.drop_column('users', 'slack_id')
