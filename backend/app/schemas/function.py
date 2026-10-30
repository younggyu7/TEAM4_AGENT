"""기능 명세: 요구사항(입력)을 구현 가능한 기능 단위 명세(출력)로 만든다.

  입력: Requirement·Question (schemas/requirement.py), 사람이 고친 기능 명세(design.json)
  출력: FunctionSpec (행위자·입출력·업무 규칙·흐름·데이터 객체), 데이터 모델 Agent의 입력이 된다
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class FunctionIO(BaseModel):
    """기능의 입력·출력 항목."""
    name: str
    description: str = ""
    required: bool = True


class ExceptionFlow(BaseModel):
    """예외 흐름: 조건 → 처리."""
    condition: str
    handling: str


class FunctionSpec(BaseModel):
    """기능 명세 하나. requirement_ids로 근거 요구사항에, data_objects로 데이터 모델 설계에 이어진다."""
    function_id: str
    group: str                           # 생성 단위 (보통 원문 요구사항 번호)
    name: str
    description: str
    requirement_ids: list[str]
    nfr_ids: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    inputs: list[FunctionIO] = Field(default_factory=list)
    outputs: list[FunctionIO] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    normal_flow: list[str] = Field(default_factory=list)
    exception_flows: list[ExceptionFlow] = Field(default_factory=list)
    data_objects: list[str] = Field(default_factory=list)   # 데이터 모델 Agent 입력
