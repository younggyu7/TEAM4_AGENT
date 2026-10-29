"""데이터 모델 Agent: 속성 설계 실패 처리와 연결 의도(상위 엔티티) 보존 (API 불필요)."""
from langgraph.checkpoint.memory import InMemorySaver

from fake_llm import FakeLLM
from app.agents.llm import client as llm_module
from app.agents.flow.graph import build_graph
from app.schemas.design import Attribute, DataModel, Entity
from app.agents.tools.model_validator import normalize_model, validate_model


def _run(sample_doc, fake, thread="dm"):
    llm_module.set_llm(fake)
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": thread}, "recursion_limit": 500}
    graph.invoke({"run_id": thread, "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": False}}, config)
    return graph.get_state(config).values


class _FlakyBatchLLM(FakeLLM):
    """여러 엔티티를 한 번에 묶은 속성 설계 요청은 실패하고, 하나씩 요청하면 성공한다."""

    def _AttributesOut(self, user):
        if user.count("\n### ") > 1:
            raise ValueError("묶음 응답 오류")
        return super()._AttributesOut(user)


def test_failed_attribute_batch_is_retried_one_by_one(sample_doc):
    state = _run(sample_doc, _FlakyBatchLLM(broken_fk=False))
    entities = {e["entity_id"]: e for e in state["data_model"]["entities"]}
    assert all(e["attributes"] for e in entities.values()), "묶음이 실패해도 하나씩 다시 요청해 속성을 채움"
    assert not [i for i in state["issues"] if i["kind"] == "DATA_ATTR_DESIGN_FAILED" and i["status"] == "OPEN"]
    assert state["completion"]["satisfied"]


class _ServiceRequestFailsLLM(FakeLLM):
    """service_request 속성 설계는 처음엔 계속 실패하고, 검증 오류를 받아 다시 설계할 때만 성공한다."""

    def __init__(self):
        super().__init__(broken_fk=False)
        self.fix_prompts = []

    def _AttributesOut(self, user):
        if "반드시 해결할 검증 오류" in user:
            self.fix_prompts.append(user)
        elif "### service_request " in user:
            raise ValueError("속성 응답 형식 오류")
        return super()._AttributesOut(user)


def test_parents_survive_redesign_and_failure_reason_is_recorded(sample_doc):
    fake = _ServiceRequestFailsLLM()
    state = _run(sample_doc, fake)
    failed = next(i for i in state["issues"] if i["kind"] == "DATA_ATTR_DESIGN_FAILED")
    assert failed["target_id"] == "service_request" and "ValueError: 속성 응답 형식 오류" in failed["message"]
    assert failed["status"] == "RESOLVED", "검증 오류로 다시 설계해 성공하면 해결로 바뀜"

    prompt = next(p for p in fake.fix_prompts if "### service_request " in p)
    assert "상위 엔티티: app_user" in prompt, "속성이 비어도 카탈로그의 상위 엔티티를 그대로 넘김"
    sr = next(e for e in state["data_model"]["entities"] if e["entity_id"] == "service_request")
    assert sr["parents"] == ["app_user"]
    assert any(a["references"] == "app_user" for a in sr["attributes"]), "다시 설계해도 회원 연결이 남음"


def test_parent_without_fk_warns_and_empty_target_is_not_blamed_on_referrer():
    model = DataModel(entities=[
        Entity(entity_id="vehicle", parents=["app_user"], attributes=[
            Attribute(name="vehicle_id", data_type="bigint", primary_key=True),
            Attribute(name="vehicle_number", data_type="varchar", length=20)]),
        Entity(entity_id="app_user", attributes=[]),   # 속성 설계 실패로 비어 있음
        Entity(entity_id="driving_record", attributes=[
            Attribute(name="driving_record_id", data_type="bigint", primary_key=True),
            Attribute(name="app_user_id", data_type="bigint", references="app_user")]),
    ])
    model, _ = normalize_model(model, {})
    issues = {(i.kind, i.target_id): i for i in validate_model(model)}
    assert issues[("MODEL_PARENT_NOT_REFERENCED", "vehicle")].severity == "WARNING"
    assert ("MODEL_NO_ATTRIBUTES", "app_user") in issues
    assert not [k for k in issues if k[1] == "driving_record"], "비어 있는 대상의 문제를 참조하는 쪽 오류로 만들지 않음"


class _PromptRecorder(FakeLLM):
    def __init__(self):
        super().__init__(broken_fk=False)
        self.catalog_prompts = []

    def _CatalogOut(self, user):
        self.catalog_prompts.append(user)
        return super()._CatalogOut(user)


