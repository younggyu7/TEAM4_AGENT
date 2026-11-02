"""의미 검토 Agent: LLM이 산출물의 '내용'을 검토해 담당자가 확인할 지적을 남긴다 (이 Agent는 고치지 않는다).

추적 검토 Agent(trace_reviewer)가 연결이 '있는지'만 본다면, 이 Agent는 내용이 '맞는지'를 본다.
검토는 두 부분으로 나눠 병렬로 요청하고, 바뀐 산출물에 해당하는 부분만 다시 검토한다.
요구사항 자체(분류·요구사항 사이의 모순)는 요구사항 검토 Agent(requirement_reviewer)가 기능 명세 전에 확인하므로 여기서 보지 않는다.

    기능 명세      : 요구사항 내용·수용 기준 누락, 요구사항과 어긋나는 규칙, '(가정)' 표시 없이 단정한 규칙
    데이터 모델    : 기능에 필요한 데이터·상태값 누락, 업무 규칙상 필요한 제약 누락 (예: 둘 중 하나만 채우는 FK)

- 지적은 SEM_* 이슈(WARNING, 수정 담당 none)로 남는다. 검토 보고서의 '의미 검토' 절에 근거·제안과 함께 나온다.
- SEMANTIC_REVIEW_MODE=erd(기본)면 ERD를 만들 때 데이터 모델만 검토한다. 데이터 지적은 조정 Agent가 데이터 모델 Agent에
  한 번 넘겨 관련 테이블만 고치게 하고(coordinator.SEM_FIX), 고친 테이블과 그 테이블을 쓰는 기능만 다시 검토해
  (state.semantic_recheck, 모델 전체를 다시 보내지 않아 토큰 절약) 남은 지적은 ERD 화면에서 사람이 처리한다.
- 목록에 없는 ID를 대상으로 쓴 지적은 근거를 확인할 수 없으므로 버린다.
- 남기지 않는 지적: 원문에 없는 세부를 더 정하라는 지적(UNCLEAR)·애플리케이션 처리(APP_LOGIC), 근거 인용(quote)이 입력에 없는 것,
  모델이 스스로 이미 처리됐다고 답한 것(already_covered), 기능 ID 없이 요구사항만 가리키는
  기능 명세 지적(요구사항끼리의 문제는 요구사항 검토 Agent 몫), '(가정)'·'(확인 필요…)'가 붙은 규칙을 표시 없는 가정이라고 한 지적.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.prompts import load_prompt
from app.core.config import get_settings
from app.agents.llm.client import get_llm, run_parallel
from app.schemas.data_model import DataModel
from app.schemas.function import FunctionSpec
from app.schemas.requirement import Requirement
from app.agents.tools.search import normalize, quote_in_text
from app.agents.flow.state import (AgentState, basis, finding, get_data_model, issues_of, load, merge_usage, reconcile_issues, rev,
                                   semantic_parts)

log = logging.getLogger(__name__)
BY = "semantic_reviewer"
PART_LABELS = {"functions": "기능 명세", "model": "데이터 모델"}
PART_PREFIX = {"functions": "SEM_FUNC_", "model": "SEM_MODEL_"}

COMMON_RULES = load_prompt("semantic_reviewer/common_rules")   # {limit}을 채워 각 검토 프롬프트 뒤에 붙인다

FUNCTIONS_PROMPT = load_prompt("semantic_reviewer/functions") + COMMON_RULES.format(limit=20)

MODEL_PROMPT = load_prompt("semantic_reviewer/model") + COMMON_RULES.format(limit=20)


# LLM 응답 스키마: 지적 하나의 공통 항목
class _FindingBase(BaseModel):
    target_ids: list[str] = Field(description="대상 ID (요구사항 ID, 기능 ID, 엔티티명 중 제공된 것)")
    message: str = Field(description="무엇이 문제인지 한 문장")
    evidence: str = Field(description="문제를 보여주는 원문·명세·컬럼 인용")
    suggestion: str = Field(description="구체적인 수정안 (모순이면 고객에게 확인할 질문)")
    quote: str = Field(description="근거 문장 하나를 제공된 내용에서 글자 그대로 복사 (10자 이상)")
    already_covered: bool = Field(False, description="이미 표시('(가정)'·'(확인 필요…)')됐거나 기존 컬럼·제약으로 표현되거나 "
                                                     "요구사항끼리의 문제이면 true (그 지적은 버린다)")


# LLM 응답 스키마: 기능 명세 검토 지적
class FunctionFinding(_FindingBase):
    category: Literal["GAP", "CONFLICT", "UNMARKED_ASSUMPTION", "UNCLEAR"]


# LLM 응답 스키마: 데이터 모델 검토 지적
class ModelFinding(_FindingBase):
    category: Literal["GAP", "INTEGRITY", "APP_LOGIC"]


# LLM 응답 스키마: 기능 명세 검토 결과
class FunctionReviewOut(BaseModel):
    findings: list[FunctionFinding]


# LLM 응답 스키마: 데이터 모델 검토 결과
class ModelReviewOut(BaseModel):
    findings: list[ModelFinding]


def run(state: AgentState) -> dict:
    """의미 검토 Agent 노드: 바뀐 산출물 부분만 LLM으로 검토하고 SEM_* 이슈를 갱신한다."""
    parts = semantic_parts(state)
    requirements = load(state, "requirements", Requirement)
    functions = load(state, "functions", FunctionSpec)
    model = get_data_model(state)
    llm = get_llm()
    issues = issues_of(state)
    # 지적을 고친 직후: 고친 테이블 범위만 다시 검토하고, 그 범위 밖 지적은 그대로 둔다
    recheck = state.get("semantic_recheck") or {}
    scope = _recheck_scope(recheck, functions, model, issues) if "model" in parts and model and recheck else None

    jobs = [_job(part, requirements, functions, model, scope if part == "model" else None) for part in parts]
    results = run_parallel(lambda job: llm.structured(system=job[1], user=job[2], schema=job[3],
                                                      purpose=f"의미 검토 {PART_LABELS[job[0]]}"), jobs)
    known = {
        "functions": {r.requirement_id for r in requirements} | {f.function_id for f in functions},
        "model": {f.function_id for f in functions} | {e.entity_id for e in (model.entities if model else [])},
    }
    r, b = rev(state), basis(state)
    reviewed = dict(b["semantic"])
    function_ids = {f.function_id for f in functions}
    done, failed, dropped, handled, counts = [], [], 0, 0, {}
    for (part, _system, user, _schema), out, err in results:
        if err or out is None:
            failed.append(part)
            continue
        findings = []
        for f in out.findings:
            ids = sorted({i.strip() for i in f.target_ids if i.strip() in known[part]})
            if not ids:
                dropped += 1
                continue
            if _already_handled(part, f, ids, function_ids, user):
                handled += 1
                continue
            message = f"{f.message.strip()}\n근거: {f.evidence.strip()}\n제안: {f.suggestion.strip()}"
            findings.append(finding(f"{PART_PREFIX[part]}{f.category}", "WARNING", message,
                                    target_id=", ".join(ids), raised_by=BY))
        rechecked = set(recheck.get("issues") or []) if part == "model" and scope else None
        issues = reconcile_issues(issues, _merge_same_target(findings), lambda k, p=PART_PREFIX[part]: k.startswith(p),
                                  get_settings().fix_attempts,
                                  keep=(lambda i: i.issue_id not in rechecked) if rechecked is not None else None)
        reviewed[part] = _basis_value(part, r)
        done.append(part)
        counts[part] = len(findings)
    log.info("[의미 검토] %s%s → 지적 %s%s%s%s", ", ".join(PART_LABELS[p] for p in parts),
             f" (고친 테이블 {len(scope[1])}개만)" if scope else "",
             ", ".join(f"{PART_LABELS[p]} {n}건" for p, n in counts.items()) or "0건",
             f", 대상 ID 확인 불가로 제외 {dropped}건" if dropped else "",
             f", 남기지 않은 지적 {handled}건 (세부 미정·앱 처리·인용 없음·이미 처리)" if handled else "",
             f", 실패 {', '.join(PART_LABELS[p] for p in failed)}" if failed else "")
    update = {
        "issues": [i.model_dump() for i in issues],
        "basis": {**b, "semantic": reviewed},
        "semantic_review": {"parts": done, "failed": failed, "dropped": dropped, "handled": handled,
                            "scoped": len(scope[1]) if scope else 0},
        "semantic_recheck": None,
        "llm_usage": merge_usage(state, llm.pop_usage()),
    }
    if failed:
        update["failures"] = {**(state.get("failures") or {}), BY: (state.get("failures") or {}).get(BY, 0) + 1}
    return update


MARKED = ("(가정)", "(확인 필요")
NOT_KEPT = {"UNCLEAR", "APP_LOGIC"}   # 원문에 없는 세부 요구·애플리케이션 처리: 모델이 내보낼 곳을 주고 버린다
MIN_QUOTE = 10


def _already_handled(part: str, f, ids: list[str], function_ids: set[str], source: str = "") -> bool:
    """남기지 않을 지적인지: 모델이 처리됐다고 답함, 세부 미정·애플리케이션 처리 종류, 근거 인용이 입력에 없음,
    기능 없이 요구사항만 가리킴(요구사항 검토 Agent 몫), '(가정)'·'(확인 필요…)' 표시가 있는 규칙을 표시 없는 가정이라고 함."""
    if f.already_covered or f.category in NOT_KEPT:
        return True
    if source and not (len(normalize(f.quote)) >= MIN_QUOTE and quote_in_text(f.quote, source)):
        return True
    if part != "functions":
        return False
    if not function_ids & set(ids):
        return True
    return f.category == "UNMARKED_ASSUMPTION" and any(m in f.evidence for m in MARKED)


def _basis_value(part: str, r: dict):
    """부분별로 검토 기준이 된 산출물 수정 횟수 (state.semantic_stale_parts와 같은 형태)."""
    if part == "functions":
        return [r["requirements"], r["functions"]]
    return [r["functions"], r["model"]]


def _merge_same_target(findings: list) -> list:
    """같은 종류·같은 대상의 지적은 하나로 합친다 (이슈 키가 (종류, 대상)이라 따로 두면 뒤의 것이 사라진다)."""
    merged: dict[tuple[str, str], object] = {}
    for f in findings:
        if f.key in merged:
            prev = merged[f.key]
            merged[f.key] = prev.model_copy(update={"message": f"{prev.message}\n\n{f.message}"})
        else:
            merged[f.key] = f
    return list(merged.values())


# ------------------------------------------------------------------ 입력 렌더링
def _recheck_scope(recheck: dict, functions: list[FunctionSpec], model: DataModel, issues) -> tuple[set[str], set[str]]:
    """다시 검토할 (기능 ID, 엔티티 ID): 다시 설계한 엔티티·고친 지적의 대상 엔티티와, 지적 대상 기능·그 엔티티를 쓰는 기능.
    사용자처럼 거의 모든 기능이 쓰는 공통 테이블은 쓰는 기능을 다 넣지 않는다 (지적 대상 기능만으로 충분하고 입력만 커진다)."""
    ids = set(recheck.get("issues") or [])
    targets = {t.strip() for i in issues if i.issue_id in ids for t in i.target_id.split(",")}
    entity_ids = {e.entity_id for e in model.entities}
    entities = (set(recheck.get("entities") or []) | targets) & entity_ids
    shared = max(5, len(functions) // 4)
    fns = {fid for e in model.entities if e.entity_id in entities and len(e.function_ids) <= shared for fid in e.function_ids}
    fns |= targets & {f.function_id for f in functions}
    return fns, entities


def _job(part: str, requirements: list[Requirement], functions: list[FunctionSpec], model: DataModel | None,
         scope: tuple[set[str], set[str]] | None = None):
    """(부분, system 프롬프트, user 프롬프트, 응답 스키마). scope가 있으면 데이터 모델은 그 기능·엔티티만 넣는다."""
    if part == "functions":
        designed = {rid for f in functions for rid in f.requirement_ids}
        reqs = [r for r in requirements if r.requirement_id in designed]
        user = "\n".join(["## 요구사항 (기능 명세의 근거)", *[_req_line(r) for r in reqs], "",
                          "## 기능 명세", *[_function_block(f) for f in functions]])
        return part, FUNCTIONS_PROMPT, user, FunctionReviewOut
    entities = model.entities if model else []
    if scope:
        fns, names = scope
        others = [e.entity_id for e in entities if e.entity_id not in names]
        functions = [f for f in functions if f.function_id in fns]
        entities = [e for e in entities if e.entity_id in names]
    user = "\n".join(["## 기능 명세 (데이터 요구)", *[_function_data_line(f) for f in functions], "",
                      "## 데이터 모델", *[_entity_line(e) for e in entities]])
    if scope and others:   # 범위 밖 테이블도 있다는 것만 알려 '없는 테이블' 지적을 막는다
        user += "\n\n## 이번 검토 대상이 아닌 다른 테이블 (이름만, 지적하지 않는다)\n" + ", ".join(others)
    return part, MODEL_PROMPT, user, ModelReviewOut


def _req_line(r: Requirement) -> str:
    """요구사항 한 줄: ID, 제목·설명, 수용 기준, 결정, 확인된 원문 인용."""
    line = f"- [{r.requirement_id}] {r.title} — {r.description}"
    if r.acceptance_criteria:
        line += f" | 수용 기준: {'; '.join(r.acceptance_criteria)}"
    for d in r.decisions:
        line += f" | 결정: {d}"
    quotes = [e.quote for e in r.evidence if e.verified and e.quote]
    if quotes:
        line += " | 원문: " + " / ".join(f'"{q}"' for q in quotes)
    return line


def _function_block(f: FunctionSpec) -> str:
    """기능 명세 하나를 검토용 여러 줄로 쓴다."""
    lines = [f"### [{f.function_id}] {f.name} (근거: {', '.join(f.requirement_ids)})", f"설명: {f.description}"]
    if f.actors:
        lines.append(f"행위자: {', '.join(f.actors)}")
    if f.inputs:
        lines.append("입력: " + ", ".join(i.name for i in f.inputs))
    if f.outputs:
        lines.append("출력: " + ", ".join(o.name for o in f.outputs))
    lines += [f"업무 규칙: {rule}" for rule in f.business_rules]
    if f.normal_flow:
        lines.append("정상 흐름: " + " → ".join(f.normal_flow))
    lines += [f"예외: {e.condition} → {e.handling}" for e in f.exception_flows]
    return "\n".join(lines)


def _function_data_line(f: FunctionSpec) -> str:
    """데이터 모델 검토용 기능 요약: 데이터 객체, 입출력, 업무 규칙."""
    line = (f"- [{f.function_id}] {f.name} | 데이터: {', '.join(f.data_objects) or '없음'}"
            f" | 입력: {', '.join(i.name for i in f.inputs) or '없음'} | 출력: {', '.join(o.name for o in f.outputs) or '없음'}")
    if f.business_rules:
        line += " | 업무 규칙: " + "; ".join(f.business_rules)
    return line


def _entity_line(e) -> str:
    """엔티티 하나: 종류·한글명·연결 기능·컬럼(타입, PK, FK, NOT NULL, UNIQUE, 허용값)·여러 컬럼 제약."""
    cols = []
    for a in e.attributes:
        col = f"{a.name}:{a.data_type}"
        col += "(PK)" if a.primary_key else ""
        col += f"→{a.references}" if a.references else ""
        col += "" if a.nullable or a.primary_key else " NOT NULL"
        col += " UNIQUE" if a.unique and not a.primary_key else ""
        col += f" [{'/'.join(a.allowed_values)}]" if a.allowed_values else ""
        cols.append(col)
    line = (f"- {e.entity_id} ({e.kind}) {e.label} | 기능: {', '.join(e.function_ids) or '없음'}"
            f" | 컬럼: {', '.join(cols)}")
    # 여러 컬럼에 걸친 제약: 빠지면 모델이 이미 있는 UNIQUE·CHECK를 없다고 지적한다
    for group in e.unique_together:
        line += f" | 조합 유일: ({', '.join(group)})"
    for group in e.exactly_one_of:
        line += f" | 정확히 하나: ({', '.join(group)})"
    for w in e.required_when:
        line += f" | 조건부 필수: {w.when_column} ∈ [{'/'.join(w.when_values)}]이면 {w.column} NOT NULL"
    for c in e.checks:
        line += f" | 값 조건: {c.column} {c.op} {c.value}"
    return line
