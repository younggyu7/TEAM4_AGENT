"""후속 단계의 결정적 계약 검사와 실행 전이 규칙.

기존 동기 ``/design`` 그래프와는 분리하고, 새 문서·실행 그래프에서 이 계약을 사용한다.
"""

from typing import Literal

from pydantic import Field, model_validator

from app.agents.modeling.validation import validate_answer_snapshot
from app.schemas.design import StrictModel, Text
from app.schemas.design_v1 import (
    AnswerSnapshot,
    QuestionV1,
    VerificationV1,
)

RunStatus = Literal[
    "queued",
    "running",
    "awaiting_input",
    "completed",
    "completed_with_warnings",
    "completed_with_errors",
    "failed",
    "superseded",
]
RunPhase = Literal[
    "analysis",
    "requirements_review",
    "function_design",
    "data_design",
    "structure_validation",
    "render",
    "semantic_review",
    "db_validation",
    "delivery",
]
RunEvent = Literal[
    "start",
    "analysis_completed",
    "requirements_review_completed",
    "requirements_review_revise",
    "continue",
    "stage_completed",
    "structure_repair_requested",
    "technical_failure",
    "supersede",
]
RetryError = Literal["format", "provider_transient", "auth", "input", "other"]


class StageFailure(StrictModel):
    stage: RunPhase
    code: Text
    message: Text
    retryable: bool
    attempts: int = Field(ge=0)


class RunCursor(StrictModel):
    status: RunStatus
    phase: RunPhase
    answers_frozen: bool = False
    has_unresolved: bool = False
    has_review_warnings: bool | None = None
    analysis_revisions: int = Field(default=0, ge=0, le=1)
    data_repairs: int = Field(default=0, ge=0, le=1)
    repair_mode: Literal["full", "cover", "fix"] = "full"
    repair_scope: list[Text] = Field(default_factory=list)
    failure: StageFailure | None = None
    verification: VerificationV1 = Field(
        default_factory=lambda: VerificationV1(
            structure="not_run", semantic_review="not_run", database_execution="not_run"
        )
    )

    @model_validator(mode="after")
    def valid_position(self):
        if (self.status == "failed") != (self.failure is not None):
            raise ValueError("failed status requires a stage failure; other statuses cannot carry one")
        if self.failure is not None and self.failure.stage != self.phase:
            raise ValueError("failure stage must match the active phase")
        if self.phase in {"analysis", "requirements_review"} and self.answers_frozen:
            raise ValueError("analysis and requirements review cannot use a frozen answer snapshot")
        if not self.answers_frozen and self.has_unresolved:
            raise ValueError("unresolved answers require a frozen snapshot")
        if (
            self.phase not in {"analysis", "requirements_review"}
            and self.status in {"queued", "running"}
            and not self.answers_frozen
        ):
            raise ValueError("design stages require a frozen answer snapshot")
        if self.status == "queued" and self.phase not in {"analysis", "function_design"}:
            raise ValueError("queued work can start only at analysis or function design")
        if self.status == "awaiting_input" and self.phase != "requirements_review":
            raise ValueError("only reviewed analysis can await user input")
        if self.status == "superseded" and self.phase != "requirements_review":
            raise ValueError("only a reviewed analysis run can be superseded")
        if (
            self.status in {"completed", "completed_with_warnings", "completed_with_errors"}
            and self.phase != "delivery"
        ):
            raise ValueError("completion requires delivery")
        if self.status in {"completed", "completed_with_warnings", "completed_with_errors"}:
            if self.has_review_warnings is None or self.status != completion_status(
                self.verification,
                has_warnings=self.has_review_warnings,
                has_unresolved=self.has_unresolved,
            ):
                raise ValueError("completion status must match verification and warning results")
        if self.status in {
            "queued",
            "running",
            "awaiting_input",
            "completed",
            "completed_with_warnings",
            "completed_with_errors",
        }:
            verification = self.verification
            if self.phase in {
                "analysis",
                "requirements_review",
                "function_design",
                "data_design",
                "structure_validation",
            } and (verification.structure != "not_run"):
                raise ValueError("structure result cannot precede structure validation")
            if self.phase in {"render", "semantic_review"} and (
                verification.structure != "passed" or verification.semantic_review != "not_run"
            ):
                raise ValueError("render and review require passed structure")
            if self.phase == "db_validation" and (
                verification.structure != "passed"
                or verification.semantic_review != "completed"
                or verification.database_execution != "not_run"
            ):
                raise ValueError("database validation requires completed review")
            if self.phase == "delivery" and not (
                verification.structure == "failed"
                or (verification.structure == "passed" and verification.semantic_review == "completed")
            ):
                raise ValueError("delivery requires a structure failure or completed review")
        return self


def completion_status(verification: VerificationV1, *, has_warnings: bool, has_unresolved: bool) -> RunStatus:
    """기술 실패는 별도 이벤트로 기록하고, 검사 결과의 우선순위만 판정한다."""
    if verification.semantic_review == "failed":
        return "failed"
    if verification.structure == "failed" or verification.database_execution == "failed":
        return "completed_with_errors"
    if verification.structure != "passed" or verification.semantic_review != "completed":
        return "failed"
    if verification.database_execution == "not_run" or has_warnings or has_unresolved:
        return "completed_with_warnings"
    return "completed"


