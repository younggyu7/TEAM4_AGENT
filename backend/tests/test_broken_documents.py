"""깨진·글자 없는 입력 문서 처리 테스트.

고객 문서를 정제하는 과정에서 생기는 모양을 다룬다.
- PDF를 HWPX로 변환한 문서: 행 앞뒤 빈 칸, 한 표에 요구사항 여러 개, 칸 이름과 값이 붙은 셀, 문단으로 풀린 표,
  글상자 속 요구사항 표, 쪽 넘김으로 잘린 표 뒷부분
- 스캔본(글자 층이 없는 PDF): 지원하지 않으므로 입력 단계에서 막는다. 일부 그림 페이지만 건너뛴다
"""
import zipfile
from pathlib import Path

import pytest

from app.agents.tools.doc_quality import build_sources, check_document
from app.agents.tools.document_parser import (UnsupportedFormatError, _repair_requirements, check_supported,
                                              document_codes, parse_document, textless_pages,
                                              parse_requirement_table)

ROOT = Path(__file__).resolve().parents[1]


def _req(code: str, name: str, details: str, outputs: str | None = None) -> list[list[str]]:
    rows = [["요구사항 번호", code, "요구사항 분류", "보안 요구사항"], ["요구사항 이름", name], ["요구사항 내용", details]]
    return rows + ([["산출정보", outputs]] if outputs is not None else [])


def _fields(items: list[dict]) -> dict[str, dict[str, str]]:
    tables = [parse_requirement_table(i["rows"]) for i in items if i["type"] == "table"]
    return {f["code"]: f for f in tables if "code" in f}


def test_repair_converted_requirement_tables():
    raw = [
        # 행 앞뒤 빈 칸 + 같은 표 안에 붙은 다음 요구사항('SER-02요구사항 분류…')
        {"type": "table", "rows": [["", "요구사항 번호", "SER-01", "요구사항 분류", "보안 요구사항", ""],
                                   ["요구사항 이름", "계정 관리"], ["요구사항 내용", "계정을 월 1회 점검"],
                                   ["산출정보", "점검표"],
                                   ["", "요구사항 번호 요구사항 이름\n요구사항 내용", "SER-02요구사항 분류보안 요구사항 접속기록 관리\n"
                                    "접속기록을 1년 보관", ""]]},
        # 한 표에 요구사항 두 개 (번호 행이 두 번), 두 번째는 쪽 넘김으로 잘림
        {"type": "table", "rows": _req("SER-03", "암호화", "전송 구간 암호화", "") + [[""]]
            + _req("SER-04", "백신", "백신을 설치하고")},
        {"type": "table", "rows": [["", "", "", ""], ["", ""]]},                       # 틀만 남은 빈 표
        {"type": "table", "rows": [["", "", "매일 갱신", ""], ["산출정보", "백신 현황"]]},   # SER-04의 잘린 뒷부분
        # 문단으로 풀린 표
        {"type": "paragraph", "text": "요구사항 번호 요구사항 이름 요구사항 내용"},
        {"type": "paragraph", "text": "SER-05요구사항 분류보안 요구사항 보안 교육"},
        {"type": "paragraph", "text": "참여 인력에게 보안 교육을 실시하여야 함"},
        # 칸 이름과 값이 따로 온 글상자
        {"type": "paragraph", "text": "요구사항 번호"},
        {"type": "paragraph", "text": "요구사항 이름"},
        {"type": "paragraph", "text": "SER-06"},
        {"type": "paragraph", "text": "보안 점검"},
        {"type": "paragraph", "text": "요구사항 분류보안 요구사항"},
        {"type": "paragraph", "text": "요구사항 내용"},
        {"type": "paragraph", "text": "분기별 취약점 점검을 수행하여야 함"},
        {"type": "paragraph", "text": "산출정보"},
        {"type": "paragraph", "text": "점검 결과보고서"},
        {"type": "paragraph", "text": "7) 품질 요구사항 (Quality Requirement)"},
    ]
    items = _repair_requirements(raw)
    got = _fields(items)
    assert list(got) == ["SER-01", "SER-02", "SER-03", "SER-04", "SER-05", "SER-06"]
    assert got["SER-01"]["category"] == "보안 요구사항", "행 앞뒤 빈 칸이 있어도 칸 이름·값 짝이 맞음"
    assert (got["SER-02"]["name"], got["SER-02"]["details"]) == ("접속기록 관리", "접속기록을 1년 보관")
    assert got["SER-04"]["details"] == "백신을 설치하고\n매일 갱신", "잘린 뒷부분은 산출정보 칸이 없는 요구사항에 붙음"
    assert got["SER-04"]["outputs"] == "백신 현황" and "outputs" not in got["SER-03"]
    assert (got["SER-05"]["category"], got["SER-05"]["name"]) == ("보안 요구사항", "보안 교육")
    assert got["SER-06"] == {"code": "SER-06", "name": "보안 점검", "category": "보안 요구사항",
                             "details": "분기별 취약점 점검을 수행하여야 함", "outputs": "점검 결과보고서"}
    assert items[-1] == raw[-1], "다음 절 제목은 그대로 문단"


