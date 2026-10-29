"""요구사항 검토 Agent: 요구사항 Agent의 결과를 원문과 대조해 기능 명세 전에 다듬는다.

(가) 원문 대조 (아직 검토하지 않은 요구사항이 있는 원문 단위만 — requirement_review.reviewed)
    토큰을 줄이려고 코드가 먼저 보낼 것만 고른다 (보낼 것이 없는 단위는 LLM을 부르지 않는다).
    - 누락 후보: 그 단위 요구사항(설명·인용·수용 기준)과 겹침이 REQ_REVIEW_COVERAGE보다 낮은 원문 문장만 (4글자 단위)
    - 판정 후보: 분류 의심 신호(번호 접두어 관례·원문 표 '분류'·수행 의무 표현·기능 동작 표현)가 있거나,
      설명이 원문과 많이 다르거나(2글자 단위 겹침 < ADDED_OVERLAP), 인용이 원문에서 확인되지 않은 요구사항만
    요청의 생각 수준은 REQ_REVIEW_EFFORT(기본 low).
    1. 빠진 요구사항: 원문에 있는데 추출되지 않은 것은 이 Agent가 직접 요구사항으로 보충한다 (같은 단위 번호 뒤에 이어서).
       인용문이 원문에 없으면 버린다. 표시 SUPPLEMENTED (근거가 원문에 있어 묻지 않는다).
    2. 분류: 확신하면 고치고 결정으로 남긴다 ([분류 검토] A → B).
       근거가 애매하면 판단한 분류로 두고 '기능(추가 여부 판단 필요)' / '비기능(판단 필요)' 표시 + 확인 질문 RC-….
    3. 원문 근거: 원문에 없이 추가로 판단해 만든 요구사항은 지우지 않고 '원문 외 추가 판단' 표시 + 확인 질문 RC-… (포함 / 제외).
(나) 충돌 검토: 요구사항이 바뀔 때마다 (requirement_review.conflict_rev ≠ rev.requirements) 전체 목록을 LLM 1회로 본다.
    원문 단위(기능 명세 그룹)를 넘는 충돌은 기능 명세 Agent가 볼 수 없어서 여기서 찾는다.
    충돌 묶음마다 확인 질문 CF-…(선택지·추천안)을 내고 관련 요구사항에 CONFLICT 표시를 남긴다. 같은 묶음은 다시 묻지 않는다.
(다) 기능 연결 검사 (coverage_findings, 코드·LLM 없음): 기능 명세가 나온 뒤 시스템 기능 요구사항마다 기능이 연결됐는지 본다.
    초안 단계면 빠진 요구사항을 기능 명세 Agent가 빠진 것만 채우고(FUNC_FIX_ATTEMPTS번), 사람이 확정한 명세로
    데이터 설계를 하는 단계(rerun data_modeler)면 보고만 한다. 기능 명세 → ERD 연결은 추적 검토 Agent가 본다.

표시(Requirement.review)는 기능 명세 Agent에 그대로 넘어간다: 확인 대기 요구사항은 공통 결정에 쓰지 않고,
그 요구사항으로 만든 기능 규칙에 '(확인 필요 RC-…/CF-…)'를 붙인다. 질문은 기능 명세 초안과 함께 한 번에 묻는다.
답을 받으면 표시를 지운다 (분류·포함 여부는 record_answer, 충돌은 사용자 답변 기록 노드).
"""
from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.agents.llm.client import LLMFatalError, get_llm, run_parallel
from app.schemas.design import (USER_SOURCE, DocumentBlock, Evidence, FunctionSpec, Issue, Question, Requirement,
                                RequirementCategory, RequirementScope, RequirementSource, RequirementType, ReviewNote)
from app.agents.flow.state import AgentState, dump, finding, load, merge_usage, rev
from app.agents.tools.search import normalize, quote_in_text

log = logging.getLogger(__name__)
BY = "requirement_reviewer"
REQ_GAP = "REQ_WITHOUT_FUNCTION"   # 기능이 연결되지 않은 시스템 기능 요구사항
QUESTION_PREFIX = "RC-"
CONFLICT_PREFIX = "CF-"
EXCERPT_CONTEXT = 100        # 판정 후보의 근거 원문: 인용 앞뒤로 붙이는 글자 수 (인용을 못 찾으면 블록 앞 EXCERPT_CHARS자)
EXCERPT_CHARS = 300
ADDED_OVERLAP = 0.5          # 설명의 2글자 단위가 원문에 이보다 적게 있으면 '원문 외 추가' 판정 후보 (고객요구사항5: 158건 중 12건)
MIN_SENTENCE = 8             # 누락 검사로 보낼 원문 문장의 최소 길이 (공백·기호 뺀 글자 수)
INCLUDE, EXCLUDE = "포함 (원문 외 추가 요구사항으로 유지)", "제외"

