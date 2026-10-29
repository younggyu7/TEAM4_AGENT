"""구조화 데이터 모델 정규화·관계 도출·정적 검증 (결정적)."""
from __future__ import annotations

import re
from collections import defaultdict

from app.schemas.design import Attribute, CheckRule, DataModel, Entity, Issue, Relation, RequiredWhen
from app.agents.flow.state import finding

PG_RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric", "authorization", "binary", "both",
    "case", "cast", "check", "collate", "collation", "column", "concurrently", "constraint", "create", "cross",
    "current_catalog", "current_date", "current_role", "current_schema", "current_time", "current_timestamp",
    "current_user", "default", "deferrable", "desc", "distinct", "do", "else", "end", "except", "false", "fetch", "for",
    "foreign", "freeze", "from", "full", "grant", "group", "having", "ilike", "in", "initially", "inner", "intersect",
    "into", "is", "isnull", "join", "lateral", "leading", "left", "like", "limit", "localtime", "localtimestamp",
    "natural", "not", "notnull", "null", "offset", "on", "only", "or", "order", "outer", "overlaps", "placing",
    "primary", "references", "returning", "right", "select", "session_user", "similar", "some", "symmetric", "system_user",
    "table", "tablesample", "then", "to", "trailing", "true", "union", "unique", "user", "using", "variadic", "verbose",
    "when", "where", "window", "with",
}
INTEGER_TYPES = {"bigint", "integer"}

# 설계 표준 (데이터 모델 Agent 프롬프트의 규칙과 같은 내용을 결정적으로 확인한다)
AUDIT_COLUMNS = (("created_at", "생성 일시"), ("updated_at", "수정 일시"))
TEXT_TYPES = {"varchar", "text"}
# 인증 정보 컬럼: 이름 조각(_로 나눈 단어)이나 한글명 끝말로 찾는다. 해시·암호화 표시가 있으면 평문이 아니다.
SECRET_TOKENS = {"password", "passwd", "pwd", "pw", "secret", "token", "otp", "pin", "apikey"}
SECRET_PHRASES = ("api_key", "auth_code", "verification_code", "verify_code", "reset_code", "otp_code",
                  "access_key", "secret_key", "private_key")
SECRET_LABEL_ENDINGS = ("비밀번호", "패스워드", "인증번호", "인증코드", "인증 코드", "토큰", "비밀키", "OTP")
# 해시·HMAC 검증값·암호문 표시 (짧은 인증코드는 비밀키 기반 검증값(HMAC)으로 저장한다)
PROTECTED_TOKENS = {"hash", "hashed", "digest", "enc", "encrypted", "cipher", "salt", "hmac", "mac", "verifier"}
# 인증 정보에 관한 부가 정보(유형·상태·일시·횟수 등)는 비밀이 아니다
META_TOKENS = {"type", "status", "method", "policy", "count", "expires", "expiry", "expired", "at", "date", "yn",
               "rule", "length", "hint", "channel", "attempt", "attempts", "sent", "id"}


