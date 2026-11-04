"""개발 단계 뼈대 (FastAPI · Python 3.12): 개발 스펙에서 Spring 틀과 같은 HTTP 계약의 서버 코드를 만든다 (LLM 없음, 토큰 0).

- 같은 경로·JSON 모양·오류 코드·필수값·기본값·상태 전환이다. 화면(frontend)은 바꾸지 않고 그대로 붙는다.
- 고정 틀(templates/backend-fastapi)은 그대로 복사하고, 스펙에 따라 달라지는 파일(기능별 API·업무 로직·화면 보조 API·계정)만 만든다.
- 기본 동작(등록·조회·수정·삭제·상태 바꾸기)을 정하는 규칙은 scaffold.py의 것을 그대로 쓴다 (Java 값을 Python 값으로만 옮긴다).
- 업무 로직 자리는 '# <hole 이름>' … '# </hole>'이고 기본 본문은 'return crud()'다.
"""
from __future__ import annotations

import json
import keyword
import re
import textwrap
from pathlib import Path

from app.agents.tools import scaffold as sc
from app.agents.tools.dev_spec import Spec, rule_tag
from app.agents.tools.screens import SECRET_COLUMN, action_of, label_column, op_kind, op_table, path_params
from app.schemas.function import FunctionSpec

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "backend-fastapi"
ROOT = "backend/app"
# 생성한 모듈 안에서 이미 쓰는 이름 (API 함수 이름이 겹치지 않게)
TAKEN = {"router", "logic", "checks", "transaction", "values", "current_user", "not_implemented", "auto_no", "data", "user",
         "request", "body", "db", "crud", "ctx", "run", "Any", "Body", "Request", "Callable", "Ctx", "Col", "Table", "NOW"}

DOCKERFILE = """# 한 번에 빌드: 화면(Node) → 서버(Python, 화면을 함께 담음). docker compose up --build가 쓴다.
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
ENV VITE_API=server
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /web/dist ./static
ENV LOCAL_DIR=/app/local STATIC_DIR=/app/static
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
"""

DOCKERIGNORE = "**/node_modules\n**/dist\n**/dist-check\n**/__pycache__\n**/.venv\nlocal\nbackend/local\n.req-agent\n.preview\n.git\n"

# Spring의 COMPOSE·COMPOSE_MYSQL과 같고 app 서비스의 DB_URL만 Python(SQLAlchemy) 형식이다
PG_URL = "postgresql://app:app@{host}/app"
MYSQL_URL = "mysql+pymysql://app:app@{host}/app?charset=utf8mb4"


def _compose(text: str, old: str, url: str) -> str:
    if old not in text:
        raise sc.AppError("FastAPI용 compose를 만들 수 없습니다: Spring compose의 DB_URL이 바뀌었습니다.")
    return text.replace(old, f"      DB_URL: {url}\n      LOCAL_DIR: /app/local\n")


COMPOSE = _compose(sc.COMPOSE, "      DB_URL: jdbc:postgresql://db:5432/app\n      DB_USER: app\n      DB_PASSWORD: app\n", PG_URL.format(host="db:5432"))
COMPOSE_MYSQL = _compose(sc.COMPOSE_MYSQL, f'      DB_URL: "{sc.MYSQL_URL.format(host="db:3306")}"\n      DB_USER: app\n      DB_PASSWORD: app\n',
                         f'"{MYSQL_URL.format(host="db:3306")}"')


def dev_steps(mysql: bool) -> list[str]:
    """README의 '고치면서 띄우기' (Spring의 mvn 대신)."""
    url = MYSQL_URL.format(host="localhost:3307") if mysql else PG_URL.format(host="localhost:5433")
    return ["docker compose up -d db                 # " + ("MySQL만 (포트 3307)" if mysql else "PostgreSQL만 (포트 5433)"),
            "cd backend && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt",
            f"cd backend && DB_URL='{url}' .venv/bin/uvicorn app.main:app --port 8080 --reload   # 계정 파일은 backend/local/accounts.txt",
            "cd frontend && VITE_API=server npm run dev"]


