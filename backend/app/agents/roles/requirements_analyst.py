"""문서 블록에서 요구사항과 확인 질문을 만드는 전문 역할."""

import json
from typing import Literal

from pydantic import Field, ValidationError

from app.agents.flow.context import Context
from app.agents.flow.errors import failure
from app.agents.llm import client as model_client
from app.agents.modeling.validation import validate_analysis_output
from app.agents.prompts.requirements_analyst import ANALYZE
from app.agents.roles.document_analyst import find_exact_evidence
from app.schemas.design import ModelCall, StrictModel, Text
from app.schemas.design_v1 import (
    AnalysisInput,
    AnalysisOutput,
    Priority,
    QuestionV1,
    RequirementV1,
)
from app.schemas.document import EvidenceReference
from app.services.document_extraction import DocumentExtractionError


class QuotedEvidence(StrictModel):
    block_id: Text
    quote: str = Field(min_length=1, pattern=r"\S")


class RequirementCandidate(StrictModel):
    key: Text
    text: Text
    kind: Literal["functional", "nonfunctional"]
    provenance: Literal["source", "proposal"]
    evidence: list[QuotedEvidence]
    needs_confirmation: bool
    priority: Priority
    priority_reason: Text
    proposal_reason: str = ""
    source_ref: str | None = None


class QuestionCandidate(StrictModel):
    requirement_keys: list[Text] = Field(min_length=1)
    type: Literal["information_gap", "conflict"]
    question: Text
    evidence: list[QuotedEvidence] = Field(min_length=1)


class AnalysisCandidates(StrictModel):
    requirements: list[RequirementCandidate] = Field(min_length=1)
    questions: list[QuestionCandidate]


def _evidence(items: list[QuotedEvidence], stage_input: AnalysisInput) -> list[EvidenceReference]:
    result = []
    for item in items:
        try:
            reference = find_exact_evidence(stage_input, item.block_id, item.quote)
        except DocumentExtractionError:
            raise failure(
                "invalid_analysis_reference",
                "요구사항 또는 질문의 원문 근거를 확인할 수 없습니다.",
                "analysis",
            ) from None
        result.append(reference)
    return result


def _finalize(stage_input: AnalysisInput, candidates: AnalysisCandidates) -> AnalysisOutput:
    keys = [item.key for item in candidates.requirements]
    if len(set(keys)) != len(keys):
        raise failure("invalid_analysis_reference", "요구사항 임시 ID가 중복되었습니다.", "analysis")
    ids = {key: f"C-REQ-{index}" for index, key in enumerate(keys, 1)}
    if any(not set(question.requirement_keys) <= ids.keys() for question in candidates.questions):
        raise failure("invalid_analysis_reference", "질문이 없는 요구사항을 참조합니다.", "analysis")
    try:
        output = AnalysisOutput(
            requirements=[
                RequirementV1(
                    id=ids[item.key],
                    text=item.text,
                    kind=item.kind,
                    provenance=item.provenance,
                    evidence=_evidence(item.evidence, stage_input),
                    needs_confirmation=item.needs_confirmation,
                    priority=item.priority,
                    priority_reason=item.priority_reason,
                    proposal_reason=item.proposal_reason,
                    source_ref=item.source_ref,
                )
                for item in candidates.requirements
            ],
            questions=[
                QuestionV1(
                    id=f"C-Q-{index}",
                    requirement_ids=[ids[key] for key in item.requirement_keys],
                    type=item.type,
                    question=item.question,
                    evidence=_evidence(item.evidence, stage_input),
                    status="open",
                )
                for index, item in enumerate(candidates.questions, 1)
            ],
        )
    except ValidationError:
        raise failure("invalid_analysis_output", "분석 결과가 단계 스키마와 일치하지 않습니다.", "analysis") from None
    issues = validate_analysis_output(stage_input, output)
    if issues:
        raise failure(
            "invalid_analysis_reference", "분석 결과의 ID 또는 근거가 잘못되었습니다.", "analysis", issues=issues
        )
    return output


async def analyze(
    stage_input: AnalysisInput, context: Context, *, feedback: dict | None = None
) -> tuple[AnalysisOutput, ModelCall]:
    """전체 블록을 분석한다. 실패 응답이나 일부 결과를 정상 결과로 반환하지 않는다."""
    payload = {
        "document_revision_id": stage_input.document_revision_id,
        "blocks": [
            {"block_id": block.block_id, "locator": block.locator.model_dump(), "text": block.normalized_text}
            for block in stage_input.blocks
        ],
    }
    if feedback is not None:
        payload["review_feedback"] = feedback
    raw, call = await model_client.call_model(
        json.dumps(payload, ensure_ascii=False), "analysis", AnalysisCandidates, context, ANALYZE
    )
    try:
        candidates = AnalysisCandidates.model_validate_json(raw)
    except ValidationError:
        raise failure("invalid_analysis_output", "분석 결과가 단계 스키마와 일치하지 않습니다.", "analysis") from None
    return _finalize(stage_input, candidates), call
