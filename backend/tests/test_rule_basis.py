"""업무 규칙 근거 대조: 표시 없는 규칙이 원문에 근거가 없으면 '(가정)'을 붙인다 (API 불필요)."""
from app.schemas.function import ExceptionFlow, FunctionSpec
from app.schemas.requirement import Evidence, Requirement
from app.agents.tools.exporter import _confirm_md
from app.agents.tools.rule_basis import (content_words, impact_of, lift_marks, mark_unsupported, source_text, trim_rules,
                                         unsupported)

SOURCE = "연차 생성 기준을 설정하고 임직원별 연차 생성과 확정, 조정, 정산을 지원한다. 지각은 정규 출근시간 대비 10분 초과 시 표시한다."


def _req(text=SOURCE, rid="R1"):
    return Requirement(requirement_id=rid, source_key="G", type="FUNCTIONAL", scope="SYSTEM", category="기능", title="연차관리", description=text, priority="MUST",
                       evidence=[Evidence(source_block_id="b", quote=text, verified=True)])


def _fn(*rules, reqs=("R1",)):
    return FunctionSpec(function_id="FN-1", group="G", name="연차", description="", requirement_ids=list(reqs), nfr_ids=[], actors=[], preconditions=[], inputs=[], outputs=[],
                        business_rules=list(rules), normal_flow=[], exception_flows=[ExceptionFlow(condition="c", handling="h")], data_objects=[])


def test_number_not_in_source_is_unsupported():
    assert "15분" in unsupported("지각은 정규 출근시간 대비 15분 초과 시 판정한다", SOURCE)
    assert unsupported("지각은 정규 출근시간 대비 10분 초과 시 표시한다", SOURCE) == "", "원문에 있는 수치"
    assert "20" in unsupported("월근무일수는 20 이상 23 이하여야 한다", SOURCE), "단위 없는 기준값도 본다"


def test_rule_with_new_content_is_unsupported_and_paraphrase_is_not():
    assert unsupported("비밀번호는 복잡도 정책을 만족해야 하고 해시로 저장한다", SOURCE).startswith("원문에 없는 내용")
    assert unsupported("임직원별 연차를 생성 기준에 따라 만든다", SOURCE) == "", "표현만 바꾼 규칙은 근거가 있다"
    assert unsupported("연차를 조회한다", SOURCE) == "", "내용 낱말이 적은 규칙은 낱말로 판단하지 않는다"


def test_generic_words_are_not_evidence():
    assert "등록" not in content_words("휴가 등록 내역을 조회한다")
    assert content_words("지각은 자동으로 판정된다") == ["지각", "자동", "판정"]


def test_only_plain_rules_are_marked_and_result_is_stable():
    fn = _fn("비밀번호는 복잡도 정책을 만족해야 하고 해시로 저장한다", "(확인 필요 FQ-001) 비밀번호는 복잡도 정책을 만족한다", "(공통 P1) 접속 기록을 암호화해 보관한다",
             "(결정 FQ-002) 비밀번호는 복잡도 정책을 만족한다", "연차 생성과 확정, 조정을 지원한다")
    out, found = mark_unsupported([fn], {"R1": _req()})
    assert out[0].business_rules[0].startswith("(가정) 비밀번호") and len(found) == 1
    assert out[0].business_rules[1:] == fn.business_rules[1:], "이미 표시가 있거나 근거가 있는 규칙은 그대로"
    again, more = mark_unsupported(out, {"R1": _req()})
    assert again[0].business_rules == out[0].business_rules and more == [], "두 번 돌려도 같다"


def test_function_without_source_is_left_alone():
    out, found = mark_unsupported([_fn("비밀번호는 복잡도 정책을 만족해야 하고 해시로 저장한다", reqs=("없는요구사항",))], {})
    assert found == [] and not out[0].business_rules[0].startswith("(가정)")


def test_common_decisions_count_as_evidence():
    rule = "조회 결과는 엑셀 PDF 내보내기 공통 기능으로 내려받는다"
    common = {"shared_functions": [{"name": "엑셀 PDF 내보내기", "scope": "조회 결과를 내려받는 공통 기능"}]}
    assert unsupported(rule, source_text(_fn(), {"R1": _req()})) != ""
    assert unsupported(rule, source_text(_fn(), {"R1": _req()}, common)) == "", "공통 기능을 쓴다는 규칙은 근거가 있다"


def test_assumed_common_decisions_are_not_evidence():
    rule = "결재 차례를 건너뛴 문서는 보관함으로 이동시킨다"
    assumed = {"policies": [{"rule": rule, "basis": "가정"}], "code_sets": [{"name": "보관함", "values": ["결재 차례"], "basis": "확인 필요"}]}
    assert unsupported(rule, source_text(_fn(), {"R1": _req()}, assumed)) != "", "모델이 정한 규칙은 같은 내용의 규칙을 근거 있게 만들지 못한다"
    sourced = {"policies": [{"rule": rule, "basis": "원문"}]}
    assert unsupported(rule, source_text(_fn(), {"R1": _req()}, sourced)) == ""


