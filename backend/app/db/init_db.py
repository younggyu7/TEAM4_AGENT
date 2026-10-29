from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def init_db() -> None:
    """스키마는 Alembic 마이그레이션이 단일 기준입니다(create_all 사용 안 함)."""
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
