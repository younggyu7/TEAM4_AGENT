"""업무 동작(상태 전환·자동 처리·값의 출처): 스펙이 나올 때 함께 만들어지고, 없는 값을 버리고, 입력이 같으면 다시 묻지 않는다 (API 불필요)."""
import json

import pytest
from fake_llm import FakeLLM

from app import cli
from app.agents.llm import client as llm_module
from app.agents.roles import behavior
from app.agents.roles.behavior import AutomationOut, BehaviorOut, StateMachineOut, TransitionOut
from app.core.config import get_settings, override_settings
from app.agents.flow.graph import build_graph, open_checkpointer
from app.schemas.data_model import Attribute, DataModel, Entity
from app.schemas.requirement import Question

FINAL = "v1.1"   # 'ERD 최종 확정'으로 만든 최종본


def _model():
    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    return DataModel(entities=[
        Entity(entity_id="app_user", label="사용자", attributes=[Attribute(name="app_user_id", **pk)]),
        Entity(entity_id="voucher", label="전표", attributes=[
            Attribute(name="voucher_id", **pk), Attribute(name="voucher_no", nullable=False, length=30),
            Attribute(name="writer_id", data_type="bigint", references="app_user", nullable=False),
            Attribute(name="approval_status", label="승인 상태", allowed_values=["DRAFT", "APPROVED", "REJECTED"], nullable=False),
            Attribute(name="memo", data_type="jsonb", nullable=False), Attribute(name="created_at", data_type="timestamp", nullable=False)]),
        Entity(entity_id="voucher_history", label="전표 이력", attributes=[Attribute(name="voucher_history_id", **pk)]),
    ], relations=[])


def _op(oid="approveVoucher", method="POST", path="/vouchers/{voucherId}/approve", request=(), response=()):
    return {"method": method, "path": path, "operation_id": oid, "summary": "처리", "actors": [], "request": list(request), "response": list(response),
            "list_response": False, "errors": [], "sources": []}


def _field(name, column=""):
    return {"name": name, "column": column, "type": "string", "required": True, "description": ""}


STORED = {"FN-1": {"function_id": "FN-1", "operations": [_op()], "no_api_reason": ""}}


def test_invalid_values_are_dropped_and_missing_state_stays_undefined():
    out = BehaviorOut(states=[StateMachineOut(column="voucher.approval_status", initial="없는값", transitions=[
        TransitionOut(from_value="DRAFT", to_value="APPROVED", operation_id="approveVoucher", basis="원문"),
        TransitionOut(from_value="DRAFT", to_value="없는값"), TransitionOut(from_value="DRAFT", to_value="DRAFT"),
        TransitionOut(from_value="DRAFT", to_value="REJECTED", operation_id="없는API")])],
        automations=[AutomationOut(name="없는 대상", trigger="일정", when="매일", targets=["없는테이블"], action="x")])
    data = behavior.normalize(out, _model(), STORED)
    state = data["states"][0]
    assert state["initial"] == "DRAFT", "허용값이 아닌 초기 상태는 첫 허용값"
    assert [(t["from"], t["to"], t["operation_id"]) for t in state["transitions"]] == [("DRAFT", "APPROVED", "approveVoucher"), ("DRAFT", "REJECTED", "")]
    assert data["automations"] == [], "없는 테이블만 바꾸는 자동 처리는 버린다"

    empty = behavior.normalize(BehaviorOut(), _model(), STORED)
    assert empty["states"][0]["undefined"] is True, "빠진 상태 컬럼은 추측하지 않고 미정으로 남긴다"


def test_history_table_has_no_state_table():
    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    status = {"allowed_values": ["재직", "퇴직"], "label": "재직 상태"}
    model = DataModel(entities=[
        Entity(entity_id="employee", label="임직원", attributes=[Attribute(name="employee_id", **pk), Attribute(name="employment_status", **status)]),
        Entity(entity_id="employment_status_history", label="재직 상태 이력", attributes=[Attribute(name="id", **pk), Attribute(name="employment_status", **status)]),
        Entity(entity_id="login_log", attributes=[Attribute(name="id", **pk), Attribute(name="login_status", allowed_values=["성공", "실패"])]),
        Entity(entity_id="work_record", label="근무 이력", attributes=[Attribute(name="id", **pk), Attribute(name="record_status", allowed_values=["대기", "완료"])]),
    ], relations=[])
    assert [e.entity_id for e, _ in behavior.status_columns(model)] == ["employee"], "이력·로그는 지난 값을 쌓을 뿐 상태가 움직이지 않는다"


