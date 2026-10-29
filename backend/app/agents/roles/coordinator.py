"""조정 Agent: 현재 상태와 이슈를 보고 다음 작업·담당 Agent·수정 범위를 결정한다.

- 판단은 허용된 행동 목록(CoordinatorDecision.action)으로 제한된 구조화 결과다.
- baseline 정책은 규칙 기반이다. 고정 순서가 아니라 '어떤 이슈가 열려 있는가'에 따라 행동과 범위를 고른다.
    원문 근거 부족        → 문서분석 Agent 재검색 (해당 요구사항만)
    추출 실패 단위        → 요구사항 Agent 재추출 (해당 단위만)
    분류 검토 전 요구사항 → 요구사항 검토 Agent (기능/비기능·시스템/수행 의무 검증, 기능 명세·확인 질문보다 먼저)
    차단 질문 대기        → 사용자 확인 질문
    요구사항 변경·누락    → 기능 명세 Agent 부분 재설계 (해당 그룹만)
    기능 변경·데이터 누락 → 데이터 모델 Agent 연결 보완 (해당 기능만)
    엔티티 다시 설계 요청 → 데이터 모델 Agent 부분 재설계 (rerun --entities·--feedback, 해당 엔티티만)
    기능 다시 설계 요청   → 기능 명세 Agent 부분 재설계 (rerun function_designer, 해당 그룹만. 데이터 설계 전에 멈춤)
    요구사항 의견 반영    → 요구사항 Agent 의견 반영 (rerun requirements_analyst, 바뀌는·빼는·새 요구사항만. 기능 명세 전에 멈춤)
    단계에서 멈추기       → options.stop_after(run --until, rerun의 단계)보다 뒤 단계는 고르지 않는다 (designs)
    모델·DB 검증 오류     → 데이터 모델 Agent 수정 (해당 엔티티만)
    산출물 변경 후        → 추적 검토 Agent → 의미 검토 Agent (바뀐 부분만, 보고만 하고 자동 수정하지 않음)
- 이슈별 시도 횟수·전체 단계 수에 한도를 두고, 넘으면 미해결 이슈를 보존한 채 완료 조건 검사로 넘긴다.
- 반복 실패로 포기한 단계(failures ≥ MAX_FIX_ATTEMPTS)는 할 일이 남아 있어도 다시 고르지 않는다 (stuck_stages).
- LLM 기반 정책으로 바꿀 때도 decide()의 입출력(CoordinatorDecision)만 유지하면 된다.
"""
from __future__ import annotations

import logging

from app.core.config import get_settings
from app.schemas.design import CoordinatorDecision, Question
from app.agents.roles import requirement_reviewer
from app.agents.flow.state import (AgentState, actionable_issues, functions_stale, get_data_model, issues_of, load,
                                   model_stale, rev, review_stale, semantic_stale_parts)

log = logging.getLogger(__name__)
MAX_QUESTIONS_PER_ROUND = 5
REQUIREMENT_STAGES = ("requirements", "requirement_reviewer")   # 기능 명세 초안 전까지 묻지 않는 질문 단계


