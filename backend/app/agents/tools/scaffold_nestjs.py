"""개발 단계 뼈대 (NestJS · TypeScript · Node 20): 개발 스펙에서 Spring·FastAPI 틀과 같은 HTTP 계약의 서버 코드를 만든다 (LLM 없음, 토큰 0).

- 같은 경로·JSON 모양·오류 코드·필수값·기본값·상태 전환이다. 화면(frontend)은 바꾸지 않고 그대로 붙는다.
- 고정 틀(templates/backend-nestjs)은 그대로 복사하고, 스펙에 따라 달라지는 파일(기능별 API·업무 로직·화면 보조 API·계정)만 만든다.
- 기본 동작을 정하는 규칙은 scaffold.py의 것을 그대로 쓰고(Java 값을 TypeScript 값으로만 옮긴다), 모양은 scaffold_fastapi와 같다.
- 업무 로직 자리는 '// <hole 이름>' … '// </hole>'이고 기본 본문은 'return crud();'다. develop --logic이 채운 TypeScript 본문만 넣는다 (Java·Python 기록은 쓰지 않는다).
"""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

from app.agents.tools import scaffold as sc
from app.agents.tools import scaffold_fastapi as fa
from app.agents.tools.dev_spec import Spec, rule_tag
from app.agents.tools.screens import SECRET_COLUMN, action_of, label_column, op_kind, op_table, path_params
from app.schemas.function import FunctionSpec

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "backend-nestjs"
ROOT = "backend/src"
NEST = {"GET": "Get", "POST": "Post", "PUT": "Put", "PATCH": "Patch", "DELETE": "Delete"}
# 함수 이름으로 쓸 수 없는 낱말(JS 예약어)과 업무 로직 모듈 안에서 이미 쓰는 이름
TS_WORDS = {"break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do", "else", "enum", "export", "extends",
            "false", "finally", "for", "function", "if", "import", "in", "instanceof", "new", "null", "return", "super", "switch", "this",
            "throw", "true", "try", "typeof", "var", "void", "while", "with", "yield", "let", "static", "implements", "interface", "package",
            "private", "protected", "public", "await", "async", "arguments", "eval"}
TAKEN = {"Ctx", "Data", "run"}

DOCKERFILE = """# 한 번에 빌드: 화면(Node) → 서버(NestJS, TypeScript 컴파일) → 실행 이미지(Node, 화면을 함께 담음). docker compose up --build가 쓴다.
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
ENV VITE_API=server
RUN npm run build

FROM node:20-alpine AS server
WORKDIR /src
COPY backend/package.json ./
RUN npm install --no-audit --no-fund
COPY backend/ ./
RUN npm run build && npm prune --omit=dev

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=server /src/package.json ./
COPY --from=server /src/node_modules ./node_modules
COPY --from=server /src/dist ./dist
COPY --from=web /web/dist ./static
ENV LOCAL_DIR=/app/local STATIC_DIR=/app/static
EXPOSE 8080
CMD ["node", "dist/main.js"]
"""

DOCKERIGNORE = "**/node_modules\n**/dist\n**/dist-check\nlocal\nbackend/local\n.req-agent\n.preview\n.git\n"

# Spring의 COMPOSE·COMPOSE_MYSQL과 같고 app 서비스의 DB_URL만 Node(pg·mysql2) 형식이다
PG_URL = "postgresql://app:app@{host}/app"
MYSQL_URL = "mysql://app:app@{host}/app"


def _compose(text: str, old: str, url: str) -> str:
    if old not in text:
        raise sc.AppError("NestJS용 compose를 만들 수 없습니다: Spring compose의 DB_URL이 바뀌었습니다.")
    return text.replace(old, f"      DB_URL: {url}\n      LOCAL_DIR: /app/local\n")


COMPOSE = _compose(sc.COMPOSE, "      DB_URL: jdbc:postgresql://db:5432/app\n      DB_USER: app\n      DB_PASSWORD: app\n", PG_URL.format(host="db:5432"))
COMPOSE_MYSQL = _compose(sc.COMPOSE_MYSQL, f'      DB_URL: "{sc.MYSQL_URL.format(host="db:3306")}"\n      DB_USER: app\n      DB_PASSWORD: app\n',
                         f'"{MYSQL_URL.format(host="db:3306")}"')


