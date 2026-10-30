"""기능 명세 Agent: 단계 다시 만들기(v1 → v1.1), 의견 반영(바꿀 기능만), 기능 ID 유지, 이어받은 데이터 모델 (API 불필요)."""
import json
import re

from fake_llm import FakeLLM
from app import cli
from app.agents.llm import client as llm_module
from app.core.config import get_settings
from app.agents.flow.graph import build_graph, open_checkpointer
from app.agents.tools.versions import load_versions


def _base(sample_doc):
    """기준 실행(v1, 데이터 설계까지)을 만들고 출력 폴더를 돌려준다."""
    out = get_settings().output_dir
    llm_module.set_llm(FakeLLM(broken_fk=False))
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": False}}, cli._config("base"))
    return out


def _design(out, version):
    return json.loads((out / "sample" / f"v{version}" / "design.json").read_text(encoding="utf-8"))


def _functional_ids(design):
    return [r["requirement_id"] for r in design["requirements"] if r["type"] == "FUNCTIONAL" and r["scope"] == "SYSTEM"]


def test_function_rerun_needs_scope_and_stage_options(sample_doc, capsys):
    _base(sample_doc)
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1"]) == 2
    assert "다시 설계할 범위가 없습니다" in capsys.readouterr().out
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--requirements", "R-없음"]) == 2
    assert "시스템 기능 요구사항에 없는 ID: R-없음" in capsys.readouterr().out
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--full", "--entities", "app_user"]) == 2
    assert "data_modeler 단계 옵션" in capsys.readouterr().out


def test_function_rerun_redesigns_group_keeps_ids_and_stops_before_data(sample_doc):
    out = _base(sample_doc)
    v1 = _design(out, "1")
    fake = FakeLLM(broken_fk=False)
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--requirements", _functional_ids(v1)[0]]) == 0

    assert fake.calls["FunctionDesignOut"] == 1, "지정 요구사항의 그룹만 다시 설계"
    assert fake.calls["CatalogOut"] == 0 and fake.calls["AttributesOut"] == 0, "데이터 설계 전에 멈춤"
    assert (fake.calls["FunctionReviewOut"], fake.calls["ModelReviewOut"]) == (1, 0)
    folder = out / "sample" / "v1.1"
    assert (folder / "functions.md").exists() and not (folder / "data_model.json").exists()
    assert not (folder / "schema.sql").exists()
    v11 = _design(out, "1.1")
    assert [f["function_id"] for f in v11["functions"]] == [f["function_id"] for f in v1["functions"]], "이름이 같으면 ID 유지"
    assert v11["completion"]["satisfied"], "데이터 설계 조건은 해당 없음으로 통과"
    report = (folder / "review_report.md").read_text(encoding="utf-8")
    assert "다시 만든 단계: **기능 명세**" in report and "데이터 설계는 이 버전 확정 후" in report
    record = next(v for v in load_versions("sample") if v["design_version"] == "1.1")
    assert record["stage"] == "function_designer" and record["rework"]["groups"]


def test_feedback_patches_only_changed_function_and_data_design_reuses_model(sample_doc, capsys):
    out = _base(sample_doc)
    v1 = _design(out, "1")
    fake = FakeLLM(broken_fk=False)
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--feedback", "배정할 때 사유를 남긴다"]) == 0
    assert (fake.calls["FunctionScopeOut"], fake.calls["FunctionPatchOut"], fake.calls["FunctionDesignOut"]) == (1, 1, 0), \
        "범위 요청 1회 + 관련 그룹에 바꿀 기능만 요청"
    v11 = _design(out, "1.1")
    before = {f["function_id"]: f for f in v1["functions"]}
    after = {f["function_id"]: f for f in v11["functions"]}
    assert set(after) == set(before), "응답에 없는 기능은 그대로 남고 ID도 같음"
    changed = [i for i in after if after[i] != before[i]]
    assert len(changed) == 1 and after[changed[0]]["business_rules"] == ["(가정) 의견 반영"]
    assert v11["meta"]["rework"]["changed"] == changed

    # 이 기능 명세 버전에서 데이터 설계: v1의 모델을 이어받아 바뀐 기능에 연결된 엔티티만 다시 설계
    fake = FakeLLM(broken_fk=False)
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1.1"]) == 0
    printed = capsys.readouterr().out
    assert "v1의 데이터 모델을 이어받아 바뀐 기능 1개에 연결된 엔티티 1개 (service_request)" in printed
    assert fake.calls["CatalogOut"] == 0 and fake.calls["AttributesOut"] == 1
    model = {e["entity_id"]: e for e in json.loads(
        (out / "sample" / "v1.1.1" / "data_model.json").read_text(encoding="utf-8"))["entities"]}
    v1_model = {e["entity_id"]: e for e in json.loads(
        (out / "sample" / "v1" / "data_model.json").read_text(encoding="utf-8"))["entities"]}
    assert model["app_user"] == v1_model["app_user"], "바뀐 기능과 관계없는 엔티티는 그대로"
    record = next(v for v in load_versions("sample") if v["design_version"] == "1.1.1")
    assert record["rework"]["inherited_from"] == "1" and record["rework"]["changed_functions"] == changed


