"""가짜 LLM으로 흐름도 전체 경로를 검증한다 (API 키 불필요).

- 정상 경로: 문서분석 → 요구사항 → 기능 명세 → 데이터 모델 → (모델 오류 → 수정) → 검토 → 완료 → 제공 → DB 검증
- 재질문 경로: 차단 질문 → 실행 중단(interrupt) → 답변으로 재개 → 관련 기능만 부분 재설계
"""
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fake_llm import FakeLLM
from app.agents.llm import client as llm_module
from app.agents.roles import coordinator, function_designer, requirement_reviewer
from app.core.config import get_settings
from app.agents.flow.graph import build_graph
from app.agents.llm.client import LLMFatalError


def _start(sample_doc, ask_mode, validate_db=False):
    fake = FakeLLM()
    llm_module.set_llm(fake)
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}, "recursion_limit": 500}
    initial = {"run_id": "t1", "source_path": str(sample_doc),
               "options": {"ask_mode": ask_mode, "validate_db": validate_db, "only": [], "max_sources": 0}}
    graph.invoke(initial, config)
    return fake, graph, config


def test_full_flow_defer_with_db_validation(sample_doc):
    fake, graph, config = _start(sample_doc, "defer", validate_db=True)
    state = graph.get_state(config).values

    reqs = state["requirements"]
    assert {r["type"] for r in reqs} == {"FUNCTIONAL", "NON_FUNCTIONAL"}
    assert all(e["verified"] for r in reqs for e in r["evidence"])
    assert state["questions"][0]["status"] == "DEFERRED"

    # 잘못된 FK → 모델 검증 오류 → 데이터 모델 Agent 수정 → 해결
    actions = [(d["action"], d["mode"]) for d in state["decisions"]]
    assert ("data_modeler", "fix") in actions
    assert all(d["result"] for d in state["decisions"]), "판단 기록마다 실행 결과가 남아야 함"
    assert state["decisions"][-1]["result"].startswith("충족 (7/7)")
    model_issues = [i for i in state["issues"] if i["kind"] == "MODEL_FK_TARGET_MISSING"]
    assert model_issues and model_issues[0]["status"] == "RESOLVED"

    assert state["completion"]["satisfied"]
    assert state["design_version"] == "1"
    assert state["db_validation"]["passed"] and state["db_validation"]["engine"] == "sqlite"
    out = get_settings().output_dir / "sample" / "v1"   # 프로젝트 = 문서 파일 이름
    for name in ("requirements.md", "functions.md", "erd.md", "schema.sql", "traceability.md",
                 "review_report.md", "design.json", "db_validation.md"):
        assert (out / name).exists(), name
    assert "REFERENCES app_user" in (out / "schema.sql").read_text(encoding="utf-8")

    # 의미 검토: 지적은 보고만 하는 WARNING, 없는 대상 ID를 쓴 지적은 버림
    semantic = {i["kind"]: i for i in state["issues"] if i["kind"].startswith("SEM_")}
    assert set(semantic) == {"SEM_FUNC_UNMARKED_ASSUMPTION", "SEM_MODEL_INTEGRITY"}
    assert all(i["severity"] == "WARNING" and i["target_agent"] == "none" for i in semantic.values())
    assert semantic["SEM_MODEL_INTEGRITY"]["target_id"] == "service_request"
    assert state["semantic_review"]["dropped"] == 1, "ghost_table 지적은 제외"
    reviewed = next(d for d in state["decisions"] if d["action"] == "semantic_reviewer")
    assert reviewed["scope"] == ["functions", "model"] and "제외" in reviewed["result"]
    report = (out / "review_report.md").read_text(encoding="utf-8")
    assert "## 의미 검토" in report and "제약 누락" in report and "requester_id를 NOT NULL로" in report
    assert "SEM_" not in report.split("## 의미 검토")[0], "의미 검토 지적은 설계 이슈 표에 섞지 않음"