# ------------------------------------------------------------------ 이름·값
def module_name(function_id: str) -> str:
    """FN-SFR-001-05 → fn_sfr00105 (scaffold.class_name을 snake로)."""
    name = sc._snake(sc.class_name(function_id)).lower()
    return name if name[:1].isalpha() else f"f_{name}"


def func_names(ops: list[dict]) -> list[str]:
    """API마다 Python 함수 이름 (= 업무 로직 자리 이름). 예약어·모듈 안 이름과 겹치면 _op를 붙인다."""
    out: list[str] = []
    for op in ops:
        name = sc._snake(sc.method_name(op["operation_id"])).lower()
        while keyword.iskeyword(name) or name in TAKEN or name in out or not name.isidentifier():
            name += "_op"
        out.append(name)
    return out


def _q(spec: Spec, name: str) -> str:
    """SQL 이름 (MySQL 예약어도 큰따옴표: 서버가 sql_mode ANSI로 연결한다)."""
    return sc._q(spec, name)


def _s(value: str) -> str:
    """Python 문자열 리터럴."""
    return repr(value)


def _py(java: str) -> str:
    """scaffold.py가 만든 Java 값(_defaults·_stamps·_literal) → Python 값."""
    if java == "Db.NOW":
        return "NOW"
    if java == "user.id()":
        return "user.id"
    if m := re.fullmatch(r"Db\.autoNo\((\d+)\)", java):
        return f"auto_no({m.group(1)})"
    if java in ("true", "false"):
        return java.capitalize()
    if re.fullmatch(r"-?\d+L", java):
        return java[:-1]
    if re.fullmatch(r"-?\d+(\.\d+)?", java):
        return java
    if java.startswith('"'):
        return _s(json.loads(java))
    raise ValueError(f"옮길 수 없는 값: {java}")


def _pairs(java: list[str]) -> str:
    """Db.values(이름, 값 …) 인자 → dict 리터럴."""
    return "{" + ", ".join(f"{_py(java[i])}: {_py(java[i + 1])}" for i in range(0, len(java), 2)) + "}"


def _col(field: str, column: str, kind: str) -> str:
    return f"Col({_s(field)}, {_s(column)}, {_s(kind)})"


def _list(items: list[str]) -> str:
    return "[\n    " + ",\n    ".join(items) + ",\n]" if items else "[]"


def _cols(spec: Spec, table: str, cols: list[tuple[str, str]]) -> str:
    return _list([_col(f, _q(spec, spec.names.column(table, c)), sc._pg(spec, table, c)) for f, c in cols])


def _table_const(table: str) -> str:
    return "T_" + table.upper()


def _table_decl(spec: Spec, table: str) -> str:
    entity = next(e for e in spec.model.entities if e.entity_id == table)
    names = {a.name for a in entity.attributes}
    created = [_q(spec, spec.names.column(table, c)) for c in sc.AUDIT_NOW if c in names]
    updated = [_q(spec, spec.names.column(table, c)) for c in sc.AUDIT_NOW[1:] if c in names]
    pk = entity.pk()[0].name
    return (f"{_table_const(table)} = Table({_s(_q(spec, spec.names.table(table)))}, {_s(_q(spec, spec.names.column(table, pk)))}, "
            f"{_s(sc._pg(spec, table, pk))}, {created!r}, {updated!r})")


def _joined(spec: Spec, table: str, fields: list[dict]) -> tuple[list[str], list[str]]:
    """조회 응답의 Col 목록과 JOIN (scaffold._joined와 같은 규칙: 외래키로 가리키는 부모 테이블의 값은 함께 읽는다)."""
    cols, joins, aliases, seen = [], [], {}, set()
    entity = next(e for e in spec.model.entities if e.entity_id == table)
    physical = _q(spec, spec.names.table(table))
    for f in fields:
        t, _, column = (f.get("column") or "").partition(".")
        if spec.columns.get((t, column)) is None or SECRET_COLUMN.search(column) or f["name"] in seen:
            continue
        if t == table:
            source = physical
        else:
            fk = next((a for a in entity.attributes if a.references == t), None)
            parent = next((e for e in spec.model.entities if e.entity_id == t), None)
            if fk is None or parent is None or not parent.pk():
                continue
            if t not in aliases:
                aliases[t] = f"j{len(aliases) + 1}"
                joins.append(f"LEFT JOIN {_q(spec, spec.names.table(t))} {aliases[t]} ON {aliases[t]}.{_q(spec, spec.names.column(t, parent.pk()[0].name))}"
                             f" = {physical}.{_q(spec, spec.names.column(table, fk.name))}")
            source = aliases[t]
        seen.add(f["name"])
        cols.append(_col(f["name"], source + "." + _q(spec, spec.names.column(t, column)), sc._pg(spec, t, column)))
    return cols, joins


