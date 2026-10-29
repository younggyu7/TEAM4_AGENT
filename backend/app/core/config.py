"""실행 설정. 환경변수(.env)를 읽고, CLI 인자로 일부를 덮어쓴다."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# DDL 검증 기본 DB: docker-compose.yml의 validation-db (docker compose up -d validation-db)
DEFAULT_VALIDATION_DB_URL = "postgresql://reqagent:reqagent@localhost:55432/reqagent"
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def _validation_db_url() -> str:
    """VALIDATION_DB_URL: 비우면 기본 PostgreSQL(validation-db), 'sqlite'면 SQLite로만 검증 (빈 문자열로 표현)."""
    value = os.getenv("VALIDATION_DB_URL", "").strip()
    if value.lower() == "sqlite":
        return ""
    return value or DEFAULT_VALIDATION_DB_URL


def _int(name: str, default: int) -> int:
    """환경변수를 정수로 읽는다 (비어 있으면 기본값)."""
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _bool(name: str, default: bool) -> bool:
    """환경변수를 참/거짓으로 읽는다 (비어 있으면 기본값)."""
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "y"} if value else default


@dataclass(frozen=True)
class Settings:
    """실행 설정. .env 값으로 만들고 CLI 인자로 일부를 덮어쓴다."""
    llm_provider: str = "auto"
    openai_model: str = "gpt-5.4-mini"
    openai_reasoning_effort: str = "low"
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "high"
    llm_concurrency: int = 4
    req_batch_size: int = 3
    func_group_size: int = 12
    func_common_decisions: bool = True                   # 기능 명세 전에 공통 결정(용어·분류값·공통 규칙)을 먼저 정함
    func_effort: str = ""                                # 기능 명세 그룹·확인 질문 요청의 생각 수준 (빈 값이면 ANTHROPIC_EFFORT·OPENAI_REASONING_EFFORT)
    func_question_ratio: float = 0.1                     # 기능 명세 확인 대상 후보 중 고객에게 묻는 비율 (나머지는 추천안으로 정함)
    attr_batch_size: int = 5
    max_steps: int = 40
    max_fix_attempts: int = 2
    max_db_attempts: int = 2
    data_dir: Path = Path("data")                          # 입력 문서 폴더 (parse 기본 대상, run에서 이름만 줘도 찾음)
    output_dir: Path = Path("output")
    llm_exchange_dir: Path = Path("output/llm_exchange")   # manual 공급자 요청·응답 파일
    validation_db_url: str = DEFAULT_VALIDATION_DB_URL   # 빈 문자열이면 SQLite로만 검증
    keep_validation_schema: bool = False
    semantic_review: bool = True                         # 의미 검토 Agent 실행 여부 (run --no-semantic-review로 끔)
    requirement_review: bool = True                      # 요구사항 검토 Agent(원문 대조·충돌 검토) 실행 여부
    req_review_batch: int = 12                           # 원문 대조 요청 1건에 넣는 원문 단위 수 (보내는 내용이 작아 크게)
    req_review_coverage: float = 0.6                     # 원문 문장이 요구사항에 '담김'으로 보는 겹침 비율 (이보다 낮은 문장만 누락 검사로 보냄)
    req_review_effort: str = "low"                       # 요구사항 검토 요청의 생각 수준 (대조·판정 위주라 낮게. 빈 값이면 실행 설정)
    log_level: str = "INFO"                              # API 서버 로그 수준
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS  # 브라우저 요청을 허용할 프론트 주소 (로컬 Vite 개발 서버는 proxy라 없어도 됨)

    @classmethod
    def from_env(cls) -> "Settings":
        """환경변수에서 설정을 읽는다."""
        return cls(
            llm_provider=os.getenv("LLM_PROVIDER", "auto").strip().lower() or "auto",
            openai_model=os.getenv("OPENAI_MODEL", cls.openai_model),
            openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", cls.openai_reasoning_effort).strip(),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", cls.anthropic_model),
            anthropic_effort=os.getenv("ANTHROPIC_EFFORT", cls.anthropic_effort),
            llm_concurrency=_int("LLM_CONCURRENCY", cls.llm_concurrency),
            req_batch_size=_int("REQ_BATCH_SIZE", cls.req_batch_size),
            func_group_size=_int("FUNC_GROUP_SIZE", cls.func_group_size),
            func_common_decisions=_bool("FUNC_COMMON_DECISIONS", True),
            func_effort=os.getenv("FUNC_EFFORT", "").strip().lower(),
            func_question_ratio=float(os.getenv("FUNC_QUESTION_RATIO", "").strip() or cls.func_question_ratio),
            attr_batch_size=_int("ATTR_BATCH_SIZE", cls.attr_batch_size),
            max_steps=_int("MAX_STEPS", cls.max_steps),
            max_fix_attempts=_int("MAX_FIX_ATTEMPTS", cls.max_fix_attempts),
            max_db_attempts=_int("MAX_DB_ATTEMPTS", cls.max_db_attempts),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            output_dir=Path(os.getenv("OUTPUT_DIR", "output")),
            llm_exchange_dir=Path(os.getenv("LLM_EXCHANGE_DIR", "output/llm_exchange")),
            validation_db_url=_validation_db_url(),
            keep_validation_schema=_bool("KEEP_VALIDATION_SCHEMA", False),
            semantic_review=_bool("SEMANTIC_REVIEW", True),
            requirement_review=_bool("REQUIREMENT_REVIEW", True),
            req_review_batch=_int("REQ_REVIEW_BATCH", cls.req_review_batch),
            req_review_coverage=float(os.getenv("REQ_REVIEW_COVERAGE", "").strip() or cls.req_review_coverage),
            req_review_effort=os.getenv("REQ_REVIEW_EFFORT", cls.req_review_effort).strip().lower(),
            log_level=os.getenv("LOG_LEVEL", "").strip().upper() or cls.log_level,
            cors_origins=tuple(o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip())
            or DEFAULT_CORS_ORIGINS,
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    """현재 설정 (처음 한 번 환경변수에서 읽어 재사용)."""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def override_settings(**changes) -> Settings:
    """CLI 인자 등으로 설정 일부를 바꾼다. None 값은 무시한다."""
    global _settings
    changes = {k: v for k, v in changes.items() if v is not None}
    _settings = replace(get_settings(), **changes)
    return _settings
