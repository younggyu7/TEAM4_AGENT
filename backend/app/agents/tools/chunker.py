"""RFP 구조 기반 청커: 요구사항 상세 표가 없는 문서를 요구사항 추출 단위로 나눈다.

공공 RFP·과업지시서는 '조·장·절' 대신 아래 계층을 쓴다. 파서가 블록마다 붙인 제목 경로(heading_path)를 따라 자른다.
    Ⅰ. 사업 개요 / 1. 사업 개요 → 장 (로마 숫자가 없는 문서는 '1.'이 최상위)
    [붙임1] / [별지 제3호]      → 부록 장 (heading_path[0])
    1. 사업 목적               → 절 (heading_path[1])
    가. 기능 요구사항           → 항 (heading_path[2])
    □ 항목 / ○ 세부 / - 설명    → 본문 안의 항목 (크기가 넘칠 때 □ 앞에서 자른다)

규칙
1. 같은 제목 경로(장·절·항)에 속한 연속 블록을 한 단위로 묶는다. 단위의 이름·위치는 제목 경로다.
2. 한 단위가 MAX_CHARS를 넘으면 블록 경계에서 나누되, 가능하면 '□' 항목이 시작되는 곳에서 자른다.
3. 제목 줄만 있는 단위(예: 'Ⅱ. 과업 내용' 바로 뒤에 '1. …'이 오는 경우)는 같은 장의 다음 단위 앞에 붙인다.
   본문이 있으면 짧아도 독립 단위로 둔다 (그래야 단위 이름이 내용과 맞는다).
4. 제안서 작성·평가·입찰 안내·계약·서식처럼 시스템 요구가 아닌 장은 건너뛰고, 건너뛴 제목은 보고한다.
   첨부·붙임·별지·별표 같은 부록 장(서식·평가표·참조표)도 건너뛴다. 요구사항 본문은 본 장에 있다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas.document import DocumentBlock, RequirementSource

MAX_CHARS = 3000      # 요구사항 추출 요청 1건에 넣을 본문 상한 (글자 수)
SECTION_DEPTH = 3     # 제목 경로 몇 단계까지를 한 단위로 볼지 (장·절·항)

# 시스템 요구사항이 아닌 RFP 장·절 (제목 경로 중 하나라도 맞으면 건너뜀)
SKIP_SECTION = re.compile(
    r"제안서\s*(작성|제출|평가)|제안\s*안내|제안\s*관련\s*문의|평가\s*(및|기준|방법|항목)|입찰\s*(안내|참가|공고|및)"
    r"|낙찰|참가\s*자격|계약\s*(조건|체결)|계약에\s*관한|사업자\s*선정|제안\s*조건"
    r"|서식|별지|보안\s*특약|체크리스트|이행\s*확약|참조표|산정표|심의\s*결과|영향\s*평가"
)
# 부록 장 제목: '[첨부 1]', '[붙임3] …', '별지 제2호 …' (제목이 이 형식으로 시작할 때만. '가. 첨부파일 관리'는 해당 없음)
APPENDIX_HEADING = re.compile(r"^\[?\s*(첨부|붙임|별지|별표|서식)\s*(제\s*)?\d")
ITEM_START = ("□", "■")


@dataclass
class ChunkPlan:
    """청킹 결과: 추출 단위 목록과, 요구사항이 아니라서 건너뛴 장·절 제목."""
    sources: list[RequirementSource] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def plan_chunks(blocks: list[DocumentBlock]) -> ChunkPlan:
    """블록 목록을 제목 경로 단위로 묶고, 크기 상한·건너뛰기 규칙을 적용해 추출 단위를 만든다."""
    plan = ChunkPlan()
    pending: list[DocumentBlock] = []     # 제목 줄만 있어서 다음 단위 앞에 붙일 블록
    for path, members in _group_by_section(blocks):
        if any(SKIP_SECTION.search(h) or APPENDIX_HEADING.match(h) for h in path):
            label = " > ".join(path)
            if label not in plan.skipped:
                plan.skipped.append(label)
            pending = []
            continue
        # 대기 중인 제목 줄 중 지금 단위의 상위 제목(조상)만 앞에 붙인다. 옆 가지의 빈 제목은 버린다.
        members = [b for b in pending if _is_ancestor(b.location.heading_path, path)] + members
        pending = []
        if _only_headings(members):
            pending = members
            continue
        for piece in _split_oversize(members):
            _add_source(plan, path, piece)
    return plan


def _group_by_section(blocks: list[DocumentBlock]) -> list[tuple[tuple[str, ...], list[DocumentBlock]]]:
    """제목 경로(앞 SECTION_DEPTH 단계)가 같은 연속 블록끼리 묶는다."""
    groups: list[tuple[tuple[str, ...], list[DocumentBlock]]] = []
    for b in blocks:
        path = tuple(b.location.heading_path[:SECTION_DEPTH])
        if groups and groups[-1][0] == path:
            groups[-1][1].append(b)
        else:
            groups.append((path, [b]))
    return groups


def _split_oversize(members: list[DocumentBlock]) -> list[list[DocumentBlock]]:
    """MAX_CHARS를 넘는 단위를 블록 경계에서 나눈다. 절반 이상 찼으면 '□' 항목 앞에서 먼저 자른다."""
    if _chars(members) <= MAX_CHARS:
        return [members]
    pieces: list[list[DocumentBlock]] = [[]]
    size = 0
    for b in members:
        starts_item = b.text.lstrip().startswith(ITEM_START)
        full = size + len(b.text) > MAX_CHARS
        if pieces[-1] and (full or (starts_item and size >= MAX_CHARS // 2)):
            pieces.append([])
            size = 0
        pieces[-1].append(b)
        size += len(b.text)
    return [p for p in pieces if p]


def _add_source(plan: ChunkPlan, path: tuple[str, ...], members: list[DocumentBlock]) -> None:
    """묶은 블록으로 추출 단위(RequirementSource)를 만든다. 키는 C01, C02…"""
    heading = " > ".join(path)
    name = path[-1] if path else members[0].text.splitlines()[0][:40]
    plan.sources.append(RequirementSource(
        source_key=f"C{len(plan.sources) + 1:02d}", name=name,
        block_ids=[b.source_block_id for b in members], heading=heading,
    ))


def _chars(members: list[DocumentBlock]) -> int:
    """블록들의 본문 글자 수 합."""
    return sum(len(b.text) for b in members)


def _only_headings(members: list[DocumentBlock]) -> bool:
    """제목 줄만 있고 본문이 없는 묶음인지 확인한다. ('Ⅰ |  | 사업 개요' 같은 1행 제목 표도 제목으로 본다)"""
    def squash(text: str) -> str:
        return re.sub(r"[\s|]", "", text)
    return all(squash(b.text) in {squash(h) for h in b.location.heading_path} for b in members)


def _is_ancestor(heading_path: list[str], path: tuple[str, ...]) -> bool:
    """heading_path가 path와 같거나 그 상위 경로인지 확인한다."""
    depth = min(len(heading_path), SECTION_DEPTH)
    return tuple(heading_path[:depth]) == path[:depth]
