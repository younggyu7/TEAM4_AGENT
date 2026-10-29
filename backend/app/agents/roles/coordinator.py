"""저장된 실행 상태와 검토 결과로 담당·다음 단계·허용 수정 범위를 정한다."""

import re
from typing import Literal

from pydantic import Field

from app.agents.flow.transitions import RunCursor
from app.agents.roles.requirement_reviwer import RequirementsReviewOutput
from app.schemas.design import StrictModel, Text
from app.schemas.design_v1 import DataDesignOutput

Owner = Literal[
    "requirements_analyst",
    "requirements_reviewer",
    "function_designer",
    "data_modeler",
    "reviewer",
    "structure_validation",
    "render",
    "db_validation",
    "delivery",
]


class CoordinationDecision(StrictModel):
    action: Literal["dispatch", "wait", "finished", "repair_analysis", "repair_data", "fail"]
    owner: Owner | None = None
    scope: list[Text] = Field(default_factory=list)
    reason: Text


_OWNERS: dict[str, Owner] = {
    "analysis": "requirements_analyst",
    "requirements_review": "requirements_reviewer",
    "function_design": "function_designer",
    "data_design": "data_modeler",
    "structure_validation": "structure_validation",
    "render": "render",
    "semantic_review": "reviewer",
    "db_validation": "db_validation",
    "delivery": "delivery",
}


def next_action(cursor: RunCursor) -> CoordinationDecision:
    """그래프가 실행할 노드를 허용 상태·단계에서만 고른다."""
    if cursor.status == "awaiting_input":
        return CoordinationDecision(action="wait", reason="사용자 답변 또는 미해결 결정을 기다립니다.")
    if cursor.status != "running":
        return CoordinationDecision(action="finished", reason="실행이 종료되었습니다.")
    owner = _OWNERS[cursor.phase]
    return CoordinationDecision(action="dispatch", owner=owner, reason=f"{cursor.phase} 단계를 실행합니다.")


def decide_requirements_review(review: RequirementsReviewOutput, revision_count: int) -> CoordinationDecision:
    """요구사항 재분석을 한 번만 허용하고 근거 블록·요구사항 범위를 명시한다."""
    if review.verdict == "approved":
        return CoordinationDecision(action="wait", reason="요구사항 검토를 통과했습니다.")
    if revision_count >= 1:
        return CoordinationDecision(action="fail", reason="요구사항 재분석 후에도 검토 문제가 남았습니다.")
    scope = sorted(
        {item for finding in review.findings for item in finding.requirement_ids}
        | {ref.block_id for finding in review.findings for ref in finding.evidence}
    )
    return CoordinationDecision(
        action="repair_analysis",
        owner="requirements_analyst",
        scope=scope,
        reason="검토 문제의 원문 범위와 관련 요구사항을 다시 분석합니다.",
    )


def decide_structure_repair(issues: list[str], output: DataDesignOutput, repair_count: int) -> CoordinationDecision:
    """테이블을 특정할 수 있는 구조 오류만 한 번 부분 수정한다."""
    if not issues or repair_count >= 1:
        return CoordinationDecision(action="fail", reason="자동 수정 한도를 넘었거나 수정할 오류가 없습니다.")
    indexes = []
    for issue in issues:
        match = re.search(r"(?:^|\.)tables\[(\d+)\]", issue)
        if match is None:
            return CoordinationDecision(action="fail", reason="수정 대상 테이블을 특정할 수 없습니다.")
        index = int(match.group(1))
        if index >= len(output.data_model.tables):
            return CoordinationDecision(action="fail", reason="수정 대상 테이블이 모델에 없습니다.")
        indexes.append(index)
    scope = [output.data_model.tables[index].id for index in sorted(set(indexes))]
    return CoordinationDecision(
        action="repair_data", owner="data_modeler", scope=scope, reason="구조 오류가 있는 테이블의 속성만 재설계합니다."
    )
