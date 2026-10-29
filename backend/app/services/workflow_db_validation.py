"""서버 생성 DDL을 격리 PostgreSQL 트랜잭션에서 실행·구조 비교한다."""

import hashlib
import re
from collections import Counter
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg

from app.agents.modeling.design_model import model_hash_v1
from app.schemas.design_v1 import DataModelV1

ValidationStatus = Literal["passed", "failed", "not_run"]
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_DELETE_ACTION = {"no_action": "a", "restrict": "r", "cascade": "c", "set_null": "n"}


def _report(model: DataModelV1, ddl: str, status: ValidationStatus, code: str, checks: list[str]) -> dict:
    return {
        "status": status,
        "code": code,
        "model_hash": model_hash_v1(model),
        "ddl_sha256": hashlib.sha256(ddl.encode("utf-8")).hexdigest(),
        "tables_expected": len(model.tables),
        "checks": checks,
    }


def _postgres_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or not parsed.hostname or not parsed.path.strip("/"):
        return None
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _statements(ddl: str) -> list[str]:
    """서버 렌더러의 세미콜론을 분리한다. 문자열 리터럴 속 세미콜론은 유지한다."""
    result = []
    current = []
    quoted = False
    index = 0
    while index < len(ddl):
        char = ddl[index]
        current.append(char)
        if char == "'":
            if quoted and index + 1 < len(ddl) and ddl[index + 1] == "'":
                current.append("'")
                index += 1
            else:
                quoted = not quoted
        elif char == ";" and not quoted:
            statement = "".join(current).strip()
            if statement:
                result.append(statement)
            current = []
        index += 1
    if quoted or "".join(current).strip():
        raise ValueError("generated DDL has an unterminated or missing statement")
    return result


def _expected_catalog(model: DataModelV1) -> tuple[set[str], dict, Counter, Counter]:
    tables = set()
    columns = {}
    keys = Counter()
    foreign_keys = Counter()
    by_id = {table.id: table for table in model.tables}
    for table in model.tables:
        if not _IDENTIFIER.fullmatch(table.name):
            raise ValueError("invalid table identifier")
        tables.add(table.name)
        own = {column.id: column for column in table.columns}
        for column in table.columns:
            if not _IDENTIFIER.fullmatch(column.name):
                raise ValueError("invalid column identifier")
            type_name = column.type
            if type_name == "varchar":
                type_name = f"character varying({column.length})"
            elif type_name == "numeric":
                type_name = f"numeric({column.precision},{column.scale})"
            elif type_name == "timestamptz":
                type_name = "timestamp with time zone"
            columns[(table.name, column.name)] = (type_name, not column.nullable)
        keys[(table.name, "p", tuple(own[column_id].name for column_id in table.primary_key))] += 1
        for group in table.uniques:
            keys[(table.name, "u", tuple(own[column_id].name for column_id in group))] += 1
        for fk in table.foreign_keys:
            target = by_id[fk.target_table_id]
            target_columns = {column.id: column for column in target.columns}
            foreign_keys[
                (
                    table.name,
                    tuple(own[column_id].name for column_id in fk.source_column_ids),
                    target.name,
                    tuple(target_columns[column_id].name for column_id in fk.target_column_ids),
                    _DELETE_ACTION[fk.on_delete],
                )
            ] += 1
    if len(tables) != len(model.tables) or len(columns) != sum(len(table.columns) for table in model.tables):
        raise ValueError("duplicate table or column names")
    return tables, columns, keys, foreign_keys


def _actual_catalog(conn, schema: str) -> tuple[set[str], dict, Counter, Counter]:
    tables = {
        row[0]
        for row in conn.execute(
            """SELECT c.relname FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = %s AND c.relkind = 'r'""",
            (schema,),
        ).fetchall()
    }
    columns = {}
    numbers = {}
    for table, number, name, type_name, not_null in conn.execute(
        """SELECT c.relname, a.attnum, a.attname,
                  pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull
           FROM pg_catalog.pg_attribute a
           JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
           JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped""",
        (schema,),
    ).fetchall():
        columns[(table, name)] = (type_name.replace(", ", ","), not_null)
        numbers[(table, number)] = name
    keys = Counter()
    foreign_keys = Counter()
    for child, kind, source_numbers, parent, target_numbers, on_delete in conn.execute(
        """SELECT child.relname, con.contype, con.conkey::int[],
                  parent.relname, con.confkey::int[], con.confdeltype
           FROM pg_catalog.pg_constraint con
           JOIN pg_catalog.pg_class child ON child.oid = con.conrelid
           LEFT JOIN pg_catalog.pg_class parent ON parent.oid = con.confrelid
           JOIN pg_catalog.pg_namespace n ON n.oid = child.relnamespace
           WHERE n.nspname = %s AND con.contype IN ('p', 'u', 'f')""",
        (schema,),
    ).fetchall():
        source = tuple(numbers[(child, number)] for number in source_numbers)
        if kind == "f":
            target = tuple(numbers[(parent, number)] for number in target_numbers)
            foreign_keys[(child, source, parent, target, on_delete)] += 1
        else:
            keys[(child, kind, source)] += 1
    return tables, columns, keys, foreign_keys