def test_ids_follow_names_and_removed_numbers_are_not_reused():
    from app.agents.roles.function_designer import FunctionOut, _from_design
    from app.schemas.function import FunctionSpec

    def out(name, reqs=("R1",)):
        return FunctionOut(name=name, description="", requirement_ids=list(reqs), nfr_ids=[], actors=[], preconditions=[],
                           inputs=[], outputs=[], business_rules=[], normal_flow=[], exception_flows=[], data_objects=[])

    old = [FunctionSpec(function_id="FN-G-01", group="G", name="요청 등록", description="", requirement_ids=["R1"]),
           FunctionSpec(function_id="FN-G-02", group="G", name="요청 반려", description="", requirement_ids=["R1"])]
    specs = _from_design("G", old, [out("요청 반려"), out("요청 보류", ["R2"])], {"R1", "R2"}, set())
    assert [(f.function_id, f.name) for f in specs] == [("FN-G-02", "요청 반려"), ("FN-G-03", "요청 보류")], \
        "지운 기능의 번호(01)를 다른 요구사항의 새 기능에 다시 주지 않음"


def test_renamed_functions_keep_ids_by_requirements():
    from app.agents.roles.function_designer import FunctionOut, _from_design
    from app.schemas.function import FunctionSpec

    def out(name, reqs):
        return FunctionOut(name=name, description="", requirement_ids=reqs, nfr_ids=[], actors=[], preconditions=[],
                           inputs=[], outputs=[], business_rules=[], normal_flow=[], exception_flows=[], data_objects=[])

    def old(no, name, reqs):
        return FunctionSpec(function_id=f"FN-G-0{no}", group="G", name=name, description="", requirement_ids=reqs)

    before = [old(1, "사용자 APP 인트로 화면 진입", ["R1"]), old(2, "지출 비용 등록", ["R2"]), old(3, "지출 비용 조회", ["R2"]),
              old(4, "주행거리·기간 기반 정비 알림", ["R3"]), old(5, "차량정보 및 자격증 등록", ["R4", "R5"])]
    specs = _from_design("G", before, [
        out("APP 인트로 화면 진입", ["R1"]),
        out("지출내역 등록 및 조회", ["R2"]),            # 두 기능이 하나로 합쳐짐 → 이름이 가까운 쪽 ID 하나만
        out("정비 교체 시점 푸시 알림", ["R3"]),         # 이름은 많이 달라도 요구사항이 같고 짝이 하나뿐
        out("종사자격증 진위 확인 및 차량정보 등록", ["R4", "R5", "R6"]),
        out("요청 취소", ["R7"])], {f"R{i}" for i in range(1, 8)}, set())
    assert [f.function_id for f in specs] == ["FN-G-01", "FN-G-02", "FN-G-04", "FN-G-05", "FN-G-06"], \
        "이름이 조금 바뀐 기능은 이전 ID, 정말 새 기능만 새 번호 (지운 03은 재사용 안 함)"


