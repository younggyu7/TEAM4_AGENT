"""공통 데이터 계약 (인수인계 문서 7장 '최소 합의 필드'를 baseline으로 구체화).

- 그래프 상태에는 model_dump() 한 dict를 저장하고, 각 Agent는 경계에서 model_validate()로 읽는다.
  (체크포인트 직렬화를 단순하게 유지하기 위함)
- LLM 응답 전용 스키마는 각 Agent 모듈에 따로 둔다. 여기는 시스템 내부 계약이다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RequirementType = Literal["FUNCTIONAL", "NON_FUNCTIONAL"]
RequirementScope = Literal["SYSTEM", "PROJECT"]  # SYSTEM: 시스템 요구 / PROJECT: 사업 수행 의무
Priority = Literal["MUST", "SHOULD", "COULD", "UNSPECIFIED"]  # UNSPECIFIED: 원문에 우선순위 근거가 없음
RequirementCategory = Literal[
    "기능", "성능", "보안", "인터페이스", "데이터", "품질", "제약", "장비구성", "테스트", "사업관리", "사업지원",
]
AgentName = Literal[
    "document_analyst", "requirements_analyst", "function_designer", "data_modeler", "trace_reviewer", "none",
]
DataType = Literal["bigint", "integer", "decimal", "varchar", "text", "boolean", "date", "timestamp", "jsonb", "uuid"]


# ---------------------------------------------------------------- 문서
class BlockLocation(BaseModel):
    """원문 블록의 위치: 섹션, 문서 내 순번, 페이지(PDF), 제목 경로."""
    section: int = 0
    order: int = 0                       # 문서 내 블록 순번
    page: int | None = None              # PDF만 해당 (HWPX는 레이아웃 정보가 없어 None)
    heading_path: list[str] = Field(default_factory=list)

    def label(self) -> str:
        """사람이 읽는 위치 문자열 (예: 'Ⅱ 제안 요청 내용 > 나. 기능 요구사항 (p.3)')."""
        path = " > ".join(self.heading_path) or "본문"
        return f"{path}" + (f" (p.{self.page})" if self.page else "")


class DocumentBlock(BaseModel):
    """파서가 만든 원문 최소 단위(문단·표·요구사항 표). 요구사항 근거는 source_block_id로 이 블록을 가리킨다."""
    document_id: str
    document_version: str
    source_block_id: str                 # B0001 …
    block_type: Literal["paragraph", "table", "requirement_table"]
    location: BlockLocation
    text: str
    rows: list[list[str]] = Field(default_factory=list)      # 표인 경우 원본 셀
    fields: dict[str, str] = Field(default_factory=dict)     # 요구사항 상세 표 파싱 결과


class RequirementSource(BaseModel):
    """요구사항 추출 단위. 상세 요구사항 표(이어지는 표는 병합) 또는 본문 청크."""
    source_key: str                      # 문서 내 유일 키 (예: SFR-003, 중복이면 ECR-001_2)
    code: str = ""                       # 원문 요구사항 번호
    name: str = ""
    category: str = ""
    block_ids: list[str]
    heading: str = ""


# ---------------------------------------------------------------- 요구사항·질문
class Evidence(BaseModel):
    """요구사항의 원문 근거: 블록 ID + 원문 그대로의 인용문 (verified = 인용문이 원문에서 확인됨)."""
    source_block_id: str
    quote: str
    verified: bool = False               # quote가 실제 블록 원문에 존재하는지


class ReviewNote(BaseModel):
    """요구사항 검토 Agent가 남긴 표시. question_id가 있으면 고객 확인 대기 중이다 (답을 받으면 표시를 지운다).

    CLASSIFICATION: 분류 근거가 애매함 — 기능이면 '기능(추가 여부 판단 필요)', 비기능이면 '비기능(판단 필요)'
    ADDED         : 원문에 근거가 없이 추가로 판단해 만든 요구사항
    CONFLICT      : 다른 요구사항과 동시에 만족할 수 없음 (추천안이 있으면 note에 함께 쓴다)
    SUPPLEMENTED  : 원문에 있는데 추출에서 빠져 요구사항 검토 Agent가 보충함 (근거가 원문에 있어 묻지 않는다)
    기능 명세 Agent는 확인 대기 표시가 있는 요구사항을 공통 결정에 쓰지 않고, 그 기능 규칙에 '(확인 필요 질문 ID)'를 붙인다.
    """
    kind: Literal["CLASSIFICATION", "ADDED", "CONFLICT", "SUPPLEMENTED"]
    note: str
    question_id: str = ""


class Requirement(BaseModel):
    """요구사항 하나. type(기능/비기능)·scope(시스템/사업 수행 의무)로 분류하고 원문 근거·사용자 결정을 함께 가진다."""
    requirement_id: str
    source_key: str = ""
    source_ref: str = ""                 # 원문 요구사항 번호
    type: RequirementType
    scope: RequirementScope
    category: str
    title: str
    description: str
    priority: Priority = "MUST"
    acceptance_criteria: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    # EXCLUDED: 사용자 의견으로 이번 범위에서 뺀 요구사항 (기능·데이터 설계 대상이 아니다. 기록은 남긴다)
    status: Literal["CONFIRMED", "ASSUMED", "OPEN", "EXCLUDED"] = "CONFIRMED"
    decisions: list[str] = Field(default_factory=list)       # 반영된 사용자 결정 문장
    review: list[ReviewNote] = Field(default_factory=list)   # 요구사항 검토 Agent의 표시 (확인 대기·보충)

    @property
    def designed(self) -> bool:
        """기능·데이터 설계 대상인 시스템 기능 요구사항인지."""
        return self.type == "FUNCTIONAL" and self.scope == "SYSTEM" and self.status != "EXCLUDED"

    @property
    def unsettled(self) -> list[ReviewNote]:
        """고객 확인을 기다리는 검토 표시 (분류 판단 필요·원문 외 추가·충돌)."""
        return [n for n in self.review if n.question_id]


USER_SOURCE = "USER"   # 사용자 의견으로 추가한 요구사항의 원문 단위 (원문 근거가 없다)


class Question(BaseModel):
    """확인 질문: 모호성·누락·충돌 유형, 선택지, 관련 요구사항, 답변 상태.

    stage: 질문을 낸 단계. requirements(요구사항 Agent) 답변은 요구사항 결정으로, data_modeler(데이터 설계) 답변은
    데이터 설계 결정으로 반영한다. 데이터 설계 질문은 추천 선택지(recommended)로 먼저 설계해 두고, 답이 다를 때만 다시 설계한다.
    function_designer(기능 명세) 질문도 추천안으로 먼저 설계하고, 답은 관련 요구사항의 결정으로 남기며, 답이 다를 때만 관련 기능을 고친다.
    """
    question_id: str
    kind: Literal["AMBIGUITY", "MISSING", "CONFLICT"]
    question: str
    context: str
    options: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    evidence_block_ids: list[str] = Field(default_factory=list)
    blocking: bool = False
    status: Literal["PENDING", "ANSWERED", "DEFERRED"] = "PENDING"
    answer: str = ""
    stage: str = "requirements"
    recommended: str = ""                # 추천 선택지 (데이터 설계 질문은 이 선택지로 먼저 설계)
    entity_ids: list[str] = Field(default_factory=list)
    confirms: list[str] = Field(default_factory=list)   # 답하면 확정(CONFIRMED)되는 요구사항 (기능 명세 질문이 확정 안 된 요구사항을 물은 경우)


QUESTION_STAGES = {"requirements": "요구사항", "requirement_reviewer": "요구사항 검토", "function_designer": "기능 명세", "data_modeler": "데이터 설계"}


# ---------------------------------------------------------------- 기능 명세
class FunctionIO(BaseModel):
    """기능의 입력·출력 항목."""
    name: str
    description: str = ""
    required: bool = True


class ExceptionFlow(BaseModel):
    """예외 흐름: 조건 → 처리."""
    condition: str
    handling: str


class FunctionSpec(BaseModel):
    """기능 명세 하나. requirement_ids로 근거 요구사항에, data_objects로 데이터 모델 설계에 이어진다."""
    function_id: str
    group: str                           # 생성 단위 (보통 원문 요구사항 번호)
    name: str
    description: str
    requirement_ids: list[str]
    nfr_ids: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    inputs: list[FunctionIO] = Field(default_factory=list)
    outputs: list[FunctionIO] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    normal_flow: list[str] = Field(default_factory=list)
    exception_flows: list[ExceptionFlow] = Field(default_factory=list)
    data_objects: list[str] = Field(default_factory=list)   # 데이터 모델 Agent 입력


# ---------------------------------------------------------------- 데이터 모델 (ERD·DDL의 공통 입력)
class Attribute(BaseModel):
    """엔티티 속성(컬럼): 타입·NULL 허용·키·참조(FK)·허용값."""
    name: str
    label: str = ""
    data_type: DataType = "varchar"
    length: int = 0
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    references: str = ""                 # 참조 엔티티명 (FK → 해당 엔티티의 PK)
    allowed_values: list[str] = Field(default_factory=list)
    description: str = ""


class RequiredWhen(BaseModel):
    """조건부 필수: when_column 값이 when_values 중 하나이면 column은 NULL일 수 없다."""
    column: str
    when_column: str
    when_values: list[str]


class CheckRule(BaseModel):
    """값 조건: column op value. value는 같은 엔티티의 다른 컬럼 이름이나 숫자 (예: amount > 0, end_at >= start_at)."""
    column: str
    op: Literal[">", ">=", "<", "<=", "<>"]
    value: str


class Entity(BaseModel):
    """엔티티(테이블). function_ids·requirement_ids로 기능·요구사항까지 추적한다."""
    entity_id: str                       # = 테이블명 (snake_case)
    label: str = ""
    description: str = ""
    kind: Literal["MASTER", "TRANSACTION", "HISTORY", "MAPPING", "CODE"] = "MASTER"
    attributes: list[Attribute] = Field(default_factory=list)
    function_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    # 카탈로그에서 정한 상위 엔티티(외래키로 참조할 대상). 속성을 다시 설계할 때 연결 의도를 잃지 않게 보관한다.
    parents: list[str] = Field(default_factory=list)
    # 여러 컬럼에 걸친 제약 (DDL에서 UNIQUE·CHECK로 만든다)
    unique_together: list[list[str]] = Field(default_factory=list)   # 컬럼 조합이 유일
    exactly_one_of: list[list[str]] = Field(default_factory=list)    # FK 중 정확히 하나만 채움 ("둘 중 하나만")
    required_when: list[RequiredWhen] = Field(default_factory=list)  # 조건부 필수
    checks: list[CheckRule] = Field(default_factory=list)            # 값 범위·순서·금지 조건 (DDL에서 CHECK)
    # 사용자가 직접 고친 엔티티: LLM이 다시 설계하지 않는다 (rerun --entities로 직접 지정할 때만 다시 설계)
    user_edited: bool = False

    def pk(self) -> list[Attribute]:
        """기본키 속성 목록."""
        return [a for a in self.attributes if a.primary_key]


class Relation(BaseModel):
    """FK 속성에서 도출한 엔티티 관계 (부모 1 : 자식 N 또는 1:1)."""
    relation_id: str
    parent: str                          # 참조되는 엔티티
    child: str                           # FK를 가진 엔티티
    fk_attribute: str
    cardinality: Literal["1:1", "1:N"]
    optional: bool                       # FK nullable 여부


class DataModel(BaseModel):
    """ERD와 DDL의 공통 입력인 구조화 데이터 모델."""
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)

    def entity(self, name: str) -> Entity | None:
        """이름으로 엔티티를 찾는다."""
        return next((e for e in self.entities if e.entity_id == name), None)


# ---------------------------------------------------------------- 검토·추적·실행
class Issue(BaseModel):
    """검사·검토에서 나온 이슈. target_agent가 수정 담당이고 attempts는 수정 시도 횟수다."""
    issue_id: str
    kind: str                            # 예: EVIDENCE_NOT_FOUND, MODEL_NO_PK, REQ_WITHOUT_FUNCTION, TRACE_FUNC_WITHOUT_ENTITY
    severity: Literal["ERROR", "WARNING", "INFO"]
    message: str
    target_agent: AgentName = "none"     # 수정 담당 (none = 보고만)
    target_id: str = ""                  # 수정 범위 (요구사항/기능/엔티티 ID)
    status: Literal["OPEN", "RESOLVED", "UNRESOLVED"] = "OPEN"
    attempts: int = 0
    raised_by: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """같은 이슈인지 판별하는 키 (종류, 대상)."""
        return (self.kind, self.target_id)


class TraceLink(BaseModel):
    """추적 관계 한 줄 (요구사항→기능, 비기능→기능, 기능→엔티티)."""
    from_id: str
    to_id: str
    link_type: Literal["REQ_FUNC", "NFR_FUNC", "FUNC_ENTITY"]


class CoordinatorDecision(BaseModel):
    """조정 Agent의 판단 한 건: 다음 행동, 수정 범위, 모드, 사유(선택 시점 상태), 결과(실행 후 요약)."""
    step: int
    action: Literal[
        "document_analyst", "requirements_analyst", "requirement_reviewer", "function_designer", "data_modeler",
        "trace_reviewer", "semantic_reviewer", "ask_user", "check_completion",
    ]
    scope: list[str] = Field(default_factory=list)
    mode: str = ""                       # 예: full / partial / fix / research
    reason: str                          # 이 행동을 고른 이유 (단계 시작 시 상태)
    result: str = ""                     # 행동을 실행한 뒤의 결과 요약 (결과 검사·결정 기록·완료 조건 검사가 채움)


class DesignVersion(BaseModel):
    """설계 버전 기록: 부모 버전, 변경 요약, 산출물 폴더. 번호는 '2', '2.1' 같은 문자열 (예전 숫자 번호도 받음)."""
    model_config = ConfigDict(coerce_numbers_to_str=True)
    design_version: str
    parent: str | None
    created_at: str
    summary: list[str]
    output_dir: str


class DbValidationResult(BaseModel):
    """격리 DB 검증 결과: 엔진, 통과 여부, 생성된 테이블·FK 수, 오류."""
    model_config = ConfigDict(coerce_numbers_to_str=True)
    engine: Literal["postgresql", "sqlite"]
    design_version: str
    passed: bool
    statements: int = 0
    tables_expected: int = 0
    tables_created: list[str] = Field(default_factory=list)
    foreign_keys: int = 0
    checks: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    failed_tables: list[str] = Field(default_factory=list)
    note: str = ""
