"""자동 보완 흐름 (가짜 LLM, API 키 불필요).

- 의미 검토 데이터 지적 → 데이터 모델 Agent가 관련 테이블만 다시 설계 → 고친 테이블만 다시 의미 검토 → API 설계
- 규칙이 정한 상태 값을 담는 컬럼이 없음(TRACE_STATE_COLUMN_MISSING)도 같은 보완에서 고친다
- 기능이 연결되지 않은 요구사항 → 기능 명세 Agent가 빠진 기능만 채움 (gap), 라운드 한도
- 지적 보완은 이 실행에서 Agent가 설계한 테이블만 (사람이 고친 테이블·rerun 범위 밖은 사람이 본다)
"""
import re

from langgraph.checkpoint.memory import InMemorySaver

from fake_llm import FakeLLM
from app.core import config
from app.agents.llm import client as llm_module
from app.agents.roles import coordinator
from app.agents.roles.data_modeler import AttributeOut, AttributesOut, EntityAttributesOut, designed_entities
from app.agents.roles.function_designer import FunctionPatchOut, InputOut, PatchedFunctionOut
from app.agents.roles.semantic_reviewer import ModelFinding, ModelReviewOut
from app.core.config import Settings, get_settings, override_settings
from app.agents.flow.graph import build_graph
from app.schemas.data_model import Attribute, DataModel, Entity
from app.schemas.review import Issue
from app.agents.flow.state import reconcile_issues