def _filters(spec: Spec, table: str, cols: list[tuple[str, str]]) -> str:
    physical = _q(spec, spec.names.table(table))
    return _list([_col(f, physical + "." + _q(spec, spec.names.column(table, c)), sc._pg(spec, table, c)) for f, c in cols])


# ------------------------------------------------------------------ 기본 동작 (scaffold._crud_call과 같은 갈래)
def _crud(spec: Spec, op: dict, table: str, consts: list[str], prefill: list[str]) -> str:
    """이 API의 기본 동작 (Python 식). 만들 수 없으면 '구현 전' 오류를 낸다. prefill은 호출 전에 data를 채우는 문장이다."""
    kind, name = op_kind(op), sc._constant(op["operation_id"])
    todo = f"not_implemented({_s(op['operation_id'])})"
    if not table:
        return todo
    base = _table_const(table)
    id_var = path_params(op["path"])[-1] if path_params(op["path"]) else ""
    get_id = f"data.get({_s(id_var)})"
    out, joins = _joined(spec, table, op.get("response") or [])
    consts.append(f"OUT_{name} = {_list(out)}")
    t = base
    if joins:
        t = f"V_{name}"
        consts.append(f"{t} = {base}.with_joins({_s(' '.join(joins))})")

    def decl(prefix: str, cols: list[tuple[str, str]]) -> None:
        consts.append(f"{prefix}_{name} = {_cols(spec, table, cols)}")

    parents = sc._parent_cols(spec, table, op)
    keys = next(e.pk() for e in spec.model.entities if e.entity_id == table)
    pk = keys[0].name
    if len(keys) > 1 and kind not in ("list", "report", "create", "add"):   # 복합 기본키는 한 행을 ID 하나로 찾을 수 없다
        return todo
    by_parent = bool(id_var) and id_var in {p for p, _ in parents}
    if by_parent and kind not in ("list", "report", "create", "add", "get"):
        return todo
    if by_parent and kind == "get":
        consts.append(f"FILTER_{name} = {_filters(spec, table, parents)}")
        return f"db.first({t}, OUT_{name}, FILTER_{name}, data)"
    if kind == "report" and id_var and sc._snake(id_var) == pk:
        return f"db.one({t}, OUT_{name}, {get_id})"
    if kind in ("list", "report"):
        filters = sc._unique([*sc._cols(spec, table, op.get("request") or [], writable=True), *parents])
        consts.append(f"FILTER_{name} = {_filters(spec, table, filters)}")
        return f"db.{'page' if kind == 'list' else 'first'}({t}, OUT_{name}, FILTER_{name}, data)"
    if kind == "get":
        return f"db.one({t}, OUT_{name}, {get_id})"
    if kind == "delete":
        return f"db.delete({base}, {get_id})"
    if kind in ("create", "add"):
        cols = sc._unique([*sc._cols(spec, table, op.get("request") or [], writable=True), *parents])
        pairs, users, missing = sc._defaults(spec, table, {c for _, c in cols}, op)
        for c in users:
            prefill += [f"if data.get({_s(sc._camel(c))}) is None:", f"    data[{_s(sc._camel(c))}] = user.id"]
        cols += [(sc._camel(c), c) for c in (*users, *missing)]   # 업무 로직이 data에 넣으면 저장된다
        decl("IN", cols)
        needed = "".join(f", {_s(sc._camel(c))}" for c in missing)
        return f"db.insert({t}, IN_{name}, OUT_{name}, data, {_pairs(pairs)}" + (f", {_s(op['operation_id'])}{needed}" if missing else "") + ")"
    if kind == "update":
        decl("IN", sc._cols(spec, table, op.get("request") or [], writable=True))
        return f"db.update({t}, IN_{name}, OUT_{name}, {get_id}, data)"
    sets = action_of(spec, op, table).get("sets")
    stamps, vals = sc._stamps(spec, table, op, sets)
    if stamps:
        decl("STAMP", stamps)
    extra = f", STAMP_{name}, {_pairs(vals)}" if stamps else ""
    move = spec.transition_of(op["operation_id"])
    target = next((f["name"] for f in op.get("request") or [] if move and f.get("column") == f"{table}.{move['column']}"), "")
    if move and target and id_var and len(move["moves"]) > 1:   # API 하나가 여러 상태로 보낸다: 요청 값이 목표
        allowed = "{" + ", ".join(f"{_s(to)}: {list(froms)!r}" for to, froms in move["moves"].items()) + "}"
        return (f"db.move_to({t}, {_s(_q(spec, spec.names.column(table, move['column'])))}, {allowed}, data.get({_s(target)}), "
                f"{get_id}, OUT_{name}{extra})")
    if sets and id_var and sets.get("from"):   # 전환표에 있는 처리: 허용된 상태에서만 바꾼다
        return f"db.move({t}, {_s(_q(spec, sets['column']))}, {list(sets['from'])!r}, {_s(sets['value'])}, {get_id}, OUT_{name}{extra})"
    if sets and id_var:
        return f"db.set({t}, {_s(_q(spec, sets['column']))}, {_s(sets['value'])}, {get_id}, OUT_{name}{extra})"
    written = sc._cols(spec, table, op.get("request") or [], writable=True)
    if op["method"] in ("PUT", "PATCH") and id_var and written:   # 그 값만 바꾼다
        decl("IN", written)
        return f"db.update({t}, IN_{name}, OUT_{name}, {get_id}, data{extra})"
    return todo


