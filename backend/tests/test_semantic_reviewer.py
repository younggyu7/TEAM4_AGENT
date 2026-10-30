"""의미 검토 Agent: 이미 처리된 지적을 버리는 규칙과 데이터 모델 입력에 여러 컬럼 제약이 들어가는지."""
from app.agents.roles.semantic_reviewer import FunctionFinding, ModelFinding, _already_handled, _entity_line
from app.schemas.data_model import Attribute, CheckRule, Entity, RequiredWhen

FNS = {"FN-1", "FN-2"}


SOURCE = "- [R-1] 이력 조회 — 사용자는 요청 이력을 조회한다\n업무 규칙: 패스워드 변경 후 기존 세션은 모두 무효화된다."


def _fn(category="GAP", ids=("FN-1", "R-1"), evidence="요구사항: 이력을 조회한다", covered=False,
        quote="사용자는 요청 이력을 조회한다"):
    return FunctionFinding(category=category, target_ids=list(ids), message="-", evidence=evidence, suggestion="-",
                           already_covered=covered, quote=quote)


def test_function_finding_kept_when_new():
    assert not _already_handled("functions", _fn(), ["FN-1", "R-1"], FNS)
    assert not _already_handled("functions", _fn("UNMARKED_ASSUMPTION", evidence="기능 명세: '세션은 무효화된다'"),
                                ["FN-1"], FNS)


def test_model_says_already_covered():
    assert _already_handled("functions", _fn(covered=True), ["FN-1"], FNS)
    finding = ModelFinding(category="INTEGRITY", target_ids=["t"], message="-", evidence="-", suggestion="-",
                           quote="사용자는 요청 이력을 조회한다", already_covered=True)
    assert _already_handled("model", finding, ["t"], FNS)


def test_requirement_only_finding_is_left_to_requirement_reviewer():
    """기능 ID 없이 요구사항만 가리키는 지적 (예: 요구사항끼리의 개발 일정)은 요구사항 검토 Agent 몫."""
    assert _already_handled("functions", _fn("CONFLICT", ids=("R-1", "R-2")), ["R-1", "R-2"], FNS)


def test_unclear_and_app_logic_are_not_kept():
    """원문에 없는 세부를 더 정하라는 지적·애플리케이션 처리는 모델이 따로 분류하게 하고 버린다."""
    assert _already_handled("functions", _fn("UNCLEAR"), ["FN-1"], FNS, SOURCE)
    finding = ModelFinding(category="APP_LOGIC", target_ids=["t"], message="-", evidence="-", suggestion="-",
                           quote="사용자는 요청 이력을 조회한다")
    assert _already_handled("model", finding, ["t"], FNS, SOURCE)


def test_quote_must_be_in_the_input():
    assert not _already_handled("functions", _fn(), ["FN-1"], FNS, SOURCE)
    assert not _already_handled("functions", _fn(quote="사용자는  요청 이력을 조회한다."), ["FN-1"], FNS, SOURCE), \
        "공백·기호 차이는 무시"
    assert _already_handled("functions", _fn(quote="관리자는 이력을 삭제할 수 있다"), ["FN-1"], FNS, SOURCE), "입력에 없는 인용"
    assert _already_handled("functions", _fn(quote="이력 조회"), ["FN-1"], FNS, SOURCE), "너무 짧은 인용"


def test_marked_rule_is_not_an_unmarked_assumption():
    for marked in ("기능 명세: '3회 실패 후 잠금 (가정)'", "'(확인 필요 FQ-001) 관리자는 2-Factor 인증'"):
        assert _already_handled("functions", _fn("UNMARKED_ASSUMPTION", evidence=marked), ["FN-1"], FNS)
    assert not _already_handled("functions", _fn("GAP", evidence="'(가정)' 규칙과 별개로 요구사항 X가 빠짐"),
                                ["FN-1"], FNS), "GAP은 표시 여부로 버리지 않는다"


def test_entity_line_shows_multi_column_constraints():
    e = Entity(entity_id="survey_response", label="설문 응답", attributes=[
        Attribute(name="survey_id", label="설문", data_type="bigint", nullable=False, references="survey"),
        Attribute(name="app_user_id", label="응답자", data_type="bigint", nullable=False, references="app_user"),
        Attribute(name="status", label="상태", allowed_values=["작성", "제출"]),
        Attribute(name="submitted_at", label="제출 일시", data_type="timestamp"),
        Attribute(name="score", label="점수", data_type="integer"),
    ], unique_together=[["survey_id", "app_user_id"]], exactly_one_of=[["survey_id", "app_user_id"]],
        required_when=[RequiredWhen(column="submitted_at", when_column="status", when_values=["제출"])],
        checks=[CheckRule(column="score", op=">=", value="0")])
    line = _entity_line(e)
    assert "조합 유일: (survey_id, app_user_id)" in line
    assert "정확히 하나: (survey_id, app_user_id)" in line
    assert "조건부 필수: status ∈ [제출]이면 submitted_at NOT NULL" in line
    assert "값 조건: score >= 0" in line
    assert "status:varchar [작성/제출]" in line