class _FailsFirstDesign(FakeLLM):
    """기능 명세 요청이 처음 한 번 실패하고 그다음부터 성공한다."""

    def _FunctionDesignOut(self, user):
        if self.calls["FunctionDesignOut"] == 1:
            raise ValueError("응답 형식 오류")
        return super()._FunctionDesignOut(user)


def test_failed_group_keeps_old_functions_and_is_retried(sample_doc):
    out = _base(sample_doc)
    v1 = _design(out, "1")
    fake = _FailsFirstDesign(broken_fk=False)
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--full"]) == 0
    v11 = _design(out, "1.1")
    assert [f["function_id"] for f in v11["functions"]] == [f["function_id"] for f in v1["functions"]]
    assert fake.calls["FunctionDesignOut"] == 2, "실패한 그룹은 이전 기능을 두고 다음 단계에서 다시 설계"
    decisions = [d["mode"] for d in v11["decisions"] if d["action"] == "function_designer"]
    assert decisions == ["redesign", "redesign"]


# ------------------------------------------------------------------ 공통 결정
class _Prompts(FakeLLM):
    """요청 프롬프트를 스키마별로 모은다."""

    def __init__(self):
        super().__init__(broken_fk=False)
        self.prompts: dict[str, list[str]] = {}

    def _call(self, system, user, schema, max_tokens, shared="", **kw):
        self.prompts.setdefault(schema.__name__, []).append(f"{shared}\n\n{user}" if shared else user)
        return super()._call(system, user, schema, max_tokens, shared=shared, **kw)


def test_common_decisions_are_made_once_and_shared_with_groups_and_data_model(sample_doc):
    out = get_settings().output_dir
    fake = _Prompts()
    llm_module.set_llm(fake)
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": False}}, cli._config("base"))
    assert fake.calls["CommonDecisionsOut"] == 1, "전체 설계 전에 한 번만"
    assert "## 이번 요청: 공통 결정" in fake.prompts["CommonDecisionsOut"][0]
    assert all("요청 상태: 접수 / 배정 / 완료 (근거: 가정)" in p for p in fake.prompts["FunctionDesignOut"]), \
        "모든 그룹에 같은 공통 결정"
    assert "[P1] 요청은 등록한 사용자와 배정된 담당자만 볼 수 있다 (근거: 원문)" in fake.prompts["FunctionDesignOut"][0]
    assert "## 기능 명세 공통 결정 (분류·상태 값과 업무 객체 이름은 이 목록을 따른다)" in fake.prompts["CatalogOut"][0]
    assert "요청 상태: 접수 / 배정 / 완료" in fake.prompts["AttributesOut"][0], "속성 설계도 같은 분류값을 본다"

    spec = (out / "sample" / "v1" / "functions.md").read_text(encoding="utf-8")
    assert "## 공통 결정" in spec and "| 요청 상태 | 접수 / 배정 / 완료 | 가정 |" in spec
    assert _design(out, "1")["common"]["policies"][0]["basis"] == "원문"

    # 의견 반영(부분 재설계)은 공통 결정을 다시 만들지 않고 그대로 쓴다
    fake = _Prompts()
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--feedback", "배정할 때 사유를 남긴다"]) == 0
    assert fake.calls["CommonDecisionsOut"] == 0
    assert "요청 상태: 접수 / 배정 / 완료" in fake.prompts["FunctionPatchOut"][0]
    assert _design(out, "1.1")["common"] == _design(out, "1")["common"]


def test_common_decisions_can_be_turned_off(sample_doc):
    from app.core.config import override_settings

    override_settings(func_common_decisions=False)
    fake = _Prompts()
    _base_with(sample_doc, fake)
    assert fake.calls["CommonDecisionsOut"] == 0 and "## 공통 결정" not in fake.prompts["FunctionDesignOut"][0]


def _base_with(sample_doc, fake):
    out = get_settings().output_dir
    llm_module.set_llm(fake)
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": False}}, cli._config("base"))
    return out


