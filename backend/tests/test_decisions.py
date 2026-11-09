"""확인한 규칙의 '(결정)' (function_designer.confirm_rules · carry_decisions): 화면 '가정 맞음'·'추천안 맞음'으로 확인한 규칙은
새 버전에서 LLM 없이 '(결정)'이 되고, 기능을 다시 설계해 같은 문장이 '(가정)'으로 다시 나와도 '(결정)'으로 되돌린다."""
from app.agents.roles.function_designer import carry_decisions, confirm_rules
from app.schemas.function import FunctionSpec


def _fn(fid: str, rules: list[str]) -> FunctionSpec:
    return FunctionSpec(function_id=fid, name=fid, group="SFR-001", description="", requirement_ids=["SFR-001"], business_rules=rules)


def test_confirmed_assumption_becomes_decided():
    """'가정 맞음'으로 확인한 '(가정)' 규칙도 '(확인 필요)'처럼 '(결정)'으로 (표시만, 문장 그대로)"""
    fns = [_fn("FN-1", ["(가정) 근태정보는 임직원별 일자 단위로 관리한다 .", "(확인 필요) 결재는 2단계다", "제목은 필수다"])]
    out, _, missing = confirm_rules(fns, {}, [{"function_id": "FN-1", "rule": "(가정) 근태정보는 임직원별 일자 단위로 관리한다 ."},
                                             {"function_id": "FN-1", "rule": "(확인 필요) 결재는 2단계다"}])
    assert missing == []
    assert out[0].business_rules == ["(결정) 근태정보는 임직원별 일자 단위로 관리한다 .", "(결정) 결재는 2단계다", "제목은 필수다"]


def test_decided_rules_stay_decided_when_the_function_is_redesigned():
    """다시 설계해 같은 문장이 '(가정)'·'(확인 필요)'로 다시 나오면 '(결정)'으로 되돌린다 (띄어쓰기·끝 마침표 차이는 같은 문장).
    문장이 바뀐 가정, 질문 ID가 붙은 규칙, 다른 기능의 같은 문장은 그대로 둔다."""
    old = [_fn("FN-1", ["(결정) 근태정보는 임직원별 일자 단위로 관리한다 .", "(결정) 결재는 2단계다"]), _fn("FN-2", ["제목은 필수다"])]
    new = [_fn("FN-1", ["(가정) 근태정보는 임직원별 일자 단위로 관리한다.", "(확인 필요) 결재는  2단계다",
                        "(가정) 결재는 3단계다", "(확인 필요 FQ-001) 결재는 2단계다"]),
           _fn("FN-2", ["(가정) 근태정보는 임직원별 일자 단위로 관리한다."])]
    out, count = carry_decisions(old, new)
    assert count == 2
    assert out[0].business_rules == ["(결정) 근태정보는 임직원별 일자 단위로 관리한다.", "(결정) 결재는  2단계다",
                                     "(가정) 결재는 3단계다", "(확인 필요 FQ-001) 결재는 2단계다"]
    assert out[1] is new[1], "결정한 적 없는 기능은 그대로"
