"""LangGraph 공통 상태와 상태 조작 도우미.

상태 값은 JSON 직렬화 가능한 dict/list만 담는다 (SqliteSaver 체크포인트 → 재개 가능).
"""
from __future__ import annotations

from typing import Callable, Iterable, TypedDict, TypeVar

from pydantic import BaseModel

from app.schemas.data_model import DataModel
from app.schemas.review import Issue

M = TypeVar("M", bound=BaseModel)


class AgentState(TypedDict, total=False):
    # 프로젝트·실행
    """LangGraph 공통 상태. 모든 노드가 읽고, 바꾼 키만 돌려준다 (JSON 직렬화 가능한 값만)."""
    run_id: str
    source_path: str
    options: dict                 # ask_mode(ask|defer), validate_db, only, max_sources
    document: dict                # document_id, document_version, file_name
    # 문서분석 결과
    blocks: list[dict]
    sources: list[dict]
    # 설계 산출물
    requirements: list[dict]
    questions: list[dict]
    functions: list[dict]
    data_model: dict | None
    trace_links: list[dict]
    issues: list[dict]
    semantic_review: dict         # 마지막 의미 검토: 검토한 부분, 실패한 부분, 대상 ID 확인 불가로 버린 지적 수
    # 변경 추적 (부분 재설계용)
    rev: dict                     # 각 산출물의 수정 횟수 {"requirements", "functions", "model"}
    basis: dict                   # 각 산출물이 기반한 상위 산출물 rev {"functions", "model", "review", "semantic"}
    dirty_requirements: list[str] # 기능 명세에 아직 반영되지 않은 요구사항 변경
    dirty_functions: list[str]    # 데이터 모델에 아직 반영되지 않은 기능 변경
    requirement_targets: list[str] | None  # 요구사항 단계 다시 만들기 (rerun requirements_analyst --feedback, 빈 목록 = 의견 반영)
    requirement_rework: dict      # 요구사항 다시 만들기 요약 (의견으로 바뀐·뺀·새 요구사항)
    function_targets: list[str] | None  # 기능 명세만 다시 설계할 요구사항 (rerun function_designer, ["*"] 전체, 빈 목록이면 의견 관련)
    function_rework: dict         # 기능 명세 다시 만들기 요약 (다시 설계한 그룹, 새·바뀐·지운 기능)
    function_common: dict         # 기능 명세 공통 결정 (업무 객체 이름·분류·상태 값·공통 규칙·공통 기능, CommonDecisionsOut)
    function_answers: list[str]   # 추천과 다른 답이 와서 기능 명세에 반영할 확인 질문 ID (바꿀 기능만 고친다)
    requirement_review: dict      # 요구사항 분류 검토: reviewed(검토한 요구사항 ID), changed(분류를 바꾼 ID), last(요약)
    function_confirm: dict        # 마지막 확인 질문 고르기 요약 (후보 수, 질문 ID, '(가정)' 한도·규칙 수, 추천안으로 정한 수, 한도로 남긴 수, 못 정한 수)
    model_redesign: bool          # 데이터 설계 질문에 추천과 다른 답이 와서 데이터 모델을 다시 설계해야 함
    model_targets: list[str] | None  # 컬럼만 다시 설계할 엔티티 (rerun --entities, 빈 목록이면 의견과 관련된 엔티티)
    model_rework: dict            # 데이터 설계 다시 만들기 요약 (사용자 수정 반영·다시 설계 범위, ReworkSummary)
    api_design: dict              # API 설계: functions(기능별 API), fingerprints(설계 당시 기능·테이블 지문), architecture, last(요약)
    design_version: str           # 프로젝트 단위 설계 버전 번호 ('2', '2.1' …)
    versions: list[dict]
    # 실행 제어
    step: int
    next_action: dict
    last_action: dict
    decisions: list[dict]         # 조정 Agent 판단 기록
    failures: dict                # 단계별 실패 횟수
    completion: dict
    pending_answers: dict
    db_validation: dict | None
    db_attempts: int
    llm_usage: dict


def load(state: AgentState, key: str, model: type[M]) -> list[M]:
    """상태의 dict 목록을 pydantic 모델 목록으로 읽는다."""
    return [model.model_validate(d) for d in state.get(key) or []]


def dump(items: Iterable[BaseModel]) -> list[dict]:
    """pydantic 모델 목록을 상태에 넣을 dict 목록으로 바꾼다."""
    return [i.model_dump(mode="json") for i in items]


def get_data_model(state: AgentState) -> DataModel | None:
    """상태의 데이터 모델을 DataModel로 읽는다 (없으면 None)."""
    data = state.get("data_model")
    return DataModel.model_validate(data) if data else None


def rev(state: AgentState) -> dict:
    """산출물별 수정 횟수 (requirements·functions·model)."""
    return {"requirements": 0, "functions": 0, "model": 0, **(state.get("rev") or {})}


SEMANTIC_BASIS_INIT = {"functions": [-1, -1], "model": [-1, -1]}


