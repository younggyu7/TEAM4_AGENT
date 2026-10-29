"""카탈로그와 테이블별 속성을 나누어 생성하는 데이터 모델 전문 역할."""

import asyncio
import json
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from app.agents.flow.context import Context
from app.agents.flow.errors import failure
from app.agents.llm import client as model_client
from app.agents.modeling.design_model import validate_model_v1
from app.agents.modeling.validation import validate_function_design
from app.agents.prompts.data_modeler import ATTRIBUTES_PROMPT, CATALOG_PROMPT, COVER_PROMPT
from app.schemas.design import Identifier, ModelCall, StrictModel, Text
from app.schemas.design_v1 import (
    ColumnV1,
    DataDesignInput,
    DataDesignOutput,
    DataModelV1,
    ForeignKeyV1,
    FunctionDesignInput,
    RuleV1,
    TableV1,
)


class DataDesignAction(StrictModel):
    """조정자가 선택하는 전체 생성 또는 부분 재설계 범위."""

    mode: Literal["full", "cover", "fix"] = "full"
    scope: list[str] = Field(default_factory=list)
    previous: DataDesignOutput | None = None
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_scope(self):
        if self.mode == "full" and (self.scope or self.previous is not None):
            raise ValueError("full design cannot have scope or previous output")
        if self.mode != "full" and (not self.scope or self.previous is None or len(self.scope) != len(set(self.scope))):
            raise ValueError("cover/fix requires previous output and unique nonempty scope")
        return self


class TablePlan(StrictModel):
    name: Identifier
    description: Text
    requirement_ids: list[str]
    function_ids: list[str]
    parents: list[Identifier] = Field(default_factory=list)
    provenance: Literal["source", "user_answer", "proposal"]
    proposal_reason: str = ""
    answer_question_id: str | None = None
    answer_version: int | None = None


class RulePlan(StrictModel):
    description: Text
    requirement_ids: list[str]
    enforcement: Literal["database", "application", "unresolved"]
    representation: Text
    provenance: Literal["source", "user_answer", "proposal"]
    proposal_reason: str = ""
    answer_question_id: str | None = None
    answer_version: int | None = None


class CatalogDraft(StrictModel):
    tables: list[TablePlan] = Field(min_length=1)
    rules: list[RulePlan]
    rationale: list[Text]


class ColumnDraft(StrictModel):
    name: Identifier
    type: Literal["integer", "bigint", "numeric", "varchar", "text", "boolean", "date", "timestamptz", "uuid"]
    nullable: bool
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    default_kind: Literal["none", "literal", "server_function"]
    default_value: str | int | bool | None = None
    default_function: Literal["current_timestamp", "gen_random_uuid"] | None = None
    description: Text


class ForeignKeyDraft(StrictModel):
    source_column_names: list[Identifier] = Field(min_length=1)
    target_table_name: Identifier
    target_column_names: list[Identifier] = Field(min_length=1)
    on_delete: Literal["no_action", "restrict", "cascade", "set_null"]


class TableAttributes(StrictModel):
    table_name: Identifier
    columns: list[ColumnDraft] = Field(min_length=1)
    primary_key: list[Identifier] = Field(min_length=1)
    uniques: list[list[Identifier]]
    foreign_keys: list[ForeignKeyDraft]


class CoverageMapping(StrictModel):
    function_id: str
    table_names: list[Identifier]
    reason: Text


class CoverageDraft(StrictModel):
    mappings: list[CoverageMapping]
    new_tables: list[TablePlan]
    rationale: list[Text]


def _invalid_output(message: str):
    return failure("invalid_data_output", message, "data_design")


def _invalid_reference(message: str, issues: list[str] | None = None):
    return failure("invalid_data_reference", message, "data_design", issues=issues or [])


async def _call(payload: dict, schema, context: Context, prompt: str):
    raw, call = await model_client.call_model(
        json.dumps(payload, ensure_ascii=False), "data_design", schema, context, prompt
    )
    try:
        return schema.model_validate_json(raw), call
    except ValidationError:
        raise _invalid_output("데이터 설계의 부분 결과가 스키마와 일치하지 않습니다.") from None


