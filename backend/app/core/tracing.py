"""내용을 수집하지 않는 선택적 Langfuse 추적. 추적 실패는 업무 흐름과 분리한다."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from langfuse import Langfuse

from app.core.config import Settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client(public_key: str, secret_key: str, base_url: str):
    return Langfuse(public_key=public_key, secret_key=secret_key, base_url=base_url, timeout=2)


def get_tracer(settings: Settings):
    if not (
        settings.langfuse_tracing_enabled
        and settings.langfuse_public_key
        and settings.langfuse_secret_key.get_secret_value()
    ):
        return None
    try:
        return _client(
            settings.langfuse_public_key, settings.langfuse_secret_key.get_secret_value(), settings.langfuse_base_url
        )
    except Exception:
        logger.warning("langfuse_init_failed")
        return None


def update_observation(observation, **metadata):
    if observation is not None:
        try:
            observation.update(**metadata)
        except Exception:
            logger.warning("langfuse_update_failed")


@contextmanager
def observation(tracer, name: str, **kwargs) -> Iterator:
    manager, span = None, None
    if tracer is not None:
        try:
            manager = tracer.start_as_current_observation(name=name, **kwargs)
            span = manager.__enter__()
        except Exception:
            manager = None
            logger.warning("langfuse_start_failed")
    try:
        yield span
    except BaseException as exc:
        # SDK에 예외 객체를 넘기지 않는다. 공급자 본문/원문을 자동 기록하지 않도록 한다.
        update_observation(span, level="ERROR", status_message=getattr(exc, "code", "stage_failed"))
        raise
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                logger.warning("langfuse_end_failed")