def test_initial_state_after_some_action_is_reported():
    data = {"states": [{"column": "voucher.approval_status", "initial": "승인해제"}, {"column": "leave.status", "initial": "대기"}, {"column": "x.y", "initial": "UNCONFIRMED"}]}
    assert [s["column"] for s in behavior.odd_initials(data)] == ["voucher.approval_status"], "새 행이 '승인해제'로 시작할 수는 없다"
    assert behavior.odd_initials(None) == []
    assert [s["initial"] for s in behavior.odd_initials({"states": [{"initial": "CANCELLED"}, {"initial": "DRAFT"}, {"initial": "REJECTED"}]})] == ["CANCELLED", "REJECTED"], "영문 코드값도 같다"


def test_automation_id_is_kept_by_name_and_new_ones_get_next_number():
    make = lambda *names: BehaviorOut(automations=[AutomationOut(name=n, trigger="상태 변경", when="승인 뒤", targets=["voucher_history"], action="이력 저장") for n in names])   # noqa: E731
    first = behavior.normalize(make("이력 저장", "알림"), _model(), STORED)
    second = behavior.normalize(make("새 처리", "알림"), _model(), STORED, first)
    assert {a["name"]: a["id"] for a in second["automations"]} == {"새 처리": "AUTO-003", "알림": "AUTO-002"}


def test_questions_are_capped_reused_and_keep_recommendation():
    out = BehaviorOut(automations=[AutomationOut(name=f"처리 {i}", trigger="상태 변경", when="승인 뒤", targets=["voucher"], action="변경", basis="확인 필요",
                                                 question=f"질문 {i}", recommended="추천") for i in range(3)])
    data = behavior.normalize(out, _model(), STORED)
    new = behavior.attach_questions(data, [], ask=False, limit=2)
    assert [q.question_id for q in new] == ["BQ-001", "BQ-002"] and new[0].options == ["추천", behavior.NO_AUTOMATION] and new[0].status == "DEFERRED"
    assert [a["question_id"] for a in data["automations"]] == ["BQ-001", "BQ-002", ""], "한도를 넘으면 질문 없이 확인 필요로 남긴다"
    assert data["automations"][2]["basis"] == "확인 필요"
    again = behavior.normalize(out, _model(), STORED)
    more = behavior.attach_questions(again, new, ask=False, limit=2)
    assert [q.question_id for q in more] == ["BQ-003"], "이미 있는 질문은 다시 만들지 않고 남은 것만 낸다"
    assert again["automations"][0]["question_id"] == "BQ-001"


def test_answers_decide_or_exclude_without_redesign():
    q1 = Question(question_id="BQ-001", kind="MISSING", question="q", context="", status="ANSWERED", answer="가입 즉시 만든다", stage="api_designer")
    q2 = Question(question_id="BQ-002", kind="MISSING", question="q", context="", status="ANSWERED", answer=behavior.NO_AUTOMATION, stage="api_designer")
    data = {"states": [], "automations": [{"id": "AUTO-001", "basis": "확인 필요", "question_id": "BQ-001"}, {"id": "AUTO-002", "basis": "확인 필요", "question_id": "BQ-002"},
                                          {"id": "AUTO-003", "basis": "확인 필요", "question_id": ""}]}
    out = behavior.resolved(data, {"BQ-001": q1, "BQ-002": q2})["automations"]
    assert [a["basis"] for a in out] == ["결정", "제외", "확인 필요"] and out[0]["answer"] == "가입 즉시 만든다"


