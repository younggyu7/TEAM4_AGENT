from typing import Any

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: Any = None


class HealthResponse(BaseModel):
    status: str = "ok"
