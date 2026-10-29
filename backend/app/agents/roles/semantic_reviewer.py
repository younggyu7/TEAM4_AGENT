"""의미 검토 Agent: LLM이 산출물의 '내용'을 검토해 담당자가 확인할 지적을 남긴다 (자동 수정하지 않음).

추적 검토 Agent(trace_reviewer)가 연결이 '있는지'만 본다면, 이 Agent는 내용이 '맞는지'를 본다.
검토는 두 부분으로 나눠 병렬로 요청하고, 바뀐 산출물에 해당하는 부분만 다시 검토한다.
요구사항 자체(분류·요구사항 사이의 모순)는 요구사항 검토 Agent(requirement_reviewer)가 기능 명세 전에 확인하므로 여기서 보지 않는다.

    기능 명세      : 요구사항 내용·수용 기준 누락, 요구사항과 어긋나는 규칙, '(가정)' 표시 없이 단정한 규칙
    데이터 모델    : 기능에 필요한 데이터·상태값 누락, 업무 규칙상 필요한 제약 누락 (예: 둘 중 하나만 채우는 FK)

- 지적은 SEM_* 이슈(WARNING, 수정 담당 none)로 남는다. 검토 보고서의 '의미 검토' 절에 근거·제안과 함께 나온다.
- 목록에 없는 ID를 대상으로 쓴 지적은 근거를 확인할 수 없으므로 버린다.
- 남기지 않는 지적: 원문에 없는 세부를 더 정하라는 지적(UNCLEAR)·애플리케이션 처리(APP_LOGIC), 근거 인용(quote)이 입력에 없는 것,
  모델이 스스로 이미 처리됐다고 답한 것(already_covered), 기능 ID 없이 요구사항만 가리키는
  기능 명세 지적(요구사항끼리의 문제는 요구사항 검토 Agent 몫), '(가정)'·'(확인 필요…)'가 붙은 규칙을 표시 없는 가정이라고 한 지적.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.agents.llm.client import get_llm, run_parallel
from app.schemas.design import DataModel, FunctionSpec, Requirement
from app.agents.tools.search import normalize, quote_in_text
from app.agents.flow.state import (AgentState, basis, finding, get_data_model, issues_of, load, merge_usage, reconcile_issues, rev,
                                   semantic_stale_parts)

log = logging.getLogger(__name__)
BY = "semantic_reviewer"
PART_LABELS = {"functions": "기능 명세", "model": "데이터 모델"}
PART_PREFIX = {"functions": "SEM_FUNC_", "model": "SEM_MODEL_"}

COMMON_RULES = """
규칙
1. 제공된 내용으로 확인되는 문제만 지적한다. 표현 다듬기, 일반적인 권고, 내용에 없는 추측은 쓰지 않는다.
2. target_ids에는 제공된 목록에 있는 ID만 쓴다.
3. message는 무엇이 문제인지 한 문장, evidence는 문제를 보여주는 문장·항목을 제공된 내용에서 그대로 인용, suggestion은 고칠 내용을 구체적으로 쓴다.
   quote에는 지적의 근거가 되는 문장 하나를 제공된 내용에서 글자 그대로 복사한다 (10자 이상, 앞에 '기능 명세:' 같은 말을 붙이지 않는다).
   제공된 내용에서 복사할 수 없는 지적은 버려진다.
4. 같은 문제를 여러 번 쓰지 않는다. 문제가 없으면 findings는 빈 배열이다. 중요한 것부터 최대 {limit}건.
5. 지적하기 전에 해당 기능·엔티티 내용을 다시 확인한다. 이미 처리됐으면 already_covered를 true로 하고, 그런 지적은 버려진다.
6. 모든 서술은 한국어."""

FUNCTIONS_PROMPT = """당신은 기능 명세 검토자다. 요구사항과 기능 명세를 대조해 기능 명세가 요구사항을 제대로 구현하는지 검토한다.

지적 종류 (category)
- GAP: 기능이 연결된 요구사항의 내용·수용 기준 일부를 다루지 않음 (빠진 동작·조건·입출력).
- CONFLICT: 기능의 업무 규칙·흐름이 요구사항 원문과 어긋나거나, 기능끼리 같은 대상에 서로 다른 규칙을 정함.
- UNMARKED_ASSUMPTION: 원문·수용 기준·결정에 없는 수치·기간·횟수·절차를 업무 규칙에 '(가정)' 표시 없이 단정함.
- UNCLEAR: 요구사항에도 없는 세부가 정해지지 않았거나 불명확함 (시점·범위·대상·수치·역할 분담을 더 정하라는 지적).
  이 종류는 고객 확인 후보로만 쓰이고 지적으로 남지 않는다. '불명확', '명시해야', '정의되지 않음'으로 끝나는 지적은 대부분 이것이다.
target_ids에는 관련 기능 ID(반드시 하나 이상)와 요구사항 ID를 쓴다.

