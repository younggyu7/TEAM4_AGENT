"""개발 Agent: 코드가 만든 뼈대의 정해진 자리(hole)에 기능별 업무 로직만 채운다.

토큰과 결과 흔들림을 줄이려고 LLM이 하는 일을 좁혔다.
- 파일 구조·API·DB 처리·화면은 코드가 만든다 (tools/scaffold.py). LLM은 Logic 클래스 메서드의 본문만 쓴다.
- 기능 하나에 요청 하나다. 그 기능의 규칙·API·테이블만 넣어 입력을 줄이고, 앞부분(공통 규칙)은 프롬프트 캐시로 재사용한다.
- 같은 입력이면 다시 묻지 않는다: 입력 지문이 같으면 저장한 결과(.req-agent/logic/)를 그대로 쓴다.
  스펙이 바뀌면 지문이 달라진 기능만 다시 묻는다.
- 받은 코드는 코드로 검사한다 (자리 이름·금지 표현·없는 테이블). 틀리면 오류를 붙여 한 번 다시 묻고, 그래도 틀리면 기본 동작을 두고 '실패'로 보고한다.
- 고객 답을 기다리는 규칙과 공통 규칙은 구현하지 않는다 (AGENTS.md 규칙).
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.prompts import load_prompt
from app.core.config import get_settings
from app.agents.llm.client import get_llm, run_parallel
from app.schemas.function import FunctionSpec
from app.agents.tools.dev_spec import Spec, rule_tag
from app.agents.tools.scaffold import has_backend, load_logic, method_name, owned_operations, save_logic

log = logging.getLogger(__name__)
PROMPT_VERSION = "logic-v2"
CHARS_PER_TOKEN = 2.5        # 한국어·코드가 섞인 입력의 대략값 (예상 비용 계산에만 쓴다)
OUTPUT_PER_METHOD = 450      # 메서드 하나의 코드 토큰 대략값
THINKING_FACTOR = 3          # 출력의 대부분은 생각 토큰이다 (Haiku 실측 57~81%)
PRICES = {"Haiku 5.5": (0.10, 0.50), "Sonnet 5.5": (2.0, 10.0)}   # 100만 토큰당 입력·출력 달러

SYSTEM_PROMPT = load_prompt("developer/system")


class HookOut(BaseModel):
    method: str = Field(description="채울 메서드 이름 (입력의 API 목록에 있는 이름)")
    body: str = Field(description="메서드 본문 Java 문장")


class TodoOut(BaseModel):
    rule: str = Field(description="구현하지 못한 규칙의 수용 기준 ID")
    reason: str


class LogicOut(BaseModel):
    hooks: list[HookOut] = Field(default_factory=list, description="기본 동작과 달라지는 메서드만")
    todos: list[TodoOut] = Field(default_factory=list, description="입력만으로 구현할 수 없는 규칙 (없으면 생략)")


ROLE_CHECK = re.compile(r"ctx\.user\(\)\.role\(\)")
DATE_PARSE = re.compile(r"\b(?:LocalDate|LocalDateTime|OffsetDateTime|ZonedDateTime|Instant|LocalTime)\.parse\b")
FORBIDDEN = re.compile(r"\b(import|class|interface|Runtime|ProcessBuilder|Thread|System\.exit|System\.getenv|java\.io|java\.nio|java\.net|Class\.forName)\b")
TABLE_IN_SQL = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b(?!\s*\()", re.I)   # FROM CAST(…)처럼 함수는 테이블이 아니다


def _strip_strings(code: str) -> str:
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', code)


def _strip_comments(code: str) -> str:
    """주석을 지운다 (문자열을 먼저 비운 코드에 쓴다). 한 줄에 쓴 본문 앞의 '// 가정 …'이 뒤 코드를 모두 주석으로 만든 경우를 잡는다."""
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", code, flags=re.S))


def check_hook(method: str, body: str, allowed: set[str], tables: set[str], reads: set[str] = frozenset()) -> list[str]:
    """받은 코드 검사 (컴파일 전에 코드로 볼 수 있는 것만). 오류 문장 목록. reads는 조회(목록·한 건) 메서드 이름이다."""
    errors = []
    if method not in allowed:
        errors.append(f"{method}: 입력의 API 목록에 없는 메서드")
    bare = _strip_comments(_strip_strings(body))
    if not bare.strip():
        errors.append(f"{method}: 주석만 있고 코드가 없음 (본문 문장을 주석 밖에 쓴다)")
    if m := FORBIDDEN.search(bare):
        errors.append(f"{method}: 쓸 수 없는 표현 '{m.group(1)}'")
    if bare.count("{") != bare.count("}") or bare.count("(") != bare.count(")"):
        errors.append(f"{method}: 괄호가 맞지 않음")
    if method != "run" and "return" not in bare and "throw" not in bare:
        errors.append(f"{method}: return이 없음")
    if ROLE_CHECK.search(bare):
        errors.append(f"{method}: 역할로 권한을 다시 검사함 (권한 검사는 코드가 이미 한다)")
    if DATE_PARSE.search(bare):
        errors.append(f"{method}: 날짜·시각 글자를 Java에서 파싱함 (비교·계산은 SQL의 CAST로 한다)")
    if method in reads and "crud.get()" not in bare:
        errors.append(f"{method}: 조회인데 crud.get()을 부르지 않음 (그 결과를 돌려주고 고칠 것만 고친다)")
    for sql in re.findall(r'"((?:\\.|[^"\\])*)"', body):
        for name in TABLE_IN_SQL.findall(sql):
            if name.lower() not in tables and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", sql, re.I):
                errors.append(f"{method}: 스키마에 없는 테이블 '{name}'")
    return errors


def _rules(spec: Spec, f: FunctionSpec) -> tuple[list[str], list[str]]:
    """구현할 규칙(확정·가정)과 구현하지 않을 규칙(고객 답 대기)."""
    rows = spec.acceptance[f.function_id]
    todo, hold = [], []
    for rule in f.business_rules:
        kind, text, qids = rule_tag(rule)
        ac = next((r["id"] for r in rows if r["then"] == text and r["kind"].startswith("규칙")), "")
        if kind in ("확정", "가정"):
            todo.append(f"- {ac} {'(가정) ' if kind == '가정' else ''}{text}")
        elif kind == "보류":
            hold.append(f"- {text} (답 대기: {', '.join(qids) or '확인 필요'})")
    return todo, hold


def logic_request(spec: Spec, f: FunctionSpec, ops: list[dict]) -> str:
    """기능 하나의 요청. 같은 스펙이면 글자까지 같다."""
    todo, hold = _rules(spec, f)
    parts = [f"## 기능 {f.function_id} {f.name}", f.description, f"행위자: {', '.join(f.actors)}", "", "## 구현할 규칙", *(todo or ["- 없음"])]
    if hold:
        parts += ["", "## 구현하지 않을 규칙 (TODO 주석도 쓰지 않는다. 코드가 넣는다)", *hold]
    if f.exception_flows:
        parts += ["", "## 예외 흐름", *[f"- {x.condition} → {x.handling}" for x in f.exception_flows]]
    automations = spec.automations_of(f.function_id)
    if automations:
        parts += ["", "## 구현할 자동 처리 (해당 메서드에서 crud.get() 뒤에 처리한다)"]
        parts += [f"- {a['id']} {a['name']}: {a['when']} → {a['action']} | 바꾸는 테이블: {', '.join(a['targets'])} | 실패하면: {a['on_failure'] or '전체를 되돌린다'}" for a in automations]
    parts += ["", "## 채울 메서드"]
    for op in ops:
        fields = ", ".join(x["name"] + ("*" if x.get("required") else "") for x in op.get("request") or []) or "없음"
        parts.append(f"- {method_name(op['operation_id'])}: {op['method']} {op['path']} — {op.get('summary', '')} | 입력: {fields}")
    if not ops:
        parts.append(f"- run: 자동 실행 — {(spec.api.get(f.function_id) or {}).get('no_api_reason', '')}")
    parts += ["", "## 쓸 수 있는 테이블"]
    usable = {spec.names.table(t) for t in [*spec.linked[f.function_id], *[t for a in automations for t in a["targets"]]]}
    for e in spec.ddl.entities:
        if e.entity_id in usable:
            cols = ", ".join(a.name + (f"[{'|'.join(a.allowed_values)}]" if a.allowed_values else "") for a in e.attributes)
            parts.append(f"- {e.entity_id}: {cols}")
    parts += ["", "## 이번 요청", "기본 동작과 달라지는 메서드의 본문만 hooks에 쓴다."]
    return "\n".join(parts)


def _shared(spec: Spec) -> str:
    """모든 기능에 같은 앞부분 (프롬프트 캐시). 공통 규칙은 공통 모듈이 맡으므로 알려만 준다."""
    rules = [p.get("rule", "") for p in spec.common.get("policies") or []]
    return "\n".join(["## 공통 규칙 (구현하지 않는다)", *[f"- {r}" for r in rules]]) if rules else ""


def fingerprint(spec: Spec, request: str) -> str:
    """입력 지문: 프롬프트와 요청이 같으면 결과를 다시 쓴다."""
    return hashlib.sha256("\n".join((PROMPT_VERSION, SYSTEM_PROMPT, _shared(spec), request)).encode("utf-8")).hexdigest()[:16]


def targets(spec: Spec, only: set[str] | None = None) -> list[tuple[FunctionSpec, list[dict], str]]:
    """LLM이 채울 기능 (구현할 규칙이나 예외 흐름이 있는 기능만): (기능, 만들 API, 요청). 백엔드 뼈대가 없으면 채울 자리도 없다."""
    if not has_backend(spec):
        return []
    owned, _ = owned_operations(spec)
    out = []
    for f in spec.functions:
        if only and f.function_id not in only:
            continue
        if _rules(spec, f)[0] or f.exception_flows or spec.automations_of(f.function_id):
            out.append((f, owned[f.function_id], logic_request(spec, f, owned[f.function_id])))
    return out


def plan(spec: Spec, out_dir: Path, only: set[str] | None = None) -> dict:
    """LLM 없이 해야 할 일과 예상 비용을 계산한다."""
    stored = load_logic(out_dir)
    todo, cached, stale = [], 0, []
    for f, ops, request in targets(spec, only):
        record = stored.get(f.function_id)
        if record and record.get("fingerprint") == fingerprint(spec, request) and record.get("status") == "채움":
            cached += 1
            continue
        if record and record.get("status") == "채움":
            stale.append(f.function_id)
        todo.append((f, ops, request))
    shared = len(SYSTEM_PROMPT) + len(_shared(spec))
    input_tokens = int(sum(len(r) + shared for _, _, r in todo) / CHARS_PER_TOKEN)
    output_tokens = sum(max(len(ops), 1) for _, ops, _ in todo) * OUTPUT_PER_METHOD * THINKING_FACTOR
    cost = {name: round(input_tokens / 1e6 * p[0] + output_tokens / 1e6 * p[1], 3) for name, p in PRICES.items()}
    return {"functions": len(todo), "cached": cached, "stale": stale, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost}


def _ask(llm, spec: Spec, f: FunctionSpec, ops: list[dict], request: str, tables: set[str]) -> tuple[dict, list[dict], list[str]]:
    """한 기능을 묻고 검사한다. 틀리면 오류를 붙여 한 번 더 묻는다. (채운 자리, 못 한 규칙, 남은 오류)"""
    allowed = {method_name(op["operation_id"]) for op in ops} or {"run"}
    reads = _reads(ops)
    effort = get_settings().dev_effort
    user, errors = request, []
    for attempt in range(2):
        out = llm.structured(system=SYSTEM_PROMPT, user=user, schema=LogicOut, shared=_shared(spec), effort=effort,
                             purpose=f"업무 로직 {f.function_id}")
        errors = [e for h in out.hooks for e in check_hook(h.method, h.body, allowed, tables, reads)]
        if not errors:
            return {h.method: h.body for h in out.hooks}, [t.model_dump() for t in out.todos], []
        log.warning("[개발] %s 코드 검사 실패 (시도 %d): %s", f.function_id, attempt + 1, "; ".join(errors))
        user = request + "\n\n## 이전 응답의 오류 (고쳐서 다시 쓴다)\n" + "\n".join(f"- {e}" for e in errors)
    return {}, [], errors


def _reads(ops: list[dict]) -> set[str]:
    """조회 메서드 이름 (목록·한 건): crud.get()의 응답 모양을 화면이 그대로 쓴다."""
    from app.agents.tools.api_shape import op_kind

    return {method_name(op["operation_id"]) for op in ops if op_kind(op) in ("list", "get")}


def fill_logic(spec: Spec, out_dir: Path, only: set[str] | None = None) -> dict:
    """지문이 달라진 기능만 LLM에 물어 자리를 채운다. 결과는 기능마다 저장해 다음 실행이 다시 묻지 않는다."""
    stored = load_logic(out_dir)
    llm = get_llm()
    tables = {e.entity_id for e in spec.ddl.entities}
    todo = []
    for f, ops, request in targets(spec, only):
        record = stored.get(f.function_id) or {}
        allowed = {method_name(op["operation_id"]) for op in ops} or {"run"}
        # 저장한 결과도 지금 검사로 다시 본다 (검사가 나중에 엄격해졌으면 그 기능만 다시 묻는다)
        valid = not any(check_hook(m, b, allowed, tables, _reads(ops)) for m, b in (record.get("hooks") or {}).items())
        if record.get("fingerprint") == fingerprint(spec, request) and record.get("status") == "채움" and valid:
            continue
        todo.append((f, ops, request))
    results = run_parallel(lambda t: _ask(llm, spec, t[0], t[1], t[2], tables), todo)
    filled, failed = [], []
    for (f, _, request), result, err in results:
        hooks, todos, errors = result if result else ({}, [], [str(err)])
        status = "실패" if errors else "채움"
        if errors:   # 실패해도 이전에 채운 코드는 지우지 않는다
            hooks = (stored.get(f.function_id) or {}).get("hooks") or {}
        save_logic(out_dir, f.function_id, {"fingerprint": fingerprint(spec, request), "status": status, "hooks": hooks,
                                            "todos": todos, "errors": errors, "model": getattr(llm, "model", "")})
        (failed if errors else filled).append(f.function_id)
    return {"filled": filled, "failed": failed, "usage": llm.pop_usage()}
