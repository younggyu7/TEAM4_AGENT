"""문서·실행 API의 입력 계약."""

from typing import Literal

from pydantic import Field

from app.schemas.design import StrictModel, Text


class ProjectCreate(StrictModel):
    name: Text = Field(max_length=100)
    description: str = Field(default="", max_length=2000)


class RunCreate(StrictModel):
    document_revision_id: Text
    parent_run_id: str | None = None


class AnswerUpdate(StrictModel):
    expected_version: int = Field(ge=0)
    text: str | None = Field(default=None, max_length=2000)
    unresolved: bool = False


class RunContinue(StrictModel):
    expected_versions: dict[str, int]
    unresolved_question_ids: list[str] = Field(default_factory=list)


class ReviewDecisionCreate(StrictModel):
    finding_id: Text
    disposition: Literal["confirmed", "false_positive", "deferred"]
    reason: Text