def test_value_sources_are_completed_from_column_shape():
    model = _model()
    op = _op("createVoucher", path="/vouchers", request=[_field("voucherNo", "voucher.voucher_no")], response=[_field("balance")])
    op["sources"] = [{"target": "voucher.memo", "kind": "계산", "value": "합계", "basis": "원문"}, {"target": "없는.컬럼", "kind": "고정값", "value": "x", "basis": "가정"}]
    stored = {"FN-1": {"operations": [op]}}
    behavior.fill_sources(stored, model, ["FN-1"])
    got = {s["target"]: s for s in op["sources"]}
    assert "없는.컬럼" not in got, "없는 대상은 버린다"
    assert got["voucher.memo"]["kind"] == "계산" and got["voucher.memo"]["basis"] == "원문", "LLM이 쓴 것이 먼저"
    assert got["voucher.writer_id"]["kind"] == "현재 사용자" and got["voucher.approval_status"] == {"target": "voucher.approval_status", "kind": "고정값", "value": "DRAFT", "basis": "가정"}
    assert "voucher.created_at" not in got and "voucher.voucher_no" not in got, "요청에 있는 값·시각 컬럼은 대상이 아니다"
    assert got["balance"]["kind"] == "미정", "계산식을 모르면 지어내지 않는다"
    assert "POST /vouchers: balance" in behavior.undefined_sources(stored)["FN-1"][0]


def test_request_is_skipped_when_nothing_moves():
    model = DataModel(entities=[Entity(entity_id="memo", attributes=[Attribute(name="memo_id", primary_key=True, nullable=False)])], relations=[])
    assert behavior.request([], model, {}, {}) == ""


# ------------------------------------------------------------------ 실행 흐름: 고객 문서 → 스펙
class _BehaviorLLM(FakeLLM):
    """첫 API의 테이블에 자동 처리 하나를 내고, 고객 확인 질문도 하나 낸다."""

    def __init__(self):
        super().__init__(broken_fk=False)

    def _FunctionDesignOut(self, user):
        out = super()._FunctionDesignOut(user)
        for f in out.functions:
            f.business_rules = ["승인되면 처리 이력을 자동으로 남긴다"]
        return out

    def _BehaviorOut(self, user):
        table = user.split("## 테이블\n")[1].split("(")[0].split(",")[0].strip()
        line = next(x for x in user.splitlines() if x.startswith("- ") and ": POST" in x)
        oid, fid = line[2:].split(":")[0], line.rsplit("[", 1)[1].rstrip("]")
        return BehaviorOut(automations=[
            AutomationOut(name="등록 후 이력 저장", trigger="API 실행", when="등록 직후", operation_id=oid, targets=[table], action="이력을 저장한다", on_failure="전체를 되돌린다",
                          basis="원문", function_ids=[fid]),
            AutomationOut(name="계정 자동 생성", trigger="API 실행", when="직원 등록 시", targets=[table], action="계정을 만든다", basis="확인 필요", function_ids=[fid],
                          question="직원을 등록하면 계정을 자동으로 만들까요?", recommended="등록 즉시 만든다")])


def _run(sample_doc, fake):
    out = get_settings().output_dir
    llm_module.set_llm(fake)
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc), "options": {"ask_mode": "defer", "validate_db": False}}, cli._config("base"))
    assert cli.main(["review", "sample", "v1"]) == 0   # 업무 동작은 API 설계와 함께 'ERD 최종 확정'에서 만든다
    return out


def test_spec_has_behavior_from_the_first_run(sample_doc):
    fake = _BehaviorLLM()
    out = _run(sample_doc, fake)
    folder = out / "sample" / FINAL
    assert fake.calls["BehaviorOut"] == 1, "문서당 요청 1회"
    design = json.loads((folder / "design.json").read_text(encoding="utf-8"))
    motion = design["api_design"]["behavior"]
    assert [a["id"] for a in motion["automations"]] == ["AUTO-001", "AUTO-002"]
    assert motion["automations"][1]["question_id"] == "BQ-001"
    assert any(q["question_id"] == "BQ-001" and q["stage"] == "api_designer" for q in design["questions"])
    text = (folder / "BEHAVIOR.md").read_text(encoding="utf-8")
    assert "## 1. 상태 전환" in text and "AUTO-001" in text and "확인 필요 BQ-001" in text
    assert "자동 처리" in (folder / "ACCEPTANCE_CRITERIA.md").read_text(encoding="utf-8")
    assert "AUTO-001" in (folder / "TASKS.md").read_text(encoding="utf-8"), "구현할 자동 처리는 작업에 들어간다"
    assert "AUTO-002" not in (folder / "TASKS.md").read_text(encoding="utf-8"), "고객 답을 기다리는 자동 처리는 작업에 넣지 않는다"
    assert "BQ-001" in (folder / "DECISIONS.md").read_text(encoding="utf-8")