def to_identifier(name: str) -> str:
    """이름을 PostgreSQL 식별자(snake_case, 63자 이하)로 바꾼다."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", (name or "").strip())
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
    if s and s[0].isdigit():
        s = f"t_{s}"
    return s[:63]


def normalize_model(model: DataModel, function_requirements: dict[str, list[str]]) -> tuple[DataModel, list[str]]:
    """이름 규칙·중복·FK 타입 등 기계적으로 고칠 수 있는 것은 여기서 고친다. 고친 내용은 notes로 남긴다."""
    notes: list[str] = []
    entities: list[Entity] = []
    seen: set[str] = set()
    for i, e in enumerate(model.entities, start=1):
        name = to_identifier(e.entity_id) or f"entity_{i}"
        if name != e.entity_id:
            notes.append(f"엔티티명 정규화: '{e.entity_id}' → {name}")
        if name in seen:
            notes.append(f"중복 엔티티 {name} 제거 (첫 정의 유지)")
            continue
        seen.add(name)
        attrs: list[Attribute] = []
        attr_names: set[str] = set()
        for j, a in enumerate(e.attributes, start=1):
            aname = to_identifier(a.name) or f"col_{j}"
            if aname in attr_names:
                notes.append(f"{name}: 중복 속성 {aname} 제거")
                continue
            attr_names.add(aname)
            length = (min(max(a.length, 1), 4000) if a.length else 255) if a.data_type == "varchar" else 0
            attrs.append(a.model_copy(update={
                "name": aname, "length": length, "references": to_identifier(a.references),
                "nullable": False if a.primary_key else a.nullable,
            }))
        attrs = _with_primary_key(name, e.label, attrs, notes)
        function_ids = [f for f in e.function_ids if f in function_requirements]
        requirement_ids = sorted({r for f in function_ids for r in function_requirements[f]})
        entities.append(e.model_copy(update={
            "entity_id": name, "attributes": attrs, "function_ids": function_ids, "requirement_ids": requirement_ids,
            "parents": [to_identifier(x) for x in e.parents],
        }))
    names = {e.entity_id for e in entities}
    for idx, e in enumerate(entities):
        parents = sorted({x for x in e.parents if x in names and x != e.entity_id})
        entities[idx] = _normalize_constraints(e.model_copy(update={"parents": parents}), notes)

    # 감사 컬럼(created_at, updated_at)이 없으면 표준대로 추가한다 (LLM은 출력하지 않으므로 보통 모든 엔티티에 추가된다)
    added = 0
    for idx, e in enumerate(entities):
        names = {a.name for a in e.attributes}
        missing = [Attribute(name=n, label=label, data_type="timestamp", nullable=False, description=label)
                   for n, label in AUDIT_COLUMNS if n not in names]
        if missing and e.attributes:
            added += 1
            entities[idx] = e.model_copy(update={"attributes": [*e.attributes, *missing]})
    if added:
        notes.append(f"감사 컬럼 추가: 엔티티 {added}개")

    # FK 컬럼 타입을 참조 엔티티 PK 타입에 맞춘다
    by_name = {e.entity_id: e for e in entities}
    for e in entities:
        for idx, a in enumerate(e.attributes):
            target = by_name.get(a.references)
            if not target or len(target.pk()) != 1:
                continue
            pk = target.pk()[0]
            if (a.data_type, a.length) != (pk.data_type, pk.length):
                notes.append(f"{e.entity_id}.{a.name}: FK 타입을 {target.entity_id}.{pk.name}({pk.data_type})에 맞춤")
                e.attributes[idx] = a.model_copy(update={"data_type": pk.data_type, "length": pk.length})
    normalized = DataModel(entities=entities)
    normalized.relations = derive_relations(normalized)
    return normalized, notes


def _with_primary_key(entity: str, label: str, attrs: list[Attribute], notes: list[str]) -> list[Attribute]:
    """기본키가 없으면 표준 대리키(<엔티티>_id bigint)를 맨 앞에 둔다. LLM은 기본키를 출력하지 않는다 (출력 토큰 절감).

    LLM이 설계한 속성이 하나도 없으면(속성 설계 실패) 추가하지 않는다. 빈 엔티티는 검증 오류로 다시 설계해야 하기 때문이다.
    같은 이름의 컬럼을 LLM이 냈으면 그 컬럼을 기본키로 쓴다.
    """
    if not attrs or any(a.primary_key for a in attrs):
        return attrs
    pk_name = f"{entity}_id"
    pk = Attribute(name=pk_name, label=f"{label or entity} ID", data_type="bigint", nullable=False, primary_key=True,
                   unique=True, description="대리키")
    same = next((a for a in attrs if a.name == pk_name and not a.references), None)
    if same:
        pk = same.model_copy(update={"data_type": "bigint", "length": 0, "nullable": False, "primary_key": True,
                                     "unique": True, "allowed_values": []})
    return [pk, *(a for a in attrs if a.name != pk_name or a.references)]


def _normalize_constraints(e: Entity, notes: list[str]) -> Entity:
    """여러 컬럼 제약의 이름을 식별자로 맞추고, 없는 컬럼·말이 안 되는 제약은 버린다.

    - unique_together: 서로 다른 컬럼 2개 이상
    - exactly_one_of: 외래키 2개 이상. 그중 하나만 채우므로 해당 FK는 NULL 허용으로 바꾼다
    - required_when: 대상 컬럼이 NULL 허용이고, 조건 컬럼 값이 허용값 안에 있을 때만
    - checks: 숫자와 비교하면 숫자 컬럼만, 다른 컬럼과 비교하면 같은 종류(숫자·날짜·문자, 문자는 <>만)끼리만
    """
    attrs = {a.name: a for a in e.attributes}

    def cols(group: list[str]) -> list[str]:
        out = []
        for c in (to_identifier(x) for x in group):
            if c in attrs and c not in out:
                out.append(c)
        return out

    unique = [g for g in (cols(g) for g in e.unique_together) if len(g) >= 2]
    one_of = [g for g in (cols(g) for g in e.exactly_one_of) if len(g) >= 2 and all(attrs[c].references for c in g)]
    for g in one_of:
        for c in g:
            if not attrs[c].nullable:
                notes.append(f"{e.entity_id}.{c}: 둘 중 하나만 채우는 FK라 NULL 허용으로 바꿈")
                attrs[c] = attrs[c].model_copy(update={"nullable": True})
    required = []
    for r in e.required_when:
        col, when = to_identifier(r.column), to_identifier(r.when_column)
        if col not in attrs or when not in attrs or col == when or not attrs[col].nullable:
            continue
        allowed = attrs[when].allowed_values
        values = [v for v in r.when_values if not allowed or v in allowed]
        if values:
            required.append(RequiredWhen(column=col, when_column=when, when_values=values))
    checks: list[CheckRule] = []
    for c in e.checks:
        rule = _check_rule(c, attrs)
        if rule and rule not in checks:
            checks.append(rule)
    dropped = (len(e.unique_together) - len(unique)) + (len(e.exactly_one_of) - len(one_of)) \
        + (len(e.required_when) - len(required)) + (len(e.checks) - len(checks))
    if dropped:
        notes.append(f"{e.entity_id}: 컬럼이 없거나 맞지 않는 제약 {dropped}개 제외")
    return e.model_copy(update={"attributes": [attrs[a.name] for a in e.attributes], "unique_together": unique,
                                "exactly_one_of": one_of, "required_when": required, "checks": checks})


NUMBER_TYPES = {"bigint", "integer", "decimal"}
TYPE_GROUPS = (NUMBER_TYPES, {"date", "timestamp"}, TEXT_TYPES)


def _check_rule(c: CheckRule, attrs: dict[str, Attribute]) -> CheckRule | None:
    """값 조건 하나를 검사해 식별자·숫자 표기를 맞춘다. 비교할 수 없는 조건이면 None."""
    col = to_identifier(c.column)
    if col not in attrs:
        return None
    kind = attrs[col].data_type
    other = to_identifier(c.value)
    if other in attrs and other != col:
        group = next((g for g in TYPE_GROUPS if kind in g), {kind})
        if attrs[other].data_type not in group or (kind in TEXT_TYPES and c.op != "<>"):
            return None
        return CheckRule(column=col, op=c.op, value=other)
    try:
        number = float(c.value.strip())
    except ValueError:
        return None
    if kind not in NUMBER_TYPES:
        return None
    return CheckRule(column=col, op=c.op, value=str(int(number)) if number.is_integer() else str(number))


def derive_relations(model: DataModel) -> list[Relation]:
    """FK 속성에서 엔티티 관계를 도출한다 (유일 FK나 PK 겸 FK는 1:1)."""
    relations = []
    for e in model.entities:
        sole_pk = e.pk()[0].name if len(e.pk()) == 1 else None
        for a in e.attributes:
            if not a.references or not model.entity(a.references):
                continue
            one_to_one = a.unique or a.name == sole_pk
            relations.append(Relation(
                relation_id=f"REL-{e.entity_id}-{a.name}", parent=a.references, child=e.entity_id,
                fk_attribute=a.name, cardinality="1:1" if one_to_one else "1:N", optional=a.nullable,
            ))
    return relations


def is_plaintext_secret(a: Attribute) -> bool:
    """비밀번호·인증코드·토큰처럼 대조만 하면 되는 인증 정보를 평문 문자열 컬럼으로 두었는지."""
    if a.data_type not in TEXT_TYPES or a.references or a.allowed_values or a.primary_key:
        return False
    tokens = set(a.name.split("_"))
    if tokens & PROTECTED_TOKENS or any(w in a.label for w in ("해시", "암호화", "검증값", "HMAC")):
        return False
    by_name = bool(tokens & SECRET_TOKENS or any(p in a.name for p in SECRET_PHRASES)) and not tokens & META_TOKENS
    return by_name or a.label.strip().endswith(SECRET_LABEL_ENDINGS)


def validate_model(model: DataModel | None) -> list[Issue]:
    """모델 정적 검증.

    - 구조 오류: 속성·PK 없음, FK 대상 없음, 복합 PK 참조
    - 설계 표준: 인증 정보 평문 컬럼은 오류(데이터 모델 Agent 수정), 기본키·외래키 명명 규칙과 예약어는 경고
    - 연결 의도: 카탈로그에서 정한 상위 엔티티를 참조하는 외래키가 없으면 경고
      (감사 컬럼·FK 타입·식별자 형식은 normalize_model이 자동으로 맞춘다)
    - 사용자가 고친 엔티티(user_edited)의 오류는 자동으로 고치지 않고 보고만 한다 (수정 담당 none, '직접 확인')
    """
    by = "inspect_result"
    if model is None or not model.entities:
        return [finding("MODEL_EMPTY", "ERROR", "데이터 모델에 엔티티가 없습니다.", "data_modeler", "*", by)]
    problems: dict[tuple[str, str], list[str]] = defaultdict(list)
    severity = {"MODEL_NO_ATTRIBUTES": "ERROR", "MODEL_NO_PK": "ERROR", "MODEL_FK_TARGET_MISSING": "ERROR",
                "MODEL_FK_COMPOSITE_TARGET": "ERROR", "MODEL_PLAINTEXT_SECRET": "ERROR",
                "MODEL_RESERVED_WORD": "WARNING", "MODEL_PK_STANDARD": "WARNING", "MODEL_FK_NAMING": "WARNING",
                "MODEL_PARENT_NOT_REFERENCED": "WARNING"}
    names = {e.entity_id for e in model.entities}
    for e in model.entities:
        if not e.attributes:
            problems[("MODEL_NO_ATTRIBUTES", e.entity_id)].append("속성이 없음")
            continue
        pk = e.pk()
        if not pk:
            problems[("MODEL_NO_PK", e.entity_id)].append("기본키가 없음")
        elif [(a.name, a.data_type) for a in pk] != [(f"{e.entity_id}_id", "bigint")]:
            problems[("MODEL_PK_STANDARD", e.entity_id)].append(
                f"기본키 {', '.join(f'{a.name}({a.data_type})' for a in pk)}가 표준(단일 대리키 {e.entity_id}_id bigint)과 다름")
        if e.entity_id in PG_RESERVED:
            problems[("MODEL_RESERVED_WORD", e.entity_id)].append(f"테이블명 {e.entity_id}는 예약어 (DDL에서 따옴표 처리)")
        for a in e.attributes:
            if a.name in PG_RESERVED:
                problems[("MODEL_RESERVED_WORD", e.entity_id)].append(f"컬럼명 {a.name}은 예약어 (DDL에서 따옴표 처리)")
            if is_plaintext_secret(a):
                problems[("MODEL_PLAINTEXT_SECRET", e.entity_id)].append(
                    f"{a.name}({a.label or a.name})이 인증 정보를 평문으로 저장함 → 단방향 해시 컬럼(예: {a.name}_hash) "
                    f"또는 복호화가 필요하면 암호화 컬럼(예: {a.name}_enc)으로 바꿀 것")
            if not a.references:
                continue
            if not a.name.endswith("_id"):
                problems[("MODEL_FK_NAMING", e.entity_id)].append(f"외래키 {a.name}이 _id로 끝나지 않음")
            if a.references not in names:
                problems[("MODEL_FK_TARGET_MISSING", e.entity_id)].append(
                    f"{a.name} → 존재하지 않는 엔티티 '{a.references}' 참조")
            elif len(model.entity(a.references).pk()) > 1:
                problems[("MODEL_FK_COMPOSITE_TARGET", e.entity_id)].append(
                    f"{a.name} → {a.references}의 기본키가 여러 컬럼임 (baseline은 단일 PK 참조만 지원)")
            # 기본키가 없는 대상(속성이 빈 엔티티)은 대상 쪽 오류(MODEL_NO_PK·NO_ATTRIBUTES)로 고친다
        referenced = {a.references for a in e.attributes if a.references}
        missing = [p for p in e.parents if p in names and p not in referenced]
        if missing:
            problems[("MODEL_PARENT_NOT_REFERENCED", e.entity_id)].append(
                f"카탈로그에서 정한 상위 엔티티 {', '.join(missing)}를 참조하는 외래키가 없음")
    edited = {e.entity_id for e in model.entities if e.user_edited}
    issues = []
    for (kind, entity), msgs in problems.items():
        sev = severity[kind]
        mine = sev == "ERROR" and entity not in edited
        note = USER_EDITED_NOTE if sev == "ERROR" and entity in edited else ""
        issues.append(finding(kind, sev, f"{entity}: " + "; ".join(msgs) + note,
                              "data_modeler" if mine else "none", entity, by))
    return issues


USER_EDITED_NOTE = " (사용자가 고친 엔티티라 자동으로 고치지 않음 — 직접 확인)"
