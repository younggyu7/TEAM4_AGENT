"""공통 DB 처리. 테이블·컬럼·타입 이름은 코드가 만든 상수(Table, Col)에서만 오고, 값은 모두 바인딩한다.

업무 로직에서 직접 쿼리가 필요하면 query·execute를 쓴다 (SQLAlchemy text의 :이름 바인딩).
DB 방언에 따라 다른 SQL은 이 파일에만 둔다: PostgreSQL(DB_URL=postgresql://…)과 MySQL 8(DB_URL=mysql+pymysql://…).
MySQL은 연결마다 sql_mode ANSI를 켠다: 이름은 큰따옴표, ||는 문자열 잇기라 PostgreSQL과 같은 SQL 모양을 쓴다.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from app.common.errors import ApiError

DEFAULT_SIZE = 20
MAX_SIZE = 200
NOW = object()   # 값 자리에 넣으면 현재 시각이 들어간다


@dataclass(frozen=True)
class AutoNo:
    """값 자리에 넣으면 날짜와 임의 문자로 만든 기본 번호가 들어간다. 실제 채번 규칙은 업무 로직에서 정한다."""
    length: int


def auto_no(length: int) -> AutoNo:
    return AutoNo(length)


@dataclass(frozen=True)
class Col:
    """API 필드 ↔ DB 컬럼. type은 PostgreSQL 타입 이름이고 코드가 만든 상수에서만 온다."""
    field: str
    column: str
    type: str


@dataclass(frozen=True)
class Table:
    """테이블 정보. created·updated는 입력·수정 때 현재 시각을 넣을 컬럼이고, joins는 조회할 때 붙일 부모 테이블이다."""
    name: str
    pk: str
    pk_type: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    joins: str = ""

    def with_joins(self, joins: str) -> "Table":
        return replace(self, joins=joins)

    @property
    def source(self) -> str:
        return f"{self.name} {self.joins}" if self.joins else self.name

    @property
    def key(self) -> str:
        return f"{self.name}.{self.pk}"


def values(*pairs: Any) -> dict:
    """이름, 값, 이름, 값 … → 순서 있는 dict (값이 None이어도 된다)."""
    return {pairs[i]: pairs[i + 1] for i in range(0, len(pairs) - 1, 2)}


_engine: Engine | None = None
# ANSI: 이름은 큰따옴표, ||는 문자열 잇기. STRICT: 맞지 않는 값은 잘라 넣지 않고 오류 (Spring 틀의 application.yml과 같다)
MYSQL_MODE = "SET SESSION sql_mode = 'ANSI,STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO'"


def engine() -> Engine:
    """DB_URL: postgresql://app:app@db:5432/app (psycopg) 또는 mysql+pymysql://app:app@db:3306/app?charset=utf8mb4."""
    global _engine
    if _engine is None:
        url = os.getenv("DB_URL", "postgresql://app:app@localhost:5433/app")
        for plain, driver in (("postgresql://", "postgresql+psycopg://"), ("mysql://", "mysql+pymysql://")):
            if url.startswith(plain):
                url = driver + url[len(plain):]
        _engine = create_engine(url, pool_pre_ping=True)
        if _engine.dialect.name == "mysql":
            @event.listens_for(_engine, "connect")
            def _ansi(conn, _record):
                with conn.cursor() as cursor:
                    cursor.execute(MYSQL_MODE)
    return _engine


def mysql() -> bool:
    return engine().dialect.name == "mysql"


def as_text(expr: str) -> str:
    """값을 글자로 읽는 SQL (MySQL의 CAST 타입 이름은 char)."""
    return f"CAST({expr} AS {'char' if mysql() else 'text'})"


@contextmanager
def transaction() -> Iterator["Db"]:
    """요청 하나 = 트랜잭션 하나: 업무 로직·자동 처리까지 한 묶음, 예외가 나면 모두 되돌린다."""
    with engine().begin() as conn:
        yield Db(conn)