def test_common_rules_are_referenced_by_number_and_expanded_by_code():
    from app.agents.roles.function_designer import FunctionOut, _from_design, _function_text, common_rule_texts

    common = {"terms": [], "code_sets": [], "policies": [
        {"rule": "승인된 회원만 이용한다", "basis": "원문", "requirement_ids": []},
        {"rule": "첨부 파일은 10MB 이하", "basis": "가정", "requirement_ids": []}]}
    texts = common_rule_texts(common)
    assert texts == {"P1": "(공통 P1) 승인된 회원만 이용한다", "P2": "(공통 P2, 가정) 첨부 파일은 10MB 이하"}, \
        "근거가 가정이면 코드가 표시를 붙인다 (모델이 '(공통)'으로 가정을 감추지 않게)"
    f = FunctionOut(name="게시글 등록", description="", requirement_ids=["R1"], nfr_ids=[], actors=[], preconditions=[],
                    inputs=[], outputs=[], business_rules=["제목은 필수"], normal_flow=[], exception_flows=[],
                    data_objects=[], common_rules=["P2", "P1", "P9", "P2"])
    spec = _from_design("G", [], [f], {"R1"}, set(), texts)[0]
    assert spec.business_rules == ["제목은 필수", "(공통 P2, 가정) 첨부 파일은 10MB 이하", "(공통 P1) 승인된 회원만 이용한다"]
    text = _function_text(spec)
    assert "업무 규칙: 제목은 필수\n공통 규칙: P2, P1" in text, "의견 반영 요청에는 번호로만 보여 준다"


# ------------------------------------------------------------------ 공통 기능
class _SharedLLM(_Prompts):
    """공통 결정에서 C01-01·C01-03(서로 다른 그룹)을 공통 기능 하나로 묶고, 만드는 영역은 그 기능을 함께 만든다."""

    def _CommonDecisionsOut(self, user):
        from app.agents.roles.function_designer import SharedFunctionOut

        out = super()._CommonDecisionsOut(user)
        out.shared_functions = [SharedFunctionOut(name="요청 내보내기", scope="요청을 파일로 내보낸다",
                                                  requirement_ids=["C01-01", "C01-03", "없는-ID"])]
        return out

    def _FunctionDesignOut(self, user):
        out = super()._FunctionDesignOut(user)
        m = re.search(r"\| 연결할 요구사항: (.+)$", user, re.M)
        if m:
            out.functions.append(out.functions[0].model_copy(update={
                "name": "요청 내보내기", "requirement_ids": [x.strip() for x in m.group(1).split(",")]}))
        return out


def test_shared_function_is_made_once_by_owner_group(sample_doc):
    from app.core.config import override_settings

    override_settings(func_group_size=1)   # C01.1 [C01-01] · C01.2 [C01-02] · C01.3 [C01-03]
    fake = _SharedLLM()
    out = _base_with(sample_doc, fake)
    assert fake.calls["FunctionDesignOut"] == 2, "요구사항이 모두 다른 영역의 공통 기능에 들어간 그룹(C01.3)은 요청하지 않음"
    assert all("## 공통 기능 (만드는 영역에서 한 번만" in p for p in fake.prompts["FunctionDesignOut"])
    owner = next(p for p in fake.prompts["FunctionDesignOut"] if "(group C01.1)" in p)
    assert "## 이 영역에서 만들 공통 기능" in owner and "  - [C01-03]" in owner, "만드는 영역에 다른 영역 요구사항도 준다"

    v1 = _design(out, "1")
    shared = next(f for f in v1["functions"] if f["name"] == "요청 내보내기")
    assert shared["group"] == "C01.1" and shared["requirement_ids"] == ["C01-01", "C01-03"]
    assert not [f for f in v1["functions"] if f["group"] == "C01.3"], "같은 기능을 영역마다 만들지 않음"
    assert v1["common"]["shared_functions"] == [{"name": "요청 내보내기", "scope": "요청을 파일로 내보낸다",
                                                 "requirement_ids": ["C01-01", "C01-03"], "owner": "C01.1"}]
    assert v1["completion"]["satisfied"], "C01-03도 공통 기능으로 연결됨"
    spec = (out / "sample" / "v1" / "functions.md").read_text(encoding="utf-8")
    assert f"| 요청 내보내기 | 요청을 파일로 내보낸다 | C01-01, C01-03 | C01.1 | {shared['function_id']} |" in spec