# 번호 접두어 관례 (행정안전부 요구사항 분류): 접두어 → (유형, 범위, 분류명)
PREFIX_EXPECT: dict[str, tuple[str, str, str]] = {
    "SFR": ("FUNCTIONAL", "SYSTEM", "기능"), "PER": ("NON_FUNCTIONAL", "SYSTEM", "성능"),
    "SIR": ("NON_FUNCTIONAL", "SYSTEM", "인터페이스"), "DAR": ("NON_FUNCTIONAL", "SYSTEM", "데이터"),
    "SER": ("NON_FUNCTIONAL", "SYSTEM", "보안"), "QUR": ("NON_FUNCTIONAL", "SYSTEM", "품질"),
    "COR": ("NON_FUNCTIONAL", "SYSTEM", "제약"), "ECR": ("NON_FUNCTIONAL", "SYSTEM", "장비구성"),
    "TER": ("NON_FUNCTIONAL", "SYSTEM", "테스트"), "PMR": ("NON_FUNCTIONAL", "PROJECT", "사업관리"),
    "PSR": ("NON_FUNCTIONAL", "PROJECT", "사업지원"),
}
CATEGORIES = ("기능", "성능", "보안", "인터페이스", "데이터", "품질", "제약", "장비구성", "테스트", "사업관리", "사업지원")
# 수행업체 의무로 보이는 표현 (시스템 요구사항이면 의심)
PROJECT_WORDS = re.compile(r"교육|훈련|산출물|하자\s*보수|인력|투입|납품|보안\s*서약|검수|착수|완료\s*보고|기술\s*지원|보고서\s*제출")
# 시스템이 제공하는 동작 표현 (비기능이면 의심)
ACTION_WORDS = re.compile(r"(등록|조회|검색|신청|승인|반려|알림|발송|통보|다운로드|업로드|출력|연계|배정|접수|저장|삭제|수정)"
                          r"\s*(할\s*수\s*있|하는\s*기능|기능을\s*제공|기능\s*제공|하여야|해야)")

SOURCE_PROMPT = """당신은 공공·기업 IT 구축 사업의 요구사항 검토자다. 원문 블록과 요구사항 Agent가 그 원문에서 추출한 요구사항을 대조한다.

분류 기준 (요구사항 Agent와 같다)
- type
  · FUNCTIONAL: 시스템이 사용자·외부 시스템에 제공하는 동작 (등록, 조회, 접수, 배정, 승인, 알림, 연계, 다운로드 등)
  · NON_FUNCTIONAL: 성능, 보안, 가용성, 호환성, 데이터 관리 정책, 품질, 제약, 장비 구성, 테스트, 사업관리·지원 등 기능이 아닌 조건
- scope
  · SYSTEM: 구축될 시스템이 갖추거나 충족해야 하는 것
  · PROJECT: 수행업체의 사업 수행 의무 (보안서약·교육·산출물 제출·하자보수·장비 납품·검수 절차·인력 투입 등)
- category: 기능, 성능, 보안, 인터페이스, 데이터, 품질, 제약, 장비구성, 테스트, 사업관리, 사업지원 중 하나.
  FUNCTIONAL이면 보통 '기능'이다. 원문 표의 '분류' 칸이 있으면 그것을 우선한다.

입력: 원문 단위마다 '요구사항 제목'(그 단위에서 추출된 요구사항), '담기지 않은 원문 문장'(어느 요구사항과도 겹침이 적은 문장),
'판정할 요구사항'(근거 원문은 인용 앞뒤만).

할 일
1. verdicts: '판정할 요구사항'마다 한 번씩 낸다.
   - type·scope·category: 원문 내용으로 판단한 분류 (지금 분류가 맞으면 그대로).
     내용(무엇을 하는 것인지, 무엇을 지켜야 하는 조건인지, 누가 하는 일인지)으로 판단한다. '의심 이유'와 번호 접두어는 참고일 뿐이다.
     예: PER 번호라도 알림을 보내는 동작이면 FUNCTIONAL, SFR 번호라도 화면 응답 시간 기준이면 NON_FUNCTIONAL.
   - confidence: 원문으로 분류가 분명하면 high, 해석에 따라 갈려 고객 확인이 필요하면 low.
   - in_source: 요구사항 내용이 원문에 근거하면 true. 원문에 없는 기능·조건을 추가로 판단해 만들었으면 false이고
     source_note에 원문에 없는 부분을 한 문장으로 쓴다. 원문 표현을 요약·정리한 것은 근거가 있는 것이다.
   - reason은 분류 이유 한 문장, evidence는 원문에서 그대로 인용한 구간.
2. missing: '담기지 않은 원문 문장' 중 검증 가능한 요구인데 '요구사항 제목' 어디에도 해당하지 않는 것만 쓴다.
   제목 목록의 요구사항에 이미 담긴 내용, 입찰 안내·평가 기준·사업 개요처럼 요구사항이 아닌 내용은 쓰지 않는다.
   원문 단위 키와 근거 블록 ID는 그 문장에 붙은 것을 쓰고, quote는 그 문장에서 그대로 복사한 연속 구간(15~120자)이다. 없으면 빈 배열이다.
3. requirement_id는 제공된 것만 쓴다. 모든 서술은 한국어."""


