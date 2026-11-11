"""LLM 어댑터: 응답 잘림을 원인대로 알리고, 생각 수준을 낮춰 다시 시도하며, 실패한 호출 사용량도 센다 (API 불필요)."""
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.agents.llm.client import AnthropicClient, LLMClient, LLMTruncated


class _Out(BaseModel):
    names: list[str]


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def _message(text: str, stop_reason: str, output_tokens: int):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None,
                           content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=output_tokens))


def _client(responses: list):
    """미리 정한 응답을 차례로 돌려주는 Anthropic 클라이언트 흉내. 보낸 요청을 기록한다."""
    requests = []

    def stream(**kwargs):
        requests.append(kwargs)
        return _Stream(responses.pop(0))

    client = AnthropicClient.__new__(AnthropicClient)
    LLMClient.__init__(client, "claude-haiku-5-5")
    client.effort = "high"
    client.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return client, requests


def test_truncated_response_is_retried_with_lower_effort():
    """잘린 JSON(ValidationError)이 아니라 '출력 한도에 닿아 잘림'으로 알리고, effort를 낮춰 다시 시도한다."""
    client, requests = _client([
        _message('{"names": ["a", "b', "max_tokens", 64000),   # 생각에 한도를 다 써서 잘림
        _message('{"names": ["a", "b"]}', "end_turn", 9000),
    ])
    out = client.structured(system="s", user="u", schema=_Out)
    assert out.names == ["a", "b"]
    assert [r["output_config"]["effort"] for r in requests] == ["high", "medium"]
    assert requests[0]["max_tokens"] >= 64000
    assert requests[0]["output_config"]["format"]["type"] == "json_schema"
    assert "output_format" not in requests[0], "SDK가 잘린 JSON을 먼저 파싱하지 않도록 형식은 output_config로 보냄"
    assert client.pop_usage() == {"calls": 2, "input_tokens": 2000, "output_tokens": 73000, "cache_read_tokens": 0,
                                  "cache_write_tokens": 0}, "실패한 호출도 과금 집계"


def test_truncated_twice_raises_truncated_error():
    client, _ = _client([_message('{"names": [', "max_tokens", 64000), _message('{"names": [', "max_tokens", 64000)])
    with pytest.raises(LLMTruncated, match="출력 한도"):
        client.structured(system="s", user="u", schema=_Out)


def test_shared_prefix_is_sent_as_cached_block_and_cache_usage_is_counted():
    """여러 요청에 공통인 앞부분(shared)과 system에 캐시 표시를 달고, 캐시 읽기·쓰기 토큰을 따로 센다."""
    message = _message('{"names": ["a"]}', "end_turn", 50)
    message.usage.cache_read_input_tokens, message.usage.cache_creation_input_tokens = 9000, 0
    client, requests = _client([message])
    client.structured(system="s", user="묶음별 내용", schema=_Out, shared="공통 카탈로그")
    req = requests[0]
    assert req["system"] == [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
    assert req["messages"][0]["content"] == [
        {"type": "text", "text": "공통 카탈로그", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "묶음별 내용"}]
    usage = client.pop_usage()
    assert (usage["input_tokens"], usage["cache_read_tokens"], usage["cache_write_tokens"]) == (1000, 9000, 0)


class _Counting(LLMClient):
    """부른 횟수를 세는 가짜 공급자 (실제 호출 흉내)."""
    provider = "anthropic"

    def __init__(self):
        super().__init__("claude-sonnet-5-5")
        self.calls = 0

    def _call(self, system, user, schema, max_tokens, reduce_effort=False, shared="", effort=""):
        self.calls += 1
        return schema(names=[user]), 1200, 300, 50, 10


def test_record_then_replay_returns_saved_answer_and_tokens_without_calling(tmp_path):
    """record는 실제로 부르고 저장, replay는 같은 요청이면 부르지 않고 저장한 응답·토큰을 그대로 보고한다 (시연 mock)."""
    from app.agents.llm.client import MockClient

    real = _Counting()
    recorder = MockClient(lambda: real, "record", tmp_path, 0, "anthropic", real.model)
    assert recorder.structured(system="s", user="u1", shared="공통", schema=_Out).names == ["u1"]
    assert real.calls == 1

    fresh = _Counting()
    player = MockClient(lambda: fresh, "replay", tmp_path, 0, "anthropic", "claude-sonnet-5-5")
    assert player.structured(system="s", user="u1", shared="공통", schema=_Out).names == ["u1"]
    assert fresh.calls == 0, "녹화한 요청은 실제 호출 없이 답한다"
    assert player.pop_usage() == {"calls": 1, "input_tokens": 1200, "output_tokens": 300,
                                  "cache_read_tokens": 50, "cache_write_tokens": 10}
    assert player.caches_prompts, "묶음 요청 순서가 녹화(Anthropic)와 같아야 키가 맞는다"

    player.structured(system="s", user="새 요청", schema=_Out)
    assert fresh.calls == 1, "mock에 없는 요청은 실제로 부른다 (시연이 멈추지 않게)"
    assert len(list(tmp_path.glob("*.json"))) == 2, "그 응답도 녹화해 둔다"


def test_replay_pins_model_settings_to_recorded(tmp_path, monkeypatch):
    """replay에서는 화면·CLI에서 다른 모델을 골라도 녹화한 모델 설정으로 고정한다 (사용량·금액이 녹화 때와 같게)."""
    from app.agents.llm.client import _mock_model_file
    from app.core import config

    recorded = config.override_settings(llm_mode="record", llm_mock_dir=tmp_path, anthropic_model="claude-sonnet-5-5",
                                        anthropic_effort="medium")
    _mock_model_file(recorded, "anthropic")
    s = config.override_settings(llm_mode="replay", llm_provider="openai", anthropic_model="claude-opus-5-5")
    assert (s.llm_provider, s.anthropic_model, s.anthropic_effort) == ("anthropic", "claude-sonnet-5-5", "medium")
    monkeypatch.setenv("LLM_MODE", "녹화")
    with pytest.raises(ValueError):
        config._llm_mode()
