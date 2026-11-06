"""원문 목록 항목과, 요구사항에 담기지 않은 항목 찾기 (코드, LLM 없음).

요구사항 Agent의 '빠진 항목 채우기'에 쓴다: 한 원문 단위에 항목이 많으면 모델이 뒤쪽 항목을 통째로 빠뜨리는 일이 있어
(예: 하위 목록이 수십 개인 기능 요구사항 표), 추출 뒤 코드로 빠진 항목을 짚어 그 단위만 한 번 더 추출하게 한다.
문서 형식(번호 체계·표 머리 문구)에 기대지 않는다:
- 목록 항목: 목록 기호(❍ ○ 〇 ● ◎ ■ □ ◆ ◇ ▶ ▷ ◦ • ▪ ※ * · - – — → 와 'ㅇ '·'o ' 글머리, 1. 1) (1) 가. ① …)로
  시작하는 줄이 항목 하나다. 기호 없는 줄은 앞 줄이 문장 끝(…다·함·음·임·됨·것·요·기능, 마침표·괄호 닫기 등)으로 끝나지 않았을 때만
  앞 항목에 이어 붙인다 (PDF·HWP 줄바꿈으로 끊긴 문장). 기호 없이 한 줄에 한 문장씩 쓴 문서는 줄마다 항목이다.
- 틀 줄: 여러 원문 단위에 똑같이 되풀이되는 줄(표 머리 칸 이름, 산출물 안내 같은 서식)은 항목으로 보지 않고 앞 항목에도 붙이지 않는다.
  원문 단위 수의 REPEAT_SHARE 이상(최소 REPEAT_MIN개 단위)에 나오면 틀 줄이다. 단위 자기 번호가 든 줄(표 제목 줄)도 뺀다.
- 목록 머리: 바로 아래에 다른 기호의 하위 항목이 이어지는 짧은 항목('❍ 근태설정' 아래 '- …')은 하위 항목으로 보므로 뺀다.
- 담김: 항목의 4글자 조각이 그 단위 요구사항 하나(제목·설명·수용 기준·인용)에 coverage 이상 있으면 담긴 것이다.
  요구사항 여럿에 흩어진 낱말로는 담긴 것으로 보지 않는다 (거의 같은 형제 항목 하나만 추출된 경우를 잡는다).
기준값은 고객요구사항 정답지 10개 문서(evals/golden)로 맞췄다: 추출이 완벽할 때 다시 보낼 단위가 적고(약 23%),
단위마다 뒤쪽 항목이 빠졌을 때 빠진 것을 많이 짚도록(약 85%, 빠진 항목 1개만인 단위는 요구사항 검토 Agent의 보충이 맡는다).
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from app.agents.tools.search import normalize
from app.schemas.document import DocumentBlock, RequirementSource
from app.schemas.requirement import Requirement

MARK = re.compile(r"^\s*(?:[❍○〇●◎◯■□◆◇▶▷►▸◦•▪‣※*·∙ㆍ\-–—→☞✓✔√]|[ㅇo](?=\s)|\(?\d{1,2}[.)]|[가-하][.)]|[①-⑳])\s*")
SENTENCE_END = re.compile(r"(?:다|함|음|임|됨|것|요|[.。!?:;)\]]|기능)\s*$")   # 이렇게 끝난 줄 다음의 기호 없는 줄은 새 항목
MIN_ITEM = 8          # 항목으로 볼 최소 길이 (공백·기호 뺀 글자 수, 요구사항 검토 Agent의 MIN_SENTENCE와 같다)
HEADER_CHARS = 20     # 목록 머리로 볼 수 있는 항목의 최대 길이 (공백·기호 뺀 글자 수)
REPEAT_SHARE = 0.2    # 이 비율 이상의 원문 단위에 똑같이 나오는 줄은 틀 줄
REPEAT_MIN = 3


def grams(text: str, n: int) -> set[str]:
    """공백·기호를 뺀 글자 n개 단위 조각 (겹침 계산용)."""
    t = normalize(text)
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def overlap(part: set[str], whole: set[str]) -> float:
    """part 조각 중 whole에 있는 비율 (part가 비면 1)."""
    return len(part & whole) / len(part) if part else 1.0


def _marker(line: str) -> str:
    """항목 기호의 종류 (숫자·가나다는 같은 종류로)."""
    m = MARK.match(line)
    return re.sub(r"\d+|[가-하]", "#", m.group(0).strip()) if m else ""


def boilerplate(sources: Iterable[RequirementSource], blocks: dict[str, DocumentBlock]) -> set[str]:
    """여러 원문 단위에 똑같이 되풀이되는 줄 (정규화한 글)."""
    sources = list(sources)
    seen: Counter[str] = Counter()
    for src in sources:
        seen.update({normalize(line) for bid in src.block_ids if bid in blocks
                     for line in blocks[bid].text.splitlines() if line.strip()})
    least = max(REPEAT_MIN, REPEAT_SHARE * len(sources))
    return {text for text, n in seen.items() if text and n >= least}


def list_items(text: str, boiler: set[str] = frozenset()) -> list[str]:
    """블록 글의 목록 항목 (목록 머리·틀 줄 빼고, 끊긴 줄은 이어 붙여서)."""
    raw: list[list[str] | None] = []
    previous = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if normalize(line) in boiler:
            raw.append(None)   # 틀 줄: 버리고, 다음 줄을 앞 항목에 붙이지 않는다
            previous = ""
            continue
        wrapped = not MARK.match(line) and raw and raw[-1] is not None and not SENTENCE_END.search(previous)
        if wrapped:
            raw[-1][0] += " " + line.strip()   # 줄바꿈으로 끊긴 문장
        else:
            raw.append([line.strip(), _marker(line)])
        previous = line.strip()
    items = [x for x in raw if x is not None]
    # 깊이: 처음 보는 기호가 나오면 한 단계 아래, 앞에서 본 기호가 다시 나오면 그 단계로 돌아간다 (문서마다 기호가 달라도 된다)
    levels, stack = [], []
    for _, mark in items:
        if mark in stack:
            del stack[stack.index(mark) + 1:]
        else:
            stack.append(mark)
        levels.append(len(stack))
    out = []
    for i, (item, mark) in enumerate(items):
        deeper = i + 1 < len(items) and levels[i + 1] > levels[i]
        if mark and deeper and len(normalize(item)) <= HEADER_CHARS:
            continue   # 목록 머리 (바로 아래 하위 항목으로 본다)
        out.append(item)
    return out


def requirement_text(r: Requirement) -> str:
    return " ".join([r.title, r.description, *r.acceptance_criteria, *(e.quote for e in r.evidence)])


def uncovered_items(src: RequirementSource, blocks: dict[str, DocumentBlock], reqs: Iterable[Requirement],
                    boiler: set[str], coverage: float) -> list[tuple[str, str]]:
    """원문 단위의 목록 항목 중 그 단위 요구사항 어느 하나에도 담기지 않은 것 [(블록 ID, 항목)]."""
    covers = [grams(requirement_text(r), 4) for r in reqs]
    out = []
    for bid in src.block_ids:
        if bid not in blocks:
            continue
        for item in list_items(blocks[bid].text, boiler):
            if len(normalize(item)) < MIN_ITEM or (src.code and src.code in item):
                continue   # 너무 짧은 줄, 단위 자기 번호가 든 표 제목 줄
            words = grams(item, 4)
            if max((overlap(words, c) for c in covers), default=0.0) < coverage:
                out.append((bid, item))
    return out
