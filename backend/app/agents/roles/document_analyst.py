"""업로드 문서의 비LLM 추출과 정확한 원문 근거 조회."""

from app.schemas.design_v1 import AnalysisInput
from app.schemas.document import EvidenceReference
from app.services.document_extraction import DocumentExtractionError


def find_exact_evidence(stage_input: AnalysisInput, block_id: str, quote: str) -> EvidenceReference:
    """지정 블록의 정규화 원문과 완전히 일치하는 첫 인용 구간만 찾는다."""
    blocks = [block for block in stage_input.blocks if block.block_id == block_id]
    if len(blocks) != 1 or not quote or not quote.strip():
        raise DocumentExtractionError("invalid_evidence_reference", "인용할 원문 블록이나 문구가 없습니다.")
    block = blocks[0]
    start = block.normalized_text.find(quote)
    if start < 0:
        raise DocumentExtractionError("invalid_evidence_reference", "인용 문구가 지정한 원문 블록과 일치하지 않습니다.")
    reference = EvidenceReference(
        document_revision_id=stage_input.document_revision_id,
        block_id=block_id,
        start=start,
        end=start + len(quote),
        quote=quote,
    )
    if not reference.matches(block):
        raise DocumentExtractionError("invalid_evidence_reference", "원문 근거 위치를 확인할 수 없습니다.")
    return reference