def dev_steps(mysql: bool) -> list[str]:
    """README의 '고치면서 띄우기' (Spring의 mvn 대신)."""
    url = MYSQL_URL.format(host="localhost:3307") if mysql else PG_URL.format(host="localhost:5433")
    return ["docker compose up -d db                 # " + ("MySQL만 (포트 3307)" if mysql else "PostgreSQL만 (포트 5433)"),
            "cd backend && npm install && npm run build",
            f"cd backend && DB_URL='{url}' npm start   # 서버 8080 (계정 파일은 backend/local/accounts.txt)",
            "cd frontend && VITE_API=server npm run dev"]


# ------------------------------------------------------------------ 이름·값
module_name = fa.module_name   # FN-SFR-001-05 → fn_sfr00105 (파일 이름)


def controller_name(function_id: str) -> str:
    return sc.class_name(function_id) + "Controller"


def func_names(ops: list[dict]) -> list[str]:
    """API마다 함수 이름 (= 업무 로직 자리 이름). 예약어·모듈 안 이름과 겹치면 Op를 붙인다."""
    out: list[str] = []
    for op in ops:
        name = sc.method_name(op["operation_id"])
        while name in TS_WORDS or name in TAKEN or name in out or not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            name += "Op"
        out.append(name)
    return out


def _s(value: str) -> str:
    """TypeScript 문자열 리터럴 (작은따옴표)."""
    body = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return "'" + body.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + "'"


def _ts(java: str) -> str:
    """scaffold.py가 만든 Java 값(_defaults·_stamps·_literal) → TypeScript 값."""
    if java == "Db.NOW":
        return "NOW"
    if java == "user.id()":
        return "user.id"
    if m := re.fullmatch(r"Db\.autoNo\((\d+)\)", java):
        return f"autoNo({m.group(1)})"
    if java in ("true", "false"):
        return java
    if re.fullmatch(r"-?\d+L", java):
        return java[:-1]
    if re.fullmatch(r"-?\d+(\.\d+)?", java):
        return java
    if java.startswith('"'):
        return _s(json.loads(java))
    raise ValueError(f"옮길 수 없는 값: {java}")


def _pairs(java: list[str]) -> str:
    """Db.values(이름, 값 …) 인자 → 객체 리터럴."""
    items = [f"{_ts(java[i])}: {_ts(java[i + 1])}" for i in range(0, len(java), 2)]
    return "{ " + ", ".join(items) + " }" if items else "{}"


def _strings(values) -> str:
    return "[" + ", ".join(_s(v) for v in values) + "]"


def _col(field: str, column: str, kind: str) -> str:
    return f"col({_s(field)}, {_s(column)}, {_s(kind)})"


def _list(items: list[str]) -> str:
    return "[\n  " + ",\n  ".join(items) + ",\n]" if items else "[]"


def _cols(spec: Spec, table: str, cols: list[tuple[str, str]]) -> str:
    return _list([_col(f, sc._q(spec, spec.names.column(table, c)), sc._pg(spec, table, c)) for f, c in cols])


def _table_const(table: str) -> str:
    return "T_" + table.upper()


def _table_decl(spec: Spec, table: str) -> str:
    entity = next(e for e in spec.model.entities if e.entity_id == table)
    names = {a.name for a in entity.attributes}
    created = [sc._q(spec, spec.names.column(table, c)) for c in sc.AUDIT_NOW if c in names]
    updated = [sc._q(spec, spec.names.column(table, c)) for c in sc.AUDIT_NOW[1:] if c in names]
    pk = entity.pk()[0].name
    return (f"const {_table_const(table)} = new Table({_s(sc._q(spec, spec.names.table(table)))}, {_s(sc._q(spec, spec.names.column(table, pk)))}, "
            f"{_s(sc._pg(spec, table, pk))}, {_strings(created)}, {_strings(updated)});")