def _aggregate_calls(calls: list[ModelCall]) -> ModelCall:
    return ModelCall(
        stage="data_design",
        model=calls[0].model if len({call.model for call in calls}) == 1 else "mixed",
        duration_ms=sum(call.duration_ms for call in calls),
        input_tokens=sum(call.input_tokens for call in calls),
        output_tokens=sum(call.output_tokens for call in calls),
        cache_creation_input_tokens=sum(call.cache_creation_input_tokens for call in calls),
        cache_read_input_tokens=sum(call.cache_read_input_tokens for call in calls),
    )


def _check_output(stage_input: DataDesignInput, output: DataDesignOutput) -> DataDesignOutput:
    issues = validate_model_v1(stage_input, output)
    if issues:
        raise _invalid_reference("데이터 설계의 키·타입·ID·참조가 잘못되었습니다.", issues)
    return output


def _check_plans(plans: list[TablePlan], known: set[str] | None = None) -> None:
    known = known or set()
    names = [plan.name for plan in plans]
    if len(set(names)) != len(names) or set(names) & known:
        raise _invalid_reference("테이블 카탈로그에 중복된 이름이 있습니다.")
    all_names = known | set(names)
    if any(len(set(plan.parents)) != len(plan.parents) or not set(plan.parents) <= all_names for plan in plans):
        raise _invalid_reference("테이블 카탈로그가 없는 상위 테이블을 참조합니다.")


def _plan_from_table(table: TableV1, names_by_id: dict[str, str]) -> TablePlan:
    return TablePlan(
        name=table.name,
        description=table.description,
        requirement_ids=table.requirement_ids.copy(),
        function_ids=table.function_ids.copy(),
        parents=sorted(
            {names_by_id[fk.target_table_id] for fk in table.foreign_keys if fk.target_table_id in names_by_id}
        ),
        provenance=table.provenance,
        proposal_reason=table.proposal_reason,
        answer_question_id=table.answer_question_id,
        answer_version=table.answer_version,
    )


