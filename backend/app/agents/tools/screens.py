"""화면 설계: 개발 스펙(기능 명세·API 설계·데이터 모델)에서 화면 목록을 코드로 만든다 (LLM 없음).

- 기능 하나 = 화면(탭) 하나 = 작업(TASK) 하나. 화면 ID는 기능 ID에서 나오므로 버전이 바뀌어도 그대로다 (FN-SFR-001-05 → SCR-SFR-001-05).
- 메뉴는 업무 대상 단위다: 같은 영역에서 같은 테이블을 다루는 화면은 메뉴 하나의 탭으로 묶는다 (주문 입력·승인·출력 → 주문 관리).
- 화면 유형은 API 모양으로 정한다: crud(목록+등록·수정·삭제) · list(조회) · report(조건 조회+요약) · form(입력만) ·
  action(대상을 골라 처리) · auto(사람이 부르는 API가 없는 자동 실행, 메뉴에 넣지 않고 안내만).
- 값은 모두 입력에서 가져오고 시각·난수를 쓰지 않는다. 같은 입력이면 항상 같은 결과다.
- 비밀 컬럼(*_hash·*_enc 등)과 감사 컬럼(created_at 등)은 화면에 내보내지 않는다.
"""
from __future__ import annotations

import re
from collections import Counter

from app.agents.roles.function_designer import SYSTEM_ACTOR, role_names
from app.agents.tools.api_shape import is_status, op_kind, path_params, snake, table_of
from app.agents.tools.dev_spec import Spec, _tasks, rule_tag

SECRET_COLUMN = re.compile(r"(_hash|_enc|_salt|password|secret)$|^password")
AUDIT_COLUMNS = {"created_at", "updated_at", "created_by", "updated_by", "deleted_at"}
# 처리 API의 앞 동사 → (버튼 이름, 상태 값을 찾을 때 볼 표현)
ACTIONS = {
    "approve": ("승인", ("APPROVED", "승인")), "reject": ("반려", ("REJECTED", "반려")),
    "cancel": ("취소", ("CANCELED", "CANCELLED", "취소", "DRAFT")), "revoke": ("철회", ("REVOKED", "철회")),
    "submit": ("제출", ("SUBMITTED", "IN_REVIEW", "제출", "상신")), "request": ("요청", ("REQUESTED", "IN_REVIEW", "요청")),
    "confirm": ("확정", ("CONFIRMED", "확정")), "complete": ("완료", ("COMPLETED", "DONE", "완료")),
    "close": ("마감", ("CLOSED", "마감")), "pay": ("지급", ("PAID", "지급")), "publish": ("게시", ("PUBLISHED", "게시")),
    "send": ("발송", ("SENT", "발송")), "retire": ("퇴직 처리", ("RETIRED", "퇴직")), "settle": ("정산", ("SETTLED", "정산")),
}
# 코드값 → 화면에 보일 말 (모르는 값은 그대로 보여 준다)
CODE_LABELS = {
    "DRAFT": "작성 중", "IN_REVIEW": "검토 중", "APPROVED": "승인", "REJECTED": "반려", "CANCELED": "취소", "CANCELLED": "취소",
    "SUBMITTED": "제출", "REQUESTED": "요청", "CONFIRMED": "확정", "COMPLETED": "완료", "DONE": "완료", "PENDING": "대기",
    "ACTIVE": "사용", "INACTIVE": "미사용", "CLOSED": "마감", "OPEN": "진행 중", "PAID": "지급 완료", "DEBIT": "차변",
    "CREDIT": "대변", "PUBLISHED": "게시", "SENT": "발송", "FAILED": "실패", "SUCCESS": "성공",
}
PATTERN_LABELS = {"crud": "목록·등록", "list": "조회", "report": "조건 조회·요약", "form": "입력", "action": "대상 처리", "auto": "자동 실행"}


def screen_id(function_id: str) -> str:
    """기능 ID → 화면 ID."""
    return "SCR-" + function_id.removeprefix("FN-")


def op_table(spec: Spec, op: dict, function_id: str = "") -> str:
    """API가 다루는 테이블. 필드에 컬럼이 없으면(DELETE·처리 API) 경로의 ID로 찾고, 기능에 테이블이 하나면 그 테이블이다."""
    return table_of(op, spec.model.entities, spec.linked.get(function_id) or [])


def _hidden(column: str) -> bool:
    name = column.rpartition(".")[2]
    return bool(SECRET_COLUMN.search(name)) or name in AUDIT_COLUMNS


