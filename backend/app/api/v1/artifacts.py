"""산출물 조회.

  GET      /design?project=<id>&version=<N>        design.json (data_model·trace_links를 합쳐서)
  GET·HEAD /file?project=<id>&version=<N>&name=…   산출물 원문 (schema.sql, erd.mmd, 개발 스펙 API.md·openapi.yaml …, 허용 목록만)
  GET      /spec-format                            개발 스펙 문서의 형식 정의 (backend/SPEC_FORMAT.md, 모든 버전에 같다)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from app.api.v1.deps import _file
from app.core.exceptions import NotFound
from app.services import output_store

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그
SPEC_FORMAT = Path(__file__).resolve().parents[3] / "SPEC_FORMAT.md"


@router.get("/design", summary="설계 버전의 design.json")
def get_design(project: str, version: str):
    _file(project, version, "design.json")
    return JSONResponse(output_store.read_design(project, version))


@router.api_route("/file", methods=["GET", "HEAD"], summary="산출물 원문")
def get_file(project: str, version: str, name: str):
    media = "application/json" if name.endswith(".json") else "text/plain"
    return Response(_file(project, version, name), media_type=f"{media}; charset=utf-8")


@router.get("/spec-format", summary="개발 스펙 문서의 형식 정의")
def get_spec_format():
    if not SPEC_FORMAT.exists():
        raise NotFound("SPEC_FORMAT.md 파일이 없습니다.")
    return Response(SPEC_FORMAT.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")