async def _attributes(
    plans: list[TablePlan],
    catalog: list[TablePlan],
    stage_input: DataDesignInput,
    context: Context,
    previous: dict[str, TableV1],
    issues: list[str],
) -> tuple[dict[str, TableAttributes], list[ModelCall]]:
    async def one(plan: TablePlan):
        payload = {
            "plan": plan.model_dump(mode="json"),
            "catalog": [item.model_dump(mode="json") for item in catalog],
            "functions": [
                item.model_dump(mode="json") for item in stage_input.functions.functions if item.id in plan.function_ids
            ],
            "requirements": [
                item.model_dump(mode="json")
                for item in stage_input.analysis.requirements
                if item.id in plan.requirement_ids
            ],
            "answers": stage_input.answers.model_dump(mode="json"),
            "existing": previous[plan.name].model_dump(mode="json") if plan.name in previous else None,
            "issues": issues,
        }
        return await _call(payload, TableAttributes, context, ATTRIBUTES_PROMPT)

    results = await asyncio.gather(*(one(plan) for plan in plans), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    attributes = {}
    calls = []
    for plan, result in zip(plans, results, strict=True):
        item, call = result
        if item.table_name != plan.name:
            raise _invalid_reference("속성 설계가 요청한 테이블과 다릅니다.")
        if len({column.name for column in item.columns}) != len(item.columns):
            raise _invalid_reference("테이블 속성에 중복된 컬럼이 있습니다.")
        attributes[plan.name] = item
        calls.append(call)
    return attributes, calls


def _next_id(prefix: str, existing: list[str]) -> int:
    return max((int(value.removeprefix(prefix)) for value in existing if value.startswith(prefix)), default=0) + 1


def _assemble(
    plans: list[TablePlan],
    attributes: dict[str, TableAttributes],
    previous: DataDesignOutput | None,
    rule_plans: list[RulePlan],
    rationale: list[str],
) -> DataDesignOutput:
    old_tables = previous.data_model.tables if previous else []
    old_by_name = {table.name: table for table in old_tables}
    table_sequence = _next_id("C-TBL-", [table.id for table in old_tables])
    column_sequence = _next_id("C-COL-", [column.id for table in old_tables for column in table.columns])
    table_ids = {table.name: table.id for table in old_tables}
    for plan in plans:
        if plan.name not in table_ids:
            table_ids[plan.name] = f"C-TBL-{table_sequence}"
            table_sequence += 1
    columns_by_name = {
        table.name: {column.name: column.id for column in table.columns}
        for table in old_tables
        if table.name not in attributes
    }
    for plan in plans:
        if plan.name not in attributes:
            continue
        old_columns = (
            {column.name: column.id for column in old_by_name[plan.name].columns} if plan.name in old_by_name else {}
        )
        column_ids = {}
        for column in attributes[plan.name].columns:
            if column.name in old_columns:
                column_ids[column.name] = old_columns[column.name]
            else:
                column_ids[column.name] = f"C-COL-{column_sequence}"
                column_sequence += 1
        columns_by_name[plan.name] = column_ids

    tables = []
    for plan in plans:
        if plan.name not in attributes:
            if plan.name not in old_by_name:
                raise _invalid_output("새 테이블의 속성 설계가 누락되었습니다.")
            old = old_by_name[plan.name]
            tables.append(
                old.model_copy(update={"requirement_ids": plan.requirement_ids, "function_ids": plan.function_ids})
            )
            continue
        item = attributes[plan.name]
        own = columns_by_name[plan.name]
        try:
            columns = [ColumnV1(id=own[column.name], **column.model_dump()) for column in item.columns]
            fks = [
                ForeignKeyV1(
                    source_column_ids=[own[name] for name in fk.source_column_names],
                    target_table_id=table_ids[fk.target_table_name],
                    target_column_ids=[columns_by_name[fk.target_table_name][name] for name in fk.target_column_names],
                    on_delete=fk.on_delete,
                )
                for fk in item.foreign_keys
            ]
            tables.append(
                TableV1(
                    id=table_ids[plan.name],
                    name=plan.name,
                    description=plan.description,
                    columns=columns,
                    primary_key=[own[name] for name in item.primary_key],
                    uniques=[[own[name] for name in group] for group in item.uniques],
                    foreign_keys=fks,
                    requirement_ids=plan.requirement_ids,
                    function_ids=plan.function_ids,
                    provenance=plan.provenance,
                    proposal_reason=plan.proposal_reason,
                    answer_question_id=plan.answer_question_id,
                    answer_version=plan.answer_version,
                )
            )
        except (KeyError, ValidationError):
            raise _invalid_reference("테이블 속성이 존재하지 않는 컬럼·테이블을 참조합니다.") from None
    rules = list(previous.data_model.rules) if previous else []
    rule_sequence = _next_id("C-RULE-", [rule.id for rule in rules])
    try:
        for plan in rule_plans:
            rules.append(RuleV1(id=f"C-RULE-{rule_sequence}", **plan.model_dump(exclude_none=True)))
            rule_sequence += 1
        return DataDesignOutput(data_model=DataModelV1(tables=tables, rules=rules), rationale=rationale)
    except ValidationError:
        raise _invalid_reference("데이터 설계가 최종 모델 계약과 일치하지 않습니다.") from None


async def design_data(
    stage_input: DataDesignInput,
    context: Context,
    *,
    action: DataDesignAction | None = None,
    validate_structure: bool = True,
) -> tuple[DataDesignOutput, ModelCall]:
    """full/cover/fix를 수행하고 실패나 불완전한 부분 결과를 반환하지 않는다."""
    issues = validate_function_design(
        FunctionDesignInput(analysis=stage_input.analysis, answers=stage_input.answers), stage_input.functions
    )
    if issues:
        raise failure("invalid_answer_snapshot", "동결 답변이 질문과 일치하지 않습니다.", "data_design", issues=issues)
    action = action or DataDesignAction()
    if action.mode == "full":
        raw, call = await model_client.call_model(
            json.dumps(stage_input.model_dump(mode="json"), ensure_ascii=False),
            "data_design",
            CatalogDraft,
            context,
            CATALOG_PROMPT,
        )
        try:
            value = json.loads(raw)
            # 기존 단일 출력 형식의 호출·테스트와의 호환 경계.
            if isinstance(value, dict) and "data_model" in value:
                output = DataDesignOutput.model_validate(value)
                calls = [call]
            else:
                catalog = CatalogDraft.model_validate(value)
                _check_plans(catalog.tables)
                attr, attr_calls = await _attributes(catalog.tables, catalog.tables, stage_input, context, {}, [])
                output = _assemble(catalog.tables, attr, None, catalog.rules, catalog.rationale)
                calls = [call, *attr_calls]
        except (ValidationError, ValueError, TypeError):
            raise _invalid_output("데이터 모델 카탈로그가 단계 스키마와 일치하지 않습니다.") from None
        covered = {function_id for table in output.data_model.tables for function_id in table.function_ids}
        missing = sorted({item.id for item in stage_input.functions.functions} - covered)
        if missing:
            output, cover_call = await design_data(
                stage_input,
                context,
                action=DataDesignAction(mode="cover", scope=missing, previous=output),
                validate_structure=False,
            )
            calls.append(cover_call)
        return (_check_output(stage_input, output) if validate_structure else output), _aggregate_calls(calls)

    previous = action.previous
    old_tables = previous.data_model.tables
    old_by_name = {table.name: table for table in old_tables}
    old_names_by_id = {table.id: table.name for table in old_tables}
    plans = [_plan_from_table(table, old_names_by_id) for table in old_tables]
    if action.mode == "fix":
        targets = {table.name for table in old_tables if table.id in action.scope}
        if len(targets) != len(action.scope):
            raise _invalid_reference("수정 대상 테이블 ID가 이전 모델에 없습니다.")
        selected = [plan for plan in plans if plan.name in targets]
        attr, calls = await _attributes(selected, plans, stage_input, context, old_by_name, action.issues)
        output = _assemble(plans, attr, previous, [], [*previous.rationale, "지정 테이블의 속성을 재검토함"])
        return (_check_output(stage_input, output) if validate_structure else output), _aggregate_calls(calls)

    known_functions = {item.id: item for item in stage_input.functions.functions}
    if not set(action.scope) <= known_functions.keys():
        raise _invalid_reference("보완 대상 기능 ID가 현재 기능 명세에 없습니다.")
    payload = {
        "scope": action.scope,
        "functions": [known_functions[item].model_dump(mode="json") for item in action.scope],
        "requirements": stage_input.analysis.model_dump(mode="json"),
        "answers": stage_input.answers.model_dump(mode="json"),
        "previous": previous.model_dump(mode="json"),
        "issues": action.issues,
    }
    coverage, call = await _call(payload, CoverageDraft, context, COVER_PROMPT)
    mappings = {item.function_id: item for item in coverage.mappings}
    if len(mappings) != len(coverage.mappings) or set(mappings) != set(action.scope):
        raise _invalid_reference("기능 보완 결과가 대상 기능 전체를 정확히 다루지 않습니다.")
    _check_plans(coverage.new_tables, set(old_by_name))
    new_names = {item.name for item in coverage.new_tables}
    touched = set()
    for item in coverage.mappings:
        if (
            len(set(item.table_names)) != len(item.table_names)
            or not set(item.table_names) <= set(old_by_name) | new_names
        ):
            raise _invalid_reference("기능 보완 결과가 없는 테이블을 참조합니다.")
        touched.update(item.table_names)
    for plan in plans:
        for item in coverage.mappings:
            if plan.name in item.table_names and item.function_id not in plan.function_ids:
                plan.function_ids.append(item.function_id)
                plan.requirement_ids = list(
                    dict.fromkeys([*plan.requirement_ids, *known_functions[item.function_id].requirement_ids])
                )
    for plan in coverage.new_tables:
        mapped_functions = {item.function_id for item in coverage.mappings if plan.name in item.table_names}
        if plan.name not in touched or set(plan.function_ids) != mapped_functions:
            raise _invalid_reference("새 테이블이 대상 기능과 연결되지 않았습니다.")
    all_plans = [*plans, *coverage.new_tables]
    selected = [plan for plan in all_plans if plan.name in touched]
    attr, attr_calls = await _attributes(selected, all_plans, stage_input, context, old_by_name, action.issues)
    output = _assemble(all_plans, attr, previous, [], [*previous.rationale, *coverage.rationale])
    return (_check_output(stage_input, output) if validate_structure else output), _aggregate_calls([call, *attr_calls])
