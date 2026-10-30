"""요구사항 추출 단위 구성과 문서 품질 검사 (결정적).

- 요구사항 상세 표가 있으면 표 단위로, 없으면 RFP 제목 계층(Ⅰ → 1. → 가.) 기준으로 나눈다 (chunker.py).
- 공공 RFP 관례(요구사항 번호 접두어, 목록표 ↔ 상세표)를 이용해 중복 번호·목록 누락·분류 불일치를 경고한다.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from app.schemas.document import DocumentBlock, RequirementSource
from app.schemas.review import Issue
from app.agents.flow.state import finding
from app.agents.tools.chunker import plan_chunks

CODE_RE = re.compile(r"^[A-Z]{2,4}-\d{2,4}$")

# 행정안전부 요구사항 분류 관례 (접두어 → 분류명에 포함되어야 할 단어)
PREFIX_CATEGORY = {
    "ECR": "장비", "SFR": "기능", "PER": "성능", "SIR": "인터페이스", "DAR": "데이터", "TER": "테스트",
    "SER": "보안", "QUR": "품질", "COR": "제약", "PMR": "프로젝트관리", "PSR": "프로젝트지원",
}
_HEADING_PREFIX = re.compile(r"^[가-하]\.\s*")
_CODE_TAG = re.compile(r"\(\s*[A-Z]{2,4}\s*\)")              # 분류명 뒤에 적은 번호 접두어: '시스템 장비구성(ECR)'
_BULLET = re.compile(r"^[‣•·▪◦○●□■◆◇▶▷*\-–]+\s*")          # 목록표 명칭 앞 글머리표: '‣ 상품 Master DB 구축'


def _squash(text: str) -> str:
    """비교용으로 공백을 모두 지운다."""
    return re.sub(r"\s+", "", text or "")


def _category_key(text: str) -> str:
    """분류명 비교용 핵심어: 공백·괄호 속 접두어·끝의 '요구사항'·'사항'을 뗀다 ('제약 사항'·'제약(COR)' → '제약')."""
    key = _squash(_CODE_TAG.sub("", text or ""))
    for suffix in ("요구사항", "사항"):
        if key.endswith(suffix):
            return key[:-len(suffix)]
    return key


def _is_category_name(text: str) -> bool:
    """요구사항 분류명인지 ('보안 요구사항'·'제약사항'·'투입인력 요건'·'지원 (PSR)').

    분류 칸에 업무 묶음 이름('MRO 지급 신청서 - 모바일')을 쓰는 문서도 있어, 그런 값은 분류 검사에서 뺀다.
    """
    return bool(_CODE_TAG.search(text or "")) or _squash(text).endswith(("요구사항", "사항", "요건"))


# 표 밖 본문의 의무 문장 ('…하여야 함', '…할 것')
OBLIGATION = re.compile(r"(하여야|해야)\s*(함|한다|하며|하고|할)|할\s*것")
NARRATIVE_MIN_OBLIGATIONS = 3   # 이 수 이상이면 '요구사항 표 밖 요구'로 본다


def build_sources(blocks: list[DocumentBlock], include_narrative: bool = False) -> list[RequirementSource]:
    """요구사항 추출 단위를 만든다. 요구사항 상세 표가 있으면 표 단위, 없으면 제목 계층 기반 청크.

    include_narrative=True면 표가 있는 문서에서도 표 밖 본문 중 의무 문장이 많은 절(예: '2. 일반 요건')을 추가한다.
    """
    req_tables = [b for b in blocks if b.block_type == "requirement_table"]
    if req_tables:
        sources = _sources_from_tables(req_tables)
        return sources + narrative_sources(blocks) if include_narrative else sources
    return plan_chunks(blocks).sources


def narrative_sources(blocks: list[DocumentBlock]) -> list[RequirementSource]:
    """요구사항 표가 없는 본문 단위 중 의무 문장이 NARRATIVE_MIN_OBLIGATIONS개 이상인 것 (키 N01, N02 …).

    요구사항 표를 둔 RFP도 '일반 요건'·'일반사항' 같은 본문에 수행 의무를 따로 적는 경우가 많다.
    입찰·평가·서식 장은 청커 규칙대로 건너뛴다.
    """
    block_map = {b.source_block_id: b for b in blocks}
    found = []
    for src in plan_chunks(blocks).sources:
        parts = [block_map[i] for i in src.block_ids]
        if any(b.block_type == "requirement_table" for b in parts):
            continue
        if len(OBLIGATION.findall(_text(block_map, src))) >= NARRATIVE_MIN_OBLIGATIONS:
            found.append(src.model_copy(update={"source_key": f"N{len(found) + 1:02d}"}))
    return found


def _sources_from_tables(tables: list[DocumentBlock]) -> list[RequirementSource]:
    """요구사항 상세 표를 추출 단위로 만든다. 이어지는 표는 병합하고, 중복 번호는 _2, _3을 붙여 유일하게 한다."""
    merged: list[dict] = []
    for b in tables:
        code, name = b.fields.get("code", "").strip(), b.fields.get("name", "").strip()
        prev = merged[-1] if merged else None
        # 같은 번호·명칭의 표가 연달아 나오면 (1/2), (2/2)처럼 이어지는 표로 보고 병합
        if prev and code and prev["code"] == code and prev["name"] == name:
            prev["block_ids"].append(b.source_block_id)
            continue
        merged.append({"code": code, "name": name, "category": b.fields.get("category", ""),
                       "block_ids": [b.source_block_id], "heading": " > ".join(b.location.heading_path)})
    seen: Counter[str] = Counter()
    sources = []
    for i, m in enumerate(merged, start=1):
        if m["code"]:
            seen[m["code"]] += 1
            key = m["code"] if seen[m["code"]] == 1 else f"{m['code']}_{seen[m['code']]}"
        else:
            key = f"RT{i:02d}"
        sources.append(RequirementSource(source_key=key, **m))
    return sources


def check_document(blocks: list[DocumentBlock], sources: list[RequirementSource],
                   document_codes: set[str] | None = None, textless_pages: list[int] | None = None) -> list[Issue]:
    """문서 자체의 품질 문제. 수정 담당이 없는 보고용 이슈(target_agent=none)다.

    document_codes: 원문 전체에 나오는 요구사항 번호(document_parser.document_codes). 주면 추출 누락을 확인한다.
    textless_pages: 글자가 없어 건너뛴 PDF 페이지(document_parser.textless_pages). 스캔본은 입력 단계에서 막으므로
                    여기 오는 것은 그림 페이지 등 일부 페이지뿐이다.
    """
    found: list[Issue] = []
    by = "document_analyst"
    if not blocks:
        return [finding("DOC_NO_TEXT", "ERROR",
                        "문서에서 텍스트를 추출하지 못했습니다. 스캔본·이미지 PDF는 지원하지 않습니다 "
                        "→ 원본 프로그램에서 'PDF로 저장/내보내기'로 다시 만들어 주세요.", raised_by=by)]
    if textless_pages:
        found.append(finding("DOC_TEXTLESS_PAGES", "WARNING",
                             f"글자가 없는 PDF 페이지 {len(textless_pages)}쪽은 읽지 않았습니다: p."
                             + ",".join(map(str, textless_pages))
                             + " — 그림이나 스캔된 페이지에 요구사항이 있으면 원문을 확인하세요.", raised_by=by))
    detail = [s for s in sources if s.code]
    if not detail:
        found.append(finding("DOC_NO_REQUIREMENT_TABLE", "INFO",
                             "요구사항 상세 표가 없어 본문을 제목 계층(Ⅰ → 1. → 가.) 단위로 나눠 분석합니다.",
                             raised_by=by))
        skipped = plan_chunks(blocks).skipped
        if skipped:
            found.append(finding("DOC_SECTIONS_SKIPPED", "INFO",
                                 f"요구사항이 아닌 것으로 보고 건너뛴 장·절 {len(skipped)}개: " + "; ".join(skipped[:10])
                                 + (" …" if len(skipped) > 10 else ""), raised_by=by))
        return found

    block_map = {b.source_block_id: b for b in blocks}
    codes = Counter(s.code for s in detail)
    for code, n in codes.items():
        if n > 1:
            names = ", ".join(f"'{s.name}'" for s in detail if s.code == code)
            found.append(finding("DOC_DUPLICATE_REQ_CODE", "WARNING",
                                 f"요구사항 번호 {code}가 서로 다른 요구사항 {n}건에 중복 사용됨: {names}",
                                 target_id=code, raised_by=by))
    doc_prefixes = prefix_categories_from_summary(blocks)
    misplaced = _misplaced_by_heading(detail, block_map)
    for s in detail:
        prefix = re.split(r"[-_ ]", s.code)[0].upper()
        mismatch = ""
        # 업무 묶음 이름을 쓴 분류 칸은 비교하지 않고, '지원 (PSR)'처럼 분류에 접두어를 적었으면 일치로 본다
        category = s.category if _is_category_name(s.category) else ""
        if category and f"({prefix})" not in _squash(category):
            if prefix in doc_prefixes:                     # 문서 요약표에 정의된 접두어가 우선
                if not _same_category(doc_prefixes[prefix], category):
                    mismatch = f"문서 요약표상 '{doc_prefixes[prefix]}'"
            elif prefix in PREFIX_CATEGORY:                # 없으면 행정안전부 관례로 확인
                if PREFIX_CATEGORY[prefix] not in _squash(category):
                    mismatch = f"관례상 '{PREFIX_CATEGORY[prefix]}'"
        if mismatch:
            found.append(finding("DOC_CODE_CATEGORY_MISMATCH", "WARNING",
                                 f"{s.source_key}: 번호 접두어 {prefix}({mismatch})와 표의 분류 '{s.category}'가 다름",
                                 target_id=s.source_key, raised_by=by))
        if s.source_key in misplaced:
            found.append(finding("DOC_HEADING_CATEGORY_MISMATCH", "WARNING",
                                 f"{s.source_key}: 목차 제목 '{misplaced[s.source_key]}' 아래에 분류 '{s.category}' 요구사항이 있음",
                                 target_id=s.source_key, raised_by=by))
        details = "".join(block_map[bid].fields.get("details", "") for bid in s.block_ids if bid in block_map)
        if not details.strip():
            found.append(finding("DOC_EMPTY_DETAIL", "WARNING", f"{s.source_key}: 세부내용이 비어 있음",
                                 target_id=s.source_key, raised_by=by))

    found.extend(_check_prefix_consistency(detail))
    listed = _check_list_vs_detail(blocks, detail)
    found.extend(listed)
    found.extend(_check_unextracted_codes(blocks, detail, document_codes, {f.target_id for f in listed}))
    if not any(s.source_key.startswith("N") and not s.code for s in sources):   # 이미 추출 대상에 넣었으면 생략
        narrative = narrative_sources(blocks)
        if narrative:
            counts: Counter[str] = Counter()     # 길어서 여러 단위로 나뉜 절은 합쳐서 센다
            for n in narrative:
                counts[n.heading] += len(OBLIGATION.findall(_text(block_map, n)))
            labels = "; ".join(f"{h} ({c}문장)" for h, c in list(counts.items())[:8])
            found.append(finding("DOC_NARRATIVE_OUTSIDE_TABLES", "INFO",
                                 f"요구사항 표 밖 본문에 '…하여야 함' 같은 의무 문장이 있는 절 {len(counts)}개는 "
                                 f"추출 대상이 아닙니다: {labels}" + (" …" if len(counts) > 8 else "")
                                 + " — 포함하려면 run --include-narrative", raised_by=by))
    return found


def _text(block_map: dict[str, DocumentBlock], src: RequirementSource) -> str:
    return "\n".join(block_map[i].text for i in src.block_ids if i in block_map)


_SUMMARY_PREFIX = re.compile(r"([가-힣][가-힣\s]*?(?:요구사항|제약사항))\s*[-–—(]\s*\(?([A-Z]{3})\b")


def prefix_categories_from_summary(blocks: list[DocumentBlock]) -> dict[str, str]:
    """문서의 요구사항 총괄·요약표에서 '번호 접두어 → 분류명'을 읽는다.

    예) '유지관리 수행 요구사항 – MAR', '기능 요구사항(SFR)', '데이터 요구사항\\n(DAR, …)' → {'MAR': '유지관리 수행 요구사항', …}
    발주기관마다 접두어가 달라(MAR·MHR·INR 등) 고정 관례 대신 문서에 적힌 정의를 우선한다.
    """
    found: dict[str, str] = {}
    for b in blocks:
        if b.block_type != "table":
            continue
        for row in b.rows:
            for cell in row:
                for name, prefix in _SUMMARY_PREFIX.findall(cell):
                    found.setdefault(prefix, re.sub(r"\s+", " ", name).strip())
    return found


def _same_category(a: str, b: str) -> bool:
    """분류명이 같은지 (공백·괄호 속 접두어·'요구사항'·'사항' 차이와 포함 관계는 같은 것으로 본다)."""
    x, y = _category_key(a), _category_key(b)
    return x == y or (bool(x) and bool(y) and (x in y or y in x))


def _misplaced_by_heading(detail: list[RequirementSource], block_map: dict[str, DocumentBlock]) -> dict[str, str]:
    """분류명 제목('라. 성능 요구사항') 아래에 다른 분류의 요구사항이 있으면 {source_key: 제목}.

    - '다. 요구사항별 상세 내용'처럼 분류명이 아닌 제목과, 업무 묶음 이름을 쓴 분류 칸은 보지 않는다.
    - '나. 비기능 요구사항'·'가. 사용자 요구사항'처럼 여러 분류를 묶는 제목(아래 분류가 둘 이상이고 어느 것도 제목과
      같지 않음)은 보지 않는다. 분류가 하나뿐이면 묶음 제목인지 알 수 없어 그대로 알린다.
    """
    sections: dict[tuple[str, ...], list[RequirementSource]] = defaultdict(list)
    for s in detail:
        first = block_map.get(s.block_ids[0])
        path = tuple(first.location.heading_path) if first else ()
        if path and _HEADING_PREFIX.match(path[-1]) and _is_category_name(path[-1]) and _is_category_name(s.category):
            sections[path].append(s)
    out: dict[str, str] = {}
    for path, items in sections.items():
        wrong = [s for s in items if not _same_category(_HEADING_PREFIX.sub("", path[-1]), s.category)]
        umbrella = len(wrong) == len(items) and len({_category_key(s.category) for s in items}) > 1
        if not umbrella:
            out.update({s.source_key: path[-1] for s in wrong})
    return out


def _check_prefix_consistency(detail: list[RequirementSource]) -> list[Issue]:
    """같은 분류의 요구사항이 대부분 같은 접두어를 쓰는데 하나만 다르면 알린다 (예: SER-001~003 다음 SR-004).

    원문 번호는 고치지 않는다. 오기인지 의도인지는 담당자가 판단한다.
    """
    by_category: dict[str, list[RequirementSource]] = defaultdict(list)
    for s in detail:
        if s.category:
            by_category[_squash(s.category)].append(s)
    found = []
    for items in by_category.values():
        prefixes = Counter(re.split(r"[-_ ]", s.code)[0].upper() for s in items)
        major, n = prefixes.most_common(1)[0]
        if len(prefixes) < 2 or n < 2 or n <= len(items) / 2:
            continue
        for s in items:
            prefix = re.split(r"[-_ ]", s.code)[0].upper()
            if prefix != major and prefixes[prefix] == 1:
                found.append(finding("DOC_CODE_PREFIX_INCONSISTENT", "WARNING",
                                     f"{s.source_key}: 같은 분류 '{s.category}'의 다른 요구사항은 {major}- 번호인데 "
                                     f"이 요구사항만 {prefix}- 입니다. 원문 번호는 그대로 두었으니 오기인지 확인하세요.",
                                     target_id=s.source_key, raised_by="document_analyst"))
    return found


def _check_unextracted_codes(blocks: list[DocumentBlock], detail: list[RequirementSource],
                             document_codes: set[str] | None, reported: set[str]) -> list[Issue]:
    """원문에 나오는 요구사항 번호 중 추출 단위가 되지 않은 것 (표가 깨졌거나 글상자처럼 읽지 못한 영역).

    같은 접두어(SFR·COR …)를 쓰는 번호만 본다 ('ISO-9001' 같은 다른 번호 제외). 목록표 대조에서 이미 알린 번호도 제외.
    """
    parsed = {s.code for s in detail}
    prefixes = {c.split("-")[0] for c in parsed}
    mentioned = document_codes if document_codes is not None else {
        m for b in blocks for m in re.findall(r"(?<![A-Za-z])([A-Z]{2,6}-\d{1,4})(?!\d)", b.text)}
    missing = sorted(c for c in mentioned if c.split("-")[0] in prefixes and c not in parsed and c not in reported)
    if not missing:
        return []
    return [finding("DOC_REQ_CODE_NOT_EXTRACTED", "WARNING",
                    f"원문에 있는 요구사항 번호 {len(missing)}개가 추출 단위가 되지 않았습니다: {', '.join(missing[:15])}"
                    + (" …" if len(missing) > 15 else "")
                    + " — 표가 깨졌거나(변환 문서) 다른 요구사항 본문에서 언급만 된 번호일 수 있으니 원문을 확인하세요.",
                    raised_by="document_analyst")]


def _check_list_vs_detail(blocks: list[DocumentBlock], detail: list[RequirementSource]) -> list[Issue]:
    """요구사항 목록표(번호·명칭 나열)와 상세 표를 대조한다. 문서 앞쪽의 첫 목록표를 기준으로 한다."""
    by = "document_analyst"
    list_table = next((b for b in blocks if b.block_type == "table"
                       and sum(1 for r in b.rows for c in r if CODE_RE.match(c.strip())) >= 3), None)
    if not list_table:
        return []
    listed: dict[str, list[str]] = defaultdict(list)
    for row in list_table.rows:
        cells = [c.strip() for c in row]
        for i, c in enumerate(cells):
            if CODE_RE.match(c):
                listed[c].append(_BULLET.sub("", cells[i + 1]) if i + 1 < len(cells) else "")
    detailed: dict[str, list[str]] = defaultdict(list)
    for s in detail:
        detailed[s.code].append(s.name)

    found = []
    where = f"목록표({list_table.source_block_id})"
    for code in listed:
        if code not in detailed:
            found.append(finding("DOC_DETAIL_MISSING", "WARNING",
                                 f"{where}의 {code} '{listed[code][0]}'에 대한 상세 요구사항 표가 없음",
                                 target_id=code, raised_by=by))
    for code in detailed:
        if code not in listed:
            found.append(finding("DOC_NOT_IN_LIST", "WARNING",
                                 f"상세 요구사항 {code} '{detailed[code][0]}'가 {where}에 없음",
                                 target_id=code, raised_by=by))
    for code, names in listed.items():
        for i, list_name in enumerate(names):
            if i >= len(detailed.get(code, [])):
                break
            a, b = _squash(list_name).replace("요구사항", ""), _squash(detailed[code][i]).replace("요구사항", "")
            if a and b and a != b and a not in b and b not in a:
                found.append(finding("DOC_NAME_MISMATCH", "INFO",
                                     f"{code}: 목록 명칭 '{list_name}' ≠ 상세 명칭 '{detailed[code][i]}'",
                                     target_id=f"{code}#{i + 1}", raised_by=by))
    return found
