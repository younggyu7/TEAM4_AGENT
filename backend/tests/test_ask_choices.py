"""고르는 질문만 묻기 (ASK_CHOICES_ONLY, 운영 기본값 켬): 예/아니오로 확인만 하는 것은 묻지 않고 추천안대로 확정하고,
고객이 서로 다른 안 중에서 골라야 하는 것만 질문으로 보인다.

- 기능 명세 확인 질문 중 선택지가 '예 / 아니오'·'둔다 / 두지 않는다'처럼 부정만 다른 것은 만들지 않고 그 후보를 추천안대로 확정한다.
- 질문 없이 남은 영향이 작은 '(확인 필요)'는 영역 묶음 질문('추천안대로 확정할까요?') 대신 추천안대로 확정한다 (표시 없음).
- 확정한 규칙은 기능 명세서 '고객 확인'에 목록으로 남는다.
다른 테스트는 conftest가 이 설정을 끄고 기존 질문 흐름(영역 묶음 질문)을 본다.
"""
import pytest

from app.agents.roles.function_designer import CHOICES_RULE, ConfirmOut, ConfirmQuestionOut, _confirm, confirm_only
from app.agents.tools.exporter import _confirm_md
from app.core.config import override_settings
from test_function_designer import _bundle_case, _confirm_case, _confirm_resolutions, _StubLLM


@pytest.mark.parametrize("options, yes_no", [
    (["예", "아니오"], True),
    (["포함", "제외"], True),
    (["채팅 기능을 둔다", "채팅 기능을 두지 않는다"], True),
    (["알림을 보내지 않는다", "알림을 보낸다"], True),
    (["사진 첨부를 필수로 한다", "사진 첨부를 필수로 하지 않는다"], True),
    (["관리자만 처리한다", "역할별로 처리한다"], False),
    (["결재선 단계 없음", "결재선 2단계"], False),
    (["재발송한다", "재발송하지 않고 실패로 기록한다"], False),
    (["기한 없이 둔다", "7일 뒤 자동 종료한다"], False),
    (["예", "아니오", "모름"], False),
])
def test_yes_no_options_are_told_apart_from_real_choices(options, yes_no):
    assert confirm_only(options) is yes_no


def test_yes_no_question_is_not_asked_and_takes_the_recommendation():
    override_settings(auto_assume=True, ask_choices_only=True)
    reqs, funcs, common = _confirm_case()
    yes_no = ConfirmQuestionOut(kind="MISSING", question="채팅 기능을 둘까요?", context="", recommended=1, item_ids=["C2"],
                                options=["채팅 기능을 둔다", "채팅 기능을 두지 않는다"])
    choice = ConfirmQuestionOut(kind="MISSING", question="관리자 권한 범위는?", context="권한", recommended=1, item_ids=["K2"],
                                options=["관리자만 처리한다", "역할별로 처리한다"])
    stub = _StubLLM(ConfirmOut(questions=[yes_no, choice], resolutions=_confirm_resolutions()))
    result, questions, _, summary = _confirm(stub, funcs, {"FN-G-01", "FN-G-02"}, common, True, reqs, [], "", whole=True)
    assert CHOICES_RULE in stub.users[0], "모델에도 고르는 질문만 만들라고 한다"
    assert [(q.question_id, q.question) for q in questions] == [("FQ-001", "관리자 권한 범위는?")], "서로 다른 안을 고르는 것만 질문"
    assert result[1].business_rules[0] == "채팅은 1:1 방으로 한다", "예/아니오 확인은 추천안대로 확정 (표시 없음)"
    assert summary["yes_no"] == 1 and {"function_id": "FN-G-02", "rule": "채팅은 1:1 방으로 한다"} in summary["settled"]


def test_minor_rules_left_over_are_settled_instead_of_a_bundle_question():
    override_settings(func_assumption_limit=0.0, ask_choices_only=True)   # 한도 방식: 묻지 않은 규칙이 '(확인 필요)'로 남는다
    reqs, funcs = _bundle_case()
    result, questions, _, summary = _confirm(_StubLLM(ConfirmOut(questions=[], resolutions=[])), funcs,
                                             {f.function_id for f in funcs}, {}, False, reqs, [], "", whole=True)
    assert questions == [] and summary["bundles"] == 0, "'추천안대로 확정할까요?' 묶음 질문을 만들지 않는다"
    assert result[0].business_rules[:2] == ["일정 제목은 비워 둘 수 없다", "일정 목록은 시작일 순으로 보여 준다"]
    assert result[0].business_rules[2].startswith("(확인 필요) "), "영향이 큰 규칙은 사람이 본다"
    assert result[1].business_rules == ["예약 메모는 200자까지 적는다", "확정 규칙"]
    assert result[2].business_rules == ["공지 제목은 비워 둘 수 없다"], "규칙이 하나뿐인 영역도 (묶음과 달리)"
    assert len(summary["settled"]) == 4 and summary["kept"] == 1


def test_functions_doc_lists_rules_settled_without_asking():
    _, funcs, _ = _confirm_case()
    confirm = {"candidates": 3, "questions": [], "settled": [{"function_id": "FN-G-02", "rule": "채팅은 1:1 방으로 한다"}]}
    text = "\n".join(_confirm_md(funcs, {}, [], confirm))
    assert "규칙 1개는 묻지 않고 추천안대로 확정했다" in text and "**묻지 않고 추천안대로 확정한 규칙**" in text
    assert "| FN-G-02 | 채팅 | 채팅은 1:1 방으로 한다 |" in text