def _ui_type(attr) -> str:
    if attr.allowed_values:
        return "select"
    if attr.references:
        return "ref"
    return {"bigint": "number", "integer": "number", "decimal": "number", "date": "date", "timestamp": "datetime",
            "boolean": "boolean", "text": "textarea"}.get(attr.data_type, "text")


def _field(spec: Spec, field: dict) -> dict:
    """API 필드 하나 → 화면 칸."""
    table, _, column = (field.get("column") or "").partition(".")
    attr = spec.columns.get((table, column))
    out = {"name": field["name"], "label": (attr.label if attr else "") or field.get("description") or field["name"],
           "type": _ui_type(attr) if attr else {"number": "number", "integer": "number", "boolean": "boolean"}.get(field.get("type"), "text"),
           "required": bool(field.get("required")),
           "column": spec.names.ref(field["column"]) if attr else ""}
    if attr and attr.allowed_values:
        out["options"] = [{"value": v, "label": CODE_LABELS.get(v, v)} for v in attr.allowed_values]
        out["status"] = is_status(attr)
    if attr and attr.references:
        out["ref"] = spec.names.table(attr.references)
    return out


def _fields(spec: Spec, fields: list[dict]) -> list[dict]:
    return [_field(spec, f) for f in fields if not (f.get("column") and _hidden(f["column"]))]


def _status_column(spec: Spec, table: str) -> str:
    """테이블의 상태 컬럼 (허용값이 있고 이름이 status·state로 끝나는 첫 컬럼)."""
    entity = next((e for e in spec.model.entities if e.entity_id == table), None)
    return next((a.name for a in (entity.attributes if entity else []) if is_status(a)), "")


BOUND_WORD = r"(을|를|은|는|이|가|의|에|서|로|와|과|고|여|게|된|한)$"   # 조사·어미가 붙어 동사와 이어지지 않는 낱말


def _button(summary: str, fallback: str) -> str:
    """API 설명의 첫 동작으로 버튼 이름을 만든다. 목적어가 있으면 목적어와 동사, 없으면 끝의 두 낱말이다.

    ('결재문서를 결재선과 함께 상신한다' → '결재문서 상신', '주문 승인을 요청한다' → '승인 요청', '임직원 인사정보 등록' → '인사정보 등록')"""
    text = re.sub(r"\s*\([^)]*\)", "", summary).replace(" 또는 ", "·")   # 괄호 설명은 버튼에 넣지 않는다
    text = re.split(r"(?<=[가-힣])(?:하고|하며)\s", text, maxsplit=1)[0]   # '조정하고 변경이력을 저장한다' → 앞 동작
    if not re.search(r"[을를](\s|$)", text):   # 명사구 설명: '재직 상태 변경 및 이력 저장' → 앞 동작
        text = text.split(" 및 ", 1)[0]
    words = [w for w in (re.sub(r"(한다|합니다|하다)\.?$", "", w) for w in text.split()) if w]
    objects = [w[:-1] for w in words[:-1] if w[-1] in "을를" and len(w) > 1]
    compound = len(words) > 1 and words[-2] not in ("함께", "직접", "모두", "즉시") and not re.search(BOUND_WORD, words[-2])   # '승인 처리'처럼 붙여 쓰는 동사
    pair = [objects[-1], words[-1]] if objects and not compound else [re.sub(r"(의|에|으로|로)$", "", w) for w in words[-2:]]
    label = " ".join(pair)
    return label if words and len(label) <= 14 else fallback or (words[-1] if words else "")


def action_of(spec: Spec, op: dict, table: str) -> dict:
    """처리 API 하나 → 버튼. 상태 컬럼을 바꾸는 처리면 어떤 값으로 바꾸는지도 정한다 (LLM 없이 동작하는 기본 처리).

    스펙의 상태 전환표를 먼저 쓴다. 표에 없는 API만 동사와 허용값으로 추정한다 (전환표가 없는 옛 버전)."""
    verb = re.match(r"[a-z]+", op["operation_id"])
    name, words = ACTIONS.get(verb.group(0) if verb else "", ("", ()))
    out = {"operation_id": op["operation_id"], "label": _button(op.get("summary") or "", name) or op["operation_id"], "kind": op_kind(op)}
    move = spec.transition_of(op["operation_id"])
    if move and move["table"] == table and len(move["moves"]) > 1:   # 목표 상태는 입력 칸에서 고른다
        return out
    if move and move["table"] == table:
        out["sets"] = {"column": spec.names.column(table, move["column"]), "value": move["to"], "label": CODE_LABELS.get(move["to"], move["to"]), "from": move["from"]}
        return out
    column = _status_column(spec, table)
    if column and words:
        allowed = next(a.allowed_values for e in spec.model.entities if e.entity_id == table for a in e.attributes if a.name == column)
        value = next((v for w in words for v in allowed if w.lower() in v.lower()), "")
        if value:
            out["sets"] = {"column": spec.names.column(table, column), "value": value, "label": CODE_LABELS.get(value, value)}
    return out