def test_id_only_table_joins_next_labeled_table():
    """PDF에서 '요구사항 고유번호 | SFR-001' 표와 명칭·세부내용·산출물 표가 따로 나뉜 경우 하나로 붙인다 (고객요구사항13 형식)"""
    raw = [{"type": "table", "rows": [["요구사항 고유번호", "SFR-001"]]},
           {"type": "table", "rows": [["요구사항 명칭", "예산 관리"], ["요구사항 세부내용", "■ 예산편성: 신청금액을 입력한다."],
                                      ["산출물", "검증 결과"]]},
           {"type": "table", "rows": [["요구사항 고유번호", "SFR-002"]]},
           {"type": "table", "rows": [["요구사항 명칭", "인사 관리"], ["요구사항 세부내용", "■ 인사정보를 관리한다."],
                                      ["산출물", "검증 결과"]]}]
    got = _fields(_repair_requirements(raw))
    assert list(got) == ["SFR-001", "SFR-002"]
    assert (got["SFR-001"]["name"], got["SFR-001"]["details"]) == ("예산 관리", "■ 예산편성: 신청금액을 입력한다.")
    assert got["SFR-002"]["outputs"] == "검증 결과"


def test_normal_requirement_table_is_untouched():
    raw = [{"type": "table", "rows": [["고유번호", "SFR-001"], ["명칭", "요청 등록"], ["분류", "기능 요구사항"],
                                      ["설명", "세부내용", "요청을 등록한다"]]},
           {"type": "paragraph", "text": "나. 성능 요구사항"}]
    assert _repair_requirements(raw) == raw


def test_sub_table_rows_inside_details_are_kept():
    fields = parse_requirement_table([
        ["요구사항 번호", "SFR-04", "요구사항 분류", "시스템 기능 요구사항"], ["요구사항 이름", "솔루션 신청"],
        ["요구사항 내용", "신청 기능 개발"], ["구분", "분류", "중분류", ""], ["신청", "공급기업 페이지", "솔루션 등록"],
        ["산출정보", ""]])
    assert fields["category"] == "시스템 기능 요구사항", "하위 표 머리글 '구분 | 분류'가 분류를 덮지 않음"
    assert fields["details"] == "신청 기능 개발\n구분 | 분류 | 중분류\n신청 | 공급기업 페이지 | 솔루션 등록"


_HWPX_SECTION = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
  <hp:p><hp:run><hp:t>보안 요구사항</hp:t></hp:run></hp:p>
  <hp:p><hp:run><hp:tbl>
    <hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>요구사항 번호</hp:t></hp:run></hp:p></hp:subList></hp:tc>
           <hp:tc><hp:subList><hp:p><hp:run><hp:t>SER-01</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>
    <hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>요구사항 이름</hp:t></hp:run></hp:p></hp:subList></hp:tc>
           <hp:tc><hp:subList><hp:p><hp:run><hp:t>계정 관리</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>
    <hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>요구사항 내용</hp:t></hp:run></hp:p></hp:subList></hp:tc>
           <hp:tc><hp:subList><hp:p><hp:run><hp:t>계정을 점검한다</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>
  </hp:tbl>
  <hp:container><hp:polygon><hp:drawText><hp:subList>
    <hp:p><hp:run><hp:tbl>
      <hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>요구사항 번호</hp:t></hp:run></hp:p></hp:subList></hp:tc>
             <hp:tc><hp:subList><hp:p><hp:run><hp:t>SER-</hp:t></hp:run><hp:run><hp:t>02</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>
      <hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>요구사항 이름</hp:t></hp:run></hp:p></hp:subList></hp:tc>
             <hp:tc><hp:subList><hp:p><hp:run><hp:t>접속기록 관리</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>
      <hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>요구사항 내용</hp:t></hp:run></hp:p></hp:subList></hp:tc>
             <hp:tc><hp:subList><hp:p><hp:run><hp:t>접속기록을 보관한다</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>
    </hp:tbl></hp:run></hp:p>
  </hp:subList></hp:drawText></hp:polygon></hp:container>
  </hp:run></hp:p>
  <hp:p><hp:run><hp:t>SER-03 항목은 별도 협의</hp:t></hp:run></hp:p>