def test_rerun_data_design_makes_sub_versions(sample_doc, capsys):
    """팀 규칙: v1이 마음에 들면 데이터 설계만 다시 → v1.1, v1.2 … (앞 단계는 v1 그대로, 의견 반영)."""
    import json

    from app import cli
    from app.core.config import get_settings
    from app.agents.flow.graph import open_checkpointer
    from app.agents.tools.versions import load_versions

    out = get_settings().output_dir
    llm_module.set_llm(FakeLLM(broken_fk=False))
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": True}}, cli._config("base"))

    fake = _PromptRecorder()
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--full", "--feedback", "차량에 소유 회원을 연결"]) == 0
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--full"]) == 0
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1.2", "--full"]) == 0

    versions = {v["design_version"]: v for v in load_versions(out, "sample")}
    assert set(versions) == {"1", "1.1", "1.2", "1.3"}
    assert (versions["1.1"]["stage"], versions["1.1"]["base"], versions["1.1"]["parent"]) == ("data_modeler", "1", "1")
    assert versions["1.3"]["base"] == "1", "같은 단계를 다시 만든 버전(v1.2)에서 출발하면 형제 번호"
    assert versions["1.1"]["feedback"] == "차량에 소유 회원을 연결" and not versions["1.2"]["feedback"]
    assert all(versions[v]["db_validation"] == "PASSED" for v in ("1.1", "1.2", "1.3"))

    assert fake.calls["ExtractionOut"] == 0 and fake.calls["FunctionDesignOut"] == 0, "앞 단계는 다시 하지 않음"
    assert (fake.calls["FunctionReviewOut"], fake.calls["ModelReviewOut"]) == (0, 3)
    assert "## 사용자 의견 (이번 설계에 반드시 반영)\n차량에 소유 회원을 연결" in fake.catalog_prompts[0]
    assert "사용자 의견" not in fake.catalog_prompts[1]

    v1 = json.loads((out / "sample" / "v1" / "design.json").read_text(encoding="utf-8"))
    v11 = json.loads((out / "sample" / "v1.1" / "design.json").read_text(encoding="utf-8"))
    assert v1["requirements"] == v11["requirements"] and v1["functions"] == v11["functions"]
    report = (out / "sample" / "v1.1" / "review_report.md").read_text(encoding="utf-8")
    assert "다시 만든 단계: **데이터 설계** · 기준 버전: v1" in report and "의견: 차량에 소유 회원을 연결" in report
    assert "| 1 | data_modeler | full |" in report, "판단 기록은 이번 실행 것만"

    assert cli.main(["versions", "sample"]) == 0
    printed = capsys.readouterr().out
    assert "v1.3" in printed and "데이터 설계" in printed and "의견: 차량에 소유 회원을 연결" in printed


# ------------------------------------------------------------------ 입력·토큰
def test_function_line_has_actors_and_is_not_truncated():
    from app.agents.roles.data_modeler import _function_line
    from app.schemas.design import FunctionIO, FunctionSpec

    f = FunctionSpec(function_id="FN-1", group="G", name="정비 이력 등록", description="", requirement_ids=["R1"],
                     actors=["차량 소유자", "시스템(알림 스케줄러)"],
                     inputs=[FunctionIO(name=f"입력{i}") for i in range(12)],
                     business_rules=[f"규칙{i}" for i in range(10)] + ["(가정) 자가정비 모드에서는 정비소 입력을 생략할 수 있다."])
    line = _function_line(f)
    assert "행위자: 차량 소유자, 시스템(알림 스케줄러)" in line
    assert "입력11" in line and "자가정비 모드에서는 정비소 입력을 생략" in line, "규칙·입력을 자르지 않음"


class _Recorder(FakeLLM):
    def __init__(self):
        super().__init__(broken_fk=False)
        self.attr_calls = []

    def _call(self, system, user, schema, max_tokens, shared="", **_):
        if schema.__name__ == "AttributesOut":
            self.attr_calls.append((shared, user))
        return super()._call(system, user, schema, max_tokens, shared=shared)