def _filters(spec: Spec, table: str, cols: list[tuple[str, str]]) -> str:
    physical = sc._q(spec, spec.names.table(table))
    return _list([_col(f, physical + "." + sc._q(spec, spec.names.column(table, c)), sc._pg(spec, table, c)) for f, c in cols])


def _joined(spec: Spec, table: str, fields: list[dict]) -> tuple[list[str], list[str]]:
    """조회 응답의 col 목록과 JOIN (scaffold._joined와 같은 규칙: 외래키로 가리키는 부모 테이블의 값은 함께 읽는다)."""
    cols, joins, aliases, seen = [], [], {}, set()
    entity = next(e for e in spec.model.entities if e.entity_id == table)
    physical = sc._q(spec, spec.names.table(table))
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
                joins.append(f"LEFT JOIN {sc._q(spec, spec.names.table(t))} {aliases[t]} ON {aliases[t]}.{sc._q(spec, spec.names.column(t, parent.pk()[0].name))}"
                             f" = {physical}.{sc._q(spec, spec.names.column(table, fk.name))}")
            source = aliases[t]
        seen.add(f["name"])
        cols.append(_col(f["name"], source + "." + sc._q(spec, spec.names.column(t, column)), sc._pg(spec, t, column)))
    return cols, joins


# ------------------------------------------------------------------ 기본 동작 (scaffold._crud_call과 같은 갈래)
def _crud(spec: Spec, op: dict, table: str, consts: list[str], prefill: list[str]) -> str:
    """이 API의 기본 동작 (TypeScript 식, Promise). 만들 수 없으면 '구현 전' 오류를 낸다. prefill은 호출 전에 data를 채우는 문장이다."""
    kind, name = op_kind(op), sc._constant(op["operation_id"])
    todo = f"notImplemented({_s(op['operation_id'])})"
    if not table:
        return todo
    base = _table_const(table)
    id_var = path_params(op["path"])[-1] if path_params(op["path"]) else ""
    get_id = f"data[{_s(id_var)}]"
    out, joins = _joined(spec, table, op.get("response") or [])
    consts.append(f"const OUT_{name}: Col[] = {_list(out)};")
    t = base
    if joins:
        t = f"V_{name}"
        consts.append(f"const {t} = {base}.withJoins({_s(' '.join(joins))});")

    def decl(prefix: str, cols: list[tuple[str, str]]) -> None:
        consts.append(f"const {prefix}_{name}: Col[] = {_cols(spec, table, cols)};")

    parents = sc._parent_cols(spec, table, op)
    keys = next(e.pk() for e in spec.model.entities if e.entity_id == table)
    pk = keys[0].name
    if len(keys) > 1 and kind not in ("list", "report", "create", "add"):   # 복합 기본키는 한 행을 ID 하나로 찾을 수 없다
        return todo
    by_parent = bool(id_var) and id_var in {p for p, _ in parents}
    if by_parent and kind not in ("list", "report", "create", "add", "get"):
        return todo
    if by_parent and kind == "get":
        consts.append(f"const FILTER_{name}: Col[] = {_filters(spec, table, parents)};")
        return f"db.first({t}, OUT_{name}, FILTER_{name}, data)"
    if kind == "report" and id_var and sc._snake(id_var) == pk:
        return f"db.one({t}, OUT_{name}, {get_id})"
    if kind in ("list", "report"):
        filters = sc._unique([*sc._cols(spec, table, op.get("request") or [], writable=True), *parents])
        consts.append(f"const FILTER_{name}: Col[] = {_filters(spec, table, filters)};")
        return f"db.{'page' if kind == 'list' else 'first'}({t}, OUT_{name}, FILTER_{name}, data)"
    if kind == "get":
        return f"db.one({t}, OUT_{name}, {get_id})"
    if kind == "delete":
        return f"db.delete({base}, {get_id})"
    if kind in ("create", "add"):
        cols = sc._unique([*sc._cols(spec, table, op.get("request") or [], writable=True), *parents])
        pairs, users, missing = sc._defaults(spec, table, {c for _, c in cols}, op)
        for c in users:
            prefill += [f"if (data[{_s(sc._camel(c))}] == null) {{", f"  data[{_s(sc._camel(c))}] = user.id;", "}"]
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
        allowed = "{ " + ", ".join(f"{_s(to)}: {_strings(froms)}" for to, froms in move["moves"].items()) + " }"
        return (f"db.moveTo({t}, {_s(sc._q(spec, spec.names.column(table, move['column'])))}, {allowed}, data[{_s(target)}], "
                f"{get_id}, OUT_{name}{extra})")
    if sets and id_var and sets.get("from"):   # 전환표에 있는 처리: 허용된 상태에서만 바꾼다
        return f"db.move({t}, {_s(sc._q(spec, sets['column']))}, {_strings(sets['from'])}, {_s(sets['value'])}, {get_id}, OUT_{name}{extra})"
    if sets and id_var:
        return f"db.set({t}, {_s(sc._q(spec, sets['column']))}, {_s(sets['value'])}, {get_id}, OUT_{name}{extra})"
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
        return ["if (!user.isSystem()) {", f"  user.require({roles});", "}"]
    if roles:
        return [f"user.require({roles});"]
    return ["user.requireSystem();"] if system else ["user.requireLogin();"]


