"""개발 Agent: 코드가 만든 뼈대의 정해진 자리(hole)에 기능별 업무 로직만 채운다.

토큰과 결과 흔들림을 줄이려고 LLM이 하는 일을 좁혔다.
- 파일 구조·API·DB 처리·화면은 코드가 만든다 (tools/scaffold.py). LLM은 Logic 클래스 메서드의 본문만 쓴다.
- 기능 하나에 요청 하나다. 그 기능의 규칙·API·테이블만 넣어 입력을 줄이고, 앞부분(공통 규칙)은 프롬프트 캐시로 재사용한다.
- 같은 입력이면 다시 묻지 않는다: 입력 지문이 같으면 저장한 결과(.req-agent/logic/)를 그대로 쓴다.
  스펙이 바뀌면 지문이 달라진 기능만 다시 묻는다.
- 받은 코드는 코드로 검사한다 (자리 이름·금지 표현·없는 테이블). 틀리면 오류를 붙여 한 번 다시 묻고, 그래도 틀리면 기본 동작을 두고 '실패'로 보고한다.
- 공통 규칙은 구현하지 않는다. '(확인 필요)'가 남은 규칙은 최종 승인을 거쳤으므로 가정으로 구현한다.
- 서버 틀마다 자리의 언어가 다르다: Spring은 Java, FastAPI는 Python, NestJS는 TypeScript. 프롬프트·검사·자리 이름은 틀을 따르고,
  기록 형식({hooks: {자리 이름: 본문}})은 같다. Spring이 아닌 기록에는 stack을 적어 다른 틀의 본문이 섞이지 않게 한다.
- FastAPI·NestJS는 쓴 뒤에 모듈 불러오기·tsc로 한 번 더 본다 (verify). 틀린 기능은 자리를 기본 동작으로 되돌린다 (drop_hooks).
"""
from __future__ import annotations

import ast
import hashlib
import logging
import re
import subprocess
import sys
import textwrap
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
# 서버 틀별 프롬프트 (Spring은 SYSTEM_PROMPT 그대로: 지문이 바뀌지 않는다)
STACK_PROMPTS = {"fastapi": load_prompt("developer/system_fastapi"), "nestjs": load_prompt("developer/system_nestjs")}
# 자동 처리를 붙일 자리 (요청 문장)
CRUD_CALL = {"spring": "crud.get()", "fastapi": "crud()", "nestjs": "await crud()"}
# DB가 MySQL 8이면 바꿀 문장 (PostgreSQL 문장은 그대로: 같은 스펙이면 지문이 바뀌지 않는다). 서버는 sql_mode ANSI로 연결한다
MYSQL_PROMPT = [("CAST(:값 AS timestamptz)·CAST(:값 AS date)로 한다.", """CAST(:값 AS DATETIME(6))·CAST(:값 AS DATE)로 한다.
12. DB는 MySQL 8이다 (sql_mode ANSI: 이름은 큰따옴표, 문자열은 작은따옴표, ||는 문자열 잇기). 숫자 변환은 CAST(:값 AS SIGNED)·CAST(:값 AS DECIMAL(18,2)).
    ::타입 변환, ILIKE, RETURNING, to_char, date_trunc, interval 'n day' 같은 PostgreSQL 문법은 쓰지 않는다 (날짜 계산은 DATE_ADD·DATEDIFF, 현재 시각 NOW()).
    rank·key·range 같은 MySQL 예약어 이름은 큰따옴표로 감싼다.""")]
# FastAPI·NestJS 프롬프트에만 있는 문장 (현재 사용자 ID는 문자열)
STACK_MYSQL_PROMPT = [("CAST(:userId AS bigint)", "CAST(:userId AS SIGNED)")]