def test_attribute_batches_share_prefix_and_list_each_function_once(sample_doc):
    import re

    fake = _Recorder()
    _run(sample_doc, fake)
    shared, user = fake.attr_calls[0]
    assert "## 엔티티 카탈로그" in shared and "## 엔티티 카탈로그" not in user, "카탈로그는 캐시할 공통 앞부분으로"
    assert len({s for s, _ in fake.attr_calls}) == 1, "묶음마다 공통 앞부분이 같아야 캐시가 맞음"
    for fid in set(re.findall(r"^- \[(FN-[^\]]+)\]", user, re.M)):
        assert user.count(f"- [{fid}]") == 1, "묶음 안에서 같은 기능은 한 번만"


# ------------------------------------------------------------------ 여러 컬럼 제약
def test_group_constraints_are_enforced_in_ddl():
    import sqlite3

    import pytest

    from app.schemas.design import RequiredWhen
    from app.agents.tools.ddl import render_script, render_statements

    model = DataModel(entities=[
        Entity(entity_id="app_user", attributes=[Attribute(name="app_user_id", data_type="bigint", primary_key=True)]),
        Entity(entity_id="admin_account", attributes=[
            Attribute(name="admin_account_id", data_type="bigint", primary_key=True)]),
        Entity(entity_id="inspection_result", attributes=[
            Attribute(name="inspection_result_id", data_type="bigint", primary_key=True),
            Attribute(name="inspection_id", data_type="bigint", nullable=False),
            Attribute(name="item_id", data_type="bigint", nullable=False),
            Attribute(name="result_status", data_type="varchar", length=20, nullable=False,
                      allowed_values=["NORMAL", "ABNORMAL"]),
            Attribute(name="abnormal_memo", data_type="varchar", length=200),
            Attribute(name="app_user_id", data_type="bigint", references="app_user", nullable=False),
            Attribute(name="admin_account_id", data_type="bigint", references="admin_account")],
            unique_together=[["inspection_id", "item_id"], ["ghost", "item_id"]],
            exactly_one_of=[["app_user_id", "admin_account_id"], ["inspection_id", "item_id"]],
            required_when=[RequiredWhen(column="abnormal_memo", when_column="result_status", when_values=["ABNORMAL"]),
                           RequiredWhen(column="abnormal_memo", when_column="result_status", when_values=["UNKNOWN"])]),
    ])
    model, _ = normalize_model(model, {})
    e = model.entity("inspection_result")
    assert e.unique_together == [["inspection_id", "item_id"]], "없는 컬럼이 든 제약은 버림"
    assert e.exactly_one_of == [["app_user_id", "admin_account_id"]], "외래키가 아닌 묶음은 버림"
    assert len(e.required_when) == 1, "허용값에 없는 조건 값은 버림"
    assert next(a for a in e.attributes if a.name == "app_user_id").nullable, "둘 중 하나만이면 NULL 허용으로"

    sql = render_script(model, "1")
    assert "UNIQUE (inspection_id, item_id)" in sql
    assert "CASE WHEN app_user_id IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN admin_account_id IS NOT NULL" in sql
    assert "CHECK (result_status NOT IN ('ABNORMAL') OR abnormal_memo IS NOT NULL)" in sql

    db = sqlite3.connect(":memory:")
    for st in render_statements(model, "sqlite"):
        db.execute(st.sql)
    db.execute("INSERT INTO app_user VALUES (1, 'now', 'now')")
    db.execute("INSERT INTO admin_account VALUES (1, 'now', 'now')")
    row = ("INSERT INTO inspection_result (inspection_result_id, inspection_id, item_id, result_status, abnormal_memo, "
           "app_user_id, admin_account_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'now', 'now')")
    db.execute(row, (1, 1, 1, "NORMAL", None, 1, None))
    for bad in [(2, 1, 1, "NORMAL", None, 1, None),        # 같은 점검·항목 중복
                (3, 1, 2, "NORMAL", None, 1, 1),           # 회원·관리자 둘 다
                (4, 1, 3, "NORMAL", None, None, None),     # 둘 다 없음
                (5, 1, 4, "ABNORMAL", None, 1, None)]:     # 이상인데 메모 없음
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(row, bad)


# ------------------------------------------------------------------ 데이터 설계 질문
from app.agents.roles.data_modeler import DesignQuestionOut  # noqa: E402

CATEGORY_Q = DesignQuestionOut(kind="CONFLICT", question="지출 분류와 영수증 분류가 다릅니다. 어떻게 할까요?",
                               context="지출: 유류비·통행료·정비비·보험료 / 영수증: 주유·정비·주차·통행료·세차·기타",
                               options=["영수증 분류 6종으로 통일", "지출 분류 4종으로 통일", "두 분류를 따로 두고 연결"],
                               recommended=1, function_ids=[], entity_names=["service_request"])


