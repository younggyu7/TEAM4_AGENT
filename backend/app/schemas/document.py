"""문서 리비전의 위치 블록과 검증 가능한 원문 근거 계약.

파일 추출과 저장은 B/E 단계가 담당한다. 이 모델은 단계 간 전달 형식만 정의한다.
"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.design import StrictModel, Text


class PdfLocator(StrictModel):
    kind: Literal["pdf"]
    page: int = Field(ge=1)


class DocxParagraphLocator(StrictModel):
    kind: Literal["docx_paragraph"]
    paragraph_index: int = Field(ge=0)


class DocxTableCellLocator(StrictModel):
    kind: Literal["docx_table_cell"]
    table_index: int = Field(ge=0)
    row_index: int = Field(ge=0)
    cell_index: int = Field(ge=0)


class TxtLocator(StrictModel):
    kind: Literal["txt"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered_lines(self):
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


Locator = Annotated[
    PdfLocator | DocxParagraphLocator | DocxTableCellLocator | TxtLocator,
    Field(discriminator="kind"),
]


class DocumentBlock(StrictModel):
    document_revision_id: Text
    block_id: Text
    locator: Locator
    raw_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    normalization_version: Text

    @model_validator(mode="after")
    def has_content(self):
        if not self.normalized_text.strip():
            raise ValueError("normalized_text must contain visible text")
        return self


class EvidenceReference(StrictModel):
    document_revision_id: Text
    block_id: Text
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_offsets(self):
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self

    def matches(self, block: DocumentBlock) -> bool:
        """정규화 문자열의 정확한 구간만 근거로 인정한다."""
        return (
            self.document_revision_id == block.document_revision_id
            and self.block_id == block.block_id
            and self.end <= len(block.normalized_text)
            and block.normalized_text[self.start : self.end] == self.quote
        )