def test_question_interrupt_and_partial_redesign(sample_doc):
    fake, graph, config = _start(sample_doc, "ask")
    snapshot = graph.get_state(config)
    assert snapshot.interrupts, "차단 질문에서 실행이 멈춰야 함"
    question = snapshot.interrupts[0].value["questions"][0]
    assert question["question_id"] == "Q-001"
    designs_before = fake.calls["FunctionDesignOut"]
    assert designs_before == 1, "요구사항 질문은 기능 명세 초안을 만든 뒤 한 번에 묻는다"
    assert not snapshot.values.get("data_model"), "답을 받기 전에는 데이터 설계로 넘어가지 않음"

    graph.invoke(Command(resume={"Q-001": "필요"}), config)
    state = graph.get_state(config).values
    assert not graph.get_state(config).next
    assert state["questions"][0]["status"] == "ANSWERED"
    asked = next(d for d in state["decisions"] if d["action"] == "ask_user")
    assert asked["result"].startswith("답변 1건")
    decided = [r for r in state["requirements"] if r["decisions"]]
    assert decided and "필요" in decided[0]["decisions"][0]
    assert any("(승인 반영)" in f["name"] for f in state["functions"]), "결정이 기능 명세에 반영됨"
    assert state["completion"]["satisfied"]


def test_partial_redesign_routing(sample_doc):
    """요구사항 변경 → 해당 그룹 기능만 재설계 → 변경 기능 기준으로 데이터 모델 보완."""
    fake, graph, config = _start(sample_doc, "defer")
    state = dict(graph.get_state(config).values)
    target = next(r for r in state["requirements"] if r["type"] == "FUNCTIONAL")
    target["decisions"] = ["[CHG-1] 승인을 2단계로 변경"]
    state["rev"] = {**state["rev"], "requirements": state["rev"]["requirements"] + 1}
    state["dirty_requirements"] = [target["requirement_id"]]

    decision, _ = coordinator.decide(state)
    assert decision.action == "requirement_reviewer", "요구사항이 바뀌면 기능 명세 전에 충돌부터 다시 검토"
    state.update(requirement_reviewer.run(state))
    decision, _ = coordinator.decide(state)
    assert (decision.action, decision.mode, decision.scope) == ("function_designer", "partial",
                                                                 [target["requirement_id"]])
    state.update(function_designer.run({**state, "next_action": decision.model_dump()}))
    assert state["dirty_functions"], "바뀐 기능을 데이터 모델 Agent에 넘길 범위로 기록"
    decision, _ = coordinator.decide(state)
    assert (decision.action, decision.mode) == ("data_modeler", "cover")
    assert set(decision.scope) == set(state["dirty_functions"])


class _QuotaError(Exception):
    status_code = 429

    def __str__(self):
        return "insufficient_quota: no credits"


class _BrokeLLM(FakeLLM):
    def _call(self, system, user, schema, max_tokens):
        raise _QuotaError()


def test_fatal_llm_error_stops_and_is_resumable(sample_doc):
    llm_module.set_llm(_BrokeLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "f1"}, "recursion_limit": 500}
    with pytest.raises(LLMFatalError):
        graph.invoke({"run_id": "f1", "source_path": str(sample_doc),
                      "options": {"ask_mode": "defer", "validate_db": False}}, config)
    assert graph.get_state(config).next == ("requirements_analyst",), "실패 지점에서 재개 가능"

    llm_module.set_llm(FakeLLM())
    graph.invoke(None, config)
    assert graph.get_state(config).values["completion"]["satisfied"]