class _AskingLLM(FakeLLM):
    """카탈로그에서 분류 질문을 한 번 낸다. '이미 다룬 질문'이 주어지면 다시 묻지 않는다."""

    def __init__(self, extra=0):
        super().__init__(broken_fk=False)
        self.catalog_prompts, self.extra = [], extra

    def _CatalogOut(self, user):
        self.catalog_prompts.append(user)
        return super()._CatalogOut(user)

    def _design_questions(self, user):
        if "이미 다룬 질문" in user:
            return []
        more = [CATEGORY_Q.model_copy(update={"question": f"추가 질문 {i}"}) for i in range(self.extra)]
        return [CATEGORY_Q, *more]


def _ask_flow(sample_doc, fake, data_answer):
    from langgraph.types import Command

    llm_module.set_llm(fake)
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "dq"}, "recursion_limit": 500}
    graph.invoke({"run_id": "dq", "source_path": str(sample_doc),
                  "options": {"ask_mode": "ask", "validate_db": True}}, config)
    asked = []
    while (snap := graph.get_state(config)).interrupts:
        questions = snap.interrupts[0].value["questions"]
        asked += questions
        answers = {q["question_id"]: (data_answer if q["stage"] == "data_modeler" else "필요") for q in questions}
        graph.invoke(Command(resume=answers), config)
    return graph.get_state(config).values, asked


def test_data_question_is_designed_with_recommendation_in_defer_mode(sample_doc):
    fake = _AskingLLM()
    state = _run(sample_doc, fake)
    dq = next(q for q in state["questions"] if q["stage"] == "data_modeler")
    assert (dq["question_id"], dq["status"], dq["recommended"]) == ("DQ-001", "DEFERRED", "영수증 분류 6종으로 통일")
    assert fake.calls["CatalogOut"] == 1 and state["completion"]["satisfied"], "멈추지 않고 추천으로 설계"


def test_recommended_answer_does_not_redesign(sample_doc):
    fake = _AskingLLM()
    state, asked = _ask_flow(sample_doc, fake, "영수증 분류 6종으로 통일")
    dq = next(q for q in asked if q["stage"] == "data_modeler")
    assert dq["recommended"] == "영수증 분류 6종으로 통일" and len(dq["options"]) == 3
    assert fake.calls["CatalogOut"] == 1, "추천과 같은 답이면 이미 만든 설계를 그대로 씀"
    assert state["completion"]["satisfied"]


def test_other_answer_redesigns_once_with_decision(sample_doc):
    fake = _AskingLLM()
    state, asked = _ask_flow(sample_doc, fake, "지출 분류 4종으로 통일")
    assert fake.calls["CatalogOut"] == 2, "추천과 다른 답이면 데이터 모델을 한 번 다시 설계"
    assert "→ 결정: 지출 분류 4종으로 통일" in fake.catalog_prompts[1]
    assert sum(q["stage"] == "data_modeler" for q in asked) == 1, "같은 질문을 다시 묻지 않음"
    assert not state.get("model_redesign") and state["completion"]["satisfied"]


def test_at_most_three_data_questions_are_asked(sample_doc):
    state = _run(sample_doc, _AskingLLM(extra=4))
    dqs = [q for q in state["questions"] if q["stage"] == "data_modeler"]
    assert len(dqs) == 5 and sum(q["blocking"] for q in dqs) == 3, "4번째부터는 묻지 않고 추천으로 진행"


# ------------------------------------------------------------------ 수정본 프롬프트와 맞춘 검사·보고
def test_hmac_verifier_columns_are_not_plaintext_secrets():
    from app.agents.tools.model_validator import is_plaintext_secret

    def secret(name, label):
        return is_plaintext_secret(Attribute(name=name, label=label, data_type="varchar", length=128))

    assert not secret("verification_code_hmac", "인증번호 검증값"), "비밀키 기반 검증값(HMAC)은 평문이 아님"
    assert not secret("otp_verifier", "OTP 검증값")
    assert secret("verification_code", "인증번호") and secret("password", "비밀번호")


