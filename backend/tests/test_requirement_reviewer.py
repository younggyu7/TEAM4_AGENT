"""요구사항 검토 Agent: 원문 대조(빠진 요구사항 보충·분류 확인·원문 외 추가 표시)와 요구사항 충돌 검토,
그리고 그 표시가 기능 명세 Agent에 넘어가는지 (가짜 LLM, API 키 불필요)."""
import re
from collections import defaultdict

from langgraph.checkpoint.memory import InMemorySaver

from fake_llm import FakeLLM
from app.agents.llm import client as llm_module
from app.agents.flow import nodes
from app.agents.roles import coordinator, function_designer, requirement_reviewer
from app.agents.roles.requirement_reviewer import (EXCLUDE, INCLUDE, ConflictReviewOut, MissingRequirement,
                                                   RequirementConflict, RequirementVerdict, SourceReviewOut, signals)
from app.agents.flow.graph import build_graph
from app.schemas.design import Requirement

FUNC = ("FUNCTIONAL", "SYSTEM", "기능")


class ReviewLLM(FakeLLM):
    """원문 대조 응답을 테스트마다 정한다.

    verdict(rid, 지금 분류) -> (분류, confidence, in_source). 없으면 모두 지금 분류 유지.
    missing(user) -> [MissingRequirement]. 요청마다 user 프롬프트를 schema 이름별로 모은다 (seen)."""

    def __init__(self, verdict=None, missing=None, fail=False):
        super().__init__()
        self.verdict, self.missing, self.fail = verdict, missing, fail
        self.seen: dict[str, list[str]] = defaultdict(list)
        self.efforts: list[str] = []

    def _call(self, system, user, schema, max_tokens, shared="", **kw):
        self.seen[schema.__name__].append(f"{shared}\n\n{user}" if shared else user)
        self.efforts.append(kw.get("effort", ""))
        return super()._call(system, user, schema, max_tokens, shared, **kw)

    def _SourceReviewOut(self, user):
        if self.fail:
            raise RuntimeError("원문 대조 요청 실패")
        out = []
        for rid, t, s, c in re.findall(r"^- \[([^\]]+)\] 검토 대상 \(지금: (\w+) / (\w+) / (\S+)\)", user, re.M):
            cls, conf, in_source = self.verdict(rid, (t, s, c)) if self.verdict else ((t, s, c), "high", True)
            out.append(RequirementVerdict(requirement_id=rid, type=cls[0], scope=cls[1], category=cls[2],
                                          confidence=conf, in_source=in_source,
                                          source_note="" if in_source else "원문에 없는 재발송 기능",
                                          reason="내용상 화면 기능", evidence="요청을 등록"))
        return SourceReviewOut(verdicts=out, missing=self.missing(user) if self.missing else [])


