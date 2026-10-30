"""문서 분석: 업로드한 고객 문서(입력)를 블록으로 나누고 요구사항 추출 단위를 만든다(출력).

  입력: 문서 파일 (PDF·DOCX·HWPX·TXT, tools/document_parser)
  출력: DocumentBlock (원문 블록·위치), RequirementSource (요구사항 Agent에 넘길 추출 단위)
그래프 상태에는 model_dump()한 dict로 두고, 각 Agent는 경계에서 model_validate()로 읽는다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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