CONFLICT_PROMPT = """당신은 공공·기업 IT 구축 사업의 요구사항 검토자다. 고객 문서에서 추출한 요구사항 목록을 보고 서로 충돌하는 요구사항을 찾는다.

충돌: 한 요구사항을 지키면 다른 요구사항을 반드시 어기게 되어, 둘을 동시에 만족하는 구현이 없는 경우만이다.
같은 대상·같은 조건에 대해 수치·기간·권한·절차·상태·책임 주체가 서로 양립할 수 없게 다르다.
(예: 같은 이력에 '보관 1년'과 '보관 3년 후 삭제' / 같은 게시글에 '관리자만 삭제'와 '작성자가 삭제')

충돌이 아닌 것 (쓰지 않는다)
- 중복·포함: 같은 내용을 두 곳에서 말하거나 한쪽이 더 엄격함 (예: '별도 서버로 구분'과 '별도 서버로 zone 구분' → 엄격한 쪽을 지키면 둘 다 만족)
- 보완: 대상이나 범위를 나눠 각각 정함 (예: '조회 3초 이내'와 '입력·수정 3초 이내', '1개월 이내 5초'와 '1개월 초과는 5초 이상 가능')
- 조건부: 한쪽이 다른 쪽의 특정 경우만 정함 (예: '오픈소스 또는 상용 제안 가능'과 '상용 제안 시 비용 세분화')
- 함께 적용: 여러 통제·기능을 모두 요구함 (예: 'IP ACL·포트 변경'과 '2-Factor·캡차')
- 무관: 대상이 다름, 숫자나 단어만 같음, 기산점이 다른 별개 일정, 같은 기한
- 모호함·누락: 상한이 없거나 세부가 정해지지 않음 (충돌이 아니라 확인할 미정 사항이다)

규칙
1. 제공된 내용으로 확인되는 충돌만 쓴다. 추측으로 의존성·우선순위 문제를 만들지 않는다.
2. 각 충돌마다 incompatible에 '한쪽을 지키면 다른 쪽을 어기는 이유'를 두 요구사항의 문장으로 쓴다.
   둘을 함께 만족하는 구현이 하나라도 있으면 both_possible을 true로 하고, 그런 지적은 버려진다. 확신이 없으면 쓰지 않는다.
3. 요구사항의 '결정'이 이미 충돌을 정리했으면 쓰지 않는다.
4. requirement_ids에는 충돌하는 요구사항 ID를 목록에 있는 것만 2개 이상 쓴다.
5. title은 무엇이 충돌하는지 20자 안팎의 명사구, conflict는 무엇이 어떻게 다른지 한 문장,
   evidence는 두 요구사항의 해당 문장을 그대로 인용한다.
6. question은 고객에게 확인할 질문 한 문장, options는 두 요구사항 중 어느 쪽을 따를지 등 구체적인 해결안 2~4개,
   recommended는 options 중 하나. 해결안에 요구사항에 없는 새 조건(하드웨어·수치·절차 등)을 만들지 않는다.
7. 같은 충돌을 여러 번 쓰지 않는다. 충돌이 없으면 conflicts는 빈 배열이며, 이것이 흔한 정상 결과다. 최대 5건.
8. 모든 서술은 한국어."""


# LLM 응답 스키마: 요구사항 하나의 원문 대조 판정
class RequirementVerdict(BaseModel):
    requirement_id: str
    type: RequirementType
    scope: RequirementScope
    category: RequirementCategory
    confidence: Literal["high", "low"]
    in_source: bool = Field(True, description="요구사항 내용이 원문에 근거하면 true, 원문에 없이 추가로 판단해 만들었으면 false")
    source_note: str = Field("", description="in_source가 false일 때 원문에 없는 부분 한 문장")
    reason: str = Field(description="분류 이유 한 문장")
    evidence: str = Field(description="원문에서 그대로 인용한 구간")


# LLM 응답 스키마: 원문에 있는데 추출되지 않은 요구사항
class MissingRequirement(BaseModel):
    source_key: str = Field(description="원문 단위 키 (제공된 것)")
    title: str
    description: str
    type: RequirementType
    scope: RequirementScope
    category: RequirementCategory
    acceptance_criteria: list[str] = Field(default_factory=list)
    source_block_id: str = Field(description="근거 블록 ID (그 원문 단위의 블록)")
    quote: str = Field(description="블록 원문에서 그대로 복사한 연속 구간 15~120자")
    reason: str = Field(description="왜 빠진 요구사항인지 한 문장")


# LLM 응답 스키마: 원문 단위 대조 결과
class SourceReviewOut(BaseModel):
    verdicts: list[RequirementVerdict]
    missing: list[MissingRequirement] = Field(default_factory=list)


# LLM 응답 스키마: 요구사항 사이의 충돌 하나
class RequirementConflict(BaseModel):
    requirement_ids: list[str] = Field(description="충돌하는 요구사항 ID (2개 이상)")
    title: str = Field("", description="무엇이 충돌하는지 20자 안팎의 명사구 (기능 규칙에 짧게 붙는다)")
    conflict: str = Field(description="무엇이 어떻게 다른지 한 문장")
    incompatible: str = Field("", description="한쪽을 지키면 다른 쪽을 어기는 이유")
    both_possible: bool = Field(False, description="둘을 함께 만족하는 구현이 있으면 true (그 지적은 버린다)")
    evidence: str = Field(description="두 요구사항의 해당 문장 인용")
    question: str = Field(description="고객에게 확인할 질문")
    options: list[str] = Field(description="해결안 2~4개")
    recommended: str = Field(description="options 중 추천안")


# LLM 응답 스키마: 충돌 검토 결과
class ConflictReviewOut(BaseModel):
    conflicts: list[RequirementConflict]


Cls = tuple[str, str, str]   # (type, scope, category)


def label(c: Cls) -> str:
    """분류 표시 (질문 선택지로도 쓴다): '기능 / 시스템 / 기능'."""
    return f"{'기능' if c[0] == 'FUNCTIONAL' else '비기능'} / {'시스템' if c[1] == 'SYSTEM' else '수행 의무'} / {c[2]}"


