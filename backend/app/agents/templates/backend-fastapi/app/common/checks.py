"""요청 값 검사: 스펙의 필수 값·허용값을 코드가 만든 API가 부른다."""
from __future__ import annotations

from typing import Any

from app.common.errors import ApiError


def input_of(values: Any) -> dict:
    """요청 본문·쿼리를 고칠 수 있는 입력 묶음으로 바꾼다 (업무 로직이 값을 채울 수 있다)."""
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ApiError(400, "VALIDATION_FAILED", "요청 형식이 올바르지 않습니다.")
    return dict(values)


def required(data: dict, *names: str) -> None:
    for name in names:
        if data.get(name) is None or data.get(name) == "":
            raise ApiError(400, "VALIDATION_FAILED", "필수 값이 없습니다: " + name)


def one_of(data: dict, name: str, *allowed: str) -> None:
    value = data.get(name)
    if value is not None and _text(value) not in allowed:
        raise ApiError(400, "VALIDATION_FAILED", "허용되지 않은 값입니다: " + name)


def _text(value: Any) -> str:
    """Java toString과 같게: 참·거짓은 true/false."""
    return str(value).lower() if isinstance(value, bool) else str(value)