def test_group_scope_hides_requirements_owned_elsewhere_and_targets_owner():
    from collections import OrderedDict

    from app.agents.roles.function_designer import _groups_for, _group_scope
    from app.schemas.requirement import Requirement

    reqs = {i: Requirement(requirement_id=i, source_key="G", type="FUNCTIONAL", scope="SYSTEM", category="기능",
                           title=f"요구 {i}", description="설명", priority="MUST") for i in ("A1", "A2", "B1")}
    groups = OrderedDict([("A", [reqs["A1"], reqs["A2"]]), ("B", [reqs["B1"]])])
    shared = [{"id": "S1", "name": "내보내기", "scope": "", "requirement_ids": ["B1", "A2"], "owner": "B"}]
    lines, valid = _group_scope("A", groups["A"], shared, reqs)
    assert valid == {"A1"}, "다른 영역이 맡은 요구사항으로만 만든 기능은 근거가 없어 버려진다"
    assert "공통 기능 [S1] 내보내기(B 영역)이 맡음" in lines[1]
    lines, valid = _group_scope("B", groups["B"], shared, reqs)
    assert valid == {"B1", "A2"} and any(x.startswith("  - [A2]") for x in lines)
    assert _groups_for({"A2"}, groups, shared) == ["A", "B"], "공통 기능 요구사항이 바뀌면 만드는 영역도 다시 설계"


# ------------------------------------------------------------------ 확인 질문
class _StubLLM:
    """정해 둔 확인 질문 응답을 돌려주고 요청 프롬프트를 남긴다."""

    def __init__(self, out):
        self.out, self.users = out, []

    def structured(self, *, user, **_):
        self.users.append(user)
        return self.out


def _confirm_case():
    from app.schemas.function import FunctionSpec
    from app.schemas.requirement import Requirement

    reqs = [Requirement(requirement_id=i, source_key="G", type="FUNCTIONAL", scope="SYSTEM", category="기능",
                        title=f"요구 {i}", description="설명", priority="MUST", status=s)
            for i, s in (("R1", "CONFIRMED"), ("R2", "ASSUMED"))]
    funcs = [FunctionSpec(function_id="FN-G-01", group="G", name="요청 처리", description="", requirement_ids=["R1"],
                          business_rules=["(확인 필요) 처리 기한", "제목은 필수", "(가정) 제목은 100자 이하",
                                          "(공통 P1, 확인 필요) 관리자만 처리한다"]),
             FunctionSpec(function_id="FN-G-02", group="G", name="채팅", description="", requirement_ids=["R2"],
                          business_rules=["채팅 방 구성 방식은 원문에 없다 (확인 필요)", "재발송 정책은 확인 필요로 둔다"])]
    common = {"terms": [], "code_sets": [{"name": "요청 상태", "values": ["접수", "완료"], "basis": "확인 필요",
                                          "requirement_ids": ["R1"]}],
              "policies": [{"rule": "관리자만 처리한다", "basis": "확인 필요", "requirement_ids": ["R1"]}]}
    return reqs, funcs, common


