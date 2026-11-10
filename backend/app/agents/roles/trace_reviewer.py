"""추적 검토 Agent: 요구사항 ↔ 기능 ↔ 데이터 모델 추적 관계(추적표)를 만들고, 기능 명세 → ERD 연결 누락을 이슈로 보고한다.

결정적 규칙으로 '연결이 있는지'만 본다. 분류(기능/비기능)가 맞는지, ERD 설계가 좋은지는 검토하지 않는다
(ERD 구조 검사는 tools/model_validator.py). 요구사항 → 기능 연결 누락은 요구사항 검토 Agent(coverage_findings)가 본다.
- 확정되지 않은 요구사항에만 기댄 기능은 보고만 한다 (고객 답에 따라 바뀔 수 있음).
- 기능 규칙이 상태 값을 정하는데('…상태는 A, B, C 중 하나로 관리한다') 연결된 테이블에 그 값을 가진 상태 컬럼이 없으면 경고한다.
- 이전 버전 대비 변경과 영향(바뀐 요구사항 → 기능 → 엔티티, 데이터 설계 때 다시 설계할 엔티티)은
  산출물을 쓸 때 추적표 4절로 만든다 (tools/trace_diff.py).
"""
from __future__ import annotations

import logging
import re

from app.core.config import get_settings
from app.schemas.function import FunctionSpec
from app.schemas.requirement import Requirement
from app.schemas.review import TraceLink
from app.agents.flow.state import AgentState, basis, dump, finding, get_data_model, issues_of, load, reconcile_issues, rev

log = logging.getLogger(__name__)
# '전표 처리 상태는 입력, 승인, 출력 중 하나로 관리한다', '결재 진행상태는 상신, 결재 중, 승인 4개 값으로 관리한다'
STATE_RULE = re.compile(r"상태\s*(?:값)?(?:는|은|를|가)?\s+(?P<values>[^.]+?)\s*(?:\d+\s*개\s*(?:값|상태)?\s*)?(?:중\s*하나|(?:으)?로\s*(?:관리|표시|구분))")
VALUE_SPLIT = re.compile(r"\s*(?:,|·|/|또는)\s*")


def stated_states(rule: str) -> list[str]:
    """규칙이 정한 상태 값 목록 (둘 이상일 때만)."""
    found = STATE_RULE.search(re.sub(r"^\([^)]*\)\s*", "", rule))
    if not found:
        return []
    values = [v.strip() for v in VALUE_SPLIT.split(found.group("values")) if 0 < len(v.strip()) <= 12]
    return values if len(values) >= 2 else []


def _holds(a, values: list[str]) -> bool:
    """이 컬럼이 규칙의 상태를 담는가: 값을 모두 갖거나, 상태 컬럼이면서 절반 이상 갖는다 (값 하나가 우연히 겹치는 분류 컬럼은 아니다)."""
    shared = set(a.allowed_values) & set(values)
    named = re.search(r"status|state|stage", a.name) or "상태" in (a.label or "")
    return shared == set(values) or bool(named) and len(shared) >= max(2, (len(values) + 1) // 2)


def state_gaps(functions: list[FunctionSpec], model) -> list[tuple[FunctionSpec, list[str], list[str]]]:
    """(기능, 규칙이 정한 상태 값, 연결된 테이블): 연결된 테이블에 이 상태를 담는 컬럼이 없는 것. 이력 테이블은 상태를 담는 곳이 아니라 보지 않는다."""
    out = []
    for f in functions:
        linked = [e for e in model.entities if f.function_id in e.function_ids]
        holders = [e for e in linked if e.kind != "HISTORY"]
        for rule in f.business_rules:
            values = stated_states(rule)
            if values and linked and not any(_holds(a, values) for e in holders for a in e.attributes):
                out.append((f, values, [e.entity_id for e in linked]))
    return out


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


HUMAN_CONFIRMED = "사람이 확인함"


def run(state: AgentState) -> dict:
    """추적 검토 Agent 노드: 추적 관계를 만들고 기능 명세 → ERD 누락(엔티티 없는 기능, 상태 컬럼 없음 등)을 이슈로 보고한다."""
    requirements = load(state, "requirements", Requirement)
    functions = load(state, "functions", FunctionSpec)
    model = get_data_model(state)
    links = build_trace_links(requirements, functions, model)
    by = "trace_reviewer"

    findings = []
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
        for f, values, tables in state_gaps(functions, model):   # 상태 흐름이 데이터 모델에 없으면 상태 전환표·화면 처리도 만들 수 없다
            findings.append(finding("TRACE_STATE_COLUMN_MISSING", "WARNING",
                                    f"{f.function_id} '{f.name}'의 규칙이 상태 값({', '.join(values)})을 정하는데 "
                                    f"연결된 테이블({', '.join(tables)})에 이 값을 가진 상태 컬럼이 없음",
                                    target_id=f.function_id, raised_by=by))
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

    # 사람이 ERD 화면에서 확인한 지적은 다시 검사해도 열지 않는다 (처리는 rerun.DISPOSITIONS 'confirmed')
    issues = reconcile_issues(issues_of(state), findings, lambda k: k.startswith("TRACE_"), get_settings().fix_attempts,
                              keep=lambda i: i.status == "RESOLVED" and i.resolution.startswith(HUMAN_CONFIRMED))
    errors = sum(f.severity == "ERROR" for f in findings)
    log.info("[검토] 추적 링크 %d개, 검토 이슈 %d건 (오류 %d)", len(links), len(findings), errors)
    r = rev(state)
    return {
        "trace_links": dump(links),
        "issues": [i.model_dump() for i in issues],
        "basis": {**basis(state), "review": [r["functions"], r["model"]]},
    }
