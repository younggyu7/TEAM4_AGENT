"""요구사항 Agent: 요구사항 추출·기능/비기능 분류·근거 연결·질문 후보 제안.

- mode=full     : 모든 추출 단위(상세 표/청크)를 배치로 나눠 병렬 추출
- mode=fix      : 지정한 추출 단위만 다시 추출 (결과가 비었거나 실패한 경우)
- mode=redesign : 요구사항 단계 다시 만들기(rerun requirements_analyst --feedback). 다시 추출하지 않고 요청 1회로
  의견이 바꾸는 요구사항(결정으로 남김), 범위에서 빼는 요구사항(EXCLUDED), 새로 생기는 요구사항(USR-…)만 받는다.
  원문 근거·ID는 그대로 두고, 바뀐 요구사항은 다음 기능 명세 단계에서 그 그룹만 다시 설계한다 (dirty_requirements).
"""
from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.prompts import load_prompt
from app.core.config import get_settings
from app.agents.llm.client import get_llm, run_parallel
from app.schemas.document import DocumentBlock, RequirementSource
from app.schemas.requirement import (Evidence, Priority, Question, Requirement, RequirementCategory, RequirementScope, RequirementType, USER_SOURCE)
from app.agents.flow.state import AgentState, dump, load, merge_usage, rev
from app.agents.tools.search import quote_in_text

log = logging.getLogger(__name__)

SYSTEM_PROMPT = load_prompt("requirements_analyst/system")

PREFIX_HINT = ("요구사항 번호 접두어 관례(참고용, 원문 표의 '분류'가 우선): SFR=기능, PER=성능, SIR=인터페이스, DAR=데이터, "
               "SER=보안, QUR=품질, COR=제약, ECR=장비구성, TER=테스트, PMR=사업관리, PSR=사업지원")


# LLM 응답 스키마: 근거 (블록 ID + 원문 인용)
class EvidenceOut(BaseModel):
    source_block_id: str = Field(description="근거 블록 ID (제공된 블록 중 하나)")
    quote: str = Field(description="블록 원문에서 그대로 복사한 연속 구간 15~120자")


# LLM 응답 스키마: 요구사항 하나 (key는 응답 안의 임시 ID, 최종 ID는 코드에서 부여)
class RequirementOut(BaseModel):
    key: str
    source_key: str
    type: RequirementType
    scope: RequirementScope
    category: RequirementCategory
    title: str = Field(description="30자 이내 요구사항 명")
    description: str
    priority: Priority
    acceptance_criteria: list[str]
    evidence: list[EvidenceOut]
    status: Literal["CONFIRMED", "ASSUMED", "OPEN"]


# LLM 응답 스키마: 확인 질문 후보
class QuestionOut(BaseModel):
    kind: Literal["AMBIGUITY", "MISSING", "CONFLICT"]
    question: str
    context: str = Field(description="왜 확인이 필요한지, 원문 근거 요약")
    options: list[str]
    requirement_keys: list[str]
    evidence_block_ids: list[str]
    blocking: bool


# LLM 응답 스키마: 요구사항 추출 결과 전체
class ExtractionOut(BaseModel):
    requirements: list[RequirementOut]
    questions: list[QuestionOut]


# LLM 응답 스키마: 의견 반영 (요구사항 단계 다시 만들기). 의견과 관계없는 요구사항은 넣지 않는다
class RequirementChangeOut(BaseModel):
    requirement_id: str
    decision: str = Field(description="의견으로 정해진 내용 한 문장 (이 요구사항의 결정으로 남는다)")


class ExcludedOut(BaseModel):
    requirement_id: str
    reason: str


class NewRequirementOut(BaseModel):
    type: RequirementType
    category: RequirementCategory
    title: str = Field(description="30자 이내 요구사항 명")
    description: str
    acceptance_criteria: list[str]


class RequirementFeedbackOut(BaseModel):
    changes: list[RequirementChangeOut] = Field(default_factory=list,
                                                description="의견으로 내용·조건이 바뀌는 기존 요구사항만 (없으면 생략)")
    excluded: list[ExcludedOut] = Field(default_factory=list, description="의견이 이번 범위에서 빼라고 한 요구사항만")
    added: list[NewRequirementOut] = Field(default_factory=list,
                                           description="의견이 원문에 없는 기능·조건을 새로 요구할 때만")