def parse_label(text: str) -> Cls | None:
    """질문 답(선택지 문자열)을 분류로 되돌린다. 형식이 아니면 None (직접 입력한 답)."""
    parts = [p.strip() for p in text.split("/")]
    if len(parts) != 3 or parts[0] not in ("기능", "비기능") or parts[1] not in ("시스템", "수행 의무") \
            or parts[2] not in CATEGORIES:
        return None
    return ("FUNCTIONAL" if parts[0] == "기능" else "NON_FUNCTIONAL",
            "SYSTEM" if parts[1] == "시스템" else "PROJECT", parts[2])


def current(r: Requirement) -> Cls:
    return r.type, r.scope, r.category


def flag_label(r: Requirement) -> str:
    """분류 근거가 애매한 요구사항의 표시: 기능이면 추가 여부를, 비기능이면 분류를 판단해야 한다."""
    return "기능(추가 여부 판단 필요)" if r.type == "FUNCTIONAL" else "비기능(판단 필요)"


def settle(r: Requirement, question_id: str) -> ReviewNote | None:
    """질문에 답을 받은 검토 표시를 지운다 (지운 표시를 돌려준다)."""
    note = next((n for n in r.review if n.question_id == question_id), None)
    r.review = [n for n in r.review if n.question_id != question_id]
    return note


def enabled(state: AgentState) -> bool:
    """요구사항 검토 Agent를 쓰는지 (REQUIREMENT_REVIEW=false 또는 options.requirement_review=false면 끔)."""
    return bool((state.get("options") or {}).get("requirement_review", get_settings().requirement_review))


def unreviewed(state: AgentState) -> list[str]:
    """아직 원문과 대조하지 않은 요구사항 ID (범위에서 뺀 요구사항 제외)."""
    done = set((state.get("requirement_review") or {}).get("reviewed") or [])
    return [r["requirement_id"] for r in state.get("requirements") or []
            if r["requirement_id"] not in done and r.get("status") != "EXCLUDED"]


def conflicts_stale(state: AgentState) -> bool:
    """요구사항이 바뀐 뒤 충돌 검토를 아직 하지 않았는지."""
    return bool(state.get("requirements")) and \
        (state.get("requirement_review") or {}).get("conflict_rev", -1) != rev(state)["requirements"]


def coverage_findings(requirements: list[Requirement], functions: list[FunctionSpec], fix: bool) -> list[Issue]:
    """(다) 요구사항 → 기능 명세 연결 검사: 기능이 하나도 연결되지 않은 시스템 기능 요구사항.
    fix면 기능 명세 Agent가 빠진 것만 채우게 하고(ERROR), 아니면(사람이 확정한 명세) 보고만 한다(WARNING)."""
    if not functions:
        return []
    covered = {i for f in functions for i in f.requirement_ids}
    return [finding(REQ_GAP, "ERROR" if fix else "WARNING", f"{r.requirement_id} '{r.title}'에 연결된 기능이 없음",
                    "function_designer" if fix else "none", r.requirement_id, BY)
            for r in requirements if r.designed and r.requirement_id not in covered]


def pending(state: AgentState) -> list[str]:
    """남은 검토 (조정 Agent가 고르는 근거): 원문 대조 전인 요구사항 n건 / 충돌 검토. 꺼져 있으면 빈 목록."""
    if not enabled(state):
        return []
    todo = []
    if unchecked := unreviewed(state):
        todo.append(f"원문 대조 전인 요구사항 {len(unchecked)}건")
    if conflicts_stale(state):
        todo.append("요구사항 충돌 검토")
    return todo


# ------------------------------------------------------------------ 분류 의심 신호 (LLM 없음, 원문 대조의 참고)
def signals(r: Requirement, source: RequirementSource | None) -> list[str]:
    """분류가 틀렸을 수 있다는 신호 목록 (없으면 빈 목록)."""
    out = []
    text = f"{r.title} {r.description}"
    if (r.category == "기능") != (r.type == "FUNCTIONAL"):
        out.append(f"분류명 '{r.category}'와 유형 {r.type}이 맞지 않음")
    prefix = re.match(r"[A-Z]{2,4}", r.source_ref or r.source_key or "")
    expect = PREFIX_EXPECT.get(prefix.group(0)) if prefix else None
    if expect and expect[0] != r.type:
        out.append(f"번호 접두어 {prefix.group(0)}는 관례상 {label(expect)}")
    elif expect and expect[1] == "PROJECT" and r.scope == "SYSTEM":
        out.append(f"번호 접두어 {prefix.group(0)}는 관례상 수행 의무")
    table = (source.category if source else "").replace(" ", "")
    found = next((c for c in CATEGORIES if c in table), None)
    if found and (found == "기능") != (r.category == "기능"):
        out.append(f"원문 표의 분류 '{source.category}'와 분류명 '{r.category}'가 다름")
    if r.scope == "SYSTEM" and PROJECT_WORDS.search(text):
        out.append(f"수행 의무 표현 '{PROJECT_WORDS.search(text).group(0)}'이 있는데 시스템 요구사항")
    if r.type == "NON_FUNCTIONAL" and r.scope == "SYSTEM" and ACTION_WORDS.search(text):
        out.append(f"기능 동작 표현 '{ACTION_WORDS.search(text).group(0)}'이 있는데 비기능")
    return out


