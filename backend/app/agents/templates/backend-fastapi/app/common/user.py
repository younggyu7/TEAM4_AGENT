"""현재 사용자: 포털 로그인(app.auth)의 세션에서 읽는다 (Authorization: Bearer 토큰). 로그인하지 않았으면 역할이 빈 값.

id는 시연 계정에 연결한 사용자 ID다 (app/accounts.json). 역할 헤더 같은 요청 값은 믿지 않는다."""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from fastapi import Request

from app.common.errors import ApiError

ALL = "전체 보기"


@dataclass(frozen=True)
class CurrentUser:
    id: str | None
    role: str
    system: bool = False   # X-Api-Key가 SYSTEM_API_KEY와 같다

    def is_system(self) -> bool:
        return self.system

    def require_system(self) -> None:
        if not self.system:
            raise ApiError(401, "UNAUTHORIZED", "외부 시스템 인증이 필요합니다.")

    def require_login(self) -> None:
        if not self.role:
            raise ApiError(401, "UNAUTHORIZED", "인증이 필요합니다.")

    def require(self, *roles: str) -> None:
        self.require_login()
        if self.role != ALL and self.role not in roles:
            raise ApiError(403, "FORBIDDEN", "이 역할은 쓸 수 없습니다: " + self.role)


def system_call(request: Request) -> bool:
    """외부 시스템의 호출인가: X-Api-Key가 환경 변수 SYSTEM_API_KEY와 같아야 한다. 키를 정하지 않으면 아무도 통과하지 못한다."""
    key = os.getenv("SYSTEM_API_KEY", "")
    given = request.headers.get("X-Api-Key")
    return bool(key) and given is not None and hmac.compare_digest(key.encode("utf-8"), given.encode("utf-8"))


def current_user(request: Request) -> CurrentUser:
    """로그인 미들웨어(main.py)가 붙인 사용자."""
    return getattr(request.state, "user", None) or CurrentUser(None, "", system_call(request))
