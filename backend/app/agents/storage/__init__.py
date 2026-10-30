"""산출물 저장소: 포트(ArtifactStore)와 어댑터 선택.

get_store()는 설정(OUTPUT_DIR, CLI --output-dir)의 로컬 폴더 어댑터를 돌려준다. set_store()로 바꿔 끼울 수 있다
(다른 저장소 어댑터·테스트). LLM 포트(agents/llm)의 get_llm()·set_llm()과 같은 방식이다.
"""
from __future__ import annotations

from app.agents.storage.local import LocalArtifactStore
from app.agents.storage.port import ArtifactStore
from app.core.config import get_settings

_override: ArtifactStore | None = None


def get_store() -> ArtifactStore:
    """지금 쓸 산출물 저장소. 바꿔 끼운 것이 없으면 설정의 output 폴더 (설정이 바뀌면 따라간다)."""
    return _override or LocalArtifactStore(get_settings().output_dir)


def set_store(store: ArtifactStore | None) -> None:
    """산출물 저장소를 바꿔 끼운다 (None이면 설정의 로컬 폴더로 되돌린다)."""
    global _override
    _override = store


__all__ = ["ArtifactStore", "LocalArtifactStore", "get_store", "set_store"]