</hs:sec>"""


def test_hwpx_text_box_contents_and_unextracted_codes(tmp_path):
    path = tmp_path / "converted.hwpx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("Contents/section0.xml", _HWPX_SECTION)
    blocks = parse_document(path)
    codes = [b.fields["code"] for b in blocks if b.block_type == "requirement_table"]
    assert codes == ["SER-01", "SER-02"], "글상자 속 표('SER-' + '02'로 나뉜 글자)도 요구사항 표로 읽음"
    assert document_codes(path) == {"SER-01", "SER-02", "SER-03"}
    sources = build_sources(blocks)
    notes = [f for f in check_document(blocks, sources, document_codes(path))
             if f.kind == "DOC_REQ_CODE_NOT_EXTRACTED"]
    assert len(notes) == 1 and "SER-03" in notes[0].message, "원문에만 있고 추출되지 않은 번호를 알림"


def _pdf(path: Path, pages: list[bool]) -> Path:
    """페이지마다 글자 있음(True)·없음(False)을 정해 PDF를 만든다. 글자 없는 페이지는 스캔본처럼 선·도형만 있다."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    width, height = A4
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for has_text in pages:
        if has_text:
            pdf.setFont("HYSMyeongJo-Medium", 11)
            for i, line in enumerate(["Ⅰ. 사업 개요", "1. 추진 배경", "□ 서비스 요청 접수와 담당자 배정 기능을 구축한다.",
                                      "□ 모든 화면은 3초 이내에 응답해야 한다."]):
                pdf.drawString(60, height - 80 - i * 20, line)
        else:
            pdf.rect(60, 100, width - 120, height - 200)
            pdf.line(60, height / 2, width - 60, height / 2)
        pdf.showPage()
    pdf.save()
    return path


def test_scanned_pdf_is_rejected_before_parsing(tmp_path, capsys):
    """스캔본(글자 없는 페이지가 절반 초과)은 OCR하지 않고 입력 단계에서 막고, 다시 만드는 방법을 안내한다."""
    pytest.importorskip("reportlab")
    from app import cli

    path = _pdf(tmp_path / "scanned.pdf", [False, False, True])
    with pytest.raises(UnsupportedFormatError) as caught:
        check_supported(path)
    assert "스캔본" in str(caught.value) and "PDF로 저장" in caught.value.hint and "3쪽 중 2쪽" in caught.value.hint
    with pytest.raises(UnsupportedFormatError):
        parse_document(path)
    assert cli.main(["parse", str(path)]) == 2
    assert "스캔본 PDF는 지원하지 않습니다" in capsys.readouterr().out


def test_textless_page_in_text_pdf_is_skipped_with_warning(tmp_path):
    """글자 있는 문서 안의 일부 그림·스캔 페이지는 건너뛰고 문서 품질 경고로 알린다."""
    pytest.importorskip("reportlab")
    path = _pdf(tmp_path / "mostly_text.pdf", [True, True, False])
    check_supported(path)
    blocks = parse_document(path)
    assert blocks and {b.location.page for b in blocks} == {1, 2}
    assert textless_pages(path) == [3]
    notes = [f for f in check_document(blocks, build_sources(blocks), textless_pages=textless_pages(path))
             if f.kind == "DOC_TEXTLESS_PAGES"]
    assert len(notes) == 1 and "p.3" in notes[0].message


def _data_files() -> list[Path]:
    folder = ROOT / "data"
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in {".hwpx", ".docx", ".pdf"}) if folder.exists() else []


@pytest.mark.parametrize("path", _data_files(), ids=lambda p: p.name)
def test_data_folder_documents(path):
    """data/ 폴더의 실제 입력 문서 점검: 지원 형식이고(스캔본 아님), 원문의 요구사항 번호가 모두 추출됐고,
    표마다 번호·명칭·내용이 있다."""
    check_supported(path)
    blocks = parse_document(path)
    sources = build_sources(blocks)
    kinds = {f.kind for f in check_document(blocks, sources, document_codes(path))}
    assert not kinds & {"DOC_NO_TEXT", "DOC_REQ_CODE_NOT_EXTRACTED"}, kinds
    for b in blocks:
        if b.block_type == "requirement_table":
            missing = [k for k in ("code", "name", "details") if not b.fields.get(k)]
            assert not missing, f"{b.source_block_id} {b.fields.get('code')}: {missing} 없음"