def test_design_notes_collect_tags_for_reviewers():
    from app.agents.tools.exporter import _design_notes_md

    model = DataModel(entities=[Entity(entity_id="vehicle", description="(카탈로그 수정 필요) 소유 회원 참조 없음", attributes=[
        Attribute(name="vehicle_id", data_type="bigint", primary_key=True, description="대리키"),
        Attribute(name="tonnage", data_type="decimal", description="톤수 (확인 필요) 단위 미정"),
        Attribute(name="memo", data_type="varchar", length=200, description="(가정) 200자")])])
    text = "\n".join(_design_notes_md(model))
    assert "| vehicle | (카탈로그 수정 필요) 소유 회원 참조 없음 |" in text
    assert "| vehicle.tonnage | 톤수 (확인 필요) 단위 미정 |" in text
    assert "판단 1건" in text and "vehicle.memo" not in text, "(가정)은 개수만"


# ------------------------------------------------------------------ 출력 줄이기
def test_code_adds_primary_key_and_audit_columns_but_not_to_empty_entities():
    model = DataModel(entities=[
        Entity(entity_id="vehicle", label="차량", attributes=[Attribute(name="vehicle_number", data_type="varchar", length=20)]),
        Entity(entity_id="driver", label="운전자", attributes=[
            Attribute(name="driver_id", data_type="integer", nullable=True),     # 모델이 기본키 이름을 낸 경우
            Attribute(name="driver_name", data_type="varchar", length=50)]),
        Entity(entity_id="receipt", label="영수증", attributes=[]),              # 속성 설계 실패
    ])
    model, _ = normalize_model(model, {})
    vehicle, driver, receipt = (model.entity(n) for n in ("vehicle", "driver", "receipt"))
    assert [a.name for a in vehicle.attributes] == ["vehicle_id", "vehicle_number", "created_at", "updated_at"]
    assert vehicle.pk()[0].data_type == "bigint" and vehicle.pk()[0].label == "차량 ID"
    assert [a.name for a in driver.pk()] == ["driver_id"] and driver.pk()[0].data_type == "bigint"
    assert sum(a.name == "driver_id" for a in driver.attributes) == 1
    assert receipt.attributes == [], "속성 설계에 실패한 엔티티는 비워 둬서 검증 오류로 다시 설계"


def test_attribute_response_needs_only_name_label_type_nullable():
    from app.agents.roles.data_modeler import AttributeOut, EntityAttributesOut

    a = AttributeOut(name="memo", label="메모", data_type="text", nullable=True)
    assert (a.unique, a.references, a.allowed_values, a.length, a.description) == (False, "", [], 0, "")
    e = EntityAttributesOut(name="x", attributes=[a])
    assert (e.unique_together, e.exactly_one_of, e.required_when) == ([], [], [])


def test_batch_reply_with_slightly_different_names_is_not_requested_again():
    from app.agents.roles.data_modeler import AttributeOut, EntityAttributesOut, EntityPlanOut, _match

    def plan(n):
        return EntityPlanOut(name=n, label=n, description="", kind="MASTER", function_ids=[], parents=[])

    col = [AttributeOut(name="c", label="c", data_type="text", nullable=True)]
    batch = [plan("push_message"), plan("chat_room"), plan("daily_inspection")]
    got = _match(batch, [EntityAttributesOut(name="Push Message", attributes=col),       # 대소문자·공백만 다름
                         EntityAttributesOut(name="채팅방", attributes=col),              # 한글명으로 돌려줌
                         EntityAttributesOut(name="일상점검", attributes=col)])
    assert set(got) == {"push_message", "chat_room", "daily_inspection"}
    assert _match(batch, [EntityAttributesOut(name="push_message", attributes=col)]).keys() == {"push_message"}


def test_cover_matches_entity_names_written_differently():
    """연결 보완 응답이 기존 엔티티를 'ServiceRequest'처럼 다르게 적어도 기존 엔티티에 연결하고 새로 만들지 않는다."""
    from app.agents.roles.data_modeler import CoverageOut, EntityPlanOut, MappingOut, _cover
    from app.schemas.design import FunctionSpec

    class CoverLLM(FakeLLM):
        def _CoverageOut(self, user):
            return CoverageOut(mappings=[MappingOut(function_id="FN-2", entity_names=["ServiceRequest"])],
                               new_entities=[
                                   EntityPlanOut(name="ServiceRequest", label="서비스 요청", description="", kind="TRANSACTION",
                                                 function_ids=["FN-2"], parents=[]),
                                   EntityPlanOut(name="Approval Line", label="결재선", description="", kind="MASTER",
                                                 function_ids=["FN-2", "FN-404"], parents=["service_request"])])

    fake = CoverLLM(broken_fk=False)
    model = DataModel(entities=[Entity(entity_id="service_request", label="서비스 요청", kind="TRANSACTION",
                                       function_ids=["FN-1"], attributes=[Attribute(name="title", label="제목")])])
    functions = [FunctionSpec(function_id=f, group="SFR-001", name=f, description="", requirement_ids=["SFR-001"],
                              data_objects=["요청"]) for f in ("FN-1", "FN-2")]
    model, failed = _cover(fake, model, functions, [], {"FN-2"})
    by_name = {e.entity_id: e for e in model.entities}
    assert set(by_name) == {"service_request", "approval_line"} and not failed
    assert by_name["service_request"].function_ids == ["FN-1", "FN-2"]
    assert by_name["approval_line"].function_ids == ["FN-2"], "없는 기능 ID는 버림"
    assert fake.calls["AttributesOut"] == 1, "기존 엔티티를 다시 설계하지 않고 새 엔티티만 설계"


