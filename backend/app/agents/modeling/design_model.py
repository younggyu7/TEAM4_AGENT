"""v1 공통 데이터 모델의 구조 검사와 결정적 산출물 생성.

현재 ``/design``의 단일 키 모델과 렌더러는 그대로 유지한다. 이 모듈은 새 단계
계약의 모델을 단 한 번 검사한 뒤 같은 스냅샷에서 ERD, DDL, 추적 데이터를 만든다.
SQL 실행과 업무 의미 판단은 이 모듈의 책임이 아니다.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from app.agents.modeling.validation import validate_data_design
from app.schemas.design_v1 import (
    SCHEMA_VERSION,
    AnalysisInput,
    ColumnV1,
    DataDesignInput,
    DataDesignOutput,
    DataModelV1,
    ReviewFindingV1,
    TraceLinkV1,
)

TraceStatus = Literal["linked", "unlinked", "not_applicable"]
_TRACE_RELATIONS = {
    "requirement_function": ("requirement", "function"),
    "requirement_table": ("requirement", "table"),
    "requirement_column": ("requirement", "column"),
    "function_table": ("function", "table"),
    "table_column": ("table", "column"),
}
_INTEGER_LIMITS = {"integer": (-(2**31), 2**31 - 1), "bigint": (-(2**63), 2**63 - 1)}
_NUMERIC = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


@dataclass(frozen=True)
class TraceRowV1:
    requirement_id: str
    kind: str
    status: TraceStatus
    function_ids: tuple[str, ...]
    table_ids: tuple[str, ...]
    column_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ModelArtifactsV1:
    schema_version: str
    model_hash: str
    mermaid_erd: str
    postgresql_ddl: str
    trace_rows: tuple[TraceRowV1, ...]
    review_candidates: tuple[ReviewFindingV1, ...]


class ModelStructureError(ValueError):
    """구조 오류를 담는다. 모델 본문·자유 텍스트는 예외 메시지에 넣지 않는다."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("v1 data model has structural errors")