def nest_path(path: str) -> str:
    """/orders/{orderId} → /orders/:orderId (Nest 경로)."""
    return re.sub(r"\{(\w+)\}", r":\1", path)


DB_NAMES = ("Col", "NOW", "Table", "autoNo", "col", "transaction")


def _api(spec: Spec, f: FunctionSpec, ops: list[dict]) -> str:
    """기능 하나의 API 컨트롤러. 경로·필드·필수값·허용값은 스펙에서 오고, 업무 규칙은 logic 모듈의 자리로 넘긴다."""
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
        lines = [f"/** {op['method']} {sc._doc(op['path'])} — {sc._doc(op.get('summary') or '')} */",
                 f"@{NEST[op['method']]}({_s(nest_path(op['path']))})",
                 *(["@HttpCode(200)"] if op["method"] == "POST" else []),
                 f"async {name}(@Req() req: Request) {{",
                 "  const user = currentUser(req);"]
        lines += [f"  {x}" for x in _access(spec, op)]
        lines.append(f"  const data = checks.{'queryOf(req.query)' if reads else 'inputOf(req.body)'};")
        lines += [f"  data[{_s(p)}] = req.params[{_s(p)}];" for p in path_params(op["path"])]
        lines += [f"  {x}" for x in prefill]
        required = [x["name"] for x in op.get("request") or [] if x.get("required") and not reads]
        if required:
            lines.append(f"  checks.required(data, {', '.join(_s(r) for r in required)});")
        for x in op.get("request") or []:
            t, _, c = (x.get("column") or "").partition(".")
            attr = spec.columns.get((t, c))
            if attr and attr.allowed_values:
                lines.append(f"  checks.oneOf(data, {_s(x['name'])}, {', '.join(_s(v) for v in attr.allowed_values)});")
        lines += ["  // 업무 로직·자동 처리까지 한 묶음: 예외가 나면 모두 되돌린다",
                  f"  return transaction((db) => logic.{name}(data, new Ctx(db, user), () => {crud}));", "}"]
        bodies.append("\n".join("  " + x for x in lines))
    decls = [*[_table_decl(spec, t) for t in tables], *consts]
    used = "\n".join([*decls, *bodies])
    nest = sorted({"Controller", "Req", *(NEST[op["method"]] for op in ops), *(["HttpCode"] if any(op["method"] == "POST" for op in ops) else [])})
    db_names = [n for n in DB_NAMES if re.search(rf"\b{n}\b", used)]
    head = ["// 코드가 만든 파일 (스펙에서 생성). 고치지 말고 스펙을 고치세요. 업무 규칙은 src/logic에 있습니다.",
            f"/** {f.function_id} {sc._doc(f.name)} */",
            f"import {{ {', '.join(nest)} }} from '@nestjs/common';",
            "import type { Request } from 'express';", "",
            "import * as checks from '../common/checks';",
            "import { Ctx } from '../common/ctx';",
            f"import {{ {', '.join(db_names)} }} from '../common/db';",
            *(["import { notImplemented } from '../common/errors';"] if "notImplemented(" in used else []),
            "import { currentUser } from '../common/user';",
            f"import * as logic from '../logic/{module_name(f.function_id)}';"]
    parts = ["\n".join(head)]
    if decls:
        parts.append("\n".join(decls))
    parts.append("@Controller('api/v1')\n" + f"export class {controller_name(f.function_id)} {{\n" + "\n\n".join(bodies) + "\n}")
    return "\n\n".join(parts) + "\n"