class _DropsEntityLLM(FakeLLM):
    """묶음 요청에서는 app_user를 빠뜨리고, 하나씩 요청하면 낸다."""

    def _AttributesOut(self, user):
        out = super()._AttributesOut(user)
        if user.count("\n### ") > 1 or user.startswith("## 속성을 설계할 엔티티\n### service_request"):
            out.entities = [e for e in out.entities if e.name != "app_user"]
        return out


def test_retry_is_recorded_with_reason(sample_doc):
    state = _run(sample_doc, _DropsEntityLLM(broken_fk=False))
    retried = next(i for i in state["issues"] if i["kind"] == "DATA_ATTR_RETRIED")
    assert "app_user" in retried["message"] and "돌려받은 이름: service_request" in retried["message"]
    assert all(e["attributes"] for e in state["data_model"]["entities"])


def test_rerun_keeps_model_in_run_settings(sample_doc):
    """rerun --model로 고른 모델이 새 실행 설정에 저장되어, 이후 resume도 같은 모델로 이어 간다."""
    from app import cli
    from app.core.config import get_settings
    from app.agents.flow.graph import open_checkpointer
    from app.agents.tools.versions import load_versions

    out = get_settings().output_dir
    llm_module.set_llm(FakeLLM(broken_fk=False))
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": False}}, cli._config("base"))
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--full", "--model", "claude-sonnet-5-5"]) == 0
    run_id = next(v for v in load_versions(out, "sample") if v["design_version"] == "1.1")["run_id"]
    with open_checkpointer(out) as saver:
        settings = build_graph(saver).get_state(cli._config(run_id)).values["options"]["settings"]
    assert "claude-sonnet-5-5" in (settings.get("anthropic_model"), settings.get("openai_model"))


# ------------------------------------------------------------------ 부분 재설계·사용자 수정
def _base(sample_doc, validate_db=False):
    """기준 실행(v1)을 만들고 출력 폴더를 돌려준다."""
    from app import cli
    from app.core.config import get_settings
    from app.agents.flow.graph import open_checkpointer

    out = get_settings().output_dir
    llm_module.set_llm(FakeLLM(broken_fk=False))
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": validate_db}}, cli._config("base"))
    return out


def _entities(out, version):
    """버전 폴더의 data_model.json 엔티티 (이름 → dict)."""
    import json

    data = json.loads((out / "sample" / f"v{version}" / "data_model.json").read_text(encoding="utf-8"))
    return {e["entity_id"]: e for e in data["entities"]}


class _AttributePrompts(FakeLLM):
    def __init__(self):
        super().__init__(broken_fk=False)
        self.attribute_prompts = []

    def _AttributesOut(self, user):
        self.attribute_prompts.append(user)
        return super()._AttributesOut(user)


def test_rerun_without_scope_or_with_unknown_entity_stops(sample_doc, capsys):
    from app import cli

    _base(sample_doc)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1"]) == 2, "실수로 전체 비용을 쓰지 않게 범위를 요구"
    assert "다시 설계할 범위가 없습니다" in capsys.readouterr().out
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--entities", "ghost"]) == 2
    assert "데이터 모델에 없는 엔티티: ghost" in capsys.readouterr().out


