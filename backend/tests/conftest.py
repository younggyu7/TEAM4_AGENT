import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SAMPLE_TEXT = """IT 서비스 요청 관리 시스템 요구사항

1. 사용자는 IT 서비스 요청을 등록할 수 있어야 한다. 요청에는 제목과 내용이 포함된다.

2. 서비스데스크 담당자는 접수된 요청에 처리 담당자를 배정한다.

3. 모든 화면은 사용자 요청 후 3초 이내에 응답해야 한다.
"""


@pytest.fixture
def sample_doc(tmp_path) -> Path:
    path = tmp_path / "sample.txt"
    path.write_text(SAMPLE_TEXT, encoding="utf-8")
    return path


def _sample(name: str) -> Path:
    """원본 RFP 샘플 위치: data/ 또는 samples/ (data/는 정제본 입력용으로 바뀔 수 있어 원본은 samples/에 둬도 된다)."""
    for folder in ("data", "samples"):
        if (ROOT / folder / name).exists():
            return ROOT / folder / name
    return ROOT / "samples" / name


@pytest.fixture
def rfp_path() -> Path:
    return _sample("붙임 2. 제안요청서(ITSM 구축).hwpx")


@pytest.fixture
def kobaco_path() -> Path:
    return _sample("2. 제안요청서_KOBACO 정보시스템 통합유지운영.hwpx")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from app.core import config
    from app.agents.llm import client as llm

    # 테스트는 가짜 LLM을 쓰지만 공급자 선택이 키 존재를 확인하므로, .env 키가 없는 환경(CI)에서도 돌게 한다
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config._settings = config.Settings(output_dir=tmp_path / "output", validation_db_url="")
    yield
    config._settings = None
    llm.set_llm(None)