def test_rerun_with_same_input_does_not_ask_again(sample_doc):
    fake = _BehaviorLLM()
    _run(sample_doc, fake)
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--entities", "service_request"]) == 0
    assert cli.main(["review", "sample", "v1.2"]) == 0   # 새 ERD를 최종 확정: 이전 최종본의 API 설계·업무 동작을 이어받는다
    assert fake.calls["BehaviorOut"] == 1, "기능·API가 그대로면 다시 묻지 않는다"


def test_acceptance_ids_of_existing_rows_do_not_shift(sample_doc):
    from app.agents.tools import scaffold

    _run(sample_doc, _BehaviorLLM())
    spec = scaffold.load_spec("sample", FINAL)
    fid = spec.behavior["automations"][0]["function_ids"][0]
    rows = spec.acceptance[fid]
    kinds = [r["kind"] for r in rows]
    assert kinds[-2:] == ["자동 처리", "자동 처리 (보류 BQ-001)"], "업무 동작 수용 기준은 맨 뒤에 붙고, 고객 답을 기다리면 보류다"
    assert [r["id"] for r in rows] == [f"AC-{fid}-{n}" for n in range(1, len(rows) + 1)]


def test_failed_behavior_request_keeps_spec_and_is_reported(sample_doc):
    class Broken(_BehaviorLLM):
        def _BehaviorOut(self, user):
            raise llm_module.LLMError("응답 형식 오류")

    override_settings(max_steps=40)
    out = _run(sample_doc, Broken())
    design = json.loads((out / "sample" / FINAL / "design.json").read_text(encoding="utf-8"))
    assert design["api_design"]["functions"], "API 설계는 남는다"
    assert (out / "sample" / FINAL / "BEHAVIOR.md").exists(), "문서는 비어 있어도 만든다"


@pytest.mark.parametrize("kind,value,expected", [("현재 사용자", "", 'input.putIfAbsent("writerId", user.id());'), ("자동 번호", "", "Db.autoNo(30)"), ("고정값", "A-1", '"A-1"'), ("현재 시각", "", "Db.NOW")])
def test_generated_code_follows_value_sources(sample_doc, kind, value, expected):
    from test_develop import _spec

    from app.agents.tools import scaffold

    spec = _spec(sample_doc)
    model = _model()
    spec.model.entities += model.entities
    spec.columns.update({(e.entity_id, a.name): a for e in model.entities for a in e.attributes})
    spec.physical = dict(spec.columns)
    target = "voucher.writer_id" if kind == "현재 사용자" else "voucher.voucher_no"
    op = _op("createVoucher", path="/vouchers", request=[_field("memo", "voucher.memo")], response=[_field("voucherId", "voucher.voucher_id")])
    op["sources"] = [{"target": target, "kind": kind, "value": value, "basis": "가정"}]
    assert expected in scaffold._controller(spec, spec.functions[0], [op])


def test_undecided_value_uses_column_shape_or_is_reported_as_not_implemented(sample_doc):
    from test_develop import _spec

    from app.agents.tools import scaffold

    spec = _spec(sample_doc)
    model = _model()
    voucher = next(e for e in model.entities if e.entity_id == "voucher")
    voucher.attributes += [Attribute(name="tax_amount", data_type="decimal", nullable=False), Attribute(name="paid", data_type="boolean", nullable=False),
                           Attribute(name="vendor_id", data_type="bigint", references="vendor", nullable=False)]
    spec.model.entities += model.entities
    spec.columns.update({(e.entity_id, a.name): a for e in model.entities for a in e.attributes})
    spec.physical = dict(spec.columns)
    op = _op("createVoucher", path="/vouchers", request=[_field("memo", "voucher.memo")], response=[_field("voucherId", "voucher.voucher_id")])
    op["sources"] = [{"target": f"voucher.{c}", "kind": k, "value": "", "basis": "확인 필요"} for c, k in (("tax_amount", "계산"), ("paid", "미정"), ("vendor_id", "미정"))]
    code = scaffold._controller(spec, spec.functions[0], [op])
    assert '"tax_amount", 0' in code and '"paid", false' in code, "모양으로 정할 수 있는 값은 저장되게 한다 (업무 로직이 주면 그 값)"
    assert '"createVoucher", "vendorId")' in code, "정할 수 없는 값은 DB 제약 오류가 아니라 구현 전으로 알린다"


