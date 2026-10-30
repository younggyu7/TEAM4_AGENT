"""실행 설정. 환경변수(프로젝트 루트 TEAM4_AGENT/.env)를 읽고, CLI 인자로 일부를 덮어쓴다."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[2]   # backend/
PROJECT_ROOT = BACKEND_ROOT.parent                    # TEAM4_AGENT/ (.env·compose.yaml·Dockerfile)

# 이미 있는 환경변수(docker env_file·셸에서 준 값)가 우선. 루트에 .env가 없으면(docker 이미지 안) 환경변수만 쓴다
load_dotenv(PROJECT_ROOT / ".env")

# DDL 검증 기본 DB: 루트 compose.yaml의 db 컨테이너 안 reqagent_validation DB (서비스 DB reqagent와 따로)
DEFAULT_VALIDATION_DB_URL = "postgresql://reqagent:reqagent@localhost:55432/reqagent_validation"
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def _validation_db_url() -> str:
    """VALIDATION_DB_URL: 비우면 기본 PostgreSQL(db 컨테이너의 reqagent_validation), 'sqlite'면 SQLite로만 검증 (빈 문자열로 표현)."""
    value = os.getenv("VALIDATION_DB_URL", "").strip()
    if value.lower() == "sqlite":
        return ""
    return value or DEFAULT_VALIDATION_DB_URL


def _dir(name: str, default: str) -> Path:
    """폴더 경로 환경변수. 상대 경로는 backend/ 기준 (서버를 어느 폴더에서 띄워도 같은 곳)"""
    path = Path(os.getenv(name, "").strip() or default)
    return path if path.is_absolute() else BACKEND_ROOT / path


def _int(name: str, default: int) -> int:
    """환경변수를 정수로 읽는다 (비어 있으면 기본값)."""
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _bool(name: str, default: bool) -> bool:
    """환경변수를 참/거짓으로 읽는다 (비어 있으면 기본값)."""
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "y"} if value else default


SEMANTIC_REVIEW_MODES = ("auto", "final", "off")


def _semantic_review_mode(default: str) -> str:
    """SEMANTIC_REVIEW_MODE: auto(산출물이 바뀔 때마다) / final(ERD 최종 확정 때만, review 명령) / off.
    SEMANTIC_REVIEW=false면 off (예전 설정 호환)."""
    if not _bool("SEMANTIC_REVIEW", True):
        return "off"
    value = os.getenv("SEMANTIC_REVIEW_MODE", "").strip().lower() or default
    if value not in SEMANTIC_REVIEW_MODES:
        raise ValueError(f"SEMANTIC_REVIEW_MODE는 {'·'.join(SEMANTIC_REVIEW_MODES)} 중 하나입니다: {value}")
    return value


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
    func_assumption_limit: float = 0.1                   # 기능 명세 '(가정)' 규칙 한도 (전체 업무 규칙 대비). 넘으면 질문으로 정한다. 1이면 묻지 않음
    func_max_questions: int = 12                         # 기능 명세 확인 질문 상한 (한도를 넘어 덮지 못한 것은 '(확인 필요)'로 남아 사람이 확인)
    attr_batch_size: int = 5
    max_steps: int = 40
    max_fix_attempts: int = 2                            # Agent 실패·이슈 자동 수정 횟수 (아래 단계별 값이 0이면 이 값)
    func_fix_attempts: int = 0                           # 기능 명세: 누락 요구사항 재설계·실패 재시도 횟수 (0이면 max_fix_attempts)
    model_fix_attempts: int = 0                          # 데이터 설계: 모델 오류 수정·연결 보완·실패 재시도 횟수 (0이면 max_fix_attempts)
    max_db_attempts: int = 2
    data_dir: Path = Path("data")                          # 입력 문서 폴더 (parse 기본 대상, run에서 이름만 줘도 찾음)
    output_dir: Path = Path("output")
    llm_exchange_dir: Path = Path("output/llm_exchange")   # manual 공급자 요청·응답 파일
    database_url: str = ""                              # 서비스 DB (PostgreSQL). 비우면 output 폴더·SQLite 체크포인트 (로컬·테스트)
    validation_db_url: str = DEFAULT_VALIDATION_DB_URL   # 빈 문자열이면 SQLite로만 검증
    keep_validation_schema: bool = False
    semantic_review: bool = True                         # 의미 검토 Agent 실행 여부 (run --no-semantic-review로 끔)
    semantic_review_mode: str = "auto"                   # auto: 산출물이 바뀔 때마다 / final: ERD 최종 확정 때만 (review) / off
    semantic_review_attempts: int = 0                    # 의미 검토 실패 시 시도 횟수 (0이면 max_fix_attempts)
    semantic_review_source: bool = False                 # 최종 확정 검토에서 기능 명세 ↔ 요구사항(원문)도 볼지 (기본은 ERD ↔ 기능 명세만)
    requirement_review: bool = True                      # 요구사항 검토 Agent(원문 대조·충돌 검토) 실행 여부
    req_review_batch: int = 12                           # 원문 대조 요청 1건에 넣는 원문 단위 수 (보내는 내용이 작아 크게)
    req_review_coverage: float = 0.6                     # 원문 문장이 요구사항에 '담김'으로 보는 겹침 비율 (이보다 낮은 문장만 누락 검사로 보냄)
    req_review_effort: str = "low"                       # 요구사항 검토 요청의 생각 수준 (대조·판정 위주라 낮게. 빈 값이면 실행 설정)
    api_design: bool = True                              # API 설계 Agent(개발 스펙의 API·아키텍처) 실행 여부 ('ERD 최종 확정' 때만)
    api_fix_attempts: int = 1                            # API 검사 오류(없는 컬럼·API 없는 기능)를 그 기능만 다시 설계하는 횟수 (0이면 max_fix_attempts)
    api_batch_size: int = 6                              # API 설계 요청 1건에 넣는 기능 수
    api_effort: str = ""                                 # API 설계·아키텍처 요청의 생각 수준 (빈 값이면 실행 설정)
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
            func_assumption_limit=float(os.getenv("FUNC_ASSUMPTION_LIMIT", "").strip() or cls.func_assumption_limit),
            func_max_questions=_int("FUNC_MAX_QUESTIONS", cls.func_max_questions),
            attr_batch_size=_int("ATTR_BATCH_SIZE", cls.attr_batch_size),
            max_steps=_int("MAX_STEPS", cls.max_steps),
            max_fix_attempts=_int("MAX_FIX_ATTEMPTS", cls.max_fix_attempts),
            func_fix_attempts=_int("FUNC_FIX_ATTEMPTS", cls.func_fix_attempts),
            model_fix_attempts=_int("MODEL_FIX_ATTEMPTS", cls.model_fix_attempts),
            max_db_attempts=_int("MAX_DB_ATTEMPTS", cls.max_db_attempts),
            data_dir=_dir("DATA_DIR", "data"),
            output_dir=_dir("OUTPUT_DIR", "output"),
            llm_exchange_dir=_dir("LLM_EXCHANGE_DIR", "output/llm_exchange"),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            validation_db_url=_validation_db_url(),
            keep_validation_schema=_bool("KEEP_VALIDATION_SCHEMA", False),
            semantic_review=_bool("SEMANTIC_REVIEW", True),
            semantic_review_mode=_semantic_review_mode(cls.semantic_review_mode),
            semantic_review_attempts=_int("SEMANTIC_REVIEW_ATTEMPTS", cls.semantic_review_attempts),
            semantic_review_source=_bool("SEMANTIC_REVIEW_SOURCE", cls.semantic_review_source),
            requirement_review=_bool("REQUIREMENT_REVIEW", True),
            req_review_batch=_int("REQ_REVIEW_BATCH", cls.req_review_batch),
            req_review_coverage=float(os.getenv("REQ_REVIEW_COVERAGE", "").strip() or cls.req_review_coverage),
            req_review_effort=os.getenv("REQ_REVIEW_EFFORT", cls.req_review_effort).strip().lower(),
            api_design=_bool("API_DESIGN", True),
            api_fix_attempts=_int("API_FIX_ATTEMPTS", cls.api_fix_attempts),
            api_batch_size=_int("API_BATCH_SIZE", cls.api_batch_size),
            api_effort=os.getenv("API_EFFORT", "").strip().lower(),
            log_level=os.getenv("LOG_LEVEL", "").strip().upper() or cls.log_level,
            cors_origins=tuple(o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip())
            or DEFAULT_CORS_ORIGINS,
        )

    def fix_attempts(self, agent: str) -> int:
        """그 Agent의 반복 실패·이슈 자동 수정 한도. 단계별 값(FUNC·MODEL_FIX_ATTEMPTS 등)이 0이면 MAX_FIX_ATTEMPTS."""
        limit = {"function_designer": self.func_fix_attempts, "data_modeler": self.model_fix_attempts,
                 "semantic_reviewer": self.semantic_review_attempts, "api_designer": self.api_fix_attempts}.get(agent, 0)
        return limit or self.max_fix_attempts

    @property
    def semantic_auto(self) -> bool:
        """실행 중 산출물이 바뀔 때마다 의미 검토를 하는지 (final이면 ERD 최종 확정 때 review 명령으로만)."""
        return self.semantic_review and self.semantic_review_mode == "auto"


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
