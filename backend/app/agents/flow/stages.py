"""문서 분석→질문 대기→전문 설계→검증→제공의 영속 LangGraph."""

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.runtime import Runtime
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.flow.context import Context
from app.agents.flow.transitions import RunCursor, StageFailure, advance_cursor, apply_cursor, retry_allowed
from app.agents.modeling.design_model import render_model_v1, validate_model_v1
from app.agents.roles.coordinator import (
    decide_requirements_review,
    decide_structure_repair,
)
from app.agents.roles.data_modeler import design_data
from app.agents.roles.function_designer import design_functions
from app.agents.roles.requirement_reviwer import review_requirements
from app.agents.roles.requirements_analyst import analyze
from app.core.config import Settings
from app.core.exceptions import AppError
from app.models import DesignRun, StageAttempt
from app.schemas.design_v1 import (
    AnalysisInput,
    AnalysisOutput,
    AnswerSnapshot,
    DataDesignInput,
    DataDesignOutput,
    FunctionDesignInput,
    FunctionDesignOutput,
    VerificationV1,
)
from app.schemas.document import DocumentBlock


class WorkflowState(TypedDict):
    run_id: str
    phase: str
    stop: bool


@dataclass
class WorkflowContext:
    settings: Settings
    session_factory: Callable[[], Session]
    tracer: Any = None


def _digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cursor_for(run: DesignRun) -> RunCursor:
    return RunCursor.model_validate(run.cursor_json)


def next_state(run: DesignRun) -> WorkflowState:
    return {"run_id": run.id, "phase": run.phase, "stop": run.status != "running"}


def _record_attempt(
    db: Session,
    run: DesignRun,
    stage: str,
    attempt_no: int,
    stage_input: Any,
    elapsed_ms: int,
    *,
    output: BaseModel | None = None,
    call: BaseModel | None = None,
    error_code: str | None = None,
) -> None:
    db.add(
        StageAttempt(
            run_id=run.id,
            stage=stage,
            attempt_no=attempt_no,
            input_hash=_digest(stage_input),
            output_json=output.model_dump(mode="json") if output else None,
            error_code=error_code,
            duration_ms=elapsed_ms,
            token_usage=call.model_dump(mode="json") if call else None,
        )
    )
    db.commit()


async def _model_stage(
    db: Session, run: DesignRun, stage: str, stage_input: Any, invoke, workflow_context: WorkflowContext
):
    context = Context(settings=workflow_context.settings, tracer=workflow_context.tracer)
    previous = (
        db.scalar(
            select(func.max(StageAttempt.attempt_no)).where(StageAttempt.run_id == run.id, StageAttempt.stage == stage)
        )
        or 0
    )
    for retry_index in (1, 2):
        attempt_no = previous + retry_index
        started = time.monotonic()
        try:
            output, call = await invoke(stage_input, context)
        except (AppError, ValidationError) as exc:
            code = exc.code if isinstance(exc, AppError) else "output_invalid"
            _record_attempt(
                db, run, stage, attempt_no, stage_input, round((time.monotonic() - started) * 1000), error_code=code
            )
            format_codes = {
                "invalid_analysis_output",
                "invalid_analysis_reference",
                "invalid_requirements_review_output",
                "invalid_requirements_review_reference",
                "invalid_function_output",
                "invalid_function_reference",
                "invalid_data_output",
                "invalid_data_reference",
                "invalid_semantic_review_output",
                "invalid_semantic_review_reference",
                "output_invalid",
            }
            retry_kind = (
                "format"
                if code in format_codes
                else "provider_transient"
                if code in {"model_timeout", "model_provider_transient"}
                else "other"
            )
            if retry_allowed(retry_index, retry_kind):
                continue
            raise
        _record_attempt(
            db,
            run,
            stage,
            attempt_no,
            stage_input,
            round((time.monotonic() - started) * 1000),
            output=output,
            call=call,
        )
        return output
    raise AssertionError("unreachable retry loop")


