"""agents_flow_diagram.png 흐름을 LangGraph StateGraph로 구성한다.

    문서 업로드·프로젝트 생성 ─▶ 조정 Agent ◀──────────────────────────────┐
                                  │                                          │
          ┌───────────────────────┼──────────────────────┐                  │
          ▼                       ▼                      ▼                  │
  선택한 전문 Agent 작업 수행   확인 질문 제시·응답 대기   완료 조건 검사 ──미충족─┤
          │                       │                      │충족               │
          ▼                       ▼                      ▼                  │
  결과 검사·공통 상태 갱신 ─▶   사용자 답변·결정 기록 ─▶  명세서·ERD·DDL·추적표 제공
          (→ 조정 Agent)          (→ 조정 Agent)          │ 사용자가 DB 검증 요청
                                                         ▼
                                              별도 DB에 DDL 적용·검증
                                                 │통과          │실패
                                                 ▼              ▼
                                   DB 구조·검증 보고서 제공   오류와 관련 설계 버전 기록 ─▶ 조정 Agent
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.core.config import get_settings
from app.core.database import psycopg_url
from app.agents.flow import nodes
from app.agents.roles import (coordinator, data_modeler, document_analyst, function_designer, requirement_reviewer,
                              requirements_analyst, semantic_reviewer, trace_reviewer)
from app.agents.llm.client import LLMFatalError
from app.agents.flow.state import AgentState, with_result

log = logging.getLogger(__name__)

SPECIALISTS: dict[str, Callable[[AgentState], dict]] = {
    "document_analyst": document_analyst.run,
    "requirements_analyst": requirements_analyst.run,
    "requirement_reviewer": requirement_reviewer.run,
    "function_designer": function_designer.run,
    "data_modeler": data_modeler.run,
    "trace_reviewer": trace_reviewer.run,
    "semantic_reviewer": semantic_reviewer.run,
}


def _specialist(name: str, fn: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
    """전문 Agent 실패가 전체 실행을 멈추지 않도록 감싼다. 실패 횟수는 조정 Agent가 판단에 사용한다.

    실패는 판단 기록의 결과에 먼저 적어 두고, 결과 검사가 나머지 요약을 덧붙인다.
    """
    def node(state: AgentState) -> dict:
        before = (state.get("failures") or {}).get(name, 0)
        try:
            update = fn(state)
        except LLMFatalError:
            raise  # 인증·크레딧 문제: 실행을 멈추고 체크포인트에서 재개
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] 실패: %s", name, e)
            failures = dict(state.get("failures") or {})
            failures[name] = failures.get(name, 0) + 1
            return {"failures": failures, "decisions": with_result(state, f"실패: {type(e).__name__}")}
        if (update.get("failures") or {}).get(name, before) > before:   # 일부 배치·그룹만 실패
            update["decisions"] = with_result(state, "일부 실패")
        return update
    node.__name__ = name
    return node


def _route_from_coordinator(state: AgentState) -> str:
    """조정 Agent가 고른 행동(next_action.action)으로 다음 노드를 정한다."""
    return (state.get("next_action") or {}).get("action", "check_completion")


def build_graph(checkpointer=None):
    """흐름도의 노드와 분기를 연결해 실행 가능한 그래프를 만든다. checkpointer를 주면 중단·재개할 수 있다."""
    g = StateGraph(AgentState)
    g.add_node("intake", nodes.intake)                         # 문서 업로드·프로젝트 생성
    g.add_node("coordinator", coordinator.run)                 # 조정 Agent
    for name, fn in SPECIALISTS.items():                       # 선택한 전문 Agent 작업 수행
        g.add_node(name, _specialist(name, fn))
    g.add_node("inspect_result", nodes.inspect_result)         # 결과 검사·공통 상태 갱신
    g.add_node("ask_user", nodes.ask_user)                     # 확인 질문 제시·응답 대기
    g.add_node("record_decision", nodes.record_decision)       # 사용자 답변·결정 기록
    g.add_node("check_completion", nodes.check_completion)     # 완료 조건 검사
    g.add_node("deliver", nodes.deliver)                       # 명세서·ERD·DDL·추적표 제공
    g.add_node("db_validate", nodes.db_validate)               # 별도 DB에 DDL 적용·검증
    g.add_node("db_report", nodes.db_report)                   # DB 구조·검증 보고서 제공
    g.add_node("record_db_failure", nodes.record_db_failure)   # 오류와 관련 설계 버전 기록

    g.add_edge(START, "intake")
    g.add_edge("intake", "coordinator")
    g.add_conditional_edges("coordinator", _route_from_coordinator,
                            [*SPECIALISTS, "ask_user", "check_completion"])
    for name in SPECIALISTS:
        g.add_edge(name, "inspect_result")
    g.add_edge("inspect_result", "coordinator")
    g.add_edge("ask_user", "record_decision")
    g.add_edge("record_decision", "coordinator")
    g.add_conditional_edges("check_completion", nodes.route_after_completion,
                            {"met": "deliver", "unmet": "coordinator"})
    g.add_conditional_edges("deliver", nodes.route_after_deliver, {"validate": "db_validate", "end": END})
    g.add_conditional_edges("db_validate", nodes.route_after_db, {"pass": "db_report", "fail": "record_db_failure"})
    g.add_conditional_edges("record_db_failure", nodes.route_after_db_failure,
                            {"retry": "coordinator", "give_up": "db_report"})
    g.add_edge("db_report", END)
    return g.compile(checkpointer=checkpointer)


_PG_READY: set[str] = set()   # 체크포인트 테이블을 이미 준비한 DB (프로세스마다 한 번만 setup)


@contextmanager
def open_checkpointer(output_dir: Path) -> Iterator[BaseCheckpointSaver]:
    """실행 체크포인트 저장소. 질문 대기 중 프로세스가 끝나도 run_id로 재개할 수 있다.

    DATABASE_URL이 있으면 서비스 DB(PostgreSQL)의 LangGraph 체크포인트 테이블, 없으면 output_dir/checkpoints.sqlite.
    """
    url = get_settings().database_url
    if url:
        from langgraph.checkpoint.postgres import PostgresSaver   # DB를 쓸 때만 불러온다

        with PostgresSaver.from_conn_string(psycopg_url(url)) as saver:
            if url not in _PG_READY:
                saver.setup()   # 체크포인트 테이블 만들기·올리기 (LangGraph가 관리, 몇 번 불러도 안전)
                _PG_READY.add(url)
            yield saver
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(output_dir / "checkpoints.sqlite", check_same_thread=False)
    try:
        yield SqliteSaver(conn)
    finally:
        conn.close()
