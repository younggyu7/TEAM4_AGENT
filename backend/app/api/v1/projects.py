"""프로젝트·설계 버전 API: 산출물 조회, 버전 제목, 새 버전 만들기(작업), 문서 업로드 실행.

  GET  /projects                               프로젝트와 설계 버전 목록 (버전 번호는 '4', '4.1' 같은 문자열)
  GET  /design?project=<id>&version=<N>        design.json (data_model·trace_links를 합쳐서)
  GET  /file?project=<id>&version=<N>&name=…   산출물 원문 (schema.sql, erd.mmd …)
  POST /projects/title                         버전 제목 저장
  POST /projects/versions                      기능 명세 내보내기·ERD 저장·확인 질문 답 반영 → {job}
  POST /runs                                   문서 업로드 → 전체 실행 → {job}
  GET  /capabilities                           백엔드 rerun이 기능 명세 수정본을 받는지 {design, spec_md}
  GET  /jobs?id=<job>                          작업 진행 상황
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.agents.tools.document_parser import UnsupportedFormatError, check_supported
from app.core import guards
from app.core.config import get_settings
from app.core.exceptions import BadRequest, Conflict, NotFound, PayloadTooLarge
from app.schemas.api import TitleUpdate, VersionCreate
from app.services import jobs, output_store

router = APIRouter(tags=["projects"])


def _project_dir(project: str) -> Path:
    folder = output_store.project_dir(project)
    if folder is None:
        raise BadRequest("project 값이 올바르지 않습니다.")
    return folder


def _file(project: str, version: str, name: str) -> Path:
    file = output_store.resolve_file(project, version, name)
    if file is None:
        raise BadRequest("project·version·name 값이 올바르지 않습니다.")
    if not file.exists():
        raise NotFound(f"{name} 파일이 없습니다.")
    return file


def _no_running_job(project: str) -> None:
    if jobs.running_job(project):
        raise Conflict("이 프로젝트에서 이미 작업이 진행 중입니다.")


@router.get("/projects", summary="프로젝트와 설계 버전 목록")
def list_projects():
    return {"output_dir": str(output_store.output_root()), "projects": output_store.find_projects()}


@router.get("/design", summary="설계 버전의 design.json")
def get_design(project: str, version: str):
    return JSONResponse(output_store.read_design(_file(project, version, "design.json")))


@router.api_route("/file", methods=["GET", "HEAD"], summary="산출물 원문")
def get_file(project: str, version: str, name: str):
    media = "application/json" if name.endswith(".json") else "text/plain"
    return FileResponse(_file(project, version, name), media_type=f"{media}; charset=utf-8")


@router.post("/projects/title", summary="버전 제목 저장")
def set_title(body: TitleUpdate):
    folder = _project_dir(body.project)
    output_store.save_title(folder, guards.version(body.version), body.title)
    return {"ok": True}


@router.post("/projects/versions", status_code=202, summary="새 설계 버전 만들기 (작업)")
def create_version(body: VersionCreate):
    folder = _project_dir(body.project)
    base = guards.version(body.base, "base")
    if not (folder / f"v{base}").is_dir():
        raise NotFound(f"기준 버전 v{base}이 없습니다.")
    _no_running_job(body.project)
    title = (body.title or "").strip()
    job = jobs.start_rerun(
        output_dir=output_store.output_root(), project=body.project, base=base, source=body.source,
        data_model=body.data_model, design=body.design, spec_md=body.functions_md, spec_summary=body.spec_summary,
        answers=body.answers, feedback=body.feedback,
        on_done=(lambda v: output_store.save_title(folder, v, title)) if title else None,
    )
    return {"job": job.to_dict()}


@router.post("/runs", status_code=202, summary="문서 업로드 → 전체 실행 (작업)")
async def create_run(
    file: Annotated[UploadFile, File(description="요구사항 문서 (PDF·DOCX·HWPX·TXT 등)")],
    project: Annotated[str, Form(description="프로젝트 이름. 생략하면 문서 파일 이름")] = "",
    title: Annotated[str, Form(description="사업 이름 (LLM에 사업 맥락으로 줌)")] = "",
    validate_db: Annotated[bool, Form(description="산출 후 별도 DB에 DDL 적용·검증")] = False,
):
    name = guards.upload_name(file.filename)
    project = guards.project_name(project or Path(name).stem)
    _no_running_job(project)
    folder = get_settings().data_dir.resolve() / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{name}"
    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > guards.MAX_UPLOAD:
                break
            out.write(chunk)
    if size > guards.MAX_UPLOAD:
        target.unlink(missing_ok=True)
        raise PayloadTooLarge(f"문서가 너무 큽니다 (최대 {guards.MAX_UPLOAD // 1024 // 1024}MB).")
    try:
        check_supported(target)
    except UnsupportedFormatError as e:
        target.unlink(missing_ok=True)
        raise BadRequest(str(e)) from e
    job = jobs.start_run(output_dir=output_store.output_root(), document=target, project=project,
                         title=title.strip(), validate_db=validate_db)
    return {"job": job.to_dict()}


@router.get("/capabilities", summary="백엔드 rerun이 기능 명세 수정본을 받는지")
def get_capabilities():
    return jobs.capabilities()


@router.get("/jobs", summary="작업 진행 상황")
def get_job(id: str):
    job = jobs.get_job(id)
    if job is None:
        raise NotFound("작업을 찾을 수 없습니다. 서버를 다시 띄우면 작업 기록이 사라집니다.")
    return {"job": job.to_dict()}
