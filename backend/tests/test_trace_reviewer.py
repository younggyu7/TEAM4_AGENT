"""추적 검토: 기능 명세 단계 버전의 추적표, 이전 버전 대비 변경·영향, 확정 안 된 요구사항 (API 불필요)."""
from fake_llm import FakeLLM
from app import cli
from app.agents.llm import client as llm_module
from app.agents.roles import trace_reviewer
from app.core.config import get_settings
from app.agents.flow.graph import build_graph, open_checkpointer
from app.schemas.data_model import Attribute, DataModel, Entity
from app.schemas.function import FunctionSpec
from app.schemas.requirement import Requirement
from app.agents.flow.state import dump
from app.agents.tools.trace_diff import version_changes


def _base(sample_doc):
    out = get_settings().output_dir
    llm_module.set_llm(FakeLLM(broken_fk=False))
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": False}}, cli._config("base"))
    return out


def _trace(out, version):
    return (out / "sample" / f"v{version}" / "traceability.md").read_text(encoding="utf-8")


def test_state_values_without_status_column_are_reported():
    from app.agents.roles.trace_reviewer import state_gaps, stated_states
    from app.schemas.data_model import Attribute, DataModel, Entity
    from app.schemas.function import ExceptionFlow, FunctionSpec

    assert stated_states("전표 처리 상태는 입력, 승인, 승인해제, 출력 중 하나로 관리한다") == ["입력", "승인", "승인해제", "출력"]
    assert stated_states("(확인 필요 FQ-001) 결재 진행상태는 상신, 결재 중, 승인, 반려 4개 값으로 관리한다") == ["상신", "결재 중", "승인", "반려"]
    assert stated_states("임직원의 재직 상태는 재직자 또는 퇴직자 중 하나로 관리한다.") == ["재직자", "퇴직자"]
    assert stated_states("상태를 확인한 뒤 승인한다") == [] and stated_states("처리 상태는 저장 시 기록한다") == []
    assert stated_states("재직 상태 값은 재직, 퇴직 중 하나다") == ["재직", "퇴직"]

    fn = FunctionSpec(function_id="FN-1", group="G", name="전표 관리", description="", requirement_ids=["R1"],
                      business_rules=["전표 처리 상태는 입력, 승인, 승인해제, 출력 중 하나로 관리한다"],
                      exception_flows=[ExceptionFlow(condition="c", handling="h")])
    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    line = Entity(entity_id="approval_line", function_ids=["FN-1"], attributes=[Attribute(name="id", **pk), Attribute(name="line_status", allowed_values=["진행", "승인", "반려"])])
    voucher = Entity(entity_id="voucher", function_ids=["FN-1"], attributes=[Attribute(name="voucher_id", **pk), Attribute(name="memo")])
    history = Entity(entity_id="approval_history", kind="HISTORY", function_ids=["FN-1"], attributes=[Attribute(name="id", **pk), Attribute(name="action_type", allowed_values=["승인", "반려", "승인해제"])])
    gaps = state_gaps([fn], DataModel(entities=[voucher, line, history], relations=[]))
    assert [(f.function_id, values, tables) for f, values, tables in gaps] == [("FN-1", ["입력", "승인", "승인해제", "출력"], ["voucher", "approval_line", "approval_history"])], \
        "값 일부가 겹치는 다른 상태 컬럼이나 이력의 처리 구분으로는 전표 상태를 담지 못한다"
    voucher.attributes.append(Attribute(name="voucher_status", allowed_values=["입력", "승인", "승인해제", "출력"]))
    assert state_gaps([fn], DataModel(entities=[voucher, line], relations=[])) == []


