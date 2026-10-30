"""산출물 읽기 (화면 API): 프로젝트·설계 버전 목록, design.json, 산출물 원문, 화면에서 붙인 버전 제목.

읽기·쓰기는 산출물 저장소 포트(agents/storage)로 한다. v<N>/design.json이 있는 버전을 가진 것을 프로젝트로 본다
(output/semantic-review/고객요구사항5-review처럼 묶음 아래 프로젝트도 저장소가 찾는다).
프로젝트 ID나 이름이 저장소 밖을 가리키면 저장소가 ValueError를 내고, 여기서는 '없음'(None·False)으로 돌려준다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from app.agents.storage import get_store
from app.core.config import get_settings
from app.core.guards import is_version

FILES = {
    "design.json", "schema.sql", "erd.mmd", "erd.md", "review_report.md", "requirements.md",
    "functions.md", "traceability.md", "traceability.csv", "db_validation.md", "data_model.json", "functions.submitted.md",
    "semantic_review.md", "semantic_review.json",
    # 개발 스펙 (최종본, app/agents/tools/dev_spec.py)
    "AGENTS.md", "PRD.md", "ARCHITECTURE.md", "DATABASE.md", "API.md", "openapi.yaml", "ACCEPTANCE_CRITERIA.md", "TASKS.md",
    "DECISIONS.md",
}


def output_root() -> Path:
    """로컬 산출물 폴더 (작업이 CLI 하위 프로세스에 --output-dir로 넘긴다)."""
    return get_settings().output_dir.resolve()


def _json(text: str | None):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _label(v) -> str | None:
    return None if v is None else re.sub(r"^v", "", str(v), flags=re.I)


def version_key(v: str) -> list[int]:
    """'4.1' > '4' > '3.2' (점으로 나눈 숫자 비교)"""
    return [int(x) for x in v.split(".")]


def version_labels(project: str) -> set[str]:
    """공간이 있는 버전 번호 (작업이 새로 생긴 버전을 찾을 때)."""
    return get_store().version_labels(project)


def _project_versions(project: str) -> list[dict]:
    """versions.json이 있으면 버전 정보를 거기서, 없으면 design.json의 meta에서 가져온다."""
    store = get_store()
    labels = [n for n in store.version_labels(project) if store.exists(project, n, "design.json")]
    recorded = store.load_version_records(project)
    titles = store.load_titles(project)
    out = []
    for n in labels:
        record = next((v for v in recorded if _label(v.get("design_version")) == n), None)
        if record:
            item = {**record, "design_version": n, "parent": _label(record.get("parent")),
                    "base": _label(record.get("base"))}
        else:
            meta = (_json(store.read(project, n, "design.json")) or {}).get("meta") or {}
            item = {"design_version": n, "created_at": meta.get("generated_at"), "run_id": meta.get("run_id"),
                    "document": (meta.get("document") or {}).get("file_name")}
        if titles.get(n):
            item["title"] = titles[n]
        out.append(item)
    return sorted(out, key=lambda v: version_key(v["design_version"]), reverse=True)


def find_projects() -> list[dict]:
    projects = [{"id": p, "name": PurePosixPath(p).name, "versions": _project_versions(p)}
                for p in get_store().list_projects()]
    projects = [p for p in projects if p["versions"]]
    # 최근 버전이 위로
    projects.sort(key=lambda p: max((v.get("created_at") or "" for v in p["versions"]), default=""), reverse=True)
    return projects


def has_project(project: str) -> bool:
    """저장소 안에 있는 프로젝트인지 (밖을 가리키거나 없으면 False)."""
    return bool(project) and get_store().has_project(project)


def has_version(project: str, version: str) -> bool:
    return has_project(project) and version in get_store().version_labels(project)


def valid_file(project: str, version: str, name: str) -> bool:
    """화면이 받을 수 있는 산출물 이름·버전 형식인지 (허용 목록만)."""
    return bool(project) and is_version(version) and name in FILES


def read_file(project: str, version: str, name: str) -> str | None:
    """산출물 원문. 없거나 저장소 밖을 가리키면 None."""
    try:
        return get_store().read(project, version, name)
    except ValueError:
        return None


def _trace_links(text: str) -> list[dict]:
    """traceability.csv (from_id,to_id,link_type,design_version) → trace_links. 엑셀용 BOM이 붙어 있다."""
    header, *rows = [line for line in text.lstrip("﻿").splitlines() if line]
    cols = header.split(",")
    return [dict(zip(("from_id", "to_id", "link_type"),
                     (cells[cols.index(c)] for c in ("from_id", "to_id", "link_type")))) for cells in
            (line.split(",") for line in rows)]


def read_design(project: str, version: str) -> dict | None:
    """design.json. 새 design.json은 크기를 줄이려고 데이터 모델·추적 링크를 빼고 meta.files에 이름만 적으므로,
    예전 형태와 같은 모양(data_model·trace_links 포함)으로 맞춰서 준다. 없으면 None."""
    design = _json(read_file(project, version, "design.json"))
    if design is None:
        return None
    files = (design.get("meta") or {}).get("files")
    if not files or ("data_model" in design and "trace_links" in design):
        return design
    model = _json(read_file(project, version, files["data_model"])) if files.get("data_model") else None
    trace = read_file(project, version, files["trace_links"]) if files.get("trace_links") else None
    return {
        **design,
        "data_model": design.get("data_model") or model or {"entities": [], "relations": []},
        "trace_links": design.get("trace_links") or (_trace_links(trace) if trace else []),
    }


def save_title(project: str, version: str, title: str) -> None:
    store = get_store()
    titles = store.load_titles(project)
    if title.strip():
        titles[version] = title.strip()[:80]
    else:
        titles.pop(version, None)
    store.save_titles(project, titles)
