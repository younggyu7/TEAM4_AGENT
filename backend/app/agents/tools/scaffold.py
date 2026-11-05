"""개발 단계 뼈대: 개발 스펙에서 실행할 수 있는 서비스 프로젝트를 코드로 만든다 (LLM 없음, 토큰 0).

- 같은 스펙이면 파일 내용이 바이트까지 같다. 시각·난수를 쓰지 않고, 고정 틀(templates/app)에 스펙 값만 넣는다.
- 화면은 screens.json 값대로 고정 틀 하나가 그린다. API는 기능마다 컨트롤러 하나, DB 처리는 공통 Db 하나다.
- 등록·조회·수정·삭제와 상태 바꾸기는 코드가 만든 기본 동작으로 돌아간다. 업무 규칙은 기능마다 정해진 자리(hole)에만 들어가고,
  그 자리는 agents/developer.py(LLM)가 채운다. 채운 결과는 .req-agent/logic/에 저장해 같은 스펙이면 다시 묻지 않는다.
- 이 앱은 기본으로 예시 데이터로 뜬다. 고객에게 화면을 보여 줄 때 서버와 DB가 필요 없다.
  화면은 기술 스택과 무관하게 항상 만들고, 백엔드는 뼈대가 있는 스택(Spring 계열)일 때만 만든다 (스펙과 다른 코드를 내지 않는다).
- 사람이 고친 파일은 덮어쓰지 않는다 (manifest의 해시와 다르면 그대로 두고 알린다).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from app.agents.roles.api_designer import tech_stack
from app.schemas.data_model import DataModel
from app.schemas.function import FunctionSpec
from app.schemas.requirement import Question
from app.agents.tools.api_shape import AUDIT_COLUMNS, NUMBER_COLUMN, USER_TABLE, main_table, table_of
from app.agents.tools.convention import apply_profile, ddl_model, profile_of
from app.agents.tools.ddl import mysql_reserved, q, render_script
from app.agents.tools.dev_spec import FILES as SPEC_FILES, Names, Spec, rule_tag
from app.agents.tools.sample_data import ROWS, load_words, mock_data, sample_rows, seed_sql
from app.agents.tools.screens import (SECRET_COLUMN, build_screens, label_column, op_kind, op_table, path_params,
                                      screens_md)
from app.agents.storage import get_store
from app.agents.tools.versions import find_version, project_info, project_name, version_label
from app.core.config import get_settings

log = logging.getLogger(__name__)
APP_FORMAT = "app-format v1"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "app"
MANIFEST = ".req-agent/manifest.json"
LOGIC_DIR = ".req-agent/logic"
PG_TYPE = {"bigint": "bigint", "integer": "integer", "decimal": "numeric", "varchar": "varchar", "text": "text",
           "boolean": "boolean", "date": "date", "timestamp": "timestamptz", "jsonb": "jsonb", "uuid": "uuid"}
SPRING = {"GET": "GetMapping", "POST": "PostMapping", "PUT": "PutMapping", "PATCH": "PatchMapping", "DELETE": "DeleteMapping"}
AUDIT_NOW = ("created_at", "updated_at")
JAVA_WORDS = {"abstract", "assert", "boolean", "break", "case", "catch", "class", "default", "delete", "do", "double", "else", "enum",
              "final", "for", "if", "import", "new", "package", "public", "return", "switch", "this", "throw", "try", "void", "while"}


class AppError(RuntimeError):
    """개발 단계를 시작할 수 없을 때 (사용자에게 그대로 보여 준다)."""


# ------------------------------------------------------------------ 스펙 읽기
def load_spec(project: str, version: str) -> Spec:
    """산출물 저장소(DB 또는 로컬 폴더)의 버전 산출물로 개발 스펙을 다시 읽는다. 개발 스펙이 있는 최종본('ERD 최종 확정')만 된다."""
    label = version_label(version)
    store = get_store()

    def read(name: str) -> str | None:
        return store.read(project, label, name)

    design_text = read("design.json")
    if design_text is None:
        raise AppError(f"버전이 없습니다: {project} v{label} ({store.location})")
    if read("data_model.json") is None or read("openapi.yaml") is None:
        raise AppError(f"v{label}에는 데이터 설계·개발 스펙이 없습니다. 'ERD 최종 확정'으로 만든 최종본(예: v1.1.1)을 지정하세요.")
    design = json.loads(design_text)
    model = DataModel.model_validate_json(read("data_model.json"))
    record = find_version(project, label) or {}
    title = re.search(r"^- 사업: (.+)$", read("AGENTS.md") or "", re.M)
    # 회사 이름: 따로 정하지 않았으면 업로드할 때 넣은 프로젝트 이름 (로그인 표지·메뉴에 보인다)
    options = {"standard": record.get("standard"), "title": title.group(1).strip() if title else "",
               "company": project_info(project).get("company") or project_name(project),
               "theme": project_info(project).get("theme", "")}   # 화면 스타일 (최종 개발하기에서 고름, tools/themes.py)
    profile = profile_of(options)
    physical, conv = model, None
    if profile and profile.renames:
        physical, conv = apply_profile(model, profile)
    questions = [Question.model_validate(q) for q in design.get("questions") or []]
    filled = fill_read_responses(design.get("api_design") or {}, model)
    unstored = relax_unstored_updates(design.get("api_design") or {})
    state = {"requirements": design.get("requirements"), "functions": design.get("functions"), "questions": design.get("questions"),
             "function_common": design.get("common"), "api_design": design.get("api_design"), "options": options}
    chosen = (record.get("tech_stack") or {}).get("basis") == "확정"   # 최종 산출물 화면에서 정한 스택
    spec = Spec(state, project, label, model, ddl_model(physical, profile), Names(conv, profile),
                tech_stack(design.get("api_design"), questions, chosen=chosen), profile)
    spec.meta = design.get("meta") or {}
    from app.agents.tools import stacks
    spec.parts = stacks.parts_of(spec.stack.get("backend", ""), (record.get("tech_stack") or {}).get("parts"))
    spec.read = read
    spec.words = store.read_project_file(project, "sample_words.json")
    spec.filled_reads = filled
    spec.unstored_inputs = unstored
    return spec


def relax_unstored_updates(api: dict) -> list[str]:
    """저장할 컬럼이 없는 입력(어디에도 저장하지 않는 '구분' 같은 값, 파일·비밀번호처럼 바꿔 저장하는 값은 제외)을 찾는다.

    수정 API(PUT·PATCH)에서는 그 값을 필수로 두지 않는다: 저장하지 않는 값이라 수정 화면이 처음 값을 불러올 수 없어,
    필수면 고칠 때마다 다시 입력해야 한다. 등록에서는 스펙대로 필수다. 스펙(데이터 모델) 확인용으로 'API 필드(설명)' 목록을 돌려준다."""
    found = []
    for entry in (api.get("functions") or {}).values():
        for op in entry.get("operations") or []:
            if op["method"] not in ("POST", "PUT", "PATCH"):   # 조회 조건은 저장하는 값이 아니다
                continue
            for f in op.get("request") or []:
                if f.get("column") or f["name"] in path_params(op["path"]) or TRANSFORMED.search(f["name"]):
                    continue
                found.append(f"{op['method']} {op['path']} {f['name']}" + (f"({f['description']})" if f.get("description") else ""))
                if op["method"] in ("PUT", "PATCH"):
                    f["required"] = False
    return found


JSON_TYPE = {"bigint": "integer", "integer": "integer", "decimal": "number", "boolean": "boolean"}
TRANSFORMED = re.compile(r"(?i)password|file|attachment|image|photo|token|code$")   # 바꿔 저장하거나 받기만 하는 값 (컬럼이 없는 것이 맞다)


def _collection(path: str) -> str:
    """모음 경로: /orders/{orderId} → /orders (같은 데이터를 다루는 API끼리 묶는다)."""
    return re.sub(r"(/\{[^}]+\})+$", "", path)


def _named_table(path: str, names: set[str]) -> str:
    """경로 끝 이름과 같은 테이블: /order-items → order_item (복수형 s·es·ies)."""
    last = next((p for p in reversed(path.split("/")) if p and not p.startswith("{")), "").replace("-", "_")
    for name in (last, re.sub(r"ies$", "y", last), re.sub(r"es$", "", last), re.sub(r"s$", "", last)):
        if name in names:
            return name
    return ""


def fill_read_responses(api: dict, model: DataModel) -> list[str]:
    """응답 칸이 하나도 없는 조회 API(목록·한 건·요약)에 그 테이블의 컬럼을 응답으로 채운다 (화면 표와 조회 SQL이 비지 않게).

    테이블은 필드·경로 ID·기능의 테이블로 찾고, 없으면 같은 모음 경로를 쓰는 다른 API의 테이블(GET /orders ← POST /orders),
    그다음 경로 이름과 같은 테이블이다. 찾지 못하면 그대로 둔다 (업무 로직이 채울 '구현 전'). 비밀 컬럼·감사 컬럼은 넣지 않는다.
    채운 API('GET /orders')를 돌려준다."""
    functions = api.get("functions") or {}
    entities = {e.entity_id: e for e in model.entities}
    by_path: dict[str, str] = {}
    for entry in functions.values():
        for op in entry.get("operations") or []:
            if main_table(op):
                by_path.setdefault(_collection(op["path"]), main_table(op))
    filled = []
    for fid, entry in functions.items():
        linked = [e.entity_id for e in model.entities if fid in e.function_ids]
        for op in entry.get("operations") or []:
            if op.get("response") or op.get("shared_with") or op_kind(op) not in ("list", "get", "report"):
                continue
            table = (table_of(op, model.entities, linked) or by_path.get(_collection(op["path"]))
                     or _named_table(op["path"], set(entities)))
            if table not in entities:
                continue
            op["response"] = [{"name": _camel(a.name), "column": f"{table}.{a.name}", "type": JSON_TYPE.get(a.data_type, "string"),
                               "required": not a.nullable, "description": a.label}
                              for a in entities[table].attributes if not SECRET_COLUMN.search(a.name) and a.name not in AUDIT_COLUMNS]
            filled.append(f"{op['method']} {op['path']}")
    return filled


def stack_parts(spec: Spec) -> dict[str, str]:
    """개발할 세 층 {frontend, backend, database}: 버전 기록의 tech_stack.parts(기능 명세에서 고름), 없으면 스택 문장에서 짐작"""
    from app.agents.tools import stacks
    return getattr(spec, "parts", None) or stacks.parts_of(spec.stack.get("backend", ""))


def has_backend(spec: Spec) -> bool:
    """이 스펙의 서버 스택에 개발 틀이 있는가. 없으면 화면·DB·스펙만 만든다 (틀 없는 스택은 AI가 뼈대부터: develop --scratch)."""
    from app.agents.tools import stacks
    return stack_parts(spec)["backend"] in stacks.TEMPLATED["backend"]


def db_dialect(spec: Spec) -> str:
    """앱이 쓸 DB 문법: mysql 또는 postgresql (틀이 없는 DB는 설계 기준인 PostgreSQL로 만든다)."""
    return "mysql" if stack_parts(spec)["database"] == "mysql" else "postgresql"


def _q(spec: Spec, name: str) -> str:
    """Java 코드의 SQL 이름. MySQL은 서버가 sql_mode ANSI라 같은 큰따옴표로 감싸고, MySQL 예약어(rank·key 등)도 감싼다."""
    if db_dialect(spec) == "mysql" and mysql_reserved(name):
        return f'"{name}"'
    return q(name)


# ------------------------------------------------------------------ 이름
def class_name(function_id: str) -> str:
    """FN-SFR-001-05 → FnSfr00105. 영문·숫자·하이픈 밖의 글자가 있으면 지문을 붙여 겹치지 않게 한다."""
    name = "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", function_id) if p)
    return name if re.fullmatch(r"[A-Za-z0-9-]+", function_id) else f"{name}X{hashlib.sha1(function_id.encode('utf-8')).hexdigest()[:6]}"


def method_name(operation_id: str) -> str:
    name = re.sub(r"\W", "", operation_id)
    return f"{name}Op" if name in JAVA_WORDS or not name or name[0].isdigit() else name


def _path_var(param: str) -> str:
    """경로 변수의 Java 이름 (지역 변수 input·user와 겹치지 않게)."""
    return "path" + param[:1].upper() + re.sub(r"\W", "", param[1:])


def _constant(operation_id: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", re.sub(r"\W", "", operation_id)).upper()


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def _text(value: str) -> str:
    """Java 문자열 리터럴."""
    return json.dumps(value, ensure_ascii=False)


def _doc(value: str) -> str:
    """주석에 넣을 한 줄."""
    return re.sub(r"\s+", " ", str(value).replace("*/", "* /").replace("\\", "/")).strip()


# ------------------------------------------------------------------ 백엔드 코드
def _pg(spec: Spec, table: str, column: str) -> str:
    """컬럼의 PostgreSQL 타입. 설계는 boolean인데 표준이 Y/N 글자로 바꾼 컬럼은 yn이다 (API는 참·거짓, DB는 Y/N)."""
    actual = spec.physical.get((table, column)) or spec.columns[(table, column)]
    return "yn" if spec.columns[(table, column)].data_type == "boolean" and actual.data_type != "boolean" else PG_TYPE[actual.data_type]


def _col(spec: Spec, table: str, field: str, column: str) -> str:
    return f'new Col({_text(field)}, {_text(_q(spec, spec.names.column(table, column)))}, {_text(_pg(spec, table, column))})'


def _cols(spec: Spec, table: str, fields: list[dict], *, writable: bool = False) -> list[tuple[str, str]]:
    """API 필드 중 이 테이블에 있는 컬럼 (필드, 설계 컬럼). 비밀 컬럼은 넣지 않는다: 값은 업무 로직이 해시·암호화한다.

    writable이면 저장할 컬럼만: 기본키와 시각 컬럼(created_at·updated_at, Db가 넣는다)은 뺀다."""
    out = []
    for f in fields:
        t, _, column = (f.get("column") or "").partition(".")
        attr = spec.columns.get((t, column))
        if t != table or attr is None or SECRET_COLUMN.search(column) or (writable and (attr.primary_key or column in AUDIT_NOW)):
            continue
        if column not in {c for _, c in out}:
            out.append((f["name"], column))
    return out


def _unique(cols: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """같은 컬럼을 두 번 쓰지 않는다 (요청 필드와 경로 변수가 같은 컬럼일 때)."""
    seen: dict[str, str] = {}
    for field, column in cols:
        seen.setdefault(column, field)
    return [(field, column) for column, field in seen.items()]


def _joined(spec: Spec, table: str, fields: list[dict]) -> tuple[list[str], list[str]]:
    """조회 응답의 Col 목록과 JOIN. 주 테이블이 외래키로 가리키는 부모 테이블의 값은 함께 읽는다.

    자식 테이블·계산 값처럼 한 행으로 읽을 수 없는 필드는 빼고, 업무 로직 자리가 채운다."""
    cols, joins, aliases, seen = [], [], {}, set()
    entity = next(e for e in spec.model.entities if e.entity_id == table)
    physical = _q(spec, spec.names.table(table))
    for f in fields:
        t, _, column = (f.get("column") or "").partition(".")
        attr = spec.columns.get((t, column))
        if attr is None or SECRET_COLUMN.search(column) or f["name"] in seen:
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
        cols.append(f'new Col({_text(f["name"])}, {_text(source + "." + _q(spec, spec.names.column(t, column)))}, {_text(_pg(spec, t, column))})')
    return cols, joins


def _java_list(items: list[str]) -> str:
    return "List.of(\n            " + ",\n            ".join(items) + ")" if items else "List.of()"


def _col_list(spec: Spec, table: str, cols: list[tuple[str, str]]) -> str:
    if not cols:
        return "List.of()"
    return "List.of(\n            " + ",\n            ".join(_col(spec, table, f, c) for f, c in cols) + ")"


def _parent_cols(spec: Spec, table: str, op: dict) -> list[tuple[str, str]]:
    """경로 변수 중 이 테이블의 컬럼을 가리키는 것 (/orders/{orderId}/items → order_item.order_id)."""
    out = []
    for p in path_params(op["path"]):
        attr = spec.columns.get((table, _snake(p)))
        if attr and not attr.primary_key:
            out.append((p, attr.name))
    return out


def _literal(spec: Spec, table: str, column: str, value: str) -> str:
    """고정값 → 컬럼 타입에 맞는 Java 값."""
    kind = _pg(spec, table, column)
    if kind == "boolean":
        return "true" if value.strip().lower() in ("true", "y", "예") else "false"
    if kind == "yn":
        return '"Y"' if value.strip().lower() in ("true", "y", "예") else '"N"'
    if kind in ("integer", "numeric") and re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
        return value.strip()
    if kind == "bigint" and re.fullmatch(r"-?\d+", value.strip()):
        return f"{value.strip()}L"
    return _text(value)


def _defaults(spec: Spec, table: str, given: set[str], op: dict | None = None) -> tuple[list[str], list[str], list[str]]:
    """요청에 없는 NOT NULL 컬럼: 기본값 (Java 인자), 현재 사용자로 채울 컬럼, 업무 로직이 채울 컬럼.

    스펙의 값 출처를 먼저 따르고, 출처가 없거나 미정·계산이면 컬럼 모양으로 정한다 (업무 로직이 값을 주면 그 값이 우선이다).
    모양으로도 못 정하는 값(참조 ID·글자)과 다른 테이블의 값은 업무 로직이 채운다."""
    entity = next(e for e in spec.model.entities if e.entity_id == table)
    sources = {s["target"]: s for s in (op or {}).get("sources") or []}
    pairs, users, missing = [], [], []
    fixed = {}   # 코드가 정한 값: 조건부 필수를 보려고 둔다
    for a in entity.attributes:
        if a.nullable or a.primary_key or a.name in given or a.name in AUDIT_NOW:
            continue
        name = _q(spec, spec.names.column(table, a.name))
        source = sources.get(f"{table}.{a.name}")
        initial = spec.initial_of(table, a.name)
        if initial:   # 상태 컬럼은 전환표의 초기 상태
            pairs += [_text(name), _text(initial)]
            fixed[a.name] = initial
        elif source and source["kind"] == "현재 사용자":
            users.append(a.name)
        elif source and source["kind"] == "현재 시각":
            pairs += [_text(name), "Db.NOW"]
        elif source and source["kind"] == "자동 번호" and a.data_type == "varchar":
            pairs += [_text(name), f"Db.autoNo({a.length or 20})"]
        elif source and source["kind"] == "고정값" and source["value"]:
            pairs += [_text(name), _literal(spec, table, a.name, source["value"])]
            fixed[a.name] = source["value"]
        elif source and source["kind"] not in ("미정", "계산"):
            missing.append(a.name)
        elif a.allowed_values:
            pairs += [_text(name), _text(a.allowed_values[0])]
        elif a.data_type == "boolean":
            pairs += [_text(name), '"N"' if _pg(spec, table, a.name) == "yn" else "false"]
        elif a.data_type in ("integer", "decimal") or (a.data_type == "bigint" and not a.references):
            pairs += [_text(name), "0"]
        elif a.data_type in ("date", "timestamp"):
            pairs += [_text(name), "Db.NOW"]
        elif a.references and USER_TABLE.search(a.references):
            users.append(a.name)
        elif a.data_type == "varchar" and NUMBER_COLUMN.search(a.name):
            pairs += [_text(name), f"Db.autoNo({a.length or 20})"]
        else:
            missing.append(a.name)
    # 정한 값 때문에 필수가 되는 컬럼 (매출이면 전송 상태 필수): 코드로 정할 수 없어 업무 로직이 채운다
    missing += [r.column for r in entity.required_when if fixed.get(r.when_column) in r.when_values and r.column not in given]
    return pairs, users, missing


def _table_const(table: str) -> str:
    return "T_" + table.upper()


def _table_decl(spec: Spec, table: str) -> str:
    entity = next(e for e in spec.model.entities if e.entity_id == table)
    pk = entity.pk()[0]
    names = {a.name for a in entity.attributes}
    created = [_q(spec, spec.names.column(table, c)) for c in AUDIT_NOW if c in names]
    updated = [_q(spec, spec.names.column(table, c)) for c in AUDIT_NOW[1:] if c in names]
    as_list = lambda xs: "List.of(" + ", ".join(_text(x) for x in xs) + ")"   # noqa: E731
    return (f"    private static final Table {_table_const(table)} = new Table({_text(_q(spec, spec.names.table(table)))}, "
            f"{_text(_q(spec, spec.names.column(table, pk.name)))}, {_text(_pg(spec, table, pk.name))}, {as_list(created)}, {as_list(updated)});")


def _roles_of(spec: Spec, op: dict) -> list[str]:
    from app.agents.roles.function_designer import SYSTEM_ACTOR, role_names

    names = role_names(spec.common)
    return list(dict.fromkeys(names.get(re.sub(r"\s+", "", a), a) for a in op.get("actors") or [] if not SYSTEM_ACTOR.match(a)))


def _system_caller(op: dict) -> bool:
    from app.agents.roles.function_designer import SYSTEM_ACTOR

    return any(SYSTEM_ACTOR.match(a) for a in op.get("actors") or [])


def _access(spec: Spec, op: dict) -> list[str]:
    """API를 부를 수 있는 주체 검사. 외부 시스템은 API 키로 확인한다. 주체가 없는 API도 로그인 없이 열지 않는다."""
    roles = ", ".join(_text(r) for r in _roles_of(spec, op))
    system = _system_caller(op)
    if roles and system:
        return ["        if (!CurrentUser.isSystem()) {", f"            user.require({roles});", "        }"]
    if roles:
        return [f"        user.require({roles});"]
    return ["        CurrentUser.requireSystem();"] if system else ["        user.requireLogin();"]


def _camel(column: str) -> str:
    return re.sub(r"_(\w)", lambda m: m.group(1).upper(), column)


def _stamps(spec: Spec, table: str, op: dict, sets: dict | None) -> tuple[list[tuple[str, str]], list[str]]:
    """상태를 바꿀 때 함께 남기는 값: 스펙의 값 출처 중 처리자(현재 사용자)·처리 시각(현재 시각)·고정값. (필드, 컬럼) 목록과 Db.values 인자."""
    cols, values = [], []
    for s in op.get("sources") or []:
        owner, _, column = s["target"].partition(".")
        if owner != table or (table, column) not in spec.columns or column in AUDIT_NOW or (sets and spec.names.column(table, column) == sets["column"]):
            continue
        value = {"현재 사용자": "user.id()", "현재 시각": "Db.NOW"}.get(s["kind"]) or (_literal(spec, table, column, s["value"]) if s["kind"] == "고정값" and s["value"] else "")
        if value:
            cols.append((_camel(column), column))
            values += [_text(_camel(column)), value]
    return cols, values


def _crud_call(spec: Spec, op: dict, table: str, constants: list[str], prefill: list[str]) -> str:
    """이 API의 기본 동작 (LLM 없이 도는 부분). 만들 수 없으면 '구현 전' 오류를 낸다. prefill은 호출 전에 input을 채우는 Java 문장이다."""
    kind, name, what = op_kind(op), _constant(op["operation_id"]), f'ApiError.notImplemented({_text(op["operation_id"])})'
    if not table:
        return f"{{ throw {what}; }}"
    base = _table_const(table)
    id_var = path_params(op["path"])[-1] if path_params(op["path"]) else ""
    out, joins = _joined(spec, table, op.get("response") or [])
    constants.append(f"    private static final List<Col> OUT_{name} = {_java_list(out)};")
    t = base
    if joins:
        t = f"V_{name}"
        constants.append(f"    private static final Table {t} = {base}.with({_text(' '.join(joins))});")
    decl = lambda n, cols: constants.append(f"    private static final List<Col> {n}_{name} = {_col_list(spec, table, cols)};")   # noqa: E731
    parents = _parent_cols(spec, table, op)
    keys = next(e.pk() for e in spec.model.entities if e.entity_id == table)
    pk = keys[0].name
    if len(keys) > 1 and kind not in ("list", "report", "create", "add"):   # 복합 기본키는 한 행을 ID 하나로 찾을 수 없다 (업무 로직이 처리)
        return f"{{ throw {what}; }}"
    # 경로의 마지막 ID가 이 테이블의 부모 키면(/users/{userId}/permissions → permission.user_id) 그 값으로 행 하나를 고칠 수 없다:
    # 고치기·지우기·상태 바꾸기는 업무 로직 몫이고, 한 건 조회는 그 부모 키로 찾는다
    by_parent = bool(id_var) and id_var in {p for p, _ in parents}
    if by_parent and kind not in ("list", "report", "create", "add", "get"):
        return f"{{ throw {what}; }}"
    if by_parent and kind == "get":
        items = [f'new Col({_text(f)}, {_text(_q(spec, spec.names.table(table)) + "." + _q(spec, spec.names.column(table, c)))}, {_text(_pg(spec, table, c))})'
                 for f, c in parents]
        constants.append(f"    private static final List<Col> FILTER_{name} = {_java_list(items)};")
        return f"db.first({t}, OUT_{name}, FILTER_{name}, input)"
    if kind == "report" and id_var and _snake(id_var) == pk:   # /orders/{orderId}/print: 그 행 하나
        return f"db.one({t}, OUT_{name}, input.get({_text(id_var)}))"
    if kind in ("list", "report"):
        physical = _q(spec, spec.names.table(table))
        filters = _unique([*_cols(spec, table, op.get("request") or [], writable=True), *parents])
        items = [f'new Col({_text(f)}, {_text(physical + "." + _q(spec, spec.names.column(table, c)))}, {_text(_pg(spec, table, c))})' for f, c in filters]
        constants.append(f"    private static final List<Col> FILTER_{name} = {_java_list(items)};")
        return f"db.{'page' if kind == 'list' else 'first'}({t}, OUT_{name}, FILTER_{name}, input)"
    if kind == "get":
        return f"db.one({t}, OUT_{name}, input.get({_text(id_var)}))"
    if kind == "delete":
        return f"db.delete({base}, input.get({_text(id_var)}))"
    if kind in ("create", "add"):
        cols = _unique([*_cols(spec, table, op.get("request") or [], writable=True), *parents])
        pairs, users, missing = _defaults(spec, table, {c for _, c in cols}, op)
        prefill += [f"input.putIfAbsent({_text(_camel(c))}, user.id());" for c in users]
        cols += [(_camel(c), c) for c in (*users, *missing)]   # 업무 로직이 input에 넣으면 저장된다
        decl("IN", cols)
        needed = "".join(f", {_text(_camel(c))}" for c in missing)
        return f"db.insert({t}, IN_{name}, OUT_{name}, input, Db.values({', '.join(pairs)})" + (f", {_text(op['operation_id'])}{needed}" if missing else "") + ")"
    if kind == "update":
        decl("IN", _cols(spec, table, op.get("request") or [], writable=True))
        return f"db.update({t}, IN_{name}, OUT_{name}, input.get({_text(id_var)}), input)"
    from app.agents.tools.screens import action_of

    sets = action_of(spec, op, table).get("sets")
    stamps, values = _stamps(spec, table, op, sets)
    if stamps:
        decl("STAMP", stamps)
    extra = f", STAMP_{name}, Db.values({', '.join(values)})" if stamps else ""
    move = spec.transition_of(op["operation_id"])
    target = next((f["name"] for f in op.get("request") or [] if move and f.get("column") == f"{table}.{move['column']}"), "")
    if move and target and id_var and len(move["moves"]) > 1:   # API 하나가 여러 상태로 보낸다: 요청 값이 목표이고 전환표가 허용한 경우만 바꾼다
        allowed = ", ".join(f"{_text(to)}, List.of({', '.join(_text(x) for x in froms)})" for to, froms in move["moves"].items())
        return (f"db.moveTo({t}, {_text(_q(spec, spec.names.column(table, move['column'])))}, Map.of({allowed}), input.get({_text(target)}), "
                f"input.get({_text(id_var)}), OUT_{name}{extra or ', List.of(), Map.of()'})")
    if sets and id_var and sets.get("from"):   # 전환표에 있는 처리: 허용된 상태에서만 바꾼다
        allowed = ", ".join(_text(v) for v in sets["from"])
        return f"db.move({t}, {_text(_q(spec, sets['column']))}, List.of({allowed}), {_text(sets['value'])}, input.get({_text(id_var)}), OUT_{name}{extra})"
    if sets and id_var:
        return f"db.set({t}, {_text(_q(spec, sets['column']))}, {_text(sets['value'])}, input.get({_text(id_var)}), OUT_{name}{extra})"
    written = _cols(spec, table, op.get("request") or [], writable=True)
    if op["method"] in ("PUT", "PATCH") and id_var and written:   # PATCH /employees/{id}/employment-status: 그 값만 바꾼다
        decl("IN", written)
        return f"db.update({t}, IN_{name}, OUT_{name}, input.get({_text(id_var)}), input{extra})"
    return f"{{ throw {what}; }}"


def _controller(spec: Spec, f: FunctionSpec, ops: list[dict]) -> str:
    """기능 하나의 컨트롤러. 경로·필드·필수값·허용값은 스펙에서 오고, 업무 규칙은 Logic 클래스의 자리로 넘긴다."""
    cls = class_name(f.function_id)
    constants: list[str] = []
    methods: list[str] = []
    tables: list[str] = []
    for op in ops:
        table = op_table(spec, op, f.function_id)
        table = table if table in {e.entity_id for e in spec.model.entities if e.pk()} else ""
        if table and table not in tables:
            tables.append(table)
        prefill: list[str] = []
        crud = _crud_call(spec, op, table, constants, prefill)
        reads = op["method"] in ("GET", "DELETE")
        params = [f'@PathVariable("{p}") String {_path_var(p)}' for p in path_params(op["path"])]
        params.append("@RequestParam Map<String, String> query" if reads else "@RequestBody(required = false) Map<String, Object> body")
        name = method_name(op["operation_id"])
        lines = [f"    /** {op['method']} {_doc(op['path'])} — {_doc(op.get('summary') or '')} */",
                 *(["    @Transactional"] if op["method"] != "GET" else []),   # 업무 로직·자동 처리까지 한 묶음: 예외가 나면 모두 되돌린다
                 f'    @{SPRING[op["method"]]}({_text(op["path"])})',
                 f"    public Object {name}({', '.join(params)}) {{",
                 "        CurrentUser user = CurrentUser.fromRequest();"]
        lines += _access(spec, op)
        lines.append(f"        Map<String, Object> input = Checks.input({'query' if reads else 'body'});")
        lines += [f'        input.put("{p}", {_path_var(p)});' for p in path_params(op["path"])]
        lines += [f"        {x}" for x in prefill]
        required = [x["name"] for x in op.get("request") or [] if x.get("required") and not reads]
        if required:
            lines.append(f"        Checks.required(input, {', '.join(_text(r) for r in required)});")
        for x in op.get("request") or []:
            t, _, c = (x.get("column") or "").partition(".")
            attr = spec.columns.get((t, c))
            if attr and attr.allowed_values:
                lines.append(f"        Checks.oneOf(input, {_text(x['name'])}, {', '.join(_text(v) for v in attr.allowed_values)});")
        lines += [f"        return logic.{name}(input, new Ctx(db, user), () -> {crud});", "    }"]
        methods.append("\n".join(lines))
    head = ["// 코드가 만든 파일 (스펙에서 생성). 고치지 말고 스펙을 고치세요. 업무 규칙은 logic 패키지에 있습니다.",
            "package app.api;", "",
            "import app.common.ApiError;", "import app.common.Checks;", "import app.common.Col;", "import app.common.CurrentUser;",
            "import app.common.Ctx;", "import app.common.Db;", "import app.common.Table;", f"import app.logic.{cls}Logic;",
            "import java.util.List;", "import java.util.Map;", "import org.springframework.transaction.annotation.Transactional;",
            "import org.springframework.web.bind.annotation.DeleteMapping;",
            "import org.springframework.web.bind.annotation.GetMapping;", "import org.springframework.web.bind.annotation.PatchMapping;",
            "import org.springframework.web.bind.annotation.PathVariable;", "import org.springframework.web.bind.annotation.PostMapping;",
            "import org.springframework.web.bind.annotation.PutMapping;", "import org.springframework.web.bind.annotation.RequestBody;",
            "import org.springframework.web.bind.annotation.RequestMapping;", "import org.springframework.web.bind.annotation.RequestParam;",
            "import org.springframework.web.bind.annotation.RestController;", "",
            f"/** {f.function_id} {_doc(f.name)} */", "@RestController", '@RequestMapping("/api/v1")', f"public class {cls}Controller {{",
            *[_table_decl(spec, t) for t in tables], *constants, "",
            "    private final Db db;", f"    private final {cls}Logic logic;", "",
            f"    public {cls}Controller(Db db, {cls}Logic logic) {{", "        this.db = db;", "        this.logic = logic;", "    }"]
    return "\n".join(head) + "\n\n" + "\n\n".join(methods) + "\n}\n"


def _lookup_controller(spec: Spec, data: dict) -> str:
    """화면의 선택 목록용 API: 다른 테이블을 ID 대신 이름으로 보여 준다. openapi.yaml의 업무 API가 아니라 화면 보조 API다 (/api/ui).

    화면이 참조하는 테이블만, 기본키와 이름 컬럼만 돌려준다 (비밀 컬럼 없음). 로그인한 사용자면 쓸 수 있다."""
    refs = sorted({f["ref"] for s in data["screens"] for f in _screen_fields(s) if f.get("ref")})
    by_name = {spec.names.table(e.entity_id): e for e in spec.model.entities}
    rows = []
    for ref in refs:
        entity = by_name.get(ref)
        if entity and entity.pk():
            pk = spec.names.column(entity.entity_id, entity.pk()[0].name)
            label = spec.names.column(entity.entity_id, label_column(entity))
            rows.append(f"        Map.entry({_text(ref)}, new Lookup({_text(_q(spec, ref))}, {_text(_q(spec, pk))}, {_text(_q(spec, label))}))")
    entries = ",\n".join(rows)
    tables, reads = _form_reads(spec, data)
    decls = "\n".join(_table_decl(spec, t) for t in tables)
    read_entries = ",\n".join(reads)
    text = "char" if db_dialect(spec) == "mysql" else "text"   # MySQL CAST에는 text 타입이 없다
    return f"""// 코드가 만든 파일 (스펙에서 생성). 화면 보조 API다: 선택 목록, 수정 폼의 처음 값.