def system_prompt(spec: Spec) -> str:
    """앱 DB에 맞춘 시스템 프롬프트. PostgreSQL이면 SYSTEM_PROMPT 그대로다."""
    from app.agents.tools.scaffold import db_dialect

    text = STACK_PROMPTS.get(logic_stack(spec), SYSTEM_PROMPT)
    if db_dialect(spec) == "mysql":
        for old, new in MYSQL_PROMPT:
            assert old in text, old
            text = text.replace(old, new)
        for old, new in STACK_MYSQL_PROMPT if logic_stack(spec) != "spring" else []:
            assert old in text, old
            text = text.replace(old, new)
    return text


class HookOut(BaseModel):
    method: str = Field(description="채울 메서드 이름 (입력의 API 목록에 있는 이름)")
    body: str = Field(description="메서드 본문 Java 문장")


class TodoOut(BaseModel):
    rule: str = Field(description="구현하지 못한 규칙의 수용 기준 ID")
    reason: str


class LogicOut(BaseModel):
    hooks: list[HookOut] = Field(default_factory=list, description="기본 동작과 달라지는 메서드만")
    todos: list[TodoOut] = Field(default_factory=list, description="입력만으로 구현할 수 없는 규칙 (없으면 생략)")


class HookOutPy(BaseModel):
    method: str = Field(description="채울 함수 이름 (입력의 '채울 메서드'에 있는 이름)")
    body: str = Field(description="함수 본문 Python 문장 (들여쓰기 없이 시작)")


class LogicOutPy(BaseModel):
    hooks: list[HookOutPy] = Field(default_factory=list, description="기본 동작과 달라지는 함수만")
    todos: list[TodoOut] = Field(default_factory=list, description="입력만으로 구현할 수 없는 규칙 (없으면 생략)")


class HookOutTs(BaseModel):
    method: str = Field(description="채울 함수 이름 (입력의 '채울 메서드'에 있는 이름)")
    body: str = Field(description="함수 본문 TypeScript 문장")


class LogicOutTs(BaseModel):
    hooks: list[HookOutTs] = Field(default_factory=list, description="기본 동작과 달라지는 함수만")
    todos: list[TodoOut] = Field(default_factory=list, description="입력만으로 구현할 수 없는 규칙 (없으면 생략)")


SCHEMAS = {"spring": LogicOut, "fastapi": LogicOutPy, "nestjs": LogicOutTs}


def logic_stack(spec: Spec) -> str:
    """자리를 채울 서버 틀: fastapi · nestjs · spring (그 밖의 틀은 spring으로 본다: 채울 자리가 없으면 targets가 비어 있다)."""
    from app.agents.tools.scaffold import stack_parts

    server = stack_parts(spec)["backend"]
    return server if server in STACK_PROMPTS else "spring"


def hole_names(spec: Spec, ops: list[dict]) -> list[str]:
    """API마다 자리 이름 (= 그 틀의 메서드·함수 이름). API가 없으면 run 하나."""
    stack = logic_stack(spec)
    if not ops:
        return ["run"]
    if stack == "fastapi":
        from app.agents.tools.scaffold_fastapi import func_names
        return func_names(ops)
    if stack == "nestjs":
        from app.agents.tools.scaffold_nestjs import func_names
        return func_names(ops)
    return [method_name(op["operation_id"]) for op in ops]