def test_manual_provider_request_response_resume(sample_doc, tmp_path):
    """manual 공급자: 요청 파일 기록 → 멈춤 → 응답 파일 작성 → 재개를 끝까지 반복. 두 번째 실행은 녹화 응답만으로 완료."""
    import re

    from app.agents.llm.client import LLMPending, ManualClient, pending_requests

    exchange = tmp_path / "exchange"
    llm_module.set_llm(ManualClient(exchange))
    responder = FakeLLM()
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "m1"}, "recursion_limit": 500}
    payload = {"run_id": "m1", "source_path": str(sample_doc),
               "options": {"ask_mode": "defer", "validate_db": False}}
    rounds = 0
    while True:
        try:
            graph.invoke(payload, config)
            break
        except LLMPending:
            rounds += 1
            assert rounds < 15
            for req in pending_requests(exchange):
                text = req.read_text(encoding="utf-8")
                schema = re.search(r"응답 스키마: `(\w+)`", text).group(1)
                user = text.split("\n## User\n\n", 1)[1].rsplit("\n\n## JSON Schema", 1)[0]
                out = getattr(responder, f"_{schema}")(user)
                req.with_name(req.name.replace(".request.md", ".response.json")).write_text(
                    out.model_dump_json(), encoding="utf-8")
            payload = None
    assert rounds >= 3, "요구사항 → 기능 → 카탈로그 → 속성 순서로 여러 번 멈춤"
    assert graph.get_state(config).values["completion"]["satisfied"]

    # 같은 문서 재실행: 녹화된 응답만으로 멈춤 없이 완료
    graph2 = build_graph(InMemorySaver())
    config2 = {"configurable": {"thread_id": "m2"}, "recursion_limit": 500}
    graph2.invoke({"run_id": "m2", "source_path": str(sample_doc),
                   "options": {"ask_mode": "defer", "validate_db": False}}, config2)
    assert graph2.get_state(config2).values["completion"]["satisfied"]


class _NoFunctionLLM(FakeLLM):
    """유지운영 RFP처럼 시스템 기능 요구사항 없이 비기능·수행 의무만 나오는 경우."""

    def _ExtractionOut(self, user):
        out = super()._ExtractionOut(user)
        out.requirements = [r for r in out.requirements if r.type == "NON_FUNCTIONAL"]
        out.questions = []
        return out


