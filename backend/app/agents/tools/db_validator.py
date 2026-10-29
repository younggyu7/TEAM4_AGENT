"""격리된 검증용 DB에 DDL을 적용하고 구조를 검사한다.

- 기본: PostgreSQL (VALIDATION_DB_URL, 비우면 docker-compose의 validation-db).
  실행마다 임시 스키마를 만들어 DDL 실행 → 구조 검사 → 스키마 삭제
- PostgreSQL에 접속할 수 없으면 SQLite 메모리 DB로 대체 검증하고 그 사실을 보고서에 남긴다
  (문법·FK 대상 존재 수준. PostgreSQL 전용 문법은 검증하지 못함). VALIDATION_DB_URL=sqlite면 처음부터 SQLite.
검사 통과는 '스키마 생성·기본 제약 존재'를 뜻할 뿐, 업무 정확성·성능 보장이 아니다.
"""
from __future__ import annotations

import logging
import re
import sqlite3

from app.core.security import mask_url
from app.schemas.design import DataModel, DbValidationResult
from app.agents.tools.ddl import render_statements

log = logging.getLogger(__name__)
START_HINT = "docker compose up -d validation-db 로 검증용 PostgreSQL을 띄우면 PostgreSQL로 검증한다"


class DbUnavailable(RuntimeError):
    """검증용 PostgreSQL에 접속할 수 없음 (DDL 오류가 아니므로 데이터 모델 수정 대상이 아니다)."""


def validate_ddl(model: DataModel, design_version: str, run_id: str, db_url: str = "",
                 keep_schema: bool = False) -> DbValidationResult:
    """PostgreSQL로 DDL을 적용·검증한다. URL이 없거나 접속할 수 없으면 SQLite로 대체 검증한다."""
    if not db_url:
        return _validate_sqlite(model, design_version, "VALIDATION_DB_URL=sqlite → SQLite 검증")
    try:
        return _validate_postgres(model, design_version, run_id, db_url, keep_schema)
    except DbUnavailable as e:
        log.warning("[DB 검증] PostgreSQL(%s) 접속 실패 → SQLite 대체 검증: %s", mask_url(db_url), e)
        return _validate_sqlite(model, design_version,
                                f"PostgreSQL({mask_url(db_url)}) 접속 실패 → SQLite 대체 검증. {START_HINT}")


def _expected(model: DataModel) -> tuple[set[str], int]:
    """모델에서 기대하는 테이블 이름 목록과 FK 수."""
    tables = {e.entity_id for e in model.entities if e.attributes}
    fks = sum(1 for s in render_statements(model, "postgresql") if s.kind == "fk")
    return tables, fks


