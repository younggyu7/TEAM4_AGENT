"""요청 값 검사: 화면·업로드에서 온 값이 산출물 폴더 밖을 가리키거나 형식이 틀리면 막는다."""
from __future__ import annotations

import re
from pathlib import Path

from app.core.exceptions import BadRequest

VERSION = re.compile(r"^\d+(\.\d+)*$")
MAX_UPLOAD = 50 * 1024 * 1024


def inside(root: Path, *parts: str) -> Path | None:
    """root 아래 경로. '..'·절대 경로로 root 밖(또는 root 자체)을 가리키면 None"""
    root = root.resolve()
    path = root.joinpath(*parts).resolve()
    return path if path.is_relative_to(root) and path != root else None


def is_version(value: str | None) -> bool:
    return bool(VERSION.match(value or ""))


def version(value: str | None, field: str = "version") -> str:
    """'v2.1'·'2.1' → '2.1'. 형식이 틀리면 400"""
    label = re.sub(r"^v", "", (value or "").strip(), flags=re.I)
    if not is_version(label):
        raise BadRequest(f"{field} 값이 올바르지 않습니다.")
    return label


def project_name(value: str) -> str:
    """새 프로젝트 이름 (output 아래 폴더 이름 하나). 경로 구분자·숨김 폴더 이름은 막는다"""
    name = value.strip()
    if not name or "/" in name or "\\" in name or name.startswith(".") or len(name) > 100:
        raise BadRequest("project 값이 올바르지 않습니다.")
    return name


def upload_name(filename: str | None) -> str:
    """업로드 파일 이름에서 경로를 떼고 이름만 남긴다"""
    name = Path(filename or "").name
    if not name or name.startswith("."):
        raise BadRequest("파일 이름이 올바르지 않습니다.")
    return name
