"""실행별 산출물의 상태별 제공, 원자 저장과 해시 검증 다운로드."""

import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.modeling.design_model import model_hash_v1
from app.core.config import Settings
from app.core.exceptions import AppError, NotFound
from app.models import Artifact, DesignRun
from app.schemas.design_v1 import ArtifactManifestV1, DataDesignOutput, VerificationV1

_FILES = {
    "requirements": ("requirements.md", "text/markdown; charset=utf-8"),
    "functions": ("functions.md", "text/markdown; charset=utf-8"),
    "tables": ("tables.md", "text/markdown; charset=utf-8"),
    "review": ("review.md", "text/markdown; charset=utf-8"),
    "traceability": ("traceability.csv", "text/csv; charset=utf-8"),
    "erd": ("erd.mmd", "text/plain; charset=utf-8"),
    "sql": ("schema.sql", "text/plain; charset=utf-8"),
    "result": ("result.json", "application/json; charset=utf-8"),
}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ArtifactDownload:
    content: bytes
    media_type: str
    filename: str


def _run_key(run_id: str) -> str:
    try:
        return str(UUID(run_id))
    except (ValueError, AttributeError):
        raise AppError("실행 ID가 올바르지 않습니다.", status_code=422, code="INVALID_RUN_ID") from None


def _allowed(verification: VerificationV1, *, failed_run: bool = False) -> tuple[str, ...]:
    if failed_run or verification.structure == "not_run":
        return ("review", "result")
    if verification.structure == "failed":
        return ("requirements", "functions", "tables", "review", "result")
    if verification.semantic_review != "completed":
        return ("review", "result")
    kinds = ("requirements", "functions", "tables", "review", "traceability", "erd", "result")
    return (*kinds, "sql") if verification.database_execution != "failed" else kinds


def visible_result(result: dict, verification: VerificationV1, *, failed_run: bool = False) -> dict:
    """다운로드가 금지된 DDL 본문을 결과 JSON에서도 제외한다."""
    visible = dict(result)
    if "sql" not in _allowed(verification, failed_run=failed_run):
        visible.pop("postgresql_ddl", None)
    return visible


def _validation_status(kind: str, verification: VerificationV1) -> str:
    if kind == "sql":
        return verification.database_execution
    if kind == "review":
        return {"completed": "passed", "failed": "failed", "not_run": "not_run"}[verification.semantic_review]
    return verification.structure


def _safe_text(value: object) -> str:
    """고객 문구가 Markdown의 HTML 또는 테이블 구분자로 해석되지 않게 한다."""
    text = html.escape(str(value or ""), quote=False).replace("\r", " ").replace("\n", " ")
    for marker in ("\\", "|", "[", "]", "(", ")", "!"):
        text = text.replace(marker, "\\" + marker)
    return text


def _header(title: str, run_id: str, verification: VerificationV1, warnings: list) -> list[str]:
    lines = [
        f"# {title}",
        "",
        f"- 실행 ID: `{run_id}`",
        f"- 구조 검사: `{verification.structure}`",
        f"- 의미 검토: `{verification.semantic_review}`",
        f"- DB 실행 검증: `{verification.database_execution}`",
        f"- 경고·미해결 항목: {len(warnings)}건",
        "",
    ]
    if warnings:
        lines.extend(["## 경고·미해결", "", *[f"- {_safe_text(item)}" for item in warnings], ""])
    return lines


