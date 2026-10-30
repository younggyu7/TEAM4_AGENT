"""문서 업로드 실행 (작업).

  POST /runs   문서 업로드(multipart file, project?, title?, validate_db?) → 기능 명세 초안까지 실행 → {job}
"""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from app.agents.tools.document_parser import UnsupportedFormatError, check_supported
from app.api.v1.deps import _no_running_job
from app.core import guards
from app.core.config import get_settings
from app.core.exceptions import BadRequest, PayloadTooLarge
from app.services import jobs, output_store

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.post("/runs", status_code=202, summary="문서 업로드 → 기능 명세 초안까지 실행 (작업)")
async def create_run(
    file: Annotated[UploadFile, File(description="요구사항 문서 (PDF·DOCX·HWPX·TXT 등)")],
    project: Annotated[str, Form(description="프로젝트 이름. 생략하면 문서 파일 이름")] = "",
    title: Annotated[str, Form(description="사업 이름 (LLM에 사업 맥락으로 줌)")] = "",
    validate_db: Annotated[bool, Form(description="산출 후 별도 DB에 DDL 적용·검증")] = False,
):
    name = guards.upload_name(file.filename)
    project = guards.project_name(project or Path(name).stem)
    _no_running_job(project)
    # 올린 시각별 폴더에 원래 파일 이름 그대로 둔다 (산출물의 문서 이름이 원본과 같게)
    folder = get_settings().data_dir.resolve() / "uploads" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True)
    target = folder / name
    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > guards.MAX_UPLOAD:
                break
            out.write(chunk)
    if size > guards.MAX_UPLOAD:
        shutil.rmtree(folder, ignore_errors=True)
        raise PayloadTooLarge(f"문서가 너무 큽니다 (최대 {guards.MAX_UPLOAD // 1024 // 1024}MB).")
    try:
        check_supported(target)
    except UnsupportedFormatError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise BadRequest(str(e)) from e
    job = jobs.start_run(output_dir=output_store.output_root(), document=target, project=project,
                         title=title.strip(), validate_db=validate_db)
    return {"job": job.to_dict()}