FEEDBACK_REQUEST = """## 이번 요청: 사용자 의견 반영
요구사항을 다시 추출하지 않는다. 위 요구사항 목록에 사용자 의견을 반영할 때 바뀌는 것만 낸다.
- changes: 의견으로 내용·조건이 바뀌는 기존 요구사항만, 바뀐 내용을 결정 한 문장으로 쓴다.
- excluded: 의견이 이번 범위에서 빼라고 한 요구사항만 (이유 포함). 빼라고 하지 않은 요구사항은 넣지 않는다.
- added: 의견이 원문에 없는 기능·조건을 새로 요구할 때만. 기존 요구사항을 고쳐 반영할 수 있으면 changes로 한다.
- 의견과 관계없는 요구사항은 어디에도 넣지 않는다. 의견이 모호하면 가장 좁게 해석한다. 목록에 있는 ID만 쓴다."""


def run(state: AgentState) -> dict:
    """요구사항 Agent 노드: 추출 단위를 배치로 나눠 병렬 추출하고 요구사항 ID·질문 ID를 부여한다."""
    s = get_settings()
    action = state.get("next_action") or {}
    if action.get("mode") == "redesign":
        return _apply_feedback(state)
    blocks = {b.source_block_id: b for b in load(state, "blocks", DocumentBlock)}
    sources = load(state, "sources", RequirementSource)
    fix_scope = set(action.get("scope") or []) if action.get("mode") == "fix" else None
    targets = [src for src in sources if fix_scope is None or src.source_key in fix_scope]
    # 문서 이름: --title이 있으면 그것, 없으면 첫 블록 첫 줄 (정제본은 표지가 없어 '다. 요구사항별 상세 내용' 같은 줄이 됨)
    title = (state.get("options") or {}).get("title") \
        or next((b.text.splitlines()[0][:80] for b in blocks.values() if b.text.strip()), "")
    batches = [targets[i:i + s.req_batch_size] for i in range(0, len(targets), s.req_batch_size)]
    llm = get_llm()
    results = run_parallel(lambda batch: _extract(llm, batch, blocks, title), batches)

    target_keys = {src.source_key for src in targets}
    previous = load(state, "requirements", Requirement)
    kept = [r for r in previous if fix_scope is not None and r.source_key not in target_keys]
    questions = load(state, "questions", Question)
    if fix_scope is None:
        questions = []
    else:   # 다시 추출하는 단위의 대기 중 질문은 새 응답의 질문으로 바뀐다 (같은 질문이 두 번 쌓이지 않게)
        replaced = {r.requirement_id for r in previous if r.source_key in target_keys}
        questions = [q for q in questions if not (q.status == "PENDING" and q.requirement_ids
                                                  and set(q.requirement_ids) <= replaced)]
    next_q = max((int(q.question_id.split("-")[-1]) for q in questions), default=0) + 1
    new_reqs: list[Requirement] = []
    failed = 0
    for batch, out, err in results:
        if err or out is None:
            failed += 1
            continue
        reqs, key_to_id = _to_requirements(out, batch, blocks)
        new_reqs.extend(reqs)
        for q in out.questions:
            questions.append(Question(
                question_id=f"Q-{next_q:03d}", kind=q.kind, question=q.question, context=q.context,
                options=q.options, blocking=q.blocking,
                requirement_ids=[key_to_id[k] for k in q.requirement_keys if k in key_to_id],
                evidence_block_ids=[b for b in q.evidence_block_ids if b in blocks],
            ))
            next_q += 1

    order = {src.source_key: i for i, src in enumerate(sources)}
    requirements = sorted(kept + new_reqs, key=lambda r: (order.get(r.source_key, 999), r.requirement_id))
    counts = {t: sum(r.type == t for r in new_reqs) for t in ("FUNCTIONAL", "NON_FUNCTIONAL")}
    log.info("[요구사항] 추출 단위 %d개 → 요구사항 %d건 (기능 %d / 비기능 %d), 질문 후보 %d건, 실패 배치 %d",
             len(targets), len(new_reqs), counts["FUNCTIONAL"], counts["NON_FUNCTIONAL"],
             len(questions), failed)
    r = rev(state)
    update = {
        "requirements": dump(requirements),
        "questions": dump(questions),
        "rev": {**r, "requirements": r["requirements"] + 1},
        "llm_usage": merge_usage(state, llm.pop_usage()),
    }
    if fix_scope is not None:
        update["dirty_requirements"] = sorted(set(state.get("dirty_requirements") or [])
                                              | {x.requirement_id for x in new_reqs})
    if failed:
        update["failures"] = {**(state.get("failures") or {}), "requirements_analyst":
                              (state.get("failures") or {}).get("requirements_analyst", 0) + 1}
    return update