def test_confirm_asks_only_a_few_and_decides_the_rest_with_recommendations():
    from app.agents.roles.function_designer import (ConfirmOut, ConfirmQuestionOut, ResolutionOut, _confirm)

    reqs, funcs, common = _confirm_case()
    q = ConfirmQuestionOut(kind="MISSING", question="관리자 권한 범위와 채팅 기능을 둘 것인가?", context="권한·범위",
                           options=["관리자만, 채팅 둠", "역할별, 채팅 없음"], recommended=1, item_ids=["K2", "R1", "없음"])
    extra = q.model_copy(update={"question": "두 번째 질문", "item_ids": ["C1"]})
    stub = _StubLLM(ConfirmOut(questions=[q, extra], resolutions=[
        ResolutionOut(item_id="C1", rule="처리 기한은 시스템 설정값으로 둔다"),
        ResolutionOut(item_id="C2", rule="(가정) 채팅은 1:1 방으로 한다"),
        ResolutionOut(item_id="C3", rule="발송에 실패하면 재발송하지 않는다")]))
    result, questions, new_common, summary = _confirm(stub, funcs, {"FN-G-01", "FN-G-02"}, common, True, reqs, [],
                                                       "", whole=True)
    user = stub.users[0]
    assert "최대 1개" in user and "resolutions에 반드시 다음 C 후보 모두: C1, C2, C3" in user, "후보 7개의 10% → 1개"
    assert "- [R1] 요구사항 R2 요구 R2 (가정한 해석)" in user and "- [A1] (가정) 제목은 100자 이하" in user
    assert "### FN-G-01 요청 처리 (요구사항 R1) | 입력: 없음\n- [C1] (확인 필요) 처리 기한\n- 제목은 필수" in user, \
        "후보 규칙과 같은 기능의 다른 규칙·입력을 함께 보여 준다"
    assert [x.question_id for x in questions] == ["FQ-001"], "한도를 넘는 질문은 버림"
    fq = questions[0]
    assert (fq.recommended, fq.requirement_ids, fq.confirms, fq.blocking, fq.stage) == \
        ("관리자만, 채팅 둠", ["R1", "R2"], ["R2"], True, "function_designer")
    assert result[0].business_rules == ["(가정) 처리 기한은 시스템 설정값으로 둔다", "제목은 필수", "(가정) 제목은 100자 이하",
                                        "(공통 P1, 확인 필요 FQ-001) 관리자만 처리한다"]
    assert result[1].business_rules == ["(가정) 채팅은 1:1 방으로 한다", "(가정) 발송에 실패하면 재발송하지 않는다"], \
        "묻지 않은 확인 필요는 추천안 + (가정) (괄호 없이 쓴 '확인 필요'도)"
    assert new_common["code_sets"][0]["basis"] == "가정" and new_common["policies"][0]["question"] == "FQ-001"
    assert common["policies"][0].get("question") is None, "입력 공통 결정은 바꾸지 않음"
    assert (summary["candidates"], summary["budget"], summary["assumed"], summary["unresolved"]) == (7, 1, 3, 0)


def _answer_state(funcs, common, questions):
    from app.agents.flow.state import dump

    return {"functions": dump(funcs), "function_common": common, "questions": dump(questions),
            "rev": {"requirements": 1, "functions": 1, "model": 0}, "dirty_functions": []}


def test_answer_same_as_recommendation_only_marks_decided():
    from app.agents.roles.function_designer import ConfirmOut, ConfirmQuestionOut, ResolutionOut, _confirm, record_answers

    reqs, funcs, common = _confirm_case()
    q = ConfirmQuestionOut(kind="MISSING", question="관리자 권한과 처리 기한은?", context="", options=["관리자만", "역할별"],
                           recommended=1, item_ids=["K2", "C1", "R1", "A1"])
    out = ConfirmOut(questions=[q], resolutions=[ResolutionOut(item_id="A1", rule="제목 길이는 시스템 설정값으로 둔다")])
    funcs, questions, common, _ = _confirm(_StubLLM(out), funcs, {"FN-G-01", "FN-G-02"}, common, True, reqs, [], "",
                                           whole=True)
    assert funcs[0].business_rules[0] == "(확인 필요 FQ-001) 처리 기한", "추천안 규칙이 없으면 원래 문장으로 질문 표시"
    assert funcs[0].business_rules[2] == "(확인 필요 FQ-001) 제목 길이는 시스템 설정값으로 둔다", "질문에 넣은 가정도 추천안대로"
    assert funcs[1].business_rules[0].startswith("채팅 방 구성"), "정하지 못한 확인 필요는 그대로"
    fq = questions[0].model_copy(update={"status": "ANSWERED", "answer": "관리자만"})
    update = record_answers(_answer_state(funcs, common, [fq]), [fq], reqs)
    rules = update["functions"][0]["business_rules"]
    assert rules[0] == "(결정 FQ-001) 처리 기한" and rules[3] == "(공통 P1, 결정 FQ-001) 관리자만 처리한다"
    assert update["function_common"]["policies"][0]["basis"] == "결정" and "function_answers" not in update
    assert update["rev"]["functions"] == 2 and update["dirty_functions"] == ["FN-G-01"]
    assert reqs[1].status == "CONFIRMED", "확정 안 된 요구사항을 물은 질문에 답하면 확정"
    assert reqs[0].decisions == ["[FQ-001] 관리자 권한과 처리 기한은? → 관리자만"]


