"""에이전트 호출에 주입하는 실행 환경."""

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings


@dataclass
class Context:
    settings: Settings = field(default_factory=get_settings)
    tracer: Any = None
