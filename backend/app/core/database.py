"""서비스 DB(PostgreSQL) 연결과 트랜잭션.

DATABASE_URL(.env)이 있을 때만 쓴다. 없으면 산출물은 output 폴더, 체크포인트는 SQLite다 (로컬·테스트).
  - SQLAlchemy는 'postgresql+psycopg://…', LangGraph 체크포인트(psycopg)는 'postgresql://…'를 쓰므로 둘 다 만들어 준다.
  - session_scope(): 블록 하나가 트랜잭션 하나 (끝나면 커밋, 오류면 롤백). 저장소(repositories)는 이 안에서 쓴다.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def sqlalchemy_url(url: str) -> str:
    """postgresql://… → postgresql+psycopg://… (SQLAlchemy가 psycopg 3을 쓰게)."""
    return "postgresql+psycopg://" + url.split("://", 1)[1] if url.startswith(("postgresql://", "postgres://")) else url


def psycopg_url(url: str) -> str:
    """postgresql+psycopg://… → postgresql://… (LangGraph PostgresSaver·psycopg 직접 접속용)."""
    return "postgresql://" + url.split("://", 1)[1] if url.startswith("postgresql+") else url


def database_enabled() -> bool:
    return bool(get_settings().database_url)


@cache
def _engine(url: str) -> Engine:
    return create_engine(sqlalchemy_url(url), pool_pre_ping=True)


@cache
def _sessions(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=_engine(url), expire_on_commit=False)


def get_engine() -> Engine:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL이 없습니다 (.env에 서비스 DB 주소를 넣으세요).")
    return _engine(url)


@contextmanager
def session_scope() -> Iterator[Session]:
    """트랜잭션 하나: 블록이 끝나면 커밋하고, 오류가 나면 롤백한다."""
    get_engine()
    session = _sessions(get_settings().database_url)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
