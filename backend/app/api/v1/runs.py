"""문서 업로드 실행 (작업).

  POST /runs          문서 업로드(multipart file, project?, title?, model?, effort?, members?)
                      → 새 프로젝트면 담당자 = 고른 회원 + 올린 사람 (담당자·PM만 그 프로젝트를 봄)
                      → (Slack이 켜져 있으면 프로젝트 비공개 채널을 만들고 담당자 초대) → 기능 명세 초안까지 실행 → {job, slack}
  GET  /llm-options   업로드 화면에서 고를 모델·생각 수준 (키가 있는 공급자만)
"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from starlette.concurrency import run_in_threadpool

from app.agents.tools.document_parser import UPLOAD_NOTE, UPLOAD_SUFFIXES, UnsupportedFormatError, check_upload
from app.api.v1.deps import _no_running_job, current_user, ensure
from app.core import guards
from app.agents.llm.client import resolve_provider
from app.core.config import ANTHROPIC_MODELS, EFFORTS, get_settings
from app.core.exceptions import BadRequest, Conflict, PayloadTooLarge
from app.services import jobs, output_store, project_access, slack, users

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


def llm_options() -> dict:
    """키가 있는 공급자의 모델과 생각 수준, 지금 .env 기본값. 모델마다 effort를 받는지(Haiku 4.5는 없음)도 준다."""
    s = get_settings()
    models = []
    if os.getenv("ANTHROPIC_API_KEY"):
        known = {m[0] for m in ANTHROPIC_MODELS}
        extra = [] if s.anthropic_model in known else [(s.anthropic_model, s.anthropic_model, ".env의 ANTHROPIC_MODEL")]
        models += [{"id": i, "label": label, "note": note, "provider": "anthropic", "effort": not i.startswith("claude-haiku-4")}
                   for i, label, note in [*ANTHROPIC_MODELS, *extra]]
    if os.getenv("OPENAI_API_KEY"):
        models.append({"id": s.openai_model, "label": s.openai_model, "note": ".env의 OPENAI_MODEL", "provider": "openai",
                       "effort": True})
    try:
        provider = resolve_provider()
    except RuntimeError:
        provider = ""
    default_model = {"anthropic": s.anthropic_model, "openai": s.openai_model}.get(provider, "")
    default_effort = {"anthropic": s.anthropic_effort, "openai": s.openai_reasoning_effort}.get(provider, "")
    return {"models": models, "efforts": list(EFFORTS), "default_model": default_model,
            "default_effort": default_effort if default_effort in EFFORTS else "medium"}


@router.get("/llm-options", summary="업로드에서 고를 LLM 모델·생각 수준")
def get_llm_options():
    return llm_options()


@router.post("/runs", status_code=202, summary="문서 업로드 → 기능 명세 초안까지 실행 (작업)")
async def create_run(
    file: Annotated[UploadFile, File(description="요구사항 문서 (PDF·DOCX·HWPX만, 스캔본·이미지는 안 됨)")],
    project: Annotated[str, Form(description="프로젝트 이름. 생략하면 문서 파일 이름")] = "",
    title: Annotated[str, Form(description="사업 이름 (LLM에 사업 맥락으로 줌)")] = "",
    model: Annotated[str, Form(description="LLM 모델 ID (GET /llm-options의 models, 비우면 .env)")] = "",
    effort: Annotated[str, Form(description="생각 수준 low·medium·high (비우면 .env)")] = "",
    members: Annotated[str, Form(description="담당자 회원 ID (쉼표로 구분, GET /members). 올린 사람·PM은 늘 담당자. "
                                             "담당자만 이 프로젝트를 보고 Slack 채널에 초대된다")] = "",
    member: users.Member = Depends(current_user),
):
    ensure(member, "upload")
    try:
        chosen = [int(x) for x in members.split(",") if x.strip()]
    except ValueError as e:
        raise BadRequest("members는 회원 ID를 쉼표로 이은 값입니다.") from e
    if model and model not in {m["id"] for m in llm_options()["models"]}:
        raise BadRequest(f"고를 수 없는 모델입니다: {model}")
    if effort and effort not in EFFORTS:
        raise BadRequest(f"생각 수준은 {', '.join(EFFORTS)} 중 하나입니다.")
    name = guards.upload_name(file.filename)
    if Path(name).suffix.lower() not in UPLOAD_SUFFIXES:   # 저장하기 전에 확장자로 먼저 막는다
        raise BadRequest(f"지원하지 않는 형식입니다: {Path(name).suffix or '(확장자 없음)'}. {UPLOAD_NOTE}")
    project = guards.project_name(project or Path(name).stem)
    existing = output_store.has_project(project)
    if existing and not project_access.can_see(member, project):
        raise Conflict("같은 이름의 프로젝트가 이미 있습니다 (담당자만 볼 수 있음). 다른 회사 이름을 적어 주세요.")
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
        check_upload(target)
    except UnsupportedFormatError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise BadRequest(f"{e} — {e.hint}" if e.hint and e.hint not in str(e) else str(e)) from e
    # 담당자: 새 프로젝트만 정한다 (같은 프로젝트에 다시 올리면 그대로 두고, 바꾸기는 PM이 프로젝트 화면에서)
    if not existing:
        await run_in_threadpool(project_access.set_members, project, [member.id, *chosen])
    # Slack(.env SLACK_BOT_TOKEN이 있을 때): 프로젝트 채널을 만들고 담당자를 초대한다. 실패해도 분석은 하고,
    # 서버가 나중에 다시 만든다 (services/slack.ensure_channels)
    connected = None
    if slack.enabled():
        try:
            assigned = [m.id for m in project_access.assignees(project)]
            connected = await run_in_threadpool(slack.create_channel, project, assigned, member)
        except slack.SlackError as e:
            connected = {"error": f"Slack 채널을 만들지 못했습니다: {e} (서버가 잠시 뒤 다시 만들어 봅니다)"}
    job = jobs.start_run(output_dir=output_store.output_root(), document=target, project=project,
                         title=title.strip(), model=model, effort=effort,
                         on_done=lambda v: output_store.record_maker(project, v, member))
    return {"job": job.to_dict(), "slack": connected}
