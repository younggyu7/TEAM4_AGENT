"""보안: 허용할 프론트 출처(CORS)와 화면·로그에 나가는 글에서 비밀 값 가리기."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.core.config import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

# API 키 형태 (Anthropic sk-ant-…, OpenAI sk-…)와 접속 URL의 사용자·비밀번호
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")
_URL_USER = re.compile(r"//[^@/\s]*@")


def mask_url(url: str) -> str:
    """접속 URL에서 사용자·비밀번호를 가린다."""
    return _URL_USER.sub("//***@", url)


def redact(text: str) -> str:
    """작업 기록처럼 화면에 그대로 보여 주는 글에서 API 키·접속 비밀번호를 가린다."""
    return mask_url(_API_KEY.sub("sk-***", text))


def add_cors(app: FastAPI) -> None:
    """설정의 출처(CORS_ORIGINS)에서 오는 브라우저 요청만 허용한다. 로컬 개발 서버는 Vite proxy라 같은 출처다."""
    # CLI(Agent)도 mask_url을 쓰므로 FastAPI는 여기서만 불러온다
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(CORSMiddleware, allow_origins=list(get_settings().cors_origins),
                       allow_methods=["GET", "HEAD", "POST"], allow_headers=["*"])