package app.api;

import app.common.ApiError;
import app.common.Col;
import app.common.CurrentUser;
import app.common.Db;
import app.common.Table;
import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/ui")
public class LookupController {{
    private record Lookup(String table, String pk, String label) {{
    }}

    private record Read(Table table, List<Col> fields, List<String> roles) {{
    }}

{decls}
    private static final Map<String, Lookup> TABLES = Map.ofEntries(
{entries});
    private static final Map<String, Read> FORMS = Map.ofEntries(
{read_entries});
    private final Db db;

    public LookupController(Db db) {{
        this.db = db;
    }}

    @GetMapping("/lookups/{{table}}")
    public List<Map<String, Object>> lookup(@PathVariable("table") String table) {{
        CurrentUser.fromRequest().requireLogin();
        Lookup found = TABLES.get(table);
        if (found == null) {{
            throw ApiError.notFound();
        }}
        return db.query("SELECT CAST(" + found.pk() + " AS {text}) AS \\"value\\", CAST(" + found.label() + " AS {text}) AS \\"label\\" FROM " + found.table()
                + " ORDER BY " + found.pk() + " LIMIT 500", Map.of());
    }}

    /** 수정 폼의 처음 값: 그 화면이 고칠 수 있는 칸만, 수정 API와 같은 역할로 읽는다 */
    @GetMapping("/rows/{{screen}}/{{id}}")
    public Map<String, Object> row(@PathVariable("screen") String screen, @PathVariable("id") String id) {{
        CurrentUser user = CurrentUser.fromRequest();
        Read form = FORMS.get(screen);
        if (form == null || form.roles().isEmpty()) {{
            user.requireLogin();
        }} else {{
            user.require(form.roles().toArray(String[]::new));
        }}
        return form == null ? Map.of() : db.one(form.table(), form.fields(), id);
    }}
}}
"""


def _form_reads(spec: Spec, data: dict) -> tuple[list[str], list[str]]:
    """수정 화면마다 읽을 칸 (복합 기본키는 ID 하나로 찾을 수 없어 뺀다). 쓰는 테이블과 Java 항목을 돌려준다."""
    owned = {o["operation_id"]: o for _, o in spec.operations()}
    tables: list[str] = []
    reads: list[str] = []
    for s in data["screens"]:
        op = owned.get((s.get("update") or {}).get("operation_id"))
        table = op_table(spec, op, s["function_id"]) if op else ""
        entity = next((e for e in spec.model.entities if e.entity_id == table), None)
        cols = _cols(spec, table, op.get("request") or [], writable=True) if entity and len(entity.pk()) == 1 else []
        if cols:
            tables.append(table)
            roles = ", ".join(_text(r) for r in _roles_of(spec, op))
            reads.append(f"        Map.entry({_text(s['screen_id'])}, new Read({_table_const(table)}, {_col_list(spec, table, cols)}, List.of({roles})))")
    return list(dict.fromkeys(tables)), reads


def _screen_fields(screen: dict) -> list[dict]:
    """화면의 모든 칸."""
    out: list[dict] = []
    for view in (screen.get("list"), screen.get("report")):
        if view:
            out += [*view["filters"], *view["columns"]]
    for form in (screen.get("create"), screen.get("update")):
        if form:
            out += form["fields"]
    for action in screen.get("actions") or []:
        out += action["fields"]
    return out


NULL_UNSAFE_MAP = re.compile(r"\b(?:java\.util\.)?Map\.of\(")


def hole(name: str, body: str) -> str:
    """LLM이 채우는 자리. 표시를 남겨 어디가 업무 로직인지 보이게 한다.
    Map.of(키, 값 …)는 값이 null이면(비워 둔 조회 조건 등) 바로 오류이므로, 같은 모양으로 null을 받는 Db.values(…)로 바꿔 쓴다."""
    body = NULL_UNSAFE_MAP.sub("app.common.Db.values(", body)
    inner = "\n".join("        " + line if line.strip() else "" for line in body.strip().splitlines())
    return f"        // <hole {name}>\n{inner}\n        // </hole>"


def _logic(spec: Spec, f: FunctionSpec, ops: list[dict], task: str, hooks: dict[str, str]) -> str:
    """기능 하나의 업무 로직 자리. 규칙은 주석으로 적고, 고객 답을 기다리는 규칙은 TODO로 남긴다."""
    cls = class_name(f.function_id)
    rows = spec.acceptance[f.function_id]
    notes = []
    for rule in f.business_rules:
        kind, text, qids = rule_tag(rule)
        ac = next((r["id"] for r in rows if r["then"] == text and r["kind"].startswith("규칙")), "")
        notes.append(f" * {'TODO(' + (', '.join(qids) or '확인 필요') + ') ' if kind == '보류' else ''}[{kind}] {ac} {_doc(text)}".rstrip())
    notes += [f" * [자동 처리] {a['id']} {_doc(a['name'])}: {_doc(a['when'])} → {_doc(a['action'])}" for a in spec.automations_of(f.function_id)]
    lines = ["// 업무 로직 자리. // <hole> 안만 채운다 (develop --logic이 채우거나 사람이 고친다. 사람이 고친 파일은 덮어쓰지 않는다).",
             "package app.logic;", ""]
    imports = ["app.common.Ctx", "org.springframework.stereotype.Component"]
    if ops:
        imports += ["java.util.Map", "java.util.function.Supplier"]
    lines += [f"import {i};" for i in sorted(imports)] + ["", "/**", f" * {f.function_id} {_doc(f.name)} ({task or '작업 없음'})", " *", *notes, " */",
                                                         "@Component", f"public class {cls}Logic {{"]
    bodies = []
    for op in ops:
        name = method_name(op["operation_id"])
        bodies.append(f"    /** {op['method']} {_doc(op['path'])} — {_doc(op.get('summary') or '')} */\n"
                      f"    public Object {name}(Map<String, Object> input, Ctx ctx, Supplier<Object> crud) {{\n"
                      f"{hole(name, hooks.get(name) or 'return crud.get();')}\n    }}")
    if not ops:
        bodies.append(f"    /** 자동 실행 (API 없음). 실행 시점은 스펙의 이유를 따른다: {_doc((spec.api.get(f.function_id) or {}).get('no_api_reason', ''))} */\n"
                      f"    public void run(Ctx ctx) {{\n{hole('run', hooks.get('run') or '// 구현 전')}\n    }}")
    return "\n".join(lines) + "\n" + "\n\n".join(bodies) + "\n}\n"


def _acceptance_test(spec: Spec, f: FunctionSpec) -> str:
    """수용 기준 하나 = 테스트 하나 (이름에 AC ID). 내용을 채우기 전에는 건너뛴다."""
    cls = class_name(f.function_id)
    tests = []
    for n, row in enumerate(spec.acceptance[f.function_id], start=1):
        tests.append(f'    @Test\n    @Disabled("구현 전")\n    @DisplayName({_text(_doc(row["id"] + " " + row["kind"] + ": " + row["then"])[:180])})\n'
                     f"    void ac{n}() {{\n    }}")
    head = ["// 코드가 만든 파일. 수용 기준(ACCEPTANCE_CRITERIA.md) 하나가 테스트 하나다.", "package app.acceptance;", "",
            "import org.junit.jupiter.api.Disabled;", "import org.junit.jupiter.api.DisplayName;", "import org.junit.jupiter.api.Test;", "",
            f"/** {f.function_id} {_doc(f.name)} */", f"class {cls}AcceptanceTest {{"]
    return "\n".join(head) + "\n" + "\n\n".join(tests) + "\n}\n"


# ------------------------------------------------------------------ 프로젝트
def owned_operations(spec: Spec) -> tuple[dict[str, list[dict]], list[str]]:
    """기능 → 만들 API. 경로 변수 이름만 다른 같은 경로는 Spring이 띄우지 못하므로 먼저 나온 것만 둔다."""
    out: dict[str, list[dict]] = {f.function_id: [] for f in spec.functions}
    seen: set[tuple[str, str]] = set()
    skipped: list[str] = []
    for f, op in spec.operations():
        key = (op["method"], re.sub(r"\{\w+\}", "{}", op["path"]))
        if key in seen:
            skipped.append(f"{op['method']} {op['path']} ({f.function_id})")
            continue
        seen.add(key)
        out[f.function_id].append(op)
    return out, skipped


ALL_ROLES = "전체 보기"   # 모든 화면을 보는 시연 역할 (CurrentUser.java, App.tsx와 같은 이름)


def demo_accounts(data: dict) -> list[dict]:
    """임시 계정 두 개: admin(모든 역할의 권한 = '전체 보기') + user(스펙의 첫 일반 역할, 이름에 '관리자'가 없는 첫 역할).
    userId는 예시 데이터 행 ID(1…ROWS)에 맞춰 결재자·신청자 같은 업무 데이터와 이어지게 한다."""
    out = [{"loginId": "admin", "role": ALL_ROLES, "userId": "1", "name": "관리자 (전체 권한)"}]
    roles = data["roles"]
    general = next((r for r in roles if "관리자" not in r["name"]), roles[0] if roles else None)
    if general:
        out.append({"loginId": "user", "role": general["name"], "userId": str(min(2, ROWS)), "name": general["name"]})
    return out


def _brand(spec: Spec) -> str:
    """화면에 보일 이름: 회사 이름, 없으면 사업 이름."""
    return spec.options.get("company") or spec.options.get("title") or spec.project


def _first_tasks(data: dict, role: str, limit: int = 3) -> str:
    """역할이 처음 해 볼 업무 (그 역할이 쓰는 메뉴 앞쪽)."""
    usable = {s["screen_id"] for s in data["screens"] if role == ALL_ROLES or not s["roles"] or role in s["roles"]}
    names = [p["name"] for m in data["menu"] for p in m["pages"] if usable & set(p["screens"])]
    return ", ".join(list(dict.fromkeys(names))[:limit]) or "-"


def _readme(spec: Spec, data: dict, skipped: list[str]) -> str:
    backend = has_backend(spec)
    mysql = db_dialect(spec) == "mysql"
    python = backend and stack_parts(spec)["backend"] == "fastapi"
    nest = backend and stack_parts(spec)["backend"] == "nestjs"
    lines = [f"# {_brand(spec)} 업무 포털", "",
             f"{spec.options.get('title') or spec.project} 개발 스펙 v{spec.version}에서 코드로 만든 서비스다 ({APP_FORMAT}). "
             f"업무 {sum(len(m['pages']) for m in data['menu'])}개 · 화면 {len(data['screens'])}개 · 자동 실행 {len(data['autos'])}개.",
             "구현 범위와 고객 답을 기다리는 규칙은 [IMPLEMENTATION.md](IMPLEMENTATION.md), 화면 목록은 [spec/SCREENS.md](spec/SCREENS.md), "
             "스펙 문서는 [spec/](spec/)에 있다.", ""]
    if backend:
        lines += ["## 바로 실행 (Docker만 있으면 된다)", "", "```bash", "docker compose up --build", "```", "",
                  "처음에는 이미지와 라이브러리를 내려받느라 몇 분 걸린다. 다 뜨면 브라우저에서 **http://localhost:8080** 을 연다 "
                  "(포트가 겹치면 `APP_PORT=8081 docker compose up --build`처럼 바꾼다. DB 포트는 `DB_PORT`).",
                  ("DB(MySQL 8)에는 스펙의 데이터 모델을 MySQL 문법으로 옮긴 db/schema.sql과 예시 데이터(seed.sql)가 들어간다. "
                   if mysql else "DB에는 스펙의 schema.sql과 예시 데이터(seed.sql)가 들어간다. ") + "저장한 데이터는 다시 시작해도 남는다 "
                  "(`docker compose down -v`로 처음 상태로 돌린다).", "",
                  "### 계정", "",
                  "처음 실행할 때 역할마다 시연 계정을 만들고 비밀번호를 **`local/accounts.txt`** 에 적는다 (로그에는 남기지 않는다). "
                  "모두 같은 비밀번호로 하려면 실행 전에 환경 변수 `DEMO_PASSWORD`를 정한다. "
                  "비밀번호를 새로 만들려면 `RESET_PASSWORDS=true`로 한 번 실행한다.", "",
                  "| 아이디 | 역할 | 처음 해 볼 업무 |", "|---|---|---|",
                  *[f"| {a['loginId']} | {a['name']} | {_first_tasks(data, a['role'])} |" for a in data["accounts"]], ""]
    lines += ["## 화면만 보기 (서버·DB 없이 예시 데이터)", "", "```bash", "cd frontend", "npm install", "npm run dev", "```", "",
              "로그인 대신 역할을 골라 들어간다. 새로고침하면 처음 데이터로 돌아간다. 오른쪽 위 '검토 모드'를 켜면 화면마다 업무 규칙과 "
              "고객 확인이 필요한 항목이 보인다. LLM이 구현한 기능 화면은 서버가 있어야 동작한다.", ""]
    if python:
        from app.agents.tools.scaffold_fastapi import dev_steps

        lines += ["## 고치면서 띄우기 (Python 3.12 · Node 20)", "", "```bash", *dev_steps(mysql), "```", ""]
    elif nest:
        from app.agents.tools import scaffold_nestjs

        lines += ["## 고치면서 띄우기 (Node 20 · NestJS)", "", "```bash", *scaffold_nestjs.dev_steps(mysql), "```", ""]
    elif backend:
        lines += ["## 고치면서 띄우기 (Java 17 · Maven · Node 20)", "", "```bash",
                  "docker compose up -d db                 # MySQL만 (포트 3307)" if mysql else "docker compose up -d db                 # PostgreSQL만 (포트 5433)",
                  "cd backend && mvn spring-boot:run       # 서버 8080 (계정 파일은 backend/local/accounts.txt)",
                  "cd frontend && VITE_API=server npm run dev", "```", ""]
    else:
        lines += ["## 백엔드", "", f"기술 스택 '{spec.stack.get('backend') or '미정'}'의 뼈대는 아직 없어 화면·DB·스펙만 만들었다.", ""]
    lines += ["## 어디를 고치나", "",
              "| 위치 | 내용 | 고쳐도 되나 |", "|---|---|---|"]
    if python:
        lines += ["| `backend/app/feature/` · `frontend/src/pages/` | LLM이 구현한 기능 (develop --build) | 예. 고친 파일은 다시 생성해도 그대로 둔다 |",
                  "| `backend/app/logic/` | 기능별 업무 로직 (`# <hole>` 안) | 예. 고친 파일은 다시 생성해도 그대로 둔다 |",
                  "| `backend/app/api/` | 스펙에서 만든 API | 아니오. 스펙을 고치고 다시 생성한다 |",
                  "| `frontend/src/generated/` | 스펙에서 만든 화면 설계·예시 데이터 | 아니오. 스펙을 고치고 다시 생성한다 |",
                  "| `backend/tests/acceptance/` | 수용 기준 테스트 틀 (`pytest.mark.skip`) | 예. 채우면 그대로 둔다 |", ""]
    elif nest:
        lines += ["| `backend/src/feature/` · `frontend/src/pages/` | LLM이 구현한 기능 (develop --build) | 예. 고친 파일은 다시 생성해도 그대로 둔다 |",
                  "| `backend/src/logic/` | 기능별 업무 로직 (`// <hole>` 안) | 예. 고친 파일은 다시 생성해도 그대로 둔다 |",
                  "| `backend/src/api/` | 스펙에서 만든 API | 아니오. 스펙을 고치고 다시 생성한다 |",
                  "| `frontend/src/generated/` | 스펙에서 만든 화면 설계·예시 데이터 | 아니오. 스펙을 고치고 다시 생성한다 |",
                  "| `backend/test/acceptance/` | 수용 기준 테스트 틀 (`skip`, `npm test`) | 예. 채우면 그대로 둔다 |", ""]
    else:
        lines += [
              "| `backend/.../feature/` · `frontend/src/pages/` | LLM이 구현한 기능 (develop --build) | 예. 고친 파일은 다시 생성해도 그대로 둔다 |",
              "| `backend/.../logic/` | 기능별 업무 로직 (`// <hole>` 안) | 예. 고친 파일은 다시 생성해도 그대로 둔다 |",
              "| `backend/.../api/` | 스펙에서 만든 API | 아니오. 스펙을 고치고 다시 생성한다 |",
              "| `frontend/src/generated/` | 스펙에서 만든 화면 설계·예시 데이터 | 아니오. 스펙을 고치고 다시 생성한다 |",
              "| `backend/src/test/.../acceptance/` | 수용 기준 테스트 틀 (`@Disabled`) | 예. 채우면 그대로 둔다 |", ""]
    lines += ["## 다시 만들기", "",
              "같은 스펙으로 `develop`을 다시 실행하면 LLM을 부르지 않고 같은 파일이 나온다 (LLM 결과는 `.req-agent/`에 저장). "
              "스펙이 바뀐 기능만 다시 만든다. 이 폴더를 git에 커밋해 두면 확정본으로 고정된다.", ""]
    if any(_system_caller(op) for _, op in spec.operations()):
        lines += ["## 외부 시스템 호출", "", "외부 시스템이 호출하는 API는 `X-Api-Key` 헤더로 확인한다. "
                  "서버 환경 변수 `SYSTEM_API_KEY`에 키를 정한다 (정하지 않으면 모두 막힌다).", ""]
    if skipped:
        lines += ["## 경로가 겹쳐 만들지 않은 API", "", *[f"- {s}" for s in skipped], ""]
    return "\n".join(lines)


def _todo_text(todo) -> str:
    """LLM이 남긴 TODO 한 줄."""
    if isinstance(todo, dict):   # builder.TodoOut {rule, reason}
        head = str(todo.get("rule") or "").strip()
        body = str(todo.get("reason") or "").strip() or json.dumps(todo, ensure_ascii=False)
        return f"{head} — {body}" if head else body
    return str(todo)


def _implementation(spec: Spec, data: dict, owned: dict[str, list[dict]], built: dict[str, dict], logic: dict[str, dict],
                    records: dict[str, dict], skipped: list[str]) -> str:
    """구현 범위: 기능마다 누가 만들었나(LLM 구현 / 코드 틀), API 수, 고객 답을 기다리는 규칙. 코드가 만든 파일."""
    backend = has_backend(spec)
    autos = {a["function_id"] for a in data["autos"]}

    def how(fid: str) -> str:
        if not backend:
            return "화면만 (백엔드 뼈대 없음)"
        if fid in built:
            return "LLM 구현"
        if records.get(fid, {}).get("status") == "실패":
            return "코드 틀 (LLM 구현 실패)"
        if not owned.get(fid):
            return "자동 실행" if fid in autos else "API 없음"
        return "코드 틀 + 업무 로직" if logic.get(fid) else "코드 틀"

    held: list[tuple[str, str, list[str]]] = []
    rows, kinds = [], {}
    for f in spec.functions:
        kind = how(f.function_id)
        kinds[kind] = kinds.get(kind, 0) + 1
        waits = [(text, qids) for tag, text, qids in (rule_tag(r) for r in f.business_rules) if tag == "보류"]
        held += [(f.function_id, text, qids) for text, qids in waits]
        todos = len((built.get(f.function_id) or {}).get("todos") or [])
        rows.append(f"| {f.function_id} | {f.name} | {kind} | {len(owned.get(f.function_id) or [])} | "
                    f"{', '.join(sorted({q for _, qs in waits for q in qs})) or '-'} | {todos or '-'} |")
    total_ops = sum(len(v) for v in owned.values())
    built_ops = sum(len(owned.get(fid) or []) for fid in built)
    lines = ["# 구현 범위", "",
             f"{_brand(spec)} · {spec.options.get('title') or spec.project} 개발 스펙 v{spec.version}. "
             f"기능 {len(spec.functions)}개 · API {total_ops}개 (LLM 구현 {built_ops}개). 코드가 만든 파일이다 (develop을 다시 실행하면 갱신).", "",
             "| 구현 | 기능 수 |", "|---|---|", *[f"| {k} | {v} |" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])], "",
             "- **LLM 구현**: 개발 스펙(AGENTS·TASKS·API·BEHAVIOR·수용 기준·schema.sql)을 기능별로 잘라 LLM이 서버 API와 화면을 썼다 "
             "(`develop --build`). 계약 검사와 컴파일을 통과한 것만 쓴다.",
             "- **코드 틀**: 등록·조회·수정·삭제와 상태 바꾸기는 코드가 만든 기본 동작이다. 업무 규칙은 `logic/`의 자리에 들어간다.",
             "- **고객 답 대기**: '확인 필요' 규칙은 구현하지 않고 `TODO(질문 ID)`로 둔다 (AGENTS.md 3절).", "",
             "## 기능별", "", "| 기능 | 이름 | 구현 | API | 고객 답 대기 | LLM이 남긴 TODO |", "|---|---|---|---|---|---|", *rows, ""]
    if held:
        lines += ["## 고객 답을 기다리는 규칙", "", "| 기능 | 질문 | 규칙 |", "|---|---|---|",
                  *[f"| {fid} | {', '.join(qids) or '-'} | {text.replace('|', '/')} |" for fid, text, qids in held], ""]
    notes = [(fid, t) for fid, r in sorted(built.items()) for t in r.get("todos") or []]
    if notes:
        lines += ["## LLM이 남긴 TODO", "", "구현하며 스펙에서 정하지 않았다고 본 것이다. 고객 확인이나 스펙 보완이 필요하다.", "",
                  *[f"- {fid}: {_todo_text(t)}" for fid, t in notes], ""]
    if skipped:
        lines += ["## 경로가 겹쳐 만들지 않은 API", "", *[f"- {s}" for s in skipped], ""]
    return "\n".join(lines)


DOCKERFILE = """# 한 번에 빌드: 화면(Node) → 서버(Maven, 화면을 함께 담음) → 실행 이미지(JRE). docker compose up --build가 쓴다.
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
ENV VITE_API=server
RUN npm run build