ROLE_CHECK = re.compile(r"ctx\.user\(\)\.role\(\)")
DATE_PARSE = re.compile(r"\b(?:LocalDate|LocalDateTime|OffsetDateTime|ZonedDateTime|Instant|LocalTime)\.parse\b")
FORBIDDEN = re.compile(r"\b(import|class|interface|Runtime|ProcessBuilder|Thread|System\.exit|System\.getenv|java\.io|java\.nio|java\.net|Class\.forName)\b")
TABLE_IN_SQL = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?!(?:SET|SELECT|LATERAL|ONLY|DUAL)\b)([a-zA-Z_][a-zA-Z0-9_]*)\b(?!\s*\()", re.I)   # FROM CAST(…)처럼 함수는 테이블이 아니다
CTE_NAME = re.compile(r"(?:\bWITH(?:\s+RECURSIVE)?|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(", re.I)


# SQL을 조각으로 이을 때 공백이 빠진 것: "… AS total" + "FROM …" → "totalFROM" (컴파일은 되지만 실행하면 문법 오류)
SQL_KEY = r"(?:SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|GROUP|ORDER|HAVING|LIMIT|OFFSET|UNION|AND|OR|ON|SET|VALUES|RETURNING|INTO)\b"
_OPEN = r"(?:\"\"\"[ \t]*\n\s*|[\"'`])"   # 다음 조각의 시작: 텍스트 블록(\"\"\" 다음 줄) 또는 따옴표
_GLUED = re.compile(r"([\"'`])([^\"'`\n]*[A-Za-z0-9_)])\1\s*\+?\s*" + _OPEN + "(" + SQL_KEY + ")")
_SQL_VAR = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*" + _OPEN + r"\s*" + SQL_KEY)


def glued_sql(code: str) -> list[str]:
    """SQL 조각을 이을 때 앞 조각 끝과 뒤 조각 앞 사이에 공백이 없는 곳 (문자열끼리, 또는 SQL로 시작하는 변수와)"""
    out = [f"SQL 조각 사이에 공백이 없음: '…{m.group(2)[-20:]}' + '{m.group(3)} …' (→ '{m.group(2)[-8:]}{m.group(3)}'). 조각 앞이나 끝에 공백을 둔다"
           for m in _GLUED.finditer(code)]
    for var in set(_SQL_VAR.findall(code)):
        for m in re.finditer(r"([\"'`])([^\"'`\n]*[A-Za-z0-9_)])\1\s*\+\s*" + re.escape(var) + r"\b", code):
            out.append(f"SQL 조각 사이에 공백이 없음: '…{m.group(2)[-20:]}' + {var} ({var}는 SQL로 시작). 조각 앞이나 끝에 공백을 둔다")
    return list(dict.fromkeys(out))


def sql_tables(sql: str) -> list[str]:
    """SQL이 읽고 쓰는 테이블 이름. 'DO UPDATE SET'의 SET 같은 예약어와 WITH 이름(CTE, 예: base)은 테이블이 아니다"""
    ctes = {m.lower() for m in CTE_NAME.findall(sql)}
    return [t for t in TABLE_IN_SQL.findall(sql) if t.lower() not in ctes]


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
        for name in sql_tables(sql):
            if name.lower() not in tables and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", sql, re.I):
                errors.append(f"{method}: 스키마에 없는 테이블 '{name}'")
    return errors


PY_IMPORTS = {"json", "re", "decimal", "math"}
PY_BANNED = (ast.AsyncFunctionDef, ast.FunctionDef, ast.ClassDef, ast.Await, ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal)


def check_hook_py(method: str, body: str, allowed: set[str], tables: set[str], reads: set[str] = frozenset()) -> list[str]:
    """FastAPI 자리의 Python 본문 검사 (AST): 문법, 쓸 수 있는 import, 금지 이름, 정의하지 않은 이름, 스키마에 없는 테이블."""
    from app.agents.roles import builder_fastapi as bf
    from app.agents.tools.scaffold_fastapi import py_text

    errors = []
    if method not in allowed:
        errors.append(f"{method}: 입력의 API 목록에 없는 함수")
    code = py_text(body)   # 생성기가 자리에 넣는 글자와 같다
    if not [line for line in code.splitlines() if line.strip() and not line.strip().startswith("#")]:
        return errors + [f"{method}: 주석만 있고 코드가 없음 (본문 문장을 주석 밖에 쓴다)"]
    if any(line.rstrip().endswith(";") for line in code.splitlines()):
        errors.append(f"{method}: 문장 끝에 ;가 있음 (Python 문장으로 쓴다)")
    args = "ctx" if method == "run" else "data, ctx, crud"
    try:
        tree = ast.parse(f"def _({args}):\n" + textwrap.indent(code, "    ") + "\n")
    except SyntaxError as e:
        return errors + [f"{method}: 문법 오류 {max((e.lineno or 1) - 1, 1)}행: {e.msg}"]
    nodes = [n for stmt in tree.body[0].body for n in ast.walk(stmt)]
    for node in nodes:
        if isinstance(node, PY_BANNED):
            errors.append(f"{method}: 쓸 수 없는 문장 '{type(node).__name__}' (def·class·async·await·yield·global)")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else ["." * node.level + (node.module or "")]
            errors += [f"{method}: 쓸 수 없는 import {m}" for m in mods if m.split(".")[0] not in PY_IMPORTS]
        elif isinstance(node, ast.Name) and (node.id in bf.FORBIDDEN_NAMES or node.id.startswith("__")):
            errors.append(f"{method}: 쓸 수 없는 표현 '{node.id}'")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                errors.append(f"{method}: 쓸 수 없는 표현 '{node.attr}'")
            if bf.DATE_PARSE.fullmatch(node.attr):
                errors.append(f"{method}: 날짜·시각 글자를 Python에서 파싱함 (비교·계산은 SQL의 CAST로 한다)")
            if (node.attr == "role" and isinstance(node.value, ast.Attribute) and node.value.attr == "user"
                    and isinstance(node.value.value, ast.Name) and node.value.value.id == "ctx"):
                errors.append(f"{method}: 역할로 권한을 다시 검사함 (권한 검사는 코드가 이미 한다)")
    if method != "run" and not any(isinstance(n, (ast.Return, ast.Raise)) for n in nodes):
        errors.append(f"{method}: return이 없음")
    if method in reads and not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "crud" for n in nodes):
        errors.append(f"{method}: 조회인데 crud()를 부르지 않음 (그 결과를 돌려주고 고칠 것만 고친다)")
    bound = bf._bound(tree)
    errors += [f"{method}: 정의하지 않은 이름 '{n.id}'" for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound]
    for sql in bf._strings(tree):
        if bf.SQL_WORD.search(sql):
            errors += [f"{method}: 스키마에 없는 테이블 '{name}'" for name in sql_tables(sql) if name.lower() not in tables]
    return list(dict.fromkeys(errors))