# ------------------------------------------------------------------ 노드
def run(state: AgentState) -> dict:
    """요구사항 검토 Agent 노드: (가) 검토 전인 요구사항이 있는 원문 단위를 원문과 대조해 보충·분류·표시한다.
    (나) 요구사항이 바뀌었으면 전체 목록에서 충돌을 찾아 표시하고 확인 질문을 낸다."""
    s = get_settings()
    requirements = load(state, "requirements", Requirement)
    by_id = {r.requirement_id: r for r in requirements}
    targets = [by_id[i] for i in unreviewed(state) if i in by_id]
    sources = {src.source_key: src for src in load(state, "sources", RequirementSource)}
    blocks = {b.source_block_id: b for b in load(state, "blocks", DocumentBlock)}
    # 사용자 의견으로 추가한 요구사항(원문 없음)과 단위를 모르는 요구사항은 대조할 원문이 없어 검토한 것으로 둔다
    done = [r.requirement_id for r in targets if r.source_key == USER_SOURCE or r.source_key not in sources]
    units = list(dict.fromkeys(r.source_key for r in targets if r.requirement_id not in done))
    target_ids = {r.requirement_id for r in targets}
    plans = {u: _plan(sources[u], blocks, [r for r in requirements if r.source_key == u], target_ids, s.req_review_coverage)
             for u in units}
    # 담기지 않은 문장도 판정 후보도 없는 단위는 LLM 없이 검토한 것으로 둔다
    sent = [u for u in units if plans[u][0] or plans[u][1]]
    done += [r.requirement_id for r in targets if r.source_key in units and r.source_key not in sent]
    batches = [sent[i:i + s.req_review_batch] for i in range(0, len(sent), s.req_review_batch)]

    llm = get_llm() if batches else None
    verdicts: list[RequirementVerdict] = []
    missing: list[MissingRequirement] = []
    failed = 0
    if batches:
        results = run_parallel(lambda batch: llm.structured(
            system=SOURCE_PROMPT, user=_source_prompt(batch, sources, blocks, requirements, plans),
            schema=SourceReviewOut, effort=s.req_review_effort,
            purpose=f"요구사항 원문 대조 {batch[0]} 외 {len(batch) - 1}단위"), batches)
        for batch, out, err in results:
            if err or out is None:
                failed += 1
                continue
            mine = {r.requirement_id for r in targets if r.source_key in batch}
            asked = {rid for u in batch for rid in plans[u][1]}
            verdicts += [v for v in out.verdicts if v.requirement_id in asked]
            missing += [m for m in out.missing if m.source_key in batch]
            done += sorted(mine)   # 요청이 성공한 단위는 판정이 빠진 요구사항도 본 것으로 둔다

    ask = (state.get("options") or {}).get("ask_mode", "ask") == "ask"
    questions = load(state, "questions", Question)
    number = max((int(q.question_id.split("-")[-1]) for q in questions if q.question_id.startswith(QUESTION_PREFIX)),
                 default=0)

    def question(r: Requirement, text: str, context: str, options: list[str], recommended: str) -> str:
        nonlocal number
        number += 1
        qid = f"{QUESTION_PREFIX}{number:03d}"
        questions.append(Question(
            question_id=qid, kind="AMBIGUITY", question=text, context=context, options=options,
            requirement_ids=[r.requirement_id], evidence_block_ids=[e.source_block_id for e in r.evidence if e.verified],
            blocking=True, stage=BY, recommended=recommended, status="PENDING" if ask else "DEFERRED"))
        return qid

    changed: list[str] = []
    counts = {"reclassified": 0, "flagged": 0, "added": 0}
    for v in verdicts:
        r = by_id[v.requirement_id]
        before, proposed = current(r), (v.type, v.scope, v.category)
        if v.confidence == "high" and proposed != before:
            _reclassify(r, proposed, f"[분류 검토] {label(before)} → {label(proposed)} ({v.reason.strip()})")
            counts["reclassified"] += 1
            changed.append(r.requirement_id)
        elif v.confidence == "low":
            # 근거가 애매하면 판단한 분류로 두고 표시한다 (기능이면 초안에 넣어 두고 포함 여부를 묻는다)
            if proposed != before:
                _reclassify(r, proposed, f"[분류 검토] {label(before)} → {label(proposed)} (판단 필요, 확인 대기)")
            flag = flag_label(r)
            qid = question(r, f"'{r.title}'({r.requirement_id})를 어떤 요구사항으로 볼까요?",
                           f"검토 결과: {flag}. {v.reason.strip()}"
                           + (f"\n요구사항 Agent 분류: {label(before)}" if proposed != before else "")
                           + f"\n원문: {v.evidence.strip()}",
                           _options(before, proposed), label(proposed))
            r.review.append(ReviewNote(kind="CLASSIFICATION", note=f"{flag} — {v.reason.strip()}", question_id=qid))
            r.status = "ASSUMED"
            counts["flagged"] += 1
            changed.append(r.requirement_id)
        if not v.in_source:
            note = v.source_note.strip() or v.reason.strip()
            qid = question(r, f"'{r.title}'({r.requirement_id})는 원문에 근거가 없는 요구사항입니다. 포함할까요?",
                           f"원문에 없는 부분: {note}", [INCLUDE, EXCLUDE], INCLUDE)
            r.review.append(ReviewNote(kind="ADDED", note=f"원문 외 추가 판단 — {note}", question_id=qid))
            counts["added"] += 1
            if r.requirement_id not in changed:
                changed.append(r.requirement_id)

    supplemented, dropped = _supplement(missing, sources, blocks, requirements)
    by_id.update({r.requirement_id: r for r in supplemented})
    changed += [r.requirement_id for r in supplemented]
    done += [r.requirement_id for r in supplemented]   # 보충한 요구사항은 이 Agent가 분류까지 정했다

    review = dict(state.get("requirement_review") or {})
    review["reviewed"] = sorted(set(review.get("reviewed") or []) | set(done))
    review["changed"] = sorted(set(review.get("changed") or []) | set(changed))
    summary = (f"원문 대조: 요구사항 {len(targets)}건 · 원문 단위 {len(units)}개 (LLM {len(sent)}단위, "
               f"누락 후보 문장 {sum(len(plans[u][0]) for u in sent)}, 판정 후보 {sum(len(plans[u][1]) for u in sent)}건)"
               f" → 분류 변경 {counts['reclassified']}"
               f", 판단 필요 {counts['flagged']}, 원문 외 추가 {counts['added']}, 빠진 요구사항 보충 {len(supplemented)}"
               + (f" (인용 확인 불가로 제외 {dropped})" if dropped else "")
               + (f", 요청 실패 {failed}" if failed else ""))

    # (나) 충돌 검토: 이번에 고친 분류·보충까지 반영한 목록으로 본다
    conflict = _conflicts(requirements, questions, ask) if conflicts_stale(state) else None
    if conflict:
        summary += f" · 충돌: {conflict['summary']}"
        changed += [rid for rid in conflict["flagged"] if rid not in changed]
        failed += conflict["failed"]
    review["last"] = summary
    log.info("[요구사항 검토] %s", summary)

    update: dict = {"requirement_review": review}
    if llm is not None or conflict:
        update["llm_usage"] = merge_usage(state, get_llm().pop_usage())
    r0 = rev(state)
    new_rev = r0["requirements"] + (1 if changed else 0)
    if conflict and not conflict["failed"]:
        review["conflict_rev"] = new_rev   # 이번 실행에서 바꾼 요구사항까지 본 목록이다
    if changed:
        update["requirements"] = dump(requirements)
        update["rev"] = {**r0, "requirements": new_rev}
        update["dirty_requirements"] = sorted(set(state.get("dirty_requirements") or []) | set(changed))
    if counts["flagged"] or counts["added"] or (conflict and conflict["questions"]):
        update["questions"] = dump(questions)
    if failed:
        update["failures"] = {**(state.get("failures") or {}), BY: (state.get("failures") or {}).get(BY, 0) + 1}
    return update


