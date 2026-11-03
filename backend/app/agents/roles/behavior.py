"""업무 동작 설계: 시스템이 스스로 움직이는 부분을 스펙으로 정한다 (API 설계 단계에서 함께 돈다).

- 값의 출처: 등록 API의 필수 컬럼 중 요청에 없는 값과 컬럼 없는 응답 값이 어디서 오는지 정한다.
  API 설계 요청에서 LLM이 쓰고, 빠진 것은 코드가 컬럼 모양으로 채운다 (fill_sources). 토큰이 거의 늘지 않는다.
- 상태 전환·자동 처리: API가 정해진 뒤 문서 전체를 요청 1회로 본다 (기능 영역을 넘는 연쇄가 보이게).
  입력 지문이 같으면 다시 묻지 않는다.
- 지어내지 않는다: 항목마다 근거(원문·가정·확인 필요)를 달고, 답에 따라 범위가 달라지는 것은 고객 확인 질문(BQ-…)으로 넘긴다.
  답은 문서를 쓸 때 코드가 반영한다 (다시 설계하지 않는다). 없는 상태값·API·테이블을 쓴 항목은 코드가 버린다.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.prompts import load_prompt
from app.schemas.data_model import Attribute, DataModel, Entity
from app.schemas.function import FunctionSpec
from app.schemas.requirement import Question
from app.agents.tools.api_shape import (AUDIT_COLUMNS, NUMBER_COLUMN, USER_TABLE, is_status, op_kind, path_params, snake,
                                        table_of)

log = logging.getLogger(__name__)
BY = "api_designer"
PROMPT_VERSION = "behavior-v3"
NO_AUTOMATION = "자동 처리하지 않는다"
Basis = Literal["원문", "가정", "확인 필요"]
# 규칙·흐름 중 상태나 다른 업무로 이어지는 문장만 넣는다 (입력을 줄인다)
CUE = re.compile(r"자동|연동|연계|반영|참조|알림|통보|발송|생성|승인|반려|취소|해제|마감|확정|상태|주기|매월|매일|매년|월말|연말|스케줄|배치|동기화|이관")

PROMPT = load_prompt("behavior/system")


# LLM 응답 스키마: 상태 전환 하나
class TransitionOut(BaseModel):
    from_value: str = Field(description="현재 상태 (허용값 하나)")
    to_value: str = Field(description="바뀐 상태 (허용값 하나)")
    operation_id: str = Field("", description="전환을 일으키는 API (사람이 부르는 API가 없으면 생략)")
    condition: str = Field("", description="전환 조건 한 문장 (없으면 생략)")
    basis: Basis = "가정"


# LLM 응답 스키마: 상태 컬럼 하나의 전환표
class StateMachineOut(BaseModel):
    column: str = Field(description="상태 컬럼 '테이블.컬럼'")
    initial: str = Field(description="새 행의 상태 (허용값 하나)")
    transitions: list[TransitionOut] = Field(default_factory=list)
    question: str = Field("", description="확인 필요일 때 고객 질문 (없으면 생략)")
    recommended: str = Field("", description="질문의 추천안 (없으면 생략)")


# LLM 응답 스키마: 자동 처리 하나
class AutomationOut(BaseModel):
    name: str = Field(description="한국어 이름")
    trigger: Literal["API 실행", "상태 변경", "일정", "외부 수신"]
    when: str = Field(description="언제 일어나는지 한 문장")
    operation_id: str = Field("", description="trigger가 API 실행일 때 그 API")
    schedule: str = Field("", description="trigger가 일정일 때 주기 (없으면 생략)")
    targets: list[str] = Field(default_factory=list, description="바꾸는 테이블 이름")
    action: str = Field(description="무엇을 어떻게 바꾸는지")
    on_failure: str = Field("", description="실패했을 때 처리")
    basis: Basis = "가정"
    function_ids: list[str] = Field(default_factory=list, description="이 처리를 맡는 기능 ID")
    requirement_ids: list[str] = Field(default_factory=list)
    question: str = Field("", description="확인 필요일 때 고객 질문 (없으면 생략)")
    recommended: str = Field("", description="질문의 추천안 (없으면 생략)")


# LLM 응답 스키마: 문서 전체의 업무 동작
class BehaviorOut(BaseModel):
    states: list[StateMachineOut] = Field(default_factory=list)
    automations: list[AutomationOut] = Field(default_factory=list)


HISTORY_TABLE = re.compile(r"(_history|_hist|_logs?|_audit)$|(이력|로그)$")
# 어떤 처리 뒤에 오는 값: 새 행의 상태일 수 없다. 허용값은 한글도 영문 코드도 쓰인다
REVERSED_STATE = re.compile(r"해제|취소|반려|폐기|삭제|만료|종료|마감|cancel|reject|revoke|expire|delete|close|terminat|void", re.I)


def status_columns(model: DataModel) -> list[tuple[Entity, Attribute]]:
    """상태 전환표를 만들 컬럼. 이력·로그 테이블은 지난 값을 쌓을 뿐 상태가 움직이지 않으므로 뺀다."""
    return [(e, a) for e in model.entities if not (HISTORY_TABLE.search(e.entity_id) or HISTORY_TABLE.search(e.label or ""))
            for a in e.attributes if is_status(a)]


def odd_initials(data: dict | None) -> list[dict]:
    """처음 상태가 어떤 처리 뒤에 오는 값인 상태표 (고객 확인 경고로 알린다)."""
    return [s for s in (data or {}).get("states") or [] if REVERSED_STATE.search(s.get("initial") or "")]


def request(functions: list[FunctionSpec], model: DataModel, stored: dict[str, dict], common: dict) -> str:
    """요청 본문. 상태 컬럼도 상태·연쇄 규칙도 없는 문서는 빈 문자열이다 (요청하지 않는다). 같은 입력이면 글자까지 같다."""
    states = [f"- {e.entity_id}.{a.name} ({a.label or a.name}): {' | '.join(a.allowed_values)}" for e, a in status_columns(model)]
    blocks: list[str] = []
    for f in functions:
        lines = [re.sub(r"^\(([^)]*)\)\s*", r"[\1] ", r) for r in f.business_rules if CUE.search(r) and not r.startswith("(공통")]
        lines += [x for x in f.normal_flow if re.search(r"자동|연동|연계|반영|알림|통보|발송|주기", x)]
        if lines:
            blocks += [f"### [{f.function_id}] {f.name} (요구사항: {', '.join(f.requirement_ids)})", *[f"- {x}" for x in lines]]
    if not states and not blocks:
        return ""
    ops = []
    for f in functions:
        for op in (stored.get(f.function_id) or {}).get("operations") or []:
            if not op.get("shared_with") and op_kind(op) not in ("list", "get", "report"):
                ops.append(f"- {op['operation_id']}: {op['method']} {op['path']} — {op.get('summary', '')} [{f.function_id}]")
    policies = [f"- {p.get('rule', '')}" for p in common.get("policies") or []]
    return "\n".join(["## 상태 컬럼", *(states or ["- 없음"]), "", "## 데이터를 바꾸는 API", *(ops or ["- 없음"]), "",
                      "## 테이블", ", ".join(f"{e.entity_id}({e.label})" if e.label else e.entity_id for e in model.entities), "",
                      "## 기능별 규칙·흐름 (상태·연쇄에 관한 것만)", *(blocks or ["- 없음"]),
                      *(["", "## 공통 규칙", *policies] if policies else [])])


def fingerprint(text: str) -> str:
    """입력 지문: 같으면 상태 전환·자동 처리를 다시 묻지 않는다."""
    return hashlib.sha1("\n".join((PROMPT_VERSION, PROMPT, text)).encode("utf-8")).hexdigest()[:16]


def _key(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def normalize(out: BehaviorOut, model: DataModel, stored: dict[str, dict], previous: dict | None = None) -> dict:
    """응답을 저장 형식으로 정리한다: 없는 값·API·테이블을 버리고, 빠진 상태 컬럼은 미정으로 남긴다. 자동 처리 ID는 이름이 같으면 이어 쓴다."""
    operations = {op["operation_id"] for e in stored.values() for op in e.get("operations") or []}
    functions = set(stored)
    tables = {e.entity_id for e in model.entities}
    columns = {f"{e.entity_id}.{a.name}": a for e, a in status_columns(model)}
    states = []
    written: dict[str, StateMachineOut] = {}
    for item in out.states:
        written.setdefault(item.column.strip(), item)
    for column, attr in columns.items():
        item = written.get(column)
        allowed = attr.allowed_values
        if item is None:
            states.append({"column": column, "initial": allowed[0], "transitions": [], "undefined": True, "basis": "", "question": "", "recommended": ""})
            continue
        rows, seen = [], set()
        for t in item.transitions:
            pair = (t.from_value, t.to_value, t.operation_id)
            if t.from_value not in allowed or t.to_value not in allowed or t.from_value == t.to_value or pair in seen:
                log.warning("[업무 동작] 허용값이 아닌 상태 전환을 버림: %s %s → %s", column, t.from_value, t.to_value)
                continue
            seen.add(pair)
            rows.append({"from": t.from_value, "to": t.to_value, "operation_id": t.operation_id if t.operation_id in operations else "",
                         "condition": t.condition.strip(), "basis": t.basis})
        states.append({"column": column, "initial": item.initial if item.initial in allowed else allowed[0], "transitions": rows,
                       "undefined": not rows, "basis": "확인 필요" if item.question.strip() else "", "question": item.question.strip(),
                       "recommended": item.recommended.strip()})
    known = {_key(a["name"]): a["id"] for a in (previous or {}).get("automations") or []}
    number = max((int(i.split("-")[1]) for i in known.values()), default=0)
    automations = []
    for item in out.automations:
        targets = [t.strip() for t in item.targets if t.strip() in tables]
        if not targets and not item.question.strip():
            log.warning("[업무 동작] 대상 테이블이 없는 자동 처리를 버림: %s", item.name)
            continue
        key = _key(item.name)
        if key in {_key(a["name"]) for a in automations}:
            continue
        if key not in known:
            number += 1
        automations.append({"id": known.get(key) or f"AUTO-{number:03d}", "name": item.name.strip(), "trigger": item.trigger, "when": item.when.strip(),
                            "operation_id": item.operation_id if item.operation_id in operations else "", "schedule": item.schedule.strip(), "targets": targets,
                            "action": item.action.strip(), "on_failure": item.on_failure.strip(), "basis": item.basis,
                            "function_ids": [f for f in item.function_ids if f in functions], "requirement_ids": item.requirement_ids,
                            "question": item.question.strip(), "recommended": item.recommended.strip()})
    return {"states": states, "automations": automations}


def attach_questions(data: dict, questions: list[Question], ask: bool, limit: int) -> list[Question]:
    """'확인 필요' 항목의 고객 질문을 만든다 (BQ-…). 같은 질문은 다시 만들지 않고, 한도를 넘으면 질문 없이 확인 필요로 남긴다."""
    existing = {_key(q.question): q for q in questions if q.question_id.startswith("BQ-")}
    number = max((int(q.question_id.split("-")[1]) for q in existing.values()), default=0)
    new: list[Question] = []
    for item in [*data["states"], *data["automations"]]:
        text = item.pop("question", "")
        recommended = item.pop("recommended", "")
        item["question_id"] = ""
        if not text:
            continue
        found = existing.get(_key(text))
        if found is None:
            if len(new) >= limit:
                continue
            number += 1
            is_auto = "action" in item
            found = Question(question_id=f"BQ-{number:03d}", kind="MISSING", question=text, stage=BY, blocking=True,
                             context=(item.get("action") or item.get("column", "")), recommended=recommended,
                             options=[x for x in (recommended, NO_AUTOMATION if is_auto else "") if x],
                             requirement_ids=item.get("requirement_ids") or [], status="PENDING" if ask else "DEFERRED")
            new.append(found)
            existing[_key(text)] = found
        item["question_id"] = found.question_id
    return new


def _guess(a: Attribute, target: str) -> dict:
    """요청에 없는 필수 컬럼의 출처를 컬럼 모양으로 정한다 (LLM 없음). 알 수 없으면 미정."""
    entry = {"target": target, "basis": "가정", "value": ""}
    if a.allowed_values:
        return {**entry, "kind": "고정값", "value": a.allowed_values[0]}
    if a.references and USER_TABLE.search(a.references):
        return {**entry, "kind": "현재 사용자"}
    if a.data_type in ("date", "timestamp"):
        return {**entry, "kind": "현재 시각"}
    if a.data_type == "boolean":
        return {**entry, "kind": "고정값", "value": "false"}
    if a.data_type in ("integer", "decimal") or (a.data_type == "bigint" and not a.references):
        return {**entry, "kind": "고정값", "value": "0"}
    if a.data_type == "varchar" and NUMBER_COLUMN.search(a.name):
        return {**entry, "kind": "자동 번호"}
    return {**entry, "kind": "미정", "basis": "확인 필요"}


def fill_sources(stored: dict[str, dict], model: DataModel, order: list[str]) -> None:
    """API마다 값의 출처를 완성한다: 없는 대상은 버리고, 등록 API의 필수 컬럼과 컬럼 없는 응답 값이 빠지지 않게 채운다."""
    by_id = {e.entity_id: e for e in model.entities}
    columns = {f"{e.entity_id}.{a.name}" for e in model.entities for a in e.attributes}
    for fid in order:
        for op in (stored.get(fid) or {}).get("operations") or []:
            computed = [f["name"] for f in op.get("response") or [] if not f.get("column")]
            sources = [s for s in op.get("sources") or [] if s["target"] in columns or s["target"] in computed]
            known = {s["target"] for s in sources}
            table = table_of(op, model.entities)
            if op_kind(op) in ("create", "add") and table in by_id:
                given = {f["column"].split(".", 1)[1] for f in op.get("request") or [] if f.get("column", "").startswith(f"{table}.")}
                given |= {snake(p) for p in path_params(op["path"])}
                for a in by_id[table].attributes:
                    target = f"{table}.{a.name}"
                    if not (a.nullable or a.primary_key or a.name in given or a.name in AUDIT_COLUMNS or target in known):
                        sources.append(_guess(a, target))
            sources += [{"target": name, "kind": "미정", "value": "", "basis": "확인 필요"} for name in computed if name not in known]
            op["sources"] = sources


def undefined_sources(stored: dict[str, dict]) -> dict[str, list[str]]:
    """기능 ID → 출처가 정해지지 않은 '메서드 경로: 대상'."""
    out: dict[str, list[str]] = {}
    for fid, entry in stored.items():
        for op in entry.get("operations") or []:
            if op.get("shared_with"):
                continue
            items = [s["target"] for s in op.get("sources") or [] if s["kind"] == "미정"]
            if items:
                out.setdefault(fid, []).append(f"{op['method']} {op['path']}: {', '.join(items)}")
    return out


def resolved(data: dict | None, questions: dict[str, Question]) -> dict:
    """고객 답을 반영한 업무 동작 (문서를 쓸 때). 답한 항목은 근거가 결정이 되고, '자동 처리하지 않는다'는 제외한다."""
    data = data or {}

    def apply(item: dict) -> dict:
        q = questions.get(item.get("question_id") or "")
        if not (q and q.status == "ANSWERED" and q.answer):
            return item
        return {**item, "basis": "제외" if q.answer.strip() == NO_AUTOMATION else "결정", "answer": q.answer.strip()}

    return {"states": [apply(x) for x in data.get("states") or []], "automations": [apply(x) for x in data.get("automations") or []]}