def test_no_system_function_requirements_is_not_a_failure(sample_doc):
    llm_module.set_llm(_NoFunctionLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "nf"}, "recursion_limit": 500}
    graph.invoke({"run_id": "nf", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": True}}, config)
    state = graph.get_state(config).values
    assert state["functions"] == [] and state["data_model"] is None
    assert state["completion"]["satisfied"], "설계할 기능이 없으면 기능·데이터 조건은 해당 없음으로 통과"
    assert state["design_version"] == "1" and not state.get("db_validation"), "모델이 없으면 DB 검증은 건너뜀"


def test_semantic_review_reruns_only_changed_part(sample_doc):
    """데이터 모델만 바뀌면 추적 검토 뒤 의미 검토도 데이터 모델 부분만 다시 한다."""
    from app.agents.roles import semantic_reviewer
    from app.agents.flow.state import semantic_stale_parts

    fake, graph, config = _start(sample_doc, "defer")
    state = dict(graph.get_state(config).values)
    assert semantic_stale_parts(state) == []
    state["rev"] = {**state["rev"], "model": state["rev"]["model"] + 1}
    state["basis"] = {**state["basis"], "review": [state["rev"]["functions"], state["rev"]["model"]]}
    assert semantic_stale_parts(state) == ["model"]

    decision, _ = coordinator.decide(state)
    assert (decision.action, decision.mode, decision.scope) == ("semantic_reviewer", "partial", ["model"])
    before = fake.calls["FunctionReviewOut"], fake.calls["ModelReviewOut"]
    state.update(semantic_reviewer.run({**state, "next_action": decision.model_dump()}))
    after = fake.calls["FunctionReviewOut"], fake.calls["ModelReviewOut"]
    assert (after[0] - before[0], after[1] - before[1]) == (0, 1)
    assert coordinator.decide(state)[0].action == "check_completion"


def test_semantic_review_can_be_turned_off(sample_doc):
    llm_module.set_llm(FakeLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "off"}, "recursion_limit": 500}
    graph.invoke({"run_id": "off", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": False, "semantic_review": False}}, config)
    state = graph.get_state(config).values
    assert not any(d["action"] == "semantic_reviewer" for d in state["decisions"])
    assert state["completion"]["satisfied"]
    review = next(c for c in state["completion"]["checks"] if c["name"] == "검토 최신화")
    assert "의미 검토 꺼짐" in review["detail"]


class _SemanticFailLLM(FakeLLM):
    def _ModelReviewOut(self, user):
        raise ValueError("검토 응답 오류")


def test_semantic_review_failure_is_reported_as_unverified(sample_doc):
    """의미 검토가 계속 실패하면 시도 한도 후 멈추고, 산출물은 제공하되 '미검증'으로 표시한다."""
    llm_module.set_llm(_SemanticFailLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "sf"}, "recursion_limit": 500}
    graph.invoke({"run_id": "sf", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": False}}, config)
    state = graph.get_state(config).values
    attempts = [d for d in state["decisions"] if d["action"] == "semantic_reviewer"]
    assert len(attempts) == get_settings().max_fix_attempts and "일부 실패" in attempts[0]["result"]
    assert attempts[-1]["scope"] == ["model"], "성공한 부분은 다시 검토하지 않음"
    review = next(c for c in state["completion"]["checks"] if c["name"] == "검토 최신화")
    assert not review["passed"] and "미검증" in review["detail"]
    assert state["design_version"] == "1", "더 시도할 작업이 없으면 현재 상태로 제공"
    report = (get_settings().output_dir / "sample" / "v1" / "review_report.md").read_text(encoding="utf-8")
    assert "검토 실패(미검증): 데이터 모델" in report


class _ModelFailLLM(FakeLLM):
    def _CatalogOut(self, user):
        raise ValueError("카탈로그 응답 오류")


def test_failed_stage_does_not_loop_until_step_limit(sample_doc):
    """데이터 모델 Agent가 계속 실패하면 한도 후 포기하고, 완료 조건 검사를 되풀이하지 않고 현재 상태로 제공한다."""
    llm_module.set_llm(_ModelFailLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "mf"}, "recursion_limit": 500}
    graph.invoke({"run_id": "mf", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": False}}, config)
    state = graph.get_state(config).values
    actions = [d["action"] for d in state["decisions"]]
    assert actions.count("data_modeler") == get_settings().max_fix_attempts
    assert actions.count("check_completion") == 1, "포기한 단계 때문에 완료 조건 검사를 반복하지 않음"
    assert not state["completion"]["forced"] and state["design_version"] == "1"
    assert "데이터 모델 Agent 반복 실패" in state["decisions"][-1]["reason"]
    assert "반복 실패로 더 진행할 수 없음" in state["decisions"][-1]["result"]
    checks = {c["name"]: c for c in state["completion"]["checks"]}
    assert not checks["데이터 모델 무결성"]["passed"]
    assert checks["데이터 모델 무결성"]["detail"].startswith("데이터 모델 없음 (데이터 모델 Agent")
    assert checks["기능→데이터 추적"]["detail"].startswith("데이터 모델 없음")


def test_resume_retry_failed_continues_from_failed_stage(sample_doc):
    """resume --retry-failed: 끝난 실행에서 실패한 데이터 모델 단계부터 다시 진행해 v2를 만든다 (앞 단계는 재사용)."""
    from app import cli
    from app.agents.flow.graph import open_checkpointer

    out_dir = get_settings().output_dir
    fail = _ModelFailLLM()
    llm_module.set_llm(fail)
    with open_checkpointer(out_dir) as saver:
        build_graph(saver).invoke({"run_id": "rf", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": True}}, cli._config("rf"))

    fixed = FakeLLM()
    llm_module.set_llm(fixed)
    assert cli.main(["resume", "rf", "--retry-failed"]) == 0
    with open_checkpointer(out_dir) as saver:
        state = build_graph(saver).get_state(cli._config("rf")).values
    assert state["design_version"] == "2" and state["data_model"], "실패한 단계부터 다시 해서 v2 제공"
    assert fixed.calls["ExtractionOut"] == 0 and fixed.calls["FunctionDesignOut"] == 0, "앞 단계는 다시 하지 않음"
    assert state["completion"]["satisfied"] and state["db_validation"]["passed"]
    assert (out_dir / "sample" / "v2" / "schema.sql").exists()