def test_confirm_section_reports_rules_marked_by_code():
    lines = _confirm_md([_fn("(가정) 규칙")], {}, [], {"candidates": 1, "limit": 0.1, "basis_marked": 3})
    assert any("3개는 표시가 없었지만" in line for line in lines), "코드가 붙인 표시 수를 문서에 적는다"
    assert not any("표시가 없었지만" in line for line in _confirm_md([_fn("(가정) 규칙")], {}, [], {"candidates": 1, "limit": 0.1}))


def _trim(*rules, **fields):
    fn = _fn(*rules).model_copy(update={"name": "자원 관리", "actors": ["시스템 관리자"], "data_objects": ["자원"], **fields})
    out, removed = trim_rules([fn])
    return out[0].business_rules, [x[1] for x in removed]


def test_history_table_follows_values_of_its_source():
    from app.schemas.data_model import Attribute, Entity
    from app.agents.tools.model_validator import _align_history_values

    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    owner = Entity(entity_id="employee", attributes=[Attribute(name="employee_id", **pk), Attribute(name="employment_status", allowed_values=["재직자", "퇴직자"])])
    history = Entity(entity_id="employment_status_history", kind="HISTORY", attributes=[
        Attribute(name="id", **pk), Attribute(name="employee_id", data_type="bigint", references="employee"), Attribute(name="employment_status", allowed_values=["재직", "퇴직"])])
    other = Entity(entity_id="payment", kind="TRANSACTION", attributes=[Attribute(name="employee_id", data_type="bigint", references="employee"), Attribute(name="employment_status", allowed_values=["A", "B"])])
    notes = []
    _align_history_values([owner, history, other], {e.entity_id: e for e in (owner, history, other)}, notes)
    assert history.attributes[2].allowed_values == ["재직자", "퇴직자"] and len(notes) == 1, "엔티티를 나눠 설계해도 이력의 값 표현은 원본과 같다"
    assert other.attributes[1].allowed_values == ["A", "B"], "이력이 아닌 테이블은 다른 뜻일 수 있다"


def test_user_feedback_counts_as_evidence():
    rule = "근로기준법에 따라 지각자는 다음 달 상여금을 감액한다"
    assert mark_unsupported([_fn(rule)], {"R1": _req()})[1] != [], "원문에 없으면 가정이다"
    assert mark_unsupported([_fn(rule)], {"R1": _req()}, note=f"의견: {rule}")[1] == [], "사용자가 의견으로 정한 내용은 다시 가정으로 표시하지 않는다"


def test_word_stem_keeps_two_letter_words():
    assert content_words("권한과 기한을 회의에서 제한한다") == ["권한", "기한", "회의", "제한"], "'한'·'의'는 어미가 아니다"
    assert content_words("문서를 승인한 담당자가 보관할 자원은") == ["문서", "승인", "담당자", "보관", "자원"]


def test_rule_without_any_word_is_not_treated_as_repeated():
    assert _trim("(가정) 등록 후 수정", "(확인 필요) 처리 기한")[1] == [], "낱말이 없다고 되풀이로 보지 않는다"


def test_rule_repeating_other_fields_is_removed():
    kept, removed = _trim("(확인 필요) 시스템 관리자가 자원을 등록, 수정, 사용 중지할 수 있다.", "(가정) 자원은 등록, 조회, 수정으로 관리한다.")
    assert kept == [] and len(removed) == 2, "조건 없이 기능의 다른 칸을 되풀이하면 고객이 정할 내용이 없다"


def test_rule_with_behavior_stays_even_if_its_words_are_elsewhere():
    fn = {"name": "회원 정보 변경과 수당 계산", "description": "회원 정보 변경 이력 저장, 소득코드 수당 추가, 앱 알림 공지 전달, 외부 자격 확인 API 진위 확인, 이월 연차 보유", "data_objects": ["회원 정보"]}
    rules = ["(가정) 회원 정보 변경 시 변경 이력을 저장한다.", "(가정) 수당은 등록된 소득코드에 따라 추가된다.", "(가정) 공지 전달은 앱 알림으로 제공한다.",
             "(가정) 진위 확인은 외부 자격 확인 API를 통해 수행한다.", "(가정) 이월 연차는 별도로 보유할 수 있다."]
    assert _trim(*rules, **fn) == (rules, []), "낱말이 다른 칸에 있어도 조건·계산·연계·동작이면 규칙이다"


def test_rule_with_constraint_or_new_content_stays():
    rules = ["(확인 필요) 시스템 관리자만 자원을 수정할 수 있다.", "(가정) 사용 중지된 자원은 예약할 수 없다.", "(확인 필요) 임직원은 본인의 자원을 등록할 수 있다.",
             "(가정) 자원은 3개까지 등록한다.", "(확인 필요) 자원은 팀장의 결재를 받아 사내 메신저로 공유한다."]
    assert _trim(*rules) == (rules, []), "조건·제한·수치가 있거나 새 내용이면 남긴다"


