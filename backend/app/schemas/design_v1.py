"""단계별 설계 결과의 v1 계약.

기존 ``schemas.design``은 동기 ``/design`` API의 모델 출력 계약이다. 이 모델들은
후속 전문 역할이 공유할 새 계약이며 기존 API 결과에 암묵적으로 섞지 않는다.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.schemas.design import Identifier, StrictModel, Text
from app.schemas.document import DocumentBlock, EvidenceReference

SCHEMA_VERSION = "1"

RequirementId = Annotated[str, StringConstraints(pattern=r"^C-REQ-[0-9]+$")]
QuestionId = Annotated[str, StringConstraints(pattern=r"^C-Q-[0-9]+$")]
FunctionId = Annotated[str, StringConstraints(pattern=r"^C-FUN-[0-9]+$")]
TableId = Annotated[str, StringConstraints(pattern=r"^C-TBL-[0-9]+$")]
ColumnId = Annotated[str, StringConstraints(pattern=r"^C-COL-[0-9]+$")]
RuleId = Annotated[str, StringConstraints(pattern=r"^C-RULE-[0-9]+$")]
FindingId = Annotated[str, StringConstraints(pattern=r"^C-FIND-[0-9]+$")]
Provenance = Literal["source", "user_answer", "proposal"]
Priority = Literal["high", "medium", "low", "undecided"]
VerificationStatus = Literal["passed", "failed", "not_run"]


class RequirementV1(StrictModel):
    id: RequirementId
    text: Text
    kind: Literal["functional", "nonfunctional"]
    provenance: Provenance
    evidence: list[EvidenceReference]
    needs_confirmation: bool
    priority: Priority
    priority_reason: Text
    proposal_reason: str = ""
    source_ref: str | None = None
    answer_question_id: QuestionId | None = None
    answer_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def provenance_fields(self):
        if self.provenance == "source" and not self.evidence:
            raise ValueError("source requirement needs evidence")
        if self.provenance == "proposal" and (not self.proposal_reason.strip() or not self.needs_confirmation):
            raise ValueError("proposal needs a reason and confirmation")
        if self.provenance == "user_answer" and (self.answer_question_id is None or self.answer_version is None):
            raise ValueError("user_answer needs a question and answer version")
        if self.provenance != "user_answer" and (
            self.answer_question_id is not None or self.answer_version is not None
        ):
            raise ValueError("answer reference requires user_answer provenance")
        return self


class QuestionV1(StrictModel):
    id: QuestionId
    requirement_ids: list[RequirementId] = Field(min_length=1)
    type: Literal["information_gap", "conflict"]
    question: Text
    evidence: list[EvidenceReference] = Field(min_length=1)
    status: Literal["open", "answered", "unresolved"]


class AnswerV1(StrictModel):
    question_id: QuestionId
    version: int = Field(ge=1)
    text: str | None = Field(default=None, max_length=2000)
    unresolved: bool
    created_at: datetime

    @model_validator(mode="after")
    def answer_or_unresolved(self):
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if self.unresolved:
            if self.text is not None:
                raise ValueError("unresolved answer cannot have text")
        elif self.text is None or not self.text.strip():
            raise ValueError("resolved answer needs text")
        return self


class AnswerSnapshot(StrictModel):
    answers: list[AnswerV1]
    frozen_at: datetime

    @model_validator(mode="after")
    def aware_freeze_time(self):
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() is None:
            raise ValueError("frozen_at must include a timezone")
        return self


class FunctionV1(StrictModel):
    id: FunctionId
    name: Text
    actor: Text
    description: Text
    inputs: list[Text]
    outputs: list[Text]
    preconditions: list[Text]
    rules: list[Text]
    exceptions: list[Text]
    priority: Priority
    requirement_ids: list[RequirementId]
    provenance: Provenance
    proposal_reason: str = ""
    answer_question_id: QuestionId | None = None
    answer_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def linked_or_proposed(self):
        if self.provenance == "proposal" and not self.proposal_reason.strip():
            raise ValueError("proposal function needs a reason")
        if not self.requirement_ids and self.provenance != "proposal":
            raise ValueError("unlinked function needs proposal provenance")
        if self.provenance == "user_answer" and (self.answer_question_id is None or self.answer_version is None):
            raise ValueError("user_answer function needs a question and answer version")
        if self.provenance != "user_answer" and (
            self.answer_question_id is not None or self.answer_version is not None
        ):
            raise ValueError("answer reference requires user_answer provenance")
        return self


class ColumnV1(StrictModel):
    id: ColumnId
    name: Identifier
    type: Literal["integer", "bigint", "numeric", "varchar", "text", "boolean", "date", "timestamptz", "uuid"]
    nullable: bool
    length: int | None = Field(default=None, ge=1)
    precision: int | None = Field(default=None, ge=1)
    scale: int | None = Field(default=None, ge=0)
    default_kind: Literal["none", "literal", "server_function"]
    default_value: str | int | bool | None = None
    default_function: Literal["current_timestamp", "gen_random_uuid"] | None = None
    description: Text

    @model_validator(mode="after")
    def bounded_type_and_default(self):
        if (self.type == "varchar") != (self.length is not None):
            raise ValueError("varchar requires length; other types cannot have it")
        if self.type == "numeric":
            if self.precision is None or self.scale is None or self.scale > self.precision:
                raise ValueError("numeric requires valid precision and scale")
        elif self.precision is not None or self.scale is not None:
            raise ValueError("precision and scale require numeric")
        if self.default_kind == "none" and (self.default_value is not None or self.default_function is not None):
            raise ValueError("no default cannot have a value or function")
        if self.default_kind == "literal" and (self.default_value is None or self.default_function is not None):
            raise ValueError("literal default needs only a value")
        if self.default_kind == "server_function" and (self.default_function is None or self.default_value is not None):
            raise ValueError("server default needs an allowed function")
        return self


class ForeignKeyV1(StrictModel):
    source_column_ids: list[ColumnId] = Field(min_length=1)
    target_table_id: TableId
    target_column_ids: list[ColumnId] = Field(min_length=1)
    on_delete: Literal["no_action", "restrict", "cascade", "set_null"]

    @model_validator(mode="after")
    def same_arity(self):
        if len(self.source_column_ids) != len(self.target_column_ids):
            raise ValueError("foreign key column counts differ")
        return self


class TableV1(StrictModel):
    id: TableId
    name: Identifier
    description: Text
    columns: list[ColumnV1] = Field(min_length=1)
    primary_key: list[ColumnId] = Field(min_length=1)
    uniques: list[list[ColumnId]]
    foreign_keys: list[ForeignKeyV1]
    requirement_ids: list[RequirementId]
    function_ids: list[FunctionId]
    provenance: Provenance
    proposal_reason: str = ""
    answer_question_id: QuestionId | None = None
    answer_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def linked_or_proposed(self):
        if self.provenance == "proposal" and not self.proposal_reason.strip():
            raise ValueError("proposal table needs a reason")
        if not (self.requirement_ids or self.function_ids) and self.provenance != "proposal":
            raise ValueError("unlinked table needs proposal provenance")
        if self.provenance == "user_answer" and (self.answer_question_id is None or self.answer_version is None):
            raise ValueError("user_answer table needs a question and answer version")
        if self.provenance != "user_answer" and (
            self.answer_question_id is not None or self.answer_version is not None
        ):
            raise ValueError("answer reference requires user_answer provenance")
        return self


class RuleV1(StrictModel):
    id: RuleId
    description: Text
    requirement_ids: list[RequirementId]
    enforcement: Literal["database", "application", "unresolved"]
    representation: Text
    provenance: Provenance
    proposal_reason: str = ""
    answer_question_id: QuestionId | None = None
    answer_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def provenance_fields(self):
        if self.provenance == "proposal" and not self.proposal_reason.strip():
            raise ValueError("proposal rule needs a reason")
        if not self.requirement_ids and self.provenance != "proposal":
            raise ValueError("unlinked rule needs proposal provenance")
        if self.provenance == "user_answer" and (self.answer_question_id is None or self.answer_version is None):
            raise ValueError("user_answer rule needs a question and answer version")
        if self.provenance != "user_answer" and (
            self.answer_question_id is not None or self.answer_version is not None
        ):
            raise ValueError("answer reference requires user_answer provenance")
        return self


class DataModelV1(StrictModel):
    tables: list[TableV1]
    rules: list[RuleV1]


class TraceLinkV1(StrictModel):
    source_id: Text
    target_id: Text
    relation: Text
    reason: Text
    provenance: Provenance
    answer_question_id: QuestionId | None = None
    answer_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def answer_source(self):
        if self.provenance == "user_answer" and (self.answer_question_id is None or self.answer_version is None):
            raise ValueError("user_answer trace needs a question and answer version")
        if self.provenance != "user_answer" and (
            self.answer_question_id is not None or self.answer_version is not None
        ):
            raise ValueError("answer reference requires user_answer provenance")
        return self


class ReviewFindingV1(StrictModel):
    id: FindingId
    type: Literal[
        "structure_error", "omission", "contradiction", "information_gap", "unimplemented_rule", "not_verified"
    ]
    severity: Literal["error", "warning", "info"]
    related_ids: list[Text]
    evidence: list[EvidenceReference]
    description: Text
    suggestion: Text
    detector: Text
    answer_question_id: QuestionId | None = None
    answer_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def paired_answer_reference(self):
        if (self.answer_question_id is None) != (self.answer_version is None):
            raise ValueError("answer reference needs question and version together")
        return self


class VerificationV1(StrictModel):
    structure: VerificationStatus
    semantic_review: Literal["completed", "failed", "not_run"]
    database_execution: VerificationStatus

    @model_validator(mode="after")
    def ordered_checks(self):
        if self.structure != "passed" and (self.semantic_review != "not_run" or self.database_execution != "not_run"):
            raise ValueError("review and database validation require passed structure")
        if self.semantic_review != "completed" and self.database_execution != "not_run":
            raise ValueError("database validation requires completed semantic review")
        return self


class ArtifactManifestV1(StrictModel):
    kind: Text
    run_id: Text
    schema_version: Literal["1"]
    model_hash: Text
    file_hash: Text
    validation_status: VerificationStatus


class AnalysisInput(StrictModel):
    document_revision_id: Text
    blocks: list[DocumentBlock] = Field(min_length=1)

    @model_validator(mode="after")
    def one_revision(self):
        if any(block.document_revision_id != self.document_revision_id for block in self.blocks):
            raise ValueError("analysis blocks must belong to one document revision")
        if len({block.block_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("analysis block IDs must be unique")
        return self


class AnalysisOutput(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    requirements: list[RequirementV1] = Field(min_length=1)
    questions: list[QuestionV1]


class FunctionDesignInput(StrictModel):
    analysis: AnalysisOutput
    answers: AnswerSnapshot


class FunctionDesignOutput(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    functions: list[FunctionV1] = Field(min_length=1)


class DataDesignInput(StrictModel):
    analysis: AnalysisOutput
    answers: AnswerSnapshot
    functions: FunctionDesignOutput


class DataDesignOutput(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    data_model: DataModelV1
    rationale: list[Text]