def _access(spec: Spec, op: dict) -> list[str]:
    """API를 부를 수 있는 주체 검사 (scaffold._access와 같다)."""
    roles = ", ".join(_s(r) for r in sc._roles_of(spec, op))
    system = sc._system_caller(op)
    if roles and system:
        return ["if not user.is_system():", f"    user.require({roles})"]
    if roles:
        return [f"user.require({roles})"]
    return ["user.require_system()"] if system else ["user.require_login()"]


def _api(spec: Spec, f: FunctionSpec, ops: list[dict]) -> str:
    """기능 하나의 API 모듈. 경로·필드·필수값·허용값은 스펙에서 오고, 업무 규칙은 logic 모듈의 자리로 넘긴다."""
    consts: list[str] = []
    tables: list[str] = []
    bodies: list[str] = []
    for op, name in zip(ops, func_names(ops)):
        table = op_table(spec, op, f.function_id)
        table = table if table in {e.entity_id for e in spec.model.entities if e.pk()} else ""
        if table and table not in tables:
            tables.append(table)
        prefill: list[str] = []
        crud = _crud(spec, op, table, consts, prefill)
        reads = op["method"] in ("GET", "DELETE")
        lines = [f"@router.{op['method'].lower()}({_s(op['path'])})",
                 f"def {name}(request: Request{'' if reads else ', body: Any = Body(None)'}):",
                 f"    {_s(op['method'] + ' ' + op['path'] + ' — ' + sc._doc(op.get('summary') or ''))}",
                 "    user = current_user(request)"]
        lines += [f"    {x}" for x in _access(spec, op)]
        lines.append(f"    data = checks.input_of({'dict(request.query_params)' if reads else 'body'})")
        lines += [f"    data[{_s(p)}] = request.path_params[{_s(p)}]" for p in path_params(op["path"])]
        lines += [f"    {x}" for x in prefill]
        required = [x["name"] for x in op.get("request") or [] if x.get("required") and not reads]
        if required:
            lines.append(f"    checks.required(data, {', '.join(_s(r) for r in required)})")
        for x in op.get("request") or []:
            t, _, c = (x.get("column") or "").partition(".")
            attr = spec.columns.get((t, c))
            if attr and attr.allowed_values:
                lines.append(f"    checks.one_of(data, {_s(x['name'])}, {', '.join(_s(v) for v in attr.allowed_values)})")
        lines += ["    with transaction() as db:   # 업무 로직·자동 처리까지 한 묶음: 예외가 나면 모두 되돌린다",
                  f"        return logic.{name}(data, Ctx(db, user), lambda: {crud})"]
        bodies.append("\n".join(lines))
    head = ["# 코드가 만든 파일 (스펙에서 생성). 고치지 말고 스펙을 고치세요. 업무 규칙은 app/logic에 있습니다.",
            _s(f"{f.function_id} {sc._doc(f.name)}"),
            "from typing import Any", "",
            "from fastapi import APIRouter, Body, Request", "",
            "from app.common import checks",
            "from app.common.ctx import Ctx",
            "from app.common.db import NOW, Col, Table, auto_no, transaction  # noqa: F401",
            "from app.common.errors import not_implemented  # noqa: F401",
            "from app.common.user import current_user",
            f"from app.logic import {module_name(f.function_id)} as logic", "",
            'router = APIRouter(prefix="/api/v1")',
            *[_table_decl(spec, t) for t in tables], *consts]
    return "\n".join(head) + "\n\n\n" + "\n\n\n".join(bodies) + "\n"