def test_answer_different_from_recommendation_targets_tagged_functions():
    from app.agents.roles.function_designer import ConfirmOut, ConfirmQuestionOut, _confirm, record_answers

    reqs, funcs, common = _confirm_case()
    q = ConfirmQuestionOut(kind="MISSING", question="관리자 권한은?", context="", options=["관리자만", "역할별"],
                           recommended=1, item_ids=["K2"])
    funcs, questions, common, _ = _confirm(_StubLLM(ConfirmOut(questions=[q], resolutions=[])), funcs,
                                           {"FN-G-01", "FN-G-02"}, common, True, reqs, [], "", whole=True)
    fq = questions[0].model_copy(update={"status": "ANSWERED", "answer": "역할별"})
    update = record_answers(_answer_state(funcs, common, [fq]), [fq], reqs)
    assert update["function_answers"] == ["FQ-001"] and update["function_targets"] == ["R1"]
    assert update["function_common"]["policies"][0]["answer"] == "역할별"
    assert update["functions"][0]["business_rules"][3] == "(공통 P1, 결정 FQ-001) 관리자만 처리한다 → 결정: 역할별"


class _AskingFunctionLLM(FakeLLM):
    """기능마다 '(확인 필요)'·'(가정)' 규칙을 두고, 확인 질문 고르기에서 첫 기능의 C 후보 하나를 묻는다."""

    def __init__(self):
        super().__init__(broken_fk=False)
        self.patch_prompts: list[str] = []

    def _FunctionDesignOut(self, user):
        out = super()._FunctionDesignOut(user)
        for f in out.functions:
            f.business_rules = ["(확인 필요) 처리 기한", "(가정) 제목은 100자 이하"]
        return out

    def _confirm_questions(self, user):
        from app.agents.roles.function_designer import ConfirmQuestionOut

        if "최대" not in user:
            return []
        return [ConfirmQuestionOut(kind="MISSING", question="요청 처리 기한을 둘 것인가?", context="기한이 원문에 없음",
                                   options=["기한 없음", "접수 후 24시간"], recommended=1, item_ids=["C1"])]

    def _FunctionPatchOut(self, user):
        self.patch_prompts.append(user)
        return super()._FunctionPatchOut(user)


def _ask_run(sample_doc, fake, answer):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    llm_module.set_llm(fake)
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "fq"}, "recursion_limit": 500}
    graph.invoke({"run_id": "fq", "source_path": str(sample_doc),
                  "options": {"ask_mode": "ask", "validate_db": False}}, config)
    asked = []
    while (snap := graph.get_state(config)).interrupts:
        batch = snap.interrupts[0].value["questions"]
        asked += batch
        graph.invoke(Command(resume={q["question_id"]: (answer if q["stage"] == "function_designer" else "필요")
                                     for q in batch}), config)
    return graph.get_state(config).values, asked


def test_function_question_recommended_answer_keeps_design(sample_doc):
    fake = _AskingFunctionLLM()
    state, asked = _ask_run(sample_doc, fake, "기한 없음")
    fq = [q for q in asked if q["stage"] == "function_designer"]
    assert len(fq) == 1 and fq[0]["recommended"] == "기한 없음"
    # 요구사항 질문도 초안 뒤에 함께 묻는다: 그 답('필요', 추천 없음)이 요구사항을 바꿔 그 그룹만 한 번 다시 설계한다.
    # 기능 명세 질문은 추천과 같은 답이라 고치는 요청(FunctionPatchOut)이 없다
    assert fake.calls["FunctionPatchOut"] == 0 and fake.calls["FunctionDesignOut"] == 2
    assert {q["stage"] for q in asked} >= {"requirements", "function_designer"}
    rules = [r for f in state["functions"] for r in f["business_rules"]]
    # 같은 그룹을 다시 설계해 '(결정 FQ-001)' 표시는 새 규칙에 남지 않는다. 답은 요구사항 결정으로 재설계에 넘어간다
    assert "(가정) 추천안 C1" in rules
    assert rules.count("(가정) 추천안 C2") == 1 and rules.count("(가정) 제목은 100자 이하") == 3, "묻지 않은 것은 추천안"
    assert any("[FQ-001]" in d for r in state["requirements"] for d in r["decisions"])
    assert state["completion"]["satisfied"]


