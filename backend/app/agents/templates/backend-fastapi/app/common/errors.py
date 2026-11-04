"""API 오류. 응답은 openapi.yaml의 Error 형식 {code, message}."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    @classmethod
    def not_implemented(cls, what: str) -> "ApiError":
        return cls(501, "NOT_IMPLEMENTED", "구현 전: " + what)

    @classmethod
    def not_found(cls) -> "ApiError":
        return cls(404, "NOT_FOUND", "대상을 찾을 수 없습니다.")


def not_implemented(what: str):
    """기본 동작을 만들 수 없는 API (lambda 안에서 쓴다)."""
    raise ApiError.not_implemented(what)