def decide(state: AgentState) -> tuple[CoordinatorDecision, list[str]]:
    """(결정, 이번 결정으로 처리를 시도하는 issue_id 목록)."""
    s = get_settings()
    step = (state.get("step") or 0) + 1
    failures = state.get("failures") or {}
    options = state.get("options") or {}

    def d(action, reason, mode="", scope=None):
        return CoordinatorDecision(step=step, action=action, mode=mode, scope=scope or [], reason=reason)

    def gave_up(stage: str) -> bool:
        return failures.get(stage, 0) >= s.max_fix_attempts

    stale_review = review_stale(state)

    def open_for(agent: str):
        # 검토 결과(TRACE_*)는 산출물이 바뀐 뒤 검토가 다시 돌기 전까지는 낡은 정보이므로 보류
        return [i for i in actionable_issues(state, agent) if not (stale_review and i.kind.startswith("TRACE_"))]

    if step > s.max_steps:
        return d("check_completion", f"최대 단계 수({s.max_steps}) 도달 → 현재 초안으로 완료 조건 검사"), []
    if not state.get("blocks"):
        if gave_up("document_analyst"):
            return d("check_completion", "문서 분석 반복 실패 → 중단"), []
        return d("document_analyst", "문서가 아직 분석되지 않음", "full"), []
    if not state.get("sources"):
        return d("check_completion", "요구사항 추출 단위가 없음"), []

    issues = open_for("document_analyst")
    if issues:
        return d("document_analyst", f"원문 근거가 확인되지 않은 요구사항 {len(issues)}건 → 관련 영역 재검색",
                 "research", [i.target_id for i in issues]), [i.issue_id for i in issues]

    r = rev(state)
    if r["requirements"] == 0:
        if gave_up("requirements_analyst"):
            return d("check_completion", "요구사항 추출 반복 실패"), []
        return d("requirements_analyst", "요구사항이 아직 추출되지 않음", "full"), []
    issues = open_for("requirements_analyst")
    if issues:
        return d("requirements_analyst", f"요구사항이 추출되지 않은 원문 단위 {len(issues)}개 → 해당 단위 재추출",
                 "fix", [i.target_id for i in issues]), [i.issue_id for i in issues]
    if state.get("requirement_targets") is not None and not gave_up("requirements_analyst"):
        return d("requirements_analyst", "사용자 의견 반영 (바뀌는·빼는·새 요구사항만)", "redesign",
                 state["requirement_targets"]), []

    # 요구사항 검토(분류·충돌): 분류는 기능 명세 범위를, 충돌은 설계 방향을 정하므로 기능 명세 전에,
    # 분류·충돌 확인 질문도 다른 질문과 함께 묻도록 질문 전에 한다
    review_todo = requirement_reviewer.pending(state)
    if review_todo and not gave_up("requirement_reviewer"):
        return d("requirement_reviewer", f"{' · '.join(review_todo)} → 요구사항 검토 (원문 대조·충돌)"), []

    if options.get("ask_mode", "ask") == "ask":
        pending = [q for q in load(state, "questions", Question) if q.status == "PENDING" and q.blocking]
        if designs_functions(state) and not state.get("functions"):
            # 요구사항·분류·충돌 질문은 기능 명세 초안이 나온 뒤 기능 명세 질문과 한 번에 묻는다 (그동안은 추천안·가정으로 설계)
            pending = [q for q in pending if q.stage not in REQUIREMENT_STAGES]
        if pending:
            batch = pending[:MAX_QUESTIONS_PER_ROUND]
            return d("ask_user", f"설계에 영향을 주는 확인 질문 {len(pending)}건", "",
                     [q.question_id for q in batch]), []

    ftargets = state.get("function_targets")
    if designs_functions(state) and ftargets is not None and not gave_up("function_designer"):
        reason = ("기능 명세 전체 다시 만들기" if "*" in ftargets else
                  f"지정 요구사항 {len(ftargets)}건의 기능 그룹만 다시 설계" if ftargets else
                  "사용자 의견과 관련된 기능 그룹만 다시 설계")
        return d("function_designer", reason, "redesign", ftargets), []
    if designs_functions(state) and functions_stale(state) and not gave_up("function_designer"):
        dirty = state.get("dirty_requirements") or []
        if state.get("functions") and dirty:
            return d("function_designer", f"변경된 요구사항 {len(dirty)}건 → 관련 기능만 부분 재설계",
                     "partial", dirty), []
        return d("function_designer", "기능 명세가 아직 없음", "full"), []
    issues = open_for("function_designer") if designs_functions(state) else []
    if issues:
        return d("function_designer", f"기능이 연결되지 않은 요구사항 {len(issues)}건 → 해당 그룹 재설계",
                 "partial", [i.target_id for i in issues]), [i.issue_id for i in issues]

    if state.get("functions") and designs_data(state):
        targets = state.get("model_targets")
        if targets is not None and get_data_model(state) is not None and not gave_up("data_modeler"):
            if "*" in targets:
                return d("data_modeler", "데이터 설계 전체 다시 만들기 (사용자가 고친 엔티티는 유지)", "full", ["*"]), []
            reason = (f"지정 엔티티 {len(targets)}개만 컬럼 다시 설계" if targets else
                      "사용자 의견과 관련된 엔티티만 다시 설계 (사용자 수정 반영)")
            return d("data_modeler", reason, "redesign", targets), []
        if state.get("model_redesign") and not gave_up("data_modeler"):
            return d("data_modeler", "데이터 설계 질문에 추천과 다른 답 → 데이터 모델 다시 설계", "full"), []
        if get_data_model(state) is None:
            if not gave_up("data_modeler"):
                return d("data_modeler", "데이터 모델이 아직 없음", "full"), []
        elif model_stale(state) and not gave_up("data_modeler"):
            dirty = state.get("dirty_functions") or []
            return d("data_modeler", f"변경된 기능 {len(dirty)}건 → 엔티티 연결 보완", "cover", dirty), []
    issues = open_for("data_modeler")
    if issues:
        fix = [i for i in issues if i.kind.startswith(("MODEL_", "DB_"))]
        if fix:
            return d("data_modeler", f"모델·DB 검증 오류 {len(fix)}건 → 해당 엔티티 수정",
                     "fix", sorted({i.target_id for i in fix})), [i.issue_id for i in fix]
        return d("data_modeler", f"저장 엔티티가 없는 기능 {len(issues)}건 → 엔티티 연결 보완",
                 "cover", [i.target_id for i in issues]), [i.issue_id for i in issues]

    if review_stale(state) and not gave_up("trace_reviewer"):
        return d("trace_reviewer", "기능 명세·데이터 모델이 변경됨 → 추적 관계·누락 검토"), []
    parts = semantic_stale_parts(state)
    if parts and semantic_enabled(state) and not gave_up("semantic_reviewer"):
        labels = {"functions": "기능 명세", "model": "데이터 모델"}
        mode = "full" if len(parts) == 2 else "partial"
        return d("semantic_reviewer", f"의미 검토 전인 산출물 → {', '.join(labels[p] for p in parts)} 내용 검토",
                 mode, parts), []
    stuck = stuck_stages(state)
    if stuck:
        names = ", ".join(STAGE_LABELS[a] for a in stuck)
        return d("check_completion", f"{names} Agent 반복 실패({s.max_fix_attempts}회) → 더 진행할 수 없어 완료 조건 검사"), []
    return d("check_completion", "남은 작업 없음 → 완료 조건 검사"), []