def basis(state: AgentState) -> dict:
    """각 산출물을 만들 때 기반으로 삼은 상위 산출물의 수정 횟수."""
    return {"functions": -1, "model": -1, "review": [-1, -1], "semantic": dict(SEMANTIC_BASIS_INIT),
            **(state.get("basis") or {})}


def functions_stale(state: AgentState) -> bool:
    """요구사항이 바뀐 뒤 기능 명세가 아직 갱신되지 않았는지."""
    return bool(state.get("requirements")) and basis(state)["functions"] < rev(state)["requirements"]


def model_stale(state: AgentState) -> bool:
    """기능 명세가 바뀐 뒤 데이터 모델이 아직 갱신되지 않았는지."""
    return bool(state.get("functions")) and basis(state)["model"] < rev(state)["functions"]


def review_stale(state: AgentState) -> bool:
    """기능 명세·데이터 모델이 바뀐 뒤 검토가 아직 다시 돌지 않았는지."""
    r = rev(state)
    return bool(state.get("functions")) and list(basis(state)["review"]) != [r["functions"], r["model"]]


def semantic_stale_parts(state: AgentState) -> list[str]:
    """의미 검토가 아직 최신 산출물을 보지 않은 부분 (functions·model 중).

    기능 명세 검토는 요구사항·기능이, 데이터 모델 검토는 기능·모델이 바뀌면 다시 한다.
    """
    r, done = rev(state), {**SEMANTIC_BASIS_INIT, **basis(state)["semantic"]}
    parts = []
    if state.get("functions") and list(done["functions"]) != [r["requirements"], r["functions"]]:
        parts.append("functions")
    if state.get("data_model") and list(done["model"]) != [r["functions"], r["model"]]:
        parts.append("model")
    return parts


def issues_of(state: AgentState) -> list[Issue]:
    """상태의 이슈 목록을 Issue 모델로 읽는다."""
    return load(state, "issues", Issue)


def actionable_issues(state: AgentState, agent: str | None = None) -> list[Issue]:
    """수정 담당이 있고 아직 열려 있는 이슈 (agent를 주면 그 담당 것만)."""
    return [
        i for i in issues_of(state)
        if i.status == "OPEN" and i.target_agent != "none" and (agent is None or i.target_agent == agent)
    ]


def reconcile_issues(
    existing: list[Issue],
    findings: list[Issue],
    owns: Callable[[str], bool],
    max_attempts: int | Callable[[str], int],
) -> list[Issue]:
    """검사 결과(findings)로 기존 이슈 목록을 갱신한다.

    - owns(kind)가 참인 종류만 이 검사의 소유로 보고 갱신한다.
    - max_attempts: 시도 한도. 함수(Settings.fix_attempts)를 주면 이슈의 수정 담당 Agent별 한도를 쓴다.
    - 다시 발견된 이슈: 시도 횟수가 한도에 도달했으면 UNRESOLVED, 아니면 OPEN 유지
    - 더 이상 발견되지 않은 이슈: RESOLVED
    """
    owned_prev = {i.key: i for i in existing if owns(i.kind)}
    result = [i for i in existing if not owns(i.kind)]
    next_no = max((int(i.issue_id.split("-")[1]) for i in existing), default=0) + 1
    seen: set[tuple[str, str]] = set()
    for f in findings:
        if f.key in seen:
            continue
        seen.add(f.key)
        prev = owned_prev.get(f.key)
        if prev:
            limit = max_attempts(prev.target_agent) if callable(max_attempts) else max_attempts
            exhausted = prev.target_agent != "none" and prev.attempts >= limit
            result.append(prev.model_copy(update={
                "message": f.message, "severity": f.severity, "target_agent": f.target_agent,
                "status": "UNRESOLVED" if exhausted else "OPEN",
            }))
        else:
            result.append(f.model_copy(update={"issue_id": f"ISS-{next_no:04d}", "status": "OPEN", "attempts": 0}))
            next_no += 1
    for key, prev in owned_prev.items():
        if key not in seen:
            result.append(prev.model_copy(update={"status": "RESOLVED"}))
    return sorted(result, key=lambda i: i.issue_id)


def finding(kind: str, severity: str, message: str, target_agent: str = "none", target_id: str = "",
            raised_by: str = "") -> Issue:
    """검사기가 만드는 이슈 후보. issue_id는 reconcile_issues에서 부여한다."""
    return Issue(issue_id="ISS-0000", kind=kind, severity=severity, message=message,
                 target_agent=target_agent, target_id=target_id, raised_by=raised_by)


def merge_usage(state: AgentState, delta: dict) -> dict:
    """LLM 사용량(호출 수·토큰)을 상태에 누적한다."""
    total = dict(state.get("llm_usage") or {})
    for k, v in delta.items():
        total[k] = total.get(k, 0) + v
    return total


def with_result(state: AgentState, result: str) -> list[dict]:
    """조정 Agent 판단 기록의 마지막 항목(방금 실행한 행동)에 결과 요약을 적은 새 목록."""
    decisions = list(state.get("decisions") or [])
    if decisions:
        decisions[-1] = {**decisions[-1], "result": result}
    return decisions
