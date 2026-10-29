"""산출물 폴더(output/) 읽기: 프로젝트·설계 버전 목록, design.json, 산출물 원문, 화면에서 붙인 버전 제목.

v<N>/design.json이 있는 폴더를 프로젝트로 본다 (output/semantic-review/고객요구사항5-review처럼 아래 폴더도 찾음).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.config import get_settings
from app.core.guards import inside, is_version

FILES = {
    "design.json", "schema.sql", "erd.mmd", "erd.md", "review_report.md", "requirements.md",
    "functions.md", "traceability.md", "traceability.csv", "db_validation.md", "data_model.json", "functions.submitted.md",
    "semantic_review.md", "semantic_review.json",
}
VERSION_DIR = re.compile(r"^v(\d+(?:\.\d+)*)$")
MAX_DEPTH = 3
# 백엔드 versions.json과 따로 화면에서 붙인 버전 제목만 둔다
TITLES = "titles.json"


def output_root() -> Path:
    return get_settings().output_dir.resolve()


def _read_json(file: Path):
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _label(v) -> str | None:
    return None if v is None else re.sub(r"^v", "", str(v), flags=re.I)


def version_key(v: str) -> list[int]:
    """'4.1' > '4' > '3.2' (점으로 나눈 숫자 비교)"""
    return [int(x) for x in v.split(".")]


def version_labels(project_dir: Path) -> set[str]:
    if not project_dir.is_dir():
        return set()
    return {m[1] for d in project_dir.iterdir() if (m := VERSION_DIR.match(d.name))}


def _project_versions(folder: Path) -> list[dict]:
    """versions.json이 있으면 버전 정보를 거기서, 없으면 design.json의 meta에서 가져온다."""
    dirs = [d for d in folder.iterdir() if d.is_dir() and VERSION_DIR.match(d.name) and (d / "design.json").exists()]
    if not dirs:
        return []
    recorded = (_read_json(folder / "versions.json") or {}).get("versions") or []
    titles = _read_json(folder / TITLES) or {}
    out = []
    for d in dirs:
        n = VERSION_DIR.match(d.name)[1]
        record = next((v for v in recorded if _label(v.get("design_version")) == n), None)
        if record:
            item = {**record, "design_version": n, "parent": _label(record.get("parent")),
                    "base": _label(record.get("base"))}
        else:
            meta = (_read_json(d / "design.json") or {}).get("meta") or {}
            item = {"design_version": n, "created_at": meta.get("generated_at"), "run_id": meta.get("run_id"),
                    "document": (meta.get("document") or {}).get("file_name")}
        if titles.get(n):
            item["title"] = titles[n]
        out.append(item)
    return sorted(out, key=lambda v: version_key(v["design_version"]), reverse=True)


def find_projects() -> list[dict]:
    root = output_root()
    projects: list[dict] = []

    def walk(folder: Path, depth: int):
        versions = _project_versions(folder)
        if versions:
            projects.append({"id": folder.relative_to(root).as_posix(), "name": folder.name, "versions": versions})
            return
        if depth >= MAX_DEPTH:
            return
        for d in sorted(folder.iterdir()):
            if d.is_dir() and d.name != "llm_exchange" and not d.name.startswith("parse-"):
                walk(d, depth + 1)

    if root.is_dir():
        walk(root, 0)
    # 최근 버전이 위로
    projects.sort(key=lambda p: max((v.get("created_at") or "" for v in p["versions"]), default=""), reverse=True)
    return projects


def project_dir(project: str) -> Path | None:
    """output 안의 프로젝트 폴더. 밖을 가리키거나 없으면 None"""
    folder = inside(output_root(), project) if project else None
    return folder if folder and folder.is_dir() else None


def resolve_file(project: str, version: str, name: str) -> Path | None:
    """프로젝트·버전·파일 이름 → output 안의 실제 경로. 값이 올바르지 않거나 output 밖이면 None."""
    if not project or not is_version(version) or name not in FILES:
        return None
    return inside(output_root(), project, f"v{version}", name)


def _read_trace_csv(file: Path) -> list[dict]:
    """traceability.csv (from_id,to_id,link_type,design_version) → trace_links. 엑셀용 BOM이 붙어 있다."""
    if not file.exists():
        return []
    header, *rows = [line for line in file.read_text(encoding="utf-8-sig").splitlines() if line]
    cols = header.split(",")
    return [dict(zip(("from_id", "to_id", "link_type"),
                     (cells[cols.index(c)] for c in ("from_id", "to_id", "link_type")))) for cells in
            (line.split(",") for line in rows)]


def read_design(file: Path) -> dict:
    """새 design.json은 크기를 줄이려고 데이터 모델·추적 링크를 빼고 meta.files에 위치만 적는다.
    예전 형태와 같은 모양(data_model·trace_links 포함)으로 맞춰서 준다."""
    design = json.loads(file.read_text(encoding="utf-8"))
    files = (design.get("meta") or {}).get("files")
    if not files or ("data_model" in design and "trace_links" in design):
        return design
    folder = file.parent

    def located(name: str | None) -> Path | None:
        p = inside(folder, name) if name else None
        return p if p and p.exists() else None

    model_file, trace_file = located(files.get("data_model")), located(files.get("trace_links"))
    return {
        **design,
        "data_model": design.get("data_model") or (_read_json(model_file) if model_file else None)
        or {"entities": [], "relations": []},
        "trace_links": design.get("trace_links") or (_read_trace_csv(trace_file) if trace_file else []),
    }


def save_title(folder: Path, version: str, title: str) -> None:
    file = folder / TITLES
    titles = _read_json(file) or {}
    if title.strip():
        titles[version] = title.strip()[:80]
    else:
        titles.pop(version, None)
    file.write_text(json.dumps(titles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
