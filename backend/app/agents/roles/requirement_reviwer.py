"""요구사항 분석 결과를 원문과 독립적으로 대조하는 검토 Agent."""

import json
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from app.agents.flow.context import Context
from app.agents.flow.errors import failure
from app.agents.llm import client as model_client
from app.agents.modeling.validation import validate_analysis_output
from app.agents.prompts.requirements_reviewer import REVIEW_REQUIREMENTS
from app.agents.roles.document_analyst import find_exact_evidence
from app.schemas.design import ModelCall, StrictModel, Text
from app.schemas.design_v1 import AnalysisInput, AnalysisOutput
from app.schemas.document import EvidenceReference
from app.services.document_extraction import DocumentExtractionError


class RequirementReviewQuote(StrictModel):
    block_id: Text
    quote: Text


class RequirementReviewCandidate(StrictModel):
    kind: Literal["missing", "duplicate", "classification", "evidence", "question"]
    description: Text
    requirement_ids: list[Text] = Field(default_factory=list)
    question_ids: list[Text] = Field(default_factory=list)
    evidence: list[RequirementReviewQuote] = Field(min_length=1)


class RequirementReviewCandidates(StrictModel):
    verdict: Literal["approved", "revision_required"]
    findings: list[RequirementReviewCandidate]
    summary: Text

    @model_validator(mode="after")
    def consistent_verdict(self):
        if (self.verdict == "approved") != (not self.findings):
            raise ValueError("approved requires no findings and revision_required requires findings")
        return self


class RequirementReviewFinding(StrictModel):
    kind: Literal["missing", "duplicate", "classification", "evidence", "question"]
    description: Text
    requirement_ids: list[Text]
    question_ids: list[Text]
    evidence: list[EvidenceReference] = Field(min_length=1)


class RequirementsReviewOutput(StrictModel):
    verdict: Literal["approved", "revision_required"]
    findings: list[RequirementReviewFinding]
    summary: Text


async def review_requirements(
    stage_input: AnalysisInput, analysis: AnalysisOutput, context: Context
) -> tuple[RequirementsReviewOutput, ModelCall]:
    """잘못된 참조나 인용을 검토 의견으로 통과시키지 않는다."""
    issues = validate_analysis_output(stage_input, analysis)
    if issues:
        raise failure(
            "invalid_requirements_review_input",
            "검토할 요구사항 분석의 근거가 올바르지 않습니다.",
            "requirements_review",
            422,
            issues=issues,
        )
    payload = {
        "document_revision_id": stage_input.document_revision_id,
        "blocks": [
            {"block_id": block.block_id, "locator": block.locator.model_dump(), "text": block.normalized_text}
            for block in stage_input.blocks
        ],
        "analysis": analysis.model_dump(mode="json"),
    }
    raw, call = await model_client.call_model(
        json.dumps(payload, ensure_ascii=False),
        "requirements_review",
        RequirementReviewCandidates,
        context,
        REVIEW_REQUIREMENTS,
    )
    try:
        candidates = RequirementReviewCandidates.model_validate_json(raw)
    except ValidationError:
        raise failure(
            "invalid_requirements_review_output",
            "요구사항 검토 결과가 단계 스키마와 일치하지 않습니다.",
            "requirements_review",
        ) from None
    requirement_ids = {item.id for item in analysis.requirements}
    question_ids = {item.id for item in analysis.questions}
    findings = []
    for item in candidates.findings:
        if not set(item.requirement_ids) <= requirement_ids or not set(item.question_ids) <= question_ids:
            raise failure(
                "invalid_requirements_review_reference",
                "요구사항 검토가 존재하지 않는 요구사항·질문을 참조합니다.",
                "requirements_review",
            )
        try:
            evidence = [find_exact_evidence(stage_input, quote.block_id, quote.quote) for quote in item.evidence]
        except DocumentExtractionError:
            raise failure(
                "invalid_requirements_review_reference",
                "요구사항 검토의 원문 인용이 일치하지 않습니다.",
                "requirements_review",
            ) from None
        findings.append(
            RequirementReviewFinding(
                kind=item.kind,
                description=item.description,
                requirement_ids=item.requirement_ids,
                question_ids=item.question_ids,
                evidence=evidence,
            )
        )
    return RequirementsReviewOutput(verdict=candidates.verdict, findings=findings, summary=candidates.summary), call
