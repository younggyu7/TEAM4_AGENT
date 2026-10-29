"""요구사항 Agent의 결정적 동작 (LLM 없이): ID·출처 연결, 배치 실패, fix 모드 재처리 범위·보존·변경 전파."""
import re

import pytest

from app.agents.llm import client as llm_module
from app.agents.roles import requirements_analyst as ra
from app.agents.roles.requirements_analyst import EvidenceOut, ExtractionOut, QuestionOut, RequirementOut
from app.agents.llm.client import LLMClient, LLMError
from app.schemas.design import BlockLocation, DocumentBlock, RequirementSource


class ScriptedLLM(LLMClient):
    """프롬프트의 source마다 요구사항 1건 + 질문 1건을 돌려준다. fail_keys에 든 source가 있는 배치는 실패한다."""
    provider = "scripted"

    def __init__(self, fail_keys=(), wrong_source=False):
        super().__init__("scripted")
        self.fail_keys, self.wrong_source = set(fail_keys), wrong_source
        self.seen: list[list[str]] = []

    def _call(self, system, user, schema, max_tokens, reduce_effort=False):
        keys = re.findall(r'<source key="([^"]+)"', user)
        self.seen.append(keys)
        if self.fail_keys & set(keys):
            raise LLMError("응답 형식 오류")
        reqs, questions = [], []
        for i, key in enumerate(keys, 1):
            bid = re.search(rf'<source key="{re.escape(key)}"[^>]*>\n<block id="(B\d+)">\n(.*?)\n</block>', user, re.S)
            claimed = "없는-키" if self.wrong_source else key
            reqs.append(RequirementOut(key=f"R{i}", source_key=claimed, type="FUNCTIONAL", scope="SYSTEM",
                                       category="기능", title=f"{key} 기능", description=bid.group(2), priority="MUST",
                                       acceptance_criteria=["확인"], status="CONFIRMED",
                                       evidence=[EvidenceOut(source_block_id=bid.group(1), quote=bid.group(2))]))
            questions.append(QuestionOut(kind="AMBIGUITY", question=f"{key} 범위?", context="", options=["a", "b"],
                                         requirement_keys=[f"R{i}"], evidence_block_ids=[bid.group(1)], blocking=False))
        return ExtractionOut(requirements=reqs, questions=questions), 10, 5


def _state(n: int = 5) -> dict:
    blocks, sources = [], []
    for i in range(1, n + 1):
        bid, key = f"B{i:04d}", f"SFR-{i:03d}"
        blocks.append(DocumentBlock(document_id="d", document_version="v", source_block_id=bid,
                                    block_type="requirement_table", location=BlockLocation(order=i),
                                    text=f"사용자는 기능 {i}을 사용할 수 있어야 함", fields={"code": key}))
        sources.append(RequirementSource(source_key=key, code=key, name=f"기능 {i}", block_ids=[bid]))
    return {"blocks": [b.model_dump() for b in blocks], "sources": [s.model_dump() for s in sources],
            "next_action": {"mode": "full"}}


@pytest.fixture
def use_llm():
    def install(client):
        llm_module.set_llm(client)
        return client
    yield install
    llm_module.set_llm(None)


def test_ids_are_unique_and_linked_to_source(use_llm):
    use_llm(ScriptedLLM())
    out = ra.run(_state())
    ids = [r["requirement_id"] for r in out["requirements"]]
    assert len(ids) == len(set(ids)) == 5
    assert all(r["requirement_id"].startswith(r["source_key"]) and r["source_ref"] == r["source_key"]
               for r in out["requirements"])
    assert all(e["verified"] for r in out["requirements"] for e in r["evidence"])
    assert [q["question_id"] for q in out["questions"]] == [f"Q-{i:03d}" for i in range(1, 6)]
    assert all(q["requirement_ids"] for q in out["questions"]), "질문은 같은 응답의 요구사항 ID로 연결"


def test_unknown_source_key_follows_evidence_block(use_llm):
    """LLM이 틀린 source_key를 써도 근거 블록이 속한 추출 단위로 연결한다 (배치 첫 단위로 몰지 않음)."""
    use_llm(ScriptedLLM(wrong_source=True))
    out = ra.run(_state())
    assert sorted(r["source_key"] for r in out["requirements"]) == [f"SFR-{i:03d}" for i in range(1, 6)]


def test_failed_batch_keeps_other_batches_and_records_failure(use_llm):
    use_llm(ScriptedLLM(fail_keys={"SFR-004"}))      # 배치 크기 3: [1,2,3] 성공, [4,5] 실패
    out = ra.run(_state())
    assert {r["source_key"] for r in out["requirements"]} == {"SFR-001", "SFR-002", "SFR-003"}
    assert out["failures"]["requirements_analyst"] == 1


def test_fix_mode_reprocesses_only_scope_and_keeps_the_rest(use_llm):
    first = use_llm(ScriptedLLM(fail_keys={"SFR-004"}))
    state = _state()
    state.update(ra.run(state))
    before = {r["requirement_id"]: r for r in state["requirements"]}
    questions_before = list(state["questions"])

    again = use_llm(ScriptedLLM())
    state["next_action"] = {"mode": "fix", "scope": ["SFR-004", "SFR-005"]}
    out = ra.run(state)
    assert again.seen == [["SFR-004", "SFR-005"]], "재처리 대상만 다시 보낸다"
    after = {r["requirement_id"]: r for r in out["requirements"]}
    assert all(after[k] == v for k, v in before.items()), "대상이 아닌 결과는 그대로"
    assert set(out["dirty_requirements"]) == {"SFR-004-01", "SFR-005-01"}, "다시 추출한 요구사항을 변경 목록에"
    assert out["questions"][:len(questions_before)] == questions_before
    assert first.seen[-1] == ["SFR-004", "SFR-005"], "실패한 배치는 한 번 재시도한다"


def test_fix_mode_replaces_questions_of_reextracted_sources(use_llm):
    """같은 단위를 다시 추출하면 그 단위의 대기 중 질문은 새 질문으로 바뀐다 (같은 질문이 두 번 쌓이지 않음)."""
    use_llm(ScriptedLLM())
    state = _state(3)
    state.update(ra.run(state))
    state["next_action"] = {"mode": "fix", "scope": ["SFR-002"]}
    out = ra.run(state)
    about = [q["question"] for q in out["questions"]]
    assert about.count("SFR-002 범위?") == 1
    ids = [q["question_id"] for q in out["questions"]]
    assert len(ids) == len(set(ids)), "질문 ID 중복 없음"


def test_priority_follows_source_acceptance_level_or_stays_unspecified(use_llm):
    """응락 수준이 있으면 그 값, 없으면 인용문에 '필수' 같은 근거가 있을 때만 LLM 우선순위를 인정한다."""
    use_llm(ScriptedLLM())          # 언제나 MUST라고 답한다
    state = _state(2)
    state["blocks"][0]["fields"]["acceptance"] = "권장"
    out = ra.run(state)
    priority = {r["source_key"]: r["priority"] for r in out["requirements"]}
    assert priority == {"SFR-001": "SHOULD", "SFR-002": "UNSPECIFIED"}