FROM maven:3.9-eclipse-temurin-17 AS server
WORKDIR /src
COPY backend/pom.xml ./
RUN mvn -q -B dependency:go-offline
COPY backend/src ./src
COPY --from=web /web/dist ./src/main/resources/static
RUN mvn -q -B -DskipTests package

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=server /src/target/*.jar app.jar
ENV LOCAL_DIR=/app/local
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
"""

DOCKERIGNORE = "**/node_modules\n**/target\n**/dist\n**/dist-check\nlocal\n.req-agent\n.preview\n.git\n"

COMPOSE = """# 시연용 서비스: PostgreSQL(schema.sql + seed.sql) + 서버(화면 포함). 운영 설정이 아니다.
# docker compose up --build → http://localhost:8080 (계정: local/accounts.txt). 포트가 겹치면 APP_PORT·DB_PORT로 바꾼다.
# 다른 PC에서 열려면 APP_HOST=0.0.0.0 (이 PC의 모든 네트워크에 연다).
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    ports:
      - "127.0.0.1:${DB_PORT:-5433}:5432"
    volumes:
      - ./db/schema.sql:/docker-entrypoint-initdb.d/01_schema.sql:ro
      - ./db/seed.sql:/docker-entrypoint-initdb.d/02_seed.sql:ro
      - db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U app -d app"]
      interval: 3s
      timeout: 3s
      retries: 40
  app:
    build: .
    depends_on:
      db:
        condition: service_healthy
    environment:
      DB_URL: jdbc:postgresql://db:5432/app
      DB_USER: app
      DB_PASSWORD: app
      DEMO_PASSWORD: ${DEMO_PASSWORD:-}
      RESET_PASSWORDS: ${RESET_PASSWORDS:-false}
      SYSTEM_API_KEY: ${SYSTEM_API_KEY:-}
    ports:
      - "${APP_HOST:-127.0.0.1}:${APP_PORT:-8080}:8080"
    volumes:
      - ./local:/app/local
volumes:
  db-data:
"""