def _run(sample_doc, fake, **options):
    llm_module.set_llm(fake)
    graph = build_graph(InMemorySaver())
    run = {"configurable": {"thread_id": "fix"}, "recursion_limit": 500}
    graph.invoke({"run_id": "fix", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": False, **options}}, run)
    return graph.get_state(run).values


def _actions(state):
    return [(d["action"], d.get("mode") or "") for d in state["decisions"]]


class _FixesWhenToldLLM(FakeLLM):
    """의미 검토는 요청자 참조가 NULL 허용이면 지적하고, 컬럼 설계는 지적 글을 받았을 때만 NOT NULL로 고친다.
    상태 값을 정한 규칙이 있으면 상태 컬럼 누락 지적을 받았을 때만 상태 컬럼을 만든다."""

    def __init__(self, state_rule: str = ""):
        super().__init__(broken_fk=False)
        self.state_rule = state_rule
        self.told = []

    def _FunctionDesignOut(self, user):
        out = super()._FunctionDesignOut(user)
        if self.state_rule and out.functions:
            out.functions[0].business_rules = [self.state_rule]
        return out

    def _AttributesOut(self, user):
        out = super()._AttributesOut(user)
        if "의미 검토가 지적한" not in user:
            return out
        self.told.append(user)
        values = re.search(r"상태 값\(([^)]+)\)", user)
        entities = []
        for e in out.entities:
            attrs = list(e.attributes)
            if e.name == "service_request":
                if "requester_id를 NOT NULL로" in user:
                    attrs = [a.model_copy(update={"nullable": False}) if a.name == "requester_id" else a for a in attrs]
                if values:
                    attrs.append(AttributeOut(name="request_status", label="요청 상태", data_type="varchar", length=20,
                                              nullable=False, allowed_values=[v.strip() for v in values.group(1).split(",")]))
            entities.append(EntityAttributesOut(name=e.name, attributes=attrs))
        return AttributesOut(entities=entities)

    def _ModelReviewOut(self, user):
        nullable = "requester_id:bigint→app_user" in user and "requester_id:bigint→app_user NOT NULL" not in user
        return ModelReviewOut(findings=[
            ModelFinding(category="INTEGRITY", target_ids=["service_request"], message="요청자 참조가 NULL 허용",
                         evidence="requester_id:bigint→app_user", suggestion="requester_id를 NOT NULL로",
                         quote="requester_id:bigint→app_user")] if nullable else [])


def test_semantic_fix_tells_column_design_the_finding_and_rechecks_only_fixed_table(sample_doc):
    fake = _FixesWhenToldLLM()
    state = _run(sample_doc, fake)
    actions = _actions(state)
    fix_at = actions.index(("data_modeler", "semantic"))
    integrity = next(i for i in state["issues"] if i["kind"] == "SEM_MODEL_INTEGRITY")
    assert state["decisions"][fix_at]["scope"] == [integrity["issue_id"]]
    assert fake.told and "requester_id를 NOT NULL로" in fake.told[0], "지적 글이 컬럼 설계 요청에도 들어감 (범위만 고르고 지적 없이 다시 설계하지 않음)"
    assert integrity["status"] == "RESOLVED" and integrity["attempts"] == 1, "고친 뒤 다시 검토에서 사라짐"
    recheck = [d for d in state["decisions"][fix_at:] if d["action"] == "semantic_reviewer"]
    assert recheck and "고친 테이블 1개만" in recheck[0]["result"]
    assert all(a != "api_designer" for a, _ in actions), "API 설계는 실행 중이 아니라 'ERD 최종 확정'(review) 때만"
    sr = next(e for e in state["data_model"]["entities"] if e["entity_id"] == "service_request")
    assert next(a for a in sr["attributes"] if a["name"] == "requester_id")["nullable"] is False
    report = (get_settings().output_dir / "sample" / "v1" / "review_report.md").read_text(encoding="utf-8")
    assert "지적을 반영해 다시 설계함" in report and "다시 검토에서 해결된 지적 1건" in report


def test_state_column_gap_is_fixed_with_semantic_findings(sample_doc):
    fake = _FixesWhenToldLLM(state_rule="요청 상태는 접수, 처리 중, 완료 중 하나로 관리한다")
    state = _run(sample_doc, fake)
    gap = next(i for i in state["issues"] if i["kind"] == "TRACE_STATE_COLUMN_MISSING")
    fix = next(d for d in state["decisions"] if d["action"] == "data_modeler" and d["mode"] == "semantic")
    assert gap["issue_id"] in fix["scope"], "상태 컬럼 누락도 같은 보완 요청에 넣는다"
    assert gap["status"] == "RESOLVED"
    sr = next(e for e in state["data_model"]["entities"] if e["entity_id"] == "service_request")
    status = next(a for a in sr["attributes"] if a["name"] == "request_status")
    assert status["allowed_values"] == ["접수", "처리 중", "완료"]


def test_erd_mode_reviews_only_data_model(sample_doc):
    state = _run(sample_doc, _FixesWhenToldLLM(), semantic_review_mode="erd", semantic_review=False)
    reviews = [d for d in state["decisions"] if d["action"] == "semantic_reviewer"]
    assert reviews and all(d["scope"] == ["model"] for d in reviews), "기능 명세는 사람이 확인하므로 데이터 모델만"
    assert not any(i["kind"].startswith("SEM_FUNC_") for i in state["issues"])
    assert ("data_modeler", "semantic") in _actions(state)


def test_erd_mode_skips_review_when_run_stops_at_function_spec(sample_doc):
    state = _run(sample_doc, _FixesWhenToldLLM(), semantic_review_mode="erd", semantic_review=False,
                 stop_after="function_designer")
    assert not any(d["action"] == "semantic_reviewer" for d in state["decisions"])


class _MissesLastRequirementLLM(FakeLLM):
    """기능 명세 그룹 설계에서 마지막 요구사항의 기능을 빠뜨리고, 누락 채우기 요청을 받으면 그것만 만든다."""

    def __init__(self, fills: bool = True):
        super().__init__(broken_fk=False)
        self.fills = fills
        self.patch_prompts = []

    def _FunctionDesignOut(self, user):
        out = super()._FunctionDesignOut(user)
        out.functions = out.functions[:-1]
        return out

    def _FunctionPatchOut(self, user):
        self.patch_prompts.append(user)
        missing = re.findall(r"^- \[([^\]]+)\] ", user.split("아래 요구사항에 연결된 기능이 없다")[1], re.M)
        if not self.fills:
            return FunctionPatchOut(functions=[])
        return FunctionPatchOut(functions=[PatchedFunctionOut(
            name=f"기능 {rid}", description="설명", requirement_ids=[rid], nfr_ids=[], actors=["요청자"], preconditions=[],
            inputs=[InputOut(name="제목", description="")], outputs=[], business_rules=[], normal_flow=["입력", "저장"],
            exception_flows=[], data_objects=["서비스 요청"]) for rid in missing[:1]])


def test_missing_requirement_is_filled_without_redesigning_group(sample_doc):
    fake = _MissesLastRequirementLLM()
    state = _run(sample_doc, fake)
    gap = next(d for d in state["decisions"] if d["mode"] == "gap")
    assert gap["action"] == "function_designer" and len(gap["scope"]) == 1
    assert fake.calls["FunctionDesignOut"] == 1 and fake.calls["FunctionPatchOut"] == 1, "그룹을 다시 설계하지 않고 빠진 기능만 받음"
    linked = {rid for f in state["functions"] for rid in f["requirement_ids"]}
    assert gap["scope"][0] in linked
    issue = next(i for i in state["issues"] if i["kind"] == "REQ_WITHOUT_FUNCTION")
    assert issue["status"] == "RESOLVED" and issue["raised_by"] == "requirement_reviewer"
    first_data = next(n for n, (a, _) in enumerate(_actions(state)) if a == "data_modeler")
    assert _actions(state).index(("function_designer", "gap")) < first_data, "데이터 설계 전에 채운다"


def test_gap_rounds_stop_at_func_fix_attempts(sample_doc):
    override_settings(func_fix_attempts=1)
    state = _run(sample_doc, _MissesLastRequirementLLM(fills=False))
    assert sum(1 for d in state["decisions"] if d["mode"] == "gap") == 1, "FUNC_FIX_ATTEMPTS 라운드만큼만 채우기를 시도"
    check = next(c for c in state["completion"]["checks"] if c["name"] == "요구사항→기능 추적")
    assert not check["passed"] and "기능 미연결 1건" in check["detail"]


def test_fix_only_targets_tables_designed_in_this_run():
    def entity(name, fids=()):
        return Entity(entity_id=name, function_ids=list(fids), attributes=[
            Attribute(name=f"{name}_id", data_type="bigint", primary_key=True, nullable=False)])

    model = DataModel(entities=[entity("order_sheet", ["FN-1"]), entity("member", ["FN-2"])])
    state = {"data_model": model.model_dump(mode="json"), "model_designed": ["order_sheet"]}

    def issue(kind, target):
        return Issue(issue_id="ISS-0001", kind=kind, severity="WARNING", message="-", target_id=target)

    assert coordinator._on_designed(state, issue("SEM_MODEL_GAP", "order_sheet, FN-1"))
    assert not coordinator._on_designed(state, issue("SEM_MODEL_GAP", "member, FN-2")), "이번에 설계하지 않은 테이블"
    assert coordinator._on_designed(state, issue("TRACE_STATE_COLUMN_MISSING", "FN-1")), "기능에 연결된 테이블로 본다"
    assert not coordinator._on_designed(state, issue("TRACE_STATE_COLUMN_MISSING", "FN-2"))
    assert not coordinator._on_designed({**state, "model_designed": []}, issue("SEM_MODEL_GAP", "order_sheet"))


def test_designed_entities_ignore_link_only_and_user_edited_changes():
    before = DataModel(entities=[
        Entity(entity_id="a", function_ids=["FN-1"], attributes=[Attribute(name="a_id", data_type="bigint", primary_key=True)]),
        Entity(entity_id="b", attributes=[Attribute(name="b_id", data_type="bigint", primary_key=True)])])
    after = DataModel(entities=[
        before.entities[0].model_copy(update={"function_ids": ["FN-1", "FN-2"]}),   # 기능 연결만 바뀜
        before.entities[1].model_copy(update={"user_edited": True, "label": "사람이 고침"}),
        Entity(entity_id="c", attributes=[Attribute(name="c_id", data_type="bigint", primary_key=True)])])
    assert designed_entities(before, after) == {"c"}
    assert designed_entities(None, before) == {"a", "b"}


def test_fix_attempts_fall_back_to_max_and_semantic_mode_is_read(monkeypatch):
    s = Settings(max_fix_attempts=3, func_fix_attempts=1)
    assert (s.fix_attempts("function_designer"), s.fix_attempts("data_modeler"), s.fix_attempts("trace_reviewer")) == (1, 3, 3)
    monkeypatch.setenv("SEMANTIC_REVIEW_MODE", "final")
    assert Settings.from_env().semantic_review_mode == "erd", "예전 값 final은 erd"
    monkeypatch.setenv("SEMANTIC_REVIEW", "false")
    env = Settings.from_env()
    assert env.semantic_review_mode == "off" and not env.semantic_auto
    config._settings = None


def test_reconcile_keeps_issues_outside_recheck_scope():
    def sem(no, target):
        return Issue(issue_id=f"ISS-{no:04d}", kind="SEM_MODEL_GAP", severity="WARNING", message="-", target_id=target)

    existing = [sem(1, "order_sheet"), sem(2, "member")]
    out = reconcile_issues(existing, [], lambda k: k.startswith("SEM_MODEL_"), 2, keep=lambda i: i.issue_id != "ISS-0001")
    status = {i.issue_id: i.status for i in out}
    assert status == {"ISS-0001": "RESOLVED", "ISS-0002": "OPEN"}, "다시 검토한 지적만 해결로, 범위 밖 지적은 그대로"
