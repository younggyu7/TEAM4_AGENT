"""데이터 설계: 기능 명세(입력)로 엔티티·속성·관계·제약(출력)을 만든다. ERD·DDL은 이 모델에서 코드가 만든다.

  입력: FunctionSpec (schemas/function.py), 사람이 고친 data_model.json
  출력: DataModel (Entity·Attribute·Relation·제약), DbValidationResult (DDL을 별도 DB에 적용한 검증 결과)
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DataType = Literal["bigint", "integer", "decimal", "varchar", "text", "boolean", "date", "timestamp", "jsonb", "uuid"]


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
