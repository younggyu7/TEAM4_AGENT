"""PostgreSQL 실검증 경로 (TEST_PG_URL 이 있을 때만 실행).

  (TEAM4_AGENT에서) docker compose up -d db
  TEST_PG_URL=postgresql://reqagent:reqagent@localhost:55432/reqagent_validation pytest tests/test_pg_validation.py

실제 DB에서만 드러나는 오류(시스템 컬럼명 xmin)를 넣어
DB 검증 실패 → 오류·설계 버전 기록 → 조정 Agent → 데이터 모델 수정 → v2 재검증 통과 경로를 확인한다.
"""
import os

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fake_llm import FakeLLM, _attr
from app.core import config
from app.agents.llm import client as llm_module
from app.agents.flow.graph import build_graph

PG_URL = os.getenv("TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL 미설정")


class XminLLM(FakeLLM):
    def _AttributesOut(self, user):
        out = super()._AttributesOut(user)
        if "반드시 해결할 검증 오류" not in user:
            for ent in out.entities:
                if ent.name == "app_user":
                    ent.attributes.append(_attr("xmin", "잘못된 컬럼"))
        return out


def test_postgres_failure_then_fix(sample_doc, tmp_path):
    config._settings = config.Settings(output_dir=tmp_path / "output", validation_db_url=PG_URL)
    llm_module.set_llm(XminLLM(broken_fk=False))
    graph = build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "pg"}, "recursion_limit": 500}
    graph.invoke({"run_id": "pg", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": True}}, cfg)
    state = graph.get_state(cfg).values

    assert state["design_version"] == "2", "v1 실패 → 수정 → v2"
    assert state["versions"][0]["db_validation"] == "FAILED"
    assert any("xmin" in e for e in state["versions"][0]["db_errors"])
    db_issue = next(i for i in state["issues"] if i["kind"] == "DB_VALIDATION_FAILED")
    assert db_issue["target_id"] == "app_user" and db_issue["status"] == "RESOLVED"
    assert state["db_validation"]["engine"] == "postgresql" and state["db_validation"]["passed"]
    assert (tmp_path / "output" / "sample" / "v1" / "db_validation.md").exists()
    assert (tmp_path / "output" / "sample" / "v2" / "db_validation.md").exists()
