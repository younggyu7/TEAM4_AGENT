"""역할 노드가 공유하는 Anthropic 구조화 출력 호출 경계."""

import asyncio
import time

import anthropic

from app.agents.flow.context import Context
from app.agents.flow.errors import failure
from app.agents.prompts.common import SYSTEM
from app.core.tracing import observation, update_observation
from app.schemas.design import ModelCall


# 모델 호출: 역할별 Claude 구조화 출력을 요청하고 시간·토큰·실패를 기록한다.
async def call_model(
    source: str, stage: str, schema, context: Context, role_instructions: str
) -> tuple[str, ModelCall]:
    settings = context.settings
    if not settings.anthropic_api_key.get_secret_value():
        raise failure("model_not_configured", "로컬 backend/.env에 ANTHROPIC_API_KEY를 설정하세요.", stage, 503)
    prompt = {"type": "text", "text": SYSTEM + role_instructions}
    if settings.enable_prompt_caching:
        prompt["cache_control"] = {"type": "ephemeral"}
    started = time.monotonic()
    with observation(context.tracer, stage, as_type="generation", model=settings.anthropic_model) as span:
        try:
            # SDK 자동 재시도를 끄고 전체 호출에 wall-clock 제한을 적용한다.
            async with asyncio.timeout(settings.anthropic_timeout_seconds):
                async with anthropic.AsyncAnthropic(
                    api_key=settings.anthropic_api_key.get_secret_value(),
                    timeout=settings.anthropic_timeout_seconds,
                    max_retries=0,
                ) as client:
                    response = await client.messages.create(
                        model=settings.anthropic_model,
                        max_tokens=settings.anthropic_max_tokens,
                        system=[prompt],
                        messages=[{"role": "user", "content": source}],
                        output_config={"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
                    )
        except (TimeoutError, anthropic.APITimeoutError):
            raise failure(
                "model_timeout", "모델 호출 시간 초과: ANTHROPIC_TIMEOUT_SECONDS를 확인하세요.", stage, 504
            ) from None
        except anthropic.APIStatusError as exc:
            if stage not in {"generate", "review"} and (exc.status_code == 429 or exc.status_code >= 500):
                raise failure(
                    "model_provider_transient", "모델 공급자의 일시 오류입니다. 같은 단계를 한 번 재시도합니다.", stage
                ) from None
            raise failure(
                "model_api_failed", "Anthropic API 호출 실패. 키·권한·사용 한도·연결을 확인하세요.", stage
            ) from None
        except anthropic.APIConnectionError:
            if stage not in {"generate", "review"}:
                raise failure(
                    "model_provider_transient",
                    "모델 공급자에 연결할 수 없습니다. 같은 단계를 한 번 재시도합니다.",
                    stage,
                ) from None
            raise failure(
                "model_api_failed", "Anthropic API 호출 실패. 키·권한·사용 한도·연결을 확인하세요.", stage
            ) from None
        except anthropic.APIError:
            raise failure(
                "model_api_failed", "Anthropic API 호출 실패. 키·권한·사용 한도·연결을 확인하세요.", stage
            ) from None
        usage = response.usage
        call = ModelCall(
            stage=stage,
            model=response.model,
            duration_ms=round((time.monotonic() - started) * 1000),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        )
        update_observation(
            span,
            usage_details={
                "input": call.input_tokens,
                "output": call.output_tokens,
                "cache_creation_input_tokens": call.cache_creation_input_tokens,
                "cache_read_input_tokens": call.cache_read_input_tokens,
            },
            metadata={"duration_ms": call.duration_ms, "stop_reason": response.stop_reason},
        )
        if response.stop_reason == "max_tokens":
            raise failure(
                "model_output_truncated",
                "출력 토큰 한도로 응답이 잘렸습니다. ANTHROPIC_MAX_TOKENS를 늘리거나 입력 범위를 줄이세요.",
                stage,
                usage=call.model_dump(),
            )
        if response.stop_reason != "end_turn":
            raise failure("model_incomplete", "모델이 정상적으로 생성을 완료하지 않았습니다.", stage)
        text = "".join(block.text for block in response.content if block.type == "text")
        return text, call
