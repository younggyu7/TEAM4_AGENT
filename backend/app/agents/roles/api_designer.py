"""API 설계 Agent: 기능 명세·데이터 모델 → 기능별 API와 아키텍처 요약 (개발 스펙의 API.md·openapi.yaml·ARCHITECTURE.md).

데이터 설계와 같은 버전에서 돈다 (팀 결정 10-29: API 단계 때문에 버전 번호를 한 자리 더 깊게 만들지 않는다).
추적 검토가 기능→엔티티 연결을 맞춘 뒤에 돌고, 의미 검토 전에 끝난다.
- API: 기능을 API_BATCH_SIZE개씩 묶어 병렬로 요청한다. 묶음에는 그 기능이 쓰는 테이블과 부모 테이블의 컬럼만 넣는다.
  LLM은 메서드·경로·권한·필드와 컬럼 연결·오류만 정하고, 필드 타입·길이·허용값은 문서를 쓸 때 연결한 컬럼에서 코드가 정한다
  (DB와 항상 같게).
- 기능 지문(fingerprint): 기능 명세와 연결 테이블 구조가 바뀐 기능만 다시 설계한다 (rerun data_modeler·DB 검증 수정 뒤에도).
- 코드 검사: 기능마다 API(또는 API가 필요 없는 이유)가 있는지, 연결한 컬럼이 데이터 모델에 있는지 → 오류는 API_* 이슈로 남기고
  조정 Agent가 그 기능만 다시 설계하게 한다 (mode=fix). 같은 메서드·경로를 두 기능이 쓰면 경고만 남긴다.
- 아키텍처: 시스템 비기능 요구사항과 기능 영역으로 요청 1회. 원문이 개발 언어·프레임워크를 정하지 않았으면 확인 질문 TQ-001
  (선택지 3개 + 추천)을 낸다. 답 전에는 추천안을 가정으로 쓰고, 답은 문서를 쓸 때 코드가 반영한다 (다시 설계하지 않는다).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.prompts import load_prompt
from app.core.config import get_settings
from app.agents.roles.function_designer import SYSTEM_ACTOR, role_names
from app.agents.llm.client import get_llm, run_parallel
from app.schemas.data_model import DataModel, Entity
from app.schemas.document import RequirementSource
from app.schemas.function import FunctionSpec
from app.schemas.requirement import Question, Requirement
from app.agents.flow.state import AgentState, dump, finding, get_data_model, issues_of, load, merge_usage, reconcile_issues
from app.agents.tools.model_validator import to_identifier

log = logging.getLogger(__name__)
BY = "api_designer"
STACK_QUESTION = "TQ-001"
AUDIT_COLUMNS = {"created_at", "updated_at"}
# 원문이 개발 언어·프레임워크를 정하지 않았을 때 묻는 선택지 (DB는 DDL을 검증한 PostgreSQL)
STACK_OPTIONS = (
    "Java 17 · Spring Boot 3 · PostgreSQL 16",
    "Python 3.12 · FastAPI · PostgreSQL 16",
    "TypeScript · NestJS · PostgreSQL 16",
)
STACK_NOTES = (
    "공공 SI에서 많이 쓰는 Spring 계열 (전자정부 표준프레임워크와 같은 기반)",
    "AI·데이터 분석 처리가 많은 사업",
    "웹 화면과 같은 언어(TypeScript)로 맞출 때",
)
STACK_LAYERS = ("언어", "프레임워크")   # 이 층을 원문이 정했으면 묻지 않는다
# 기능 명세 초안에서 원문이 언어·프레임워크를 정했는지 보는 단어 (LLM 없이 기술 스택 질문을 낼지 정한다)
STACK_WORDS = re.compile(r"java|spring|전자\s*정부|egov|python|django|fastapi|flask|node\.?js|nest\.?js|typescript"
                         r"|\.net|c#|php|laravel|ruby on rails|kotlin", re.I)

API_PROMPT = load_prompt("api_designer/api")

ARCH_PROMPT = load_prompt("api_designer/architecture")

FieldType = Literal["string", "integer", "number", "boolean", "object", "array"]


# LLM 응답 스키마: API 요청·응답 필드 (column이 있으면 타입은 컬럼에서 코드가 정한다)
class ApiFieldOut(BaseModel):
    name: str = Field(description="영문 camelCase 필드 이름")
    column: str = Field("", description="연결한 컬럼 '테이블.컬럼' (컬럼이 아니면 생략)")
    type: FieldType = Field("string", description="column이 없을 때만")
    required: bool = True
    description: str = Field("", description="한국어 한 줄 (이름만으로 충분하면 생략)")


# LLM 응답 스키마: 기능별 오류 (예외 흐름에서)
class ApiErrorOut(BaseModel):
    status: int = Field(description="404·409·422 등")
    code: str = Field(description="영문 대문자 snake 오류 코드")
    condition: str = Field(description="오류가 나는 조건 (예외 흐름)")


# LLM 응답 스키마: API 하나
class ApiOperationOut(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    operation_id: str
    summary: str = Field(description="한국어 한 줄")
    actors: list[str]
    request: list[ApiFieldOut] = Field(default_factory=list)
    response: list[ApiFieldOut] = Field(default_factory=list)
    list_response: bool = False
    errors: list[ApiErrorOut] = Field(default_factory=list)


# LLM 응답 스키마: 기능 하나의 API (API가 필요 없으면 이유)
class FunctionApiOut(BaseModel):
    function_id: str
    operations: list[ApiOperationOut] = Field(default_factory=list)
    no_api_reason: str = Field("", description="API가 필요 없는 기능이면 이유 (아니면 생략)")


# LLM 응답 스키마: 묶음의 API 설계 결과
class ApiDesignOut(BaseModel):
    functions: list[FunctionApiOut]


# LLM 응답 스키마: 아키텍처 항목 하나
class ArchItemOut(BaseModel):
    name: str
    detail: str
    requirement_ids: list[str]


# LLM 응답 스키마: 원문이 정한 기술
class StackMentionOut(BaseModel):
    layer: Literal["언어", "프레임워크", "DBMS", "WAS", "OS", "프런트엔드", "기타"]
    value: str = Field(description="원문 표현 그대로")
    requirement_ids: list[str]


# LLM 응답 스키마: 아키텍처 요약 (모듈은 기능 그룹으로 코드가 만든다)
class ArchitectureOut(BaseModel):
    summary: str
    integrations: list[ArchItemOut] = Field(default_factory=list)
    security: list[ArchItemOut] = Field(default_factory=list)
    performance: list[ArchItemOut] = Field(default_factory=list)
    constraints: list[ArchItemOut] = Field(default_factory=list)
    stack_in_source: list[StackMentionOut] = Field(default_factory=list, description="원문이 정한 기술만 (없으면 생략)")
    recommended_stack: int = Field(1, description="원문이 언어·프레임워크를 정하지 않았을 때 추천 선택지 번호 (1~3)")
    recommend_reason: str = Field("", description="추천 이유 한 문장")


# ------------------------------------------------------------------ 조정 Agent가 쓰는 판단
def enabled(state: AgentState) -> bool:
    """API 설계 Agent를 쓰는지 (API_DESIGN=false 또는 options.api_design=false면 끔)."""
    return bool((state.get("options") or {}).get("api_design", get_settings().api_design))


def fingerprint(f: FunctionSpec, model: DataModel) -> str:
    """기능 명세와 연결 테이블 구조의 지문. 바뀐 기능만 API를 다시 설계한다.

    업무 규칙 앞 표시('(확인 필요 FQ-001)' → '(결정 FQ-001)')만 바뀐 것은 API에 영향이 없어 뺀다.
    """
    spec = {"name": f.name, "actors": f.actors, "pre": f.preconditions,
            "inputs": [i.model_dump(mode="json") for i in f.inputs],
            "outputs": [o.model_dump(mode="json") for o in f.outputs],
            "rules": [re.sub(r"^\([^)]*\)\s*", "", r) for r in f.business_rules], "flow": f.normal_flow,
            "exceptions": [x.model_dump(mode="json") for x in f.exception_flows]}
    tables = [{"name": e.entity_id, "columns": [[a.name, a.data_type, a.length, a.nullable, a.primary_key, a.references,
                                                  a.allowed_values] for a in e.attributes]}
              for e in sorted(model.entities, key=lambda e: e.entity_id) if f.function_id in e.function_ids]
    return hashlib.sha1(json.dumps([spec, tables], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def pending(state: AgentState) -> list[str]:
    """API를 아직 설계하지 않았거나 기능·연결 테이블이 바뀐 기능 ID (꺼져 있거나 데이터 모델이 없으면 빈 목록)."""
    model = get_data_model(state)
    if not enabled(state) or model is None or not state.get("functions"):
        return []
    prints = (state.get("api_design") or {}).get("fingerprints") or {}
    return [f.function_id for f in load(state, "functions", FunctionSpec) if prints.get(f.function_id) != fingerprint(f, model)]


def architecture_stale(state: AgentState) -> bool:
    """비기능 요구사항·기능 영역이 바뀐 뒤 아키텍처 요약을 아직 다시 만들지 않았는지."""
    if not enabled(state) or get_data_model(state) is None or not state.get("functions"):
        return False
    return (state.get("api_design") or {}).get("arch_fingerprint") != _arch_fingerprint(
        load(state, "requirements", Requirement), load(state, "functions", FunctionSpec))


# ------------------------------------------------------------------ 노드
def run(state: AgentState) -> dict:
    """API 설계 Agent 노드: 바뀐 기능(또는 검사 오류가 난 기능)의 API와, 필요하면 아키텍처 요약을 설계한다."""
    s = get_settings()
    action = state.get("next_action") or {}
    functions = load(state, "functions", FunctionSpec)
    by_fid = {f.function_id: f for f in functions}
    model = get_data_model(state)
    requirements = load(state, "requirements", Requirement)
    questions = load(state, "questions", Question)
    design = dict(state.get("api_design") or {})
    # 지운 기능의 API는 버린다
    stored = {k: v for k, v in (design.get("functions") or {}).items() if k in by_fid}
    prints = {k: v for k, v in (design.get("fingerprints") or {}).items() if k in stored}

    errors: dict[str, list[str]] = {}
    if action.get("mode") == "fix":
        targets = [fid for fid in dict.fromkeys(action.get("scope") or []) if fid in by_fid]
        for i in issues_of(state):
            if i.status == "OPEN" and i.target_agent == BY and i.target_id in targets:
                errors.setdefault(i.target_id, []).append(i.message)
    else:
        targets = pending(state)
    batch_size = max(1, s.api_batch_size)
    batches = [targets[i:i + batch_size] for i in range(0, len(targets), batch_size)]
    jobs: list[tuple[str, list[str]]] = [("api", b) for b in batches]
    arch_needed = architecture_stale(state)
    if arch_needed:
        jobs.append(("arch", []))

    llm = get_llm()
    sources = {src.source_key: src.name for src in load(state, "sources", RequirementSource)}

    def call(job: tuple[str, list[str]]):
        kind, ids = job
        if kind == "arch":
            return llm.structured(system=ARCH_PROMPT, user=_arch_prompt(requirements, functions, sources),
                                  schema=ArchitectureOut, effort=s.api_effort, purpose="아키텍처 요약")
        others = sorted({f"- {op['method']} {op['path']}" for fid, entry in stored.items() if fid not in ids
                         for op in entry.get("operations") or []})
        return llm.structured(system=API_PROMPT, user=_batch_prompt([by_fid[i] for i in ids], model, others, errors),
                              schema=ApiDesignOut, effort=s.api_effort, purpose=f"API 설계 {ids[0]} 외 {len(ids) - 1}개")

    failed = 0
    architecture = design.get("architecture")
    arch_print = design.get("arch_fingerprint")
    for (kind, ids), out, err in run_parallel(call, jobs):
        if err or out is None:
            failed += 1
            log.warning("[API 설계] %s 요청 실패: %s", "아키텍처" if kind == "arch" else ", ".join(ids), err)
            continue
        if kind == "arch":
            architecture = out.model_dump(mode="json")
            arch_print = _arch_fingerprint(requirements, functions)
            continue
        got = {to_key(x.function_id): x for x in out.functions}
        for fid in ids:
            # 응답에서 빠진 기능은 빈 설계로 두어 검사 오류(API_FUNCTION_WITHOUT_API)로 다시 설계한다 (무한 재요청 방지)
            stored[fid] = _normalize(got.get(to_key(fid)) or FunctionApiOut(function_id=fid), by_fid[fid])
            prints[fid] = fingerprint(by_fid[fid], model)
    stored = _unique_operation_ids(stored, [f.function_id for f in functions])
    roles = role_names(state.get("function_common") or {})
    for entry in stored.values():   # 권한은 공통 결정의 사용자 역할 이름으로 (시스템·외부 시스템은 권한이 아니다)
        for op in entry.get("operations") or []:
            op["actors"] = list(dict.fromkeys(roles.get(re.sub(r"\s+", "", a), a) for a in op["actors"]
                                              if not SYSTEM_ACTOR.match(a)))
    moved = _split_routes(stored, [f.function_id for f in functions], {e.entity_id for e in model.entities})

    ask =(state.get("options") or {}).get("ask_mode", "ask") == "ask"
    new_question = _stack_question(ArchitectureOut.model_validate(architecture), questions, ask) if architecture else None

    findings = check(stored, functions, model)
    issues = reconcile_issues(issues_of(state), findings, lambda k: k.startswith("API_"), s.fix_attempts)
    ops = sum(len(e.get("operations") or []) for e in stored.values())
    batch_fns = sum(1 for e in stored.values() if not e.get("operations") and e.get("no_api_reason"))
    summary = (f"API {ops}개 (기능 {len(stored)}/{len(functions)}개, 이번에 설계 {len(targets)}개"
               + (f", 배치·자동 실행 {batch_fns}개" if batch_fns else "") + ")"
               + (", 아키텍처 요약" if arch_needed and arch_print == _arch_fingerprint(requirements, functions) else "")
               + (f", 기술 스택 질문 {STACK_QUESTION}" if new_question else "")
               + (f", 겹친 경로 나눔 {len(moved)}개" if moved else "")
               + (f", 요청 실패 {failed}" if failed else ""))
    for line in moved:
        log.info("[API 설계] 경로 나눔: %s", line)
    log.info("[API 설계] %s", summary)
    design.update({"functions": stored, "fingerprints": prints, "architecture": architecture,
                   "arch_fingerprint": arch_print, "last": summary})
    update: dict = {"api_design": design, "issues": [i.model_dump() for i in issues],
                    "llm_usage": merge_usage(state, llm.pop_usage())}
    if new_question:
        update["questions"] = dump(questions + [new_question])
    if failed:
        update["failures"] = {**(state.get("failures") or {}), BY: (state.get("failures") or {}).get(BY, 0) + 1}
    return update


def to_key(function_id: str) -> str:
    """응답의 기능 ID를 맞춰 보는 키 (공백·대소문자 차이 무시)."""
    return re.sub(r"\s+", "", function_id or "").upper()


# ------------------------------------------------------------------ 정리·검사
def _normalize(out: FunctionApiOut, f: FunctionSpec) -> dict:
    """응답을 저장 형식으로 정리한다: 경로 앞 /·공통 접두어, 메서드 대문자, 필드 이름·컬럼 표기, 오류 코드 형식."""
    operations = []
    for n, op in enumerate(out.operations, start=1):
        path = "/" + re.sub(r"\s+", "", op.path).strip("/")
        path = re.sub(r"^/api(/v\d+)?(?=/|$)", "", path) or "/"
        fields = {"request": _fields(op.request), "response": _fields(op.response)}
        errors = [{"status": e.status if 400 <= e.status <= 599 else 422,
                   "code": re.sub(r"[^A-Z0-9]+", "_", e.code.upper()).strip("_") or "BUSINESS_RULE_VIOLATION",
                   "condition": e.condition.strip()} for e in op.errors]
        operations.append({"method": op.method, "path": path, "operation_id": _operation_id(op.operation_id, op.method, f, n),
                           "summary": op.summary.strip() or f.name, "actors": [a.strip() for a in op.actors if a.strip()],
                           **fields, "list_response": op.list_response, "errors": errors})
    return {"function_id": f.function_id, "operations": operations,
            "no_api_reason": "" if operations else out.no_api_reason.strip()}


def _fields(items: list[ApiFieldOut]) -> list[dict]:
    """필드 이름을 camelCase 식별자로 맞추고(겹치면 뒤 것을 버림) 컬럼 표기를 '테이블.컬럼' 식별자로 맞춘다."""
    out, seen = [], set()
    for item in items:
        table, _, column = item.column.strip().partition(".")
        column_ref = f"{to_identifier(table)}.{to_identifier(column)}" if table and column else ""
        name = _camel(item.name) or (_camel(column_ref.split(".")[-1].replace("_", " ")) if column_ref else "")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "column": column_ref, "type": item.type, "required": item.required,
                    "description": item.description.strip()})
    return out


def _camel(text: str) -> str:
    """'serviceRequestId'·'service request id' → camelCase 식별자. 만들 수 없으면 빈 문자열."""
    if re.fullmatch(r"[a-z][A-Za-z0-9]*", text or ""):
        return text
    words = [w for w in re.split(r"[^A-Za-z0-9]+", text or "") if w]
    if not words or not words[0][0].isalpha():
        return ""
    return words[0][0].lower() + words[0][1:] + "".join(w[:1].upper() + w[1:] for w in words[1:])


def _operation_id(value: str, method: str, f: FunctionSpec, n: int) -> str:
    """응답의 operation_id가 식별자가 아니면 메서드 + 기능 ID로 만든다 (예: postFnSfr00101)."""
    if re.fullmatch(r"[a-z][A-Za-z0-9]*", value or ""):
        return value
    base = method.lower() + "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", f.function_id) if p)
    return base + (str(n) if n > 1 else "")


def _unique_operation_ids(stored: dict[str, dict], order: list[str]) -> dict[str, dict]:
    """operation_id가 기능끼리 겹치면 기능 순서상 뒤의 것에 번호를 붙인다 (openapi.yaml에서 유일해야 한다)."""
    seen: set[str] = set()
    for fid in order:
        for op in (stored.get(fid) or {}).get("operations") or []:
            name, n = op["operation_id"], 1
            while name in seen:
                n += 1
                name = f"{op['operation_id']}{n}"
            op["operation_id"] = name
            seen.add(name)
    return stored


def _collection(table: str) -> str:
    """테이블 이름 → 경로 조각 (복수형 kebab-case): approval_document_instance → approval-document-instances."""
    name = table.replace("_", "-")
    if re.search(r"(s|x|z|ch|sh)$", name):
        return name + "es"
    if re.search(r"[^aeiou]y$", name):
        return name[:-1] + "ies"
    return name + "s"


def _segment_table(op: dict, i: int, tables: set[str]) -> str:
    """경로 조각 i가 가리키는 테이블: 바로 뒤가 경로 변수(…Id)면 그 테이블, 아니면 요청·응답 컬럼에 가장 많이 나온 테이블."""
    segs = op["path"].strip("/").split("/")
    var = re.fullmatch(r"\{([A-Za-z][A-Za-z0-9]*)\}", segs[i + 1]) if i + 1 < len(segs) else None
    if var:
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", var.group(1)).lower()
        if snake.endswith("_id") and snake[:-3] in tables:
            return snake[:-3]
    counts = Counter(x["column"].split(".")[0] for x in op["request"] + op["response"] if x["column"])
    return max(counts, key=counts.__getitem__) if counts else ""   # 같으면 먼저 나온 테이블


def _split_routes(stored: dict[str, dict], order: list[str], tables: set[str]) -> list[str]:
    """여러 기능이 같은 메서드·경로를 쓰면 코드로 정리한다 (LLM 없음, 같은 입력이면 같은 결과). 옮긴 경로 목록을 돌려준다.

    - 다루는 테이블이 다르면 다른 자원이다: 경로 조각과 이름이 맞는 테이블(없으면 기능 순서상 앞)은 그대로 두고,
      다른 테이블의 API는 그 조각을 자기 테이블 이름의 복수형으로 옮긴다. 같은 조각 아래 같은 테이블의 다른 API도 함께 옮긴다.
    - 같은 테이블이면 한 API다: 뒤 기능의 API에 shared_with(앞 API의 operation_id)를 달아 한 번만 만들게 한다.
    """
    ops = [(fid, op) for fid in order for op in (stored.get(fid) or {}).get("operations") or []]
    for _, op in ops:
        op.pop("shared_with", None)
    collections = {_collection(t): t for t in tables}
    moved: list[str] = []
    for _ in range(3):   # 옮긴 경로가 다시 겹치면 한 번 더
        routes: dict[tuple[str, str], list[tuple[str, dict]]] = {}
        for fid, op in ops:
            routes.setdefault((op["method"], op["path"]), []).append((fid, op))
        changed = False
        for (method, path), users in sorted(routes.items()):
            if len({fid for fid, _ in users}) < 2:
                continue
            segs = path.strip("/").split("/")
            static = [i for i, s in enumerate(segs) if not s.startswith("{")]
            if not static:
                continue
            i = next((i for i in static if segs[i] in collections), static[0])
            prefix = segs[:i]
            owner = collections.get(segs[i]) or _segment_table(users[0][1], i, tables)
            for t in dict.fromkeys(_segment_table(op, i, tables) for _, op in users):
                if not t or t == owner:
                    continue
                new = _collection(t)
                for _, op in ops:
                    s = op["path"].strip("/").split("/")
                    if len(s) > i and s[:i] == prefix and s[i] == segs[i] and _segment_table(op, i, tables) == t:
                        old = op["path"]
                        op["path"] = "/" + "/".join([*prefix, new, *s[i + 1:]])
                        moved.append(f"{op['method']} {old} → {op['path']} ({t})")
                        changed = True
        if not changed:
            break
    routes = {}
    for fid, op in ops:
        routes.setdefault((op["method"], op["path"]), []).append((fid, op))
    for users in routes.values():
        first_fid, first = users[0]
        for fid, op in users[1:]:
            if fid != first_fid:
                op["shared_with"] = first["operation_id"]
    return moved


def check(stored: dict[str, dict], functions: list[FunctionSpec], model: DataModel):
    """API 설계 결과 검사: 기능마다 API(또는 이유)가 있는지, 연결한 컬럼이 있는지, 메서드·경로가 겹치는지."""
    columns = {(e.entity_id, a.name) for e in model.entities for a in e.attributes}
    found = []
    routes: dict[tuple[str, str], list[str]] = {}
    for f in functions:
        entry = stored.get(f.function_id)
        if entry is None or (not entry.get("operations") and not entry.get("no_api_reason")):
            found.append(finding("API_FUNCTION_WITHOUT_API", "ERROR", f"{f.function_id} '{f.name}'의 API가 설계되지 않음",
                                 BY, f.function_id, BY))
            continue
        unknown = sorted({fld["column"] for op in entry.get("operations") or [] for fld in op["request"] + op["response"]
                          if fld["column"] and tuple(fld["column"].split(".", 1)) not in columns})
        if unknown:
            found.append(finding("API_UNKNOWN_COLUMN", "ERROR",
                                 f"{f.function_id} '{f.name}'의 API가 데이터 모델에 없는 컬럼을 씀: {', '.join(unknown)}",
                                 BY, f.function_id, BY))
        for op in entry.get("operations") or []:
            routes.setdefault((op["method"], op["path"]), []).append(f.function_id)
    for (method, path), fids in sorted(routes.items()):
        if len(set(fids)) > 1:   # 다른 테이블이면 _split_routes가 경로를 나눴으므로 남은 것은 같은 테이블을 다루는 API
            found.append(finding("API_DUPLICATE_ROUTE", "INFO",
                                 f"{method} {path}를 기능 {', '.join(dict.fromkeys(fids))}가 함께 씀 → 한 API로 만들고 함께 씀 "
                                 "(같은 테이블, openapi.yaml에는 앞 기능의 API 하나)",
                                 target_id=f"{method} {path}", raised_by=BY))
    return found


# ------------------------------------------------------------------ 기술 스택 질문
def stack_question(requirements: list[Requirement], questions: list[Question], ask: bool) -> Question | None:
    """기능 명세 초안의 기술 스택 확인 질문 (LLM 없음). 화면 기능 명세 '확인 필요'에 필수로 나온다.

    요구사항 제목·설명에 개발 언어·프레임워크 이름이 없으면 선택지 3개 + 추천(1번)으로 묻는다 (이미 있으면 다시 묻지 않는다).
    답은 'ERD 최종 확정'의 개발 스펙 문서에만 쓰이고 기능 명세·ERD·API를 다시 설계하지 않는다.
    원문에 이름이 있으면 묻지 않고, 최종 확정의 아키텍처 요약이 원문 스택을 찾는다."""
    if any(q.question_id == STACK_QUESTION for q in questions):
        return None
    if any(STACK_WORDS.search(f"{r.title} {r.description}") for r in requirements):
        return None
    notes = "\n".join(f"{i}. {o} — {n}" for i, (o, n) in enumerate(zip(STACK_OPTIONS, STACK_NOTES), start=1))
    context = ("원문에서 개발 언어·프레임워크를 찾지 못했습니다. 최종 확정 때 만드는 개발 스펙(AGENTS·ARCHITECTURE·TASKS)에 "
               "쓸 기술 스택을 정해 주세요. 답은 문서에만 쓰이고 기능 명세·ERD를 다시 설계하지 않습니다.\n" + notes)
    return Question(question_id=STACK_QUESTION, kind="MISSING", question="개발에 쓸 기술 스택을 무엇으로 할까요?",
                    context=context, options=list(STACK_OPTIONS), requirement_ids=[], blocking=True, stage=BY,
                    recommended=STACK_OPTIONS[0], status="PENDING" if ask else "DEFERRED")


def _stack_question(arch: ArchitectureOut, questions: list[Question], ask: bool) -> Question | None:
    """원문이 개발 언어·프레임워크를 정하지 않았으면 기술 스택 확인 질문을 만든다 (이미 있으면 다시 묻지 않는다)."""
    if any(q.question_id == STACK_QUESTION for q in questions):
        return None
    if any(m.layer in STACK_LAYERS for m in arch.stack_in_source):
        return None
    pick = STACK_OPTIONS[min(max(arch.recommended_stack, 1), len(STACK_OPTIONS)) - 1]
    known = "; ".join(f"{m.layer} {m.value}" for m in arch.stack_in_source) or "없음"
    notes = "\n".join(f"{i}. {o} — {n}" for i, (o, n) in enumerate(zip(STACK_OPTIONS, STACK_NOTES), start=1))
    context = (f"원문에 개발 언어·프레임워크가 정해져 있지 않습니다 (원문의 기술 정보: {known}). "
               f"개발 AI가 쓸 기술 스택을 정해 주세요. 추천 이유: {arch.recommend_reason.strip() or '-'}\n{notes}")
    return Question(question_id=STACK_QUESTION, kind="MISSING", question="개발에 쓸 기술 스택을 무엇으로 할까요?",
                    context=context, options=list(STACK_OPTIONS),
                    requirement_ids=sorted({rid for m in arch.stack_in_source for rid in m.requirement_ids}),
                    blocking=True, stage=BY, recommended=pick, status="PENDING" if ask else "DEFERRED")


def stack_choices() -> list[dict]:
    """화면에서 고를 기술 스택 선택지 (추천 순서, 이유). 직접 입력도 받는다."""
    return [{"stack": o, "note": n} for o, n in zip(STACK_OPTIONS, STACK_NOTES)]


def answer_stack(questions: list[Question], answer: str) -> list[Question]:
    """사람이 정한 기술 스택을 TQ-001의 답으로 남긴다 (질문이 없던 버전이면 만들어서). 새 목록을 돌려준다."""
    out = [q.model_copy() for q in questions]
    q = next((q for q in out if q.question_id == STACK_QUESTION), None)
    if q is None:
        q = Question(question_id=STACK_QUESTION, kind="MISSING", question="개발에 쓸 기술 스택을 무엇으로 할까요?",
                     context="최종 산출물 화면에서 사람이 정한 기술 스택", options=list(STACK_OPTIONS), requirement_ids=[],
                     blocking=True, stage=BY, recommended=STACK_OPTIONS[0])
        out.append(q)
    q.status, q.answer = "ANSWERED", answer
    return out


def tech_stack(design: dict | None, questions: list[Question]) -> dict:
    """개발 스펙에 쓸 기술 스택: 원문이 정했으면 원문, 아니면 질문의 답(확정), 답 전이면 추천안(가정).

    {"backend": 백엔드 스택, "basis": 원문|확정|가정|없음, "question": 질문 ID, "source": [(층, 원문 표현, 근거 ID)]}
    """
    arch = (design or {}).get("architecture")
    if not arch:
        return {"backend": "", "basis": "없음", "question": "", "source": []}
    a = ArchitectureOut.model_validate(arch)
    source = [(m.layer, m.value, m.requirement_ids) for m in a.stack_in_source]
    if any(m.layer in STACK_LAYERS for m in a.stack_in_source):
        return {"backend": " · ".join(m.value for m in a.stack_in_source if m.layer in STACK_LAYERS), "basis": "원문",
                "question": "", "source": source}
    q = next((q for q in questions if q.question_id == STACK_QUESTION), None)
    if q and q.status == "ANSWERED" and q.answer:
        return {"backend": q.answer, "basis": "확정", "question": q.question_id, "source": source}
    pick = q.recommended if q else STACK_OPTIONS[min(max(a.recommended_stack, 1), len(STACK_OPTIONS)) - 1]
    return {"backend": pick, "basis": "가정", "question": q.question_id if q else "", "source": source}


# ------------------------------------------------------------------ 입력 렌더링
def _table_line(e: Entity) -> str:
    """테이블 한 줄: 컬럼 이름·타입·길이·키·필수·참조·허용값."""
    cols = []
    for a in e.attributes:
        kind = f"{a.data_type}({a.length})" if a.data_type == "varchar" and a.length else a.data_type
        cols.append(f"{a.name} {kind}" + (" PK" if a.primary_key else "") + ("" if a.nullable or a.primary_key else " 필수")
                    + (f"→{a.references}" if a.references else "")
                    + (f" [{'|'.join(a.allowed_values)}]" if a.allowed_values else ""))
    return f"- {e.entity_id} ({e.label}): {', '.join(cols)}"


def _function_block(f: FunctionSpec, linked: list[str], errors: list[str]) -> list[str]:
    """설계할 기능 하나 (입출력·흐름·규칙을 자르지 않는다)."""
    lines = [f"### [{f.function_id}] {f.name}",
             f"설명: {f.description}",
             f"행위자: {', '.join(f.actors) or '없음'}",
             f"사전 조건: {'; '.join(f.preconditions) or '없음'}",
             "입력: " + ("; ".join(i.name + ("" if i.required else "(선택)") + (f" — {i.description}" if i.description else "")
                               for i in f.inputs) or "없음"),
             "출력: " + ("; ".join(o.name + (f" — {o.description}" if o.description else "") for o in f.outputs) or "없음"),
             "정상 흐름: " + (" → ".join(f.normal_flow) or "없음"),
             "예외 흐름: " + ("; ".join(f"{x.condition} → {x.handling}" for x in f.exception_flows) or "없음"),
             "업무 규칙: " + ("; ".join(f.business_rules) or "없음"),
             f"연결 테이블: {', '.join(linked) or '없음'}"]
    lines += [f"반드시 해결할 검사 오류: {m}" for m in errors]
    return lines


def _batch_prompt(batch: list[FunctionSpec], model: DataModel, others: list[str], errors: dict[str, list[str]]) -> str:
    """API 설계 묶음의 사용자 프롬프트: 기능, 그 기능의 테이블과 부모 테이블, 다른 기능이 이미 쓰는 API."""
    by_name = {e.entity_id: e for e in model.entities}
    parts = ["## 설계할 기능"]
    names: list[str] = []
    for f in batch:
        linked = [e.entity_id for e in model.entities if f.function_id in e.function_ids]
        names += [n for n in linked if n not in names]
        parts += _function_block(f, linked, errors.get(f.function_id, []))
    for n in list(names):   # 외래키로 참조하는 부모 테이블도 넣는다 (경로·응답의 연결 정보)
        for a in by_name[n].attributes:
            if a.references in by_name and a.references not in names:
                names.append(a.references)
    parts += ["", "## 테이블·컬럼 (column에는 이 이름만 쓴다)", *([_table_line(by_name[n]) for n in names] or ["- 없음"])]
    if others:
        parts += ["", "## 이미 다른 기능이 쓰는 API (같은 메서드·경로를 쓰지 않는다)", *others]
    parts += ["", f"## 응답 functions에 반드시 {len(batch)}개 모두 이 function_id 그대로", ", ".join(f.function_id for f in batch)]
    return "\n".join(parts)


def _system_nfrs(requirements: list[Requirement]) -> list[Requirement]:
    """아키텍처 입력이 되는 시스템 비기능 요구사항 (범위에서 뺀 것 제외)."""
    return [r for r in requirements if r.type == "NON_FUNCTIONAL" and r.scope == "SYSTEM" and r.status != "EXCLUDED"]


def _arch_prompt(requirements: list[Requirement], functions: list[FunctionSpec], sources: dict[str, str]) -> str:
    """아키텍처 요약 요청의 사용자 프롬프트: 시스템 비기능 요구사항, 기능 영역, 기술 스택 선택지."""
    parts = ["## 시스템 비기능 요구사항"]
    for r in _system_nfrs(requirements):
        line = f"- [{r.requirement_id}] ({r.category}) {r.title}: {r.description}"
        if r.acceptance_criteria:
            line += f" | 수용 기준: {'; '.join(r.acceptance_criteria)}"
        parts.append(line)
    if len(parts) == 1:
        parts.append("- 없음")
    groups: dict[str, list[str]] = {}
    for f in functions:
        groups.setdefault(f.group, []).append(f.name)
    parts += ["", "## 기능 영역", *[f"- {g} {sources.get(g.split('.')[0], '')}: {', '.join(names)}" for g, names in groups.items()]]
    parts += ["", "## 기술 스택 선택지 (원문이 언어·프레임워크를 정하지 않았을 때 하나를 추천)",
              *[f"{i}. {o} — {n}" for i, (o, n) in enumerate(zip(STACK_OPTIONS, STACK_NOTES), start=1)]]
    return "\n".join(parts)


def _arch_fingerprint(requirements: list[Requirement], functions: list[FunctionSpec]) -> str:
    """아키텍처 입력(시스템 비기능 요구사항·기능 영역)의 지문."""
    payload = [[r.requirement_id, r.category, r.title, r.description, r.acceptance_criteria] for r in _system_nfrs(requirements)]
    payload.append(sorted({f.group for f in functions}))
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
