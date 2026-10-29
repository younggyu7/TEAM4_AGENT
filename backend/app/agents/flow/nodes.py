"""흐름도의 Agent 외 노드: 접수, 결과 검사, 질문·답변 기록, 완료 조건 검사, 산출물 제공, DB 검증."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from langgraph.types import interrupt

from app.core.config import get_settings
from app.schemas.design import (USER_SOURCE, DbValidationResult, DesignVersion, DocumentBlock, FunctionSpec, Question,
                                Requirement, RequirementSource)
from app.agents.roles.coordinator import (STAGE_LABELS, designs_data, designs_functions, pending_stages, semantic_enabled,
                                          stuck_stages)
from app.agents.roles.function_designer import mark_decided, record_answers
from app.agents.roles.requirement_reviewer import record_answer as record_review_answer, settle
from app.agents.flow.state import (SEMANTIC_BASIS_INIT, AgentState, actionable_issues, dump, finding, get_data_model, issues_of, load,
                                   reconcile_issues, rev, review_stale, semantic_stale_parts, with_result)
from app.agents.tools.convention import apply_profile, ddl_model, profile_of
from app.agents.tools.db_validator import validate_ddl
from app.agents.tools.document_parser import check_supported, file_fingerprint
from app.agents.tools.exporter import export_db_report, export_design
from app.agents.tools.model_validator import USER_EDITED_NOTE, validate_model
from app.agents.tools.search import quote_in_text
from app.agents.tools.versions import claim_next_version, latest_top_level, load_versions, project_of, save_version, update_version

log = logging.getLogger(__name__)


def _issue_dicts(issues) -> list[dict]:
    """Issue 목록을 상태용 dict 목록으로 바꾼다."""
    return [i.model_dump() for i in issues]


# ------------------------------------------------------------------ 문서 업로드·프로젝트 생성
def intake(state: AgentState) -> dict:
    """문서 업로드·프로젝트 생성: 파일 확인, 문서 ID·버전 부여, 실행 상태 초기화."""
    path = Path(state["source_path"])
    if not path.exists():
        raise FileNotFoundError(path)
    check_supported(path)   # 지원하지 않는 형식이면 변환 안내와 함께 중단
    document_id, version = file_fingerprint(path)
    log.info("[접수] 실행 %s · 문서 %s (%s)", state["run_id"], path.name, document_id)
    return {
        "document": {"document_id": document_id, "document_version": version, "file_name": path.name},
        "blocks": [], "sources": [], "requirements": [], "questions": [], "functions": [], "data_model": None,
        "trace_links": [], "issues": [], "decisions": [], "versions": [], "failures": {}, "llm_usage": {},
        "rev": {"requirements": 0, "functions": 0, "model": 0},
        "basis": {"functions": -1, "model": -1, "review": [-1, -1], "semantic": dict(SEMANTIC_BASIS_INIT)},
        "semantic_review": {},
        "dirty_requirements": [], "dirty_functions": [], "model_redesign": False, "design_version": "", "step": 0,
        "db_attempts": 0,
        "db_validation": None, "completion": {}, "pending_answers": {},
    }


# ------------------------------------------------------------------ 결과 검사·공통 상태 갱신
def inspect_result(state: AgentState) -> dict:
    """방금 실행한 전문 Agent의 결과를 결정적으로 검사하고 이슈 목록을 최신 상태로 맞춘다."""
    max_attempts = get_settings().fix_attempts
    last = state.get("next_action") or {}
    issues = issues_of(state)
    update: dict = {"last_action": last}
    requirements = load(state, "requirements", Requirement)
    questions = load(state, "questions", Question)

    if rev(state)["requirements"] > 0:
        blocks = {b.source_block_id: b for b in load(state, "blocks", DocumentBlock)}
        ev_findings = []
        for r in requirements:
            for ev in r.evidence:
                block = blocks.get(ev.source_block_id)
                ev.verified = bool(block and quote_in_text(ev.quote, block.text))
            # 사용자 의견으로 추가한 요구사항은 원문 근거가 없는 것이 맞다 (근거는 결정에 남은 의견)
            if r.source_key != USER_SOURCE and not any(ev.verified for ev in r.evidence):
                ev_findings.append(finding("EVIDENCE_NOT_FOUND", "WARNING",
                                           f"{r.requirement_id} '{r.title}'의 인용문이 원문에서 확인되지 않음",
                                           "document_analyst", r.requirement_id, "inspect_result"))
        extracted = {r.source_key for r in requirements}
        src_findings = []
        for s in load(state, "sources", RequirementSource):
            if s.source_key in extracted:
                continue
            if s.code:   # 요구사항 상세 표는 반드시 요구사항이 나와야 한다 → 재추출 대상
                src_findings.append(finding("REQ_SOURCE_EMPTY", "ERROR",
                                            f"요구사항 표 {s.source_key} '{s.name}'에서 추출된 요구사항이 없음",
                                            "requirements_analyst", s.source_key, "inspect_result"))
            else:        # 본문 청크는 요구사항이 없을 수 있다 (사업 개요·일반사항 등) → 보고만
                src_findings.append(finding("REQ_SOURCE_NO_REQUIREMENT", "INFO",
                                            f"본문 단위 {s.source_key} '{s.name}'에서 요구사항을 찾지 못함",
                                            target_id=s.source_key, raised_by="inspect_result"))
        issues = reconcile_issues(issues, ev_findings, lambda k: k == "EVIDENCE_NOT_FOUND", max_attempts)
        issues = reconcile_issues(issues, src_findings, lambda k: k.startswith("REQ_SOURCE_"), max_attempts)
        update["requirements"] = dump(requirements)

        # 질문 정리: defer 모드이거나 차단 질문이 아니면 보류(가정으로 진행)로 기록
        ask = (state.get("options") or {}).get("ask_mode", "ask") == "ask"
        for q in questions:
            if q.status == "PENDING" and (not ask or not q.blocking):
                q.status = "DEFERRED"
        update["questions"] = dump(questions)

    if state.get("data_model") is not None:
        issues = reconcile_issues(issues, validate_model(get_data_model(state)),
                                  lambda k: k.startswith("MODEL_"), max_attempts)
    if last.get("action") == "data_modeler":
        # DB 검증 오류는 수정 후 다음 DB 검증에서 다시 판정한다 (재발 시 시도 횟수를 이어서 센다)
        issues = [i.model_copy(update={"status": "RESOLVED"})
                  if i.kind.startswith("DB_") and i.status == "OPEN" and i.attempts > 0 else i for i in issues]

    update["issues"] = _issue_dicts(issues)
    decisions = state.get("decisions") or []
    prev = decisions[-1].get("result", "") if decisions else ""
    if not prev.startswith("실패"):   # 전문 Agent가 통째로 실패했으면 그 기록을 그대로 둔다
        summary = _step_result(state, last, issues, requirements, questions)
        update["decisions"] = with_result(state, f"{summary} · {prev}" if prev else summary)
    open_errors = sum(1 for i in issues if i.status == "OPEN" and i.severity == "ERROR")
    log.info("[결과 검사] %s 완료 → 미해결 오류 %d건", last.get("action"), open_errors)
    return update


def _step_result(state: AgentState, last: dict, issues: list, requirements: list[Requirement],
                 questions: list[Question]) -> str:
    """방금 실행한 전문 Agent의 결과를 판단 기록의 '결과' 한 줄로 요약한다."""
    action, mode = last.get("action"), last.get("mode")

    def open_count(prefix: str, severity: str | None = None) -> int:
        return sum(1 for i in issues if i.kind.startswith(prefix) and i.status == "OPEN"
                   and (severity is None or i.severity == severity))

    def extra(*pairs: tuple[str, int]) -> str:
        return "".join(f", {label} {n}건" for label, n in pairs if n)

    if action == "document_analyst" and mode == "research":
        scope = set(last.get("scope") or [])
        found = sum(1 for r in requirements if r.requirement_id in scope and any(ev.verified for ev in r.evidence))
        return f"근거 재검색 {len(scope)}건 → 확인 {found}건"
    if action == "document_analyst":
        blocks = state.get("blocks") or []
        tables = sum(b.get("block_type") == "requirement_table" for b in blocks)
        return (f"블록 {len(blocks)}개 (요구사항 표 {tables}) → 추출 단위 {len(state.get('sources') or [])}개, "
                f"문서 경고 {open_count('DOC_')}건")
    if action == "requirements_analyst":
        functional = sum(r.type == "FUNCTIONAL" for r in requirements)
        return (f"요구사항 {len(requirements)}건 (기능 {functional} / 비기능 {len(requirements) - functional}), "
                f"확인 질문 {len(questions)}건"
                + extra(("미추출 단위", open_count("REQ_SOURCE_EMPTY")), ("근거 미확인", open_count("EVIDENCE_NOT_FOUND"))))
    if action == "function_designer":
        functions = state.get("functions") or []
        changed = f" (변경 {len(state.get('dirty_functions') or [])}개)" if mode != "full" else ""
        return f"기능 {len(functions)}개{changed}"
    if action == "data_modeler":
        model = get_data_model(state)
        if model is None:
            return "데이터 모델 없음"
        return (f"엔티티 {len(model.entities)}개 · 관계 {len(model.relations)}개"
                + extra(("모델 오류", open_count("MODEL_", "ERROR"))))
    if action == "requirement_reviewer":
        return (state.get("requirement_review") or {}).get("last") or "분류 검토"
    if action == "trace_reviewer":
        return (f"추적 링크 {len(state.get('trace_links') or [])}개, "
                f"추적 누락 오류 {open_count('TRACE_', 'ERROR')}건")
    if action == "semantic_reviewer":
        meta = state.get("semantic_review") or {}
        prefixes = {"functions": ("기능 명세", "SEM_FUNC_"), "model": ("데이터 모델", "SEM_MODEL_")}
        counts = [f"{prefixes[p][0]} {open_count(prefixes[p][1])}건" for p in meta.get("parts", []) if p in prefixes]
        text = f"지적 {', '.join(counts)}" if counts else "검토 못 함"
        if meta.get("dropped"):
            text += f" (대상 ID를 확인할 수 없는 지적 {meta['dropped']}건 제외)"
        return text
    return ""


# ------------------------------------------------------------------ 확인 질문 제시·응답 대기 / 사용자 답변·결정 기록
def ask_user(state: AgentState) -> dict:
    """확인 질문 제시·응답 대기: 질문과 원문 근거를 담아 실행을 멈추고(interrupt) 답변을 기다린다."""
    ids = set((state.get("next_action") or {}).get("scope") or [])
    blocks = {b.source_block_id: b for b in load(state, "blocks", DocumentBlock)}
    payload = []
    for q in load(state, "questions", Question):
        if q.question_id not in ids:
            continue
        evidence = [f"{bid} ({blocks[bid].location.label()}): {blocks[bid].text[:200]}"
                    for bid in q.evidence_block_ids if bid in blocks]
        payload.append({"question_id": q.question_id, "kind": q.kind, "question": q.question,
                        "context": q.context, "options": q.options, "requirement_ids": q.requirement_ids,
                        "evidence": evidence, "stage": q.stage, "recommended": q.recommended})
    # 실행을 멈추고(WAITING_USER) 체크포인트에 저장. 재개 시 {question_id: 답변} 이 돌아온다.
    answers = interrupt({"type": "questions", "questions": payload})
    return {"pending_answers": answers if isinstance(answers, dict) else {}}


def record_decision(state: AgentState) -> dict:
    """사용자 답변·결정 기록: 답변을 관련 요구사항에 결정으로 남기고, 바뀐 요구사항을 부분 재설계 대상으로 표시한다.

    데이터 설계 질문(stage=data_modeler)은 이미 추천 선택지로 설계해 두었으므로, 답이 추천과 다를 때만
    데이터 모델을 다시 설계하게 표시한다 (결정은 질문에 남고 데이터 모델 Agent가 프롬프트로 받는다).
    기능 명세 질문(stage=function_designer)도 추천안으로 설계해 두었다. 답은 요구사항 결정으로 남기고,
    다른 답만 관련 기능을 고치게 표시한다 (function_designer.record_answers).
    요구사항 검토 질문(분류·포함 여부 RC-…, 충돌 CF-…)은 답을 받으면 요구사항의 검토 표시를 지운다. 기능 명세 초안은 추천안으로
    만들어 두었으므로 추천과 같은 답이면 규칙 표시만 '(결정 …)'으로 바꾸고, 다른 답이면 그 요구사항을 다시 설계한다.
    """
    ids = set((state.get("next_action") or {}).get("scope") or [])
    answers = state.get("pending_answers") or {}
    questions = load(state, "questions", Question)
    requirements = load(state, "requirements", Requirement)
    by_id = {r.requirement_id: r for r in requirements}
    changed: set[str] = set()
    redesign_model = False
    function_answered: list[Question] = []
    decided: set[str] = set()   # 추천과 같은 답을 받은 요구사항 검토 질문 (기능 규칙 표시만 바꾼다)
    for q in questions:
        if q.question_id not in ids:
            continue
        answer = str(answers.get(q.question_id, "")).strip()
        if not answer:
            q.status = "DEFERRED"
            continue
        q.status, q.answer = "ANSWERED", answer
        if q.stage == "data_modeler":
            redesign_model = redesign_model or answer != q.recommended.strip()
            continue
        if q.stage == "function_designer":
            function_answered.append(q)
            continue
        if q.stage == "requirement_reviewer":   # 분류·포함 여부 질문: 답대로 고치면 기능 명세 범위가 바뀌므로 재설계 대상
            if record_review_answer(q, by_id):
                changed.update(rid for rid in q.requirement_ids if rid in by_id)
            else:
                decided.add(q.question_id)
            continue
        targets = [rid for rid in q.requirement_ids if rid in by_id] or [
            r.requirement_id for r in requirements
            if any(ev.source_block_id in q.evidence_block_ids for ev in r.evidence)]
        # 추천안으로 이미 설계한 질문(충돌 CF-…)에 추천과 같은 답이면 확정 표시만 한다 (다시 설계하지 않음)
        assumed = bool(q.recommended) and answer == q.recommended.strip()
        if assumed:
            decided.add(q.question_id)
        for rid in targets:
            settle(by_id[rid], q.question_id)
            by_id[rid].decisions.append(f"[{q.question_id}] {q.question} → {answer}{' (추천안 확정)' if assumed else ''}")
            by_id[rid].status = "CONFIRMED"
            if not assumed:
                changed.add(rid)
    log.info("[결정 기록] 답변 %d건 → 영향 요구사항 %d건", sum(q.status == "ANSWERED" and q.question_id in ids
                                                     for q in questions), len(changed))
    answered = sum(q.status == "ANSWERED" and q.question_id in ids for q in questions)
    deferred = sum(q.status == "DEFERRED" and q.question_id in ids for q in questions)
    # 기능 명세 질문의 답 (요구사항 결정을 남기므로 요구사항을 저장하기 전에 반영한다)
    function_update = record_answers(state, function_answered, requirements) if function_answered else {}
    result = f"답변 {answered}건 · 보류 {deferred}건 → 영향 요구사항 {len(changed)}건"
    if function_answered:
        result += f" · 기능 명세 질문 {len(function_answered)}건 (다시 설계할 요구사항 " \
                  f"{len(function_update.get('function_targets') or [])}건)"
    update = {"questions": dump(questions), "requirements": dump(requirements), "pending_answers": {},
              "decisions": with_result(state, result), **function_update}
    if decided and (update.get("functions") or state.get("functions")):
        # 규칙 표시만 바뀌므로 기능 명세 수정 횟수는 올리지 않는다 (데이터 설계를 다시 하지 않게)
        functions = [FunctionSpec.model_validate(f) for f in update.get("functions") or state.get("functions")]
        update["functions"] = dump(mark_decided(functions, decided))
    if redesign_model:
        update["model_redesign"] = True
    if changed:
        r = update.get("rev") or rev(state)
        update["rev"] = {**r, "requirements": r["requirements"] + 1}
        update["dirty_requirements"] = sorted(set(state.get("dirty_requirements") or []) | changed)
    return update


# ------------------------------------------------------------------ 완료 조건 검사
def check_completion(state: AgentState) -> dict:
    """완료 조건 검사: 추출·근거·추적·모델 무결성·차단 질문·검토 최신화 조건을 판정한다."""
    s = get_settings()
    issues = issues_of(state)
    requirements = load(state, "requirements", Requirement)
    model = get_data_model(state)
    ask = (state.get("options") or {}).get("ask_mode", "ask") == "ask"
    pending = [q for q in load(state, "questions", Question) if q.status == "PENDING" and q.blocking]

    def remaining(prefix: str, severity: str | None = None) -> list:
        return [i for i in issues if i.kind.startswith(prefix) and i.status != "RESOLVED"
                and (severity is None or i.severity == severity)]

    semantic_pending = semantic_enabled(state) and bool(semantic_stale_parts(state))
    stuck = stuck_stages(state)   # 할 일이 남았지만 반복 실패로 포기한 단계
    sys_func = [r for r in requirements if r.designed]
    # 유지운영 사업처럼 시스템 기능 요구사항이 하나도 없으면 기능·데이터 설계 조건은 '해당 없음'으로 통과
    no_design = bool(requirements) and not sys_func
    na = "해당 없음 (설계 대상 시스템 기능 요구사항 0건)"

    def missing(what: str, agent: str) -> str:
        return f"{what} 없음" + (f" ({STAGE_LABELS[agent]} Agent {s.fix_attempts(agent)}회 실패)" if agent in stuck else "")

    stage_only = not designs_data(state)   # 기능 명세 단계 버전: 데이터 설계는 확정 후 rerun data_modeler로
    requirements_only = not designs_functions(state)   # 요구사항 단계 버전: 기능 명세는 확정 후 rerun function_designer로
    if no_design:
        func_detail = model_detail = trace_detail = na
    else:
        func_detail = (f"시스템 기능 요구사항 {len(sys_func)}건 중 기능 미연결 {len(remaining('TRACE_REQ_WITHOUT_FUNCTION'))}건"
                       if state.get("functions") else missing("기능 명세", "function_designer"))
        trace_detail = (f"엔티티 미연결 기능 {len(remaining('TRACE_FUNC_WITHOUT_ENTITY'))}건"
                        if model is not None else missing("데이터 모델", "data_modeler"))
        model_detail = (f"모델 오류 {len(remaining('MODEL_', 'ERROR'))}건"
                        if model is not None else missing("데이터 모델", "data_modeler"))
        if stage_only:
            trace_detail = model_detail = "해당 없음 (기능 명세 단계 버전 — 데이터 설계는 rerun data_modeler --from 이 버전)"
        if requirements_only:
            func_detail = trace_detail = model_detail = \
                "해당 없음 (요구사항 단계 버전 — 기능 명세는 rerun function_designer --from 이 버전)"
    checks = [
        ("요구사항 추출", bool(requirements) and not remaining("REQ_SOURCE_EMPTY"),
         f"요구사항 {len(requirements)}건, 미추출 단위 {len(remaining('REQ_SOURCE_EMPTY'))}개"),
        ("원문 근거 연결", not remaining("EVIDENCE_NOT_FOUND"),
         f"근거 미확인 {len(remaining('EVIDENCE_NOT_FOUND'))}건"),
        ("요구사항→기능 추적",
         no_design or requirements_only or (bool(state.get("functions")) and not remaining("TRACE_REQ_WITHOUT_FUNCTION")),
         func_detail),
        ("기능→데이터 추적",
         no_design or stage_only or (model is not None and not remaining("TRACE_FUNC_WITHOUT_ENTITY")), trace_detail),
        ("데이터 모델 무결성",
         no_design or stage_only or (model is not None and bool(model.entities) and not remaining("MODEL_", "ERROR")),
         model_detail),
        ("차단 질문 해소", not (ask and pending), f"대기 질문 {len(pending)}건"),
        ("검토 최신화", not review_stale(state) and not semantic_pending, _review_detail(state, semantic_pending)),
    ]
    satisfied = all(passed for _, passed, _ in checks)
    # 더 시도할 작업: 열린 이슈, 차단 질문, 할 일이 남은 단계 중 아직 포기하지 않은 것.
    # 포기한 단계를 넣으면 조정 Agent가 고를 일이 없는데도 '미충족 → 조정 Agent'를 단계 한도까지 되풀이한다.
    retryable = [a for a in pending_stages(state) if a not in stuck]
    actionable = bool(actionable_issues(state)) or bool(retryable) or (ask and bool(pending))
    forced = (state.get("step") or 0) >= s.max_steps
    completion = {
        "satisfied": satisfied, "forced": forced, "actionable": actionable,
        "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in checks],
    }
    passed = sum(p for _, p, _ in checks)
    log.info("[완료 조건] %s (%d/%d 충족)%s", "충족" if satisfied else "미충족", passed, len(checks),
             "" if satisfied or actionable else " — 더 시도할 작업이 없어 미해결 항목을 보존하고 제공")
    if satisfied:
        result = f"충족 ({passed}/{len(checks)}) → 산출물 제공"
    else:
        failed = ", ".join(n for n, p, _ in checks if not p)
        then = ("최대 단계 도달 → 현재 상태로 제공" if forced else
                "다시 조정 Agent로" if actionable else
                f"{', '.join(STAGE_LABELS[a] for a in stuck)} Agent 반복 실패로 더 진행할 수 없음 → 현재 상태로 제공"
                if stuck else "더 시도할 작업 없음 → 현재 상태로 제공")
        result = f"미충족 ({passed}/{len(checks)}: {failed}) → {then}"
    return {"completion": completion, "decisions": with_result(state, result)}


def _review_detail(state: AgentState, semantic_pending: bool) -> str:
    """'검토 최신화' 조건의 상세: 추적 검토와 의미 검토가 최신 산출물을 봤는지, 의미 검토 지적 수."""
    if review_stale(state):
        return "추적 검토 다시 필요"
    if not semantic_enabled(state):
        return "추적 검토가 최신 산출물 기준 (의미 검토 꺼짐)"
    if semantic_pending:
        labels = {"functions": "기능 명세", "model": "데이터 모델"}
        return f"의미 검토 미완료 ({', '.join(labels[p] for p in semantic_stale_parts(state))}) — 미검증"
    remarks = sum(1 for i in issues_of(state) if i.kind.startswith("SEM_") and i.status != "RESOLVED")
    return f"추적·의미 검토가 최신 산출물 기준 (의미 검토 지적 {remarks}건, 담당자 확인)"


def route_after_completion(state: AgentState) -> str:
    """충족했거나 더 시도할 작업이 없으면 제공(met), 아니면 조정 Agent로(unmet)."""
    c = state.get("completion") or {}
    return "met" if c.get("satisfied") or c.get("forced") or not c.get("actionable") else "unmet"


# ------------------------------------------------------------------ 명세서·ERD·DDL·추적표 제공
def deliver(state: AgentState) -> dict:
    """명세서·ERD·DDL·추적표 제공: 프로젝트의 다음 설계 버전 번호로 산출물 파일을 쓰고 버전 기록을 남긴다.

    버전 번호는 실행이 아니라 프로젝트(고객 문서) 단위다. 같은 문서를 다시 실행해도 v2, v3 …으로 이어진다.
    한 단계만 다시 만든 실행(rerun)은 기준 버전 아래 번호를 받는다 (v2 기준 데이터 설계 → v2.1, v2.2 …).
    이전 버전(parent)은 이 실행이 앞서 만든 버전, 없으면 기준 버전, 그것도 없으면 프로젝트의 마지막 최상위 버전이다.
    """
    root, project = get_settings().output_dir, project_of(state)
    options = state.get("options") or {}
    base, stage = options.get("base_version"), options.get("stage", "full")
    previous = load_versions(root, project)
    version, out_dir = claim_next_version(root, project, base)
    versions = list(state.get("versions") or [])
    last_step = versions[-1]["summary_until_step"] if versions else 0
    summary = [f"#{d['step']} {d['action']}({d.get('mode', '')}): {d['reason']}"
               for d in state.get("decisions") or [] if d["step"] > last_step]
    # 요구사항 다시 만들기는 다음 최상위 번호를 받지만 이전 버전은 기준 버전이다 (options.parent_version)
    parent = versions[-1]["design_version"] if versions else (options.get("parent_version") or base
                                                              or latest_top_level(previous))
    # 추적표의 '이전 버전 대비 변경'은 parent 버전 폴더와 비교한다
    files = export_design({**state, "design_version": version, "parent_version": parent}, out_dir, version)
    record = DesignVersion(design_version=version, parent=parent,
                           created_at=datetime.now().isoformat(timespec="seconds"), summary=summary,
                           output_dir=str(out_dir)).model_dump()
    save_version(root, project, {**record, "run_id": state["run_id"], "stage": stage, "base": base,
                                 "until": options.get("stop_after"), "standard": options.get("standard"),
                                 "feedback": (options.get("feedback") or {}).get(stage, ""),
                                 "rework": (state.get("function_rework") if stage == "function_designer" else
                                            state.get("model_rework")) if base else None,
                                 "document": (state.get("document") or {}).get("file_name", "")})
    record["summary_until_step"] = state.get("step") or 0
    log.info("[제공] %s 설계 버전 v%s 산출물 %d개 → %s", project, version, len(files), out_dir)
    return {"design_version": version, "versions": versions + [record]}


def route_after_deliver(state: AgentState) -> str:
    """DB 검증을 요청했으면 검증으로, 아니면 종료."""
    wants = (state.get("options") or {}).get("validate_db")
    return "validate" if wants and state.get("data_model") else "end"


# ------------------------------------------------------------------ 별도 DB에 DDL 적용·검증
def db_validate(state: AgentState) -> dict:
    """별도 DB에 DDL 적용·검증 (VALIDATION_DB_URL이 있으면 PostgreSQL, 없으면 SQLite)."""
    s = get_settings()
    model = get_data_model(state)
    version = state.get("design_version") or ""
    # 설계 표준을 적용했으면 고객이 실제로 쓸 표준 이름 DDL을 검증하고, 실패한 테이블은 설계 이름으로 되돌려 수정 대상으로 쓴다
    profile = profile_of(state.get("options"))
    checked, back = model, {}
    if model is not None and profile and profile.renames:
        physical, conv = apply_profile(model, profile)
        checked = ddl_model(physical, profile)
        back = {r.physical_table.lower(): r.table for r in conv.rows if not r.column}
    try:
        result = validate_ddl(checked, version, state["run_id"], s.validation_db_url, s.keep_validation_schema)
    except Exception as e:  # noqa: BLE001 — 접속 실패 등은 검증 실패로 기록
        result = DbValidationResult(engine="postgresql" if s.validation_db_url else "sqlite",
                                    design_version=version, passed=False, errors=[f"검증 실행 오류: {e}"])
    if back:
        result.failed_tables = [back.get(t.lower(), t) for t in result.failed_tables]
        result.errors = [re.sub(r"\[([^\]]+)\]", lambda m: f"[{m.group(1)}={back[m.group(1)]}]"
                                if m.group(1) in back else m.group(0), e) for e in result.errors]
    log.info("[DB 검증] v%s %s (%s) %s", version, "통과" if result.passed else "실패", result.engine,
             "; ".join(result.checks))
    return {"db_validation": result.model_dump()}


def route_after_db(state: AgentState) -> str:
    """검증 통과면 보고서로, 실패면 오류 기록으로."""
    return "pass" if (state.get("db_validation") or {}).get("passed") else "fail"


def _version_dir(state: AgentState) -> Path:
    """현재 설계 버전의 산출물 폴더 (deliver가 기록한 위치)."""
    return Path(state["versions"][-1]["output_dir"])


def _record_db_result(state: AgentState, result: DbValidationResult) -> None:
    """프로젝트 버전 기록(versions.json)에 DB 검증 결과를 남긴다."""
    update_version(get_settings().output_dir, project_of(state), state.get("design_version") or "",
                   db_validation="PASSED" if result.passed else "FAILED", db_engine=result.engine,
                   db_errors=result.errors[:5])


def db_report(state: AgentState) -> dict:
    """DB 구조·검증 보고서 제공. 통과하면 남아 있던 DB 이슈를 해결 처리한다."""
    result = DbValidationResult.model_validate(state["db_validation"])
    path = export_db_report(result, _version_dir(state))
    _record_db_result(state, result)
    update: dict = {}
    if result.passed:
        issues = reconcile_issues(issues_of(state), [], lambda k: k.startswith("DB_"), get_settings().fix_attempts)
        update["issues"] = _issue_dicts(issues)
    log.info("[DB 보고서] %s", path)
    return update


def record_db_failure(state: AgentState) -> dict:
    """오류와 관련 설계 버전을 기록하고, 수정 담당(데이터 모델 Agent)에게 넘길 이슈를 만든다."""
    s = get_settings()
    result = DbValidationResult.model_validate(state["db_validation"])
    export_db_report(result, _version_dir(state))
    _record_db_result(state, result)
    version = state.get("design_version") or ""
    model = get_data_model(state)
    edited = {e.entity_id for e in model.entities if e.user_edited} if model else set()   # 자동으로 고치지 않음
    findings = []
    for table in result.failed_tables or ["*"]:
        related = [e for e in result.errors if f"[{table}]" in e or table in e] or result.errors
        findings.append(finding("DB_VALIDATION_FAILED", "ERROR", f"v{version} DDL 검증 실패: " + " / ".join(related[:3])
                                + (USER_EDITED_NOTE if table in edited else ""),
                                "none" if table in edited else "data_modeler", table, "db_validate"))
    issues = reconcile_issues(issues_of(state), findings, lambda k: k.startswith("DB_"), s.fix_attempts)
    versions = list(state.get("versions") or [])
    if versions:
        versions[-1] = {**versions[-1], "db_validation": "FAILED", "db_errors": result.errors[:10]}
    attempts = (state.get("db_attempts") or 0) + 1
    log.info("[DB 실패 기록] v%s 오류 %d건 → %s", version, len(result.errors),
             "조정 Agent로 수정 요청" if attempts < s.max_db_attempts else "재시도 한도 도달")
    return {"issues": _issue_dicts(issues), "versions": versions, "db_attempts": attempts}


def route_after_db_failure(state: AgentState) -> str:
    """재검증 한도 안이면 조정 Agent로(retry), 넘으면 실패 보고서로(give_up)."""
    return "retry" if (state.get("db_attempts") or 0) < get_settings().max_db_attempts else "give_up"