def _analysis_input(db: Session, run: DesignRun) -> AnalysisInput:
    from app.models import DocumentRevision

    document = db.get(DocumentRevision, run.document_revision_id)
    if document is None or document.status != "ready":
        raise ValueError("document revision is not ready")
    return AnalysisInput(
        document_revision_id=document.id,
        blocks=[DocumentBlock.model_validate(block) for block in document.blocks_json],
    )


def _design_input(run: DesignRun) -> DataDesignInput:
    return DataDesignInput(
        analysis=AnalysisOutput.model_validate(run.analysis_json),
        answers=AnswerSnapshot.model_validate_json(json.dumps(run.answer_snapshot_json)),
        functions=FunctionDesignOutput.model_validate(run.function_json),
    )


async def analysis(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        stage_input = _analysis_input(db, run)
        feedback = (
            {
                "review": run.requirements_review_json,
                "previous_analysis": run.analysis_json,
                "scope": cursor_for(run).repair_scope,
            }
            if cursor_for(run).analysis_revisions
            else None
        )

        async def invoke(payload, context):
            return await analyze(payload, context, feedback=feedback) if feedback else await analyze(payload, context)

        db.close()
        output = await _model_stage(db, run, "analysis", stage_input, invoke, runtime.context)
        run.analysis_json = output.model_dump(mode="json")
        apply_cursor(run, advance_cursor(cursor_for(run), "analysis_completed"))
        db.add(run)
        db.commit()
        return next_state(run)


async def requirements_review(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        stage_input = _analysis_input(db, run)
        analysis = AnalysisOutput.model_validate(run.analysis_json)

        async def invoke(_payload, context):
            return await review_requirements(stage_input, analysis, context)

        db.close()
        review = await _model_stage(
            db,
            run,
            "requirements_review",
            {"analysis": analysis.model_dump(mode="json"), "document_revision_id": stage_input.document_revision_id},
            invoke,
            runtime.context,
        )
        run.requirements_review_json = review.model_dump(mode="json")
        cursor = cursor_for(run)
        decision = decide_requirements_review(review, cursor.analysis_revisions)
        if decision.action == "wait":
            next_cursor = advance_cursor(cursor, "requirements_review_completed")
        elif decision.action == "repair_analysis":
            next_cursor = advance_cursor(cursor, "requirements_review_revise", repair_scope=decision.scope)
        else:
            next_cursor = advance_cursor(
                cursor,
                "technical_failure",
                failure=StageFailure(
                    stage="requirements_review",
                    code="requirements_review_rejected",
                    message=decision.reason,
                    retryable=False,
                    attempts=cursor.analysis_revisions + 1,
                ),
            )
        apply_cursor(run, next_cursor)
        db.add(run)
        db.commit()
        return next_state(run)


async def function_design(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        stage_input = FunctionDesignInput(
            analysis=AnalysisOutput.model_validate(run.analysis_json),
            answers=AnswerSnapshot.model_validate_json(json.dumps(run.answer_snapshot_json)),
        )
        db.close()
        output = await _model_stage(db, run, "function_design", stage_input, design_functions, runtime.context)
        run.function_json = output.model_dump(mode="json")
        apply_cursor(run, advance_cursor(cursor_for(run), "stage_completed"))
        db.add(run)
        db.commit()
        return next_state(run)


async def data_design(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        stage_input = _design_input(run)
        cursor = cursor_for(run)
        if cursor.repair_mode == "full":

            async def invoke(payload, context):
                return await design_data(payload, context, validate_structure=False)

            attempt_input = stage_input
        else:
            from app.agents.roles.data_modeler import DataDesignAction

            action = DataDesignAction(
                mode=cursor.repair_mode,
                scope=cursor.repair_scope,
                previous=DataDesignOutput.model_validate(run.data_json),
                issues=(run.result_json or {}).get("issues", []),
            )

            async def invoke(_payload, context):
                return await design_data(stage_input, context, action=action, validate_structure=False)

            attempt_input = {"design": stage_input.model_dump(mode="json"), "action": action.model_dump(mode="json")}
        db.close()
        output = await _model_stage(db, run, "data_design", attempt_input, invoke, runtime.context)
        run.data_json = output.model_dump(mode="json")
        apply_cursor(run, advance_cursor(cursor_for(run), "stage_completed"))
        db.add(run)
        db.commit()
        return next_state(run)


async def structure_validation(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        issues = validate_model_v1(_design_input(run), DataDesignOutput.model_validate(run.data_json))
        if issues:
            decision = decide_structure_repair(
                issues, DataDesignOutput.model_validate(run.data_json), cursor_for(run).data_repairs
            )
            if decision.action == "repair_data":
                run.result_json = {"issues": issues}
                apply_cursor(
                    run,
                    advance_cursor(
                        cursor_for(run), "structure_repair_requested", repair_mode="fix", repair_scope=decision.scope
                    ),
                )
                db.commit()
                return next_state(run)
        verification = VerificationV1(
            structure="failed" if issues else "passed", semantic_review="not_run", database_execution="not_run"
        )
        run.result_json = {"issues": issues, "verification": verification.model_dump(mode="json")}
        apply_cursor(run, advance_cursor(cursor_for(run), "stage_completed", verification=verification))
        db.commit()
        return next_state(run)


async def render(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        artifacts = render_model_v1(_design_input(run), DataDesignOutput.model_validate(run.data_json))
        run.result_json = {
            **(run.result_json or {}),
            "schema_version": artifacts.schema_version,
            "model_hash": artifacts.model_hash,
            "mermaid_erd": artifacts.mermaid_erd,
            "postgresql_ddl": artifacts.postgresql_ddl,
            "trace_rows": [row.__dict__ for row in artifacts.trace_rows],
            "rule_findings": [item.model_dump(mode="json") for item in artifacts.review_candidates],
        }
        apply_cursor(run, advance_cursor(cursor_for(run), "stage_completed"))
        db.commit()
        return next_state(run)


async def semantic_review(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    from app.agents.roles.trace_reviewer import review_design

    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        from app.agents.modeling.design_model import ModelArtifactsV1, TraceRowV1
        from app.schemas.design_v1 import ReviewFindingV1

        result = run.result_json
        artifacts = ModelArtifactsV1(
            schema_version=result["schema_version"],
            model_hash=result["model_hash"],
            mermaid_erd=result["mermaid_erd"],
            postgresql_ddl=result["postgresql_ddl"],
            trace_rows=tuple(
                TraceRowV1(
                    requirement_id=row["requirement_id"],
                    kind=row["kind"],
                    status=row["status"],
                    function_ids=tuple(row["function_ids"]),
                    table_ids=tuple(row["table_ids"]),
                    column_ids=tuple(row["column_ids"]),
                    reason=row["reason"],
                )
                for row in result["trace_rows"]
            ),
            review_candidates=tuple(ReviewFindingV1.model_validate(item) for item in result["rule_findings"]),
        )
        analysis_input = _analysis_input(db, run)
        design_input = _design_input(run)

        started = time.monotonic()
        review = review_design(analysis_input, design_input, DataDesignOutput.model_validate(run.data_json), artifacts)
        _record_attempt(
            db,
            run,
            "semantic_review",
            1,
            {"model_hash": artifacts.model_hash},
            round((time.monotonic() - started) * 1000),
            output=review,
        )
        findings = review.findings
        result = dict(result)
        result["review"] = {"findings": [item.model_dump(mode="json") for item in findings], "summary": review.summary}
        run.result_json = result
        verification = VerificationV1(structure="passed", semantic_review="completed", database_execution="not_run")
        apply_cursor(run, advance_cursor(cursor_for(run), "stage_completed", verification=verification))
        db.add(run)
        db.commit()
        return next_state(run)


async def db_validation(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    from app.services.workflow_db_validation import validate_ddl

    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        model = DataDesignOutput.model_validate(run.data_json).data_model
        ddl = run.result_json["postgresql_ddl"]
        db.close()
        status, report = await asyncio.to_thread(
            validate_ddl,
            model,
            ddl,
            runtime.context.settings.validation_database_url.get_secret_value(),
        )
        result = dict(run.result_json)
        result["database_report"] = report
        run.result_json = result
        verification = VerificationV1(structure="passed", semantic_review="completed", database_execution=status)
        apply_cursor(run, advance_cursor(cursor_for(run), "stage_completed", verification=verification))
        db.add(run)
        db.commit()
        return next_state(run)


async def delivery(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    from app.models import Artifact, DocumentRevision
    from app.services.workflow_artifacts import write_artifacts

    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        result = dict(run.result_json or {})
        result.update(
            run_id=run.id,
            analysis=run.analysis_json,
            requirements_review=run.requirements_review_json,
            answers=run.answer_snapshot_json,
            functions=run.function_json,
            data=run.data_json,
        )
        cursor = cursor_for(run)
        result["verification"] = cursor.verification.model_dump(mode="json")
        findings = result.get("review", {}).get("findings", [])
        warnings = []
        if cursor.has_unresolved:
            unresolved_ids = [
                answer["question_id"]
                for answer in (run.answer_snapshot_json or {}).get("answers", [])
                if answer["unresolved"]
            ]
            warnings.append(f"미해결 질문: {', '.join(unresolved_ids)}")
        document = db.get(DocumentRevision, run.document_revision_id)
        warnings.extend(document.warnings_json if document is not None else [])
        if findings:
            warnings.append("검토 후보를 확인해야 합니다.")
        if cursor.verification.database_execution == "not_run" and cursor.verification.structure == "passed":
            warnings.append("DDL은 격리 DB에서 실행 검증하지 않은 초안입니다.")
        if cursor.verification.database_execution == "failed":
            warnings.append("DDL 실행 검증에 실패했습니다.")
        if cursor.verification.structure == "failed":
            warnings.append("구조 오류로 ERD·DDL 제공을 차단했습니다.")
        result["warnings"] = warnings
        manifests = write_artifacts(run.id, result, cursor.verification, runtime.context.settings.storage_root)
        for item in manifests:
            db.add(Artifact(run_id=run.id, **item))
        run.result_json = result
        apply_cursor(run, advance_cursor(cursor, "stage_completed", has_warnings=bool(warnings)))
        db.commit()
        return next_state(run)


async def guarded(node, state: WorkflowState, runtime: Runtime[WorkflowContext]):
    try:
        return await node(state, runtime)
    except Exception as exc:
        with runtime.context.session_factory() as db:
            run = db.get(DesignRun, state["run_id"])
            if run is None or run.status != "running":
                raise
            cursor = cursor_for(run)
            attempts = (
                db.scalar(
                    select(func.count())
                    .select_from(StageAttempt)
                    .where(StageAttempt.run_id == run.id, StageAttempt.stage == cursor.phase)
                )
                or 0
            )
            code = exc.code if isinstance(exc, AppError) else "stage_failed"
            failure = StageFailure(
                stage=cursor.phase,
                code=code,
                message=str(exc) if isinstance(exc, AppError) else "단계 실행에 실패했습니다.",
                retryable=False,
                attempts=attempts,
            )
            apply_cursor(run, advance_cursor(cursor, "technical_failure", failure=failure))
            db.commit()
            return next_state(run)


async def coordinator(state: WorkflowState, runtime: Runtime[WorkflowContext]):
    """영속 커서가 허용한 다음 단계를 그래프 상태로 돌려준다."""
    from app.agents.roles.coordinator import next_action

    with runtime.context.session_factory() as db:
        run = db.get(DesignRun, state["run_id"])
        decision = next_action(cursor_for(run))
        return {"run_id": run.id, "phase": run.phase, "stop": decision.action != "dispatch"}