def _requirements(result: dict, run_id: str, verification: VerificationV1, warnings: list) -> str:
    lines = _header("요구사항", run_id, verification, warnings)
    analysis = result.get("analysis") or {}
    for item in analysis.get("requirements", []):
        lines.extend(
            [
                f"## {_safe_text(item.get('id'))} {_safe_text(item.get('text'))}",
                "",
                f"- 분류: {_safe_text(item.get('kind'))}",
                f"- 출처: {_safe_text(item.get('provenance'))}",
                f"- 우선순위: {_safe_text(item.get('priority'))} — {_safe_text(item.get('priority_reason'))}",
            ]
        )
        for evidence in item.get("evidence", []):
            lines.append(
                f"- 근거: `{_safe_text(evidence.get('block_id'))}` "
                f"[{evidence.get('start')}, {evidence.get('end')}): {_safe_text(evidence.get('quote'))}"
            )
        lines.append("")
    questions = analysis.get("questions", [])
    if questions:
        lines.extend(["## 확인 질문", ""])
        for question in questions:
            lines.append(f"- `{_safe_text(question.get('id'))}` {_safe_text(question.get('question'))}")
    return "\n".join(lines) + "\n"


def _functions(result: dict, run_id: str, verification: VerificationV1, warnings: list) -> str:
    lines = _header("기능 명세", run_id, verification, warnings)
    for item in (result.get("functions") or {}).get("functions", []):
        lines.extend(
            [
                f"## {_safe_text(item.get('id'))} {_safe_text(item.get('name'))}",
                "",
                _safe_text(item.get("description")),
                "",
                f"- 담당 역할: {_safe_text(item.get('actor'))}",
                f"- 요구사항: {', '.join(map(_safe_text, item.get('requirement_ids', [])))}",
                f"- 입력: {', '.join(map(_safe_text, item.get('inputs', [])))}",
                f"- 출력: {', '.join(map(_safe_text, item.get('outputs', [])))}",
            ]
        )
        for rule in item.get("rules", []):
            lines.append(f"- 규칙: {_safe_text(rule)}")
        for exception in item.get("exceptions", []):
            lines.append(f"- 예외: {_safe_text(exception)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _tables(result: dict, run_id: str, verification: VerificationV1, warnings: list) -> str:
    lines = _header("테이블 정의", run_id, verification, warnings)
    model = (result.get("data") or {}).get("data_model") or {}
    for table in model.get("tables", []):
        lines.extend(
            [
                f"## {_safe_text(table.get('id'))} `{_safe_text(table.get('name'))}`",
                "",
                _safe_text(table.get("description")),
                "",
                "| 컬럼 ID | 이름 | 타입 | NULL | 설명 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for column in table.get("columns", []):
            lines.append(
                "| "
                + " | ".join(
                    map(
                        _safe_text,
                        (
                            column.get("id"),
                            column.get("name"),
                            column.get("type"),
                            column.get("nullable"),
                            column.get("description"),
                        ),
                    )
                )
                + " |"
            )
        lines.extend(
            [
                "",
                f"- PK: {', '.join(map(_safe_text, table.get('primary_key', [])))}",
                f"- 요구사항: {', '.join(map(_safe_text, table.get('requirement_ids', [])))}",
                f"- 기능: {', '.join(map(_safe_text, table.get('function_ids', [])))}",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def _review(result: dict, run_id: str, verification: VerificationV1, warnings: list) -> str:
    lines = _header("검토 보고서", run_id, verification, warnings)
    review = result.get("review") or {}
    findings = review.get("findings", []) if isinstance(review, dict) else review
    for item in findings:
        lines.extend(
            [
                f"## {_safe_text(item.get('id'))} {_safe_text(item.get('type'))}",
                "",
                f"- 심각도: {_safe_text(item.get('severity'))}",
                f"- 관련 ID: {', '.join(map(_safe_text, item.get('related_ids', [])))}",
                f"- 설명: {_safe_text(item.get('description'))}",
                f"- 권고: {_safe_text(item.get('suggestion'))}",
                "",
            ]
        )
    if not findings:
        lines.append("검토 후보가 없습니다.")
    return "\n".join(lines) + "\n"


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


def _traceability(result: dict, run_id: str, verification: VerificationV1) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "run_id",
            "structure_status",
            "database_status",
            "requirement_id",
            "kind",
            "status",
            "function_ids",
            "table_ids",
            "column_ids",
            "reason",
        )
    )
    for value in result.get("trace_rows") or []:
        row = asdict(value) if is_dataclass(value) else value
        writer.writerow(
            [
                _csv_cell(item)
                for item in (
                    run_id,
                    verification.structure,
                    verification.database_execution,
                    row.get("requirement_id"),
                    row.get("kind"),
                    row.get("status"),
                    ";".join(row.get("function_ids") or []),
                    ";".join(row.get("table_ids") or []),
                    ";".join(row.get("column_ids") or []),
                    row.get("reason"),
                )
            ]
        )
    return output.getvalue()


def _contents(run_id: str, result: dict, verification: VerificationV1) -> dict[str, bytes]:
    warnings = result.get("warnings") or []
    available = {
        "requirements": _requirements(result, run_id, verification, warnings),
        "functions": _functions(result, run_id, verification, warnings),
        "tables": _tables(result, run_id, verification, warnings),
        "review": _review(result, run_id, verification, warnings),
        "traceability": _traceability(result, run_id, verification),
        "erd": (
            f"%% run_id={run_id}; structure={verification.structure}; db={verification.database_execution}; "
            f"unresolved={len(warnings)}\n" + (result.get("mermaid_erd") or "")
        ),
        "sql": (
            f"-- run_id={run_id}; db_validation={verification.database_execution}; unresolved={len(warnings)}\n"
            + (
                "-- 격리 PostgreSQL 검증 통과\n"
                if verification.database_execution == "passed"
                else "-- 미검증 초안: DB 실행 검증 미실시\n"
            )
            + (result.get("postgresql_ddl") or "")
        ),
        "result": json.dumps(
            {**visible_result(result, verification), "run_id": run_id, "verification": verification.model_dump()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=lambda value: asdict(value) if is_dataclass(value) else str(value),
        ),
    }
    return {kind: available[kind].encode("utf-8") for kind in _allowed(verification)}


def _model_hash(result: dict) -> str:
    try:
        model = DataDesignOutput.model_validate(result["data"]).data_model
        digest = model_hash_v1(model)
    except (KeyError, TypeError, ValidationError):
        raise AppError("데이터 모델 스냅샷이 없습니다.", status_code=422, code="ARTIFACT_MODEL_INVALID") from None
    supplied = result.get("model_hash")
    if supplied is not None and (not _DIGEST.fullmatch(str(supplied)) or supplied != digest):
        raise AppError("산출물의 모델 해시가 일치하지 않습니다.", status_code=409, code="MODEL_HASH_MISMATCH")
    return digest


def _manifest(run_id: str, model_hash: str, verification: VerificationV1, payloads: dict[str, bytes]) -> list[dict]:
    records = []
    for kind, content in payloads.items():
        validated = ArtifactManifestV1(
            kind=kind,
            run_id=run_id,
            schema_version="1",
            model_hash=model_hash,
            file_hash=hashlib.sha256(content).hexdigest(),
            validation_status=_validation_status(kind, verification),
        )
        records.append(
            {
                "kind": validated.kind,
                "storage_key": f"artifacts/{run_id}/{_FILES[kind][0]}",
                "file_hash": validated.file_hash,
                "model_hash": validated.model_hash,
                "validation_status": validated.validation_status,
            }
        )
    return records


def _existing_matches(final_dir: Path, payloads: dict[str, bytes]) -> bool:
    if not final_dir.is_dir() or final_dir.is_symlink():
        return False
    if {path.name for path in final_dir.iterdir()} != {_FILES[kind][0] for kind in payloads}:
        return False
    return all(
        not (path := final_dir / _FILES[kind][0]).is_symlink() and path.read_bytes() == content
        for kind, content in payloads.items()
    )


def write_artifacts(run_id: str, result: dict, verification: VerificationV1, storage_root: Path) -> list[dict]:
    """모든 파일을 임시 폴더에 쓴 뒤 실행 폴더를 한 번에 공개한다. DB 기록은 호출자가 커밋한다."""
    key = _run_key(run_id)
    model_hash = _model_hash(result)
    if verification.structure == "passed":
        if not result.get("mermaid_erd") or not result.get("postgresql_ddl"):
            raise AppError("검증된 ERD·DDL 원본이 없습니다.", status_code=409, code="ARTIFACT_SOURCE_MISSING")
        if verification.semantic_review == "completed":
            report = result.get("database_report")
            if (
                not isinstance(report, dict)
                or report.get("status") != verification.database_execution
                or report.get("model_hash") != model_hash
            ):
                raise AppError(
                    "DB 검증 보고서가 실행 상태와 일치하지 않습니다.", status_code=409, code="DB_REPORT_MISMATCH"
                )
    result["model_hash"] = model_hash
    payloads = _contents(key, result, verification)
    manifest = _manifest(key, model_hash, verification, payloads)
    parent = Path(storage_root) / "artifacts"
    parent.mkdir(parents=True, exist_ok=True)
    final_dir = parent / key
    if final_dir.exists():
        if _existing_matches(final_dir, payloads):
            return manifest
        raise AppError("기존 실행 산출물과 내용이 다릅니다.", status_code=409, code="ARTIFACT_CONFLICT")
    staged = Path(tempfile.mkdtemp(prefix=".stage-", dir=parent))
    try:
        for kind, content in payloads.items():
            path = staged / _FILES[kind][0]
            with path.open("xb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        try:
            staged.rename(final_dir)
        except OSError:
            if _existing_matches(final_dir, payloads):
                return manifest
            raise AppError("산출물 저장 충돌이 발생했습니다.", status_code=409, code="ARTIFACT_CONFLICT") from None
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return manifest


def open_artifact(db: Session, run_id: str, kind: str, settings: Settings) -> ArtifactDownload:
    """DB에 커밋된 파일만 상태·모델/파일 해시·경로를 재확인한 뒤 반환한다."""
    key = _run_key(run_id)
    if kind not in _FILES:
        raise NotFound("산출물 종류를 찾을 수 없습니다.")
    run = db.get(DesignRun, key)
    if run is None:
        raise NotFound("실행을 찾을 수 없습니다.")
    if run.result_json is None:
        raise AppError("결과가 아직 준비되지 않았습니다.", status_code=409, code="RESULT_NOT_READY")
    verification = VerificationV1.model_validate(run.cursor_json["verification"])
    if kind not in _allowed(verification, failed_run=run.status == "failed"):
        raise AppError("현재 검증 상태에서 이 산출물을 제공할 수 없습니다.", status_code=409, code="ARTIFACT_BLOCKED")
    record = db.scalar(select(Artifact).where(Artifact.run_id == key, Artifact.kind == kind))
    if record is None:
        raise NotFound("저장된 산출물을 찾을 수 없습니다.")
    expected_key = f"artifacts/{key}/{_FILES[kind][0]}"
    if record.storage_key != expected_key or record.validation_status != _validation_status(kind, verification):
        raise AppError("산출물 메타데이터가 현재 실행과 일치하지 않습니다.", status_code=409, code="ARTIFACT_MISMATCH")
    if record.model_hash != run.result_json.get("model_hash"):
        raise AppError("모델 해시가 일치하지 않습니다.", status_code=409, code="MODEL_HASH_MISMATCH")
    root = settings.storage_root.resolve()
    candidate = root / expected_key
    path = candidate.resolve()
    if candidate.is_symlink() or not path.is_relative_to(root) or not path.is_file():
        raise AppError("산출물 파일을 찾을 수 없습니다.", status_code=500, code="STORAGE_FAILED")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != record.file_hash:
        raise AppError("산출물 파일 해시가 일치하지 않습니다.", status_code=500, code="STORAGE_FAILED")
    filename, media_type = _FILES[kind]
    return ArtifactDownload(content=content, media_type=media_type, filename=filename)
