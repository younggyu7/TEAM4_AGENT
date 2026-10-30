"""검토·추적·조정: 산출물(입력)을 검사·검토한 이슈와 추적 링크, 조정 Agent의 판단(출력).

  입력: Requirement·FunctionSpec·DataModel
  출력: Issue (수정 담당·시도 횟수), TraceLink (요구사항→기능→엔티티), CoordinatorDecision (다음 행동·범위·결과)
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AgentName = Literal[
    "document_analyst", "requirements_analyst", "function_designer", "data_modeler", "trace_reviewer", "none",
]


class Issue(BaseModel):
    """검사·검토에서 나온 이슈. target_agent가 수정 담당이고 attempts는 수정 시도 횟수다."""
    issue_id: str
    kind: str                            # 예: EVIDENCE_NOT_FOUND, MODEL_NO_PK, REQ_WITHOUT_FUNCTION, TRACE_FUNC_WITHOUT_ENTITY
    severity: Literal["ERROR", "WARNING", "INFO"]
    message: str
    target_agent: AgentName = "none"     # 수정 담당 (none = 보고만)
    target_id: str = ""                  # 수정 범위 (요구사항/기능/엔티티 ID)
    status: Literal["OPEN", "RESOLVED", "UNRESOLVED"] = "OPEN"
    attempts: int = 0
    raised_by: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """같은 이슈인지 판별하는 키 (종류, 대상)."""
        return (self.kind, self.target_id)


class TraceLink(BaseModel):
    """추적 관계 한 줄 (요구사항→기능, 비기능→기능, 기능→엔티티)."""
    from_id: str
    to_id: str
    link_type: Literal["REQ_FUNC", "NFR_FUNC", "FUNC_ENTITY"]


class CoordinatorDecision(BaseModel):
    """조정 Agent의 판단 한 건: 다음 행동, 수정 범위, 모드, 사유(선택 시점 상태), 결과(실행 후 요약)."""
    step: int
    action: Literal[
        "document_analyst", "requirements_analyst", "requirement_reviewer", "function_designer", "data_modeler",
        "trace_reviewer", "semantic_reviewer", "ask_user", "check_completion",
    ]
    scope: list[str] = Field(default_factory=list)
    mode: str = ""                       # 예: full / partial / fix / research
    reason: str                          # 이 행동을 고른 이유 (단계 시작 시 상태)
    result: str = ""                     # 행동을 실행한 뒤의 결과 요약 (결과 검사·결정 기록·완료 조건 검사가 채움)
