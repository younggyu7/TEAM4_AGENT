"""산출물 조회.

  GET      /design?project=<id>&version=<N>        design.json (data_model·trace_links를 합쳐서)
  GET·HEAD /file?project=<id>&version=<N>&name=…   산출물 원문 (schema.sql, erd.mmd …, 허용 목록만)
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from app.api.v1.deps import _file
from app.services import output_store

router = APIRouter(tags=["projects"])   # 화면 문서(OpenAPI)에서 한 묶음으로 보이게 같은 태그


@router.get("/design", summary="설계 버전의 design.json")
def get_design(project: str, version: str):
    _file(project, version, "design.json")
    return JSONResponse(output_store.read_design(project, version))


@router.api_route("/file", methods=["GET", "HEAD"], summary="산출물 원문")
def get_file(project: str, version: str, name: str):
    media = "application/json" if name.endswith(".json") else "text/plain"
    return Response(_file(project, version, name), media_type=f"{media}; charset=utf-8")