def _supplement(missing: list[MissingRequirement], sources: dict[str, RequirementSource],
                blocks: dict[str, DocumentBlock], requirements: list[Requirement]) -> tuple[list[Requirement], int]:
    """빠진 요구사항을 같은 원문 단위 번호 뒤에 이어 붙인다 (requirements에 더한다). (보충한 요구사항, 버린 수).

    인용문이 그 단위의 원문 블록에 없거나, 이미 있는 요구사항의 인용과 겹치면 버린다 (지어낸 요구사항·중복 방지)."""
    added: list[Requirement] = []
    dropped = 0
    ids = {r.requirement_id for r in requirements}
    for m in missing:
        src, block = sources.get(m.source_key), blocks.get(m.source_block_id)
        quote = m.quote.strip()
        if not src or not block or m.source_block_id not in src.block_ids or not quote_in_text(quote, block.text):
            dropped += 1
            continue
        quotes = [e.quote for r in requirements if r.source_key == m.source_key for e in r.evidence]
        if any(quote_in_text(quote, q) or quote_in_text(q, quote) for q in quotes):
            dropped += 1
            continue
        seq = max((int(x.group(1)) for r in requirements if r.source_key == m.source_key
                   if (x := re.search(r"-(\d+)$", r.requirement_id))), default=0)
        rid = f"{m.source_key}-{seq + 1:02d}"
        while rid in ids:
            seq += 1
            rid = f"{m.source_key}-{seq + 1:02d}"
        req = Requirement(
            requirement_id=rid, source_key=m.source_key, source_ref=src.code, type=m.type, scope=m.scope,
            category=m.category, title=m.title, description=m.description, priority="UNSPECIFIED",
            acceptance_criteria=m.acceptance_criteria,
            evidence=[Evidence(source_block_id=m.source_block_id, quote=quote, verified=True)],
            review=[ReviewNote(kind="SUPPLEMENTED", note=f"원문에 있으나 추출에서 빠져 보충 — {m.reason.strip()}")])
        requirements.append(req)
        ids.add(rid)
        added.append(req)
    return added, dropped


def _grams(text: str, n: int) -> set[str]:
    """공백·기호를 뺀 글자 n개 단위 조각 (겹침 계산용)."""
    t = normalize(text)
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def _overlap(part: set[str], whole: set[str]) -> float:
    """part 조각 중 whole에 있는 비율 (part가 비면 1)."""
    return len(part & whole) / len(part) if part else 1.0


Plan = tuple[list[tuple[str, str]], dict[str, list[str]]]   # (담기지 않은 (블록 ID, 문장), 판정 후보 요구사항 ID → 이유)