def model_hash_v1(model: DataModelV1) -> str:
    """스키마 버전과 모델 JSON의 안정적인 SHA-256 식별자."""
    canonical = json.dumps(
        {"schema_version": SCHEMA_VERSION, "data_model": model.model_dump(mode="json")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _column_signature(column: ColumnV1) -> tuple:
    return column.type, column.length, column.precision, column.scale


def _valid_default(column: ColumnV1) -> bool:
    if column.default_kind == "none":
        return column.default_value is None and column.default_function is None
    if column.default_kind == "server_function":
        return column.default_value is None and (column.type, column.default_function) in {
            ("timestamptz", "current_timestamp"),
            ("uuid", "gen_random_uuid"),
        }
    if column.default_kind != "literal" or column.default_function is not None or column.default_value is None:
        return False
    value = column.default_value
    if column.type in _INTEGER_LIMITS:
        low, high = _INTEGER_LIMITS[column.type]
        return type(value) is int and low <= value <= high
    if column.type == "numeric":
        if type(value) is int:
            number = Decimal(value)
        elif isinstance(value, str) and _NUMERIC.fullmatch(value):
            try:
                number = Decimal(value)
            except InvalidOperation:
                return False
        else:
            return False
        sign, digits, exponent = number.as_tuple()
        del sign
        fractional = max(-exponent, 0)
        integral = max(len(digits) + exponent, 0)
        return fractional <= column.scale and integral <= column.precision - column.scale
    if column.type == "boolean":
        return type(value) is bool
    if column.type in {"varchar", "text"}:
        return isinstance(value, str) and (column.length is None or len(value) <= column.length)
    if not isinstance(value, str):
        return False
    try:
        if column.type == "date":
            date.fromisoformat(value)
        elif column.type == "timestamptz":
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return False
        elif column.type == "uuid":
            UUID(value)
    except ValueError:
        return False
    return True


def _indices(stage_input: DataDesignInput, output: DataDesignOutput) -> tuple[dict, dict, dict, dict]:
    requirements = {item.id: item for item in stage_input.analysis.requirements}
    functions = {item.id: item for item in stage_input.functions.functions}
    tables = {item.id: item for item in output.data_model.tables}
    columns = {column.id: (table, column) for table in output.data_model.tables for column in table.columns}
    return requirements, functions, tables, columns


def _trace_issues(
    stage_input: DataDesignInput,
    output: DataDesignOutput,
    trace_links: list[TraceLinkV1],
    not_applicable_reasons: dict[str, str],
) -> list[str]:
    issues = []
    requirements, functions, tables, columns = _indices(stage_input, output)
    namespaces = {"requirement": requirements, "function": functions, "table": tables, "column": columns}
    answers = {item.question_id: item for item in stage_input.answers.answers}
    seen = set()
    for index, link in enumerate(trace_links):
        relation = _TRACE_RELATIONS.get(link.relation)
        if relation is None:
            issues.append(f"trace_links[{index}]: unsupported relation")
            continue
        source_kind, target_kind = relation
        source = namespaces[source_kind].get(link.source_id)
        target = namespaces[target_kind].get(link.target_id)
        if source is None or target is None:
            issues.append(f"trace_links[{index}]: missing source or target")
            continue
        key = link.source_id, link.target_id, link.relation
        if key in seen:
            issues.append(f"trace_links[{index}]: duplicate relation")
        seen.add(key)
        if link.relation == "requirement_function" and link.source_id not in target.requirement_ids:
            issues.append(f"trace_links[{index}]: function requirement link conflicts with model")
        if link.relation == "requirement_table" and link.source_id not in target.requirement_ids:
            issues.append(f"trace_links[{index}]: table requirement link conflicts with model")
        if link.relation == "function_table" and link.source_id not in target.function_ids:
            issues.append(f"trace_links[{index}]: table function link conflicts with model")
        if link.relation == "table_column" and target[0].id != link.source_id:
            issues.append(f"trace_links[{index}]: column belongs to another table")
        if link.provenance == "user_answer":
            answer = answers.get(link.answer_question_id)
            if answer is None or answer.unresolved or answer.version != link.answer_version:
                issues.append(f"trace_links[{index}]: invalid answer reference")
    for requirement_id, reason in not_applicable_reasons.items():
        requirement = requirements.get(requirement_id)
        if requirement is None or requirement.kind != "nonfunctional" or not reason.strip():
            issues.append(f"not_applicable[{requirement_id}]: invalid requirement or reason")
        linked_functions = {item.id for item in functions.values() if requirement_id in item.requirement_ids}
        if (
            any(link.source_id == requirement_id for link in trace_links)
            or linked_functions
            or any(
                requirement_id in table.requirement_ids or linked_functions.intersection(table.function_ids)
                for table in tables.values()
            )
        ):
            issues.append(f"not_applicable[{requirement_id}]: already linked")
    return issues


def _model_issues(model: DataModelV1) -> list[str]:
    """원문·기능 관계와 무관한 모델 자체의 구조 제약."""
    try:
        DataModelV1.model_validate(model.model_dump(mode="python"))
    except ValidationError as exc:
        return [f"data_model.{'.'.join(map(str, item['loc']))}: {item['type']}" for item in exc.errors()]
    issues = []
    tables = {table.id: table for table in model.tables}
    if len(tables) != len(model.tables) or len({table.name for table in model.tables}) != len(model.tables):
        issues.append("tables: duplicate id or name")
    all_columns = [column.id for table in model.tables for column in table.columns]
    if len(set(all_columns)) != len(all_columns):
        issues.append("columns: duplicate id")
    if len({rule.id for rule in model.rules}) != len(model.rules):
        issues.append("rules: duplicate id")
    for table_index, table in enumerate(model.tables):
        own_columns = {column.id: column for column in table.columns}
        if len(own_columns) != len(table.columns) or len({column.name for column in table.columns}) != len(
            table.columns
        ):
            issues.append(f"tables[{table_index}].columns: duplicate id or name")
        if len(table.primary_key) != len(set(table.primary_key)):
            issues.append(f"tables[{table_index}].primary_key: duplicate column")
        if not set(table.primary_key) <= own_columns.keys():
            issues.append(f"tables[{table_index}].primary_key: unknown column")
        for column_index, column in enumerate(table.columns):
            label = f"tables[{table_index}].columns[{column_index}]"
            if column.id in table.primary_key and column.nullable:
                issues.append(f"{label}: primary key cannot be nullable")
            if not _valid_default(column):
                issues.append(f"{label}: default incompatible with type")
        seen_uniques = set()
        for unique_index, group in enumerate(table.uniques):
            key = tuple(group)
            if not group or len(group) != len(set(group)) or key in seen_uniques:
                issues.append(f"tables[{table_index}].uniques[{unique_index}]: duplicate or empty key")
            if not set(group) <= own_columns.keys():
                issues.append(f"tables[{table_index}].uniques[{unique_index}]: unknown column")
            seen_uniques.add(key)
        for fk_index, foreign_key in enumerate(table.foreign_keys):
            label = f"tables[{table_index}].foreign_keys[{fk_index}]"
            if len(foreign_key.source_column_ids) != len(foreign_key.target_column_ids):
                issues.append(f"{label}: source and target arity differ")
            target = tables.get(foreign_key.target_table_id)
            target_columns = {column.id: column for column in target.columns} if target else {}
            sources = [own_columns.get(column_id) for column_id in foreign_key.source_column_ids]
            targets = [target_columns.get(column_id) for column_id in foreign_key.target_column_ids]
            if any(column is None for column in (*sources, *targets)):
                issues.append(f"{label}: invalid column reference")
                continue
            if len(set(foreign_key.source_column_ids)) != len(foreign_key.source_column_ids) or len(
                set(foreign_key.target_column_ids)
            ) != len(foreign_key.target_column_ids):
                issues.append(f"{label}: duplicate FK column")
            if target and tuple(foreign_key.target_column_ids) not in {
                tuple(target.primary_key),
                *(tuple(group) for group in target.uniques),
            }:
                issues.append(f"{label}: target must be a primary or unique key")
            if any(
                _column_signature(source) != _column_signature(dest)
                for source, dest in zip(sources, targets, strict=False)
            ):
                issues.append(f"{label}: source and target types differ")
            if foreign_key.on_delete == "set_null" and any(not column.nullable for column in sources):
                issues.append(f"{label}: set_null requires nullable source columns")
    return issues


def validate_model_v1(
    stage_input: DataDesignInput,
    output: DataDesignOutput,
    trace_links: list[TraceLinkV1] | None = None,
    not_applicable_reasons: dict[str, str] | None = None,
) -> list[str]:
    """키·타입·기본값·FK와 명시 추적 링크만 검사한다. 의미 적합성은 판단하지 않는다."""
    issues = _model_issues(output.data_model)
    if any(issue.startswith("data_model.") for issue in issues):
        return issues
    issues.extend(validate_data_design(stage_input, output))
    issues.extend(_trace_issues(stage_input, output, trace_links or [], not_applicable_reasons or {}))
    return issues


def _quote(name: str) -> str:
    return '"' + name + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_type(column: ColumnV1) -> str:
    if column.type == "varchar":
        return f"VARCHAR({column.length})"
    if column.type == "numeric":
        return f"NUMERIC({column.precision}, {column.scale})"
    return column.type.upper()


def _sql_default(column: ColumnV1) -> str:
    if column.default_kind == "none":
        return ""
    if column.default_kind == "server_function":
        return (
            " DEFAULT CURRENT_TIMESTAMP"
            if column.default_function == "current_timestamp"
            else " DEFAULT gen_random_uuid()"
        )
    value = column.default_value
    if type(value) is bool:
        return " DEFAULT TRUE" if value else " DEFAULT FALSE"
    if type(value) is int or column.type == "numeric":
        return f" DEFAULT {value}"
    return " DEFAULT " + _sql_literal(value)


def _mermaid_type(column: ColumnV1) -> str:
    if column.type == "varchar":
        return f"varchar_{column.length}"
    if column.type == "numeric":
        return f"numeric_{column.precision}_{column.scale}"
    return column.type


def _render_model(model: DataModelV1) -> tuple[str, str]:
    tables = {table.id: table for table in model.tables}
    erd = ["erDiagram"]
    ddl = []
    foreign_keys = []
    for table in model.tables:
        columns = {column.id: column for column in table.columns}
        fk_columns = {column_id for fk in table.foreign_keys for column_id in fk.source_column_ids}
        single_unique = {group[0] for group in table.uniques if len(group) == 1}
        erd.append(f"    {table.name} {{")
        sql_lines = []
        for column in table.columns:
            keys = [
                key
                for key, enabled in (
                    ("PK", column.id in table.primary_key),
                    ("FK", column.id in fk_columns),
                    ("UK", column.id in single_unique),
                )
                if enabled
            ]
            erd.append(f"        {_mermaid_type(column)} {column.name}" + (" " + ",".join(keys) if keys else ""))
            sql_lines.append(
                f"    {_quote(column.name)} {_sql_type(column)}"
                + (" NOT NULL" if not column.nullable else "")
                + _sql_default(column)
            )
        sql_lines.append(
            "    PRIMARY KEY (" + ", ".join(_quote(columns[column_id].name) for column_id in table.primary_key) + ")"
        )
        for group in table.uniques:
            sql_lines.append("    UNIQUE (" + ", ".join(_quote(columns[column_id].name) for column_id in group) + ")")
        ddl.append(f"CREATE TABLE {_quote(table.name)} (\n" + ",\n".join(sql_lines) + "\n);")
        erd.append("    }")
        for fk in table.foreign_keys:
            target = tables[fk.target_table_id]
            targets = {column.id: column for column in target.columns}
            source_names = ", ".join(_quote(columns[column_id].name) for column_id in fk.source_column_ids)
            target_names = ", ".join(_quote(targets[column_id].name) for column_id in fk.target_column_ids)
            foreign_keys.append(
                f"ALTER TABLE {_quote(table.name)} ADD FOREIGN KEY ({source_names}) "
                f"REFERENCES {_quote(target.name)} ({target_names}) "
                f"ON DELETE {fk.on_delete.replace('_', ' ').upper()};"
            )
            parent = "||" if all(not columns[column_id].nullable for column_id in fk.source_column_ids) else "|o"
            child = (
                "o|"
                if tuple(fk.source_column_ids) in {tuple(table.primary_key), *(tuple(group) for group in table.uniques)}
                else "o{"
            )
            identifying = "--" if set(fk.source_column_ids) <= set(table.primary_key) else ".."
            label = ",".join(columns[column_id].name for column_id in fk.source_column_ids)
            erd.append(f'    {target.name} {parent}{identifying}{child} {table.name} : "{label}"')
    return "\n".join(erd), "\n\n".join([*ddl, *foreign_keys])


def _trace_rows(
    stage_input: DataDesignInput,
    output: DataDesignOutput,
    trace_links: list[TraceLinkV1],
    not_applicable_reasons: dict[str, str],
) -> tuple[TraceRowV1, ...]:
    rows = []
    for requirement in stage_input.analysis.requirements:
        functions = [
            function.id for function in stage_input.functions.functions if requirement.id in function.requirement_ids
        ]
        tables = [
            table.id
            for table in output.data_model.tables
            if requirement.id in table.requirement_ids or set(functions) & set(table.function_ids)
        ]
        # 컬럼의 업무 근거는 테이블 포함 관계에서 추론하지 않는다.
        columns = [
            link.target_id
            for link in trace_links
            if link.relation == "requirement_column" and link.source_id == requirement.id
        ]
        not_applicable = requirement.id in not_applicable_reasons
        explicit_reasons = [
            link.reason
            for link in trace_links
            if link.source_id == requirement.id and link.relation.startswith("requirement_")
        ]
        reason = not_applicable_reasons.get(requirement.id) or "; ".join(dict.fromkeys(explicit_reasons))
        if not reason and (functions or tables):
            reason = "명시된 요구사항·기능 ID 참조로 연결됨; 업무적 이유 미검증"
        rows.append(
            TraceRowV1(
                requirement_id=requirement.id,
                kind=requirement.kind,
                status="not_applicable"
                if not_applicable
                else "linked"
                if functions or tables or columns
                else "unlinked",
                function_ids=tuple(dict.fromkeys(functions)),
                table_ids=tuple(dict.fromkeys(tables)),
                column_ids=tuple(dict.fromkeys(columns)),
                reason=reason,
            )
        )
    return tuple(rows)


def _review_candidates(output: DataDesignOutput, rows: tuple[TraceRowV1, ...]) -> tuple[ReviewFindingV1, ...]:
    candidates = []
    unlinked_requirements = [
        row.requirement_id for row in rows if row.status == "unlinked" and row.kind == "functional"
    ]
    if unlinked_requirements:
        candidates.append(
            ReviewFindingV1(
                id=f"C-FIND-{len(candidates) + 1}",
                type="omission",
                severity="warning",
                related_ids=unlinked_requirements,
                evidence=[],
                description="기능 요구사항의 기능·테이블 연결이 확인되지 않았습니다.",
                suggestion="필요한 기능 연결을 검토하세요.",
                detector="traceability_rule",
            )
        )
    linked_columns = {column_id for row in rows for column_id in row.column_ids}
    unlinked_columns = [
        column.id for table in output.data_model.tables for column in table.columns if column.id not in linked_columns
    ]
    if unlinked_columns:
        candidates.append(
            ReviewFindingV1(
                id=f"C-FIND-{len(candidates) + 1}",
                type="not_verified",
                severity="warning",
                related_ids=unlinked_columns,
                evidence=[],
                description="컬럼별 요구사항 연결 근거가 확인되지 않았습니다.",
                suggestion="필요한 컬럼 연결과 이유를 기록하거나 연결 불필요 사유를 검토하세요.",
                detector="traceability_rule",
            )
        )
    unresolved_rules = [rule.id for rule in output.data_model.rules if rule.enforcement == "unresolved"]
    if unresolved_rules:
        candidates.append(
            ReviewFindingV1(
                id=f"C-FIND-{len(candidates) + 1}",
                type="unimplemented_rule",
                severity="warning",
                related_ids=unresolved_rules,
                evidence=[],
                description="구현 책임이 정해지지 않은 규칙이 있습니다.",
                suggestion="DB 또는 애플리케이션 구현 책임을 확인하세요.",
                detector="model_rule",
            )
        )
    return tuple(candidates)


def render_model_v1(
    stage_input: DataDesignInput,
    output: DataDesignOutput,
    trace_links: list[TraceLinkV1] | None = None,
    not_applicable_reasons: dict[str, str] | None = None,
) -> ModelArtifactsV1:
    """구조 검사 실패 시 ERD·DDL을 만들지 않는다. DB 검증 통과는 별도 F 단계다."""
    links, reasons = trace_links or [], not_applicable_reasons or {}
    issues = validate_model_v1(stage_input, output, links, reasons)
    if issues:
        raise ModelStructureError(issues)
    model = output.data_model
    model_hash = model_hash_v1(model)
    erd, ddl = _render_model(model)
    rows = _trace_rows(stage_input, output, links, reasons)
    return ModelArtifactsV1(
        schema_version=SCHEMA_VERSION,
        model_hash=model_hash,
        mermaid_erd=erd,
        postgresql_ddl=ddl,
        trace_rows=rows,
        review_candidates=_review_candidates(output, rows),
    )


def render_ddl_v1(model: DataModelV1) -> str:
    """검증 DB가 수신할 SQL을 모델에서 재생성한다. 원문 SQL은 받지 않는다."""
    issues = _model_issues(model)
    if issues:
        raise ModelStructureError(issues)
    _, ddl = _render_model(model)
    return ddl


def validate_review_findings_v1(
    analysis_input: AnalysisInput,
    design_input: DataDesignInput,
    output: DataDesignOutput,
    findings: list[ReviewFindingV1],
) -> list[str]:
    """의미 검토 후보의 ID·원문 인용 형태만 검사하고 업무 판단은 하지 않는다."""
    issues = []
    blocks = {(block.document_revision_id, block.block_id): block for block in analysis_input.blocks}
    requirements, functions, tables, columns = _indices(design_input, output)
    allowed_ids = (
        set(requirements) | set(functions) | set(tables) | set(columns) | {rule.id for rule in output.data_model.rules}
    )
    answers = {answer.question_id: answer for answer in design_input.answers.answers}
    seen = set()
    for index, finding in enumerate(findings):
        if finding.id in seen:
            issues.append(f"findings[{index}]: duplicate id")
        seen.add(finding.id)
        if not set(finding.related_ids) <= allowed_ids:
            issues.append(f"findings[{index}]: unknown related id")
        if any(
            (block := blocks.get((evidence.document_revision_id, evidence.block_id))) is None
            or not evidence.matches(block)
            for evidence in finding.evidence
        ):
            issues.append(f"findings[{index}]: invalid evidence")
        if finding.answer_question_id is not None:
            answer = answers.get(finding.answer_question_id)
            if answer is None or answer.unresolved or answer.version != finding.answer_version:
                issues.append(f"findings[{index}]: invalid answer reference")
    return issues