def test_function_question_other_answer_patches_only_related_functions(sample_doc):
    fake = _AskingFunctionLLM()
    state, asked = _ask_run(sample_doc, fake, "접수 후 24시간")
    assert sum(q["stage"] == "function_designer" for q in asked) == 1, "같은 질문을 다시 묻지 않음"
    assert fake.calls["FunctionDesignOut"] == 1 and fake.calls["FunctionPatchOut"] == 1, "그룹 전체가 아니라 바꿀 기능만"
    assert "[FQ-001] 요청 처리 기한을 둘 것인가? → 답: 접수 후 24시간 (추천안: 기한 없음)" in fake.patch_prompts[0]
    assert "결정: [FQ-001]" in fake.patch_prompts[0], "답은 요구사항 결정으로도 넘어간다"
    assert state["function_answers"] == [] and state["function_targets"] is None
    assert not any("(확인 필요 FQ-001)" in r for f in state["functions"] for r in f["business_rules"])
    assert fake.calls["CatalogOut"] == 1 and state["completion"]["satisfied"], "데이터 설계는 답을 반영한 뒤 한 번"


def test_manual_wait_is_not_swallowed_by_common_decisions():
    from app.agents.roles.function_designer import _common_decisions
    from app.agents.llm.client import LLMPending

    class _Waiting:
        def structured(self, **_):
            raise LLMPending("응답 대기")

    import pytest
    with pytest.raises(LLMPending):
        _common_decisions(_Waiting(), {}, [])


def test_rerun_with_answers_applies_stage_one_answers_at_once(sample_doc, tmp_path, capsys):
    """화면 1단계 제출: 기능 명세 초안과 함께 받은 확인 질문 답을 한 번에 반영하고, 답이 바꾼 것만 다시 설계한다."""
    out = _base_with(sample_doc, _AskingFunctionLLM())
    v1 = _design(out, "1")
    qs = {q["question_id"]: q for q in v1["questions"]}
    assert {"Q-001", "FQ-001"} <= set(qs) and qs["Q-001"]["status"] == "DEFERRED"

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"X-999": "?"}), encoding="utf-8")
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--answers", str(bad)]) == 2
    assert "기준 버전에 없는 질문 ID: X-999" in capsys.readouterr().out
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--answers", str(bad)]) == 2

    # 추천과 같은 기능 명세 답만: 확정 표시만 하고 다시 설계하지 않는다
    same = tmp_path / "same.json"
    same.write_text(json.dumps({"FQ-001": qs["FQ-001"]["recommended"]}), encoding="utf-8")
    fake = _AskingFunctionLLM()
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--answers", str(same)]) == 0
    assert "모두 추천안과 같아 확정 표시만" in capsys.readouterr().out
    assert fake.calls["FunctionDesignOut"] == 0 and fake.calls["FunctionPatchOut"] == 0
    v11 = _design(out, "1.1")
    assert next(q for q in v11["questions"] if q["question_id"] == "FQ-001")["status"] == "ANSWERED"
    assert any("(결정 FQ-001)" in r for f in v11["functions"] for r in f["business_rules"])

    # 요구사항 질문 답(추천 없음) → 그 요구사항만 다시 설계, 데이터 설계 전에 멈춤
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"Q-001": "필요", "FQ-001": qs["FQ-001"]["recommended"]}), encoding="utf-8")
    fake = _AskingFunctionLLM()
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--answers", str(answers)]) == 0
    assert "다시 설계할 요구사항 1건" in capsys.readouterr().out
    assert fake.calls["FunctionDesignOut"] == 1 and fake.calls["CatalogOut"] == 0
    v12 = _design(out, "1.2")
    target = next(r for r in v12["requirements"] if any(d.startswith("[Q-001]") for d in r["decisions"]))
    assert target["status"] == "CONFIRMED"
    assert not (out / "sample" / "v1.2" / "data_model.json").exists()