# MySQL 8 (DB 스택 mysql). 서버는 연결마다 sql_mode ANSI를 켠다 (application.yml): 이름은 큰따옴표, ||는 문자열 잇기라
# PostgreSQL과 같은 SQL 모양을 쓴다. schema.sql·seed.sql은 MySQL 문법(백틱)이라 이 모드와 무관하다.
MYSQL_URL = "jdbc:mysql://{host}/app?characterEncoding=UTF-8&useSSL=false&allowPublicKeyRetrieval=true"
COMPOSE_MYSQL = """# 시연용 서비스: MySQL 8(schema.sql + seed.sql) + 서버(화면 포함). 운영 설정이 아니다.
# docker compose up --build → http://localhost:8080 (계정: local/accounts.txt). 포트가 겹치면 APP_PORT·DB_PORT로 바꾼다.
# 다른 PC에서 열려면 APP_HOST=0.0.0.0 (이 PC의 모든 네트워크에 연다).
services:
  db:
    image: mysql:8.4
    command: ["--character-set-server=utf8mb4", "--collation-server=utf8mb4_0900_ai_ci"]
    environment:
      MYSQL_DATABASE: app
      MYSQL_USER: app
      MYSQL_PASSWORD: app
      MYSQL_ROOT_PASSWORD: app-root
    ports:
      - "127.0.0.1:${DB_PORT:-3307}:3306"
    volumes:
      - ./db/schema.sql:/docker-entrypoint-initdb.d/01_schema.sql:ro
      - ./db/seed.sql:/docker-entrypoint-initdb.d/02_seed.sql:ro
      - db-data:/var/lib/mysql
    healthcheck:
      # 초기화 중에는 TCP를 열지 않으므로, 127.0.0.1로 응답하면 schema·seed까지 끝난 것이다
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uapp -papp --silent"]
      interval: 3s
      timeout: 3s
      retries: 60
  app:
    build: .
    depends_on:
      db:
        condition: service_healthy
    environment:
      DB_URL: "%s"
      DB_USER: app
      DB_PASSWORD: app
      DEMO_PASSWORD: ${DEMO_PASSWORD:-}
      RESET_PASSWORDS: ${RESET_PASSWORDS:-false}
      SYSTEM_API_KEY: ${SYSTEM_API_KEY:-}
    ports:
      - "${APP_HOST:-127.0.0.1}:${APP_PORT:-8080}:8080"
    volumes:
      - ./local:/app/local
volumes:
  db-data:
""" % MYSQL_URL.format(host="db:3306")

