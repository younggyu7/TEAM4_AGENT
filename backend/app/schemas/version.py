"""설계 버전: 화면 요청(입력)으로 새 버전을 만들고 버전 기록(출력)을 남긴다.

  입력 (HTTP 요청 본문): VersionCreate (기능 명세 내보내기·ERD 저장·답 반영), ReviewCreate (ERD 최종 확정),
                        TitleUpdate (버전 제목)
  출력: DesignVersion (versions.json의 버전 기록). 응답은 design.json·버전 기록을 그대로 돌려준다.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class DesignVersion(BaseModel):
    """설계 버전 기록: 부모 버전, 변경 요약, 산출물 폴더. 번호는 '2', '2.1' 같은 문자열 (예전 숫자 번호도 받음)."""
    model_config = ConfigDict(coerce_numbers_to_str=True)
    design_version: str
    parent: str | None
    created_at: str
    summary: list[str]
    output_dir: str


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