def label_column(entity) -> str:
    """행을 사람이 알아볼 컬럼 (설계 이름): 이름·제목 → 코드·번호 → 첫 글자 컬럼 → 기본키. 비밀 컬럼은 쓰지 않는다."""
    usable = [a for a in entity.attributes if a.data_type in ("varchar", "text") and not a.primary_key and not a.references
              and not a.allowed_values and not SECRET_COLUMN.search(a.name)]
    named = next((a for a in usable if re.search(r"(^|_)(name|title|subject)$", a.name) or re.search(r"(명|이름|성명|제목)$", a.label or "")), None)
    coded = next((a for a in usable if re.search(r"(_code|_no|_number)$", a.name)), None)
    chosen = named or coded or (usable[0] if usable else None)
    return chosen.name if chosen else (entity.pk()[0].name if entity.pk() else entity.attributes[0].name)


def _roles(spec: Spec) -> list[dict]:
    roles = spec.common.get("roles") or []
    if roles:
        return [{"name": r["name"], "scope": r.get("scope", "")} for r in roles]
    actors = sorted({a for f in spec.functions for a in f.actors if not SYSTEM_ACTOR.match(a)})
    return [{"name": a, "scope": ""} for a in actors]


def _screen_roles(spec: Spec, actors: list[str]) -> list[str]:
    """화면을 쓰는 사람 역할. 사람 행위자가 없으면 빈 목록 (모든 역할에 보인다)."""
    names = role_names(spec.common)
    out = [names.get(re.sub(r"\s+", "", a), a) for a in actors if not SYSTEM_ACTOR.match(a)]
    return list(dict.fromkeys(out))


def _param_field(spec: Spec, table: str, param: str) -> dict:
    """경로 변수 → 조회 칸 (예: /orders/{orderId}/print의 주문). 고를 대상이 있으면 목록에서 고른다."""
    column = snake(param)
    attr = spec.columns.get((table, column))
    if attr is None:   # 이 테이블에 없으면 그 ID를 기본키로 가진 테이블에서 찾는다
        table = next((e.entity_id for e in spec.model.entities if any(a.primary_key and a.name == column for a in e.attributes)), "")
        attr = spec.columns.get((table, column))
    if attr is None:
        return {"name": param, "label": param, "type": "text", "required": True, "column": ""}
    target = attr.references or (table if attr.primary_key else "")
    return {"name": param, "label": attr.label or param, "type": "ref" if target else "text", "required": True,
            "column": spec.names.ref(f"{table}.{column}"), **({"ref": spec.names.table(target)} if target else {})}


def _row_params(spec: Spec, op: dict, table: str, rows_op: dict | None, function_id: str = "") -> set[str]:
    """목록의 행에서 값이 정해지는 경로 변수: 행의 기본키, 행의 응답 필드, 같은 테이블 API의 마지막 변수."""
    if not rows_op:
        return set()
    params = path_params(op["path"])
    keys = {a.name for e in spec.model.entities if e.entity_id == table for a in e.pk()}
    out = {p for p in params if snake(p) in keys or any(f["name"] == p for f in rows_op.get("response") or [])}
    return out | set(params[-1:]) if op_table(spec, op, function_id) in ("", table) else out


def _inputs(spec: Spec, op: dict, table: str, rows_op: dict | None = None, function_id: str = "") -> list[dict]:
    """입력 칸. 경로 변수는 입력값이 ID를 덮어쓰지 않게 빼고, 행에서 못 정하는 변수만 고르는 칸으로 앞에 둔다 (행의 ID가 다른 테이블의 ID로 들어가면 엉뚱한 행을 고친다)."""
    params = path_params(op["path"])
    known = _row_params(spec, op, table, rows_op, function_id)
    target = op_table(spec, op, function_id) or table
    return [*[_param_field(spec, target, p) for p in params if p not in known],
            *_fields(spec, [f for f in op.get("request") or [] if f["name"] not in params])]


def _view(spec: Spec, op: dict, table: str) -> dict:
    return {"operation_id": op["operation_id"], "path": op["path"], "filters": _inputs(spec, op, table),
            "columns": _fields(spec, op.get("response") or [])}