TS_EXTRA = re.compile(r"(?<![.\w$])(import|export|class|module|exports|setTimeout|setImmediate|queueMicrotask)\b")


def check_hook_ts(method: str, body: str, allowed: set[str], tables: set[str], reads: set[str] = frozenset()) -> list[str]:
    """NestJS 자리의 TypeScript 본문 검사 (글자): 금지 API, 다른 언어 문법, 괄호, 스키마에 없는 테이블. 타입은 쓴 뒤 tsc가 본다 (verify)."""
    from app.agents.roles import builder_nestjs as bn
    from app.agents.tools.scaffold_nestjs import foreign_body

    errors = []
    if method not in allowed:
        errors.append(f"{method}: 입력의 API 목록에 없는 함수")
    bare = bn._bare(body)
    if not bare.strip():
        return errors + [f"{method}: 주석만 있고 코드가 없음 (본문 문장을 주석 밖에 쓴다)"]
    for m in [*bn.FORBIDDEN.finditer(bare), *TS_EXTRA.finditer(bare)]:
        errors.append(f"{method}: 쓸 수 없는 표현 '{m.group(1) or 'import('}'")
    if lang := foreign_body(body):
        errors.append(f"{method}: {lang} 문법으로 씀 (TypeScript 문장으로 쓴다: ctx.db.query·ctx.user.id·await crud())")
    if any(bare.count(a) != bare.count(b) for a, b in ("{}", "()", "[]")):
        errors.append(f"{method}: 괄호가 맞지 않음")
    if method != "run" and not re.search(r"\b(return|throw)\b", bare):
        errors.append(f"{method}: return이 없음")
    if re.search(r"\bctx\.user\.role\b", bare):
        errors.append(f"{method}: 역할로 권한을 다시 검사함 (권한 검사는 코드가 이미 한다)")
    if bn.DATE_PARSE.search(bare):
        errors.append(f"{method}: 날짜·시각 글자를 TypeScript에서 파싱함 (비교·계산은 SQL의 CAST로 한다)")
    if method in reads and not re.search(r"\bcrud\(\)", bare):
        errors.append(f"{method}: 조회인데 crud()를 부르지 않음 (그 결과를 돌려주고 고칠 것만 고친다)")
    for m in bn.STRING.finditer(body):
        sql = m.group(0)[1:-1]
        if bn.SQL_WORD.search(sql):
            errors += [f"{method}: 스키마에 없는 테이블 '{name}'" for name in sql_tables(sql) if name.lower() not in tables]
    return list(dict.fromkeys(errors))