def _bind(name: str, kind: str) -> str:
    """PostgreSQL은 컬럼 타입으로 바꿔 넣는다. MySQL은 값을 컬럼 타입에 맞춰 넣고 비교하므로 JSON만 감싼다."""
    if mysql():
        return f"CAST(:{name} AS JSON)" if kind == "jsonb" else f":{name}"
    return f"CAST(:{name} AS {'varchar' if kind == 'yn' else kind})"


def _select_list(t: Table, out: list[Col]) -> str:
    """응답 컬럼 목록. 응답에 컬럼이 하나도 없으면 기본키만 읽는다 (빈 SELECT 목록은 SQL 오류다)."""
    parts = []
    for c in out:
        column = as_text(c.column) if c.type == "jsonb" else f"({c.column} = 'Y')" if c.type == "yn" else c.column
        parts.append(f'{column} AS "{c.field}"')
    return ", ".join(parts) if parts else f'{t.key} AS "id"'


def _clean(value: Any) -> Any:
    """화면이 비워 보낸 값은 NULL로 저장한다. 묶음 값은 JSON 글자로."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return None if value == "" else value


def _json_text(value: str) -> str:
    """화면에서 글자로 입력한 값도 jsonb에 들어가게 한다: JSON이 아니면 JSON 문자열로 저장한다."""
    try:
        json.loads(value)
        return value
    except ValueError:
        return json.dumps(value, ensure_ascii=False)


def _to_column(c: Col, value: Any) -> Any:
    """표준이 boolean을 Y/N 글자로 바꾼 컬럼: API는 참·거짓, DB는 Y/N이다."""
    cleaned = _clean(value)
    if c.type == "yn" and cleaned is not None:
        return "Y" if str(cleaned).lower() == "true" or cleaned == "Y" else "N"
    if c.type == "boolean" and isinstance(cleaned, str) and mysql():   # 조회 조건 ?done=true: MySQL은 'true'를 0으로 본다
        return cleaned.lower() in ("true", "1", "y")
    if c.type == "jsonb" and isinstance(cleaned, str):
        return _json_text(cleaned)
    return cleaned


def _bools(rows: list[dict], out: list[Col]) -> list[dict]:
    """MySQL은 참·거짓을 0·1로 돌려준다: API는 참·거짓이다 (PostgreSQL은 그대로)."""
    names = [c.field for c in out if c.type in ("boolean", "yn")]
    if names and mysql():
        for row in rows:
            for name in names:
                if row.get(name) is not None:
                    row[name] = bool(row[name])
    return rows


def _int_of(value: Any, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        return int(str(value))
    except ValueError:
        raise ApiError(400, "VALIDATION_FAILED", f"숫자가 아닙니다: {value}") from None


class Db:
    def __init__(self, conn: Connection):
        self.conn = conn
        self._n = 0

    def _param(self, params: dict, value: Any) -> str:
        """값 자리 이름 (필드 이름은 바인딩 이름으로 쓰지 않는다)."""
        self._n += 1
        name = f"p{self._n}"
        params[name] = value
        return name

    def _rows(self, sql: str, params: dict) -> list[dict]:
        return [dict(r._mapping) for r in self.conn.execute(text(sql), params)]

    def _where(self, filters: list[Col], data: dict, params: dict) -> str:
        parts = []
        for c in filters:
            value = data.get(c.field)
            if value is None or value == "":
                continue
            name = self._param(params, _to_column(c, value))
            parts.append(f"{c.column} LIKE '%' || :{name} || '%'" if c.type in ("varchar", "text") else f"{c.column} = {_bind(name, c.type)}")
        return " WHERE " + " AND ".join(parts) if parts else ""

    def page(self, t: Table, out: list[Col], filters: list[Col], data: dict) -> dict:
        """목록 조회: 응답 {items, page, size, total}"""
        page = max(_int_of(data.get("page"), 0), 0)
        size = min(max(_int_of(data.get("size"), DEFAULT_SIZE), 1), MAX_SIZE)
        params: dict = {}
        where = self._where(filters, data, params)
        total = self.conn.execute(text(f"SELECT count(*) FROM {t.source}{where}"), params).scalar()
        params.update(__limit=size, __offset=page * size)
        items = _bools(self._rows(f'SELECT {t.key} AS "__pk", {_select_list(t, out)} FROM {t.source}{where} ORDER BY {t.key}'
                                  " LIMIT :__limit OFFSET :__offset", params), out)
        return {"items": items, "page": page, "size": size, "total": total}

    def first(self, t: Table, out: list[Col], filters: list[Col], data: dict) -> dict:
        """조건 조회 한 건 (요약 화면)"""
        params: dict = {}
        where = self._where(filters, data, params)
        rows = _bools(self._rows(f"SELECT {_select_list(t, out)} FROM {t.source}{where} ORDER BY {t.key} LIMIT 1", params), out)
        return rows[0] if rows else {}

    def one(self, t: Table, out: list[Col], id: Any) -> dict:
        rows = _bools(self._rows(f"SELECT {_select_list(t, out)} FROM {t.source} WHERE {t.key} = {_bind('__id', t.pk_type)}", {"__id": id}), out)
        if not rows:
            raise ApiError.not_found()
        return rows[0]

    def insert(self, t: Table, cols: list[Col], out: list[Col], data: dict, defaults: dict, operation: str = "", *needed: str) -> dict:
        """needed: 코드로 값을 정할 수 없는 필수 컬럼. 업무 로직이 채우지 않으면 구현 전이다 (DB 제약 오류로 보여 주지 않는다)."""
        for name in needed:
            if _clean(data.get(name)) is None:
                raise ApiError.not_implemented(f"{operation} ({name})")
        columns, marks, params, given = [], [], {}, {}
        for c in cols:
            if c.field in data:
                columns.append(c.column)
                given[c.column] = _to_column(c, data[c.field])
                marks.append(_bind(self._param(params, given[c.column]), c.type))
        for column, value in defaults.items():
            if column in columns:
                continue
            columns.append(column)
            if value is NOW:
                marks.append("now()")
            elif isinstance(value, AutoNo) and mysql():
                marks.append(f"left(concat(date_format(now(), '%y%m%d'), '-', substr(md5(rand()), 1, 6)), {value.length})")
            elif isinstance(value, AutoNo):
                marks.append(f"left(to_char(now(), 'YYMMDD') || '-' || substr(md5(CAST(random() AS text)), 1, 6), {value.length})")
            else:
                marks.append(":" + self._param(params, value))
        for column in t.created:
            columns.append(column)
            marks.append("now()")
        sql = f"INSERT INTO {t.name} ({', '.join(columns)}) VALUES ({', '.join(marks)})"
        if not mysql():
            return self.one(t, out, self.conn.execute(text(sql + f" RETURNING {t.pk}"), params).scalar())
        # MySQL에는 RETURNING이 없다: 자동 번호 키는 lastrowid, 키를 요청 값·기본값으로 정한 테이블은 그 값으로 다시 읽는다
        new_id = self.conn.execute(text(sql), params).lastrowid or None
        if new_id is None:
            new_id = given.get(t.pk, defaults.get(t.pk))
        return self.one(t, out, new_id)

    def _stamps(self, cols: list[Col], vals: dict, params: dict) -> list[str]:
        """함께 남기는 값의 SET 조각 (처리자·처리 시각). vals는 필드 이름 → 값, NOW면 현재 시각이다."""
        out = []
        for c in cols:
            value = vals.get(c.field)
            out.append(f"{c.column} = now()" if value is NOW else f"{c.column} = {_bind(self._param(params, _to_column(c, value)), c.type)}")
        return out

    def update(self, t: Table, cols: list[Col], out: list[Col], id: Any, data: dict, stamp: list[Col] = (), vals: dict | None = None) -> dict:
        """수정 (+ 함께 남기는 값: 처리자·처리 시각)"""
        params: dict = {"__id": id}
        sets = [f"{c.column} = {_bind(self._param(params, _to_column(c, data[c.field])), c.type)}" for c in cols if c.field in data]
        sets += self._stamps(list(stamp), vals or {}, params)
        sets += [f"{column} = now()" for column in t.updated]
        if sets:
            count = self.conn.execute(text(f"UPDATE {t.name} SET {', '.join(sets)} WHERE {t.pk} = {_bind('__id', t.pk_type)}"), params).rowcount
            if count == 0:
                raise ApiError.not_found()
        return self.one(t, out, id)

    def _state_sets(self, t: Table, state: str, stamp: list[Col], vals: dict, params: dict) -> str:
        """상태를 바꾸는 SET 절: 상태, 함께 남기는 값, 수정 시각"""
        return ", ".join([state, *self._stamps(list(stamp), vals, params), *[f"{column} = now()" for column in t.updated]])

    def set(self, t: Table, column: str, value: str, id: Any, out: list[Col], stamp: list[Col] = (), vals: dict | None = None) -> dict:
        """상태 값 바꾸기 (승인·반려 같은 처리의 기본 동작) + 처리자·처리 시각"""
        params: dict = {"__id": id}
        state = f"{column} = :{self._param(params, value)}"
        count = self.conn.execute(text(f"UPDATE {t.name} SET {self._state_sets(t, state, stamp, vals or {}, params)}"
                                       f" WHERE {t.pk} = {_bind('__id', t.pk_type)}"), params).rowcount
        if count == 0:
            raise ApiError.not_found()
        return self.one(t, out, id)

    def move(self, t: Table, column: str, from_: list[str], to: str, id: Any, out: list[Col], stamp: list[Col] = (),
             vals: dict | None = None) -> dict:
        """상태 전환: 허용된 현재 상태에서만 바꾼다 (BEHAVIOR.md 1절). 다른 상태면 409로 거절하고 값을 바꾸지 않는다."""
        params: dict = {"__id": id}
        state = f"{column} = :{self._param(params, to)}"
        allowed = ", ".join(":" + self._param(params, v) for v in from_)
        count = self.conn.execute(text(f"UPDATE {t.name} SET {self._state_sets(t, state, stamp, vals or {}, params)}"
                                       f" WHERE {t.pk} = {_bind('__id', t.pk_type)} AND {column} IN ({allowed})"), params).rowcount
        if count == 0:
            self.one(t, out, id)   # 대상이 없으면 404
            raise ApiError(409, "INVALID_STATE", "지금 상태에서는 처리할 수 없습니다.")
        return self.one(t, out, id)

    def move_to(self, t: Table, column: str, allowed: dict[str, list[str]], to: Any, id: Any, out: list[Col], stamp: list[Col] = (),
                vals: dict | None = None) -> dict:
        """API 하나가 여러 상태로 보내는 전환: 요청 값이 목표 상태이고, allowed는 목표 → 허용된 현재 상태다. 표에 없는 목표는 409다."""
        from_ = allowed.get(str(to))
        if from_ is None:
            raise ApiError(409, "INVALID_STATE", "지금 상태에서는 처리할 수 없습니다.")
        return self.move(t, column, from_, str(to), id, out, stamp, vals)

    def delete(self, t: Table, id: Any) -> dict:
        count = self.conn.execute(text(f"DELETE FROM {t.name} WHERE {t.pk} = {_bind('__id', t.pk_type)}"), {"__id": id}).rowcount
        if count == 0:
            raise ApiError.not_found()
        return {}

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        return self._rows(sql, params or {})

    def execute(self, sql: str, params: dict | None = None) -> int:
        return self.conn.execute(text(sql), params or {}).rowcount