def _plan(src: RequirementSource, blocks: dict[str, DocumentBlock], reqs: list[Requirement], targets: set[str],
          coverage: float) -> Plan:
    """원문 단위 하나에서 LLM에 보낼 것만 고른다 (코드, LLM 없음).

    - 담기지 않은 문장: 그 단위 요구사항 전체(범위에서 뺀 것 포함 — 다시 보충하지 않게)의 설명·인용·수용 기준·제목과
      4글자 단위 겹침이 coverage보다 낮은 원문 문장. 요구사항 설명은 원문을 바꿔 쓰는 일이 많아 넉넉하게 남는 쪽이다.
    - 판정 후보: 검토 대상 중 분류 의심 신호가 있거나, 설명의 2글자 단위가 원문에 ADDED_OVERLAP보다 적게 있거나(원문 외 추가 의심),
      인용이 원문에서 확인되지 않은 요구사항."""
    texts = [(bid, blocks[bid].text) for bid in src.block_ids if bid in blocks]
    cover: set[str] = set()
    for r in reqs:
        cover |= _grams(" ".join([r.title, r.description, *r.acceptance_criteria, *(e.quote for e in r.evidence)]), 4)
    uncovered = []
    for bid, text in texts:
        for sentence in re.split(r"(?<=[.다])\s+|\n", text):
            sentence = sentence.strip()
            if len(normalize(sentence)) >= MIN_SENTENCE and _overlap(_grams(sentence, 4), cover) < coverage:
                uncovered.append((bid, sentence))
    whole = _grams(" ".join(t for _, t in texts), 2)
    candidates: dict[str, list[str]] = {}
    for r in reqs:
        if r.requirement_id not in targets or r.status == "EXCLUDED":
            continue
        hints = signals(r, src)
        if _overlap(_grams(r.description, 2), whole) < ADDED_OVERLAP:
            hints.append("설명이 원문과 많이 다름 (원문에 없는 내용을 추가했는지 확인)")
        if r.evidence and not any(e.verified for e in r.evidence):
            hints.append("인용이 원문에서 확인되지 않음")
        if hints:
            candidates[r.requirement_id] = hints
    return uncovered, candidates


def _excerpt(text: str, quote: str) -> str:
    """근거 블록에서 인용 앞뒤만 잘라 쓴다 (분류는 인용 주변 맥락이면 충분하다)."""
    at = text.find(quote) if quote else -1
    if at < 0:
        return text[:EXCERPT_CHARS] + (" …" if len(text) > EXCERPT_CHARS else "")
    start, end = max(0, at - EXCERPT_CONTEXT), min(len(text), at + len(quote) + EXCERPT_CONTEXT)
    return ("… " if start else "") + text[start:end] + (" …" if end < len(text) else "")


def _source_prompt(units: list[str], sources: dict[str, RequirementSource], blocks: dict[str, DocumentBlock],
                   requirements: list[Requirement], plans: dict[str, Plan]) -> str:
    """원문 단위마다 요구사항 제목 목록, 담기지 않은 원문 문장, 판정할 요구사항(근거 블록 일부 포함)을 쓴다."""
    by_id = {r.requirement_id: r for r in requirements}
    parts = []
    for key in units:
        src, (uncovered, candidates) = sources[key], plans[key]
        parts += [f"## 원문 단위 [{key}] {src.name}" + (f" (원문 표 분류: {src.category})" if src.category else ""),
                  "### 요구사항 제목",
                  *[f"- [{r.requirement_id}] {r.title}" for r in requirements
                    if r.source_key == key and r.status != "EXCLUDED"]]
        if uncovered:
            parts += ["### 담기지 않은 원문 문장",
                      *[f"({bid}) {sentence}" for bid, sentence in uncovered]]
        if candidates:
            parts.append("### 판정할 요구사항")
            for rid, hints in candidates.items():
                r = by_id[rid]
                # 인용은 아래 근거 원문(인용 앞뒤)에 들어 있어 따로 쓰지 않는다
                parts.append(f"- [{rid}] 검토 대상 (지금: {r.type} / {r.scope} / {r.category}) {r.title}: {r.description}"
                             f" | 의심 이유: {'; '.join(hints)}")
                for e in {e.source_block_id: e for e in r.evidence}.values():
                    if e.source_block_id in blocks:
                        parts.append(f"  원문 ({e.source_block_id}): {_excerpt(blocks[e.source_block_id].text, e.quote)}")
        parts.append("")
    return "\n".join(parts)