def hole(name: str, body: str) -> str:
    """업무 로직 자리. 표시를 남겨 어디가 업무 로직인지 보이게 한다."""
    inner = "\n".join("    " + line if line.strip() else "" for line in body.strip().splitlines())
    return f"    # <hole {name}>\n{inner}\n    # </hole>"


def py_text(body: str) -> str:
    """자리에 넣을 Python 본문 (통째로 들여 쓴 답은 들여쓰기를 걷는다). develop --logic의 검사도 이 글자를 본다."""
    return textwrap.dedent(body.replace("\t", "    ")).strip("\n")


def _python_body(body: str | None) -> str | None:
    """채운 자리가 Python 함수 본문으로 읽히는 것만 쓴다 (Java로 채운 기록은 쓰지 않는다)."""
    if not body or not body.strip():
        return None
    body = py_text(body)
    if any(line.rstrip().endswith(";") for line in body.splitlines()):
        return None   # Java 문장 끝(;)은 Python에서 문법은 맞아도 Java 본문이다 (중괄호로 끝나는 줄은 dict일 수 있다)
    try:
        compile("def _(data, ctx, crud):\n" + textwrap.indent(body, "    ") + "\n", "<hole>", "exec")
    except SyntaxError:
        return None
    return body


def _logic(spec: Spec, f: FunctionSpec, ops: list[dict], task: str, hooks: dict[str, str]) -> str:
    """기능 하나의 업무 로직 자리. 규칙은 문서 문자열에 적고, 고객 답을 기다리는 규칙은 TODO로 남긴다."""
    rows = spec.acceptance[f.function_id]
    notes = []
    for rule in f.business_rules:
        kind, text, qids = rule_tag(rule)
        ac = next((r["id"] for r in rows if r["then"] == text and r["kind"].startswith("규칙")), "")
        notes.append(f"- {'TODO(' + (', '.join(qids) or '확인 필요') + ') ' if kind == '보류' else ''}[{kind}] {ac} {sc._doc(text)}".rstrip())
    notes += [f"- [자동 처리] {a['id']} {sc._doc(a['name'])}: {sc._doc(a['when'])} → {sc._doc(a['action'])}" for a in spec.automations_of(f.function_id)]
    doc = "\n".join([f"{f.function_id} {sc._doc(f.name)} ({task or '작업 없음'})", *([""] + notes if notes else [])])
    lines = ["# 업무 로직 자리. # <hole> 안만 채운다 (develop --logic이 채우거나 사람이 고친다. 사람이 고친 파일은 덮어쓰지 않는다).",
             '"""' + doc.replace('\\', '/').replace('"""', "'''") + '\n"""',
             "from typing import Any, Callable", "", "from app.common.ctx import Ctx"]
    bodies = []
    for op, name in zip(ops, func_names(ops)):
        bodies.append(f"def {name}(data: dict, ctx: Ctx, crud: Callable[[], Any]) -> Any:\n"
                      f"    {_s(op['method'] + ' ' + op['path'] + ' — ' + sc._doc(op.get('summary') or ''))}\n"
                      f"{hole(name, _python_body(hooks.get(name)) or 'return crud()')}")
    if not ops:
        reason = sc._doc((spec.api.get(f.function_id) or {}).get("no_api_reason", ""))
        bodies.append(f"def run(ctx: Ctx) -> None:\n    {_s('자동 실행 (API 없음). 실행 시점은 스펙의 이유를 따른다: ' + reason)}\n"
                      f"{hole('run', _python_body(hooks.get('run')) or '# 구현 전')}")
    return "\n".join(lines) + "\n\n\n" + "\n\n\n".join(bodies) + "\n"