def test_column_required_by_fixed_value_is_reported_as_not_implemented(sample_doc):
    from test_develop import _spec

    from app.schemas.data_model import RequiredWhen
    from app.agents.tools import scaffold

    spec = _spec(sample_doc)
    model = _model()
    voucher = next(e for e in model.entities if e.entity_id == "voucher")
    voucher.attributes += [Attribute(name="sent_at", data_type="timestamp")]
    voucher.required_when = [RequiredWhen(column="sent_at", when_column="approval_status", when_values=["DRAFT"])]
    spec.model.entities += model.entities
    spec.columns.update({(e.entity_id, a.name): a for e in model.entities for a in e.attributes})
    spec.physical = dict(spec.columns)
    op = _op("createVoucher", path="/vouchers", request=[_field("memo", "voucher.memo")], response=[_field("voucherId", "voucher.voucher_id")])
    op["sources"] = [{"target": "voucher.approval_status", "kind": "고정값", "value": "DRAFT", "basis": "가정"}]
    assert '"createVoucher", "sentAt")' in scaffold._controller(spec, spec.functions[0], [op]), "조건부 필수도 DB 제약 오류로 보여 주지 않는다"
    op["sources"][0]["value"] = "APPROVED"
    assert '"sentAt"' not in scaffold._controller(spec, spec.functions[0], [op]).split("db.insert")[1].split(")")[0], "조건에 맞지 않으면 필요 없다"


def _spec_with_voucher(sample_doc):
    from test_develop import _spec

    from app.agents.tools import scaffold

    spec = _spec(sample_doc)
    model = _model()
    voucher = next(e for e in model.entities if e.entity_id == "voucher")
    voucher.attributes += [Attribute(name="approver_id", data_type="bigint", references="app_user"), Attribute(name="approved_at", data_type="timestamp")]
    spec.model.entities += model.entities
    spec.columns.update({(e.entity_id, a.name): a for e in model.entities for a in e.attributes})
    spec.physical = dict(spec.columns)
    return spec, scaffold


def test_status_change_keeps_processor_and_time(sample_doc):
    spec, scaffold = _spec_with_voucher(sample_doc)
    spec.behavior = {"states": [{"column": "voucher.approval_status", "initial": "DRAFT", "transitions": [{"from": "DRAFT", "to": "APPROVED", "operation_id": "approveVoucher", "condition": "", "basis": "원문"}]}], "automations": []}
    op = _op(response=[_field("voucherId", "voucher.voucher_id")])
    op["sources"] = [{"target": "voucher.approver_id", "kind": "현재 사용자", "value": "", "basis": "가정"}, {"target": "voucher.approved_at", "kind": "현재 시각", "value": "", "basis": "가정"},
                     {"target": "voucher.approval_status", "kind": "고정값", "value": "APPROVED", "basis": "가정"}]
    code = scaffold._controller(spec, spec.functions[0], [op])
    assert 'Db.values("approverId", user.id(), "approvedAt", Db.NOW)' in code, "상태만 바꾸지 않고 처리자·처리 시각도 남긴다"
    assert code.count("new Col(\"approvalStatus\"") == 0, "상태 컬럼은 전환이 바꾼다"


def test_one_api_moving_to_several_states_follows_request_value(sample_doc):
    from app.agents.tools.screens import action_of

    spec, scaffold = _spec_with_voucher(sample_doc)
    spec.behavior = {"automations": [], "states": [{"column": "voucher.approval_status", "initial": "DRAFT", "transitions": [
        {"from": "DRAFT", "to": "APPROVED", "operation_id": "changeVoucherStatus", "condition": "", "basis": "원문"},
        {"from": "REJECTED", "to": "APPROVED", "operation_id": "changeVoucherStatus", "condition": "", "basis": "원문"},
        {"from": "APPROVED", "to": "DRAFT", "operation_id": "changeVoucherStatus", "condition": "", "basis": "가정"}]}]}
    op = _op("changeVoucherStatus", method="PATCH", path="/vouchers/{voucherId}/approval-status", request=[_field("approvalStatus", "voucher.approval_status")], response=[_field("voucherId", "voucher.voucher_id")])
    op["sources"] = [{"target": "voucher.approved_at", "kind": "현재 시각", "value": "", "basis": "가정"}]
    code = scaffold._controller(spec, spec.functions[0], [op])
    assert 'Map.of("APPROVED", List.of("DRAFT", "REJECTED"), "DRAFT", List.of("APPROVED")), input.get("approvalStatus")' in code, "첫 전환으로만 보내면 해제를 요청해도 승인된다"
    assert "db.moveTo(" in code and "Db.NOW" in code
    assert "sets" not in action_of(spec, op, "voucher"), "목표 상태는 입력 칸에서 고른다"


