"""설계 그래프 단계별 오류 응답을 만드는 공통 경계."""

from app.core.exceptions import AppError


def failure(code: str, message: str, stage: str, status: int = 502, **detail) -> AppError:
    return AppError(message, {"stage": stage, **detail}, status_code=status, code=code)