def _acceptance_test(spec: Spec, f: FunctionSpec) -> str:
    """수용 기준 하나 = 테스트 하나 (이름에 AC 번호). 내용을 채우기 전에는 건너뛴다."""
    tests = [f"@pytest.mark.skip(reason='구현 전')\ndef test_ac{n}():\n    {_s(sc._doc(row['id'] + ' ' + row['kind'] + ': ' + row['then'])[:180])}"
             for n, row in enumerate(spec.acceptance[f.function_id], start=1)]
    head = ["# 코드가 만든 파일. 수용 기준(ACCEPTANCE_CRITERIA.md) 하나가 테스트 하나다.",
            _s(f"{f.function_id} {sc._doc(f.name)}"), "import pytest"]
    return "\n".join(head) + "\n\n\n" + "\n\n\n".join(tests) + "\n"


def _lookups(spec: Spec, data: dict) -> str:
    """화면 보조 API (/api/ui): 선택 목록과 수정 폼의 처음 값 (scaffold._lookup_controller와 같다)."""
    refs = sorted({f["ref"] for s in data["screens"] for f in sc._screen_fields(s) if f.get("ref")})
    by_name = {spec.names.table(e.entity_id): e for e in spec.model.entities}
    lookups = []
    for ref in refs:
        entity = by_name.get(ref)
        if entity and entity.pk():
            pk = spec.names.column(entity.entity_id, entity.pk()[0].name)
            label = spec.names.column(entity.entity_id, label_column(entity))
            lookups.append(f"{_s(ref)}: ({_s(_q(spec, ref))}, {_s(_q(spec, pk))}, {_s(_q(spec, label))})")
    owned = {o["operation_id"]: o for _, o in spec.operations()}
    tables: list[str] = []
    forms: list[str] = []
    for s in data["screens"]:
        op = owned.get((s.get("update") or {}).get("operation_id"))
        table = op_table(spec, op, s["function_id"]) if op else ""
        entity = next((e for e in spec.model.entities if e.entity_id == table), None)
        cols = sc._cols(spec, table, op.get("request") or [], writable=True) if entity and len(entity.pk()) == 1 else []
        if cols:
            tables.append(table)
            roles = [*sc._roles_of(spec, op)]
            forms.append(f"{_s(s['screen_id'])}: ({_table_const(table)}, {_cols(spec, table, cols)}, {roles!r})")
    decls = "\n".join(_table_decl(spec, t) for t in dict.fromkeys(tables))
    lookup_items = "".join(f"    {x},\n" for x in lookups)
    form_items = "".join(f"    {x},\n" for x in forms)
    return f'''# 코드가 만든 파일 (스펙에서 생성). 화면 보조 API다: 선택 목록, 수정 폼의 처음 값.
from fastapi import APIRouter, Request

from app.common.db import Col, Table, as_text, transaction  # noqa: F401
from app.common.errors import ApiError
from app.common.user import current_user

router = APIRouter(prefix="/api/ui")
{decls}
# 화면이 참조하는 테이블 → (테이블, 기본키, 이름 컬럼)
TABLES = {{
{lookup_items}}}
# 수정 화면 → (테이블, 고칠 수 있는 칸, 수정 API와 같은 역할)
FORMS = {{
{form_items}}}


@router.get("/lookups/{{table}}")
def lookup(request: Request, table: str):
    current_user(request).require_login()
    found = TABLES.get(table)
    if found is None:
        raise ApiError.not_found()
    name, pk, label = found
    with transaction() as db:
        return db.query(f'SELECT {{as_text(pk)}} AS "value", {{as_text(label)}} AS "label" FROM {{name}} ORDER BY {{pk}} LIMIT 500')


@router.get("/rows/{{screen}}/{{id}}")
def row(request: Request, screen: str, id: str):
    """수정 폼의 처음 값: 그 화면이 고칠 수 있는 칸만, 수정 API와 같은 역할로 읽는다"""
    user = current_user(request)
    form = FORMS.get(screen)
    if form is None or not form[2]:
        user.require_login()
    else:
        user.require(*form[2])
    if form is None:
        return {{}}
    with transaction() as db:
        return db.one(form[0], form[1], id)
'''


