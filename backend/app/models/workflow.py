"""프로젝트, 문서 리비전과 설계 실행의 영속 레코드."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(UTC)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    documents: Mapped[list["DocumentRevision"]] = relationship(back_populates="project")
    runs: Mapped[list["DesignRun"]] = relationship(back_populates="project")


class DocumentRevision(Base):
    __tablename__ = "document_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    blocks_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    warnings_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    project: Mapped[Project] = relationship(back_populates="documents")


class DesignRun(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index(
            "uq_runs_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'awaiting_input')"),
            sqlite_where=text("status IN ('queued', 'running', 'awaiting_input')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    document_revision_id: Mapped[str] = mapped_column(ForeignKey("document_revisions.id"), nullable=False)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    cursor_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    analysis_json: Mapped[dict | None] = mapped_column(JSON)
    requirements_review_json: Mapped[dict | None] = mapped_column(JSON)
    answer_snapshot_json: Mapped[dict | None] = mapped_column(JSON)
    function_json: Mapped[dict | None] = mapped_column(JSON)
    data_json: Mapped[dict | None] = mapped_column(JSON)
    result_json: Mapped[dict | None] = mapped_column(JSON)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    project: Mapped[Project] = relationship(back_populates="runs")


class ClarificationAnswer(Base):
    __tablename__ = "clarification_answers"
    __table_args__ = (UniqueConstraint("run_id", "question_id", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    question_id: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    unresolved: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class StageAttempt(Base):
    __tablename__ = "stage_attempts"
    __table_args__ = (UniqueConstraint("run_id", "stage", "attempt_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_json: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    token_usage: Mapped[dict | None] = mapped_column(JSON)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    finding_id: Mapped[str] = mapped_column(String(40), nullable=False)
    disposition: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class RequestKey(Base):
    __tablename__ = "request_keys"
    __table_args__ = (UniqueConstraint("project_id", "operation", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
