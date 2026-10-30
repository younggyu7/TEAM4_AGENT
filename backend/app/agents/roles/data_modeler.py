"""데이터 모델 Agent: 기능 명세 → 엔티티·속성·관계·키·제약 (ERD·DDL의 공통 입력).

큰 모델을 한 번에 생성하면 출력이 잘리기 쉬워 2단계로 나눈다.
  1) 엔티티 카탈로그 (이름·종류·연결 기능·상위 엔티티, 꼭 필요한 확인 질문)
  2) 엔티티 묶음별 속성 설계 (병렬, 컬럼과 여러 컬럼 제약)
- mode=full  : 1) + 2)
- mode=cover : 지정 기능을 기존 엔티티에 연결하거나 새 엔티티 추가 (기능 변경·누락 시 부분 재설계)
- mode=fix   : 지정 엔티티의 속성만 오류 메시지를 반영해 다시 설계 (모델 검증·DB 검증 실패 시)
- mode=redesign : 지정 엔티티(rerun --entities)나 사용자 의견과 관련된 엔티티만 컬럼을 다시 설계하고 나머지는 그대로 둔다.
  의견만 있으면 작은 요청 하나(엔티티 목록 + 의견)로 다시 설계할 엔티티와 새로 필요한 엔티티를 고른다.
- 사용자가 고친 엔티티(user_edited)는 full·fix·redesign 어디서도 LLM이 다시 설계하지 않는다. 카탈로그에는 그대로 넘기고,
  rerun --entities로 직접 지정한 경우만 다시 설계한다 (고정이 풀린다). 사용자가 지운 엔티티는 연결 보완에서 다시 만들지 않는다.

- 입력: 기능마다 행위자·데이터·입출력·업무 규칙을 자르지 않고 넘긴다 (행위자로 계정·역할 구조를, 규칙으로 필수 여부·허용값을 정한다).
- 카탈로그에서 정한 상위 엔티티(parents)는 Entity.parents에 보관한다. 속성이 비어 다시 설계할 때도 연결 의도를 잃지 않는다.
- 속성 설계 묶음이 실패하면 엔티티 하나씩 한 번 더 요청하고, 그래도 실패하면 원인을 DATA_ATTR_DESIGN_FAILED 이슈로 남긴다.
- 확인 질문: 명세에 근거가 없거나 서로 다른 구조 결정만, 실행 전체에서 최대 MAX_DATA_QUESTIONS개를 묻는다.
  설계는 추천 선택지로 먼저 진행하고, 사용자가 다른 선택지를 고를 때만 다시 설계한다 (질문·재설계 비용 최소화).
- 토큰 절감: 속성 설계 묶음마다 같은 카탈로그·비기능 요구·결정은 공통 앞부분(shared)으로 보내 프롬프트 캐시로 재사용하고,
  첫 묶음을 먼저 보내 캐시를 만든 뒤 나머지를 병렬로 보낸다. 묶음 안에서 같은 기능은 한 번만 넣는다.
- 출력 줄이기: 기본키·감사 컬럼은 LLM이 쓰지 않고 정규화 단계에서 코드가 추가한다. 응답의 기본값 항목(unique·references·
  allowed_values·length)과 필요 없는 설명은 생략할 수 있다 (nullable은 항상 쓴다).
  묶음 응답에서 엔티티 이름만 다르게 돌려준 경우는 재요청하지 않고 짝지어 쓰며, 재요청한 엔티티와 이유는 DATA_ATTR_RETRIED로 남긴다.
  묶음 요청 끝에는 응답에 넣을 엔티티 이름을 다시 적어 엔티티가 통째로 빠지는 것을 막는다.
- 여러 컬럼 제약: unique_together·exactly_one_of·required_when, 값 조건 checks(amount > 0, end_at >= start_at 등) → DDL CHECK.
- 사용자 의견(options.feedback.data_modeler, rerun --feedback)은 카탈로그·속성 설계 프롬프트에 넣는다.
- 기능 명세의 공통 결정(state.function_common: 분류·상태 값·업무 객체 이름·공통 규칙)도 같이 넣어 허용값을 기능 명세와 맞춘다.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.prompts import load_prompt
from app.core.config import get_settings
from app.agents.llm.client import get_llm, run_parallel
from app.schemas.data_model import Attribute, CheckRule, DataModel, DataType, Entity, RequiredWhen
from app.schemas.function import FunctionSpec
from app.schemas.requirement import Question, Requirement
from app.schemas.review import Issue
from app.agents.flow.state import (AgentState, basis, dump, finding, get_data_model, issues_of, load, merge_usage,
                                   reconcile_issues, rev)
from app.agents.tools.convention import guide_block
from app.agents.tools.model_edits import ReworkSummary
from app.agents.roles.function_designer import common_block
from app.agents.tools.model_validator import normalize_model, to_identifier

log = logging.getLogger(__name__)
MAX_DATA_QUESTIONS = 3   # 한 실행에서 데이터 설계가 사용자에게 묻는 질문 수 한도

CATALOG_PROMPT = load_prompt("data_modeler/catalog")

ATTRIBUTE_PROMPT = load_prompt("data_modeler/attribute")

KINDS = Literal["MASTER", "TRANSACTION", "HISTORY", "MAPPING", "CODE"]
# 속성 설계가 정하는 엔티티 항목 (부분 재설계 때 이것만 바꾼다)
DESIGNED_FIELDS = ("attributes", "unique_together", "exactly_one_of", "required_when", "checks")


# LLM 응답 스키마: 엔티티 카탈로그 항목 (이름·종류·연결 기능·상위 엔티티)
class EntityPlanOut(BaseModel):
    name: str
    label: str
    description: str
    kind: KINDS
    function_ids: list[str]
    parents: list[str]


# LLM 응답 스키마: 데이터 설계 확인 질문 (명세에 근거가 없거나 서로 다른 구조 결정만)
class DesignQuestionOut(BaseModel):
    kind: Literal["MISSING", "CONFLICT"] = Field(description="MISSING: 명세에 없음, CONFLICT: 명세끼리 다름")
    question: str
    context: str = Field(description="근거: 서로 다른 명세 내용이나 비어 있는 부분을 짧게 인용")
    options: list[str] = Field(description="선택지 2~4개 (명세에 나온 값 우선)")
    recommended: int = Field(description="추천 선택지 번호 (1부터). 이번 설계는 이 선택지로 진행")
    function_ids: list[str]
    entity_names: list[str]


# LLM 응답 스키마: 엔티티 카탈로그
class CatalogOut(BaseModel):
    entities: list[EntityPlanOut]
    questions: list[DesignQuestionOut] = Field(default_factory=list,
                                               description="꼭 필요한 확인 질문만 (최대 3개, 없으면 생략)")


# LLM 응답 스키마: 속성(컬럼) 하나. 기본키·감사 컬럼은 코드가 추가하고, 기본값인 항목은 생략할 수 있다 (출력 토큰 절감).
class AttributeOut(BaseModel):
    name: str
    label: str
    data_type: DataType
    nullable: bool = Field(description="항상 쓴다")
    length: int = Field(0, description="varchar만 1~4000, 그 외 생략")
    unique: bool = Field(False, description="이 컬럼 하나로 유일할 때만 true, 아니면 생략")
    references: str = Field("", description="외래키면 참조 엔티티 name, 아니면 생략")
    allowed_values: list[str] = Field(default_factory=list, description="값 전체가 확인된 경우만, 아니면 생략")
    description: str = Field("", description="이름·label만으로 부족하거나 (가정)·(확인 필요)를 적을 때만")


# LLM 응답 스키마: 조건부 필수 (when_column이 when_values 중 하나이면 column 필수)
class RequiredWhenOut(BaseModel):
    column: str
    when_column: str
    when_values: list[str]


# LLM 응답 스키마: 값 조건 (column op value)
class CheckOut(BaseModel):
    column: str
    op: Literal[">", ">=", "<", "<=", "<>"]
    value: str = Field(description="같은 엔티티의 다른 컬럼 이름 또는 숫자")


# LLM 응답 스키마: 엔티티 하나의 속성 목록과 여러 컬럼 제약
class EntityAttributesOut(BaseModel):
    name: str
    attributes: list[AttributeOut]
    unique_together: list[list[str]] = Field(default_factory=list, description="조합이 유일해야 하는 컬럼 묶음 (없으면 생략)")
    exactly_one_of: list[list[str]] = Field(default_factory=list,
                                            description="정확히 하나만 채워야 하는 외래키 묶음 (없으면 생략)")
    required_when: list[RequiredWhenOut] = Field(default_factory=list, description="조건부 필수 (없으면 생략)")
    checks: list[CheckOut] = Field(default_factory=list, description="값 범위·순서·금지 조건 (없으면 생략)")


# LLM 응답 스키마: 엔티티 묶음의 속성 설계 결과
class AttributesOut(BaseModel):
    entities: list[EntityAttributesOut]


# LLM 응답 스키마: 기능 → 기존 엔티티 연결
class MappingOut(BaseModel):
    function_id: str
    entity_names: list[str]


# LLM 응답 스키마: 기능 연결 보완 결과 (기존 엔티티 연결 + 새 엔티티)
class CoverageOut(BaseModel):
    mappings: list[MappingOut] = Field(description="기존 엔티티에 연결할 기능")
    new_entities: list[EntityPlanOut] = Field(description="새로 필요한 엔티티 (기존으로 충분하면 빈 배열)")


# LLM 응답 스키마: 사용자 의견을 반영할 범위 (다시 설계할 기존 엔티티 + 새로 필요한 엔티티)
class RedesignScopeOut(BaseModel):
    entity_names: list[str] = Field(description="의견을 반영하려면 컬럼을 다시 설계해야 하는 기존 엔티티 name만")
    new_entities: list[EntityPlanOut] = Field(default_factory=list,
                                              description="의견 때문에 새로 필요한 엔티티만 (없으면 생략)")


def run(state: AgentState) -> dict:
    """데이터 모델 Agent 노드: 모드(full/cover/fix/redesign)에 맞게 설계하고, 정규화한 모델로 상태를 갱신한다."""
    action = state.get("next_action") or {}
    mode = action.get("mode") or "full"
    scope = list(action.get("scope") or [])
    functions = load(state, "functions", FunctionSpec)
    requirements = load(state, "requirements", Requirement)
    questions = load(state, "questions", Question)
    current = get_data_model(state)
    llm = get_llm()
    # 기능 명세의 공통 결정(분류·상태 값·업무 객체 이름)을 같이 넘겨 허용값·엔티티 이름을 기능 명세와 맞춘다
    common = common_block(state.get("function_common") or {},
                          "## 기능 명세 공통 결정 (분류·상태 값과 업무 객체 이름은 이 목록을 따른다)")
    # 설계 표준(--standard)의 서술형 지침만 넣는다 (이름 규칙은 산출물을 쓸 때 코드가 적용 — tools/convention.py)
    context = "\n\n".join(x for x in (common, guide_block(state.get("options")), _decisions_block(questions),
                                      feedback_block(state)) if x)
    rework = ReworkSummary.model_validate(state.get("model_rework") or {})

    asked: list[DesignQuestionOut] = []
    retried: dict[str, str] = {}
    if mode == "redesign" and current is not None:
        model, failed = _redesign(llm, current, functions, requirements, scope, _feedback(state), context, retried,
                                  rework)
    elif current is None or mode == "full" or "*" in scope:
        locked = [e for e in current.entities if e.user_edited] if current else []
        model, failed, asked = _full(llm, functions, requirements, context, retried, locked)
        rework.full = rework.full or current is not None
    elif mode == "cover":
        model, failed = _cover(llm, current, functions, requirements, set(scope), context, retried, rework.removed)
    else:
        model, failed = _fix(llm, current, functions, requirements, set(scope), issues_of(state), context, retried)

    fn_reqs = {f.function_id: f.requirement_ids for f in functions}
    model, notes = normalize_model(model, fn_reqs)
    for note in notes:
        log.info("[데이터모델] 정규화: %s", note)
    new_questions = _to_questions(asked, questions, fn_reqs, {e.entity_id for e in model.entities})
    log.info("[데이터모델] mode=%s → 엔티티 %d개, 관계 %d개%s%s", mode, len(model.entities), len(model.relations),
             f", 다시 설계 {len(rework.redesigned)}개 ({', '.join(rework.redesigned)})" if mode == "redesign" else "",
             f", 확인 질문 {len(new_questions)}건" if new_questions else "")
    # 속성 설계 실패 원인: 속성이 빈 엔티티는 검증 오류(MODEL_NO_ATTRIBUTES)로 다시 고치고, 원인은 보고서에 남긴다
    failures = [finding("DATA_ATTR_DESIGN_FAILED", "INFO", f"{name}: 속성 설계 요청 실패 — {reason}",
                        target_id=name, raised_by="data_modeler") for name, reason in failed.items()]
    # 재요청은 출력 토큰을 다시 쓰는 낭비라 원인을 남긴다 (토큰 절감 추적용)
    if retried:
        reasons = sorted(set(retried.values()))
        failures.append(finding("DATA_ATTR_RETRIED", "INFO",
                                f"속성 설계 묶음에서 엔티티 {len(retried)}개를 다시 요청함: {', '.join(sorted(retried))[:300]}"
                                f" — 이유: {' / '.join(reasons)[:300]}", target_id="*", raised_by="data_modeler"))
    owned = {"DATA_ATTR_DESIGN_FAILED", "DATA_ATTR_RETRIED"}
    if mode == "redesign":   # 사용자 수정·건너뛴 엔티티 기록은 다시 설계할 때만 새로 정한다
        owned |= {"DATA_USER_EDITS", "DATA_USER_EDITED_SKIPPED"}
        failures += _rework_findings(rework)
    issues = reconcile_issues(issues_of(state), failures, lambda k: k in owned, get_settings().fix_attempts)
    r, b = rev(state), basis(state)
    update = {
        "data_model": model.model_dump(mode="json"),
        "rev": {**r, "model": r["model"] + 1},
        "basis": {**b, "model": r["functions"]},
        "dirty_functions": [],
        "model_redesign": False,
        "model_targets": None,
        "model_rework": rework.model_dump(),
        "issues": [i.model_dump() for i in issues],
        "llm_usage": merge_usage(state, llm.pop_usage()),
    }
    if new_questions:
        update["questions"] = dump(questions + new_questions)
    return update


def _feedback(state: AgentState) -> str:
    """사용자 의견(rerun --feedback) 원문."""
    return ((state.get("options") or {}).get("feedback") or {}).get("data_modeler", "").strip()


def feedback_block(state: AgentState) -> str:
    """사용자 의견(다시 만들기 요청)을 프롬프트 블록으로 만든다. 없으면 빈 문자열."""
    text = _feedback(state)
    return f"## 사용자 의견 (이번 설계에 반드시 반영)\n{text}" if text else ""


def _rework_findings(rework: ReworkSummary) -> list[Issue]:
    """반영한 사용자 수정과, 의견과 관련 있지만 사용자 수정이라 다시 설계하지 않은 엔티티를 보고서용 이슈로 남긴다."""
    out = []
    if rework.has_edits():
        out.append(finding("DATA_USER_EDITS", "INFO", f"사용자 수정 반영 (고친·새 엔티티는 고정): {rework.edit_text()}"[:600],
                           target_id="*", raised_by="data_modeler"))
    for name in rework.skipped:
        out.append(finding("DATA_USER_EDITED_SKIPPED", "WARNING",
                           f"{name}: 사용자 의견·기능 변경과 관련 있지만 사용자가 고친 엔티티라 다시 설계하지 않음 — 직접 확인 "
                           f"(다시 설계하려면 rerun --entities {name})", target_id=name, raised_by="data_modeler"))
    return out


def _decisions_block(questions: list[Question]) -> str:
    """이미 다룬 데이터 설계·기능 명세 질문과 결정 (다시 묻지 않고 그대로 반영하게 한다)."""
    lines = []
    for q in questions:
        if q.stage not in ("data_modeler", "function_designer"):
            continue
        if q.status == "ANSWERED":
            lines.append(f"- [{q.question_id}] {q.question} → 결정: {q.answer}")
        else:
            lines.append(f"- [{q.question_id}] {q.question} → 답 없음: 추천 '{q.recommended}'으로 설계 (가정)")
    return "## 이미 다룬 질문 (다시 묻지 않는다)\n" + "\n".join(lines) if lines else ""


def _to_questions(asked: list[DesignQuestionOut], existing: list[Question], fn_reqs: dict[str, list[str]],
                  entity_ids: set[str]) -> list[Question]:
    """카탈로그의 질문을 확인 질문으로 바꾼다.

    선택지가 2개 미만이거나 이미 한 질문은 버리고, 실행 전체에서 MAX_DATA_QUESTIONS개까지만 사용자에게 묻는다(blocking).
    한도를 넘는 질문은 묻지 않고 추천 선택지로 진행한 가정으로 남는다.
    """
    data_qs = [q for q in existing if q.stage == "data_modeler"]
    seen = {q.question.strip() for q in data_qs}
    budget = max(0, MAX_DATA_QUESTIONS - sum(q.blocking for q in data_qs))
    number = max((int(q.question_id.split("-")[1]) for q in data_qs), default=0)
    out = []
    for a in asked:
        options = list(dict.fromkeys(o.strip() for o in a.options if o.strip()))[:4]
        if len(options) < 2 or a.question.strip() in seen:
            continue
        seen.add(a.question.strip())
        number += 1
        out.append(Question(
            question_id=f"DQ-{number:03d}", kind=a.kind, question=a.question.strip(), context=a.context.strip(),
            options=options, recommended=options[min(max(a.recommended, 1), len(options)) - 1],
            requirement_ids=sorted({r for f in a.function_ids for r in fn_reqs.get(f, [])}),
            entity_ids=[e for e in a.entity_names if e in entity_ids], blocking=budget > 0, stage="data_modeler"))
        budget -= 1
    return out


# ------------------------------------------------------------------ 모드별 처리
def _full(llm, functions, requirements, context: str = "", retried: dict | None = None,
          locked: list[Entity] | None = None):
    """카탈로그 → 속성 2단계로 전체 모델을 설계한다. (모델, 속성 설계에 실패한 엔티티 → 원인, 확인 질문)

    locked(사용자가 고친 엔티티)는 카탈로그에 확정 엔티티로 넘기고 그대로 둔다. 카탈로그가 같은 이름을 내도 다시 설계하지 않는다.
    """
    locked = locked or []
    fixed = {e.entity_id: e for e in locked}
    user = "\n\n".join(x for x in (_catalog_input(functions, requirements), _locked_block(locked), context) if x)
    catalog = llm.structured(system=CATALOG_PROMPT, user=user, schema=CatalogOut, purpose="엔티티 카탈로그")
    plans = [p for p in _dedupe_plans(catalog.entities) if to_identifier(p.name) not in fixed]
    locked_plans = _plans_from_model(DataModel(entities=locked))
    designed, failed = _design_attributes(llm, plans, plans + locked_plans, functions, requirements, context=context,
                                          retried=retried)
    return DataModel(entities=locked + designed), failed, catalog.questions


def _locked_block(locked: list[Entity]) -> str:
    """사용자가 확정한 엔티티 목록 (카탈로그에 그대로 둔다). 없으면 빈 문자열."""
    if not locked:
        return ""
    lines = ["## 사용자가 확정한 엔티티 (이름·구조를 바꾸지 않는다. 같은 데이터를 담는 엔티티를 새로 만들지 않는다)"]
    for e in locked:
        columns = ", ".join(a.name for a in e.attributes if not a.primary_key and a.name not in AUDIT_NAMES)
        lines.append(f"- {e.entity_id} ({e.kind}) {e.label}; 상위: {', '.join(e.parents) or '없음'}; 컬럼: {columns or '없음'}")
    return "\n".join(lines)


def _cover(llm, model: DataModel, functions, requirements, function_ids: set[str], context: str = "",
           retried: dict | None = None, removed: list[str] | None = None):
    """지정 기능을 기존 엔티티에 연결하고, 기존 엔티티로 표현할 수 없는 데이터만 새 엔티티로 추가한다.

    removed(사용자가 지운 엔티티)는 다시 만들지 않는다.
    """
    removed = removed or []
    known = {f.function_id for f in functions}
    # 삭제된 기능 연결 정리
    for e in model.entities:
        e.function_ids = [f for f in e.function_ids if f in known]
    targets = [f for f in functions if f.function_id in function_ids and f.data_objects]
    if not targets:
        return model, {}
    catalog_lines = [f"- {e.entity_id} ({e.kind}) {e.label}: {e.description}" for e in model.entities]
    user = "\n".join([
        "## 기존 엔티티 카탈로그", *catalog_lines, "",
        "## 엔티티에 연결해야 할 기능", *[_function_line(f) for f in targets], "",
        _data_nfr_block(requirements),
        "",
        "각 기능을 기존 엔티티에 연결(mappings)하고, 기존 엔티티로 표현할 수 없는 데이터만 new_entities로 추가하라.",
        "이번 응답에는 질문 항목이 없다. 근거가 없는 판단은 (가정)으로 진행한다.",
        *([f"사용자가 지운 엔티티(다시 만들지 않는다): {', '.join(removed)}"] if removed else []),
        *([context] if context else []),
    ])
    out = llm.structured(system=CATALOG_PROMPT, user=user, schema=CoverageOut, purpose="기능-엔티티 연결")
    by_name = {e.entity_id: e for e in model.entities}
    for m in out.mappings:
        for name in (to_identifier(x) for x in m.entity_names):   # 'ServiceRequest'처럼 돌려줘도 기존 엔티티에 연결
            if name in by_name and m.function_id in known and m.function_id not in by_name[name].function_ids:
                by_name[name].function_ids.append(m.function_id)
    new_plans = _new_plans(out.new_entities, known, set(by_name) | set(removed))
    failed: dict[str, str] = {}
    if new_plans:
        all_plans = _plans_from_model(model) + new_plans
        entities, failed = _design_attributes(llm, new_plans, all_plans, functions, requirements, context=context,
                                              retried=retried)
        model.entities.extend(entities)
    return model, failed


def _fix(llm, model: DataModel, functions, requirements, entity_names: set[str], issues: list[Issue],
         context: str = "", retried: dict | None = None):
    """검증 오류가 난 엔티티의 속성·제약만 오류 메시지를 반영해 다시 설계한다 (사용자가 고친 엔티티는 고치지 않는다)."""
    entity_names = {e.entity_id for e in model.entities if e.entity_id in entity_names and not e.user_edited}
    if not entity_names:
        return model, {}
    errors: dict[str, list[str]] = {}
    for i in issues:
        if i.status == "OPEN" and i.target_agent == "data_modeler" and i.target_id in entity_names:
            errors.setdefault(i.target_id, []).append(i.message)
    plans = _plans_from_model(model)
    target_plans = [p for p in plans if p.name in entity_names]
    redesigned, failed = _design_attributes(llm, target_plans, plans, functions, requirements, model=model,
                                            errors=errors, context=context, retried=retried)
    fields = DESIGNED_FIELDS
    replaced = {e.entity_id: e for e in redesigned if e.attributes}
    model.entities = [e.model_copy(update={k: getattr(replaced[e.entity_id], k) for k in fields})
                      if e.entity_id in replaced else e for e in model.entities]
    return model, failed


REDESIGN_NOTE = ("## 다시 설계 지시\n'현재 속성'을 바탕으로 사용자 의견을 반영한다. 의견과 관계없는 컬럼·제약은 그대로 둔다. "
                 "의견을 반영하려고 외래키 대상(상위 엔티티)을 바꿔야 하면 바꿔도 된다.")
CHANGE_NOTE = ("## 다시 설계 지시\n'현재 속성'을 바탕으로 내용이 바뀐 기능 {ids}을 반영한다. "
               "바뀐 내용과 관계없는 컬럼·제약은 이름·타입·필수 여부를 그대로 둔다.")


def _redesign(llm, model: DataModel, functions, requirements, names: list[str], feedback: str, context: str,
              retried: dict, rework: ReworkSummary):
    """지정 엔티티(없으면 사용자 의견과 관련된 엔티티)의 컬럼만 다시 설계하고 나머지 엔티티는 그대로 둔다.

    - names: rerun --entities로 사용자가 직접 지정한 엔티티. 사용자가 고친 엔티티도 다시 설계하고 고정을 푼다.
    - names가 없고 의견이 있으면 작은 요청 하나(엔티티 목록 + 기능 이름 + 의견)로 다시 설계할 엔티티와 새 엔티티를 고른다.
      고른 엔티티 중 사용자가 고친 것은 다시 설계하지 않고 rework.skipped로 남긴다.
    - 의견이 있으면 다시 설계한 엔티티의 상위 엔티티를 새 외래키에 맞춘다 (의견으로 관계가 바뀔 수 있다).
    다시 설계한 엔티티는 rework.redesigned에 적는다. (모델, 속성 설계에 실패한 엔티티 → 원인)
    """
    by_name = {e.entity_id: e for e in model.entities}
    new_plans: list[EntityPlanOut] = []
    if names:
        targets = [n for n in dict.fromkeys(names) if n in by_name]
    elif feedback:
        scope = llm.structured(system=CATALOG_PROMPT, user=_scope_input(model, functions, feedback),
                               schema=RedesignScopeOut, purpose="의견 반영 범위")
        chosen = [n for n in dict.fromkeys(to_identifier(x) for x in scope.entity_names) if n in by_name]
        targets = [n for n in chosen if not by_name[n].user_edited]
        rework.skipped = [n for n in chosen if by_name[n].user_edited]
        new_plans = _new_plans(scope.new_entities, {f.function_id for f in functions},
                               set(by_name) | set(rework.removed))
    else:
        targets = []
    rework.redesigned = targets + [p.name for p in new_plans]
    if not rework.redesigned:
        return model, {}
    plans = _plans_from_model(model)
    target_plans = [p for p in plans if p.name in targets] + new_plans
    # 기능 명세 단계 버전에서 이어받은 모델이면 바뀐 기능만 반영하게 한다 (처음부터 다시 설계하면 관계없는 컬럼이 빠진다)
    change = CHANGE_NOTE.format(ids=", ".join(rework.changed_functions)) if rework.changed_functions and not feedback else ""
    note = "\n\n".join(x for x in (context, REDESIGN_NOTE if feedback else change) if x)
    designed, failed = _design_attributes(llm, target_plans, plans + new_plans, functions, requirements, model=model,
                                          context=note, retried=retried)
    fields = DESIGNED_FIELDS
    replaced = {e.entity_id: e for e in designed if e.attributes}
    entities = []
    for e in model.entities:
        new = replaced.get(e.entity_id)
        if new is None:
            entities.append(e)
            continue
        update = {k: getattr(new, k) for k in fields} | {"user_edited": False}
        if feedback:
            update["parents"] = sorted({a.references for a in new.attributes if a.references and a.references != e.entity_id})
        entities.append(e.model_copy(update=update))
    # 새 엔티티는 속성 설계에 실패해도 넣는다 (속성이 빈 엔티티는 검증 오류로 다시 설계한다)
    entities += [e for e in designed if e.entity_id not in by_name]
    return DataModel(entities=entities), failed


def _scope_input(model: DataModel, functions: list[FunctionSpec], feedback: str) -> str:
    """의견 반영 범위 요청의 사용자 프롬프트 (엔티티 목록 + 기능 이름 + 의견. 기능 상세는 넣지 않는다)."""
    return "\n".join([
        "## 현재 엔티티 카탈로그",
        *[f"- {e.entity_id} ({e.kind}) {e.label}: {e.description}; 상위: {', '.join(e.parents) or '없음'}"
          for e in model.entities], "",
        "## 기능 목록", *[f"- [{f.function_id}] {f.name}" for f in functions], "",
        "## 사용자 의견", feedback, "",
        "의견을 반영하려면 컬럼을 다시 설계해야 하는 기존 엔티티 name만 entity_names에 넣는다 (관계없는 엔티티는 넣지 않는다).",
        "기존 엔티티로 담을 수 없는 데이터가 생길 때만 new_entities에 추가한다 (function_ids는 기능 목록의 ID).",
    ])


# ------------------------------------------------------------------ 속성 설계 (배치·병렬)
def _design_attributes(llm, targets: list[EntityPlanOut], catalog: list[EntityPlanOut],
                       functions: list[FunctionSpec], requirements: list[Requirement],
                       model: DataModel | None = None, errors: dict[str, list[str]] | None = None,
                       context: str = "", retried: dict | None = None) -> tuple[list[Entity], dict[str, str]]:
    """엔티티 묶음별 속성 설계를 병렬로 요청하고 결과를 Entity로 모은다.

    - 묶음마다 똑같은 카탈로그·비기능 요구·결정은 공통 앞부분(shared)으로 보내 프롬프트 캐시로 재사용한다.
      캐시를 쓰는 공급자면 첫 묶음을 먼저 보내 캐시를 만든 뒤 나머지를 병렬로 보낸다.
    - 묶음 안의 엔티티들이 같은 기능을 쓰면 그 기능은 한 번만 넣는다.
    - 응답의 엔티티 이름이 조금 다르면(대소문자·공백 등) 식별자로 맞추고, 그래도 남으면 남은 것끼리 순서대로 짝짓는다
      (이름만 다르게 돌려준 응답을 다시 요청하면 출력 토큰을 한 번 더 쓴다).
    - 묶음이 실패하거나 응답에 빠진 엔티티는 하나씩 한 번 더 요청하고, 그 엔티티와 이유를 retried에 남긴다.
    (엔티티 목록, 그래도 실패한 엔티티 → 원인)을 돌려준다.
    """
    size = get_settings().attr_batch_size
    batches = [targets[i:i + size] for i in range(0, len(targets), size)]
    fn_map = {f.function_id: f for f in functions}
    shared = "\n".join([
        "## 엔티티 카탈로그 (외래키 references에 쓸 수 있는 이름)",
        *[f"- {p.name} ({p.kind}) {p.label}; 상위: {', '.join(p.parents) or '없음'}" for p in catalog],
        "", _data_nfr_block(requirements), *(["", context] if context else []),
    ])

    def design(batch: list[EntityPlanOut]) -> AttributesOut:
        parts = ["## 속성을 설계할 엔티티"]
        used: list[str] = []
        for p in batch:
            fids = [f for f in p.function_ids if f in fn_map]
            used += [f for f in fids if f not in used]
            parts += [f"### {p.name} ({p.kind}) {p.label}: {p.description}",
                      f"상위 엔티티: {', '.join(p.parents) or '없음'} | 관련 기능: {', '.join(fids) or '없음'}"]
            if model and (current := model.entity(p.name)):
                parts.append("현재 속성: " + ", ".join(
                    f"{a.name}:{a.data_type}{'→' + a.references if a.references else ''}"
                    for a in current.attributes if not a.primary_key and a.name not in AUDIT_NAMES))
            for msg in (errors or {}).get(p.name, []):
                parts.append(f"반드시 해결할 검증 오류: {msg}")
        parts += ["", "## 관련 기능", *[_function_line(fn_map[f]) for f in used]]
        if len(batch) > 1:   # 묶음 응답에서 첫 엔티티만 돌려주는 경우가 있어 끝에 한 번 더 못박는다 (재요청은 출력 낭비)
            parts += ["", f"## 응답 entities에 반드시 {len(batch)}개 모두 이 name 그대로 넣는다",
                      ", ".join(p.name for p in batch)]
        return llm.structured(system=ATTRIBUTE_PROMPT, user="\n".join(parts), schema=AttributesOut, shared=shared,
                              purpose=f"속성 설계 {','.join(p.name for p in batch)}")

    designed: dict[str, EntityAttributesOut] = {}
    failed: dict[str, str] = {}

    def collect(results) -> list[EntityPlanOut]:
        """결과를 모으고, 속성을 받지 못한 엔티티 목록을 돌려준다."""
        missing = []
        for batch, out, err in results:
            got = _match(batch, out.entities) if out is not None and not err else {}
            for p in batch:
                if p.name in got:
                    designed[p.name] = got[p.name]
                    failed.pop(p.name, None)
                else:
                    returned = ", ".join(e.name for e in out.entities) if out is not None else ""
                    failed[p.name] = (f"{type(err).__name__}: {err}" if err else
                                      f"응답에 이 엔티티의 속성이 없음 (돌려받은 이름: {returned or '없음'})")
                    missing.append(p)
        return missing

    if getattr(llm, "caches_prompts", False) and len(batches) > 1:
        missing = collect(run_parallel(design, batches[:1])) + collect(run_parallel(design, batches[1:]))
    else:
        missing = collect(run_parallel(design, batches))
    if missing:
        log.warning("[데이터모델] 속성 설계 실패 %d개 → 하나씩 다시 요청: %s", len(missing),
                    "; ".join(f"{p.name} ({failed[p.name][:120]})" for p in missing))
        if retried is not None:
            retried.update({p.name: failed[p.name][:200] for p in missing})
        collect(run_parallel(design, [[p] for p in missing]))
    entities = [_entity(p, designed.get(p.name)) for p in targets]
    return entities, {name: reason[:300] for name, reason in failed.items()}


AUDIT_NAMES = {"created_at", "updated_at"}


def _match(batch: list[EntityPlanOut], returned: list[EntityAttributesOut]) -> dict[str, EntityAttributesOut]:
    """묶음의 엔티티와 응답을 짝짓는다: 이름(식별자로 맞춤)이 같으면 그대로, 남은 것끼리는 개수가 같을 때 순서대로."""
    names = {p.name for p in batch}
    got: dict[str, EntityAttributesOut] = {}
    leftovers = []
    for ent in returned:
        key = to_identifier(ent.name)
        if key in names and key not in got and ent.attributes:
            got[key] = ent
        elif ent.attributes:
            leftovers.append(ent)
    unmatched = [p for p in batch if p.name not in got]
    if unmatched and len(unmatched) == len(leftovers):
        got.update({p.name: ent for p, ent in zip(unmatched, leftovers)})
    return got


def _entity(p: EntityPlanOut, out: EntityAttributesOut | None) -> Entity:
    """카탈로그 항목과 속성 설계 결과로 Entity를 만든다 (설계에 실패했으면 속성 없이)."""
    entity = Entity(entity_id=p.name, label=p.label, description=p.description, kind=p.kind,
                    function_ids=p.function_ids, parents=p.parents)
    if out is None:
        return entity
    return entity.model_copy(update={
        "attributes": [Attribute(**a.model_dump()) for a in out.attributes],
        "unique_together": out.unique_together, "exactly_one_of": out.exactly_one_of,
        "required_when": [RequiredWhen(**r.model_dump()) for r in out.required_when],
        "checks": [CheckRule(**c.model_dump()) for c in out.checks],
    })


# ------------------------------------------------------------------ 입력 렌더링
def _function_line(f: FunctionSpec) -> str:
    """기능 하나를 데이터 설계 입력용 한 줄로 만든다. 자르지 않는다.

    행위자로 계정·역할 구조와 시스템이 만드는 기록을, 업무 규칙으로 필수 여부·허용값·저장할 설정값을 정한다.
    """
    parts = [f"- [{f.function_id}] {f.name}", f"행위자: {', '.join(f.actors) or '없음'}",
             f"데이터: {', '.join(f.data_objects) or '없음'}"]
    if f.inputs:
        parts.append("입력: " + ", ".join(i.name for i in f.inputs))
    if f.outputs:
        parts.append("출력: " + ", ".join(o.name for o in f.outputs))
    if f.business_rules:
        parts.append("업무 규칙: " + "; ".join(f.business_rules))
    return " | ".join(parts)


def _data_nfr_block(requirements: list[Requirement]) -> str:
    """데이터·보안·인터페이스 분류의 시스템 비기능 요구사항 목록을 프롬프트용으로 만든다."""
    related = [r for r in requirements if r.type == "NON_FUNCTIONAL" and r.scope == "SYSTEM"
               and r.category in {"데이터", "보안", "인터페이스"}]
    lines = ["## 데이터 설계에 영향을 주는 비기능 요구사항"]
    lines += [f"- [{r.requirement_id}] {r.title}: {r.description}" for r in related] or ["- 없음"]
    return "\n".join(lines)


def _catalog_input(functions: list[FunctionSpec], requirements: list[Requirement]) -> str:
    """엔티티 카탈로그 요청의 사용자 프롬프트를 만든다."""
    return "\n".join(["## 기능 명세", *[_function_line(f) for f in functions], "", _data_nfr_block(requirements)])


def _dedupe_plans(plans: list[EntityPlanOut]) -> list[EntityPlanOut]:
    """이름이 겹치는 엔티티 계획은 첫 번째만 남긴다."""
    seen, out = set(), []
    for p in plans:
        if p.name not in seen:
            seen.add(p.name)
            out.append(p)
    return out


def _new_plans(plans: list[EntityPlanOut], known: set[str], taken: set[str]) -> list[EntityPlanOut]:
    """연결 보완·의견 반영에서 LLM이 낸 새 엔티티를 식별자 이름으로 맞추고, 있는 기능 ID만 남긴다.

    taken(기존 엔티티와 사용자가 지운 엔티티)과 이름이 같으면 뺀다. 'ServiceRequest'처럼 표기만 다른 기존 엔티티를
    새로 설계하면 정규화에서 중복으로 지워져 속성 설계 요청만 낭비하고, 그 기능은 엔티티 없이 남는다.
    """
    named = [p.model_copy(update={"name": to_identifier(p.name),
                                  "function_ids": [f for f in p.function_ids if f in known]}) for p in plans]
    return [p for p in _dedupe_plans(named) if p.name and p.name not in taken]


def _plans_from_model(model: DataModel) -> list[EntityPlanOut]:
    """현재 모델을 카탈로그 형태로 되돌린다 (부분 수정 때 프롬프트 입력용)."""
    plans = []
    for e in model.entities:
        # 카탈로그에서 정한 상위 엔티티 + 지금 외래키로 참조하는 엔티티 (속성이 비어도 연결 의도가 남는다)
        parents = sorted(set(e.parents) | {a.references for a in e.attributes if a.references})
        plans.append(EntityPlanOut(name=e.entity_id, label=e.label, description=e.description, kind=e.kind,
                                   function_ids=e.function_ids, parents=parents))
    return plans