CHECKS = {"spring": check_hook, "fastapi": check_hook_py, "nestjs": check_hook_ts}


def _rules(spec: Spec, f: FunctionSpec) -> list[str]:
    """구현할 규칙: 확정·가정·표시가 남은 '확인 필요'(최종 승인을 거침, 가정으로 구현)."""
    rows = spec.acceptance[f.function_id]
    todo = []
    for rule in f.business_rules:
        kind, text, qids = rule_tag(rule)
        ac = next((r["id"] for r in rows if r["then"] == text and r["kind"].startswith("규칙")), "")
        if kind in ("확정", "가정", "보류"):
            todo.append(f"- {ac} {'' if kind == '확정' else '(가정) '}{text}")
    return todo


def logic_request(spec: Spec, f: FunctionSpec, ops: list[dict]) -> str:
    """기능 하나의 요청. 같은 스펙이면 글자까지 같다. 자리 이름은 그 서버 틀의 이름이다."""
    todo = _rules(spec, f)
    parts = [f"## 기능 {f.function_id} {f.name}", f.description, f"행위자: {', '.join(f.actors)}", "", "## 구현할 규칙", *(todo or ["- 없음"])]
    if f.exception_flows:
        parts += ["", "## 예외 흐름", *[f"- {x.condition} → {x.handling}" for x in f.exception_flows]]
    automations = spec.automations_of(f.function_id)
    if automations:
        parts += ["", f"## 구현할 자동 처리 (해당 메서드에서 {CRUD_CALL[logic_stack(spec)]} 뒤에 처리한다)"]
        parts += [f"- {a['id']} {a['name']}: {a['when']} → {a['action']} | 바꾸는 테이블: {', '.join(a['targets'])} | 실패하면: {a['on_failure'] or '전체를 되돌린다'}" for a in automations]
    parts += ["", "## 채울 메서드"]
    for op, name in zip(ops, hole_names(spec, ops)):
        fields = ", ".join(x["name"] + ("*" if x.get("required") else "") for x in op.get("request") or []) or "없음"
        parts.append(f"- {name}: {op['method']} {op['path']} — {op.get('summary', '')} | 입력: {fields}")
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
    return hashlib.sha256("\n".join((PROMPT_VERSION, system_prompt(spec), _shared(spec), request)).encode("utf-8")).hexdigest()[:16]


def targets(spec: Spec, only: set[str] | None = None) -> list[tuple[FunctionSpec, list[dict], str]]:
    """LLM이 채울 기능 (구현할 규칙이나 예외 흐름이 있는 기능만): (기능, 만들 API, 요청). 백엔드 뼈대가 없으면 채울 자리도 없다."""
    if not has_backend(spec) or not supports_logic(spec):
        return []
    owned, _ = owned_operations(spec)
    out = []
    for f in spec.functions:
        if only and f.function_id not in only:
            continue
        if _rules(spec, f) or f.exception_flows or spec.automations_of(f.function_id):
            out.append((f, owned[f.function_id], logic_request(spec, f, owned[f.function_id])))
    return out


def supports_logic(spec: Spec) -> bool:
    """--logic이 채울 수 있는 서버 틀인가 (틀이 있는 서버: Spring의 Java, FastAPI의 Python, NestJS의 TypeScript 자리)."""
    return has_backend(spec)


