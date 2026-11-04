"""원문 블록 검색 (키워드·문자 bigram 유사도). 근거 재검색과 인용문 검증에 사용한다."""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from app.schemas.document import DocumentBlock

_STRIP = re.compile(r"[\s\-·○●□■◦•※*\"'“”‘’()\[\]{}.,:;/|]+")
# 바뀌면 요구사항의 뜻이 달라지는 말: 숫자는 따로 비교한다
_NUMBER = re.compile(r"\d+")
_MEANING = re.compile(r"금지|허용|불허|불가|가능|필수|선택|이상|이하|이내|초과|미만|모든|전체|일부|제외|포함|없|않|아니"
                      r"|(?:에게|에서|에)만|관리자|사용자|이용자|제안사|사업자|발주자|발주기관|수행사|수요기업|공급기업|회원"
                      r"|계약업체|용역업체|수행업체|연구원|발주처"
                      r"|암호화|복호화|평문|마스킹|해시"
                      r"|(?<=전송|처리|사용|저장|발송|등록|승인|입력|검수|계약|착수|완료|접수|변경)(?:전|후)")
_LATIN = re.compile(r"[a-z]{2,}")                  # 영문 낱말(SMS·SNS·TLS 등)이 바뀌면 대상이 바뀐 것
_NEGATION = re.compile(r"않|없|아니|불가|금지|못")  # 일치 구간 바로 뒤에 붙은 부정 ('적용' + '되지 않음')


def normalize(text: str) -> str:
    """비교용 정규화: 공백·기호를 지우고 소문자로 바꾼다."""
    return _STRIP.sub("", text or "").lower()


def quote_in_text(quote: str, text: str) -> bool:
    """인용문이 원문에 들어 있는지 확인한다 (공백·기호 차이는 무시)."""
    q = normalize(quote)
    return bool(q) and q in normalize(text)


def quote_conflict(quote: str, text: str) -> str | None:
    """인용문과 원문 후보가 같은 내용인지 본다. 숫자·조건어·대상이 다르면 그 차이를, 같으면 None을 돌려준다.

    비슷한 줄을 찾았어도 '5초'↔'10초', '허용'↔'금지', '모든 사용자'↔'관리자에게만'이면 다른 요구사항이다.
    후보 줄에서 인용문과 맞춰진 범위 밖('• (성능기준)' 같은 앞뒤 군더더기)은 비교하지 않는다.
    """
    q, t = normalize(quote), normalize(text)
    matched = [m for m in SequenceMatcher(None, q, t, autojunk=False).get_matching_blocks() if m.size >= 2]
    if not matched:
        return "일치하는 구간 없음"
    end = matched[-1].b + matched[-1].size
    span = t[matched[0].b: end]
    # 낱말 몇 개만 긴 줄 여기저기에서 맞은 경우: 문서에 없는 문장이 비슷해 보이는 것이다
    # (어미만 다른 정상 인용 '제출해야 한다'↔'제출하여야 함'은 0.71, 문서에 없는 문장은 0.4 안팎)
    covered = sum(m.size for m in matched)
    if covered < 0.6 * len(q) or len(span) > 1.5 * len(q) + 4:
        return f"일치 부족: 인용 {len(q)}자 중 {covered}자만 원문 {len(span)}자 구간에 흩어져 맞음"
    for label, pattern in (("숫자", _NUMBER), ("조건·대상", _MEANING), ("영문 낱말", _LATIN)):
        a, b = Counter(pattern.findall(q)), Counter(pattern.findall(span))
        if a != b:
            return f"{label} 다름: 인용 {sorted((a - b).elements())} ↔ 원문 {sorted((b - a).elements())}"
    tail = t[end:end + 6]
    if _NEGATION.search(tail) and not _NEGATION.search(q[-6:]):
        return f"부정 표현 다름: 원문은 일치 구간 뒤에 '{tail}'"
    return None


def _bigrams(text: str) -> set[str]:
    """문자 2-gram 집합."""
    s = normalize(text)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def similarity(query: str, text: str) -> float:
    """query의 bigram 중 text에 포함된 비율 (0~1)."""
    q = _bigrams(query)
    return len(q & _bigrams(text)) / len(q) if q else 0.0


def search_blocks(blocks: list[DocumentBlock], query: str, top_k: int = 3,
                  prefer_ids: set[str] | None = None) -> list[tuple[float, DocumentBlock]]:
    """query와 비슷한 블록을 점수순으로 top_k개 돌려준다 (prefer_ids는 가산점)."""
    scored = []
    for b in blocks:
        score = similarity(query, b.text)
        if prefer_ids and b.source_block_id in prefer_ids:
            score += 0.05
        scored.append((score, b))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


def best_line(block: DocumentBlock, query: str) -> tuple[float, str]:
    """블록 안에서 query와 가장 비슷한 줄을 인용문 후보로 돌려준다."""
    best = (0.0, "")
    for line in block.text.splitlines():
        line = line.strip()
        if len(line) < 4:
            continue
        score = similarity(query, line)
        if score > best[0]:
            best = (score, line[:200])
    return best
