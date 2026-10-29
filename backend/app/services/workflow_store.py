"""프로젝트·실행 저장 및 질문 대기/답변 동결의 트랜잭션 경계."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.flow.transitions import RunCursor, StageFailure, advance_cursor, apply_cursor
from app.agents.modeling.validation import validate_answer_snapshot
from app.core.exceptions import AppError, NotFound, ValidationFailed
from app.models import ClarificationAnswer, DesignRun, DocumentRevision, Project, RequestKey, ReviewDecision
from app.schemas.design_v1 import AnalysisOutput, AnswerSnapshot, AnswerV1
from app.schemas.workflow import AnswerUpdate, ProjectCreate, ReviewDecisionCreate, RunContinue, RunCreate

ACTIVE = ("queued", "running", "awaiting_input")
COMPLETED = ("completed", "completed_with_warnings", "completed_with_errors")


def _conflict(code: str, message: str) -> AppError:
    return AppError(message, status_code=409, code=code)


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_project(db: Session, body: ProjectCreate) -> Project:
    project = Project(name=body.name, description=body.description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def list_projects(db: Session, *, limit: int, offset: int) -> list[dict]:
    if not 1 <= limit <= 100 or offset < 0:
        raise ValidationFailed("limit 또는 offset이 범위를 벗어났습니다.")
    projects = db.scalars(select(Project).order_by(Project.created_at.desc()).limit(limit).offset(offset)).all()
    return [
        {"id": project.id, "name": project.name, "description": project.description, "created_at": project.created_at}
        for project in projects
    ]


def project_summary(db: Session, project_id: str) -> dict:
    project = get_project(db, project_id)
    runs = db.scalars(
        select(DesignRun).where(DesignRun.project_id == project_id).order_by(DesignRun.created_at.desc())
    ).all()
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at,
        "runs": [{"id": run.id, "status": run.status, "phase": run.phase} for run in runs],
    }


def document_summary(document: DocumentRevision) -> dict:
    return {
        "id": document.id,
        "project_id": document.project_id,
        "original_name": document.original_name,
        "sha256": document.sha256,
        "extraction_version": document.extraction_version,
        "status": document.status,
        "blocks": document.blocks_json,
        "warnings": document.warnings_json,
        "created_at": document.created_at,
    }


def get_project(db: Session, project_id: str, *, lock: bool = False) -> Project:
    query = select(Project).where(Project.id == project_id)
    if lock:
        query = query.with_for_update()
    project = db.scalar(query)
    if project is None:
        raise NotFound("프로젝트를 찾을 수 없습니다.")
    return project


def get_document(db: Session, document_id: str) -> DocumentRevision:
    document = db.get(DocumentRevision, document_id)
    if document is None:
        raise NotFound("문서 리비전을 찾을 수 없습니다.")
    return document


def get_run(db: Session, run_id: str, *, lock: bool = False) -> DesignRun:
    query = select(DesignRun).where(DesignRun.id == run_id)
    if lock:
        query = query.with_for_update()
    run = db.scalar(query)
    if run is None:
        raise NotFound("실행을 찾을 수 없습니다.")
    return run


def create_run(db: Session, project_id: str, body: RunCreate, key: str) -> tuple[DesignRun, bool]:
    if not key or len(key) > 255:
        raise ValidationFailed("Idempotency-Key가 필요합니다.")
    get_project(db, project_id, lock=True)
    request_hash = _hash(body.model_dump(mode="json"))
    old_key = db.scalar(
        select(RequestKey).where(
            RequestKey.project_id == project_id,
            RequestKey.operation == "start",
            RequestKey.key == key,
        )
    )
    if old_key is not None:
        if old_key.request_hash != request_hash:
            raise _conflict("IDEMPOTENCY_CONFLICT", "같은 멱등 키로 다른 실행 요청을 보냈습니다.")
        return get_run(db, old_key.run_id), False
    document = get_document(db, body.document_revision_id)
    if document.project_id != project_id or document.status != "ready":
        raise ValidationFailed("이 프로젝트의 준비된 문서 리비전이 아닙니다.")
    if db.scalar(select(DesignRun.id).where(DesignRun.project_id == project_id, DesignRun.status.in_(ACTIVE))):
        raise _conflict("ACTIVE_RUN_EXISTS", "프로젝트에 진행 중인 실행이 있습니다.")
    if body.parent_run_id is not None:
        parent = get_run(db, body.parent_run_id)
        if parent.project_id != project_id:
            raise ValidationFailed("상위 실행이 다른 프로젝트에 속합니다.")
    cursor = RunCursor(status="queued", phase="analysis")
    run = DesignRun(
        project_id=project_id,
        document_revision_id=document.id,
        parent_run_id=body.parent_run_id,
        status=cursor.status,
        phase=cursor.phase,
        cursor_json=cursor.model_dump(mode="json"),
        config_snapshot={},
    )
    db.add(run)
    try:
        db.flush()
        db.add(RequestKey(project_id=project_id, operation="start", key=key, request_hash=request_hash, run_id=run.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.scalar(
            select(RequestKey).where(
                RequestKey.project_id == project_id,
                RequestKey.operation == "start",
                RequestKey.key == key,
            )
        )
        if previous is not None:
            if previous.request_hash != request_hash:
                raise _conflict("IDEMPOTENCY_CONFLICT", "같은 멱등 키로 다른 실행 요청을 보냈습니다.") from None
            return get_run(db, previous.run_id), False
        raise _conflict("ACTIVE_RUN_EXISTS", "프로젝트에 진행 중인 실행이 있습니다.") from None
    db.refresh(run)
    return run, True


def latest_answers(db: Session, run_id: str) -> dict[str, ClarificationAnswer]:
    rows = db.scalars(
        select(ClarificationAnswer)
        .where(ClarificationAnswer.run_id == run_id)
        .order_by(ClarificationAnswer.question_id, ClarificationAnswer.version.desc())
    ).all()
    return {row.question_id: row for row in reversed(rows)}


def update_answer(db: Session, run_id: str, question_id: str, body: AnswerUpdate) -> ClarificationAnswer:
    run = get_run(db, run_id, lock=True)
    if (
        run.status != "awaiting_input"
        or run.analysis_json is None
        or (run.requirements_review_json or {}).get("verdict") != "approved"
    ):
        raise _conflict("RUN_NOT_AWAITING_INPUT", "현재 실행은 답변 대기 중이 아닙니다.")
    analysis = AnalysisOutput.model_validate(run.analysis_json)
    if question_id not in {question.id for question in analysis.questions}:
        raise NotFound("질문을 찾을 수 없습니다.")
    previous = latest_answers(db, run_id).get(question_id)
    actual_version = previous.version if previous else 0
    if body.expected_version != actual_version:
        raise _conflict("VERSION_CONFLICT", "답변 버전이 변경되었습니다. 최신 답변을 조회하세요.")
    try:
        answer = AnswerV1(
            question_id=question_id,
            version=actual_version + 1,
            text=body.text,
            unresolved=body.unresolved,
            created_at=datetime.now(UTC),
        )
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    row = ClarificationAnswer(run_id=run_id, **answer.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict("VERSION_CONFLICT", "답변 버전이 변경되었습니다. 최신 답변을 조회하세요.") from None
    db.refresh(row)
    return row


def continue_run(db: Session, run_id: str, body: RunContinue, key: str) -> tuple[DesignRun, bool]:
    if not key or len(key) > 255:
        raise ValidationFailed("Idempotency-Key가 필요합니다.")
    run = get_run(db, run_id, lock=True)
    request_hash = _hash(body.model_dump(mode="json"))
    old_key = db.scalar(
        select(RequestKey).where(
            RequestKey.project_id == run.project_id,
            RequestKey.operation == f"continue:{run_id}",
            RequestKey.key == key,
        )
    )
    if old_key is not None:
        if old_key.request_hash != request_hash:
            raise _conflict("IDEMPOTENCY_CONFLICT", "같은 멱등 키로 다른 진행 요청을 보냈습니다.")
        return run, False
    if (
        run.status != "awaiting_input"
        or run.analysis_json is None
        or (run.requirements_review_json or {}).get("verdict") != "approved"
    ):
        raise _conflict("RUN_NOT_AWAITING_INPUT", "현재 실행은 답변 대기 중이 아닙니다.")
    analysis = AnalysisOutput.model_validate(run.analysis_json)
    question_ids = {question.id for question in analysis.questions}
    if set(body.expected_versions) != question_ids or not set(body.unresolved_question_ids) <= question_ids:
        raise ValidationFailed("모든 질문의 최신 답변 버전을 제출해야 합니다.")
    previous = latest_answers(db, run_id)
    now = datetime.now(UTC)
    answers = []
    for question in analysis.questions:
        current = previous.get(question.id)
        version = current.version if current else 0
        if body.expected_versions[question.id] != version:
            raise _conflict("VERSION_CONFLICT", "답변 버전이 변경되었습니다. 최신 답변을 조회하세요.")
        if current is None:
            if question.id not in body.unresolved_question_ids:
                raise ValidationFailed("미답변 질문은 미해결로 명시해야 합니다.")
            row = ClarificationAnswer(
                run_id=run_id,
                question_id=question.id,
                version=1,
                text=None,
                unresolved=True,
                created_at=now,
            )
            db.add(row)
            db.flush()
            current = row
        elif question.id in body.unresolved_question_ids and not current.unresolved:
            raise ValidationFailed("답변된 질문은 최신 버전을 미해결로 변경한 뒤 진행하세요.")
        answers.append(
            AnswerV1(
                question_id=question.id,
                version=current.version,
                text=current.text,
                unresolved=current.unresolved,
                created_at=current.created_at.replace(tzinfo=UTC)
                if current.created_at.tzinfo is None
                else current.created_at,
            )
        )
    snapshot = AnswerSnapshot(answers=answers, frozen_at=now)
    issues = validate_answer_snapshot(analysis.questions, snapshot)
    if issues:
        raise ValidationFailed("답변 스냅샷이 유효하지 않습니다.", detail=issues)
    cursor = advance_cursor(
        RunCursor.model_validate(run.cursor_json), "continue", answer_snapshot=snapshot, questions=analysis.questions
    )
    run.answer_snapshot_json = snapshot.model_dump(mode="json")
    apply_cursor(run, cursor)
    db.add(
        RequestKey(
            project_id=run.project_id,
            operation=f"continue:{run_id}",
            key=key,
            request_hash=request_hash,
            run_id=run_id,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict("VERSION_CONFLICT", "실행 상태 또는 답변 버전이 변경되었습니다.") from None
    db.refresh(run)
    return run, True


def supersede_run(db: Session, run_id: str) -> DesignRun:
    run = get_run(db, run_id, lock=True)
    try:
        cursor = advance_cursor(RunCursor.model_validate(run.cursor_json), "supersede")
    except ValueError as exc:
        raise _conflict("RUN_NOT_AWAITING_INPUT", "답변 대기 실행만 교체할 수 있습니다.") from exc
    apply_cursor(run, cursor)
    db.commit()
    db.refresh(run)
    return run


def fail_interrupted_runs(db: Session) -> int:
    """프로세스 재시작 뒤 더는 실행되지 않는 queued/running 작업을 실패로 기록한다."""
    runs = db.scalars(select(DesignRun).where(DesignRun.status.in_(("queued", "running")))).all()
    for run in runs:
        cursor = RunCursor.model_validate(run.cursor_json)
        failure = StageFailure(
            stage=cursor.phase,
            code="WORKER_INTERRUPTED",
            message="작업자가 중단되었습니다. 새 실행을 시작하세요.",
            retryable=False,
            attempts=0,
        )
        next_cursor = advance_cursor(cursor, "technical_failure", failure=failure)
        apply_cursor(run, next_cursor)
    db.commit()
    return len(runs)


def record_review_decision(db: Session, run_id: str, body: ReviewDecisionCreate) -> ReviewDecision:
    run = get_run(db, run_id)
    if run.status not in COMPLETED:
        raise _conflict("RESULT_NOT_READY", "완료된 실행의 검토 항목만 처분할 수 있습니다.")
    findings = (run.result_json or {}).get("review", {}).get("findings", [])
    if body.finding_id not in {finding["id"] for finding in findings}:
        raise NotFound("검토 항목을 찾을 수 없습니다.")
    row = ReviewDecision(
        run_id=run_id,
        finding_id=body.finding_id,
        disposition=body.disposition,
        reason=body.reason,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def review_decisions(db: Session, run_id: str) -> list[dict]:
    rows = db.scalars(
        select(ReviewDecision).where(ReviewDecision.run_id == run_id).order_by(ReviewDecision.created_at)
    ).all()
    return [
        {
            "id": row.id,
            "finding_id": row.finding_id,
            "disposition": row.disposition,
            "reason": row.reason,
            "created_at": row.created_at,
        }
        for row in rows
    ]