def test_plain_update_also_keeps_processor(sample_doc):
    spec, scaffold = _spec_with_voucher(sample_doc)
    op = _op("updateVoucherMemo", method="PATCH", path="/vouchers/{voucherId}/memo", request=[_field("memo", "voucher.memo")], response=[_field("voucherId", "voucher.voucher_id")])
    op["sources"] = [{"target": "voucher.approver_id", "kind": "현재 사용자", "value": "", "basis": "가정"}]
    assert 'input, STAMP_UPDATE_VOUCHER_MEMO, Db.values("approverId", user.id()))' in scaffold._controller(spec, spec.functions[0], [op])


def test_external_caller_needs_api_key_and_stays_off_screen(sample_doc):
    from app.agents.tools.screens import build_screens

    spec, scaffold = _spec_with_voucher(sample_doc)
    fid = spec.functions[0].function_id
    hook = {**_op("receiveVoucherEvent", path="/vouchers/events"), "actors": ["외부 시스템(ERP)"]}
    both = {**_op("syncVoucher", path="/vouchers/{voucherId}/sync"), "actors": ["재무 담당자", "외부 시스템(ERP)"]}
    nobody = {**_op("touchVoucher", path="/vouchers/{voucherId}/touch"), "actors": []}
    code = scaffold._controller(spec, spec.functions[0], [hook, both, nobody])
    assert "CurrentUser.requireSystem();" in code, "역할이 없다고 인증 없이 열리면 안 된다"
    assert 'if (!CurrentUser.isSystem()) {' in code and 'user.require("재무 담당자");' in code, "사람과 외부 시스템이 함께 쓰면 둘 다 확인한다"
    assert code.count("user.requireLogin();") == 1, "주체가 없는 API도 로그인 없이 열지 않는다"
    spec.api[fid] = {"function_id": fid, "no_api_reason": "", "operations": [hook]}
    data = build_screens(spec)
    assert fid not in {s["function_id"] for s in data["screens"]}, "외부 시스템만 부르는 API는 사람이 쓰는 화면이 아니다"
    assert next(a for a in data["autos"] if a["function_id"] == fid)["reason"] == "외부 시스템이 호출하는 API만 있다"
    spec.api[fid]["operations"] = [hook, {**_op("approveVoucher"), "actors": ["재무 담당자"]}]
    assert [a["operation_id"] for s in build_screens(spec)["screens"] if s["function_id"] == fid for a in s["actions"]] == ["approveVoucher"], "사람이 쓰는 API만 버튼이 된다"


def test_generated_code_and_screen_follow_state_table(sample_doc):
    from test_develop import _spec

    from app.agents.tools import scaffold
    from app.agents.tools.screens import action_of

    spec = _spec(sample_doc)
    model = _model()
    spec.model.entities += model.entities
    spec.columns.update({(e.entity_id, a.name): a for e in model.entities for a in e.attributes})
    spec.physical = dict(spec.columns)
    spec.behavior = {"automations": [], "states": [{"column": "voucher.approval_status", "initial": "DRAFT", "basis": "", "question_id": "", "undefined": False,
                                                    "transitions": [{"from": "DRAFT", "to": "APPROVED", "operation_id": "reviewVoucher", "condition": "", "basis": "원문"},
                                                                    {"from": "REJECTED", "to": "APPROVED", "operation_id": "reviewVoucher", "condition": "", "basis": "가정"}]}]}
    op = _op("reviewVoucher", path="/vouchers/{voucherId}/review", response=[_field("voucherId", "voucher.voucher_id")])
    action = action_of(spec, op, "voucher")
    assert action["sets"]["value"] == "APPROVED" and action["sets"]["from"] == ["DRAFT", "REJECTED"], "동사로 추측하지 않고 전환표를 쓴다"
    text = scaffold._controller(spec, spec.functions[0], [op])
    assert 'db.move(T_VOUCHER, "approval_status", List.of("DRAFT", "REJECTED"), "APPROVED"' in text
    spec.behavior["states"][0]["basis"] = "확인 필요"
    assert spec.transition_of("reviewVoucher") is None, "고객 답을 기다리는 전환은 구현하지 않는다"