def hole(name: str, body: str) -> str:
    """업무 로직 자리. 표시를 남겨 어디가 업무 로직인지 보이게 한다."""
    inner = "\n".join("  " + line if line.strip() else "" for line in body.strip().splitlines())
    return f"  // <hole {name}>\n{inner}\n  // </hole>"


STRING = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`", re.S)
# 다른 틀의 본문 표시: Spring 틀의 API(crud.get()·ctx.db())·Java 타입 선언, Python 문장
JAVA_HINT = re.compile(r"\bcrud\.get\(\)|\bctx\.(?:db|user)\(\)|\b(?:Map|List|Set)\.of\(|\bjava\.\w|\.equals\("
                       r"|\b(?:String|Object|Long|Integer|Boolean|int|long|boolean|double)\s+\w+\s*=")
PY_HINT = re.compile(r"^\s*(?:def |elif\b|raise\b|except\b|from [\w.]+ import\b|#)|^\s*(?:if|for|while|else|try|with)\b[^{\n]*:\s*$"
                     r"|\b(?:None|True|False)\b", re.M)


def _code(body: str) -> str:
    """문자열·주석을 비운 TypeScript (표시는 코드에서만 본다)."""
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", STRING.sub("''", body), flags=re.S))


def foreign_body(body: str) -> str:
    """TypeScript가 아닌 본문이면 그 언어 이름 (Java · Python), 아니면 빈 글자."""
    code = _code(body)
    return "Java" if JAVA_HINT.search(code) else "Python" if PY_HINT.search(code) else ""


def _ts_body(body: str | None) -> str | None:
    """채운 자리가 TypeScript 본문으로 읽히는 것만 쓴다 (Java·Python 기록, 괄호가 맞지 않는 본문은 쓰지 않는다)."""
    code = _code(body or "")
    if not code.strip() or foreign_body(body) or any(code.count(a) != code.count(b) for a, b in ("{}", "()", "[]")):
        return None
    return textwrap.dedent(body.replace("\t", "  ")).strip("\n")


def _logic(spec: Spec, f: FunctionSpec, ops: list[dict], task: str, hooks: dict[str, str] | None = None) -> str:
    """기능 하나의 업무 로직 자리. 규칙은 머리 주석에 적고, 고객 답을 기다리는 규칙은 TODO로 남긴다.
    hooks는 develop --logic이 채운 자리 {함수 이름: TypeScript 본문}이다 (사람이 고친 파일은 write_app이 그대로 둔다)."""
    hooks = hooks or {}
    rows = spec.acceptance[f.function_id]
    notes = []
    for rule in f.business_rules:
        kind, text, qids = rule_tag(rule)
        ac = next((r["id"] for r in rows if r["then"] == text and r["kind"].startswith("규칙")), "")
        notes.append(f"- {'TODO(' + (', '.join(qids) or '확인 필요') + ') ' if kind == '보류' else ''}[{kind}] {ac} {sc._doc(text)}".rstrip())
    notes += [f"- [자동 처리] {a['id']} {sc._doc(a['name'])}: {sc._doc(a['when'])} → {sc._doc(a['action'])}" for a in spec.automations_of(f.function_id)]
    doc = [f"{f.function_id} {sc._doc(f.name)} ({sc._doc(task) or '작업 없음'})", *([""] + notes if notes else [])]
    lines = ["// 업무 로직 자리. // <hole> 안만 채운다 (사람이 고친다. 사람이 고친 파일은 다시 생성해도 덮어쓰지 않는다).",
             "/**", *[(" * " + x).rstrip() for x in doc], " */",
             "import type { Ctx } from '../common/ctx';"]
    bodies = []
    if ops:
        lines.append("import type { Data } from '../common/db';")
    for op, name in zip(ops, func_names(ops)):
        bodies.append(f"/** {op['method']} {sc._doc(op['path'])} — {sc._doc(op.get('summary') or '')} */\n"
                      f"export async function {name}(data: Data, ctx: Ctx, crud: () => Promise<unknown>): Promise<unknown> {{\n"
                      f"{hole(name, _ts_body(hooks.get(name)) or 'return crud();')}\n}}")
    if not ops:
        reason = sc._doc((spec.api.get(f.function_id) or {}).get("no_api_reason", ""))
        bodies.append(f"/** 자동 실행 (API 없음). 실행 시점은 스펙의 이유를 따른다: {reason} */\n"
                      f"export async function run(ctx: Ctx): Promise<void> {{\n{hole('run', _ts_body(hooks.get('run')) or '// 구현 전')}\n}}")
    return "\n".join(lines) + "\n\n" + "\n\n".join(bodies) + "\n"


def _acceptance_test(spec: Spec, f: FunctionSpec) -> str:
    """수용 기준 하나 = 테스트 하나 (이름에 AC 번호). 내용을 채우기 전에는 건너뛴다 (node --test)."""
    tests = [f"test({_s(sc._doc(row['id'] + ' ' + row['kind'] + ': ' + row['then'])[:180])}, {{ skip: '구현 전' }}, () => {{}});"
             for row in spec.acceptance[f.function_id]]
    head = ["// 코드가 만든 파일. 수용 기준(ACCEPTANCE_CRITERIA.md) 하나가 테스트 하나다 (npm test).",
            f"// {f.function_id} {sc._doc(f.name)}", "import { test } from 'node:test';"]
    return "\n".join(head) + "\n\n" + "\n".join(tests) + "\n"


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
            lookups.append(f"[{_s(ref)}, [{_s(sc._q(spec, ref))}, {_s(sc._q(spec, pk))}, {_s(sc._q(spec, label))}]]")
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
            roles = _strings(sc._roles_of(spec, op))
            items = ", ".join(_col(f, sc._q(spec, spec.names.column(table, c)), sc._pg(spec, table, c)) for f, c in cols)
            forms.append(f"[{_s(s['screen_id'])}, [{_table_const(table)}, [{items}], {roles}]]")
    decls = "".join(_table_decl(spec, t) + "\n" for t in dict.fromkeys(tables))
    lookup_items = "".join(f"  {x},\n" for x in lookups)
    form_items = "".join(f"  {x},\n" for x in forms)
    return f'''// 코드가 만든 파일 (스펙에서 생성). 화면 보조 API다: 선택 목록, 수정 폼의 처음 값.
import {{ Controller, Get, Param, Req }} from '@nestjs/common';
import type {{ Request }} from 'express';

import {{ Col, Table, asText, col, transaction }} from '../common/db';
import {{ ApiError }} from '../common/errors';
import {{ currentUser }} from '../common/user';

{decls}// 화면이 참조하는 테이블 → (테이블, 기본키, 이름 컬럼)
const TABLES = new Map<string, [string, string, string]>([
{lookup_items}]);
// 수정 화면 → (테이블, 고칠 수 있는 칸, 수정 API와 같은 역할)
const FORMS = new Map<string, [Table, Col[], string[]]>([
{form_items}]);

@Controller('api/ui')
export class LookupsController {{
  @Get('lookups/:table')
  async lookup(@Req() req: Request, @Param('table') table: string) {{
    currentUser(req).requireLogin();
    const found = TABLES.get(table);
    if (!found) {{
      throw ApiError.notFound();
    }}
    const [name, pk, label] = found;
    return transaction((db) => db.query(`SELECT ${{asText(pk)}} AS "value", ${{asText(label)}} AS "label" FROM ${{name}} ORDER BY ${{pk}} LIMIT 500`));
  }}

  /** 수정 폼의 처음 값: 그 화면이 고칠 수 있는 칸만, 수정 API와 같은 역할로 읽는다 */
  @Get('rows/:screen/:id')
  async row(@Req() req: Request, @Param('screen') screen: string, @Param('id') id: string) {{
    const user = currentUser(req);
    const form = FORMS.get(screen);
    if (!form || !form[2].length) {{
      user.requireLogin();
    }} else {{
      user.require(...form[2]);
    }}
    if (!form) {{
      return {{}};
    }}
    return transaction((db) => db.one(form[0], form[1], id));
  }}
}}
'''


def _index(modules: list[tuple[str, str, str]]) -> str:
    """app.module.ts가 붙일 컨트롤러 목록 (기능 순서). modules는 (폴더, 모듈, 클래스)."""
    lines = ["// 코드가 만든 파일. app.module.ts가 붙이는 API 목록이다 (화면 보조 API + 기능별 API, develop --build가 쓴 기능은 src/feature).",
             "import { LookupsController } from './lookups';"]
    lines += [f"import {{ {cls} }} from '{'.' if folder == 'api' else '../feature'}/{mod}';" for folder, mod, cls in modules]
    lines += ["", "export const CONTROLLERS = [", "  LookupsController,", *[f"  {cls}," for _, _, cls in modules], "];", ""]
    return "\n".join(lines)


def ts_module(text: str | None) -> bool:
    """기능 구현 결과가 Nest 컨트롤러 TypeScript인가 (Java·Python으로 쓴 기록은 NestJS 틀에서 쓰지 않는다)."""
    return bool(text and re.search(r"^@Controller\(", text, re.M) and re.search(r"^export class \w+", text, re.M)
                and not re.search(r"^(package |def |from \w+ import )", text, re.M))


def render_backend(spec: Spec, data: dict, owned: dict[str, list[dict]], built: dict[str, dict] | None = None,
                   logic: dict[str, dict] | None = None) -> dict[str, str]:
    """NestJS 서버 파일 {상대 경로: 내용} (모두 backend/ 아래). scaffold.render_app이 Spring 대신 부른다.

    data는 build_screens 결과(accounts 포함), owned는 scaffold.owned_operations의 기능 → API.
    built는 기능 구현 결과 {기능 ID: {controller: TypeScript}}: TypeScript로 읽히지 않는 기록(Java·Python)은 쓰지 않고 코드가 만든 틀을 쓴다.
    logic은 기능별 채운 자리 {기능 ID: {함수 이름: TypeScript 본문}}이다."""
    logic = logic or {}
    built = {fid: r for fid, r in (built or {}).items() if ts_module(r.get("controller"))}
    tasks = {s["function_id"]: s["task"] for s in [*data["screens"], *data["autos"]]}
    files = {f"backend/{p.relative_to(TEMPLATES).as_posix()}": p.read_text(encoding="utf-8")
             for p in sorted(TEMPLATES.rglob("*")) if p.is_file() and not {"node_modules", "dist"} & set(p.parts)}
    modules: list[tuple[str, str, str]] = []
    for f in spec.functions:
        mod = module_name(f.function_id)
        ops = owned.get(f.function_id) or []
        if f.function_id in built:
            files[f"{ROOT}/feature/{mod}.ts"] = built[f.function_id]["controller"]
            modules.append(("feature", mod, controller_name(f.function_id)))
        else:
            if ops:
                files[f"{ROOT}/api/{mod}.ts"] = _api(spec, f, ops)
                modules.append(("api", mod, controller_name(f.function_id)))
            files[f"{ROOT}/logic/{mod}.ts"] = _logic(spec, f, ops, tasks.get(f.function_id, ""), logic.get(f.function_id))
        files[f"backend/test/acceptance/{mod}.test.mjs"] = _acceptance_test(spec, f)
    files[f"{ROOT}/api/lookups.ts"] = _lookups(spec, data)
    files[f"{ROOT}/api/index.ts"] = _index(modules)
    files[f"{ROOT}/accounts.json"] = json.dumps(data.get("accounts") or sc.demo_accounts(data), ensure_ascii=False, indent=1) + "\n"
    return files
