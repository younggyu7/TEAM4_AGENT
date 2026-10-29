"""LLM 어댑터.

LLM 공급자는 미확정이므로 모든 Agent는 `structured(system, user, schema)` 하나만 사용한다.
- schema: pydantic 모델 (기본값 없이 모든 필드를 필수로 두면 OpenAI strict 모드와도 호환된다)
- 테스트에서는 set_llm()으로 가짜 클라이언트를 주입한다.
- manual 공급자: 요청을 파일로 내보내고 사람(또는 대화형 AI)이 쓴 응답 파일을 읽는다. 같은 요청은 같은 키라서
  한 번 작성한 응답은 재생(replay)된다 → API 없이 개발·회귀 테스트·시연 안전장치로 쓴다.
- shared: 여러 요청에 똑같이 들어가는 앞부분(예: 엔티티 카탈로그). Anthropic은 system과 이 부분을 프롬프트 캐시로
  재사용해(캐시 읽기는 입력 단가의 1/10) 묶음 요청의 입력 비용을 줄인다. 다른 공급자는 앞에 붙여 보낸다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.core.config import get_settings

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")
log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """재시도 가치가 있는 응답 오류 (잘림, 파싱 실패, 거절 등). 실패한 호출도 과금되므로 사용량을 함께 담는다."""

    def __init__(self, message: str, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0,
                 cache_write: int = 0):
        super().__init__(message)
        self.input_tokens, self.output_tokens = input_tokens, output_tokens
        self.cache_read, self.cache_write = cache_read, cache_write


class LLMTruncated(LLMError):
    """출력 한도에 닿아 응답이 잘림. 다시 시도할 때는 생각(추론) 수준을 한 단계 낮춰 출력할 자리를 만든다."""


class LLMFatalError(RuntimeError):
    """인증·크레딧·모델 ID 오류처럼 재시도해도 소용없는 오류. 실행을 멈추고 설정 수정 후 resume 한다."""


class LLMPending(LLMFatalError):
    """manual 공급자: 응답 파일이 아직 없어 실행을 멈춘다. 응답 작성 후 resume 한다."""


def _is_fatal(e: Exception) -> bool:
    """재시도해도 소용없는 오류인지 판별한다 (인증 401/403, 모델 없음 404, 크레딧 부족)."""
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    if status in (401, 403, 404):
        return True
    if status == 429 and ("insufficient_quota" in text or "credit" in text):
        return True
    return status == 400 and "credit balance" in text


class LLMClient:
    """LLM 어댑터 공통 부분: 구조화 호출, 응답 오류 1회 재시도, 사용량 집계. 공급자별로 _call만 구현한다."""
    provider = "base"
    caches_prompts = False        # shared 앞부분을 캐시로 재사용하는지 (묶음 요청 순서를 정할 때 쓴다)

    def __init__(self, model: str):
        self.model = model
        self._lock = threading.Lock()
        self._usage = _empty_usage()

    def structured(self, *, system: str, user: str, schema: type[T], max_tokens: int = 16000,
                   purpose: str = "", shared: str = "", effort: str = "") -> T:
        """system·user 프롬프트를 보내 schema(pydantic) 형식의 응답을 받는다. 잘림·파싱 실패는 한 번 재시도한다.

        잘린 경우 다시 시도할 때 생각(추론) 수준을 한 단계 낮춘다 (같은 설정으로 다시 보내면 또 잘리기 쉽다).
        shared는 여러 요청에 똑같이 들어가는 앞부분이다 (user 앞에 온다).
        effort는 이 요청에만 쓸 생각 수준이다 (빈 값이면 실행 설정. 예: 기능 명세 그룹은 FUNC_EFFORT).
        """
        last: Exception | None = None
        retry_options: dict = {}
        call_options = {"shared": shared} if shared else {}
        if effort:
            call_options["effort"] = effort
        for attempt in range(2):
            try:
                result, in_tok, out_tok, *cache = self._call(system, user, schema, max_tokens, **call_options,
                                                              **retry_options)
                self._add_usage(in_tok, out_tok, *cache)
                return result
            except LLMError as e:
                last = e
                if e.input_tokens or e.output_tokens:   # 실패한 호출도 과금된다
                    self._add_usage(e.input_tokens, e.output_tokens, e.cache_read, e.cache_write)
                retry_options = {"reduce_effort": True} if isinstance(e, LLMTruncated) else {}
                log.warning("LLM 응답 오류(%s, 시도 %d): %s%s", purpose or schema.__name__, attempt + 1, e,
                            " → 생각 수준을 낮춰 다시 시도" if retry_options and attempt == 0 else "")
            except Exception as e:
                if _is_fatal(e):
                    raise LLMFatalError(f"{self.provider}/{self.model}: {e}") from e
                raise
        assert last is not None
        raise last

    def _add_usage(self, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> None:
        """호출 한 번의 사용량을 누적한다 (병렬 배치에서 함께 부르므로 잠금).

        input_tokens는 캐시를 쓰지 않은 입력, cache_read·cache_write는 캐시에서 읽은·캐시에 쓴 입력이다.
        """
        with self._lock:
            self._usage["calls"] += 1
            self._usage["input_tokens"] += input_tokens
            self._usage["output_tokens"] += output_tokens
            self._usage["cache_read_tokens"] += cache_read or 0
            self._usage["cache_write_tokens"] += cache_write or 0

    def pop_usage(self) -> dict:
        """지금까지의 사용량을 돌려주고 0으로 초기화한다."""
        with self._lock:
            usage, self._usage = self._usage, _empty_usage()
        return usage

    def _call(self, system: str, user: str, schema: type[T], max_tokens: int,
              reduce_effort: bool = False, shared: str = "", effort: str = "") -> tuple:
        """공급자별 실제 호출. (결과, 입력 토큰, 출력 토큰[, 캐시 읽기, 캐시 쓰기])를 돌려준다.

        reduce_effort: 직전 시도가 잘렸으니 생각(추론) 수준을 한 단계 낮춰 달라는 뜻 (지원하는 공급자만).
        shared: 여러 요청에 공통인 앞부분 (캐시를 지원하지 않으면 user 앞에 붙인다).
        effort: 이 요청의 생각 수준 (빈 값이면 클라이언트 설정).
        출력 한도에 닿으면 LLMTruncated, 그 밖의 응답 문제는 LLMError를 낸다.
        """
        raise NotImplementedError


def _empty_usage() -> dict:
    """사용량 집계 초기값."""
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}


def _join(shared: str, user: str) -> str:
    """캐시를 쓰지 않는 공급자용: 공통 앞부분을 user 앞에 붙인다."""
    return f"{shared}\n\n{user}" if shared else user


# 잘렸을 때 다시 시도하며 낮출 생각(추론) 수준
_LOWER_EFFORT = {"max": "high", "xhigh": "high", "high": "medium", "medium": "low", "low": "low", "minimal": "minimal"}


class OpenAIClient(LLMClient):
    """OpenAI Responses API 어댑터 (responses.parse 구조화 출력)."""
    provider = "openai"

    def __init__(self, model: str, reasoning_effort: str = ""):
        from openai import OpenAI

        super().__init__(model)
        self.client = OpenAI()
        self.reasoning_effort = reasoning_effort

    def _call(self, system, user, schema, max_tokens, reduce_effort=False, shared="", effort=""):
        """OpenAI 호출: responses.parse로 구조화 응답을 받는다. gpt-5·o 계열은 추론 토큰 여유를 더 준다.

        공통 앞부분(shared)을 맨 앞에 두면 OpenAI가 같은 앞부분을 자동으로 캐시한다.
        """
        kwargs = dict(model=self.model, instructions=system, input=_join(shared, user), text_format=schema,
                      max_output_tokens=max_tokens)
        base = effort or self.reasoning_effort
        if base and self.model.startswith(("gpt-5", "o")):
            effort = _LOWER_EFFORT.get(base, base) if reduce_effort else base
            kwargs["reasoning"] = {"effort": effort}
            kwargs["max_output_tokens"] = max_tokens + 16000  # 추론 토큰 여유분
        resp = self.client.responses.parse(**kwargs)
        usage = resp.usage
        tokens = (getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
        if resp.status == "incomplete":
            raise LLMTruncated(f"응답이 잘림: {resp.incomplete_details}", *tokens)
        if resp.output_parsed is None:
            raise LLMError("구조화 응답이 비어 있음 (거절 또는 파싱 실패)", *tokens)
        return resp.output_parsed, *tokens


# effort 파라미터가 없는 이전 세대 모델 (접두어; 날짜가 붙은 ID도 포함)
_NO_EFFORT_MODELS = ("claude-haiku-4", "claude-sonnet-4-5", "claude-sonnet-4-0", "claude-opus-4-1", "claude-opus-4-0",
                     "claude-3")
# Claude 출력 한도 하한. 생각(thinking) 토큰도 이 한도를 함께 쓴다.
# 32,000이던 때 기능 64개의 엔티티 카탈로그가 생각에 한도를 다 써서 잘렸다 (10-21, 고객요구사항5).
# 64,000은 Haiku 4.5의 최대값이고, Haiku 5.5는 128,000까지 받는다 (10-21 확인).
ANTHROPIC_MIN_MAX_TOKENS = 64000
# 서버 측 거절 대체(fallbacks="default")를 받는 모델
_SERVER_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class AnthropicClient(LLMClient):
    """Claude Messages API 어댑터 (스트리밍 + 구조화 출력 + 거절 시 서버 측 대체 모델)."""
    provider = "anthropic"
    caches_prompts = True

    def __init__(self, model: str, effort: str = "high"):
        import anthropic

        super().__init__(model)
        self.client = anthropic.Anthropic()
        self.effort = effort

    def _call(self, system, user, schema, max_tokens, reduce_effort=False, shared="", effort=""):
        """Claude 호출: 스트리밍으로 받아 구조화 응답을 꺼낸다. 거절·파싱 실패는 LLMError, 잘림은 LLMTruncated.

        system과 shared(공통 앞부분)에 캐시 표시를 달아, 같은 앞부분을 쓰는 다음 요청은 캐시에서 읽는다.
        캐시는 일정 길이 이상일 때만 만들어지고, 짧으면 표시가 있어도 그냥 일반 입력으로 처리된다.

        응답 형식(JSON Schema)은 output_config.format으로 직접 보내고 파싱도 여기서 한다. SDK의 output_format을 쓰면
        응답이 잘렸을 때 SDK가 잘린 JSON을 먼저 읽다가 ValidationError를 내서 원인(잘림)과 사용량이 가려진다.
        """
        import anthropic

        output_config: dict = {"format": {"type": "json_schema",
                                          "schema": anthropic.transform_schema(TypeAdapter(schema).json_schema())}}
        # 모델마다 받는 옵션이 다르다: effort가 없는 모델(Haiku 4.5 등)에 보내면 400.
        base = effort or self.effort
        effort = _LOWER_EFFORT.get(base, base) if reduce_effort else base
        if not self.model.startswith(_NO_EFFORT_MODELS):
            output_config["effort"] = effort
        options: dict = {}
        if self.model in _SERVER_FALLBACK_MODELS:
            # 안전 분류기 거절 시 서버가 권장 모델로 재시도
            options.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        # 긴 출력도 HTTP 타임아웃 없이 받도록 스트리밍 + 최종 메시지 수집.
        # 출력 한도는 생각(thinking) 토큰도 함께 쓰므로 넉넉히 둔다 (사용한 만큼만 과금).
        limit = max(max_tokens, ANTHROPIC_MIN_MAX_TOKENS)
        cached = {"type": "ephemeral"}
        content = ([{"type": "text", "text": shared, "cache_control": cached}, {"type": "text", "text": user}]
                   if shared else user)
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=limit,
            system=[{"type": "text", "text": system, "cache_control": cached}],
            messages=[{"role": "user", "content": content}],
            output_config=output_config,
            **options,
        ) as stream:
            message = stream.get_final_message()
        u = message.usage
        tokens = (u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0,
                  getattr(u, "cache_creation_input_tokens", 0) or 0)
        if message.stop_reason == "refusal":
            raise LLMError(f"모델이 응답을 거절함: {message.stop_details}", *tokens)
        if message.stop_reason == "max_tokens":
            raise LLMTruncated(f"출력 한도({limit:,} 토큰, effort={effort})에 닿아 응답이 잘림", *tokens)
        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            return schema.model_validate_json(text), *tokens
        except ValidationError as e:
            raise LLMError(f"구조화 응답 파싱 실패: {str(e).splitlines()[0]}", *tokens) from e


class ManualClient(LLMClient):
    """사람(또는 대화형 AI)이 LLM 역할을 하는 어댑터. 요청을 파일로 쓰고, 작성된 응답 파일을 읽는다."""
    provider = "manual"

    def __init__(self, exchange_dir: Path):
        super().__init__("manual")
        self.dir = exchange_dir

    @staticmethod
    def request_key(system: str, user: str, schema: type[BaseModel]) -> str:
        """요청 식별 키: 스키마명·시스템·사용자 프롬프트의 해시. 같은 요청이면 같은 키라서 저장된 응답이 재생된다."""
        return hashlib.sha256(f"{schema.__name__}\n{system}\n{user}".encode("utf-8")).hexdigest()[:16]

    def structured(self, *, system: str, user: str, schema: type[T], max_tokens: int = 16000,
                   purpose: str = "", shared: str = "", effort: str = "") -> T:
        """응답 파일이 있으면 스키마로 검증해 돌려주고, 없으면 요청 파일을 쓰고 LLMPending으로 실행을 멈춘다."""
        user = _join(shared, user)
        key = self.request_key(system, user, schema)
        response = self.dir / f"{key}.response.json"
        if response.exists():
            try:
                result = schema.model_validate_json(response.read_text(encoding="utf-8"))
            except ValidationError as e:
                raise LLMFatalError(f"응답 형식 오류 {response.name}: {e}") from e
            with self._lock:
                self._usage["calls"] += 1
            return result
        self.dir.mkdir(parents=True, exist_ok=True)
        request = self.dir / f"{key}.request.md"
        request.write_text("\n".join([
            f"# LLM 요청 {key}", "",
            f"- 용도: {purpose or schema.__name__}",
            f"- 응답 스키마: `{schema.__name__}`",
            f"- 응답 파일: `{response.name}` (아래 JSON Schema를 따르는 JSON 객체 하나)", "",
            "## System", "", system, "", "## User", "", user, "",
            "## JSON Schema", "", "```json", json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2), "```", "",
        ]), encoding="utf-8")
        raise LLMPending(f"응답 대기: {request.name}")


def pending_requests(exchange_dir: Path) -> list[Path]:
    """응답 파일이 아직 없는 요청 목록."""
    return sorted(p for p in exchange_dir.glob("*.request.md")
                  if not (exchange_dir / p.name.replace(".request.md", ".response.json")).exists())


_llm: LLMClient | None = None


def get_llm() -> LLMClient:
    """설정에 맞는 LLM 클라이언트를 한 번 만들어 재사용한다."""
    global _llm
    if _llm is None:
        _llm = _create_from_settings()
    return _llm


def set_llm(client: LLMClient | None) -> None:
    """LLM 클라이언트를 바꿔 끼운다 (테스트에서 가짜 LLM 주입)."""
    global _llm
    _llm = client


def resolve_provider() -> str:
    """실제로 쓸 공급자를 정한다 (auto면 키가 있는 쪽, manual은 명시해야 함)."""
    provider = get_settings().llm_provider
    if provider != "auto":
        return provider
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    raise RuntimeError("LLM 키가 없습니다. .env에 ANTHROPIC_API_KEY 또는 OPENAI_API_KEY를 설정하세요.")


def _create_from_settings() -> LLMClient:
    """설정값으로 공급자별 클라이언트를 만든다."""
    s = get_settings()
    provider = resolve_provider()
    if provider == "manual":
        client: LLMClient = ManualClient(s.llm_exchange_dir)
    elif provider == "anthropic":
        client = AnthropicClient(s.anthropic_model, s.anthropic_effort)
    elif provider == "openai":
        client = OpenAIClient(s.openai_model, s.openai_reasoning_effort)
    else:
        raise RuntimeError(f"알 수 없는 LLM_PROVIDER: {provider}")
    log.info("LLM: %s / %s", client.provider, client.model)
    return client


def run_parallel(fn: Callable[[R], T], items: Iterable[R]) -> list[tuple[R, T | None, Exception | None]]:
    """배치 LLM 호출을 병렬 실행. 입력 순서대로 (입력, 결과, 오류)를 돌려준다. 한 배치 실패가 전체를 멈추지 않는다."""
    items = list(items)

    def safe(item):
        try:
            return item, fn(item), None
        except LLMFatalError as e:
            return item, None, e
        except Exception as e:  # noqa: BLE001 — 배치 단위 실패는 이슈로 기록하고 계속 진행
            log.error("배치 처리 실패: %s", e)
            return item, None, e

    workers = max(1, min(get_settings().llm_concurrency, len(items) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(safe, items))
    pending = [err for _, _, err in results if isinstance(err, LLMPending)]
    if pending:
        raise LLMPending(f"응답 대기 {len(pending)}건")
    fatal = next((err for _, _, err in results if isinstance(err, LLMFatalError)), None)
    if fatal:
        raise fatal
    return results
