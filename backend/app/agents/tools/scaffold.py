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
from app.agents.tools.ddl import q
from app.agents.tools.dev_spec import FILES as SPEC_FILES, Names, Spec, rule_tag
from app.agents.tools.sample_data import load_words, mock_data, sample_rows, seed_sql
from app.agents.tools.screens import (SECRET_COLUMN, build_screens, label_column, op_kind, op_table, path_params,
                                      screens_md)
from app.agents.storage import get_store
from app.agents.tools.versions import find_version, project_name, version_label
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
SPRING_STACK = re.compile(r"spring|전자정부|egov", re.I)   # 백엔드 뼈대가 있는 스택


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
    options = {"standard": record.get("standard"), "title": title.group(1).strip() if title else ""}
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


def has_backend(spec: Spec) -> bool:
    """이 스펙의 기술 스택에 맞는 백엔드 뼈대가 있는가. 없으면 화면·DB·스펙만 만든다."""
    return bool(SPRING_STACK.search(spec.stack.get("backend", "")))


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
    return f'new Col({_text(field)}, {_text(q(spec.names.column(table, column)))}, {_text(_pg(spec, table, column))})'


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
    physical = q(spec.names.table(table))
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
                joins.append(f"LEFT JOIN {q(spec.names.table(t))} {aliases[t]} ON {aliases[t]}.{q(spec.names.column(t, parent.pk()[0].name))}"
                             f" = {physical}.{q(spec.names.column(table, fk.name))}")
            source = aliases[t]
        seen.add(f["name"])
        cols.append(f'new Col({_text(f["name"])}, {_text(source + "." + q(spec.names.column(t, column)))}, {_text(_pg(spec, t, column))})')
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
        name = q(spec.names.column(table, a.name))
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
    created = [q(spec.names.column(table, c)) for c in AUDIT_NOW if c in names]
    updated = [q(spec.names.column(table, c)) for c in AUDIT_NOW[1:] if c in names]
    as_list = lambda xs: "List.of(" + ", ".join(_text(x) for x in xs) + ")"   # noqa: E731
    return (f"    private static final Table {_table_const(table)} = new Table({_text(q(spec.names.table(table)))}, "
            f"{_text(q(spec.names.column(table, pk.name)))}, {_text(_pg(spec, table, pk.name))}, {as_list(created)}, {as_list(updated)});")


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
        items = [f'new Col({_text(f)}, {_text(q(spec.names.table(table)) + "." + q(spec.names.column(table, c)))}, {_text(_pg(spec, table, c))})'
                 for f, c in parents]
        constants.append(f"    private static final List<Col> FILTER_{name} = {_java_list(items)};")
        return f"db.first({t}, OUT_{name}, FILTER_{name}, input)"
    if kind == "report" and id_var and _snake(id_var) == pk:   # /orders/{orderId}/print: 그 행 하나
        return f"db.one({t}, OUT_{name}, input.get({_text(id_var)}))"
    if kind in ("list", "report"):
        physical = q(spec.names.table(table))
        filters = _unique([*_cols(spec, table, op.get("request") or [], writable=True), *parents])
        items = [f'new Col({_text(f)}, {_text(physical + "." + q(spec.names.column(table, c)))}, {_text(_pg(spec, table, c))})' for f, c in filters]
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
        return (f"db.moveTo({t}, {_text(q(spec.names.column(table, move['column'])))}, Map.of({allowed}), input.get({_text(target)}), "
                f"input.get({_text(id_var)}), OUT_{name}{extra or ', List.of(), Map.of()'})")
    if sets and id_var and sets.get("from"):   # 전환표에 있는 처리: 허용된 상태에서만 바꾼다
        allowed = ", ".join(_text(v) for v in sets["from"])
        return f"db.move({t}, {_text(q(sets['column']))}, List.of({allowed}), {_text(sets['value'])}, input.get({_text(id_var)}), OUT_{name}{extra})"
    if sets and id_var:
        return f"db.set({t}, {_text(q(sets['column']))}, {_text(sets['value'])}, input.get({_text(id_var)}), OUT_{name}{extra})"
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
            rows.append(f"        Map.entry({_text(ref)}, new Lookup({_text(q(ref))}, {_text(q(pk))}, {_text(q(label))}))")
    entries = ",\n".join(rows)
    tables, reads = _form_reads(spec, data)
    decls = "\n".join(_table_decl(spec, t) for t in tables)
    read_entries = ",\n".join(reads)
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
        return db.query("SELECT CAST(" + found.pk() + " AS text) AS \\"value\\", CAST(" + found.label() + " AS text) AS \\"label\\" FROM " + found.table()
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


def _readme(spec: Spec, data: dict, skipped: list[str]) -> str:
    backend = has_backend(spec)
    lines = [f"# {spec.project} — 서비스 코드", "",
             f"개발 스펙 v{spec.version}에서 코드로 만든 프로젝트다 ({APP_FORMAT}). 화면 {len(data['screens'])}개 · 자동 실행 {len(data['autos'])}개.",
             "화면 목록은 [spec/SCREENS.md](spec/SCREENS.md), 스펙 문서는 [spec/](spec/)에 있다.", "",
             "## 화면만 보기 (서버·DB 필요 없음)", "", "```bash", "cd frontend", "npm install", "npm run dev", "```", "",
             "예시 데이터로 모든 화면이 동작한다. 고객에게 화면 단위로 보여 줄 때 쓴다. 새로고침하면 처음 데이터로 돌아간다.",
             "오른쪽 위 '검토 모드'를 켜면 화면마다 업무 규칙과 고객 확인이 필요한 항목이 보인다.", "",
             *(["## 서버까지 띄우기", "", "```bash", "docker compose up -d          # 예시 데이터가 들어간 PostgreSQL (포트 5433)",
                "cd backend && mvn spring-boot:run", "cd frontend && VITE_API=server npm run dev", "```", ""] if backend else
               ["## 백엔드", "", f"기술 스택 '{spec.stack.get('backend') or '미정'}'의 뼈대는 아직 없어 화면·DB·스펙만 만들었다.", ""]),
             "## 어디를 고치나", "",
             "| 위치 | 내용 | 고쳐도 되나 |", "|---|---|---|",
             "| `backend/.../logic/` | 기능별 업무 로직 (`// <hole>` 안) | 예. 고친 파일은 다시 생성해도 그대로 둔다 |",
             "| `backend/.../api/` | 스펙에서 만든 API | 아니오. 스펙을 고치고 다시 생성한다 |",
             "| `frontend/src/generated/` | 스펙에서 만든 화면 설계·예시 데이터 | 아니오. 스펙을 고치고 다시 생성한다 |",
             "| `backend/src/test/.../acceptance/` | 수용 기준 테스트 틀 (`@Disabled`) | 예. 채우면 그대로 둔다 |", ""]
    if any(_system_caller(op) for _, op in spec.operations()):
        lines += ["## 외부 시스템 호출", "", "외부 시스템이 호출하는 API는 `X-Api-Key` 헤더로 확인한다. 서버 환경 변수 `SYSTEM_API_KEY`에 키를 정한다 (정하지 않으면 모두 막힌다).", ""]
    if skipped:
        lines += ["## 경로가 겹쳐 만들지 않은 API", "", *[f"- {s}" for s in skipped], ""]
    return "\n".join(lines)


def render_app(spec: Spec, menu_names: dict[str, str] | None = None, logic: dict[str, dict] | None = None) -> tuple[dict[str, str], dict]:
    """프로젝트 파일 전체 {상대 경로: 내용}과 화면 설계. logic은 기능별 채운 자리 {기능 ID: {메서드: 본문}}."""
    logic = logic or {}
    backend = has_backend(spec)
    menu = {**(spec.meta.get("group_names") or {}), **(menu_names or {})}
    data = build_screens(spec, menu)
    owned, skipped = owned_operations(spec)
    tasks = {s["function_id"]: s["task"] for s in [*data["screens"], *data["autos"]]}
    files: dict[str, str] = {}
    for path in sorted(p for p in TEMPLATES.rglob("*") if p.is_file()):
        rel = path.relative_to(TEMPLATES).as_posix()
        if backend or not rel.startswith("backend/"):
            files[rel] = path.read_text(encoding="utf-8")
    rows = sample_rows(spec.ddl, load_words(spec.words))
    files["frontend/src/generated/screens.json"] = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    files["frontend/src/generated/mock-data.json"] = json.dumps(mock_data(spec.model, spec.names, rows), ensure_ascii=False, indent=1) + "\n"
    root = "backend/src/main/java/app"
    if backend:
        files[f"{root}/api/LookupController.java"] = _lookup_controller(spec, data)
    for f in spec.functions if backend else []:
        cls = class_name(f.function_id)
        ops = owned[f.function_id]
        if ops:
            files[f"{root}/api/{cls}Controller.java"] = _controller(spec, f, ops)
        files[f"{root}/logic/{cls}Logic.java"] = _logic(spec, f, ops, tasks.get(f.function_id, ""), logic.get(f.function_id) or {})
        files[f"backend/src/test/java/app/acceptance/{cls}AcceptanceTest.java"] = _acceptance_test(spec, f)
    files["db/schema.sql"] = spec.read("schema.sql") or ""
    files["db/seed.sql"] = seed_sql(spec.ddl, rows)
    if backend:
        files["docker-compose.yml"] = ("# 시연용 PostgreSQL (schema.sql + seed.sql). 운영에 쓰지 않는다.\nservices:\n  db:\n    image: postgres:16\n    environment:\n"
                                       "      POSTGRES_USER: app\n      POSTGRES_PASSWORD: app\n      POSTGRES_DB: app\n    ports:\n      - \"5433:5432\"\n    volumes:\n"
                                       "      - ./db/schema.sql:/docker-entrypoint-initdb.d/01_schema.sql:ro\n      - ./db/seed.sql:/docker-entrypoint-initdb.d/02_seed.sql:ro\n")
    files[".gitignore"] = "node_modules/\ndist/\ntarget/\n.env\n"
    files["README.md"] = _readme(spec, data, skipped)
    files["spec/SCREENS.md"] = screens_md(spec, data)
    files["spec/SPEC_VERSION"] = json.dumps({"project": spec.project, "version": spec.version, "format": APP_FORMAT,
                                             "document": (spec.meta.get("document") or {}).get("document_version", "")},
                                            ensure_ascii=False, indent=1) + "\n"
    for name in (*SPEC_FILES, "schema.sql"):
        if (text := spec.read(name)) is not None:
            files[f"spec/{name}"] = text
    return files, data


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