지적하지 않는 것
- 앞에 '(가정)'·'(확인 필요)'·'(확인 필요 질문 ID)'·'(결정 질문 ID)'가 붙은 규칙: 이미 고객 확인 대상으로 표시됐다. 그 내용을 다시 지적하지 않는다.
- 요구사항끼리의 문제(개발 일정·우선순위, 요구사항 사이의 충돌, 요구사항 문장 자체의 모호함): 요구사항 검토 Agent가 기능 명세 전에 본다.
- 요구사항·수용 기준에도 없는 세부 정의를 더 쓰라는 요구 (예: 원문에 없는 참여 조건 항목·진행 단계 목록·'실시간'의 수치 정의)는
  GAP·CONFLICT가 아니라 UNCLEAR다. 기능 명세는 원문에 없는 판단을 최소로 한다.
  GAP은 요구사항·수용 기준에 있는 내용이 기능에서 빠진 경우만이며, quote에 그 요구사항 문장을 복사한다.
""" + COMMON_RULES.format(limit=20)

MODEL_PROMPT = """당신은 데이터 모델 검토자다. 기능 명세와 데이터 모델을 대조해 모델이 기능에 필요한 데이터와 규칙을 담는지 검토한다.

지적 종류 (category)
- GAP: 기능의 입력·출력·업무 규칙에 필요한 데이터를 저장할 컬럼이나 엔티티가 없음
  (예: 승인 기능인데 승인자·승인 일시 컬럼이 없음, 업무 규칙에 나온 상태값이 허용값에 없음).
- INTEGRITY: 업무 규칙상 지켜야 할 제약이 모델에 없음
  (예: 한 행이 여러 부모 중 정확히 하나만 가리켜야 하는데 NULL 허용 FK만 여러 개 있음,
   반드시 있어야 할 참조가 NULL 허용, 중복되면 안 되는 값에 UNIQUE가 없음).
- APP_LOGIC: 저장할 데이터나 DB 제약이 아니라 애플리케이션이 처리할 일 (검증 시점, 자동 처리, 계산, 형식 검사, 동시성, 세션, 인덱스·성능,
  '업무 규칙을 명시하라'는 제안). 이 종류는 지적으로 남지 않는다.
target_ids에는 엔티티명과 관련 기능 ID를 쓴다.

지적하지 않는 것
- 엔티티 줄에 이미 있는 것: 컬럼, NOT NULL, UNIQUE, 허용값 [..], 조합 유일, 정확히 하나, 조건부 필수, 값 조건, 참조(→).
- 기존 컬럼으로 표현되는 것 (예: locked_until이 있으면 잠금 시작 시각 컬럼은 필요 없음, 마지막 변경 일시로 주기 계산 가능).
- 저장할 데이터가 아닌 애플리케이션 처리(조회 기본값, 동시성 제어, 알림 발송, 자동 회수·만료 작업, 세션 관리)는 APP_LOGIC이다.
  업무 규칙이 그 결과를 기록하라고 할 때만 GAP이다.
- 감사 컬럼·명명 규칙·평문 인증 정보는 별도 규칙 검사가 확인한다. 정규화 방식이나 이름 취향도 지적하지 않는다.
""" + COMMON_RULES.format(limit=20)


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
    parts = semantic_stale_parts(state)
    requirements = load(state, "requirements", Requirement)
    functions = load(state, "functions", FunctionSpec)
    model = get_data_model(state)
    llm = get_llm()

    jobs = [_job(part, requirements, functions, model) for part in parts]
    results = run_parallel(lambda job: llm.structured(system=job[1], user=job[2], schema=job[3],
                                                      purpose=f"의미 검토 {PART_LABELS[job[0]]}"), jobs)
    known = {
        "functions": {r.requirement_id for r in requirements} | {f.function_id for f in functions},
        "model": {f.function_id for f in functions} | {e.entity_id for e in (model.entities if model else [])},
    }
    issues = issues_of(state)
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
        issues = reconcile_issues(issues, _merge_same_target(findings), lambda k, p=PART_PREFIX[part]: k.startswith(p),
                                  get_settings().max_fix_attempts)
        reviewed[part] = _basis_value(part, r)
        done.append(part)
        counts[part] = len(findings)
    log.info("[의미 검토] %s → 지적 %s%s%s%s", ", ".join(PART_LABELS[p] for p in parts),
             ", ".join(f"{PART_LABELS[p]} {n}건" for p, n in counts.items()) or "0건",
             f", 대상 ID 확인 불가로 제외 {dropped}건" if dropped else "",
             f", 남기지 않은 지적 {handled}건 (세부 미정·앱 처리·인용 없음·이미 처리)" if handled else "",
             f", 실패 {', '.join(PART_LABELS[p] for p in failed)}" if failed else "")
    update = {
        "issues": [i.model_dump() for i in issues],
        "basis": {**b, "semantic": reviewed},
        "semantic_review": {"parts": done, "failed": failed, "dropped": dropped, "handled": handled},
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
def _job(part: str, requirements: list[Requirement], functions: list[FunctionSpec], model: DataModel | None):
    """(부분, system 프롬프트, user 프롬프트, 응답 스키마)."""
    if part == "functions":
        designed = {rid for f in functions for rid in f.requirement_ids}
        reqs = [r for r in requirements if r.requirement_id in designed]
        user = "\n".join(["## 요구사항 (기능 명세의 근거)", *[_req_line(r) for r in reqs], "",
                          "## 기능 명세", *[_function_block(f) for f in functions]])
        return part, FUNCTIONS_PROMPT, user, FunctionReviewOut
    user = "\n".join(["## 기능 명세 (데이터 요구)", *[_function_data_line(f) for f in functions], "",
                      "## 데이터 모델", *[_entity_line(e) for e in (model.entities if model else [])]])
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
