"""팀 개발을 위한 최소 회귀 검사: 입력 보존과 실패 격리."""

import asyncio
import hashlib

from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.flow import runner, stages
from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.db.session import get_db
from app.main import app
from app.models import Base, DocumentRevision, StageAttempt
from app.schemas.design import ModelCall
from app.schemas.document import DocumentBlock
from app.schemas.workflow import ProjectCreate, RunCreate
from app.services import workflow_store


def test_document_upload_preserves_source_and_location(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def database():
        with Session() as db:
            yield db

    settings = Settings(_env_file=None, storage_root=tmp_path / "private", initialize_database=False)
    app.dependency_overrides = {get_db: database, get_settings: lambda: settings}
    try:
        client = TestClient(app)
        project = client.post("/api/v1/projects", json={"name": "ITSM", "description": ""}).json()
        source = "\ufeff\n요청은 1.5초 안에 처리한다.\n\n승인 이력을 남긴다.\n".encode()
        response = client.post(
            f"/api/v1/projects/{project['id']}/documents",
            files={"file": ("requirements.txt", source, "text/plain")},
        )
        assert response.status_code == 202, response.text
        document = response.json()
        assert document["sha256"] == hashlib.sha256(source).hexdigest()
        assert [(item["locator"]["start_line"], item["normalized_text"]) for item in document["blocks"]] == [
            (2, "요청은 1.5초 안에 처리한다."),
            (4, "승인 이력을 남긴다."),
        ]
        assert list((settings.storage_root / "documents").glob("*"))
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_failed_analysis_is_not_published_as_a_completed_design(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    revision_id = "11111111-1111-1111-1111-111111111111"
    block = DocumentBlock(
        document_revision_id=revision_id,
        block_id="B-1",
        locator={"kind": "txt", "start_line": 1, "end_line": 1},
        raw_text="요청을 등록한다.",
        normalized_text="요청을 등록한다.",
        normalization_version="1",
    )

    async def invalid_analysis(*_args, **_kwargs):
        raise AppError("근거가 일치하지 않습니다.", status_code=502, code="invalid_analysis_reference")

    monkeypatch.setattr(stages, "analyze", invalid_analysis)
    with Session() as db:
        project = workflow_store.create_project(db, ProjectCreate(name="ITSM", description=""))
        db.add(
            DocumentRevision(
                id=revision_id,
                project_id=project.id,
                original_name="source.txt",
                storage_key="documents/source.txt",
                sha256="0" * 64,
                extraction_version="1",
                status="ready",
                blocks_json=[block.model_dump(mode="json")],
                warnings_json=[],
            )
        )
        db.commit()
        run, _ = workflow_store.create_run(db, project.id, RunCreate(document_revision_id=revision_id), "start")
        run_id = run.id

        class RecordedOutput(BaseModel):
            value: str

        async def valid_model_call(*_args, **_kwargs):
            return RecordedOutput(value="ok"), ModelCall(
                stage="analysis",
                model="test",
                duration_ms=1,
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )

        asyncio.run(
            stages._model_stage(
                db,
                run,
                "analysis",
                {"document_revision_id": revision_id},
                valid_model_call,
                stages.WorkflowContext(
                    settings=Settings(_env_file=None, storage_root=tmp_path), session_factory=Session
                ),
            )
        )
        assert db.scalar(select(StageAttempt).where(StageAttempt.run_id == run_id)) is not None
    asyncio.run(runner.process_run(run_id, Settings(_env_file=None, storage_root=tmp_path), Session))
    with Session() as db:
        run = workflow_store.get_run(db, run_id)
        assert run.status == "failed"
        assert run.result_json is None
        assert run.cursor_json["failure"]["code"] == "invalid_analysis_reference"
    engine.dispose()
