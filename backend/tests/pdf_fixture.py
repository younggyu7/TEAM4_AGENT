"""테스트용 RFP 형식 PDF 생성기 (reportlab): 제목 계층 + 병합셀이 있는 요구사항 표 + 페이지를 넘는 긴 표."""
from __future__ import annotations

from pathlib import Path


def make_rfp_pdf(path: Path) -> Path:
    """'고유번호' 요구사항 표와 두 페이지에 걸친 일반 표가 있는 PDF를 만든다."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    style = ParagraphStyle("ko", fontName="HYSMyeongJo-Medium", fontSize=10, leading=14)
    grid = TableStyle([("FONTNAME", (0, 0), (-1, -1), "HYSMyeongJo-Medium"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                       ("GRID", (0, 0), (-1, -1), 0.5, colors.black)])

    req_rows = [
        ["고유번호", "SFR-001", "", "", ""],
        ["명칭", "서비스 요청 관리", "", "", ""],
        ["분류", "기능 요구사항", "", "응락 수준", "필수"],
        ["설명", "정의", "서비스 요청 등록과 처리", "", ""],
        ["", "세부내용", "사용자는 서비스 요청을 등록한다", "", ""],
        ["산출정보", "솔루션 구축 문서", "", "", ""],
    ]
    req_table = Table(req_rows, colWidths=[60, 60, 150, 70, 60])
    req_table.setStyle(TableStyle(grid.getCommands() + [
        ("SPAN", (1, 0), (4, 0)), ("SPAN", (1, 1), (4, 1)), ("SPAN", (1, 2), (2, 2)),
        ("SPAN", (0, 3), (0, 4)), ("SPAN", (2, 3), (4, 3)), ("SPAN", (2, 4), (4, 4)), ("SPAN", (1, 5), (4, 5)),
    ]))
    long_rows = [["번호", "항목", "비고"]] + [[str(i), f"점검 항목 {i}", "정상"] for i in range(1, 61)]
    long_table = Table(long_rows, colWidths=[50, 200, 100])
    long_table.setStyle(grid)

    story = [Paragraph(t, style) for t in ("Ⅱ. 제안 요청 내용", "5. 요구사항 목록", "나. 기능 요구사항")]
    story += [Spacer(1, 6), req_table, Spacer(1, 12), Paragraph("6. 점검 목록", style), long_table]
    SimpleDocTemplate(str(path), pagesize=A4).build(story)
    return path


def make_decorated_pdf(path: Path) -> Path:
    """실제 RFP PDF에서 본 장식 요소를 흉내 낸다.

    - 장 제목 상자: 글자를 한 칸씩 넣은 표 + 그림자 효과(흰 글자 위에 1.4pt 어긋난 검은 글자 → '과과업업')
    - 요구사항 표 세부내용 칸 안의 중첩 표
    - 꼬리말 쪽번호 '- 1 -'
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = "HYSMyeongJo-Medium"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    style = ParagraphStyle("ko", fontName=font, fontSize=10, leading=14)
    grid = TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 9),
                       ("GRID", (0, 0), (-1, -1), 0.5, colors.black)])

    class Shadow(Flowable):
        def __init__(self, text):
            super().__init__()
            self.text, self.width, self.height = text, 18, 18

        def draw(self):
            self.canv.setFont(font, 16)
            self.canv.setFillColor(colors.white)
            self.canv.drawString(0, 1.4, self.text)
            self.canv.setFillColor(colors.black)
            self.canv.drawString(1.4, 0, self.text)

    title_box = Table([[Shadow(ch) for ch in "Ⅳ과업내용"]], colWidths=[28] * 5)
    title_box.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    # 테두리만 있어 표로 잡히지 않는 제목 상자 → 'Ⅴ 기 타 사 항' 한 줄
    plain_box = Table([[Shadow(ch) for ch in "Ⅴ기타사항"]], colWidths=[28] * 5)
    plain_box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1, colors.black)]))
    inner = Table([["항목", "기준"], ["길이", "9자 이상"]], colWidths=[50, 70])
    inner.setStyle(grid)
    req_table = Table([
        ["고유번호", "SER-001"],
        ["명칭", "접속 비밀번호 관리"],
        ["분류", "보안 요구사항"],
        ["세부내용", [Paragraph("다음 규칙을 따른다", style), inner]],
    ], colWidths=[70, 260])
    req_table.setStyle(grid)

    def footer(canvas, doc):
        canvas.setFont(font, 9)
        canvas.drawCentredString(A4[0] / 2, 20, f"- {doc.page} -")

    story = [title_box, Spacer(1, 10), Paragraph("1. 보안 요건", style), Spacer(1, 6), req_table,
             Spacer(1, 20), plain_box, Spacer(1, 10), Paragraph("□ 기타 사항은 협의하여 정한다.", style)]
    SimpleDocTemplate(str(path), pagesize=A4).build(story, onFirstPage=footer, onLaterPages=footer)
    return path