# PostgreSQL 틀 → MySQL 틀: 바꿀 곳만 (틀 하나를 두 DB가 함께 쓴다. 틀이 바뀌어 바꿀 곳을 못 찾으면 AppError로 멈춘다)
MYSQL_SWAPS: dict[str, list[tuple[str, str]]] = {
    "backend/pom.xml": [(
        "      <groupId>org.postgresql</groupId>\n      <artifactId>postgresql</artifactId>",
        "      <groupId>com.mysql</groupId>\n      <artifactId>mysql-connector-j</artifactId>")],
    "backend/src/main/resources/application.yml": [
        ("jdbc:postgresql://localhost:5433/app", MYSQL_URL.format(host="localhost:3307")),
        ("""      data-source-properties:
        stringtype: unspecified   # 글자로 온 값(경로의 ID·날짜)을 DB가 컬럼 타입에 맞춘다 (업무 로직의 직접 쿼리가 bigint = varchar로 실패하지 않게)""",
         """      # ANSI: 이름은 큰따옴표, ||는 문자열 잇기 (PostgreSQL과 같은 SQL 모양). STRICT: 맞지 않는 값은 잘라 넣지 않고 오류
      connection-init-sql: "SET SESSION sql_mode = 'ANSI,STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO'\"""")],
    "backend/src/main/java/app/common/Db.java": [
        ("import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;",
         "import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;\n"
         "import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;\n"
         "import org.springframework.jdbc.support.GeneratedKeyHolder;"),
        (' * 업무 로직에서 직접 쿼리가 필요하면 query·execute를 쓴다.\n */',
         ' * 업무 로직에서 직접 쿼리가 필요하면 query·execute를 쓴다.\n'
         ' * MySQL 8용: 연결마다 sql_mode ANSI(큰따옴표 이름, || 잇기)를 켜고(application.yml), 값은 MySQL이 컬럼 타입에 맞춘다.\n */'),
        ('"CAST(" + c.column() + " AS text)"', '"CAST(" + c.column() + " AS char)"'),
        ('''    private static String bind(String name, String type) {
        return "CAST(:" + name + " AS " + ("yn".equals(type) ? "varchar" : type) + ")";
    }''', '''    /** MySQL은 값을 컬럼 타입에 맞춰 넣고 비교한다 (CAST 타입 이름이 PostgreSQL과 달라 감싸지 않는다). JSON만 JSON으로 읽는다. */
    private static String bind(String name, String type) {
        return "jsonb".equals(type) ? "CAST(:" + name + " AS JSON)" : ":" + name;
    }'''),
        ('''        if ("jsonb".equals(c.type()) && cleaned instanceof String text) {''',
         '''        if ("boolean".equals(c.type()) && cleaned instanceof String text) {   // 조회 조건 ?done=true: MySQL은 'true'를 0으로 본다
            return Boolean.parseBoolean(text) || "1".equals(text) || "Y".equals(text);
        }
        if ("jsonb".equals(c.type()) && cleaned instanceof String text) {'''),
        ('''marks.add("left(to_char(now(), 'YYMMDD') || '-' || substr(md5(random()::text), 1, 6), " + no.length() + ")");''',
         '''marks.add("left(concat(date_format(now(), '%y%m%d'), '-', substr(md5(rand()), 1, 6)), " + no.length() + ")");'''),
        ('''        Object id = jdbc.queryForObject("INSERT INTO " + t.name() + " (" + String.join(", ", columns) + ") VALUES ("
                + String.join(", ", marks) + ") RETURNING " + t.pk(), params, Object.class);
        return one(t, out, id);''', '''        // MySQL에는 RETURNING이 없다: 자동 번호 키는 JDBC로 받고, 키를 요청 값으로 정한 테이블은 그 값으로 다시 읽는다
        GeneratedKeyHolder keys = new GeneratedKeyHolder();
        jdbc.update("INSERT INTO " + t.name() + " (" + String.join(", ", columns) + ") VALUES (" + String.join(", ", marks) + ")",
                new MapSqlParameterSource(params), keys);
        Object id = keys.getKeyList().isEmpty() ? null : keys.getKeyList().get(0).values().iterator().next();
        for (Col c : in) {
            if (id == null && c.column().equals(t.pk())) {
                id = params.get(c.field());
            }
        }
        if (id == null) {
            id = defaults.get(t.pk());
        }
        return one(t, out, id);'''),
    ],
    "backend/src/main/java/app/auth/Accounts.java": [
        (" * - 계정은 업무 테이블과 섞이지 않게 app_auth 스키마에 둔다.",
         " * - 계정은 업무 테이블과 섞이지 않게 app_auth_account 테이블에 둔다 (MySQL은 스키마가 곧 DB라 앱 DB 안에 둔다)."),
        ('        db.execute("CREATE SCHEMA IF NOT EXISTS app_auth");\n', ""),
        ("timestamptz NOT NULL DEFAULT now()", "datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)"),
        ("app_auth.account", "app_auth_account"),
    ],
    "backend/src/main/java/app/common/ApiErrorHandler.java": [(
        '''    @ExceptionHandler(HttpMessageNotReadableException.class)''',
        '''    /** MySQL: 값 조건(CHECK 3819)·맞지 않는 값(1366)은 Spring이 분류하지 않는다. 다른 DB 오류는 500 그대로다. */
    @ExceptionHandler(org.springframework.jdbc.UncategorizedSQLException.class)
    public ResponseEntity<Map<String, String>> mysqlValue(org.springframework.jdbc.UncategorizedSQLException e) throws Exception {
        int code = e.getSQLException() == null ? 0 : e.getSQLException().getErrorCode();
        if (code == 3819 || code == 1366) {
            return integrity(null);
        }
        return unexpected(e);
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)''')],
}


