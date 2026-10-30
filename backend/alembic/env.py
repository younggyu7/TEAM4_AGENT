"""Alembic 실행 환경: DB 주소는 .env의 DATABASE_URL, 테이블 정의는 app/models (Base.metadata).

LangGraph 체크포인트 테이블(checkpoints·checkpoint_blobs·checkpoint_writes·checkpoint_migrations)은
PostgresSaver.setup()이 관리하므로 비교·생성 대상에서 뺀다.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.core.database import sqlalchemy_url
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

url = get_settings().database_url
if not url:
    raise RuntimeError("DATABASE_URL이 없습니다. .env에 서비스 DB 주소를 넣고 다시 실행하세요.")
config.set_main_option("sqlalchemy.url", sqlalchemy_url(url).replace("%", "%%"))
target_metadata = Base.metadata
LANGGRAPH_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and name in LANGGRAPH_TABLES)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