def _stack_of(record: dict) -> str:
    return record.get("stack") or "spring"   # Spring 기록에는 stack을 적지 않는다 (이전 기록과 같은 모양)


def stored_logic(spec: Spec, out_dir: Path) -> dict[str, dict]:
    """지금 서버 틀로 채운 기록만 {기능 ID: 기록} (다른 틀로 채운 본문은 이 틀의 자리에 넣지 않는다)."""
    stack = logic_stack(spec)
    return {fid: r for fid, r in load_logic(out_dir).items() if _stack_of(r) == stack}


def hooks_of(spec: Spec, out_dir: Path) -> dict[str, dict]:
    """render_app에 넘길 채운 자리 {기능 ID: {자리 이름: 본문}}."""
    return {fid: r.get("hooks") or {} for fid, r in stored_logic(spec, out_dir).items()}


def plan(spec: Spec, out_dir: Path, only: set[str] | None = None) -> dict:
    """LLM 없이 해야 할 일과 예상 비용을 계산한다."""
    stored = stored_logic(spec, out_dir)
    todo, cached, stale = [], 0, []
    for f, ops, request in targets(spec, only):
        record = stored.get(f.function_id)
        if record and record.get("fingerprint") == fingerprint(spec, request) and record.get("status") == "채움":
            cached += 1
            continue
        if record and record.get("status") == "채움":
            stale.append(f.function_id)
        todo.append((f, ops, request))
    shared = len(system_prompt(spec)) + len(_shared(spec))
    input_tokens = int(sum(len(r) + shared for _, _, r in todo) / CHARS_PER_TOKEN)
    output_tokens = sum(max(len(ops), 1) for _, ops, _ in todo) * OUTPUT_PER_METHOD * THINKING_FACTOR
    cost = {name: round(input_tokens / 1e6 * p[0] + output_tokens / 1e6 * p[1], 3) for name, p in PRICES.items()}
    return {"functions": len(todo), "cached": cached, "stale": stale, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost}


def _check_all(spec: Spec, hooks: dict[str, str], ops: list[dict], tables: set[str]) -> list[str]:
    """채운 자리 전부를 그 틀의 검사로 본다. 오류 문장 목록."""
    check = CHECKS[logic_stack(spec)]
    allowed, reads = set(hole_names(spec, ops)), _reads(spec, ops)
    return [e for m, b in hooks.items() for e in check(m, b, allowed, tables, reads)]


def _ask(llm, spec: Spec, f: FunctionSpec, ops: list[dict], request: str, tables: set[str]) -> tuple[dict, list[dict], list[str]]:
    """한 기능을 묻고 검사한다. 틀리면 오류를 붙여 한 번 더 묻는다. (채운 자리, 못 한 규칙, 남은 오류)"""
    effort = get_settings().dev_effort
    user, errors = request, []
    for attempt in range(2):
        out = llm.structured(system=system_prompt(spec), user=user, schema=SCHEMAS[logic_stack(spec)], shared=_shared(spec), effort=effort,
                             purpose=f"업무 로직 {f.function_id}")
        errors = _check_all(spec, {h.method: h.body for h in out.hooks}, ops, tables)
        if not errors:
            return {h.method: h.body for h in out.hooks}, [t.model_dump() for t in out.todos], []
        log.warning("[개발] %s 코드 검사 실패 (시도 %d): %s", f.function_id, attempt + 1, "; ".join(errors))
        user = request + "\n\n## 이전 응답의 오류 (고쳐서 다시 쓴다)\n" + "\n".join(f"- {e}" for e in errors)
    return {}, [], errors


def _reads(spec: Spec, ops: list[dict]) -> set[str]:
    """조회 자리 이름 (목록·한 건): crud의 응답 모양을 화면이 그대로 쓴다."""
    from app.agents.tools.api_shape import op_kind

    return {name for op, name in zip(ops, hole_names(spec, ops)) if op_kind(op) in ("list", "get")}