def _for_mysql(rel: str, text: str) -> str:
    """PostgreSQL 틀 파일을 MySQL용으로 바꾼다. 바꿀 곳(첫 값)은 파일에 꼭 있어야 한다 (모두 바꾼다)."""
    for old, new in MYSQL_SWAPS.get(rel, []):
        if old not in text:
            raise AppError(f"MySQL 틀을 만들 수 없습니다: {rel}에 바꿀 곳이 없습니다 ({old.strip()[:60]})")
        text = text.replace(old, new)
    return text


def render_app(spec: Spec, menu_names: dict[str, str] | None = None, logic: dict[str, dict] | None = None,
               built: dict[str, dict] | None = None) -> tuple[dict[str, str], dict]:
    """프로젝트 파일 전체 {상대 경로: 내용}과 화면 설계. logic은 기능별 채운 자리 {기능 ID: {메서드: 본문}}.
    built는 기능 구현 Agent(develop --build)가 기능 전체를 쓴 결과 {기능 ID: {controller, page}}: 그 기능은 코드가 만든 컨트롤러·업무 로직 대신
    그 코드를 쓰고, 화면도 공통 틀 대신 그 화면을 쓴다."""
    from app.agents.tools import scaffold_fastapi as fa
    from app.agents.tools import scaffold_nestjs as ne

    logic = logic or {}
    records = built or {}
    built = {fid: r for fid, r in records.items() if r.get("status") == "채움" and r.get("controller") and r.get("page")} if has_backend(spec) else {}
    backend = has_backend(spec)
    python = stack_parts(spec)["backend"] == "fastapi"   # FastAPI 틀: backend/는 scaffold_fastapi가 만든다 (Spring 틀은 쓰지 않는다)
    nest = stack_parts(spec)["backend"] == "nestjs"   # NestJS 틀: backend/는 scaffold_nestjs가 만든다
    if python:
        built = {fid: r for fid, r in built.items() if fa.python_module(r["controller"])}
    if nest:
        built = {fid: r for fid, r in built.items() if ne.ts_module(r["controller"])}
    own = python or nest   # Spring 틀(templates/app/backend)을 쓰지 않는 서버 틀
    menu = {**(spec.meta.get("group_names") or {}), **(menu_names or {})}
    data = build_screens(spec, menu)
    data["accounts"] = demo_accounts(data)
    owned, skipped = owned_operations(spec)
    tasks = {s["function_id"]: s["task"] for s in [*data["screens"], *data["autos"]]}
    files: dict[str, str] = {}
    mysql = db_dialect(spec) == "mysql"
    for path in sorted(p for p in TEMPLATES.rglob("*") if p.is_file()):
        rel = path.relative_to(TEMPLATES).as_posix()
        if (backend and not own) or not rel.startswith("backend/"):
            files[rel] = path.read_text(encoding="utf-8")
            if mysql:
                files[rel] = _for_mysql(rel, files[rel])
    rows = sample_rows(spec.ddl, load_words(spec.words))
    files["frontend/src/generated/screens.json"] = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    from app.agents.tools import themes
    files["frontend/src/theme.css"] = themes.css(spec.options.get("theme"))
    files["frontend/src/generated/mock-data.json"] = json.dumps(mock_data(spec.model, spec.names, rows), ensure_ascii=False, indent=1) + "\n"
    root = "backend/src/main/java/app"
    if backend and not own:
        files[f"{root}/api/LookupController.java"] = _lookup_controller(spec, data)
    for f in spec.functions if own else []:   # 서버 코드는 아래 render_backend, 여기서는 LLM이 쓴 화면만
        if f.function_id in built:
            files[f"frontend/src/pages/{class_name(f.function_id)}.tsx"] = built[f.function_id]["page"]
    for f in spec.functions if backend and not own else []:
        cls = class_name(f.function_id)
        ops = owned[f.function_id]
        if f.function_id in built:
            files[f"{root}/feature/{cls}Controller.java"] = built[f.function_id]["controller"]
            files[f"frontend/src/pages/{cls}.tsx"] = built[f.function_id]["page"]
        elif ops:
            files[f"{root}/api/{cls}Controller.java"] = _controller(spec, f, ops)
        if f.function_id not in built:
            files[f"{root}/logic/{cls}Logic.java"] = _logic(spec, f, ops, tasks.get(f.function_id, ""), logic.get(f.function_id) or {})
        files[f"backend/src/test/java/app/acceptance/{cls}AcceptanceTest.java"] = _acceptance_test(spec, f)
    if built:
        files["frontend/src/pages/index.ts"] = _pages_index(sorted(built))
    # 설계 산출물 schema.sql은 PostgreSQL이다. MySQL이면 같은 모델(spec.ddl)에서 MySQL 문법으로 다시 만든다 (spec/schema.sql은 그대로)
    files["db/schema.sql"] = render_script(spec.ddl, spec.version, "mysql") if mysql else spec.read("schema.sql") or ""
    files["db/seed.sql"] = seed_sql(spec.ddl, rows, db_dialect(spec))
    if python:
        files.update(fa.render_backend(spec, data, owned, logic, built))
        files["docker-compose.yml"] = fa.COMPOSE_MYSQL if mysql else fa.COMPOSE
        files["Dockerfile"] = fa.DOCKERFILE
        files[".dockerignore"] = fa.DOCKERIGNORE
    elif nest:
        files.update(ne.render_backend(spec, data, owned, built, logic))
        files["docker-compose.yml"] = ne.COMPOSE_MYSQL if mysql else ne.COMPOSE
        files["Dockerfile"] = ne.DOCKERFILE
        files[".dockerignore"] = ne.DOCKERIGNORE
    elif backend:
        files["backend/src/main/resources/accounts.json"] = json.dumps(data["accounts"], ensure_ascii=False, indent=1) + "\n"
        files["docker-compose.yml"] = COMPOSE_MYSQL if mysql else COMPOSE
        files["Dockerfile"] = DOCKERFILE
        files[".dockerignore"] = DOCKERIGNORE
    # .preview(미리보기·브랜치 작업 폴더)·.req-agent(생성기 기록)는 코드 저장소에 커밋하지 않는다 (services/repo.py)
    files[".gitignore"] = "node_modules/\ndist/\ntarget/\n.env\nlocal/\n.preview/\n.req-agent/\n" + ("__pycache__/\n.venv/\n" if python else "")
    files["README.md"] = _readme(spec, data, skipped)
    files["IMPLEMENTATION.md"] = _implementation(spec, data, owned, built, logic, records, skipped)
    files["spec/SCREENS.md"] = screens_md(spec, data)
    files["spec/SPEC_VERSION"] = json.dumps({"project": spec.project, "version": spec.version, "format": APP_FORMAT,
                                             "document": (spec.meta.get("document") or {}).get("document_version", "")},
                                            ensure_ascii=False, indent=1) + "\n"
    for name in (*SPEC_FILES, "schema.sql", "erd.mmd"):   # ERD 그림(Mermaid)도 개발 AI가 볼 수 있게
        if (text := spec.read(name)) is not None:
            files[f"spec/{name}"] = text
    if stack_parts(spec)["frontend"] == "nextjs":
        files = _for_nextjs(files)
    return files, data


