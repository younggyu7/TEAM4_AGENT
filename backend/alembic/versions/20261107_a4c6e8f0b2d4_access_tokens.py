"""개인 접속 토큰 (VS Code 확장이 Authorization: Bearer로 접속)

Revision ID: a4c6e8f0b2d4
Revises: f3b5d7e9a1c2
Create Date: 2026-11-07 10:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a4c6e8f0b2d4'
down_revision = 'f3b5d7e9a1c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("prefix", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_access_tokens_user_id", "access_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_access_tokens_user_id", table_name="access_tokens")
    op.drop_table("access_tokens")
