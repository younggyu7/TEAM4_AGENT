"""DataModel → DDL. PostgreSQL이 기본 대상이고, SQLite는 PostgreSQL 미설정 시 대체 검증용이다.
MySQL 8은 개발 단계(DB 스택 mysql)의 db/schema.sql에만 쓴다 (설계 산출물 schema.sql은 PostgreSQL 그대로)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from app.schemas.data_model import Attribute, DataModel, Entity
from app.agents.tools.model_validator import INTEGER_TYPES, PG_RESERVED

Dialect = Literal["postgresql", "sqlite", "mysql"]

PG_TYPES = {"bigint": "BIGINT", "integer": "INTEGER", "decimal": "NUMERIC(18,2)", "varchar": "VARCHAR({n})",
            "text": "TEXT", "boolean": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMPTZ", "jsonb": "JSONB",
            "uuid": "UUID"}
# MySQL 8: 참·거짓은 BOOLEAN(=TINYINT(1)), 시각은 시간대 없는 DATETIME(6). 키에 쓰는 TEXT는 VARCHAR(255) (_mysql_types)
MYSQL_TYPES = {"bigint": "BIGINT", "integer": "INT", "decimal": "DECIMAL(18,2)", "varchar": "VARCHAR({n})", "text": "TEXT",
               "boolean": "BOOLEAN", "date": "DATE", "timestamp": "DATETIME(6)", "jsonb": "JSON", "uuid": "CHAR(36)"}
MYSQL_KEY_BYTES = 3072   # InnoDB 키 하나의 길이 한도
MYSQL_ROW_CHARS = 15000   # 한 행 VARCHAR 글자 합 한도 (utf8mb4 4바이트 × 글자 ≤ 65,535바이트). 넘으면 긴 칸부터 TEXT
# MySQL 8 예약어 (식별자로 쓰면 감싸야 한다)
MYSQL_RESERVED = set("""
accessible add all alter analyze and as asc asensitive before between bigint binary blob both by call cascade case change char
character check collate column condition constraint continue convert create cross cube cume_dist current_date current_time
current_timestamp current_user cursor database databases day_hour day_microsecond day_minute day_second dec decimal declare default
delayed delete dense_rank desc describe deterministic distinct distinctrow div double drop dual each else elseif empty enclosed escaped
except exists exit explain false fetch first_value float float4 float8 for force foreign from fulltext function generated get grant
group grouping groups having high_priority hour_microsecond hour_minute hour_second if ignore in index infile inner inout insensitive
insert int int1 int2 int3 int4 int8 integer intersect interval into io_after_gtids io_before_gtids is iterate join json_table key keys
kill lag last_value lateral lead leading leave left like limit linear lines load localtime localtimestamp lock long longblob longtext
loop low_priority manual master_bind master_ssl_verify_server_cert match maxvalue mediumblob mediumint mediumtext middleint
minute_microsecond minute_second mod modifies natural not no_write_to_binlog nth_value ntile null numeric of on optimize
optimizer_costs option optionally or order out outer outfile over parallel partition percent_rank precision primary procedure purge
qualify range rank read reads read_write real recursive references regexp release rename repeat replace require resignal restrict
return revoke right rlike row rows row_number schema schemas second_microsecond select sensitive separator set show signal smallint
spatial specific sql sqlexception sqlstate sqlwarning sql_big_result sql_calc_found_rows sql_small_result ssl starting stored
straight_join system table terminated then tinyblob tinyint tinytext to trailing trigger true undo union unique unlock unsigned update
usage use using utc_date utc_time utc_timestamp values varbinary varchar varcharacter varying virtual when where while window with
write xor year_month zerofill""".split())
SQLITE_TYPES = {"bigint": "INTEGER", "integer": "INTEGER", "decimal": "NUMERIC", "varchar": "TEXT", "text": "TEXT",
                "boolean": "INTEGER", "date": "TEXT", "timestamp": "TEXT", "jsonb": "TEXT", "uuid": "TEXT"}


@dataclass
class Statement:
    """DDL 문장 하나와 대상 테이블·종류 (실패 시 수정할 엔티티를 정하는 데 쓴다)."""
    sql: str
    table: str
    kind: Literal["table", "comment", "fk", "index"]
    depends_on: str = ""          # FK 대상 테이블


def q(name: str, dialect: Dialect = "postgresql") -> str:
    """예약어인 식별자만 감싼다 (PostgreSQL·SQLite는 큰따옴표, MySQL은 백틱)."""
    if dialect == "mysql":
        return f"`{name}`" if mysql_reserved(name) else name
    return f'"{name}"' if name in PG_RESERVED else name


def mysql_reserved(name: str) -> bool:
    """MySQL에서 감쌀 이름: MySQL 8 예약어 + PostgreSQL 예약어 (설계는 PostgreSQL 기준이라 함께 감싸도 해가 없다)."""
    return name.lower() in MYSQL_RESERVED or name.lower() in PG_RESERVED


def _lit(text: str, dialect: Dialect = "postgresql") -> str:
    """SQL 문자열 리터럴로 만든다 (작은따옴표 이스케이프. MySQL은 역슬래시도 이스케이프 문자다)."""
    text = text or ""
    if dialect == "mysql":
        text = text.replace("\\", "\\\\")
    return "'" + text.replace("'", "''") + "'"


def _cname(prefix: str, *parts: str) -> str:
    """제약·인덱스 이름을 만든다 (63자를 넘으면 해시로 줄임)."""
    name = "_".join((prefix, *parts))
    if len(name) <= 63:
        return name
    return f"{name[:54]}_{hashlib.md5(name.encode()).hexdigest()[:8]}"


def _col_type(a: Attribute, dialect: Dialect) -> str:
    """논리 타입을 방언별 컬럼 타입으로 바꾼다."""
    table = PG_TYPES if dialect == "postgresql" else SQLITE_TYPES
    return table[a.data_type].format(n=a.length or 255)


def _mysql_types(e: Entity) -> dict[str, str]:
    """MySQL 컬럼 타입. TEXT는 키(기본키·UNIQUE·외래키)에 쓸 수 없어 VARCHAR(255)로 바꾸고,
    VARCHAR 합이 한 행 한도를 넘으면 키가 아닌 긴 칸부터 TEXT로 바꾼다."""
    keyed = {a.name for a in e.attributes if a.primary_key or a.unique or a.references} | {c for g in e.unique_together for c in g}
    out = {a.name: "VARCHAR(255)" if a.data_type == "text" and a.name in keyed else MYSQL_TYPES[a.data_type].format(n=a.length or 255)
           for a in e.attributes}
    sizes = {n: int(t[8:-1]) for n, t in out.items() if t.startswith("VARCHAR(")}
    for name in sorted((n for n in sizes if n not in keyed), key=lambda n: -sizes[n]):
        if sum(sizes.values()) <= MYSQL_ROW_CHARS:
            break
        out[name] = "TEXT"
        del sizes[name]
    return out


def _key(e: Entity, cols: list[str], dialect: Dialect) -> str:
    """UNIQUE 키의 컬럼 목록. MySQL은 키 길이가 3,072바이트까지라(utf8mb4 글자당 4바이트) 넘으면 VARCHAR에 앞부분 길이를 붙인다
    (값의 길이는 그대로, 유일 검사는 앞부분까지)."""
    if dialect != "mysql":
        return ", ".join(q(c, dialect) for c in cols)
    types = _mysql_types(e)
    chars = {c: int(types[c][8:-1]) for c in cols if types.get(c, "").startswith("VARCHAR(")}
    other = sum(8 for c in cols if c not in chars)
    if sum(chars.values()) * 4 + other <= MYSQL_KEY_BYTES:
        return ", ".join(q(c, dialect) for c in cols)
    room = (MYSQL_KEY_BYTES - other) // 4 // max(len(chars), 1)
    return ", ".join(q(c, dialect) + (f"({room})" if c in chars and chars[c] > room else "") for c in cols)


def _comment(label: str, description: str) -> str:
    """COMMENT 문구: 라벨과 설명이 같으면 한 번만 쓴다."""
    label, description = (label or "").strip(), (description or "").strip()
    return label if not description or description == label else f"{label} - {description}".strip(" -")


def _parent_pk(model: DataModel, a: Attribute) -> Attribute | None:
    """FK가 가리키는 부모 엔티티의 단일 PK 속성 (없거나 복합 PK면 None)."""
    parent = model.entity(a.references) if a.references else None
    return parent.pk()[0] if parent and len(parent.pk()) == 1 else None


def _create_table(model: DataModel, e: Entity, dialect: Dialect) -> str:
    """CREATE TABLE 문을 만든다 (PK·UNIQUE·CHECK, SQLite는 FK도 표 안에). MySQL은 주석을 컬럼·테이블 옵션으로 함께 쓴다."""
    qd, lit = (lambda n: q(n, dialect)), (lambda t: _lit(t, dialect))   # noqa: E731
    pk = e.pk()
    identity_col = pk[0].name if (dialect in ("postgresql", "mysql") and len(pk) == 1 and pk[0].data_type in INTEGER_TYPES
                                  and not pk[0].references) else None
    mysql = _mysql_types(e) if dialect == "mysql" else {}
    lines = []
    for a in e.attributes:
        line = f"    {qd(a.name)} {mysql.get(a.name) or _col_type(a, dialect)}"
        if a.name == identity_col and dialect == "postgresql":
            line += " GENERATED BY DEFAULT AS IDENTITY"
        if not a.nullable:
            line += " NOT NULL"
        if a.name == identity_col and dialect == "mysql":
            line += " AUTO_INCREMENT"
        if dialect == "mysql" and (text := _comment(a.label, a.description)):
            line += f" COMMENT {lit(text[:1024])}"
        lines.append(line)
    if pk:
        lines.append(f"    CONSTRAINT {_cname('pk', e.entity_id)} PRIMARY KEY ({', '.join(qd(a.name) for a in pk)})")
    for a in e.attributes:
        if a.unique and not a.primary_key:
            lines.append(f"    CONSTRAINT {_cname('uq', e.entity_id, a.name)} UNIQUE ({_key(e, [a.name], dialect)})")
        if a.allowed_values and a.data_type in {"varchar", "text"}:
            values = ", ".join(lit(v) for v in a.allowed_values)
            lines.append(f"    CONSTRAINT {_cname('ck', e.entity_id, a.name)} CHECK ({qd(a.name)} IN ({values}))")
        if dialect == "sqlite" and (ppk := _parent_pk(model, a)):
            lines.append(f"    CONSTRAINT {_cname('fk', e.entity_id, a.name)} FOREIGN KEY ({qd(a.name)}) "
                         f"REFERENCES {qd(a.references)} ({qd(ppk.name)})")
    lines = _distinct_constraints(lines + _group_constraints(e, dialect))
    tail, head = ")", ""
    if dialect == "mysql":
        text = _comment(e.label, e.description)
        tail += " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" + (f" COMMENT={lit(text[:2048])}" if text else "")
        # MySQL CHECK는 AUTO_INCREMENT 컬럼을 가리킬 수 없다 (오류 3818): 그 조건은 빼고 주석으로 남긴다 (앱·업무 로직이 지킨다)
        auto = re.compile(rf"CHECK \(.*[\s(`]{re.escape(identity_col)}[\s)`]") if identity_col else None
        dropped = [x.strip() for x in lines if auto and auto.search(x)]
        lines = [x for x in lines if x.strip() not in dropped]
        head = "".join(f"-- MySQL에서 뺀 제약 (자동 번호 컬럼을 가리키는 CHECK): {x}\n" for x in dropped)
    return f"{head}CREATE TABLE {qd(e.entity_id)} (\n" + ",\n".join(lines) + f"\n{tail};"


def _distinct_constraints(lines: list[str]) -> list[str]:
    """한 테이블 안의 제약 이름을 겹치지 않게 한다 (같은 컬럼에 조건부 필수가 둘이면 이름이 같아 DB가 거부한다).
    내용까지 같은 제약은 하나만 두고, 이름만 같으면 뒤의 것에 _2, _3을 붙인다 (겹치지 않는 이름은 그대로)."""
    out, names, seen = [], set(), set()
    for line in lines:
        m = re.match(r"(\s*CONSTRAINT )(\S+)( .*)", line)
        if not m:
            out.append(line)
            continue
        if line in seen:
            continue
        seen.add(line)
        name, n = m[2], 2
        while name in names:
            suffix = f"_{n}"
            name, n = m[2][:63 - len(suffix)] + suffix, n + 1
        names.add(name)
        out.append(m[1] + name + m[3])
    return out


def _group_constraints(e: Entity, dialect: Dialect = "postgresql") -> list[str]:
    """여러 컬럼 제약: 복합 UNIQUE, 정확히 하나만 채우는 FK, 조건부 필수, 값 조건 (PostgreSQL·SQLite·MySQL 8.0.16+ 공통 문법).

    값 조건은 비교 대상이 NULL이면 통과한다 (SQL CHECK 규칙: 선택 컬럼은 값이 있을 때만 검사된다).
    """
    qd, lit = (lambda n: q(n, dialect)), (lambda t: _lit(t, dialect))   # noqa: E731
    lines = []
    columns = {a.name for a in e.attributes}
    for g in e.unique_together:
        lines.append(f"    CONSTRAINT {_cname('uq', e.entity_id, *g)} UNIQUE ({_key(e, g, dialect)})")
    for g in e.exactly_one_of:
        count = " + ".join(f"CASE WHEN {qd(c)} IS NOT NULL THEN 1 ELSE 0 END" for c in g)
        lines.append(f"    CONSTRAINT {_cname('ck', e.entity_id, 'one', *g)} CHECK (({count}) = 1)")
    for r in e.required_when:
        values = ", ".join(lit(v) for v in r.when_values)
        lines.append(f"    CONSTRAINT {_cname('ck', e.entity_id, r.column, 'req')} "
                     f"CHECK ({qd(r.when_column)} NOT IN ({values}) OR {qd(r.column)} IS NOT NULL)")
    for c in e.checks:
        right = qd(c.value) if c.value in columns else c.value   # 다른 컬럼 또는 숫자 (정규화에서 확인함)
        name = _cname("ck", e.entity_id, c.column, OP_NAMES[c.op], re.sub(r"\W", "_", c.value))
        lines.append(f"    CONSTRAINT {name} CHECK ({qd(c.column)} {c.op} {right})")
    return lines


OP_NAMES = {">": "gt", ">=": "ge", "<": "lt", "<=": "le", "<>": "ne"}


def render_statements(model: DataModel, dialect: Dialect = "postgresql") -> list[Statement]:
    """모델 전체의 DDL 문장 목록. PostgreSQL은 테이블 → 주석 → FK·인덱스 순서."""
    stmts: list[Statement] = []
    for e in model.entities:
        if not e.attributes:
            continue
        stmts.append(Statement(_create_table(model, e, dialect), e.entity_id, "table"))
        if dialect == "postgresql":
            if text := _comment(e.label, e.description):
                stmts.append(Statement(f"COMMENT ON TABLE {q(e.entity_id)} IS {_lit(text)};", e.entity_id, "comment"))
            for a in e.attributes:
                if text := _comment(a.label, a.description):
                    stmts.append(Statement(f"COMMENT ON COLUMN {q(e.entity_id)}.{q(a.name)} IS {_lit(text)};",
                                           e.entity_id, "comment"))
    if dialect == "postgresql":
        for e in model.entities:
            for a in e.attributes:
                ppk = _parent_pk(model, a)
                if not ppk:
                    continue
                stmts.append(Statement(
                    f"ALTER TABLE {q(e.entity_id)} ADD CONSTRAINT {_cname('fk', e.entity_id, a.name)} "
                    f"FOREIGN KEY ({q(a.name)}) REFERENCES {q(a.references)} ({q(ppk.name)});",
                    e.entity_id, "fk", depends_on=a.references))
                if not a.primary_key and not a.unique:
                    stmts.append(Statement(
                        f"CREATE INDEX {_cname('ix', e.entity_id, a.name)} ON {q(e.entity_id)} ({q(a.name)});",
                        e.entity_id, "index"))
    if dialect == "mysql":   # 인덱스를 먼저 만든다: 외래 키가 같은 컬럼에 인덱스를 또 만들지 않게
        for e in model.entities:
            for a in e.attributes:
                ppk = _parent_pk(model, a)
                if not ppk:
                    continue
                table, column = q(e.entity_id, dialect), q(a.name, dialect)
                if not a.primary_key and not a.unique:
                    stmts.append(Statement(f"CREATE INDEX {_cname('ix', e.entity_id, a.name)} ON {table} ({column});", e.entity_id, "index"))
                stmts.append(Statement(
                    f"ALTER TABLE {table} ADD CONSTRAINT {_cname('fk', e.entity_id, a.name)} "
                    f"FOREIGN KEY ({column}) REFERENCES {q(a.references, dialect)} ({q(ppk.name, dialect)});",
                    e.entity_id, "fk", depends_on=a.references))
    return stmts


def render_script(model: DataModel, design_version: str, dialect: Dialect = "postgresql") -> str:
    """schema.sql 파일 내용 (머리 주석 + 전체 DDL). MySQL은 글자 집합을 먼저 정한다 (초기화 스크립트의 클라이언트 설정과 무관하게)."""
    header = [
        f"-- Generated by req_agent | design_version=v{design_version} | dialect={dialect}",
        f"-- tables={len(model.entities)} relations={len(model.relations)}",
        "-- 같은 구조화 데이터 모델(data_model.json)에서 ERD와 함께 생성됨",
        *(["SET NAMES utf8mb4;"] if dialect == "mysql" else []),
        "",
    ]
    return "\n".join(header) + "\n\n".join(s.sql for s in render_statements(model, dialect)) + "\n"