NEXT_TEMPLATES = TEMPLATES.parent / "frontend-nextjs"
VITE_ONLY = ("frontend/index.html", "frontend/vite.config.ts", "frontend/src/main.tsx", "frontend/package.json", "frontend/tsconfig.json")


def _for_nextjs(files: dict[str, str]) -> dict[str, str]:
    """화면 틀을 Next.js로: 화면 부품(React 틀의 src/)은 그대로 ui/로 옮기고(Next.js는 src/pages를 라우트로 본다)
    Next.js 설정·진입점(templates/frontend-nextjs)을 더한다. 정적 내보내기(out/)를 서버가 함께 보여 준다."""
    out: dict[str, str] = {}
    for rel, text in files.items():
        if rel in VITE_ONLY:
            continue
        if rel.startswith("frontend/src/"):
            rel = "frontend/ui/" + rel[len("frontend/src/"):]
        out[rel] = text
    for path in sorted(p for p in NEXT_TEMPLATES.rglob("*") if p.is_file()):
        out["frontend/" + path.relative_to(NEXT_TEMPLATES).as_posix()] = path.read_text(encoding="utf-8")
    api = "frontend/ui/api.ts"
    if "import.meta.env.VITE_API === 'server'" not in out.get(api, ""):
        raise AppError("Next.js 틀로 바꿀 곳을 찾지 못했습니다 (ui/api.ts의 서버 모드 설정).")
    out[api] = out[api].replace("import.meta.env.VITE_API === 'server'", "process.env.NEXT_PUBLIC_API === 'server'") \
        .replace("VITE_API=server면", "NEXT_PUBLIC_API=server면")
    if "Dockerfile" in out:
        out["Dockerfile"] = out["Dockerfile"].replace("ENV VITE_API=server", "ENV NEXT_PUBLIC_API=server").replace("/web/dist", "/web/out")
    for doc in ("README.md", "IMPLEMENTATION.md"):
        if doc in out:
            out[doc] = out[doc].replace("frontend/src/", "frontend/ui/").replace("VITE_API=server", "NEXT_PUBLIC_API=server")
    out[".gitignore"] = out.get(".gitignore", "") + ".next/\nout/\n"
    return out


