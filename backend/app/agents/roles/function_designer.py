"""요구사항과 동결 답변에서 기능 명세를 만드는 전문 역할."""

import json

from pydantic import ValidationError

from app.agents.flow.context import Context
from app.agents.flow.errors import failure
from app.agents.llm import client as model_client
from app.agents.modeling.validation import validate_answer_snapshot, validate_function_design
from app.agents.prompts.function_designer import DESIGN_FUNCTIONS
from app.schemas.design import ModelCall
from app.schemas.design_v1 import FunctionDesignInput, FunctionDesignOutput


async def design_functions(
    stage_input: FunctionDesignInput, context: Context
) -> tuple[FunctionDesignOutput, ModelCall]:
    """답변 스냅샷과 기능 참조가 맞는 완전한 결과만 돌려준다."""
    issues = validate_answer_snapshot(stage_input.analysis.questions, stage_input.answers)
    if issues:
        raise failure(
            "invalid_answer_snapshot", "동결 답변이 질문과 일치하지 않습니다.", "function_design", issues=issues
        )
    raw, call = await model_client.call_model(
        json.dumps(stage_input.model_dump(mode="json"), ensure_ascii=False),
        "function_design",
        FunctionDesignOutput,
        context,
        DESIGN_FUNCTIONS,
    )
    try:
        output = FunctionDesignOutput.model_validate_json(raw)
    except ValidationError:
        raise failure(
            "invalid_function_output", "기능 설계 결과가 단계 스키마와 일치하지 않습니다.", "function_design"
        ) from None
    issues = validate_function_design(stage_input, output)
    required = {requirement.id for requirement in stage_input.analysis.requirements if requirement.kind == "functional"}
    covered = {requirement_id for function in output.functions for requirement_id in function.requirement_ids}
    if missing := sorted(required - covered):
        issues.append(f"functions: uncovered functional requirements {', '.join(missing)}")
    if issues:
        raise failure(
            "invalid_function_reference",
            "기능 설계의 요구사항·답변 참조가 잘못되었습니다.",
            "function_design",
            issues=issues,
        )
    return output, call