def test_rerun_entities_redesigns_only_named_entity(sample_doc):
    from app import cli
    from app.agents.tools.versions import load_versions

    out = _base(sample_doc, validate_db=True)
    fake = _AttributePrompts()
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--entities", "service_request"]) == 0
    assert (fake.calls["CatalogOut"], fake.calls["RedesignScopeOut"], fake.calls["AttributesOut"]) == (0, 0, 1), \
        "카탈로그 없이 지정 엔티티 하나만 속성 설계"
    assert "현재 속성: title:varchar, requester_id:bigint→app_user" in fake.attribute_prompts[0]
    assert _entities(out, "1.1")["app_user"] == _entities(out, "1")["app_user"], "지정하지 않은 엔티티는 그대로"
    report = (out / "sample" / "v1.1" / "review_report.md").read_text(encoding="utf-8")
    assert "다시 설계한 범위: 엔티티 1개 (service_request)" in report
    record = next(v for v in load_versions(out, "sample") if v["design_version"] == "1.1")
    assert record["rework"]["redesigned"] == ["service_request"] and record["db_validation"] == "PASSED"


def test_feedback_redesigns_only_related_entities(sample_doc):
    from app import cli

    out = _base(sample_doc)
    fake = _AttributePrompts()
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--feedback", "요청에 우선순위를 추가"]) == 0
    assert (fake.calls["RedesignScopeOut"], fake.calls["CatalogOut"], fake.calls["AttributesOut"]) == (1, 0, 1), \
        "작은 범위 요청 하나로 관련 엔티티를 고르고 그것만 다시 설계"
    prompt = fake.attribute_prompts[0]
    assert "## 사용자 의견 (이번 설계에 반드시 반영)\n요청에 우선순위를 추가" in prompt and "## 다시 설계 지시" in prompt
    assert _entities(out, "1.1")["app_user"] == _entities(out, "1")["app_user"]


