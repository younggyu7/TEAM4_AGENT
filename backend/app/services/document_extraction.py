"""PDF·DOCX·UTF-8 TXT에서 위치가 있는 원문 블록을 추출한다.

입력 파일은 내부 비공개 저장소의 불변 리비전이어야 한다. 추출은 시간 제한을 둔
별도 프로세스에서 수행하며, 일부만 읽힌 문서는 성공 결과로 반환하지 않는다.
"""

import asyncio
import hashlib
import multiprocessing
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from app.core.exceptions import ValidationFailed
from app.schemas.document import DocumentBlock

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DOCX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_NORMALIZED_CHARS = 30_000
NORMALIZATION_VERSION = "1"
EXTRACTION_TIMEOUT_SECONDS = 30
_READ_CHUNK_BYTES = 64 * 1024
_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class DocumentExtractionError(ValidationFailed):
    """업로드 문서가 추출 계약을 만족하지 못한 이유를 코드로 전달한다."""

    def __init__(self, code: str, message: str, *, status_code: int = 422):
        super().__init__(message, code=code, status_code=status_code)


@dataclass(frozen=True)
class ExtractionResult:
    document_revision_id: str
    sha256: str
    normalization_version: str
    blocks: list[DocumentBlock]
    chunks: list["DocumentChunk"]
    warnings: list[str]


@dataclass(frozen=True)
class DocumentChunk:
    """원문 블록을 빼거나 고치지 않는 분석 단위 후보."""

    kind: str
    block_ids: list[str]
    oversized: bool


def _chunk_key(block: DocumentBlock) -> tuple[str, int | None]:
    locator = block.locator
    if locator.kind == "docx_table_cell":
        return "table", locator.table_index
    if locator.kind == "pdf":
        return ("table" if "\t" in block.raw_text else "body"), locator.page
    return "body", None


def plan_document_chunks(blocks: list[DocumentBlock], *, max_chars: int = 3000) -> list[DocumentChunk]:
    """연속 본문·표를 위치 순서로 묶는다. 큰 단일 블록은 절삭하지 않고 표시한다."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks = []
    current_ids = []
    current_key = None
    current_chars = 0

    def flush():
        if current_ids:
            chunks.append(DocumentChunk(current_key[0], current_ids.copy(), current_chars > max_chars))

    for block in blocks:
        key = _chunk_key(block)
        additional = len(block.normalized_text) + (1 if current_ids else 0)
        if current_ids and (key != current_key or current_chars + additional > max_chars):
            flush()
            current_ids = []
            current_chars = 0
            additional = len(block.normalized_text)
        current_key = key
        current_ids.append(block.block_id)
        current_chars += additional
    flush()
    return chunks


def normalize_text(text: str) -> str:
    """인용의 숫자·부호를 보존하고 줄바꿈과 수평 공백만 정규화한다."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return unicodedata.normalize("NFC", "\n".join(re.sub(r"[ \t\f\v]+", " ", line).strip() for line in lines).strip())


def _file_digest(path: Path) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    total = 0
    header = b""
    try:
        with path.open("rb") as source:
            while chunk := source.read(_READ_CHUNK_BYTES):
                if not header:
                    header = chunk[:8]
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise DocumentExtractionError(
                        "file_too_large", "입력 파일은 10 MiB 이하여야 합니다.", status_code=413
                    )
                digest.update(chunk)
    except OSError as exc:
        raise DocumentExtractionError("invalid_document", "입력 파일을 읽을 수 없습니다.") from exc
    if total == 0:
        raise DocumentExtractionError("document_empty", "빈 파일은 분석할 수 없습니다.")
    return digest.hexdigest(), header