def test_function_stage_version_trace_is_before_data_design_and_shows_impact(sample_doc):
    out = _base(sample_doc)
    assert "## 4. 이전 버전" not in _trace(out, "1"), "첫 버전은 비교할 이전 버전이 없음"
    llm_module.set_llm(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--feedback", "배정할 때 사유를 남긴다"]) == 0
    trace = _trace(out, "2")
    assert "⚠ 데이터 누락" not in trace and "✅ 기능 연결 (데이터 설계 전)" in trace, "데이터 설계 전 버전은 누락이 아님"
    assert "기능 명세 단계 버전이라 엔티티는 이 버전을 확정한 뒤 데이터 설계" in trace
    assert "## 4. 이전 버전(v1) 대비 변경" in trace and "- 기능: 새 0 · 바뀜 1 · 지움 0" in trace
    assert "다시 설계할 엔티티 1개" in trace and "다시 설계: service_request" in trace, \
        "이 버전에서 데이터 설계를 하면 다시 설계할 엔티티 (v1 모델 기준)"
    assert "| 바뀜 | 업무 규칙 |" in trace

    # 이 기능 명세 버전에서 데이터 설계: 엔티티 변화는 가장 가까운 데이터 모델(v1)과 비교한다
    llm_module.set_llm(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v2"]) == 0
    trace = _trace(out, "2.1")
    assert "## 4. 이전 버전(v2) 대비 변경" in trace and "- 엔티티 (v1 데이터 모델 대비):" in trace
    assert "데이터 설계 영향" not in trace


def _req(rid, status="CONFIRMED"):
    return Requirement(requirement_id=rid, source_key="G", type="FUNCTIONAL", scope="SYSTEM", category="기능",
                       title=f"요구 {rid}", description="설명", priority="MUST", status=status)


def test_unconfirmed_requirements_are_reported_and_shown():
    reqs = [_req("R1"), _req("R2", "ASSUMED"), _req("R3", "OPEN")]
    funcs = [FunctionSpec(function_id=f"FN-G-0{i}", group="G", name=f"기능 {i}", description="", requirement_ids=ids)
             for i, ids in ((1, ["R1", "R2"]), (2, ["R2"]), (3, ["R3"]))]
    update = trace_reviewer.run({"requirements": dump(reqs), "functions": dump(funcs), "issues": [],
                                 "rev": {"requirements": 1, "functions": 1, "model": 0},
                                 "basis": {"functions": 1, "model": -1, "review": [-1, -1]}})
    info = next(i for i in update["issues"] if i["kind"] == "TRACE_FUNC_ON_UNCONFIRMED")
    assert info["severity"] == "INFO" and info["target_agent"] == "none"
    assert "기능 2개" in info["message"] and "FN-G-02(R2)" in info["message"] and "FN-G-01" not in info["message"], \
        "확정된 요구사항도 근거인 기능은 빼고, 확정 안 된 요구사항에만 기댄 기능만"


def test_version_changes_pair_removed_functions_and_separate_text_only_columns():
    reqs = [_req("R1"), _req("R2")]
    old_fn = FunctionSpec(function_id="FN-G-01", group="G", name="요청 등록", description="", requirement_ids=["R1"])
    kept = FunctionSpec(function_id="FN-G-02", group="G", name="요청 배정", description="", requirement_ids=["R2"])
    new_fn = FunctionSpec(function_id="FN-G-03", group="G", name="서비스 요청 등록", description="", requirement_ids=["R1"])
    col = Attribute(name="title", label="제목", data_type="varchar", length=100)
    old_model = DataModel(entities=[Entity(entity_id="request", label="요청", function_ids=["FN-G-01"],
                                           attributes=[col, Attribute(name="memo", label="메모")])])
    new_model = DataModel(entities=[Entity(entity_id="request", label="요청", function_ids=["FN-G-03"], attributes=[
        col.model_copy(update={"length": 200}), Attribute(name="memo", label="비고")])])
    previous = {"version": "1", "requirements": reqs, "functions": [old_fn, kept], "model": old_model,
                "model_version": "1"}
    changes = version_changes([reqs[0], reqs[1].model_copy(update={"status": "ASSUMED"})], [kept, new_fn], new_model,
                              previous)
    assert changes["counts"] == {"requirements": [0, 1, 0], "functions": [1, 0, 1], "entities": [0, 1, 0]}
    req = changes["requirements"][0]
    assert (req["id"], req["fields"], req["functions"]) == ("R2", ["확정 상태"], ["FN-G-02"])
    removed = next(f for f in changes["functions"] if f["change"] == "지움")
    assert removed["replaced_by"] == ["FN-G-03"] and removed["entities"] == ["request"], "같은 요구사항의 새 기능"
    entity = changes["entities"][0]
    assert entity["columns"] == ["~title", "설명만 1개"] and entity["fields"] == ["연결 기능"]
    # 데이터 모델이 없는 버전(기능 명세 단계)은 이전 모델 기준으로 데이터 설계 영향을 미리 보여 준다
    pending = version_changes(reqs, [kept, new_fn], None, previous)["data_design"]
    assert pending == {"model_version": "1", "redesign": [], "cover": ["FN-G-03"], "unlinked": ["request"]}