# ------------------------------------------------------------------ (나) 충돌 검토
def _conflicts(requirements: list[Requirement], questions: list[Question], ask: bool) -> dict:
    """전체 요구사항에서 충돌을 찾아 확인 질문(CF-…)을 questions에 더하고 관련 요구사항에 CONFLICT 표시를 남긴다."""
    active = [r for r in requirements if r.status != "EXCLUDED"]
    by_id = {r.requirement_id: r for r in active}
    result = {"flagged": [], "questions": [], "failed": 0, "summary": ""}
    if len(active) < 2:
        result["summary"] = "대상 없음"
        return result
    try:
        out = get_llm().structured(system=CONFLICT_PROMPT, user="\n".join(["## 요구사항 목록", *map(_req_line, active)]),
                                   schema=ConflictReviewOut, effort=get_settings().req_review_effort,
                                   purpose=f"요구사항 충돌 검토 {len(active)}건")
    except LLMFatalError:
        raise   # 인증·크레딧 문제: 실행을 멈추고 체크포인트에서 재개
    except Exception as e:  # noqa: BLE001
        log.warning("[요구사항 검토] 충돌 검토 실패: %s", e)
        result.update(failed=1, summary="실패 (다음에 다시 검토)")
        return result

    seen = {frozenset(q.requirement_ids) for q in questions if q.question_id.startswith(CONFLICT_PREFIX)}
    number = max((int(q.question_id.split("-")[-1]) for q in questions if q.question_id.startswith(CONFLICT_PREFIX)),
                 default=0)
    dropped = repeated = compatible = 0
    for c in out.conflicts:
        if c.both_possible:             # 모델 스스로 함께 만족할 수 있다고 본 지적 (중복·보완·조건부)
            compatible += 1
            continue
        ids = sorted({i.strip() for i in c.requirement_ids if i.strip() in by_id})
        if len(ids) < 2:
            dropped += 1
            continue
        if frozenset(ids) in seen:      # 이미 물었다 (답했거나 보류)
            repeated += 1
            continue
        seen.add(frozenset(ids))
        number += 1
        qid = f"{CONFLICT_PREFIX}{number:03d}"
        recommended = c.recommended.strip()
        options = list(dict.fromkeys(o.strip() for o in ([recommended] if recommended else []) + c.options if o.strip()))
        context = [c.conflict.strip()]
        if c.incompatible.strip():
            context.append(f"양립 불가: {c.incompatible.strip()}")
        context.append(f"근거: {c.evidence.strip()}")
        questions.append(Question(
            question_id=qid, kind="CONFLICT", question=c.question.strip(),
            context="\n".join(context), options=options[:4], requirement_ids=ids,
            evidence_block_ids=list(dict.fromkeys(e.source_block_id for i in ids for e in by_id[i].evidence if e.verified)),
            blocking=True, stage="requirements", recommended=recommended, status="PENDING" if ask else "DEFERRED"))
        result["questions"].append(qid)
        # 기능 명세 Agent가 이 표시를 보고 추천안으로 규칙을 쓰고 '(확인 필요 CF-…)'를 붙인다.
        # 기능 규칙에 그대로 옮겨지므로 짧은 제목만 남기고, 자세한 내용은 질문 context에 둔다
        title = c.title.strip() or c.conflict.strip()
        for i in ids:
            others = ", ".join(x for x in ids if x != i)
            by_id[i].review.append(ReviewNote(
                kind="CONFLICT", question_id=qid,
                note=f"{others}와 충돌 — {title}" + (f" (추천안: {recommended})" if recommended else "")))
            result["flagged"].append(i)
    result["summary"] = (f"충돌 {len(result['questions'])}건 → 확인 질문 " + ", ".join(result["questions"])
                         if result["questions"] else "충돌 없음")
    if repeated:
        result["summary"] += f" (이미 물은 충돌 {repeated}건 제외)"
    if compatible:
        result["summary"] += f" (함께 만족할 수 있는 지적 {compatible}건 제외)"
    if dropped:
        result["summary"] += f" (대상 ID를 확인할 수 없는 지적 {dropped}건 제외)"
    return result


def _req_line(r: Requirement) -> str:
    """충돌 검토용 요구사항 한 줄: ID, 분류, 제목·설명, 수용 기준, 결정 (원문 인용은 설명과 겹쳐 넣지 않는다)."""
    line = f"- [{r.requirement_id}] ({label(current(r))}) {r.title} — {r.description}"
    if r.acceptance_criteria:
        line += f" | 수용 기준: {'; '.join(r.acceptance_criteria)}"
    for d in r.decisions:
        line += f" | 결정: {d}"
    return line


def _options(now: Cls, proposed: Cls) -> list[str]:
    """질문 선택지: 제안 분류(추천), 지금 분류, 그리고 둘이 다루지 않는 큰 갈래(기능 / 비기능 / 수행 의무)를 최대 4개."""
    chosen: list[Cls] = [proposed, now]
    for c in (("FUNCTIONAL", "SYSTEM", "기능"),
              ("NON_FUNCTIONAL", "SYSTEM", proposed[2] if proposed[2] != "기능" else "제약"),
              ("NON_FUNCTIONAL", "PROJECT", "사업관리")):
        if all(c[:2] != x[:2] for x in chosen):
            chosen.append(c)
    return list(dict.fromkeys(label(c) for c in chosen))[:4]


def _reclassify(r: Requirement, c: Cls, decision: str) -> None:
    r.type, r.scope, r.category = c
    r.decisions.append(decision)


def record_answer(q: Question, by_id: dict[str, Requirement]) -> bool:
    """요구사항 검토 질문(RC-…)의 답을 반영하고 표시를 지운다 (사용자 답변 기록 노드에서 부른다). 요구사항이 바뀌었으면 True.

    - 원문 외 추가 판단: '제외'면 범위에서 뺀다(EXCLUDED), 포함이면 확정만 한다.
    - 분류: 답이 선택지 형식이고 지금 분류와 다르면 그 분류로 고친다. 같거나 직접 입력한 답이면 결정으로만 남긴다.
    바뀌지 않았으면(추천과 같은 답) 기능 명세는 다시 설계하지 않고 규칙 표시만 '결정'으로 바꾼다."""
    changed = False
    for rid in q.requirement_ids:
        r = by_id.get(rid)
        if r is None:
            continue
        note = settle(r, q.question_id)
        if note is not None and note.kind == "ADDED":
            if q.answer.strip().startswith(EXCLUDE):
                r.status = "EXCLUDED"
                r.decisions.append(f"[{q.question_id}] 원문 외 추가 요구사항 → 제외 (사용자 답)")
                changed = True
                continue
            r.decisions.append(f"[{q.question_id}] 원문 외 추가 요구사항 → 포함 (사용자 답)")
        else:
            chosen = parse_label(q.answer)
            if chosen and chosen != current(r):
                _reclassify(r, chosen, f"[{q.question_id}] 분류 {label(current(r))} → {label(chosen)} (사용자 답)")
                changed = True
            else:
                r.decisions.append(f"[{q.question_id}] {q.question} → {q.answer}")
        if not r.unsettled:
            r.status = "CONFIRMED"
    return changed