def _preflight(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt"}:
        raise DocumentExtractionError("unsupported_format", "PDF·DOCX·UTF-8 TXT 파일만 지원합니다.")
    if path.is_symlink() or not path.is_file():
        raise DocumentExtractionError("invalid_document", "저장된 일반 파일만 분석할 수 있습니다.")
    digest, header = _file_digest(path)
    if suffix == ".pdf" and not header.startswith(b"%PDF-"):
        raise DocumentExtractionError("invalid_document", "PDF 확장자와 파일 형식이 일치하지 않습니다.")
    if suffix == ".docx" and not header.startswith(b"PK\x03\x04"):
        raise DocumentExtractionError("invalid_document", "DOCX 확장자와 파일 형식이 일치하지 않습니다.")
    if suffix == ".txt" and (header.startswith(b"%PDF-") or header.startswith(b"PK\x03\x04")):
        raise DocumentExtractionError("invalid_document", "TXT 확장자와 파일 형식이 일치하지 않습니다.")
    return suffix, digest


def _block(revision_id: str, index: int, locator: dict, text: str) -> DocumentBlock | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    return DocumentBlock(
        document_revision_id=revision_id,
        block_id=f"B{index:06d}",
        locator=locator,
        raw_text=text,
        normalized_text=normalized,
        normalization_version=NORMALIZATION_VERSION,
    )


def _validate_content(blocks: list[DocumentBlock]) -> None:
    if not blocks:
        raise DocumentExtractionError("document_empty", "추출 가능한 문자가 없습니다. 스캔 문서인지 확인하세요.")
    joined = "\n".join(block.normalized_text for block in blocks)
    if not re.search(r"\S", joined):
        raise DocumentExtractionError("document_empty", "추출 가능한 문자가 없습니다.")
    if len(joined) > MAX_NORMALIZED_CHARS:
        raise DocumentExtractionError("document_too_long", "추출 본문은 정규화 후 30,000자 이하여야 합니다.")


def _extract_txt(path: Path, revision_id: str) -> list[DocumentBlock]:
    try:
        content = path.read_bytes().decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise DocumentExtractionError("invalid_encoding", "TXT 파일은 UTF-8로 저장해야 합니다.") from exc
    if "\x00" in content:
        raise DocumentExtractionError("invalid_encoding", "TXT 파일에 바이너리 문자가 포함되어 있습니다.")
    blocks = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        block = _block(
            revision_id, len(blocks) + 1, {"kind": "txt", "start_line": line_number, "end_line": line_number}, line
        )
        if block is not None:
            blocks.append(block)
    return blocks


def _check_docx_package(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            files = archive.infolist()
            if sum(item.file_size for item in files) > MAX_DOCX_EXPANDED_BYTES:
                raise DocumentExtractionError("docx_expansion_limit", "DOCX 압축 해제 크기는 50 MiB 이하여야 합니다.")
            names = {item.filename for item in files}
            if len(names) != len(files):
                raise DocumentExtractionError("invalid_document", "DOCX 파일에 중복된 항목이 있습니다.")
            if not {"[Content_Types].xml", "word/document.xml"} <= names:
                raise DocumentExtractionError("invalid_document", "DOCX 본문이 없습니다.")
            if any(item.flag_bits & 0x1 for item in files):
                raise DocumentExtractionError("invalid_document", "암호화된 DOCX는 지원하지 않습니다.")
            for name in names:
                if not name.endswith(".rels"):
                    continue
                root = ElementTree.fromstring(archive.read(name))
                if any(
                    rel.attrib.get("TargetMode", "").lower() == "external"
                    and not rel.attrib.get("Type", "").endswith("/hyperlink")
                    for rel in root.iter(f"{{{_RELS_NS}}}Relationship")
                ):
                    raise DocumentExtractionError("external_resource", "외부 리소스가 포함된 DOCX는 지원하지 않습니다.")
            root = ElementTree.fromstring(archive.read("word/document.xml"))
            if any(
                next(root.iter(f"{{{_DOCX_NS['w']}}}{name}"), None) is not None
                for name in ("txbxContent", "altChunk", "sdt")
            ):
                raise DocumentExtractionError("partial_extraction", "DOCX의 일부 본문을 추출할 수 없습니다.")
            for name in ("word/footnotes.xml", "word/endnotes.xml"):
                if name in names and ElementTree.fromstring(archive.read(name)).find(".//w:t", _DOCX_NS) is not None:
                    raise DocumentExtractionError("partial_extraction", "DOCX 각주·미주의 내용을 추출할 수 없습니다.")
            if archive.testzip() is not None:
                raise DocumentExtractionError("invalid_document", "DOCX 압축 파일이 손상되었습니다.")
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
        raise DocumentExtractionError("invalid_document", "DOCX 파일이 손상되었습니다.") from exc


def _cell_text(cell) -> str:
    from docx.table import Table

    parts = []
    for item in cell.iter_inner_content():
        if isinstance(item, Table):
            seen = set()
            for row in item.rows:
                cells = []
                for nested in row.cells:
                    if nested._tc in seen:
                        continue
                    seen.add(nested._tc)
                    cells.append(_cell_text(nested))
                parts.append("\t".join(cells))
        elif item.text.strip():
            parts.append(item.text)
    return "\n".join(parts)


def _extract_docx(path: Path, revision_id: str) -> list[DocumentBlock]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    _check_docx_package(path)
    try:
        document = Document(str(path))
        blocks = []
        paragraph_index = 0
        table_index = 0
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                paragraph = Paragraph(child, document)
                block = _block(
                    revision_id,
                    len(blocks) + 1,
                    {"kind": "docx_paragraph", "paragraph_index": paragraph_index},
                    paragraph.text,
                )
                paragraph_index += 1
                if block is not None:
                    blocks.append(block)
            elif tag == "tbl":
                table = Table(child, document)
                seen_cells = set()
                for row_index, row in enumerate(table.rows):
                    for cell_index, cell in enumerate(row.cells):
                        identity = cell._tc
                        if identity in seen_cells:
                            continue
                        seen_cells.add(identity)
                        block = _block(
                            revision_id,
                            len(blocks) + 1,
                            {
                                "kind": "docx_table_cell",
                                "table_index": table_index,
                                "row_index": row_index,
                                "cell_index": cell_index,
                            },
                            _cell_text(cell),
                        )
                        if block is not None:
                            blocks.append(block)
                table_index += 1
        return blocks
    except (ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise DocumentExtractionError("invalid_document", "DOCX 파일을 읽을 수 없습니다.") from exc


def _inside(box: tuple, character: dict) -> bool:
    center_x = (character["x0"] + character["x1"]) / 2
    center_y = (character["top"] + character["bottom"]) / 2
    return box[0] <= center_x < box[2] and box[1] <= center_y < box[3]


def _extract_pdf(path: Path, revision_id: str) -> list[DocumentBlock]:
    import pdfplumber
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise DocumentExtractionError("encrypted_pdf", "암호화된 PDF는 지원하지 않습니다.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentExtractionError("pdf_page_limit", "PDF는 50페이지 이하여야 합니다.")
        blocks = []
        with pdfplumber.open(str(path)) as document:
            if len(document.pages) != len(reader.pages):
                raise DocumentExtractionError(
                    "partial_extraction", "PDF 페이지 수가 일치하지 않아 분석을 중단했습니다."
                )
            textless = []
            for page_number, page in enumerate(document.pages, start=1):
                page_text = page.extract_text() or ""
                if not page_text.strip() or (page.images and len(re.sub(r"\s+", "", page_text)) < 20):
                    if page.images or page.rects or page.curves or page.lines:
                        textless.append(page_number)
                    continue
                tables = page.find_tables()
                boxes = [table.bbox for table in tables]
                body = (
                    page.filter(
                        lambda item, bounding_boxes=boxes: not any(_inside(box, item) for box in bounding_boxes)
                    )
                    if boxes
                    else page
                )
                ordered = []
                for line in body.extract_text_lines():
                    if line["text"].strip():
                        ordered.append((line["top"], line["text"]))
                for table in tables:
                    for row_index, row in enumerate(table.extract()):
                        text = "\t".join(cell.strip() for cell in row if cell and cell.strip())
                        if text:
                            ordered.append((table.bbox[1] + row_index * 0.001, text))
                ordered.sort(key=lambda item: item[0])
                for _, text in ordered:
                    block = _block(revision_id, len(blocks) + 1, {"kind": "pdf", "page": page_number}, text)
                    if block is not None:
                        blocks.append(block)
            if textless:
                labels = ", ".join(map(str, textless))
                if len(textless) == len(document.pages):
                    raise DocumentExtractionError(
                        "scanned_pdf", "PDF에서 추출 가능한 글자가 없습니다. 스캔본을 확인하세요."
                    )
                raise DocumentExtractionError(
                    "partial_extraction", f"텍스트를 읽을 수 없는 PDF 페이지가 있습니다: {labels}쪽."
                )
        return blocks
    except DocumentExtractionError:
        raise
    except Exception as exc:
        raise DocumentExtractionError("invalid_document", "PDF 파일을 읽을 수 없습니다.") from exc


def _extract_file(path: Path, revision_id: str, suffix: str) -> list[DocumentBlock]:
    if suffix == ".txt":
        blocks = _extract_txt(path, revision_id)
    elif suffix == ".docx":
        blocks = _extract_docx(path, revision_id)
    else:
        blocks = _extract_pdf(path, revision_id)
    _validate_content(blocks)
    return blocks


def _worker(path: str, revision_id: str, expected_digest: str, sender) -> None:
    try:
        source = Path(path)
        suffix, digest = _preflight(source)
        if digest != expected_digest:
            raise DocumentExtractionError("document_changed", "추출 도중 입력 파일이 변경되었습니다.")
        blocks = _extract_file(source, revision_id, suffix)
        sender.send(("ok", [block.model_dump() for block in blocks]))
    except DocumentExtractionError as exc:
        sender.send(("error", exc.code, exc.message, exc.status_code))
    except Exception:
        sender.send(("error", "extraction_failed", "문서 추출에 실패했습니다.", 500))
    finally:
        sender.close()


def extract_document(
    path: Path | str,
    document_revision_id: str,
    *,
    timeout_seconds: float = EXTRACTION_TIMEOUT_SECONDS,
) -> ExtractionResult:
    """검증 완료된 블록만 반환한다. 시간 초과·부분 추출은 명시적 실패다."""
    source = Path(path)
    suffix, digest = _preflight(source)
    if not document_revision_id.strip():
        raise DocumentExtractionError("invalid_revision", "문서 리비전 ID가 필요합니다.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    receiver, sender = multiprocessing.get_context("spawn").Pipe(duplex=False)
    process = multiprocessing.get_context("spawn").Process(
        target=_worker, args=(str(source), document_revision_id, digest, sender), daemon=True
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout_seconds):
            raise DocumentExtractionError("extraction_timeout", "문서 추출 시간이 초과되었습니다.", status_code=504)
        try:
            payload = receiver.recv()
        except EOFError as exc:
            raise DocumentExtractionError(
                "extraction_failed", "추출 프로세스가 비정상 종료되었습니다.", status_code=500
            ) from exc
        if payload[0] == "error":
            raise DocumentExtractionError(payload[1], payload[2], status_code=payload[3])
        blocks = [DocumentBlock.model_validate(block) for block in payload[1]]
        if _file_digest(source)[0] != digest:
            raise DocumentExtractionError("document_changed", "추출 도중 입력 파일이 변경되었습니다.")
        chunks = plan_document_chunks(blocks)
        long_blocks = [chunk.block_ids[0] for chunk in chunks if chunk.oversized]
        warnings = [f"분석 단위 상한을 넘는 원문 블록이 있습니다: {', '.join(long_blocks)}"] if long_blocks else []
        return ExtractionResult(document_revision_id, digest, NORMALIZATION_VERSION, blocks, chunks, warnings)
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join()
        receiver.close()


async def analyze_document(path: Path, document_revision_id: str) -> ExtractionResult:
    """문서 역할과 업로드 흐름이 함께 쓰는 검증된 비동기 추출 경계."""
    result = await asyncio.to_thread(extract_document, path, document_revision_id)
    block_ids = [block.block_id for block in result.blocks]
    chunk_ids = [block_id for chunk in result.chunks for block_id in chunk.block_ids]
    if (
        result.document_revision_id != document_revision_id
        or not block_ids
        or len(set(block_ids)) != len(block_ids)
        or any(block.document_revision_id != document_revision_id for block in result.blocks)
        or chunk_ids != block_ids
    ):
        raise DocumentExtractionError("partial_extraction", "문서 추출 결과에 누락되거나 중복된 블록이 있습니다.")
    return result