def advance_cursor(
    cursor: RunCursor,
    event: RunEvent,
    *,
    answer_snapshot: AnswerSnapshot | None = None,
    questions: list[QuestionV1] | None = None,
    verification: VerificationV1 | None = None,
    has_warnings: bool | None = None,
    failure: StageFailure | None = None,
    repair_scope: list[str] | None = None,
    repair_mode: Literal["cover", "fix"] | None = None,
) -> RunCursor:
    """A 단계에서 확정한 순수 전이. E가 영속 상태와 동일 트랜잭션으로 연결한다."""
    if event == "technical_failure" and cursor.status in {"queued", "running", "awaiting_input"}:
        if failure is None or failure.stage != cursor.phase:
            raise ValueError("technical failure requires a matching stage error")
        return cursor.model_copy(update={"status": "failed", "failure": failure})
    if event == "supersede" and cursor.status == "awaiting_input":
        return cursor.model_copy(update={"status": "superseded"})
    if event == "start" and cursor.status == "queued":
        if cursor.phase in {"analysis", "function_design"}:
            return cursor.model_copy(update={"status": "running"})
    if event == "analysis_completed" and cursor.status == "running" and cursor.phase == "analysis":
        return cursor.model_copy(update={"phase": "requirements_review"})
    if (
        event == "requirements_review_completed"
        and cursor.status == "running"
        and cursor.phase == "requirements_review"
    ):
        return cursor.model_copy(update={"status": "awaiting_input", "repair_scope": []})
    if event == "requirements_review_revise" and cursor.status == "running" and cursor.phase == "requirements_review":
        if cursor.analysis_revisions >= 1 or not repair_scope:
            raise ValueError("requirements revision requires a bounded nonempty scope")
        return cursor.model_copy(
            update={
                "phase": "analysis",
                "analysis_revisions": cursor.analysis_revisions + 1,
                "repair_scope": repair_scope,
            }
        )
    if event == "continue" and cursor.status == "awaiting_input" and cursor.phase == "requirements_review":
        if answer_snapshot is None or questions is None or validate_answer_snapshot(questions, answer_snapshot):
            raise ValueError("continue requires a complete frozen answer snapshot")
        return RunCursor(
            status="queued",
            phase="function_design",
            answers_frozen=True,
            has_unresolved=any(answer.unresolved for answer in answer_snapshot.answers),
        )
    if event == "stage_completed" and cursor.status == "running" and cursor.answers_frozen:
        next_phase: dict[RunPhase, RunPhase] = {
            "function_design": "data_design",
            "data_design": "structure_validation",
            "render": "semantic_review",
        }
        if cursor.phase == "structure_validation":
            if (
                verification is None
                or verification.structure == "not_run"
                or (verification.semantic_review != "not_run" or verification.database_execution != "not_run")
            ):
                raise ValueError("structure validation needs a result")
            return cursor.model_copy(
                update={
                    "phase": "render" if verification.structure == "passed" else "delivery",
                    "verification": verification,
                }
            )
        if cursor.phase == "data_design":
            return cursor.model_copy(
                update={"phase": "structure_validation", "repair_mode": "full", "repair_scope": []}
            )
        if cursor.phase == "semantic_review":
            if verification is None or verification != VerificationV1(
                structure="passed", semantic_review="completed", database_execution="not_run"
            ):
                raise ValueError("semantic review must complete before continuing")
            return cursor.model_copy(update={"phase": "db_validation", "verification": verification})
        if cursor.phase == "db_validation":
            if (
                verification is None
                or verification.structure != cursor.verification.structure
                or verification.semantic_review != cursor.verification.semantic_review
            ):
                raise ValueError("database validation needs a result with previous checks unchanged")
            return cursor.model_copy(update={"phase": "delivery", "verification": verification})
        if cursor.phase == "delivery":
            if verification is not None and verification != cursor.verification:
                raise ValueError("delivery cannot replace validation results")
            if has_warnings is None:
                raise ValueError("delivery needs an explicit review warning result")
            return cursor.model_copy(
                update={
                    "status": completion_status(
                        cursor.verification, has_warnings=has_warnings, has_unresolved=cursor.has_unresolved
                    ),
                    "has_review_warnings": has_warnings,
                }
            )
        if cursor.phase in next_phase:
            return cursor.model_copy(update={"phase": next_phase[cursor.phase]})
    if event == "structure_repair_requested" and cursor.status == "running" and cursor.phase == "structure_validation":
        if cursor.data_repairs >= 1 or repair_mode is None or not repair_scope:
            raise ValueError("structure repair requires a bounded nonempty scope")
        return cursor.model_copy(
            update={
                "phase": "data_design",
                "data_repairs": cursor.data_repairs + 1,
                "repair_mode": repair_mode,
                "repair_scope": repair_scope,
            }
        )
    raise ValueError(f"transition not allowed: {cursor.status}/{cursor.phase} + {event}")


def apply_cursor(run, cursor: RunCursor) -> None:
    """같은 트랜잭션에서 조회용 열과 영속 커서를 함께 갱신한다."""
    run.cursor_json = cursor.model_dump(mode="json")
    run.status, run.phase = cursor.status, cursor.phase


def retry_allowed(attempts: int, error_kind: RetryError) -> bool:
    """최초 시도 뒤 형식 복구·일시 공급자 오류를 합쳐 한 번만 재시도한다."""
    return attempts == 1 and error_kind in {"format", "provider_transient"}
