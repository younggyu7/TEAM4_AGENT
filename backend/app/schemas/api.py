"""HTTP 요청 본문. 응답은 design.json·버전 기록을 그대로 돌려주므로 따로 정의하지 않는다 (원본 계약은 schemas/design.py)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class TitleUpdate(BaseModel):
    project: str
    version: str
    title: str = ""


class ReviewCreate(BaseModel):
    """ERD 최종 확정: 그 버전의 데이터 모델을 기능 명세와 대조하는 의미 검토를 한 번 한다."""
    project: str
    version: str


class VersionCreate(BaseModel):
    """기능 명세 내보내기(source: spec) 또는 ERD 저장(source: erd)으로 새 버전 만들기.
    source: spec에 answers가 있으면 확인 질문 답을 반영해 기능 명세 새 버전을 만든다."""
    project: str
    base: str
    source: Literal["spec", "erd"] = "erd"
    data_model: Any = None          # 사용자가 고친 data_model.json (ERD 저장)
    design: Any = None              # 기능 명세 수정본이 들어간 design.json (기능 명세 내보내기)
    functions_md: str | None = None  # Agent에 넘길 기능 명세 Markdown
    spec_summary: str | None = None  # 백엔드가 수정본을 못 받을 때 의견으로 넘길 변경 요약
    answers: dict[str, str] | None = None  # 기능 명세 화면에서 새로 답한 확인 질문 (질문 ID → 답)
    feedback: str | None = None
    title: str | None = None
