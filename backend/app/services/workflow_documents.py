"""업로드 파일을 비공개 저장소에 보관하고 위치 블록으로 추출한다."""

import uuid
from collections.abc import AsyncIterable
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import DocumentRevision
from app.services.document_extraction import MAX_FILE_BYTES, DocumentExtractionError, analyze_document
from app.services.workflow_store import get_project


async def save_document(
    db: Session, project_id: str, original_filename: str, chunks: AsyncIterable[bytes], settings: Settings
) -> DocumentRevision:
    get_project(db, project_id)
    original_name = Path(original_filename).name
    suffix = Path(original_name).suffix.lower()
    if not original_name or len(original_name) > 255 or suffix not in {".pdf", ".docx", ".txt"}:
        raise DocumentExtractionError("unsupported_format", "PDF·DOCX·UTF-8 TXT 파일만 지원합니다.", status_code=415)
    revision_id = str(uuid.uuid4())
    storage_key = f"documents/{revision_id}{suffix}"
    destination = settings.storage_root / storage_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with destination.open("xb") as sink:
            async for chunk in chunks:
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise DocumentExtractionError(
                        "file_too_large", "입력 파일은 10 MiB 이하여야 합니다.", status_code=413
                    )
                sink.write(chunk)
        extraction = await analyze_document(destination, revision_id)
        document = DocumentRevision(
            id=revision_id,
            project_id=project_id,
            original_name=original_name,
            storage_key=storage_key,
            sha256=extraction.sha256,
            extraction_version=extraction.normalization_version,
            status="ready",
            blocks_json=[block.model_dump(mode="json") for block in extraction.blocks],
            warnings_json=extraction.warnings,
        )
        db.add(document)
        db.commit()
        db.refresh(document)
        return document
    except Exception:
        db.rollback()
        destination.unlink(missing_ok=True)
        raise
