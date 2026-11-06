"""추천안 자동 적용 (AUTO_ASSUME, 운영 기본값 켬): 꼭 확인할 것만 묻고, 추천안이 있는 나머지 확인 대상은 추천안으로 정한다.

- 기능 명세: '(가정)' 한도 없이 묻지 않은 '(확인 필요)'를 추천안 '(가정)'으로. 질문은 모델이 꼭 필요하다고 고른 것만
  (전체 설계 FUNC_MAX_QUESTIONS개, 일부 다시 설계 AUTO_PARTIAL_QUESTIONS개까지), 영역 묶음 질문은 생기지 않는다.
- 요구사항 검토: 분류가 애매한 요구사항은 묻지 않고 판단한 분류(추천안)를 결정으로 남긴다. 원문에 없는 요구사항은 범위라 계속 묻는다.
- 요구사항 추출 질문(Q)은 막지 않는 것도 그대로 낸다 (원문끼리 다른 이관 범위처럼 고객이 정할 내용이 막지 않는 질문으로 나온다).
다른 테스트는 conftest가 AUTO_ASSUME을 끄고 기존 질문 흐름('(가정)' 한도 방식)을 본다.
"""
from app.agents.llm import client as llm_module
from app.agents.roles import coordinator, requirement_reviewer
from app.agents.roles import requirements_analyst as ra
from app.agents.roles.function_designer import AUTO_PARTIAL_QUESTIONS, ConfirmOut, ConfirmQuestionOut, _confirm
from app.core.config import override_settings
from test_function_designer import _confirm_case, _confirm_resolutions, _StubLLM
from test_requirement_reviewer import FUNC, ReviewLLM, _only, _req
from test_requirement_reviewer import _state as review_state
from test_requirements_analyst import ScriptedLLM
from test_requirements_analyst import _state as extract_state

DESIGNED = {"FN-G-01", "FN-G-02"}


def test_function_design_asks_only_what_matters_and_assumes_the_rest():
    override_settings(auto_assume=True)   # 한도(기본 10%)가 있어도 쓰지 않는다
    reqs, funcs, common = _confirm_case()
    q = ConfirmQuestionOut(kind="MISSING", question="관리자 권한 범위는?", context="권한", options=["관리자만", "역할별"],
                           recommended=1, item_ids=["K2"])
    stub = _StubLLM(ConfirmOut(questions=[q], resolutions=_confirm_resolutions(), must_review=["C2"]))
    result, questions, new_common, summary = _confirm(stub, funcs, DESIGNED, common, True, reqs, [], "", whole=True)
    user = stub.users[0]
    assert "가정 한도는 없다" in user and "많아도 12개" in user and "must_review는 비운다" in user
    assert [x.question_id for x in questions] == ["FQ-001"], "모델이 고른 질문만 (영역 묶음 질문 없음)"
    assert result[0].business_rules == ["(가정) 처리 기한은 시스템 설정값으로 둔다", "제목은 필수", "(가정) 제목은 100자 이하",
                                        "(공통 P1, 확인 필요 FQ-001) 관리자만 처리한다"], \
        "묻지 않은 확인 필요는 한도와 관계없이 추천안 (가정), 원래 (가정)도 그대로"
    assert result[1].business_rules == ["(가정) 채팅은 1:1 방으로 한다", "(가정) 발송에 실패하면 재발송하지 않는다"], \
        "모델이 하나씩 보라고 한 것(must_review)도 추천안으로 (중요하면 질문으로 고른다)"
    assert new_common["code_sets"][0]["basis"] == "가정"
    assert (summary["auto"], summary["kept"], summary["unresolved"], summary["bundles"]) == (True, 0, 0, 0)


def test_rule_without_recommendation_stays_for_review_and_partial_redesign_asks_few():
    override_settings(auto_assume=True)
    reqs, funcs, common = _confirm_case()
    stub = _StubLLM(ConfirmOut(questions=[], resolutions=_confirm_resolutions()[:2]))   # C3는 추천안을 못 정함
    result, questions, _, summary = _confirm(stub, funcs, DESIGNED, common, False, reqs, [], "", whole=False)
    assert f"많아도 {AUTO_PARTIAL_QUESTIONS}개" in stub.users[0], "일부만 다시 설계하면 질문은 조금만"
    assert questions == [] and result[1].business_rules[1].startswith("(확인 필요) "), "추천안이 없으면 사람이 본다"
    assert summary["unresolved"] == 1


def test_ambiguous_classification_takes_the_review_without_a_question(sample_doc):
    state, rid = review_state(sample_doc, ask_mode="ask")
    override_settings(auto_assume=True)
    llm_module.set_llm(ReviewLLM(_only(rid, FUNC, "low")))
    state.update(requirement_reviewer.run(state))
    req = _req(state, rid)
    assert req["type"] == "FUNCTIONAL" and req["status"] == "ASSUMED", "판단한 분류(추천안)로 둔다"
    assert req["review"] == [{"kind": "CLASSIFICATION", "note": "기능(추가 여부 판단 필요) — 내용상 화면 기능 (추천안 적용)",
                              "question_id": ""}], "확인 대기가 아닌 표시만 (기능 명세가 '(확인 필요 RC-…)'를 붙이지 않음)"
    assert "[분류 검토] 기능 / 시스템 / 기능로 판단 (추천안 적용, 확인 질문 없음)" in req["decisions"]
    assert not any(q["question_id"].startswith("RC-") for q in state["questions"])
    decision, _ = coordinator.decide(state)
    assert decision.action != "ask_user"


def test_requirement_not_in_source_is_still_asked(sample_doc):
    """원문에 없는 요구사항을 넣을지는 범위 결정이라 추천안이 있어도 묻는다."""
    state, rid = review_state(sample_doc, ask_mode="ask")
    override_settings(auto_assume=True)
    other = next(r["requirement_id"] for r in state["requirements"] if r["type"] == "FUNCTIONAL")
    _req(state, other)["description"] = "모바일 앱 푸시로 영수증 OCR 결과를 재전송한다"   # 원문과 많이 다름 → 판정 후보
    llm_module.set_llm(ReviewLLM(_only(other, FUNC, in_source=False)))
    state.update(requirement_reviewer.run(state))
    note = _req(state, other)["review"][0]
    assert note["kind"] == "ADDED" and note["question_id"].startswith("RC-")


def test_extraction_keeps_non_blocking_questions():
    override_settings(auto_assume=True)
    llm_module.set_llm(ScriptedLLM())   # 단위마다 blocking=false 질문 하나
    try:
        out = ra.run(extract_state())
    finally:
        llm_module.set_llm(None)
    assert len(out["requirements"]) == 5 and len(out["questions"]) == 5, "막지 않는 요구사항 질문도 묻는다"
