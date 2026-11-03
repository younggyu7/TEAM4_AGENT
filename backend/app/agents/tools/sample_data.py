"""시연용 예시 데이터: 데이터 모델에서 테이블마다 예시 행을 코드로 만든다 (LLM 없음, 난수·현재 시각 없음).

- 같은 데이터 모델이면 항상 같은 값이 나온다. 부모 테이블부터 채워 외래키가 항상 있는 행을 가리킨다.
- 허용값·길이·NOT NULL·UNIQUE·'둘 중 하나만'·값 조건(checks)을 지킨다 (schema.sql에 그대로 넣을 수 있어야 한다).
- 값은 schema.sql의 실제 모델(설계 표준을 쓰면 표준 이름·타입)로 만든다. 화면 목업은 mock-data.json으로, 실제 DB는 seed.sql로 같은 행을 쓴다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from app.schemas.data_model import Attribute, DataModel, Entity
from app.agents.tools.ddl import q
from app.agents.tools.dev_spec import Names
from app.agents.tools.screens import SECRET_COLUMN, label_column

ROWS = 6
BASE_DAY = date(2026, 3, 2)
PEOPLE = ("김민준", "이서연", "박지호", "최수아", "정도윤", "한지민")
PLACES = ("서울 중구 세종대로 110", "부산 해운대구 센텀로 45", "대전 유성구 대학로 99", "광주 서구 상무중앙로 7",
          "대구 수성구 달구벌대로 2300", "인천 연수구 송도과학로 32")
LATE_WORDS = ("end", "to_date", "until", "expire", "closed", "종료", "만료", "마감")
# 어떤 문서에나 쓰는 일반 값. 업무에 맞는 값은 프로젝트 파일 sample_words.json으로 더한다 (load_words, git 저장소에 넣지 않는다)
WORDS = (
    (("거래처", "업체", "공급", "협력"), ("(주)한빛상사", "대성산업(주)", "미래테크(주)", "세종물류(주)", "우리정보기술(주)", "동해건설(주)")),
    (("부서", "조직", "팀"), ("경영지원팀", "재무회계팀", "인사총무팀", "정보전략팀", "구매자재팀", "사업기획팀")),
    (("직급", "직위", "직책"), ("사원", "대리", "과장", "차장", "부장", "이사")),
    (("사유", "비고", "의견", "메모"), ("정기 처리", "업무 협의 완료", "담당자 확인 완료", "일정 변경 반영", "요청 내용 반영", "증빙 확인 완료")),
)


def load_words(text: str | None) -> tuple:
    """프로젝트 예시 값 파일(산출물 저장소의 프로젝트 파일 sample_words.json): [{"keys": ["프로젝트"], "words": ["…"]}].
    없으면 빈 값 (일반 값만 쓴다)."""
    if not text:
        return ()
    return tuple((tuple(x["keys"]), tuple(x["words"])) for x in json.loads(text) if x.get("keys") and x.get("words"))


def table_order(model: DataModel) -> list[Entity]:
    """부모가 먼저 오는 순서. 순환 참조는 들어온 순서로 끊는다 (그 외래키는 NULL 허용일 때만 비운다)."""
    by_id = {e.entity_id: e for e in model.entities}
    done: dict[str, Entity] = {}
    visiting: set[str] = set()

    def visit(e: Entity) -> None:
        if e.entity_id in done or e.entity_id in visiting:
            return
        visiting.add(e.entity_id)
        for a in e.attributes:
            if a.references and a.references in by_id and a.references != e.entity_id:
                visit(by_id[a.references])
        done[e.entity_id] = e

    for e in model.entities:
        visit(e)
    return list(done.values())


def _text(a: Attribute, i: int, entity: Entity, words: tuple = ()) -> str:
    label, name = a.label or a.name, a.name.lower()
    if SECRET_COLUMN.search(name):
        return f"demo-secret-{i + 1}"
    if "email" in name or "메일" in label:
        return f"user{i + 1}@example.com"
    if any(w in name for w in ("phone", "mobile", "tel")) or "전화" in label or "연락처" in label:
        return f"010-2000-{1000 + i}"
    if "address" in name or "주소" in label:
        return PLACES[i % len(PLACES)]
    if name.endswith(("_code", "_no", "_number")) or label.endswith(("코드", "번호")):
        prefix = "".join(p[0] for p in entity.entity_id.split("_")).upper()[:4]
        return f"{prefix}-{i + 1:04d}"
    if label.endswith(("성명", "이름")) or name in ("name", "user_name", "employee_name", "person_name"):
        return PEOPLE[i % len(PEOPLE)]
    for keys, values in (*words, *WORDS):
        if any(k in label for k in keys):
            return values[i % len(values)]
    return f"{entity.label or label} {chr(65 + i)}"


def _value(a: Attribute, i: int, entity: Entity, words: tuple = ()):
    """컬럼 값 하나 (외래키·기본키는 sample_rows가 정한다)."""
    late = any(w in a.name.lower() or w in (a.label or "") for w in LATE_WORDS)
    if a.allowed_values:   # 칸 길이를 넘는 허용값은 고르지 않는다 (스펙의 길이·허용값이 어긋난 문서도 넣을 수 있게)
        values = [v for v in a.allowed_values if not a.length or len(v) <= a.length] or a.allowed_values
        return values[i % len(values)]
    if a.data_type == "boolean":
        return i % 2 == 0
    if a.data_type in ("integer", "bigint"):
        if "year" in a.name or "연도" in (a.label or ""):
            return 2026 + i if a.unique else 2026
        return i + 1
    if a.data_type == "decimal":
        if "rate" in a.name or "율" in (a.label or ""):
            return round(1.5 + i * 0.5, 2)
        return (i + 1) * 1250000
    if a.data_type in ("date", "timestamp"):
        day = BASE_DAY + timedelta(days=7 * i + (30 if late else 0))
        return day.isoformat() if a.data_type == "date" else f"{day.isoformat()}T09:00:00+09:00"
    if a.data_type == "uuid":
        return f"00000000-0000-4000-8000-{i + 1:012d}"
    if a.data_type == "jsonb":
        return {}
    text = _text(a, i, entity, words)
    if a.length and len(text) > a.length:   # 유일해야 하는 값은 끝(번호)을 남긴다
        return text[-a.length:] if a.unique or a.name.endswith(("_code", "_no", "_number")) else text[:a.length]
    return text


def _key_of(entity: Entity | None) -> str:
    return entity.pk()[0].name if entity and entity.pk() else ""


def _satisfy(row: dict, entity: Entity, parents: dict[str, list]) -> None:
    """값 조건을 맞춘다: 비교 대상이 숫자면 값을 맞추고, 다른 컬럼이면 두 값이 어긋나지 않게 옮긴다."""
    columns = {a.name: a for a in entity.attributes}
    for rule in entity.checks:
        left = row.get(rule.column)
        if left is None:
            continue
        if rule.value in columns:
            right = row.get(rule.value)
            if right is None:
                continue
            if rule.op == "<>" and left == right:
                a = columns[rule.column]
                others = [v for v in parents.get(a.references, []) if v != right]
                if a.references and others:   # 본인 승인 금지처럼 같은 사람을 가리키면 안 되는 외래키
                    row[rule.column] = others[0]
            elif (rule.op in (">", ">=") and left < right) or (rule.op in ("<", "<=") and left > right) or (rule.op in (">", "<") and left == right):
                row[rule.column], row[rule.value] = right, left
                if rule.op in (">", "<") and left == right:
                    row[rule.column] = left + 1 if isinstance(left, int | float) else left
        else:
            try:
                limit = float(rule.value)
            except ValueError:
                continue
            bad = {">": left <= limit, ">=": left < limit, "<": left >= limit, "<=": left > limit, "<>": left == limit}[rule.op]
            if bad:
                row[rule.column] = limit + 1 if rule.op in (">", ">=", "<>") else limit - 1


def _reference(a: Attribute, i: int, entity: Entity, row: dict, rows: list[dict], out: dict[str, list[dict]], by_id: dict[str, Entity]):
    """외래키 값: 부모 행의 실제 기본키 (숫자가 아닌 기본키도 맞게). 아직 없는 부모(순환 참조)는 비운다."""
    parent = by_id.get(a.references)
    key = _key_of(parent)
    if a.references == entity.entity_id:
        if i:
            return rows[i - 1].get(key)
        return None if a.nullable else row.get(key, 1)
    ready = out.get(a.references)
    if ready and key:
        return ready[i % len(ready)].get(key)
    return None if a.nullable else i + 1


def sample_rows(model: DataModel, words: tuple = ()) -> dict[str, list[dict]]:
    """테이블 → 예시 행 (설계 이름, 부모 먼저)."""
    out: dict[str, list[dict]] = {}
    by_id = {e.entity_id: e for e in model.entities}
    for entity in table_order(model):
        rows = []
        for i in range(ROWS):
            row: dict = {}
            for a in entity.attributes:
                if a.references:
                    row[a.name] = _reference(a, i, entity, row, rows, out, by_id)
                elif a.primary_key:
                    row[a.name] = i + 1 if a.data_type in ("bigint", "integer") else _value(a, i, entity, words)
                else:
                    row[a.name] = _value(a, i, entity, words)
            for group in entity.exactly_one_of:   # 정확히 하나만 채운다 (조건부 필수가 정한 컬럼이 있으면 그 컬럼)
                needed = [r.column for r in entity.required_when if r.column in group and row.get(r.when_column) in r.when_values]
                keep = needed[0] if needed else group[i % len(group)]
                for name in group:
                    if name != keep:
                        row[name] = None
            parents = {a.references: [r.get(_key_of(by_id.get(a.references))) for r in out.get(a.references, [])] for a in entity.attributes if a.references}
            _satisfy(row, entity, parents)
            rows.append(row)
        out[entity.entity_id] = rows
    return out


def _sql_value(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'"
    return "'" + str(value).replace("'", "''") + "'"


def seed_sql(model: DataModel, rows: dict[str, list[dict]]) -> str:
    """seed.sql: 시연용 예시 행 (schema.sql 다음에 적용한다). model은 schema.sql의 실제 모델이고, 부모부터 넣는다."""
    out = ["-- 코드가 만든 시연용 예시 데이터 (운영 DB에 넣지 않는다)", "BEGIN;", ""]
    for entity in table_order(model):
        table = q(entity.entity_id)
        columns = [a.name for a in entity.attributes]
        physical = ", ".join(q(c) for c in columns)
        for row in rows[entity.entity_id]:
            out.append(f"INSERT INTO {table} ({physical}) VALUES ({', '.join(_sql_value(row[c]) for c in columns)});")
        identity = [a for a in entity.pk() if a.data_type in ("bigint", "integer") and not a.references]
        if identity:
            out.append(f"ALTER TABLE {table} ALTER COLUMN {q(identity[0].name)} RESTART WITH {ROWS + 1};")
        out.append("")
    return "\n".join([*out, "COMMIT;", ""])


def mock_data(model: DataModel, names: Names, rows: dict[str, list[dict]]) -> dict:
    """화면 목업용: rows는 schema.sql 이름이다. 비밀 컬럼은 설계 이름으로 찾아 빼고, 표준이 Y/N으로 바꾼 값은 API처럼 참·거짓으로 준다.

    keys는 테이블별 기본키 컬럼, labels는 행을 알아볼 컬럼이다 (서버의 선택 목록과 같게)."""
    out, keys, labels = {}, {}, {}
    for entity in model.entities:
        visible = [a for a in entity.attributes if not SECRET_COLUMN.search(a.name)]
        table = names.table(entity.entity_id)
        columns = [(names.column(entity.entity_id, a.name), a.data_type == "boolean") for a in visible]
        out[table] = [{c: (r[c] == "Y" if yes_no and isinstance(r[c], str) else r[c]) for c, yes_no in columns} for r in rows[table]]
        if entity.pk():
            keys[table] = names.column(entity.entity_id, entity.pk()[0].name)
            labels[table] = names.column(entity.entity_id, label_column(entity))
    return {"keys": keys, "labels": labels, "rows": out}