def _for_people(op: dict) -> bool:
    """사람이 화면에서 쓰는 API인가. 외부 시스템만 호출하는 API는 화면에 두지 않는다."""
    actors = op.get("actors") or []
    return not actors or any(not SYSTEM_ACTOR.match(a) for a in actors)


def _by_kind(ops: list[dict]) -> dict[str, dict]:
    kinds: dict[str, dict] = {}
    for op in ops:
        kinds.setdefault(op_kind(op), op)
    return kinds


def build_screens(spec: Spec, menu_names: dict[str, str] | None = None) -> dict:
    """화면 설계 전체: 메뉴·역할·화면 목록·자동 실행 목록."""
    menu_names = menu_names or {}
    tasks = {re.search(r"기능 (\S+)", t["refs"]).group(1): t for t in _tasks(spec) if "기능 " in t.get("refs", "")}
    owned = {o["operation_id"]: o for _, o in spec.operations()}
    list_of: dict[str, dict] = {}   # 테이블 → 그 테이블을 나열하는 API (처리 화면의 대상 목록)
    for _, op in spec.operations():
        if op_kind(op) == "list" and op_table(spec, op):
            list_of.setdefault(op_table(spec, op), op)
    screens, autos = [], []
    for f in spec.functions:
        ops = (spec.api.get(f.function_id) or {}).get("operations") or []
        ops = [owned.get(op["shared_with"], op) if op.get("shared_with") else op for op in ops]
        rules = [{"kind": rule_tag(r)[0], "text": rule_tag(r)[1]} for r in f.business_rules]
        base = {"screen_id": screen_id(f.function_id), "function_id": f.function_id, "name": f.name, "group": f.group,
                "description": f.description, "task": (tasks.get(f.function_id) or {}).get("id", ""), "rules": rules,
                "ac_count": len(spec.acceptance[f.function_id])}
        people = [op for op in ops if _for_people(op)]
        if not people:
            reason = (spec.api.get(f.function_id) or {}).get("no_api_reason", "") or ("외부 시스템이 호출하는 API만 있다" if ops else "")
            autos.append({**base, "reason": reason})
            continue
        ops = people
        kinds = _by_kind(ops)
        lead = kinds.get("list") or kinds.get("create") or kinds.get("update") or kinds.get("report") or ops[0]
        table = op_table(spec, lead, f.function_id) or next(iter(spec.linked[f.function_id]), "")
        rows_op = kinds.get("list") or list_of.get(table)   # 처리 화면은 다른 기능의 목록 API로 대상을 보여 준다
        # 목록의 행을 고치는 API만 수정·삭제 버튼이 된다. 행이 없거나 다른 테이블을 고치는 API는 대상을 고르는 처리 버튼으로 둔다
        detached = [op for op in ops if op_kind(op) in ("update", "delete") and not (rows_op and op_table(spec, op, f.function_id) in ("", table))]
        kinds = _by_kind([op for op in ops if op not in detached])
        writes = {"create", "update", "delete"} & set(kinds)
        pattern = ("list" if kinds.get("list") and not writes else "crud" if writes and rows_op else "form" if writes
                   else "report" if kinds.get("report") else "action")
        entity = next((e for e in spec.model.entities if e.entity_id == table), None)
        pk = entity.pk()[0].name if entity and entity.pk() else ""
        screen = {
            **base, "pattern": pattern, "pattern_label": PATTERN_LABELS[pattern], "roles": _screen_roles(spec, f.actors),
            "table": spec.names.table(table) if table else "", "table_label": entity.label if entity else "",
            "pk": spec.names.column(table, pk) if pk else "",
            "operations": [{k: op.get(k) for k in ("method", "path", "operation_id", "summary")} | {"kind": op_kind(op)} for op in ops],
            "list": None, "report": None, "create": None, "update": None, "delete": None, "actions": [],
        }
        if rows_op:
            screen["list"] = _view(spec, rows_op, op_table(spec, rows_op) or table)
        elif kinds.get("report"):
            screen["report"] = _view(spec, kinds["report"], table)
        for key in ("create", "update"):
            if kinds.get(key):
                op = kinds[key]
                screen[key] = {"operation_id": op["operation_id"], "path": op["path"], "method": op["method"],
                               "fields": _inputs(spec, op, table, rows_op if key == "update" else None, f.function_id)}
        if kinds.get("delete"):
            screen["delete"] = {"operation_id": kinds["delete"]["operation_id"], "path": kinds["delete"]["path"]}
        for op in ops:
            if op_kind(op) in ("action", "add") or op in detached:
                screen["actions"].append({**action_of(spec, op, table), "method": op["method"], "path": op["path"],
                                          "fields": _inputs(spec, op, table, rows_op, f.function_id)})
        screens.append(screen)
    roles = _roles(spec)
    known = {r["name"] for r in roles}
    roles += [{"name": n, "scope": ""} for n in sorted({r for s in screens for r in s["roles"]} - known)]   # 역할 목록에 없는 행위자도 고를 수 있게
    return {
        "project": spec.project, "title": spec.options.get("title") or spec.project, "company": spec.options.get("company") or "",
        "version": spec.version, "stack": spec.stack.get("backend", ""),
        "menu": _menu(spec, screens, menu_names),
        "roles": roles, "screens": screens, "autos": autos,
    }