def _pages_index(function_ids: list[str]) -> str:
    """기능 구현 Agent가 쓴 화면 목록 (기능 ID → 화면). App이 이 기능은 공통 틀 대신 이 화면으로 그린다."""
    lines = ["import type { ComponentType } from 'react'", "import type { PageProps } from '../kit'"]
    lines += [f"import {class_name(fid)}Page from './{class_name(fid)}'" for fid in function_ids]
    lines += ["", "// 개발 Agent(LLM)가 기능 전체를 구현한 화면 (기능 ID → 화면). 여기 없는 기능은 공통 화면 틀(Screen.tsx)로 그린다. 코드가 만든 파일.",
              "export const pages: Record<string, ComponentType<PageProps>> = {",
              *[f"  '{fid}': {class_name(fid)}Page," for fid in function_ids], "}", ""]
    return "\n".join(lines)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_app(files: dict[str, str], out_dir: Path, *, force: bool = False) -> dict:
    """파일을 쓴다. 지난번에 쓴 내용과 다른 파일(사람이 고친 파일)은 그대로 두고, 스펙에서 사라진 파일은 지운다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / MANIFEST
    old = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {}) if manifest_path.exists() else {}
    report = {"written": [], "same": 0, "kept": [], "removed": []}
    hashes = {}
    for rel, text in sorted(files.items()):
        path = out_dir / rel
        hashes[rel] = _sha(text)
        if path.exists():
            current = _sha(path.read_bytes().decode("utf-8", "replace"))
            if current == hashes[rel]:
                report["same"] += 1
                continue
            if not force and current != old.get(rel):   # 지난번 결과와 다름 = 사람이 고침 (기록이 없으면 건드리지 않는다)
                report["kept"].append(rel)
                hashes[rel] = old.get(rel, hashes[rel])
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        report["written"].append(rel)
    for rel, value in sorted(old.items()):
        path = out_dir / rel
        if rel not in files and path.exists():
            if force or _sha(path.read_bytes().decode("utf-8", "replace")) == value:
                path.unlink()
                report["removed"].append(rel)
            else:
                report["kept"].append(rel)
                hashes[rel] = value
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"format": APP_FORMAT, "files": hashes}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


def load_logic(out_dir: Path) -> dict[str, dict]:
    """저장된 채운 자리 {기능 ID: 기록}."""
    folder = out_dir / LOGIC_DIR
    if not folder.exists():
        return {}
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))}


def save_logic(out_dir: Path, function_id: str, record: dict) -> None:
    folder = out_dir / LOGIC_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{function_id}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def app_dir(project: str) -> Path:
    """프로젝트의 서비스 코드 폴더 OUTPUT_DIR/<프로젝트>/app (버전이 바뀌어도 한 폴더. 스펙이 바뀌면 바뀐 파일만 달라진다).
    스펙은 산출물 저장소(DB일 수 있다)에서 읽고, 코드는 이 로컬 폴더에 쓴다."""
    return Path(get_settings().output_dir) / project_name(project) / "app"