def _feedback(state: AgentState) -> str:
    """사용자 의견(rerun requirements_analyst --feedback) 원문."""
    return ((state.get("options") or {}).get("feedback") or {}).get("requirements_analyst", "").strip()


def _apply_feedback(state: AgentState) -> dict:
    """요구사항 단계 다시 만들기: 요청 1회로 의견이 바꾸는·빼는·새로 만드는 요구사항만 받아 반영한다.

    바뀐 요구사항은 결정 '[의견] …'을 남기고 확정한다 (기능 명세는 결정을 반드시 반영한다).
    뺀 요구사항은 EXCLUDED로 두고(기록은 남김) 기능·데이터 설계 대상에서 빠진다.
    새 요구사항은 원문 근거 없이 USR-01 …으로 만든다 (근거는 결정에 남긴 의견).
    """
    feedback = _feedback(state)
    requirements = load(state, "requirements", Requirement)
    sources = {s.source_key: s.name or s.heading for s in load(state, "sources", RequirementSource)}
    llm = get_llm()
    lines = ["## 현재 요구사항 (원문 단위별)"]
    for key in dict.fromkeys(r.source_key for r in requirements):
        lines.append(f"### [{key}] {sources.get(key, '사용자 의견' if key == USER_SOURCE else key)}")
        for r in (x for x in requirements if x.source_key == key):
            kind = "기능" if r.type == "FUNCTIONAL" else "비기능"
            line = f"- [{r.requirement_id}] ({kind}·{r.scope}·{r.status}) {r.title}: {r.description}"
            lines.append(line + "".join(f" | 결정: {d}" for d in r.decisions))
    user = "\n".join([*lines, "", "## 사용자 의견 (반드시 반영)", feedback, "", FEEDBACK_REQUEST])
    out = llm.structured(system=SYSTEM_PROMPT, user=user, schema=RequirementFeedbackOut, purpose="요구사항 의견 반영")

    by_id = {r.requirement_id: r for r in requirements}
    changed: set[str] = set()
    for c in out.changes:
        r = by_id.get(c.requirement_id.strip())
        if r and c.decision.strip() and r.status != "EXCLUDED":
            r.decisions.append(f"[의견] {c.decision.strip()}")
            r.status = "CONFIRMED"
            changed.add(r.requirement_id)
    excluded = set()
    for x in out.excluded:
        r = by_id.get(x.requirement_id.strip())
        if r and r.status != "EXCLUDED":
            r.decisions.append(f"[의견] 이번 범위에서 제외: {x.reason.strip()}")
            r.status = "EXCLUDED"
            excluded.add(r.requirement_id)
    number = max((int(r.requirement_id.split("-")[-1]) for r in requirements if r.source_key == USER_SOURCE), default=0)
    added = []
    for a in out.added:
        number += 1
        added.append(Requirement(
            requirement_id=f"USR-{number:02d}", source_key=USER_SOURCE, source_ref="사용자 의견", type=a.type,
            scope="SYSTEM", category=a.category, title=a.title, description=a.description, priority="MUST",
            acceptance_criteria=a.acceptance_criteria, evidence=[], status="CONFIRMED",
            decisions=[f"[의견] 사용자 의견으로 추가: {feedback[:200]}"]))
    requirements += added
    log.info("[요구사항] 의견 반영 → 바뀜 %d · 제외 %d · 추가 %d", len(changed), len(excluded), len(added))
    r = rev(state)
    dirty = changed | excluded | {a.requirement_id for a in added}
    return {
        "requirements": dump(requirements),
        "rev": {**r, "requirements": r["requirements"] + 1},
        "dirty_requirements": sorted(set(state.get("dirty_requirements") or []) | dirty),
        "requirement_targets": None,
        "requirement_rework": {"changed": sorted(changed), "excluded": sorted(excluded),
                               "added": [a.requirement_id for a in added]},
        "llm_usage": merge_usage(state, llm.pop_usage()),
    }