def short_name(name: str) -> str:
    """상단 메뉴에 쓸 짧은 이름 ('서비스 요청 및 처리' → '서비스 요청')."""
    return re.split(r"\s+및\s+", name)[0]


def _menu(spec: Spec, screens: list[dict], names: dict[str, str]) -> list[dict]:
    """기능 영역별 메뉴. 큰 영역을 나눈 그룹(SFR.1·SFR.2)은 한 영역이다. 화면이 없는 영역은 뺀다."""
    base = lambda group: group.split(".")[0]   # noqa: E731
    out = []
    for key in dict.fromkeys(base(f.group) for f in spec.functions):
        items = [s for s in screens if base(s["group"]) == key]
        if not items:
            continue
        first = next(f for f in spec.functions if base(f.group) == key)
        title = next((spec.by_req[r].title for r in first.requirement_ids if r in spec.by_req), "")
        name = names.get(key) or title or key
        out.append({"group": key, "name": name, "short": short_name(name), "pages": _pages(items)})
    return out


def _pages(screens: list[dict]) -> list[dict]:
    """메뉴 항목: 같은 테이블을 다루는 화면을 탭으로 묶는다. 화면이 하나면 그 이름, 여럿이면 '<업무 대상> 관리'."""
    groups: dict[str, list[dict]] = {}
    for s in screens:
        groups.setdefault(s["table"] or s["screen_id"], []).append(s)
    return [{"name": f"{items[0]['table_label']} 관리" if len(items) > 1 and items[0]["table_label"] else items[0]["name"],
             "screens": [s["screen_id"] for s in items]} for items in groups.values()]


def screens_md(spec: Spec, data: dict) -> str:
    """SCREENS.md: 화면 목록 문서 (고객·개발자가 화면 단위로 볼 때)."""
    out = [spec.head("SCREENS — 화면 목록", "기능 하나 = 화면(탭) 하나 = 작업 하나. 같은 업무 대상의 화면은 메뉴 하나로 묶는다."), ""]
    counts = Counter(s["pattern_label"] for s in data["screens"])
    menus = sum(len(m["pages"]) for m in data["menu"])
    out += [f"메뉴 {menus}개 · 화면 {len(data['screens'])}개 ({' · '.join(f'{k} {v}' for k, v in counts.most_common())}) · 자동 실행 {len(data['autos'])}개", ""]
    by_id = {s["screen_id"]: s for s in data["screens"]}
    for menu in data["menu"]:
        out += [f"## {menu['name']}", "", "| 메뉴 | 화면 | 이름 | 유형 | 쓰는 사람 | 주 테이블 | 버튼 | 작업 | 수용 기준 |", "|---|---|---|---|---|---|---|---|---|"]
        for page in menu["pages"]:
            for sid in page["screens"]:
                s = by_id[sid]
                buttons = [x for x, on in (("등록", s["create"]), ("수정", s["update"]), ("삭제", s["delete"])) if on] + [a["label"] for a in s["actions"]]
                out.append(f"| {page['name']} | {sid} | {s['name']} | {s['pattern_label']} | {', '.join(s['roles']) or '-'} | {s['table'] or '-'} | "
                           f"{', '.join(buttons) or '-'} | {s['task'] or '-'} | {s['ac_count']}개 |")
        out.append("")
    if data["autos"]:
        out += ["## 자동 실행 (화면 없음)", "", "| 기능 | 이름 | 이유 | 작업 |", "|---|---|---|---|"]
        out += [f"| {a['function_id']} | {a['name']} | {a['reason'] or '-'} | {a['task'] or '-'} |" for a in data["autos"]]
        out.append("")
    return "\n".join(out)