def _connect(db_url: str):
    """검증용 PostgreSQL에 접속한다. 드라이버가 없거나 접속할 수 없으면 DbUnavailable."""
    try:
        import psycopg
    except ImportError as e:
        raise DbUnavailable("psycopg 미설치 (pip install 'psycopg[binary]')") from e
    try:
        return psycopg.connect(db_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as e:
        raise DbUnavailable(str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__) from e


def _validate_postgres(model, design_version, run_id, db_url, keep_schema) -> DbValidationResult:
    """임시 스키마에 DDL을 실행하고 테이블·PK·FK·CHECK 수를 확인한 뒤 스키마를 지운다."""
    import psycopg

    schema = re.sub(r"[^a-z0-9_]", "_", f"reqagent_{run_id}_v{design_version}".lower())[:63]
    expected_tables, expected_fks = _expected(model)
    errors, failed, checks = [], set(), []
    statements = render_statements(model, "postgresql")
    executed = 0
    with _connect(db_url) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            conn.execute(f'SET search_path TO "{schema}"')
            for st in statements:
                # 생성에 실패한 테이블에 딸린 문장은 같은 원인의 중복 오류이므로 건너뛴다
                if st.table in failed or st.depends_on in failed:
                    continue
                try:
                    conn.execute(st.sql)
                    executed += 1
                except psycopg.Error as e:
                    msg = str(e).strip().splitlines()[0]
                    errors.append(f"[{st.table}] {msg} :: {st.sql.splitlines()[0][:160]}")
                    failed.add(st.table)
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)).fetchall()
            created = {r[0] for r in rows}
            pk_tables = {r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.table_constraints "
                "WHERE table_schema = %s AND constraint_type = 'PRIMARY KEY'", (schema,)).fetchall()}
            fk_count = conn.execute(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_schema = %s AND constraint_type = 'FOREIGN KEY'", (schema,)).fetchone()[0]
            check_count = conn.execute(
                "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                "WHERE n.nspname = %s AND c.contype = 'c'", (schema,)).fetchone()[0]
        finally:
            if not keep_schema:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    missing = sorted(expected_tables - created)
    no_pk = sorted((expected_tables & created) - pk_tables)
    checks.append(f"테이블 생성: {len(created)}/{len(expected_tables)}")
    checks.append(f"기본키 보유 테이블: {len(pk_tables)}/{len(expected_tables)}")
    checks.append(f"외래키 제약: {fk_count}/{expected_fks}")
    checks.append(f"CHECK 제약: {check_count}")
    if missing:
        errors.append(f"생성되지 않은 테이블: {', '.join(missing)}")
    if no_pk:
        errors.append(f"기본키가 없는 테이블: {', '.join(no_pk)}")
        failed.update(no_pk)
    if fk_count != expected_fks:
        errors.append(f"외래키 수 불일치: 기대 {expected_fks}, 실제 {fk_count}")
    return DbValidationResult(
        engine="postgresql", design_version=design_version, passed=not errors, statements=executed,
        tables_expected=len(expected_tables), tables_created=sorted(created), foreign_keys=fk_count,
        checks=checks, errors=errors, failed_tables=sorted(failed),
        note=f"임시 스키마 {schema}" + (" (보존됨)" if keep_schema else " (검증 후 삭제)"),
    )


def _validate_sqlite(model, design_version, note: str) -> DbValidationResult:
    """SQLite 메모리 DB에 DDL을 실행하고 테이블·PK·FK 대상 존재를 확인한다."""
    expected_tables, expected_fks = _expected(model)
    errors, failed, checks = [], set(), []
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    executed = 0
    for st in render_statements(model, "sqlite"):
        try:
            conn.execute(st.sql)
            executed += 1
        except sqlite3.Error as e:
            errors.append(f"[{st.table}] {e} :: {st.sql.splitlines()[0][:160]}")
            failed.add(st.table)
    created = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    fk_count = 0
    no_pk = []
    for table in sorted(created):
        cols = {r[1]: r for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if not any(r[5] for r in cols.values()):
            no_pk.append(table)
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            fk_count += 1
            target, to_col = fk[2], fk[4]
            if target not in created:
                errors.append(f"[{table}] FK 대상 테이블 없음: {target}")
                failed.add(table)
                continue
            target_cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{target}")')}
            if to_col not in target_cols:
                errors.append(f"[{table}] FK 대상 컬럼 없음: {target}.{to_col}")
                failed.add(table)
    conn.close()
    missing = sorted(expected_tables - created)
    checks.append(f"테이블 생성: {len(created)}/{len(expected_tables)}")
    checks.append(f"기본키 보유 테이블: {len(created) - len(no_pk)}/{len(expected_tables)}")
    checks.append(f"외래키 정의: {fk_count}/{expected_fks}")
    if missing:
        errors.append(f"생성되지 않은 테이블: {', '.join(missing)}")
    if no_pk:
        errors.append(f"기본키가 없는 테이블: {', '.join(no_pk)}")
        failed.update(no_pk)
    if fk_count != expected_fks:
        errors.append(f"외래키 수 불일치: 기대 {expected_fks}, 실제 {fk_count}")
    return DbValidationResult(
        engine="sqlite", design_version=design_version, passed=not errors, statements=executed,
        tables_expected=len(expected_tables), tables_created=sorted(created), foreign_keys=fk_count,
        checks=checks, errors=errors, failed_tables=sorted(failed),
        note=f"{note} (PostgreSQL 전용 문법·타입은 검증되지 않음)",
    )
