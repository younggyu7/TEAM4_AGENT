"""문서분석 Agent: 문서 구조·표 해석, 추출 품질 판단, 원문 근거 재검색.

baseline은 LLM 없이 동작한다 (파서·품질 규칙·키워드 검색 도구 사용).
- mode=full     : 문서 파싱 → 요구사항 추출 단위 구성 → 문서 품질 경고
- mode=research : 근거가 확인되지 않은 요구사항의 원문 위치를 다시 찾아 연결
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.schemas.design import DocumentBlock, Requirement
from app.agents.flow.state import AgentState, dump, issues_of, load, reconcile_issues
from app.agents.tools.doc_quality import build_sources, check_document
from app.agents.tools.document_parser import document_codes, parse_document, textless_pages
from app.agents.tools.search import best_line, quote_conflict, quote_in_text, search_blocks

log = logging.getLogger(__name__)
RESEARCH_MIN_SCORE = 0.6


def run(state: AgentState) -> dict:
    """문서분석 Agent 노드: 모드에 따라 문서 분석(full) 또는 원문 근거 재검색(research)을 한다."""
    action = state.get("next_action") or {}
    if action.get("mode") == "research":
        return _research(state, set(action.get("scope") or []))
    return _analyze(state)


def _analyze(state: AgentState) -> dict:
    """문서를 파싱해 추출 단위를 만들고, 문서 품질 경고를 이슈로 갱신한다."""
    options = state.get("options") or {}
    blocks = parse_document(state["source_path"])
    sources = build_sources(blocks, include_narrative=bool(options.get("include_narrative")))
    findings = check_document(blocks, sources, document_codes(state["source_path"]),   # 필터 전 전체 문서 기준
                              textless_pages(state["source_path"]))
    only = [o.strip().upper() for o in options.get("only") or [] if o.strip()]
    if only:
        sources = [s for s in sources if any(s.source_key.upper().startswith(o) for o in only)]
    if options.get("max_sources"):
        sources = sources[: int(options["max_sources"])]
    issues = reconcile_issues(issues_of(state), findings, lambda k: k.startswith("DOC_"),
                              get_settings().max_fix_attempts)
    tables = sum(b.block_type == "requirement_table" for b in blocks)
    log.info("[문서분석] 블록 %d개 (요구사항 표 %d) → 추출 단위 %d개, 문서 경고 %d건",
             len(blocks), tables, len(sources), len(findings))
    return {"blocks": dump(blocks), "sources": dump(sources), "issues": [i.model_dump() for i in issues]}


def _research(state: AgentState, requirement_ids: set[str]) -> dict:
    """근거 인용문이 원문에서 확인되지 않은 요구사항의 근거를 다시 찾는다."""
    blocks = load(state, "blocks", DocumentBlock)
    block_map = {b.source_block_id: b for b in blocks}
    requirements = load(state, "requirements", Requirement)
    fixed = refused = 0
    for req in requirements:
        if req.requirement_id not in requirement_ids:
            continue
        new_evidence = []
        for ev in req.evidence:
            block = block_map.get(ev.source_block_id)
            if block and quote_in_text(ev.quote, block.text):
                new_evidence.append(ev.model_copy(update={"verified": True}))
                continue
            query = ev.quote or f"{req.title} {req.description}"
            prefer = {ev.source_block_id} if block else None
            candidates = search_blocks(blocks, query, top_k=3, prefer_ids=prefer)
            # 같은 문장이 다른 블록에 그대로 있으면 인용문은 두고 블록만 바로잡는다
            exact = next((b for _, b in candidates if ev.quote and quote_in_text(ev.quote, b.text)), None)
            if exact:
                new_evidence.append(ev.model_copy(update={"source_block_id": exact.source_block_id, "verified": True}))
                fixed += 1
                continue
            best = max(((best_line(b, query), b) for _, b in candidates), key=lambda x: x[0][0], default=None)
            if best and best[0][0] >= RESEARCH_MIN_SCORE:
                (score, line), b = best
                # 비슷한 줄이라도 숫자·조건·대상이 다르면 인용문을 바꿔 '확인'으로 만들지 않는다 (⚠미확인 유지)
                if ev.quote and (conflict := quote_conflict(ev.quote, line)):
                    log.info("[문서분석] 근거 재연결 거부 (%s): '%s' ≠ %s '%s'", conflict, ev.quote[:40],
                             b.source_block_id, line[:40])
                    new_evidence.append(ev)
                    refused += 1
                    continue
                new_evidence.append(ev.model_copy(update={"source_block_id": b.source_block_id, "quote": line,
                                                          "verified": True}))
                fixed += 1
            else:
                new_evidence.append(ev)
        req.evidence = new_evidence
    log.info("[문서분석] 근거 재검색: 대상 %d건 중 %d건 재연결, %d건은 내용이 달라 미확인 유지",
             len(requirement_ids), fixed, refused)
    # 근거 연결 변경은 설계 내용 변경이 아니므로 rev는 올리지 않는다
    return {"requirements": dump(requirements)}
