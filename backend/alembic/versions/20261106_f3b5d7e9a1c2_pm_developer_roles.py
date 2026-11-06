"""역할을 PM·개발자 둘로: 팀장 → PM, 기획·데이터·개발 파트장·파트원 → 개발자 (users.role·requested_role)

Revision ID: f3b5d7e9a1c2
Revises: e7a9c1b3d5f2
Create Date: 2026-11-06 15:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f3b5d7e9a1c2'
down_revision = 'e7a9c1b3d5f2'
branch_labels = None
depends_on = None

OLD_TO_NEW = {"lead": "pm", "planner": "developer", "planner_head": "developer", "data": "developer",
              "data_head": "developer", "dev": "developer", "dev_head": "developer"}
NEW_TO_OLD = {"pm": "lead", "developer": "planner"}   # 되돌릴 때 (파트는 알 수 없어 기획 파트원으로)


def _convert(mapping: dict[str, str]) -> None:
    for column in ("role", "requested_role"):
        for old, new in mapping.items():
            op.execute(sa.text(f"UPDATE users SET {column} = :new WHERE {column} = :old").bindparams(new=new, old=old))


def upgrade() -> None:
    _convert(OLD_TO_NEW)


def downgrade() -> None:
    _convert(NEW_TO_OLD)