def _compare_catalog(expected: tuple, actual: tuple) -> list[str]:
    return [
        name
        for name, expected_part, actual_part in zip(
            ("tables", "columns", "primary_unique_keys", "foreign_keys"), expected, actual, strict=True
        )
        if expected_part != actual_part
    ]


def _validate_connected(conn, model: DataModelV1, ddl: str, expected: tuple) -> tuple[ValidationStatus, str, list[str]]:
    role = conn.execute(
        """SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
           FROM pg_catalog.pg_roles WHERE rolname = current_user"""
    ).fetchone()
    if role is None or any(role):
        return "not_run", "validation_role_not_restricted", ["검증 역할의 권한을 제한해야 합니다."]
    schema = f"validation_{uuid4().hex}"
    try:
        conn.execute("SET LOCAL statement_timeout = '5000ms'")
        conn.execute("SET LOCAL lock_timeout = '2000ms'")
        conn.execute("SET LOCAL idle_in_transaction_session_timeout = '15000ms'")
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET LOCAL search_path TO "{schema}", pg_catalog')
    except psycopg.Error:
        return "not_run", "validation_environment_unavailable", ["격리 스키마를 준비하지 못했습니다."]
    try:
        for statement in _statements(ddl):
            conn.execute(statement)
    except psycopg.Error as exc:
        return "failed", "ddl_execution_failed", [f"SQL 실행 오류(SQLSTATE {exc.sqlstate or 'unknown'})."]
    try:
        mismatches = _compare_catalog(expected, _actual_catalog(conn, schema))
    except (psycopg.Error, KeyError, TypeError):
        return "not_run", "catalog_inspection_failed", ["검증 DB 구조를 읽지 못했습니다."]
    if mismatches:
        return "failed", "catalog_mismatch", [f"생성 구조 불일치: {', '.join(mismatches)}"]
    return "passed", "validated", ["격리 PostgreSQL에서 테이블·컬럼·키·FK 구조 일치"]


def validate_ddl(model: DataModelV1, ddl: str, validation_database_url: str) -> tuple[ValidationStatus, dict]:
    """모델과 동일한 허용 DDL만 실행한다. 어떠한 결과에서도 검증 스키마는 커밋하지 않는다."""
    if not validation_database_url:
        return "not_run", _report(model, ddl, "not_run", "validation_database_not_configured", ["검증 DB 미설정"])
    url = _postgres_url(validation_database_url)
    if url is None:
        return "not_run", _report(model, ddl, "not_run", "validation_database_invalid", ["PostgreSQL URL 필요"])
    try:
        from app.agents.modeling.design_model import render_ddl_v1

        if not model.tables or ddl != render_ddl_v1(model):
            return "failed", _report(model, ddl, "failed", "ddl_not_from_model", ["모델 생성 DDL과 불일치"])
        expected = _expected_catalog(model)
        if len(_statements(ddl)) != len(model.tables) + sum(len(table.foreign_keys) for table in model.tables):
            raise ValueError("unexpected statement count")
    except (KeyError, ValueError):
        return "failed", _report(model, ddl, "failed", "model_or_ddl_invalid", ["모델 또는 DDL 참조 오류"])
    try:
        conn = psycopg.connect(url, connect_timeout=5, autocommit=False)
    except psycopg.Error:
        return "not_run", _report(model, ddl, "not_run", "validation_database_unavailable", ["검증 DB 접속 실패"])
    outcome: tuple[ValidationStatus, str, list[str]]
    try:
        outcome = _validate_connected(conn, model, ddl, expected)
    except psycopg.Error:
        outcome = ("not_run", "validation_environment_unavailable", ["검증 DB 실행 환경 오류"])
    finally:
        try:
            conn.rollback()
        except psycopg.Error:
            outcome = ("not_run", "validation_cleanup_failed", ["검증 트랜잭션 정리 실패"])
        conn.close()
    status, code, checks = outcome
    return status, _report(model, ddl, status, code, checks)