def test_only_marked_rules_can_be_removed():
    rules = ["자원을 등록할 수 있다.", "(결정 FQ-001) 자원을 등록할 수 있다.", "(확인 필요 FQ-002) 자원을 수정할 수 있다.", "(공통 P1) 자원을 조회한다."]
    assert _trim(*rules) == (rules, []), "원문 근거·질문·결정·공통 규칙은 지우지 않는다"


def test_duplicate_needs_every_word_in_the_longer_rule():
    longer = "(확인 필요) 전표 승인과 승인해제는 재무 담당자가 처리하며 결재 절차를 거치지 않는다."
    assert _trim("(가정) 전표 승인해제는 재무 담당자가 처리한다.", longer, data_objects=["기타"], name="결산")[1] == [], "부정이 다르면 다른 규칙"
    same = "(확인 필요) 전표 승인과 승인해제는 재무 담당자가 처리하고 회계 부서에 통보한다."
    assert _trim("(확인 필요) 전표 승인해제는 재무 담당자가 처리한다.", same, data_objects=["기타"], name="결산")[1] == ["(확인 필요) 전표 승인해제는 재무 담당자가 처리한다."]
    limited = "(가정) 승인된 전표는 승인해제 전까지 수정할 수 없다."
    other = "(가정) 전표 승인과 승인해제는 재무 담당자가 처리하고 승인된 전표는 사유 없이 바꿀 수 없다."
    assert limited in _trim(limited, other, data_objects=["기타"], name="결산")[0], "일반 낱말('수정')이 빠져도 다른 규칙이다"


def test_stronger_rule_is_not_removed_for_weaker_one():
    plain, assumed = "전표 승인은 재무 담당자가 처리한다.", "(가정) 전표 승인은 재무 담당자가 처리하고 회계 부서에 통보한다."
    assert _trim(plain, assumed, data_objects=["기타"], name="결산")[1] == [], "원문 근거 규칙을 가정 규칙 때문에 지우지 않는다"


def test_only_given_functions_are_touched():
    fn = _fn("(가정) 연차는 등록할 수 있다.")
    out, removed = trim_rules([fn], only=set())
    assert out == [fn] and removed == []
    marked, found = mark_unsupported([_fn("근로기준법에 따라 지각자는 다음 달 상여금을 감액한다")], {"R1": _req()}, only=set())
    assert found == [] and not marked[0].business_rules[0].startswith("(가정)"), "다시 설계하지 않은 기능의 규칙은 바꾸지 않는다"


def test_impact_ranks_decisions_above_simple_defaults():
    assert impact_of("(가정) 일정의 종료 일시는 시작 일시보다 늦어야 한다.") == 0
    assert impact_of("(가정) 메신저는 그룹 대화를 제공하지 않고 개인 대화만 지원한다.") >= 1
    assert impact_of("(가정) 승인된 급여는 소급 변경을 허용하지 않는다.") > impact_of("(가정) 예약 목록은 시작 일시 순으로 보여 준다.")
    assert impact_of("(가정) 예약 취소와 변경은 예약자 본인과 시스템 관리자만 가능하다.") >= 2, "누가 할 수 있는지 제한하면 사람이 하나씩 본다"
    assert impact_of("(가정) 메뉴 사용권한은 역할 단위로만 부여하며 개별 예외는 없다.") >= 2


def test_impact_does_not_depend_on_business_domain():
    for rule in ("(가정) 잠금 해제는 관리자만 할 수 있다", "(가정) 관리자 로그인은 허용 IP에서만 가능하다", "(가정) 기업 계정당 조사 하나에 한 번만 응답한다", "(가정) 그룹 채팅은 후속 범위로 한다"):
        assert impact_of(rule) >= 2, rule
    assert impact_of("(가정) 운임 요금은 거리로 청구한다") >= 1, "돈을 말하는 일반 낱말"
    assert impact_of("(가정) 일정 제목은 비워 둘 수 없다") <= 1, "단순 입력 검증은 묶어서 확인해도 된다"


def test_mark_in_the_middle_moves_to_the_front():
    assert lift_marks("개인정보는 제한될 수 있다 ((가정) 범위는 미정)") == "(가정) 개인정보는 제한될 수 있다"
    assert lift_marks("답변 등록 시 문의자에게 알린다 (가정)") == "(가정) 답변 등록 시 문의자에게 알린다"
    assert lift_marks("승인자를 정한다 (확인 필요)") == "(확인 필요) 승인자를 정한다"
    assert lift_marks("(가정) 그대로 둔다 (가정)") == "(가정) 그대로 둔다 (가정)", "이미 앞에 표시가 있으면 건드리지 않는다"
    assert lift_marks("금액(원)을 표시한다") == "금액(원)을 표시한다", "표시가 아닌 괄호는 그대로"