def make_printed_pdf(path: Path) -> Path:
    """인쇄한 PDF(Microsoft Print to PDF)처럼 원래 문서의 쪽 경계가 쪽 가운데에 온 모양.

    요구사항 표가 원래 쪽 경계에서 둘로 잘리고, 그 사이에 원래 꼬리말 쪽 번호('- 18 -')가 본문 줄로 낀다.
    - SFR-001: 뒷부분이 빈 첫 칸으로 시작 (세부내용 칸 세로 병합의 남은 줄)
    - SFR-002: 뒷부분이 칸 이름으로 시작 (번호 행 바로 뒤에서 잘림)
    - SFR-003: 온전한 표 뒤의 일반 표 ('구분 | 내용') → 붙이지 않음
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = "HYSMyeongJo-Medium"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    style = ParagraphStyle("ko", fontName=font, fontSize=10, leading=14)
    footer = ParagraphStyle("footer", parent=style, alignment=1, fontSize=9)
    grid = TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 9),
                       ("GRID", (0, 0), (-1, -1), 0.5, colors.black)])

    def table(rows: list[list[str]]) -> Table:
        t = Table(rows, colWidths=[90, 300])
        t.setStyle(grid)
        return t

    def page_break(no: int) -> list:
        return [Spacer(1, 16), Paragraph(f"- {no} -", footer), Spacer(1, 30)]

    story = [Paragraph("나. 기능 요구사항", style), Spacer(1, 6),
             table([["요구사항 분류", "기능 요구사항"], ["요구사항 고유번호", "SFR-001"], ["요구사항 명칭", "설비 등록"],
                    ["정의", "설비 정보 등록"], ["세부내용", "설비를 등록할 수 있어야 함"]]),
             *page_break(17),
             table([["", "등록한 설비를 수정할 수 있어야 함"], ["산출정보", "기능정의서"]]),
             Spacer(1, 12),
             table([["요구사항 분류", "기능 요구사항"], ["요구사항 고유번호", "SFR-002"]]),
             *page_break(18),
             table([["요구사항 명칭", "자동 보고서 생성"], ["정의", "가동 현황 보고서 생성"],
                    ["세부내용", "보고서를 정기적으로 생성하여야 함"], ["산출정보", "화면설계서"]]),
             Spacer(1, 12),
             table([["요구사항 분류", "기능 요구사항"], ["요구사항 고유번호", "SFR-003"], ["요구사항 명칭", "알림"],
                    ["세부내용", "이상 발생 시 알림을 보내야 함"]]),
             *page_break(19),
             table([["구분", "내용"], ["알림 방식", "문자, 메일"]])]
    SimpleDocTemplate(str(path), pagesize=A4).build(story)
    return path


def make_open_side_pdf(path: Path) -> Path:
    """좌우 바깥 세로선 없이 가로선과 안쪽 세로선만 있는 요구사항 표 두 개 (사이에 제목 문단)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = "HYSMyeongJo-Medium"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    style = ParagraphStyle("ko", fontName=font, fontSize=10, leading=14)
    open_side = TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 9),
                            ("LINEABOVE", (0, 0), (-1, 0), 1, colors.black),
                            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.black),
                            ("LINEAFTER", (0, 0), (0, -1), 0.5, colors.black)])   # 안쪽 세로선만

    def req_table(code: str, name: str) -> Table:
        rows = [["고유번호", code], ["요구사항 명칭", name], ["요구사항 분류", "기능 요구사항"],
                ["정의", f"{name} 기능을 제공한다"], ["세부 내용", f"사용자는 {name}을 할 수 있다"]]
        table = Table(rows, colWidths=[90, 300])
        table.setStyle(open_side)
        return table

    story = [Paragraph("나. 기능 요구사항", style), Spacer(1, 6), req_table("SFR-001", "회원 가입"),
             Spacer(1, 12), Paragraph("다. 보안 요구사항", style), Spacer(1, 6), req_table("SER-001", "접근 통제")]
    SimpleDocTemplate(str(path), pagesize=A4).build(story)
    return path