def _extract(llm, batch: list[RequirementSource], blocks: dict[str, DocumentBlock], title: str) -> ExtractionOut:
    """배치 하나(원문 단위 여러 개)의 프롬프트를 만들어 LLM에 추출을 요청한다."""
    parts = [f"문서: {title}", PREFIX_HINT, "", "추출 대상 원문:"]
    for src in batch:
        parts.append(f'<source key="{src.source_key}" heading="{src.heading}">')
        for bid in src.block_ids:
            parts.append(f'<block id="{bid}">\n{blocks[bid].text}\n</block>')
        parts.append("</source>")
    return llm.structured(system=SYSTEM_PROMPT, user="\n".join(parts), schema=ExtractionOut,
                          purpose=f"요구사항 추출 {','.join(s.source_key for s in batch)}")


def _to_requirements(out: ExtractionOut, batch: list[RequirementSource],
                     blocks: dict[str, DocumentBlock]) -> tuple[list[Requirement], dict[str, str]]:
    """LLM 응답을 Requirement로 바꾼다. ID를 부여하고 인용문이 원문에 있는지 확인한다.

    추출 단위는 응답의 source_key로 정한다. 배치에 없는 키를 쓰면 근거 블록이 속한 단위로 연결한다
    (배치 첫 단위로 몰면 추적표가 틀린 원문을 가리킨다). 우선순위는 원문 응락 수준으로 다시 맞춘다.
    """
    by_key = {src.source_key: src for src in batch}
    block_source = {bid: src for src in batch for bid in src.block_ids}
    batch_blocks = set(block_source)
    seq: dict[str, int] = {}
    reqs, key_to_id = [], {}
    for item in out.requirements:
        src = by_key.get(item.source_key) \
            or next((block_source[e.source_block_id] for e in item.evidence if e.source_block_id in block_source), None)
        if src is None:
            log.warning("[요구사항] 추출 단위를 알 수 없는 응답 '%s' (source_key=%s) → %s에 연결",
                        item.title, item.source_key, batch[0].source_key)
            src = batch[0]
        seq[src.source_key] = seq.get(src.source_key, 0) + 1
        rid = f"{src.source_key}-{seq[src.source_key]:02d}"
        key_to_id[item.key] = rid
        evidence = []
        for ev in item.evidence:
            block = blocks.get(ev.source_block_id) if ev.source_block_id in batch_blocks else None
            evidence.append(Evidence(source_block_id=ev.source_block_id, quote=ev.quote,
                                     verified=bool(block and quote_in_text(ev.quote, block.text))))
        reqs.append(Requirement(
            requirement_id=rid, source_key=src.source_key, source_ref=src.code, type=item.type, scope=item.scope,
            category=item.category, title=item.title, description=item.description,
            priority=_priority(item, src, blocks), acceptance_criteria=item.acceptance_criteria, evidence=evidence,
            status=item.status,
        ))
    return reqs, key_to_id


# 원문 응락 수준 → 우선순위, 그리고 응락 수준이 없을 때 우선순위를 인정할 문장 표현
ACCEPTANCE_PRIORITY = {"필수": "MUST", "권장": "SHOULD", "선택": "COULD"}
PRIORITY_WORDS = {"MUST": re.compile(r"필수|반드시"), "SHOULD": re.compile(r"권장|바람직"),
                  "COULD": re.compile(r"선택|필요\s*시|필요한\s*경우")}


def _priority(item: RequirementOut, src: RequirementSource, blocks: dict[str, DocumentBlock]) -> str:
    """우선순위: 원문 표의 응락 수준이 있으면 그 값(코드가 결정). 없으면 LLM 값을 쓰되,
    인용문에 그 우선순위를 뒷받침하는 표현('필수', '권장' 등)이 없으면 UNSPECIFIED로 둔다 (MUST 단정 방지)."""
    level = next((blocks[b].fields.get("acceptance", "").strip() for b in src.block_ids
                  if b in blocks and blocks[b].fields.get("acceptance", "").strip()), "")
    for word, priority in ACCEPTANCE_PRIORITY.items():
        if word in level:
            return priority
    if item.priority == "UNSPECIFIED":
        return item.priority
    quotes = " ".join(e.quote for e in item.evidence)
    return item.priority if PRIORITY_WORDS[item.priority].search(quotes) else "UNSPECIFIED"
