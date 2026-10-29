"""추적 검토 Agent: 요구사항 ↔ 기능 ↔ 데이터 모델 추적 관계를 만들고 연결 누락을 이슈로 보고한다.

결정적 규칙으로 '연결이 있는지'만 본다. 분류(기능/비기능)가 맞는지, ERD 설계가 좋은지는 검토하지 않는다
(ERD 구조 검사는 tools/model_validator.py).
- 확정되지 않은 요구사항에만 기댄 기능은 보고만 한다 (고객 답에 따라 바뀔 수 있음).
- 이전 버전 대비 변경과 영향(바뀐 요구사항 → 기능 → 엔티티, 데이터 설계 때 다시 설계할 엔티티)은
  산출물을 쓸 때 추적표 4절로 만든다 (tools/trace_diff.py).
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.schemas.design import FunctionSpec, Requirement, TraceLink
from app.agents.flow.state import AgentState, basis, dump, finding, get_data_model, issues_of, load, reconcile_issues, rev

log = logging.getLogger(__name__)


def build_trace_links(requirements: list[Requirement], functions: list[FunctionSpec], model) -> list[TraceLink]:
    """기능 명세·데이터 모델에서 추적 링크(요구사항→기능, 비기능→기능, 기능→엔티티)를 만든다."""
    links = []
    for f in functions:
        links += [TraceLink(from_id=r, to_id=f.function_id, link_type="REQ_FUNC") for r in f.requirement_ids]
        links += [TraceLink(from_id=n, to_id=f.function_id, link_type="NFR_FUNC") for n in f.nfr_ids]
    if model:
        for e in model.entities:
            links += [TraceLink(from_id=fid, to_id=e.entity_id, link_type="FUNC_ENTITY") for fid in e.function_ids]
    return links


def run(state: AgentState) -> dict:
    """추적 검토 Agent 노드: 추적 관계를 만들고 누락(기능 없는 요구사항, 엔티티 없는 기능 등)을 이슈로 보고한다."""
    requirements = load(state, "requirements", Requirement)
    functions = load(state, "functions", FunctionSpec)
    model = get_data_model(state)
    links = build_trace_links(requirements, functions, model)
    by = "trace_reviewer"

    findings = []
    covered = {l.from_id for l in links if l.link_type == "REQ_FUNC"}
    for r in requirements:
        if r.designed and r.requirement_id not in covered:
            findings.append(finding("TRACE_REQ_WITHOUT_FUNCTION", "ERROR",
                                    f"{r.requirement_id} '{r.title}'에 연결된 기능이 없음",
                                    "function_designer", r.requirement_id, by))
    if model:
        mapped = {l.from_id for l in links if l.link_type == "FUNC_ENTITY"}
        for f in functions:
            if f.data_objects and f.function_id not in mapped:
                findings.append(finding("TRACE_FUNC_WITHOUT_ENTITY", "ERROR",
                                        f"{f.function_id} '{f.name}'의 데이터({', '.join(f.data_objects)})를 저장할 엔티티가 없음",
                                        "data_modeler", f.function_id, by))
        for e in model.entities:
            if not e.function_ids:
                findings.append(finding("TRACE_ENTITY_WITHOUT_FUNCTION", "INFO",
                                        f"엔티티 {e.entity_id}를 사용하는 기능이 연결되지 않음", target_id=e.entity_id,
                                        raised_by=by))
    # 확정되지 않은 요구사항(가정한 해석·결정 필요)에만 기댄 기능: 고객 답에 따라 기능·엔티티가 바뀔 수 있다 (보고만)
    unconfirmed = {r.requirement_id for r in requirements if r.status in ("ASSUMED", "OPEN")}
    leaning = [f for f in functions if f.requirement_ids and set(f.requirement_ids) <= unconfirmed]
    if leaning:
        findings.append(finding("TRACE_FUNC_ON_UNCONFIRMED", "INFO",
                                f"확정되지 않은 요구사항에만 기댄 기능 {len(leaning)}개 → 고객 확인에 따라 바뀔 수 있음: "
                                + ", ".join(f"{f.function_id}({', '.join(f.requirement_ids)})" for f in leaning[:10])
                                + (" …" if len(leaning) > 10 else ""), raised_by=by))
    linked_nfr = {l.from_id for l in links if l.link_type == "NFR_FUNC"}
    unlinked = [r.requirement_id for r in requirements
                if r.type == "NON_FUNCTIONAL" and r.scope == "SYSTEM" and r.requirement_id not in linked_nfr]
    if unlinked:
        findings.append(finding("TRACE_NFR_UNLINKED", "INFO",
                                f"기능에 직접 연결되지 않은 시스템 비기능 요구사항 {len(unlinked)}건 → 설계 고려사항·검증 계획에서 다룸: "
                                + ", ".join(unlinked[:15]) + (" …" if len(unlinked) > 15 else ""),
                                raised_by=by))

    issues = reconcile_issues(issues_of(state), findings, lambda k: k.startswith("TRACE_"),
                              get_settings().max_fix_attempts)
    errors = sum(f.severity == "ERROR" for f in findings)
    log.info("[검토] 추적 링크 %d개, 검토 이슈 %d건 (오류 %d)", len(links), len(findings), errors)
    r = rev(state)
    return {
        "trace_links": dump(links),
        "issues": [i.model_dump() for i in issues],
        "basis": {**basis(state), "review": [r["functions"], r["model"]]},
    }
