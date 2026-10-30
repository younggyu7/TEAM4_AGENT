"""산출물 저장소: 포트(ArtifactStore)와 어댑터 선택.

get_store()는 설정에 맞는 어댑터를 돌려준다. set_store()로 바꿔 끼울 수 있다 (테스트·다른 저장소).
LLM 포트(agents/llm)의 get_llm()·set_llm()과 같은 방식이다.
  - DATABASE_URL(.env)이 있으면 PostgreSQL 어댑터 (서비스 DB의 projects·design_versions·artifacts 테이블)
  - 없으면 로컬 폴더 어댑터 (OUTPUT_DIR, CLI --output-dir)
"""
from __future__ import annotations

from functools import cache

from app.agents.storage.local import LocalArtifactStore
from app.agents.storage.port import ArtifactStore
from app.core.config import get_settings

_override: ArtifactStore | None = None


@cache
def _postgres(url: str) -> ArtifactStore:
    from app.agents.storage.postgres import PostgresArtifactStore   # DB를 쓸 때만 SQLAlchemy를 불러온다
    return PostgresArtifactStore(url)


def get_store() -> ArtifactStore:
    """지금 쓸 산출물 저장소. 바꿔 끼운 것이 없으면 설정을 따른다 (설정이 바뀌면 따라간다)."""
    if _override is not None:
        return _override
    s = get_settings()
    return _postgres(s.database_url) if s.database_url else LocalArtifactStore(s.output_dir)


def set_store(store: ArtifactStore | None) -> None:
    """산출물 저장소를 바꿔 끼운다 (None이면 설정을 따른다)."""
    global _override
    _override = store


__all__ = ["ArtifactStore", "LocalArtifactStore", "get_store", "set_store"]
