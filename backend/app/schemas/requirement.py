"""요구사항 추출·검토: 원문 추출 단위(입력)에서 요구사항과 고객 확인 질문(출력)을 만든다.

  입력: RequirementSource·DocumentBlock (schemas/document.py), 사용자 의견·질문 답
  출력: Requirement (근거 인용·검토 표시·결정 포함), Question (확인 질문·추천안·답)
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RequirementType = Literal["FUNCTIONAL", "NON_FUNCTIONAL"]
RequirementScope = Literal["SYSTEM", "PROJECT"]  # SYSTEM: 시스템 요구 / PROJECT: 사업 수행 의무
Priority = Literal["MUST", "SHOULD", "COULD", "UNSPECIFIED"]  # UNSPECIFIED: 원문에 우선순위 근거가 없음
RequirementCategory = Literal[
    "기능", "성능", "보안", "인터페이스", "데이터", "품질", "제약", "장비구성", "테스트", "사업관리", "사업지원",
]


class Evidence(BaseModel):
    """요구사항의 원문 근거: 블록 ID + 원문 그대로의 인용문 (verified = 인용문이 원문에서 확인됨)."""
    source_block_id: str
    quote: str
    verified: bool = False               # quote가 실제 블록 원문에 존재하는지


class ReviewNote(BaseModel):
    """요구사항 검토 Agent가 남긴 표시. question_id가 있으면 고객 확인 대기 중이다 (답을 받으면 표시를 지운다).

    CLASSIFICATION: 분류 근거가 애매함 — 기능이면 '기능(추가 여부 판단 필요)', 비기능이면 '비기능(판단 필요)'
    ADDED         : 원문에 근거가 없이 추가로 판단해 만든 요구사항
    CONFLICT      : 다른 요구사항과 동시에 만족할 수 없음 (추천안이 있으면 note에 함께 쓴다)
    SUPPLEMENTED  : 원문에 있는데 추출에서 빠져 요구사항 검토 Agent가 보충함 (근거가 원문에 있어 묻지 않는다)
    기능 명세 Agent는 확인 대기 표시가 있는 요구사항을 공통 결정에 쓰지 않고, 그 기능 규칙에 '(확인 필요 질문 ID)'를 붙인다.
    """
    kind: Literal["CLASSIFICATION", "ADDED", "CONFLICT", "SUPPLEMENTED"]
    note: str
    question_id: str = ""


class Requirement(BaseModel):
    """요구사항 하나. type(기능/비기능)·scope(시스템/사업 수행 의무)로 분류하고 원문 근거·사용자 결정을 함께 가진다."""
    requirement_id: str
    source_key: str = ""
    source_ref: str = ""                 # 원문 요구사항 번호
    type: RequirementType
    scope: RequirementScope
    category: str
    title: str
    description: str
    priority: Priority = "MUST"
    acceptance_criteria: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    # EXCLUDED: 사용자 의견으로 이번 범위에서 뺀 요구사항 (기능·데이터 설계 대상이 아니다. 기록은 남긴다)
    status: Literal["CONFIRMED", "ASSUMED", "OPEN", "EXCLUDED"] = "CONFIRMED"
    decisions: list[str] = Field(default_factory=list)       # 반영된 사용자 결정 문장
    review: list[ReviewNote] = Field(default_factory=list)   # 요구사항 검토 Agent의 표시 (확인 대기·보충)

    @property
    def designed(self) -> bool:
        """기능·데이터 설계 대상인 시스템 기능 요구사항인지."""
        return self.type == "FUNCTIONAL" and self.scope == "SYSTEM" and self.status != "EXCLUDED"

    @property
    def unsettled(self) -> list[ReviewNote]:
        """고객 확인을 기다리는 검토 표시 (분류 판단 필요·원문 외 추가·충돌)."""
        return [n for n in self.review if n.question_id]


USER_SOURCE = "USER"   # 사용자 의견으로 추가한 요구사항의 원문 단위 (원문 근거가 없다)


class Question(BaseModel):
    """확인 질문: 모호성·누락·충돌 유형, 선택지, 관련 요구사항, 답변 상태.

    stage: 질문을 낸 단계. requirements(요구사항 Agent) 답변은 요구사항 결정으로, data_modeler(데이터 설계) 답변은
    데이터 설계 결정으로 반영한다. 데이터 설계 질문은 추천 선택지(recommended)로 먼저 설계해 두고, 답이 다를 때만 다시 설계한다.
    function_designer(기능 명세) 질문도 추천안으로 먼저 설계하고, 답은 관련 요구사항의 결정으로 남기며, 답이 다를 때만 관련 기능을 고친다.
    """
    question_id: str
    kind: Literal["AMBIGUITY", "MISSING", "CONFLICT"]
    question: str
    context: str
    options: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    evidence_block_ids: list[str] = Field(default_factory=list)
    blocking: bool = False
    status: Literal["PENDING", "ANSWERED", "DEFERRED"] = "PENDING"
    answer: str = ""
    stage: str = "requirements"
    recommended: str = ""                # 추천 선택지 (데이터 설계 질문은 이 선택지로 먼저 설계)
    entity_ids: list[str] = Field(default_factory=list)
    confirms: list[str] = Field(default_factory=list)   # 답하면 확정(CONFIRMED)되는 요구사항 (기능 명세 질문이 확정 안 된 요구사항을 물은 경우)


QUESTION_STAGES = {"requirements": "요구사항", "requirement_reviewer": "요구사항 검토", "function_designer": "기능 명세", "data_modeler": "데이터 설계"}
