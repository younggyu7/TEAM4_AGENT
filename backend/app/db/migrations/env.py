import logging
from logging.config import fileConfig

from alembic import context

from app.core.config import get_settings
from app.models import Base

config = context.config
# CLI로 실행할 때만 alembic.ini 로깅을 씁니다. 앱(lifespan)에서 호출되면 앱 로깅을 유지합니다.
if config.config_file_name and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from app.db.session import engine

    with engine.connect() as connection:
        # render_as_batch: SQLite에서도 ALTER 계열 마이그레이션이 동작하게 합니다.
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
