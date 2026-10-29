"""에이전트와 도메인 모델이 공유하는 기본 타입."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelCall(StrictModel):
    stage: Literal["analysis", "requirements_review", "function_design", "data_design", "semantic_review"]
    model: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
