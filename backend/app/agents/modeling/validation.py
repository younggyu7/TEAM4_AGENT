"""단계 입력·출력의 결정적 참조 검증."""

from collections import Counter

from app.schemas.design_v1 import (
    AnalysisInput,
    AnalysisOutput,
    AnswerSnapshot,
    DataDesignInput,
    DataDesignOutput,
    FunctionDesignInput,
    FunctionDesignOutput,
    QuestionV1,
)
from app.schemas.document import DocumentBlock, EvidenceReference


def _duplicates(values: list[str]) -> set[str]:
    return {value for value, count in Counter(values).items() if count > 1}


def _evidence_issue(reference: EvidenceReference, blocks: dict[tuple[str, str], DocumentBlock]) -> bool:
    block = blocks.get((reference.document_revision_id, reference.block_id))
    return block is None or not reference.matches(block)


def validate_analysis_output(stage_input: AnalysisInput, output: AnalysisOutput) -> list[str]:
    """블록 ID, 정확한 인용 구간, 고객 요구·질문 참조를 검사한다."""
    issues = []
    block_index = {(stage_input.document_revision_id, block.block_id): block for block in stage_input.blocks}
    requirement_ids = [requirement.id for requirement in output.requirements]
    if _duplicates(requirement_ids):
        issues.append("requirements: duplicate id")
    question_ids = [question.id for question in output.questions]
    if _duplicates(question_ids):
        issues.append("questions: duplicate id")
    for index, requirement in enumerate(output.requirements):
        if requirement.provenance == "user_answer":
            issues.append(f"requirements[{index}]: user_answer cannot originate in analysis")
        if any(_evidence_issue(reference, block_index) for reference in requirement.evidence):
            issues.append(f"requirements[{index}]: invalid evidence reference")
    for index, question in enumerate(output.questions):
        if question.status != "open":
            issues.append(f"questions[{index}]: analysis question must be open")
        if _duplicates(question.requirement_ids) or not set(question.requirement_ids) <= set(requirement_ids):
            issues.append(f"questions[{index}]: invalid requirement reference")
        if any(_evidence_issue(reference, block_index) for reference in question.evidence):
            issues.append(f"questions[{index}]: invalid evidence reference")
    return issues


def validate_answer_snapshot(questions: list[QuestionV1], snapshot: AnswerSnapshot) -> list[str]:
    """진행 시점의 각 질문에 답변 또는 명시적 미해결 표지가 하나씩 있어야 한다."""
    issues = []
    question_ids = [question.id for question in questions]
    answer_ids = [answer.question_id for answer in snapshot.answers]
    if _duplicates(question_ids):
        issues.append("questions: duplicate id")
    if _duplicates(answer_ids):
        issues.append("answers: duplicate question_id")
    if set(question_ids) != set(answer_ids):
        issues.append("answers: question coverage mismatch")
    if any(answer.created_at > snapshot.frozen_at for answer in snapshot.answers):
        issues.append("answers: created after freeze")
    if sum(len(answer.text or "") for answer in snapshot.answers) > 10000:
        issues.append("answers: total text limit exceeded")
    return issues


def _invalid_answer_source(item, snapshot: AnswerSnapshot) -> bool:
    if item.provenance != "user_answer":
        return False
    answer = next((answer for answer in snapshot.answers if answer.question_id == item.answer_question_id), None)
    return answer is None or answer.unresolved or answer.version != item.answer_version


def validate_function_design(stage_input: FunctionDesignInput, output: FunctionDesignOutput) -> list[str]:
    issues = validate_answer_snapshot(stage_input.analysis.questions, stage_input.answers)
    requirement_ids = {requirement.id for requirement in stage_input.analysis.requirements}
    for index, requirement in enumerate(stage_input.analysis.requirements):
        if _invalid_answer_source(requirement, stage_input.answers):
            issues.append(f"requirements[{index}]: invalid answer reference")
    function_ids = [function.id for function in output.functions]
    if _duplicates(function_ids):
        issues.append("functions: duplicate id")
    for index, function in enumerate(output.functions):
        if _duplicates(function.requirement_ids) or not set(function.requirement_ids) <= requirement_ids:
            issues.append(f"functions[{index}]: invalid requirement reference")
        if _invalid_answer_source(function, stage_input.answers):
            issues.append(f"functions[{index}]: invalid answer reference")
    return issues


def validate_data_design(stage_input: DataDesignInput, output: DataDesignOutput) -> list[str]:
    """단계 경계의 ID·참조 검사. 키·타입·FK 의미 검사는 D의 design_model이 소유한다."""
    issues = validate_function_design(
        FunctionDesignInput(analysis=stage_input.analysis, answers=stage_input.answers), stage_input.functions
    )
    requirement_ids = {requirement.id for requirement in stage_input.analysis.requirements}
    function_ids = {function.id for function in stage_input.functions.functions}
    tables = output.data_model.tables
    table_ids = [table.id for table in tables]
    if _duplicates(table_ids) or _duplicates([table.name for table in tables]):
        issues.append("tables: duplicate id or name")
    columns_by_table = {table.id: {column.id for column in table.columns} for table in tables}
    all_column_ids = [column.id for table in tables for column in table.columns]
    if _duplicates(all_column_ids):
        issues.append("columns: duplicate id")
    for index, table in enumerate(tables):
        own_columns = columns_by_table[table.id]
        if _duplicates([column.name for column in table.columns]):
            issues.append(f"tables[{index}]: duplicate column name")
        if (
            _duplicates(table.requirement_ids)
            or _duplicates(table.function_ids)
            or not set(table.requirement_ids) <= requirement_ids
            or not set(table.function_ids) <= function_ids
        ):
            issues.append(f"tables[{index}]: invalid design reference")
        if _invalid_answer_source(table, stage_input.answers):
            issues.append(f"tables[{index}]: invalid answer reference")
        if not set(table.primary_key) <= own_columns or any(not set(group) <= own_columns for group in table.uniques):
            issues.append(f"tables[{index}]: invalid key column reference")
        for fk_index, foreign_key in enumerate(table.foreign_keys):
            if (
                not set(foreign_key.source_column_ids) <= own_columns
                or foreign_key.target_table_id not in columns_by_table
                or not set(foreign_key.target_column_ids) <= columns_by_table.get(foreign_key.target_table_id, set())
            ):
                issues.append(f"tables[{index}].foreign_keys[{fk_index}]: invalid column reference")
    if _duplicates([rule.id for rule in output.data_model.rules]):
        issues.append("rules: duplicate id")
    for index, rule in enumerate(output.data_model.rules):
        if _duplicates(rule.requirement_ids) or not set(rule.requirement_ids) <= requirement_ids:
            issues.append(f"rules[{index}]: invalid requirement reference")
        if _invalid_answer_source(rule, stage_input.answers):
            issues.append(f"rules[{index}]: invalid answer reference")
    return issues
