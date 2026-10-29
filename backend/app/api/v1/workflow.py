"""프로젝트·문서·단계별 실행 API."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.v1.deps import SettingsDep
from app.core.exceptions import AppError
from app.db.session import get_db
from app.schemas.workflow import AnswerUpdate, ProjectCreate, ReviewDecisionCreate, RunContinue, RunCreate
from app.services import workflow_store

router = APIRouter(tags=["workflow"])
DbDep = Annotated[Session, Depends(get_db)]


def _run_summary(run, db: Session | None = None) -> dict:
    analysis = run.analysis_json or {}
    review = run.requirements_review_json or {}
    answers = workflow_store.latest_answers(db, run.id) if db is not None else {}
    return {
        "id": run.id,
        "project_id": run.project_id,
        "document_revision_id": run.document_revision_id,
        "parent_run_id": run.parent_run_id,
        "status": run.status,
        "phase": run.phase,
        "verification": run.cursor_json["verification"],
        "failure": run.cursor_json.get("failure"),
        "questions": analysis.get("questions", []) if review.get("verdict") == "approved" else [],
        "requirements_review": review or None,
        "answers": [
            {
                "question_id": answer.question_id,
                "version": answer.version,
                "text": answer.text,
                "unresolved": answer.unresolved,
                "created_at": answer.created_at,
            }
            for answer in answers.values()
        ],
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _schedule_run(request: Request, run_id: str, settings) -> None:
    from app.agents.flow.runner import process_run

    task = asyncio.create_task(process_run(run_id, settings))
    tasks = getattr(request.app.state, "workflow_tasks", None)
    if tasks is None:
        tasks = request.app.state.workflow_tasks = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, db: DbDep):
    project = workflow_store.create_project(db, body)
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "created_at": project.created_at,
    }


@router.get("/projects")
def list_projects(db: DbDep, limit: int = 20, offset: int = 0):
    return workflow_store.list_projects(db, limit=limit, offset=offset)


@router.get("/projects/{project_id}")
def get_project(project_id: str, db: DbDep):
    return workflow_store.project_summary(db, project_id)


@router.post("/projects/{project_id}/documents", status_code=202)
async def upload_document(project_id: str, file: Annotated[UploadFile, File()], db: DbDep, settings: SettingsDep):
    from app.services.workflow_documents import save_document

    async def chunks():
        while chunk := await file.read(64 * 1024):
            yield chunk

    try:
        document = await save_document(db, project_id, file.filename or "", chunks(), settings)
    finally:
        await file.close()
    return workflow_store.document_summary(document)


@router.get("/documents/{document_id}")
def get_document(document_id: str, db: DbDep):
    return workflow_store.document_summary(workflow_store.get_document(db, document_id))


@router.post("/projects/{project_id}/runs", status_code=202)
async def create_run(
    project_id: str,
    body: RunCreate,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header()] = None,
):
    run, created = workflow_store.create_run(db, project_id, body, idempotency_key or "")
    if created:
        _schedule_run(request, run.id, settings)
    return _run_summary(run, db)


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: DbDep):
    return _run_summary(workflow_store.get_run(db, run_id), db)


@router.put("/runs/{run_id}/answers/{question_id}")
def update_answer(run_id: str, question_id: str, body: AnswerUpdate, db: DbDep):
    answer = workflow_store.update_answer(db, run_id, question_id, body)
    return {
        "question_id": answer.question_id,
        "version": answer.version,
        "text": answer.text,
        "unresolved": answer.unresolved,
        "created_at": answer.created_at,
    }


@router.post("/runs/{run_id}/continue", status_code=202)
async def continue_run(
    run_id: str,
    body: RunContinue,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header()] = None,
):
    run, created = workflow_store.continue_run(db, run_id, body, idempotency_key or "")
    if created:
        _schedule_run(request, run.id, settings)
    return _run_summary(run, db)


@router.post("/runs/{run_id}/supersede")
def supersede_run(run_id: str, db: DbDep):
    return _run_summary(workflow_store.supersede_run(db, run_id), db)


@router.get("/runs/{run_id}/result")
def get_result(run_id: str, db: DbDep):
    from app.schemas.design_v1 import VerificationV1
    from app.services.workflow_artifacts import visible_result

    run = workflow_store.get_run(db, run_id)
    if run.result_json is None or run.status not in workflow_store.COMPLETED:
        raise AppError("결과가 아직 준비되지 않았습니다.", status_code=409, code="RESULT_NOT_READY")
    verification = VerificationV1.model_validate(run.cursor_json["verification"])
    return {
        **visible_result(run.result_json, verification, failed_run=run.status == "failed"),
        "review_decisions": workflow_store.review_decisions(db, run_id),
    }


@router.post("/runs/{run_id}/review-decisions", status_code=201)
def create_review_decision(run_id: str, body: ReviewDecisionCreate, db: DbDep):
    row = workflow_store.record_review_decision(db, run_id, body)
    return {
        "id": row.id,
        "finding_id": row.finding_id,
        "disposition": row.disposition,
        "reason": row.reason,
        "created_at": row.created_at,
    }


@router.get("/runs/{run_id}/artifacts/{kind}")
def get_artifact(run_id: str, kind: str, db: DbDep, settings: SettingsDep):
    from app.services.workflow_artifacts import open_artifact

    artifact = open_artifact(db, run_id, kind, settings)
    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
    )
