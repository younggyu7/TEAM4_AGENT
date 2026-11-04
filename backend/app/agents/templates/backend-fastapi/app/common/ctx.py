"""업무 로직에 넘기는 실행 정보: DB와 현재 사용자."""
from __future__ import annotations

from dataclasses import dataclass

from app.common.db import Db
from app.common.errors import ApiError
from app.common.user import CurrentUser


@dataclass
class Ctx:
    db: Db
    user: CurrentUser

    def fail(self, status: int, code: str, message: str) -> ApiError:
        return ApiError(status, code, message)