def _record(spec: Spec, record: dict) -> dict:
    """저장할 기록. Spring이 아니면 틀 이름을 적는다 (형식은 같다: hooks는 {자리 이름: 본문})."""
    stack = logic_stack(spec)
    return record if stack == "spring" else {**record, "stack": stack}


def fill_logic(spec: Spec, out_dir: Path, only: set[str] | None = None) -> dict:
    """지문이 달라진 기능만 LLM에 물어 자리를 채운다. 결과는 기능마다 저장해 다음 실행이 다시 묻지 않는다."""
    stored = stored_logic(spec, out_dir)
    llm = get_llm()
    tables = {e.entity_id for e in spec.ddl.entities}
    todo = []
    for f, ops, request in targets(spec, only):
        record = stored.get(f.function_id) or {}
        # 저장한 결과도 지금 검사로 다시 본다 (검사가 나중에 엄격해졌으면 그 기능만 다시 묻는다)
        valid = not _check_all(spec, record.get("hooks") or {}, ops, tables)
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
        save_logic(out_dir, f.function_id, _record(spec, {"fingerprint": fingerprint(spec, request), "status": status, "hooks": hooks,
                                                          "todos": todos, "errors": errors, "model": getattr(llm, "model", "")}))
        (failed if errors else filled).append(f.function_id)
    return {"filled": filled, "failed": failed, "usage": llm.pop_usage()}


# ------------------------------------------------------------------ 쓴 뒤 검사 (FastAPI: 모듈 불러오기, NestJS: tsc)
IMPORT_CHECK = """import importlib, sys, traceback
sys.path.insert(0, ".")
for name in sys.argv[1:]:
    try:
        importlib.import_module("app.logic." + name)
    except Exception as e:
        line = next((f.lineno for f in reversed(traceback.extract_tb(e.__traceback__)) if f.filename.endswith(name + ".py")), 0)
        print(name + "\\t" + (f"{line}행: " if line else "") + type(e).__name__ + ": " + str(e).splitlines()[0][:300])
"""


def verify(spec: Spec, out_dir: Path, function_ids: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """채운 기능의 업무 로직 파일을 실제로 불러 본다. Spring은 하지 않는다 (Maven 컴파일은 앱 빌드가 한다).
    FastAPI는 app.logic 모듈 불러오기(이 도구의 Python), NestJS는 서버 tsc --noEmit (패키지가 없으면 npm install 먼저).
    (기능 ID → 오류, 하지 못한 검사)"""
    stack = logic_stack(spec)
    if stack == "spring" or not function_ids:
        return {}, []
    from app.agents.tools.scaffold_fastapi import module_name

    by_module = {module_name(fid): fid for fid in function_ids}
    if stack == "nestjs":
        from app.agents.roles import builder_nestjs

        return builder_nestjs.verify(out_dir, by_module, folder="logic")
    try:
        run = subprocess.run([sys.executable, "-I", "-B", "-c", IMPORT_CHECK, *sorted(by_module)], cwd=out_dir / "backend",
                             capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {}, [f"업무 로직 모듈 불러오기 ({e})"]
    errors: dict[str, list[str]] = {}
    for line in run.stdout.decode("utf-8", "replace").splitlines():
        name, _, message = line.partition("\t")
        if fid := by_module.get(name):
            errors.setdefault(fid, []).append(f"불러오기 오류 {message}")
    return errors, [] if run.returncode == 0 else ["업무 로직 모듈 불러오기 실패 (업무 로직 파일 밖의 오류)"]


def drop_hooks(out_dir: Path, errors: dict[str, list[str]]) -> None:
    """쓴 뒤 검사에서 틀린 기능은 자리를 기본 동작으로 되돌리고 '실패'로 적는다 (앱 전체가 빌드되게). 버린 본문은 rejected에 남긴다."""
    records = load_logic(out_dir)
    for fid, errs in errors.items():
        if fid in records:
            record = records[fid]
            save_logic(out_dir, fid, {**record, "status": "실패", "stage": "컴파일", "hooks": {}, "rejected": record.get("hooks") or {},
                                      "errors": errs})