def _routers(modules: list[tuple[str, str]]) -> str:
    """main.py가 붙일 라우터 목록 (기능 순서). modules는 (패키지, 모듈)."""
    lines = ["# 코드가 만든 파일. main.py가 붙이는 API 목록이다 (화면 보조 API + 기능별 API).",
             "from app.api import lookups"]
    lines += [f"from app.{pkg} import {mod}" for pkg, mod in modules]
    lines += ["", "ROUTERS = [", "    lookups.router,", *[f"    {mod}.router," for _, mod in modules], "]", ""]
    return "\n".join(lines)


def python_module(text: str | None) -> bool:
    """기능 구현 결과가 router를 가진 Python 모듈인가 (Java로 쓴 기록은 FastAPI 틀에서 쓰지 않는다)."""
    try:
        compile(text or "", "<built>", "exec")
    except SyntaxError:
        return False
    return bool(text and "router" in text)


def render_backend(spec: Spec, data: dict, owned: dict[str, list[dict]], logic: dict[str, dict] | None = None,
                   built: dict[str, dict] | None = None) -> dict[str, str]:
    """FastAPI 서버 파일 {상대 경로: 내용} (모두 backend/ 아래). scaffold.render_app이 Spring 대신 부른다.

    data는 build_screens 결과(accounts 포함), owned는 scaffold.owned_operations의 기능 → API.
    logic은 기능별 채운 자리 {기능 ID: {함수 이름: Python 본문}}, built는 기능 구현 결과 {기능 ID: {controller: Python 모듈}}:
    Python으로 읽히지 않는 기록(Java)은 쓰지 않고 코드가 만든 틀을 쓴다."""
    logic = logic or {}
    built = {fid: r for fid, r in (built or {}).items() if python_module(r.get("controller"))}
    tasks = {s["function_id"]: s["task"] for s in [*data["screens"], *data["autos"]]}
    files = {f"backend/{p.relative_to(TEMPLATES).as_posix()}": p.read_text(encoding="utf-8")
             for p in sorted(TEMPLATES.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    modules: list[tuple[str, str]] = []
    for f in spec.functions:
        mod = module_name(f.function_id)
        ops = owned.get(f.function_id) or []
        if f.function_id in built:
            files[f"{ROOT}/feature/{mod}.py"] = built[f.function_id]["controller"]
            modules.append(("feature", mod))
        else:
            if ops:
                files[f"{ROOT}/api/{mod}.py"] = _api(spec, f, ops)
                modules.append(("api", mod))
            files[f"{ROOT}/logic/{mod}.py"] = _logic(spec, f, ops, tasks.get(f.function_id, ""), logic.get(f.function_id) or {})
        files[f"backend/tests/acceptance/test_{mod}.py"] = _acceptance_test(spec, f)
    files[f"{ROOT}/api/lookups.py"] = _lookups(spec, data)
    files[f"{ROOT}/api/__init__.py"] = _routers(modules)
    files[f"{ROOT}/accounts.json"] = json.dumps(data.get("accounts") or sc.demo_accounts(data), ensure_ascii=False, indent=1) + "\n"
    return files
