"""프로젝트 담당자 project_members: 담당자만 그 프로젝트를 본다 (팀장은 모두). 기존 프로젝트는 Slack 채널 담당자를 옮겨 온다

Revision ID: e7a9c1b3d5f2
Revises: d2f4b6a8c0e1
Create Date: 2026-11-05 10:00:00
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = 'e7a9c1b3d5f2'
down_revision = 'd2f4b6a8c0e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table('project_members',
    sa.Column('project', sa.String(length=300), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project', 'user_id')
    )
    op.create_index(op.f('ix_project_members_user_id'), 'project_members', ['user_id'], unique=False)
    # 지금까지는 Slack 채널 담당자가 곧 프로젝트 담당자였다 → 그대로 옮긴다 (팀장은 담당자가 아니어도 모두 본다)
    conn = op.get_bind()
    users = set(conn.execute(sa.text("SELECT id FROM users")).scalars())
    rows = []
    for project, members in conn.execute(sa.text("SELECT project, members FROM slack_projects")):
        ids = json.loads(members) if isinstance(members, str) else (members or [])
        rows += [{"project": project, "user_id": i} for i in dict.fromkeys(ids) if i in users]
    if rows:
        op.bulk_insert(table, rows)


def downgrade() -> None:
    op.drop_index(op.f('ix_project_members_user_id'), table_name='project_members')
    op.drop_table('project_members')