def test_user_edits_are_kept_locked_and_released_only_on_request(sample_doc, tmp_path):
    import json

    from app import cli

    out = _base(sample_doc)
    data = json.loads((out / "sample" / "v1" / "data_model.json").read_text(encoding="utf-8"))
    user = next(e for e in data["entities"] if e["entity_id"] == "app_user")
    user["attributes"].append({"name": "phone_number", "label": "전화번호", "data_type": "varchar", "length": 20})
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    fake = FakeLLM(broken_fk=False)
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--data-model", str(edited)]) == 0
    assert fake.calls["CatalogOut"] == 0 and fake.calls["AttributesOut"] == 0, "수정만 있으면 LLM 없이 정리·검증"
    v11 = _entities(out, "1.1")
    assert v11["app_user"]["user_edited"] and not v11["service_request"]["user_edited"]
    assert "phone_number" in [a["name"] for a in v11["app_user"]["attributes"]]
    assert "### app_user (사용자) — MASTER · 사용자 수정 (고정)" in (out / "sample" / "v1.1" / "erd.md").read_text(
        encoding="utf-8")
    assert "사용자 수정: 고침 1개 (app_user)" in (out / "sample" / "v1.1" / "review_report.md").read_text(encoding="utf-8")

    # 전체를 다시 설계해도 고친 엔티티는 그대로 (카탈로그에는 확정 엔티티로 넘긴다)
    recorder = _PromptRecorder()
    llm_module.set_llm(recorder)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1.1", "--full"]) == 0
    assert "## 사용자가 확정한 엔티티" in recorder.catalog_prompts[0] and "phone_number" in recorder.catalog_prompts[0]
    assert recorder.calls["AttributesOut"] == 1, "고친 엔티티는 속성 설계를 다시 요청하지 않음"
    assert _entities(out, "1.2")["app_user"] == v11["app_user"]

    # 직접 지정하면 다시 설계하고 고정을 푼다
    llm_module.set_llm(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1.2", "--entities", "app_user"]) == 0
    v13 = _entities(out, "1.3")["app_user"]
    assert not v13["user_edited"] and "phone_number" not in [a["name"] for a in v13["attributes"]]


def test_renamed_and_deleted_entities_update_references():
    from app.agents.tools.model_edits import apply_user_edits

    user = Entity(entity_id="app_user", attributes=[
        Attribute(name="app_user_id", data_type="bigint", primary_key=True),
        Attribute(name="user_name"), Attribute(name="email")])
    request = Entity(entity_id="service_request", parents=["app_user"], attributes=[
        Attribute(name="service_request_id", data_type="bigint", primary_key=True), Attribute(name="title"),
        Attribute(name="requester_id", data_type="bigint", references="app_user")])
    original = DataModel(entities=[user, request])

    renamed, summary = apply_user_edits(original, DataModel(entities=[user.model_copy(update={"entity_id": "member"}),
                                                                      request]))
    req = renamed.entity("service_request")
    assert summary.renamed == {"app_user": "member"} and not summary.removed and not summary.added
    assert req.attributes[2].references == "member" and req.parents == ["member"] and not req.user_edited
    assert renamed.entity("member").user_edited

    deleted, summary = apply_user_edits(original, DataModel(entities=[request]))
    req = deleted.entity("service_request")
    assert summary.removed == ["app_user"] and summary.dropped_columns == ["service_request.requester_id"]
    assert [a.name for a in req.attributes] == ["service_request_id", "title"] and req.parents == []

    same, summary = apply_user_edits(original, original.model_copy(deep=True))
    assert not summary.has_edits() and not any(e.user_edited for e in same.entities)


def test_errors_on_user_edited_entity_are_reported_not_auto_fixed():
    model = DataModel(entities=[Entity(entity_id="app_user", user_edited=True, attributes=[
        Attribute(name="app_user_id", data_type="bigint", primary_key=True),
        Attribute(name="password", label="비밀번호", data_type="varchar", length=100)])])
    issue = next(i for i in validate_model(model) if i.kind == "MODEL_PLAINTEXT_SECRET")
    assert issue.severity == "ERROR" and issue.target_agent == "none" and "직접 확인" in issue.message


# ------------------------------------------------------------------ 값 조건·묶음 누락 방지
def test_value_checks_are_normalized_and_enforced_in_ddl():
    import sqlite3

    import pytest

    from app.schemas.design import CheckRule
    from app.agents.tools.ddl import render_script, render_statements

    model = DataModel(entities=[Entity(entity_id="rest_record", attributes=[
        Attribute(name="rest_record_id", data_type="bigint", primary_key=True),
        Attribute(name="rest_minutes", data_type="integer", nullable=False),
        Attribute(name="amount", data_type="decimal"),
        Attribute(name="start_at", data_type="timestamp", nullable=False),
        Attribute(name="end_at", data_type="timestamp"),
        Attribute(name="requester_id", data_type="bigint"),
        Attribute(name="approver_id", data_type="bigint"),
        Attribute(name="memo", data_type="varchar", length=100)],
        checks=[CheckRule(column="rest_minutes", op=">=", value="5"), CheckRule(column="rest_minutes", op="<=", value="120.0"),
                CheckRule(column="amount", op=">", value="0"), CheckRule(column="End_At", op=">=", value="start_at"),
                CheckRule(column="approver_id", op="<>", value="requester_id"),
                CheckRule(column="rest_minutes", op=">=", value="5"),     # 중복
                CheckRule(column="memo", op=">", value="0"),              # 문자 컬럼과 숫자
                CheckRule(column="start_at", op=">", value="amount"),     # 날짜와 금액
                CheckRule(column="ghost", op=">", value="0")])])          # 없는 컬럼
    model, _ = normalize_model(model, {})
    checks = [(c.column, c.op, c.value) for c in model.entity("rest_record").checks]
    assert checks == [("rest_minutes", ">=", "5"), ("rest_minutes", "<=", "120"), ("amount", ">", "0"),
                      ("end_at", ">=", "start_at"), ("approver_id", "<>", "requester_id")], "비교할 수 없는 조건은 버림"

    sql = render_script(model, "1")
    assert "CHECK (rest_minutes <= 120)" in sql and "CHECK (end_at >= start_at)" in sql
    assert "CHECK (approver_id <> requester_id)" in sql

    db = sqlite3.connect(":memory:")
    for st in render_statements(model, "sqlite"):
        db.execute(st.sql)
    row = ("INSERT INTO rest_record (rest_record_id, rest_minutes, amount, start_at, end_at, requester_id, approver_id, "
           "memo, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'now', 'now')")
    db.execute(row, (1, 30, 1000, "2026-10-26 09:00", None, 1, None))    # 선택 컬럼이 비어 있으면 통과
    for bad in [(2, 3, None, "2026-10-26 09:00", None, 1, 2),             # 5분 미만
                (3, 30, 0, "2026-10-26 09:00", None, 1, 2),               # 금액 0
                (4, 30, None, "2026-10-26 09:00", "2026-10-26 08:00", 1, 2),  # 종료가 시작보다 앞
                (5, 30, None, "2026-10-26 09:00", None, 7, 7)]:           # 본인 승인
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(row, bad)


def test_batch_request_ends_with_entities_it_must_return(sample_doc):
    fake = _AttributePrompts()
    _run(sample_doc, fake)
    prompt = next(p for p in fake.attribute_prompts if p.count("\n### ") > 1)
    assert prompt.rstrip().endswith("## 응답 entities에 반드시 2개 모두 이 name 그대로 넣는다\nservice_request, app_user")