STAGE_LABELS = {"requirements_analyst": "요구사항", "requirement_reviewer": "요구사항 검토",
                "function_designer": "기능 명세", "data_modeler": "데이터 모델",
                "trace_reviewer": "추적 검토", "semantic_reviewer": "의미 검토"}


STAGE_ORDER = ("requirements_analyst", "function_designer", "data_modeler")   # 단계별 버전의 단계 순서


def designs(state: AgentState, stage: str) -> bool:
    """이 실행이 그 단계까지 하는지. options.stop_after(run --until, rerun의 단계)가 그 앞 단계면 거기서 멈춘다
    (팀 규칙: 단계 버전을 확정한 뒤 다음 단계를 그 버전에서 시작한다)."""
    stop = (state.get("options") or {}).get("stop_after")
    return stop not in STAGE_ORDER or STAGE_ORDER.index(stage) <= STAGE_ORDER.index(stop)


def designs_functions(state: AgentState) -> bool:
    """이 실행이 기능 명세까지 하는지 (요구사항 단계 버전은 요구사항에서 멈춘다)."""
    return designs(state, "function_designer")


def designs_data(state: AgentState) -> bool:
    """이 실행이 데이터 설계까지 하는지. 기능 명세 단계 다시 만들기(rerun function_designer)는 기능 명세에서 멈춘다
    (팀 규칙: 기능 명세 버전을 확정한 뒤 rerun data_modeler --from 그 버전으로 데이터 설계를 한다)."""
    return designs(state, "data_modeler")


def pending_stages(state: AgentState) -> list[str]:
    """만들거나 다시 해야 할 산출물이 남은 단계 (Agent 이름)."""
    pending = []
    if state.get("requirement_targets") is not None:
        pending.append("requirements_analyst")
    if requirement_reviewer.pending(state):
        pending.append("requirement_reviewer")
    if designs_functions(state) and (functions_stale(state) or state.get("function_targets") is not None):
        pending.append("function_designer")
    if state.get("functions") and designs_data(state) and (
            get_data_model(state) is None or model_stale(state) or state.get("model_redesign")
            or state.get("model_targets") is not None):
        pending.append("data_modeler")
    if review_stale(state):
        pending.append("trace_reviewer")
    if semantic_enabled(state) and semantic_stale_parts(state):
        pending.append("semantic_reviewer")
    return pending


def stuck_stages(state: AgentState) -> list[str]:
    """할 일이 남았지만 반복 실패(MAX_FIX_ATTEMPTS)로 포기한 단계. 조정 Agent는 이 단계를 다시 고르지 않는다."""
    failures = state.get("failures") or {}
    limit = get_settings().max_fix_attempts
    return [a for a in pending_stages(state) if failures.get(a, 0) >= limit]


def semantic_enabled(state: AgentState) -> bool:
    """의미 검토 Agent를 쓰는지 (run --no-semantic-review 또는 SEMANTIC_REVIEW=false면 끔)."""
    return bool((state.get("options") or {}).get("semantic_review", get_settings().semantic_review))


def run(state: AgentState) -> dict:
    """조정 Agent 노드: decide() 결과를 상태에 기록하고, 처리를 시도하는 이슈의 시도 횟수를 올린다."""
    decision, issue_ids = decide(state)
    log.info("[조정 #%d] %s%s — %s", decision.step, decision.action,
             f"({decision.mode})" if decision.mode else "", decision.reason)
    update: dict = {
        "step": decision.step,
        "next_action": decision.model_dump(),
        "decisions": [*(state.get("decisions") or []), decision.model_dump()],
    }
    if issue_ids:
        targeted = set(issue_ids)
        update["issues"] = [
            (i.model_copy(update={"attempts": i.attempts + 1}) if i.issue_id in targeted else i).model_dump()
            for i in issues_of(state)
        ]
    return update
