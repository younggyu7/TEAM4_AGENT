"""최종 설계를 결정적 규칙으로 검토하는 Agent."""

from typing import Literal

from app.agents.flow.errors import failure
from app.agents.modeling.design_model import (
    ModelArtifactsV1,
    model_hash_v1,
    render_model_v1,
    validate_model_v1,
    validate_review_findings_v1,
)
from app.agents.modeling.validation import validate_analysis_output
from app.schemas.design import StrictModel, Text
from app.schemas.design_v1 import (
    SCHEMA_VERSION,
    AnalysisInput,
    DataDesignInput,
    DataDesignOutput,
    ReviewFindingV1,
)


class FinalReviewOutput(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    findings: list[ReviewFindingV1]
    summary: Text


def review_design(
    analysis_input: AnalysisInput,
    design_input: DataDesignInput,
    design_output: DataDesignOutput,
    artifacts: ModelArtifactsV1,
) -> FinalReviewOutput:
    """원문·모델 해시·추적 행을 재대조하고 규칙 검토 후보를 확정한다."""
    issues = validate_analysis_output(analysis_input, design_input.analysis)
    issues.extend(validate_model_v1(design_input, design_output))
    if artifacts.schema_version != SCHEMA_VERSION or artifacts.model_hash != model_hash_v1(design_output.data_model):
        issues.append("artifacts: model version or hash mismatch")
    expected_ids = [item.id for item in design_input.analysis.requirements]
    actual_ids = [row.requirement_id for row in artifacts.trace_rows]
    if actual_ids != expected_ids:
        issues.append("trace_rows: requirement coverage mismatch")
    if not issues and artifacts != render_model_v1(design_input, design_output):
        issues.append("artifacts: rendered ERD, DDL, trace rows or review findings differ from the model")
    issues.extend(
        validate_review_findings_v1(analysis_input, design_input, design_output, list(artifacts.review_candidates))
    )
    if issues:
        raise failure(
            "invalid_final_review_input",
            "최종 검토 입력의 원문·모델·추적 관계가 일치하지 않습니다.",
            "semantic_review",
            422,
            issues=issues,
        )
    findings = list(artifacts.review_candidates)
    return FinalReviewOutput(
        findings=findings,
        summary="결정적 추적·누락 규칙에서 검토 후보가 없습니다."
        if not findings
        else f"결정적 추적·누락 규칙에서 검토 후보 {len(findings)}건을 찾았습니다.",
    )