def _state(sample_doc, ask_mode="defer"):
    """가짜 LLM으로 끝까지 돈 상태에서, 기능 요구사항 하나를 SFR 번호인데 비기능으로 잘못 분류된 것으로 바꾸고 다시 검토하게 한다."""
    llm_module.set_llm(FakeLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}, "recursion_limit": 500}
    graph.invoke({"run_id": "t1", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": False, "only": [], "max_sources": 0}}, config)
    state = dict(graph.get_state(config).values)
    state["options"] = {**state["options"], "ask_mode": ask_mode}
    target = next(r for r in state["requirements"] if r["type"] == "FUNCTIONAL")
    target.update(source_ref="SFR-001", type="NON_FUNCTIONAL", category="성능")
    state["requirement_review"] = {}
    return state, target["requirement_id"]


def _only(rid, cls, conf="high", in_source=True):
    """rid만 정한 판정을 내고 나머지는 지금 분류 유지."""
    return lambda r, now: (cls, conf, in_source) if r == rid else (now, "high", True)


def _req(state, rid):
    return next(r for r in state["requirements"] if r["requirement_id"] == rid)


def _answer(state, qid, answer):
    state.update({"next_action": {"action": "ask_user", "scope": [qid]}, "pending_answers": {qid: answer},
                  "dirty_requirements": []})
    state.update(nodes.record_decision(state))


def test_signals_flag_only_suspicious_classification():
    ok = Requirement(requirement_id="SFR-001-01", source_ref="SFR-001", type="FUNCTIONAL", scope="SYSTEM",
                     category="기능", title="요청 등록", description="사용자는 요청을 등록할 수 있어야 한다")
    assert signals(ok, None) == []
    wrong = ok.model_copy(update={"type": "NON_FUNCTIONAL", "category": "성능"})
    assert any("SFR" in s for s in signals(wrong, None))
    duty = ok.model_copy(update={"type": "NON_FUNCTIONAL", "category": "사업지원", "title": "사용자 교육",
                                 "description": "운영자 교육을 2회 실시"})
    assert any("수행 의무 표현" in s for s in signals(duty, None))


def test_review_runs_after_extraction_before_function_design(sample_doc):
    llm_module.set_llm(FakeLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}, "recursion_limit": 500}
    graph.invoke({"run_id": "t1", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": False, "only": [], "max_sources": 0}}, config)
    state = graph.get_state(config).values
    actions = [d["action"] for d in state["decisions"]]
    assert actions.index("requirement_reviewer") < actions.index("function_designer")
    assert actions.count("requirement_reviewer") == 1, "검토한 요구사항은 다시 보지 않는다"
    assert set(state["requirement_review"]["reviewed"]) == {r["requirement_id"] for r in state["requirements"]}


def test_nothing_to_send_skips_llm(sample_doc):
    """요구사항에 담기지 않은 원문 문장도 판정 후보도 없으면 LLM을 부르지 않고 검토한 것으로 둔다."""
    state, rid = _state(sample_doc)
    _req(state, rid).update(source_ref="", type="FUNCTIONAL", category="기능")   # 잘못 분류한 것을 되돌림 → 신호 없음
    fake = ReviewLLM()
    llm_module.set_llm(fake)
    update = requirement_reviewer.run(state)
    assert fake.calls["SourceReviewOut"] == 0
    assert set(update["requirement_review"]["reviewed"]) == {r["requirement_id"] for r in state["requirements"]}


def test_prompt_sends_only_candidates_and_high_confidence_change_reclassifies(sample_doc):
    state, rid = _state(sample_doc)
    assert coordinator.decide(state)[0].action == "requirement_reviewer"
    fake = ReviewLLM(_only(rid, FUNC))
    llm_module.set_llm(fake)
    update = requirement_reviewer.run(state)
    prompt = fake.seen["SourceReviewOut"][0]
    assert "## 원문 단위" in prompt and "### 요구사항 제목" in prompt and "### 판정할 요구사항" in prompt
    assert re.search(rf"\[{rid}\] 검토 대상 .*의심 이유: .*SFR", prompt), "규칙 신호가 있는 요구사항만 판정한다"
    assert len(re.findall(r"검토 대상", prompt)) == 1, "신호 없는 요구사항은 판정을 받지 않는다"
    assert "  원문 (B" in prompt and "### 담기지 않은 원문 문장" not in prompt, "모두 담긴 원문은 보내지 않는다"
    assert fake.efforts == ["low", "low"], "원문 대조·충돌 검토 요청 모두 생각 수준을 낮게"

    req = _req(update, rid)
    assert (req["type"], req["scope"], req["category"]) == FUNC and req["review"] == []
    assert req["decisions"][-1].startswith("[분류 검토] 비기능 / 시스템 / 성능 → 기능 / 시스템 / 기능")
    assert update["rev"]["requirements"] == state["rev"]["requirements"] + 1
    assert rid in update["dirty_requirements"], "바뀐 요구사항만 기능 명세에서 다시 설계"
    assert "분류 변경 1" in update["requirement_review"]["last"]
    state.update(update)
    assert coordinator.decide(state)[0].action != "requirement_reviewer", "한 번 검토하면 끝"


def test_ambiguous_classification_is_flagged_and_answer_settles_it(sample_doc):
    state, rid = _state(sample_doc, ask_mode="ask")
    llm_module.set_llm(ReviewLLM(_only(rid, FUNC, "low")))
    state.update(requirement_reviewer.run(state))
    req = _req(state, rid)
    assert req["type"] == "FUNCTIONAL" and req["status"] == "ASSUMED", "판단한 분류로 두고 표시한다"
    assert req["review"] == [{"kind": "CLASSIFICATION", "note": "기능(추가 여부 판단 필요) — 내용상 화면 기능",
                              "question_id": "RC-001"}]
    q = next(q for q in state["questions"] if q["question_id"] == "RC-001")
    assert q["stage"] == "requirement_reviewer" and q["blocking"] and q["recommended"] == "기능 / 시스템 / 기능"
    assert "비기능 / 시스템 / 성능" in q["options"], "요구사항 Agent 분류도 고를 수 있다"
    decision, _ = coordinator.decide(state)
    assert decision.action == "ask_user" and "RC-001" in decision.scope

    _answer(state, "RC-001", "기능 / 시스템 / 기능")   # 추천과 같은 답: 확정만
    req = _req(state, rid)
    assert req["review"] == [] and req["status"] == "CONFIRMED" and not state["dirty_requirements"]


def test_classification_answer_different_from_review_reclassifies(sample_doc):
    state, rid = _state(sample_doc, ask_mode="ask")
    llm_module.set_llm(ReviewLLM(_only(rid, FUNC, "low")))
    state.update(requirement_reviewer.run(state))
    _answer(state, "RC-001", "비기능 / 시스템 / 성능")
    req = _req(state, rid)
    assert (req["type"], req["category"], req["status"], req["review"]) == ("NON_FUNCTIONAL", "성능", "CONFIRMED", [])
    assert req["decisions"][-1].startswith("[RC-001] 분류 기능 / 시스템 / 기능 → 비기능 / 시스템 / 성능")
    assert rid in state["dirty_requirements"], "다른 답만 다시 설계"


def test_requirement_not_in_source_is_flagged_and_can_be_excluded(sample_doc):
    state, rid = _state(sample_doc, ask_mode="ask")
    other = next(r["requirement_id"] for r in state["requirements"] if r["type"] == "FUNCTIONAL")
    _req(state, other)["description"] = "모바일 앱 푸시로 영수증 OCR 결과를 재전송한다"   # 원문과 많이 다름 → 판정 후보
    fake = ReviewLLM(_only(other, FUNC, in_source=False))
    llm_module.set_llm(fake)
    state.update(requirement_reviewer.run(state))
    assert re.search(rf"\[{other}\] 검토 대상 .*설명이 원문과 많이 다름", fake.seen["SourceReviewOut"][0])
    note = _req(state, other)["review"][0]
    assert note["kind"] == "ADDED" and note["note"] == "원문 외 추가 판단 — 원문에 없는 재발송 기능"
    q = next(q for q in state["questions"] if q["question_id"] == note["question_id"])
    assert q["options"] == [INCLUDE, EXCLUDE] and q["recommended"] == INCLUDE

    _answer(state, q["question_id"], EXCLUDE)
    req = _req(state, other)
    assert req["status"] == "EXCLUDED" and req["review"] == [] and other in state["dirty_requirements"]


def test_missing_requirement_is_supplemented_with_verified_quote(sample_doc):
    state, _ = _state(sample_doc)
    # 요청 등록 요구사항이 원문 둘째 문장(요청 항목)을 담지 않게 한다 → 그 문장만 누락 후보로 간다
    reg = next(r for r in state["requirements"] if "요청에는" in r["description"])
    reg["description"] = "사용자는 IT 서비스 요청을 등록할 수 있어야 한다."

    def missing(user):
        key = re.search(r"^## 원문 단위 \[([^\]]+)\]", user, re.M).group(1)
        bid = re.search(r"^\((B\d+)\) .*요청에는 제목과 내용", user, re.M).group(1)
        common = dict(source_key=key, type="FUNCTIONAL", scope="SYSTEM", category="기능", source_block_id=bid,
                      reason="요청 항목 요구가 빠짐")
        return [MissingRequirement(title="요청 항목 입력", description="요청에는 제목과 내용이 포함된다",
                                   quote="요청에는 제목과 내용이 포함된다", **common),
                MissingRequirement(title="지어낸 요구", description="-", quote="원문에 없는 문장이다 정말로", **common)]

    fake = ReviewLLM(missing=missing)
    llm_module.set_llm(fake)
    before = {r["requirement_id"] for r in state["requirements"]}
    update = requirement_reviewer.run(state)
    sentences = fake.seen["SourceReviewOut"][0].split("### 담기지 않은 원문 문장")[1].split("###")[0]
    assert "요청에는 제목과 내용이 포함된다" in sentences and "담당자를 배정한다" not in sentences, "담긴 문장은 보내지 않는다"
    new = [r for r in update["requirements"] if r["requirement_id"] not in before]
    assert len(new) == 1, "인용이 원문에 없는 보충은 버린다"
    req = new[0]
    assert req["evidence"][0]["verified"] and req["review"][0]["kind"] == "SUPPLEMENTED"
    assert re.fullmatch(r".+-\d{2}", req["requirement_id"]) and req["priority"] == "UNSPECIFIED"
    assert req["requirement_id"] in update["requirement_review"]["reviewed"]
    assert req["requirement_id"] in update["dirty_requirements"]
    assert "빠진 요구사항 보충 1 (인용 확인 불가로 제외 1)" in update["requirement_review"]["last"]


def test_failed_request_is_retried(sample_doc):
    state, rid = _state(sample_doc)
    llm_module.set_llm(ReviewLLM(fail=True))
    update = requirement_reviewer.run(state)
    assert rid not in update["requirement_review"]["reviewed"], "판정을 못 받은 단위는 다음에 다시 본다"
    assert update["failures"]["requirement_reviewer"] == 1


def test_function_designer_receives_review_flags(sample_doc):
    """확인 대기 요구사항: 공통 결정에서 빠지고, 요구사항 줄에 표시가 붙고, 그 기능 규칙에 '(확인 필요 RC-…)'가 붙는다."""
    state, rid = _state(sample_doc, ask_mode="ask")
    fake = ReviewLLM(_only(rid, FUNC, "low"))
    llm_module.set_llm(fake)
    state.update(requirement_reviewer.run(state))
    state.update({"functions": [], "function_common": {},
                  "next_action": {"action": "function_designer", "mode": "full", "scope": []}})
    state.update(function_designer.run(state))

    assert f"[{rid}]" not in fake.seen["CommonDecisionsOut"][0], "확정 안 된 요구사항으로 공통 결정을 만들지 않는다"
    design = next(p for p in fake.seen["FunctionDesignOut"] if f"[{rid}]" in p)
    assert f"| 검토 확인 대기 [RC-001]: 기능(추가 여부 판단 필요)" in design
    fn = next(f for f in state["functions"] if rid in f["requirement_ids"])
    assert fn["business_rules"][0].startswith("(확인 필요 RC-001) 이 기능을 둘지 확인")
    assert not any(q["question_id"].startswith("FQ-") and rid in q["requirement_ids"] for q in state["questions"]), \
        "이미 한 질문이라 기능 명세 질문으로 다시 묻지 않는다"

    _answer(state, "RC-001", "기능 / 시스템 / 기능")   # 추천과 같은 답 → 규칙 표시만 결정으로
    fn = next(f for f in state["functions"] if rid in f["requirement_ids"])
    assert fn["business_rules"][0].startswith("(결정 RC-001)") and not state["dirty_requirements"]


class ConflictLLM(FakeLLM):
    """목록의 앞 두 요구사항이 충돌한다고 답한다 (+ 없는 ID만 가리키는 지적, 함께 만족할 수 있다는 지적은 버려져야 함)."""

    def __init__(self, fail=False):
        super().__init__()
        self.fail, self.prompts = fail, []

    def _ConflictReviewOut(self, user):
        self.prompts.append(user)
        if self.fail:
            raise RuntimeError("충돌 검토 요청 실패")
        ids = re.findall(r"^- \[([^\]]+)\]", user, re.M)
        return ConflictReviewOut(conflicts=[
            RequirementConflict(requirement_ids=ids[:2], title="이력 보관 기간", conflict="보관 기간이 1년과 3년으로 다름",
                                incompatible="1년 후 삭제하면 3년 보관을 어긴다",
                                evidence="1년간 보관 / 3년간 보관", question="요청 이력 보관 기간은 몇 년인가요?",
                                options=["1년", "3년"], recommended="3년"),
            RequirementConflict(requirement_ids=ids[1:3], title="응답시간", conflict="조회와 입력의 응답시간을 따로 정함",
                                evidence="-", question="-", options=["-"], recommended="-", both_possible=True),
            RequirementConflict(requirement_ids=["GHOST-1", ids[0]], conflict="-", evidence="-", question="-",
                                options=[], recommended=""),
        ])


def _conflict_state(sample_doc, ask_mode):
    """가짜 LLM으로 끝까지 돈 상태에서 충돌 검토만 다시 하게 한다 (원문 대조는 끝)."""
    state, _ = _state(sample_doc, ask_mode)
    state["requirement_review"] = {"reviewed": [r["requirement_id"] for r in state["requirements"]]}
    return state


def test_conflict_review_runs_once_per_requirement_change(sample_doc):
    llm_module.set_llm(FakeLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}, "recursion_limit": 500}
    graph.invoke({"run_id": "t1", "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": False, "only": [], "max_sources": 0}}, config)
    state = graph.get_state(config).values
    assert state["requirement_review"]["conflict_rev"] == state["rev"]["requirements"]
    assert "충돌: 충돌 없음" in state["requirement_review"]["last"]


def test_conflict_is_flagged_for_function_designer_and_asked(sample_doc):
    state = _conflict_state(sample_doc, "ask")
    decision, _ = coordinator.decide(state)
    assert decision.action == "requirement_reviewer" and "충돌" in decision.reason

    fake = ConflictLLM()
    llm_module.set_llm(fake)
    update = requirement_reviewer.run(state)
    assert len(fake.prompts) == 1 and "## 요구사항 목록" in fake.prompts[0]
    qs = [q for q in update["questions"] if q["question_id"].startswith("CF-")]
    assert len(qs) == 1, "목록에 없는 ID만 남는 지적은 버림"
    q = qs[0]
    assert (q["kind"], q["stage"], q["blocking"], q["status"]) == ("CONFLICT", "requirements", True, "PENDING")
    assert q["options"][0] == "3년" and q["recommended"] == "3년" and len(q["requirement_ids"]) == 2
    a, b = q["requirement_ids"]
    assert _req(update, a)["review"] == [{"kind": "CONFLICT", "question_id": "CF-001",
                                          "note": f"{b}와 충돌 — 이력 보관 기간 (추천안: 3년)"}]
    assert "양립 불가: 1년 후 삭제하면 3년 보관을 어긴다" in q["context"], "자세한 내용은 질문에 둔다"
    assert "함께 만족할 수 있는 지적 1건 제외" in update["requirement_review"]["last"]
    assert not any(d.startswith("[CF-001]") for d in _req(update, a)["decisions"]), "결정이 아니라 표시로 넘긴다"
    assert set(q["requirement_ids"]) <= set(update["dirty_requirements"])

    state.update(update)
    decision, _ = coordinator.decide(state)
    assert decision.action == "ask_user" and "CF-001" in decision.scope
    _answer(state, "CF-001", "1년")
    for rid in q["requirement_ids"]:
        req = _req(state, rid)
        assert req["review"] == [] and req["decisions"][-1].endswith("→ 1년")
    assert set(q["requirement_ids"]) <= set(state["dirty_requirements"]), "추천과 다른 답이면 다시 설계"

    # 답으로 요구사항이 바뀌어 충돌 검토를 다시 하지만, 같은 요구사항 묶음은 다시 묻지 않는다
    assert coordinator.decide(state)[0].action == "requirement_reviewer"
    update = requirement_reviewer.run(state)
    assert "questions" not in update and "이미 물은 충돌 1건 제외" in update["requirement_review"]["last"]
    state.update(update)
    assert coordinator.decide(state)[0].action != "requirement_reviewer"


def test_conflict_recommended_answer_only_confirms(sample_doc):
    state = _conflict_state(sample_doc, "ask")
    llm_module.set_llm(ConflictLLM())
    state.update(requirement_reviewer.run(state))
    _answer(state, "CF-001", "3년")
    for rid in next(q for q in state["questions"] if q["question_id"] == "CF-001")["requirement_ids"]:
        req = _req(state, rid)
        assert req["review"] == [] and req["decisions"][-1].endswith("→ 3년 (추천안 확정)")
    assert not state["dirty_requirements"], "초안이 이미 추천안으로 만들어져 다시 설계하지 않는다"


def test_failed_conflict_review_is_retried(sample_doc):
    state = _conflict_state(sample_doc, "defer")
    llm_module.set_llm(ConflictLLM(fail=True))
    update = requirement_reviewer.run(state)
    assert "conflict_rev" not in update["requirement_review"]
    assert update["failures"]["requirement_reviewer"] == 1
    state.update(update)
    assert coordinator.decide(state)[0].action == "requirement_reviewer", "다음에 다시 시도"


def test_conflict_mark_on_function_is_short():
    """기능 규칙에는 충돌 제목만 붙고 추천안은 붙지 않는다 (추천안은 설계 프롬프트의 요구사항 줄로만 간다)."""
    from app.agents.roles.function_designer import _review_marks
    from app.schemas.design import FunctionSpec, Requirement, ReviewNote

    req = Requirement(requirement_id="R-1", source_key="R", source_ref="", type="FUNCTIONAL", scope="SYSTEM",
                      category="기능", title="이력 보관", description="-", priority="MUST",
                      review=[ReviewNote(kind="CONFLICT", question_id="CF-001", note="R-2와 충돌 — 이력 보관 기간 (추천안: 3년)")])
    fn = FunctionSpec(function_id="FN-1", group="G", name="이력 조회", description="-", requirement_ids=["R-1"])
    [out] = _review_marks([fn], {"R-1": req})
    assert out.business_rules == ["(확인 필요 CF-001) R-2와 충돌 — 이력 보관 기간"]
