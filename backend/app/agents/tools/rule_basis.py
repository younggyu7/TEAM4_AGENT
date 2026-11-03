"""업무 규칙 근거 대조 (LLM 없음): 표시 없는 규칙이 원문에 근거가 있는지 코드로 본다.

기능 명세 프롬프트가 '(가정)'을 붙이라고 해도 모델이 빠뜨린다. 표시가 없으면 스펙은 확정으로 읽고 그대로 구현되므로 코드로 다시 본다.
- 근거 글: 규칙이 연결된 요구사항의 제목·설명·원문 인용·수용 기준·결정과 공통 결정(공통 기능·용어와 근거가 원문·결정인 공통 규칙).
- 수치: 규칙의 수치·기간·횟수가 근거 글에 없으면 근거 없음이다 (가장 믿을 수 있는 신호).
- 낱말: 규칙의 내용 낱말이 근거 글에 얼마나 있는지 본다. 일반 낱말(등록·조회·관리 등)은 세지 않고,
  합성어는 글자 쌍으로 비교한다 ('승인해제'와 '승인·해제'가 같게).
같은 입력이면 항상 같은 결과다. 판정은 '(가정)' 표시만 바꾸고, 뒤의 고객 확인 단계가 한도(FUNC_ASSUMPTION_LIMIT)에 맞춰 질문·확인 필요로 정한다.

규칙 정리: '(가정)'·'(확인 필요)' 규칙 중 고객이 정할 내용이 없는 문장을 뺀다 (질문 없는 확인 규칙이 쌓이지 않게).
- 되풀이: 기능 자체를 말하는 동사(등록·조회·관리 등)로 끝나고, 조건·제약 없이 기능의 이름·설명·행위자·입출력·데이터만 다시 말하는 문장 ('자원을 등록, 수정할 수 있다').
- 중복: 같은 기능의 더 긴 규칙에 내용이 모두 들어 있는 문장.
표시 없는 규칙(원문 근거)과 질문·결정·공통 규칙은 지우지 않는다. 지운 내용은 기능의 다른 칸이나 더 긴 규칙에 남아 있다.
"""
from __future__ import annotations

import re

from app.schemas.function import FunctionSpec
from app.schemas.requirement import Requirement

SUPPORT_LIMIT = 0.5     # 내용 낱말 중 근거 글에 있는 비율이 이보다 낮으면 근거 없음
MIN_WORDS = 3           # 내용 낱말이 이보다 적으면 낱말로 판단하지 않는다 (짧은 규칙은 오판이 많다)
WORD_MATCH = 0.6        # 합성어: 글자 쌍이 이만큼 근거 글에 있으면 있는 낱말로 본다
NUMBER = re.compile(r"\d[\d,.]*\s*(?:분|시간|일|주|개월|달|월|년|회|번|건|개|%|퍼센트|원|명|자|글자|[KMGT]B|초|세|단계|차|배)")
LIMIT_NUMBER = re.compile(r"(\d[\d,.]*)\s*(?:이상|이하|초과|미만)")   # 단위 없는 기준값 ('20 이상 23 이하')
TAG = re.compile(r"^\([^)]*\)\s*")
MID_MARK = re.compile(r"\s*[(（]\s*\(?(가정|확인 필요)\)?[^()（）]*[)）]\)?\s*")
TOKEN = re.compile(r"[가-힣A-Za-z0-9]+")
JOSA = re.compile(r"(으로써|으로서|에서의|에서는|으로는|으로도|에게서|부터는|까지는|에서|에게|으로|로서|로써|부터|까지|마다|별로|와의|과의|보다|처럼|같이|이나|이며|이고|은|는|이|가|을|를|에|의|로|와|과|도|만|별|및)$")
ENDING = re.compile(r"(되어야|되어야만|하여야|해야|되어|되며|되고|되는|되면|하며|하고|하는|하면|하여|해서|한다|된다|있다|없다|합니다|됩니다|시킨다|받는다|준다|낸다|한|된|할|될|함|됨)$")
# 어떤 기능에나 나오는 낱말: 있어도 근거가 되지 않는다
GENERIC = frozenset("""
기능 시스템 사용자 화면 데이터 정보 내용 항목 내역 대상 경우 기준 처리 관리 등록 조회 수정 삭제 입력 저장 표시 확인 가능 필요 제공 지원 이용 사용 생성 변경 선택
목록 상세 결과 상태 값 수 때 등 후 전 시 각 각각 해당 모든 전체 일부 이상 이하 초과 미만 여부 별도 함께 따라 위해 대해 통해 의해 대한 위한 기록 요청 수행 적용 포함 관련 필수 입력값
""".split())
# 되풀이 판단에서 내용으로 치지 않는 동작 낱말
ACTION_WORDS = frozenset("중지 추가 부여 회수 담당 담당자 보관 작성 운영 수기 직접".split())
# 기능 자체를 말하는 끝맺음: 이렇게 끝나지 않으면 되풀이로 보지 않는다 ('…으로 반영한다', '…을 기록한다'는 동작이다)
PLAIN_ENDING = re.compile(r"(등록|조회|수정|삭제|관리|수행|설정|지정|부여|회수|중지|입력|지원)(한다|합니다|된다|됩니다|할 수 있다|할 수 있습니다)\.?$")
# 있으면 고객이 정할 내용이 있는 문장이다: 조건·제한·수치·연계
CONSTRAINT = re.compile(r"\S만(\s|$|을|으로)|본인|없|않|불가|금지|제외|한정|제한|경우|때|\s시\s|이상|이하|초과|미만|까지|이후|이전|필수|자동|반드시|일치|중복|따라|외부|연계|통해|API|\d")
NEGATION = re.compile(r"없|않|불가|금지")
# 영향이 큰 규칙: 틀리면 범위·권한·돈·상태가 달라진다. 어떤 업무 문서에나 맞게 문장 구조와 일반 낱말만 쓴다 (업무 고유 낱말을 넣지 않는다).
# 대상을 좁히는 문장('관리자만', 'IP에서만')과 범위를 빼는 문장('제공하지 않는다', '후속 범위')은 그것만으로 사람이 본다
IMPACT = [(2, re.compile(r"\S만(\s|$|으로)|제공하지|지원하지|후속|제외|불가|금지|차단|허용하지")), (1, re.compile(r"수 없|하지 않|한정|제한")), (1, re.compile(r"권한|본인|역할")),
          (1, re.compile(r"금액|잔액|비용|요금|결제|청구|정산|수수료|가격|단가")), (1, re.compile(r"승인|반려|결재|확정|상태|소급")),
          (1, re.compile(r"연계|외부|자동|전달")), (1, re.compile(r"보관|파기|개인정보|법령|법적"))]


def _stem(word: str) -> str:
    """조사·어미를 뗀다. 두 글자 낱말의 끝 글자는 떼지 않는다 ('권한'·'기한'의 '한', '회의'의 '의'는 어미가 아니다)."""
    for _ in range(2):
        for ending in (JOSA, ENDING):
            cut = ending.sub("", word)
            word = cut if len(cut) != 1 else word   # 낱말 전체가 어미면('있다') 비운다
    return word


def content_words(text: str, *, keep_generic: bool = False) -> list[str]:
    """내용 낱말: 조사·어미를 떼고 일반 낱말과 한 글자 낱말을 뺀다."""
    out = []
    for token in TOKEN.findall(text):
        stem = _stem(token) if re.search("[가-힣]", token) else token
        if len(stem) >= 2 and (keep_generic or stem not in GENERIC) and not stem.isdigit() and stem not in out:
            out.append(stem)
    return out


def _bigrams(word: str) -> set[str]:
    return {word[i:i + 2] for i in range(len(word) - 1)}


def _sourced(items: list[dict] | None) -> list[dict]:
    """근거가 '가정'·'확인 필요'인 공통 결정은 뺀다: 모델이 정한 규칙이 같은 내용의 규칙을 근거 있게 만들면 안 된다."""
    return [x for x in items or [] if x.get("basis") not in ("가정", "확인 필요")]


def common_text(common: dict | None) -> str:
    """공통 결정의 글: 공통 기능을 쓴다는 규칙이나 정해 둔 용어를 '원문에 없다'고 보지 않게 근거에 넣는다."""
    common = common or {}
    parts = [x.get("name", "") for x in common.get("terms") or []]
    parts += [s for x in common.get("terms") or [] for s in x.get("synonyms") or []]
    parts += [t for x in common.get("shared_functions") or [] for t in (x.get("name", ""), x.get("scope", ""))]
    parts += [x.get("rule", "") for x in _sourced(common.get("policies"))]
    parts += [t for x in _sourced(common.get("code_sets")) for t in (x.get("name", ""), *(x.get("values") or []))]
    parts += [t for x in common.get("roles") or [] for t in (x.get("name", ""), x.get("scope", ""), *(x.get("aliases") or []))]
    return " ".join(parts)


def source_text(f: FunctionSpec, by_req: dict[str, Requirement], common: dict | None = None) -> str:
    """기능의 근거 글: 연결된 요구사항의 제목·설명·원문 인용·수용 기준·결정 + 공통 결정."""
    parts: list[str] = [common_text(common)]
    for rid in f.requirement_ids:
        r = by_req.get(rid)
        if r:
            parts += [r.title, r.description, *r.acceptance_criteria, *r.decisions, *[e.quote for e in r.evidence]]
    return " ".join(parts)


def unsupported(rule: str, source: str) -> str:
    """근거가 없으면 사유를, 있으면 빈 문자열을 돌려준다."""
    text = TAG.sub("", rule)
    flat = re.sub(r"\s+", "", source)
    numbers = [n for n in NUMBER.findall(text) if re.sub(r"\s+", "", n) not in flat]
    numbers += [n for n in LIMIT_NUMBER.findall(text) if not re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", flat)]
    if numbers:
        return f"원문에 없는 수치 {', '.join(dict.fromkeys(numbers))}"
    words = content_words(text)
    if len(words) < MIN_WORDS:
        return ""
    pairs = _bigrams(flat)
    found = sum(1 for w in words if len(_bigrams(w) & pairs) / max(len(_bigrams(w)), 1) >= WORD_MATCH)
    share = found / len(words)
    return f"원문에 없는 내용 (낱말 {found}/{len(words)})" if share < SUPPORT_LIMIT else ""


def impact_of(rule: str) -> int:
    """규칙이 틀렸을 때의 영향 (높을수록 크다). '(가정)' 자리를 영향이 작은 규칙에 주려고 쓴다."""
    return sum(weight for weight, pattern in IMPACT if pattern.search(rule))


def _own_text(f: FunctionSpec) -> str:
    return " ".join([f.name, f.description, *f.actors, *[x.name for x in (*f.inputs, *f.outputs)], *f.data_objects])


def restated(rule: str, f: FunctionSpec) -> bool:
    """조건·제약 없이 기능의 다른 칸을 다시 말하는 문장인가."""
    text = TAG.sub("", rule)
    if CONSTRAINT.search(text) or not PLAIN_ENDING.search(text):
        return False
    pairs = _bigrams(re.sub(r"\s+", "", _own_text(f)))
    words = [w for w in content_words(text) if w not in ACTION_WORDS]
    return bool(words) and all(len(_bigrams(w) & pairs) / max(len(_bigrams(w)), 1) >= WORD_MATCH for w in words)


def _covered_by(short: str, long: str) -> bool:
    """짧은 규칙의 낱말이 긴 규칙에 모두 있는가. 일반 낱말도 센다 ('수정할 수 없다'의 수정이 빠지면 다른 규칙이다). 수치나 부정이 다르면 다른 규칙이다."""
    short, long = TAG.sub("", short), TAG.sub("", long)
    words = content_words(short, keep_generic=True)
    if len(words) < 2 or bool(NEGATION.search(short)) != bool(NEGATION.search(long)):
        return False
    flat = re.sub(r"\s+", "", long)
    if any(re.sub(r"\s+", "", n) not in flat for n in NUMBER.findall(short)):
        return False
    pairs = _bigrams(flat)
    return all(len(_bigrams(w) & pairs) / max(len(_bigrams(w)), 1) >= WORD_MATCH for w in words)


def _grade(rule: str) -> int:
    """지울 수 있는 규칙의 등급 (0 표시 없음, 1 가정, 2 확인 필요). 질문·결정·공통 규칙은 -1: 지우지 않는다."""
    if not TAG.match(rule):
        return 0
    return {"(가정)": 1, "(확인 필요)": 2}.get(TAG.match(rule).group(0).strip(), -1)


def trim_rules(functions: list[FunctionSpec], only: set[str] | None = None) -> tuple[list[FunctionSpec], list[tuple[str, str, str]]]:
    """고객이 정할 내용이 없는 '(가정)'·'(확인 필요)' 규칙을 뺀다. only는 손댈 기능 ID (없으면 전체).

    돌려주는 값: (기능 목록, [(기능 ID, 뺀 규칙, 사유)])."""
    out, removed = [], []
    for f in functions:
        if only is not None and f.function_id not in only:
            out.append(f)
            continue
        kept: list[str] = []
        for rule in f.business_rules:
            grade = _grade(rule)
            if grade > 0 and restated(rule, f):
                removed.append((f.function_id, rule, "기능의 다른 칸을 다시 말함"))
            elif grade >= 0 and any(len(TAG.sub("", o)) > len(TAG.sub("", rule)) and _covered_by(rule, o) and 0 <= _grade(o) <= grade and grade > 0
                                    for o in f.business_rules if o != rule):
                removed.append((f.function_id, rule, "더 긴 규칙과 같은 내용"))
            else:
                kept.append(rule)
        out.append(f.model_copy(update={"business_rules": kept}) if kept != f.business_rules else f)
    return out, removed


def lift_marks(rule: str) -> str:
    """문장 중간에 쓴 표시를 맨 앞으로 옮긴다 ('… ((가정) 범위는 미정)' → '(가정) …'). 앞 표시만 비율·수용 기준이 읽는다."""
    if TAG.match(rule):
        return rule
    found = MID_MARK.search(rule)
    if not found:
        return rule
    return f"({found.group(1)}) {MID_MARK.sub(' ', rule, count=1).strip()}"


def mark_unsupported(functions: list[FunctionSpec], by_req: dict[str, Requirement], common: dict | None = None,
                     only: set[str] | None = None, note: str = "") -> tuple[list[FunctionSpec], list[tuple[str, str, str]]]:
    """표시 없는 규칙 중 근거가 없는 것에 '(가정)'을 붙인다. 이미 표시가 있는 규칙(가정·확인 필요·결정·공통)은 건드리지 않는다.

    only는 손댈 기능 ID (없으면 전체). note는 이번에 사용자가 준 의견·답이다: 요구사항 결정에 남지 않아도 근거다.
    돌려주는 값: (기능 목록, [(기능 ID, 규칙, 사유)])."""
    out, flagged = [], []
    for f in functions:
        if only is not None and f.function_id not in only:
            out.append(f)
            continue
        source = source_text(f, by_req, common)
        source = f"{source} {note}" if source and note else source
        rules = []
        for rule in f.business_rules:
            lifted = lift_marks(rule)
            reason = "" if TAG.match(lifted) or not source else unsupported(lifted, source)
            if reason:
                flagged.append((f.function_id, lifted, reason))
                lifted = f"(가정) {lifted}"
            rules.append(lifted)
        out.append(f.model_copy(update={"business_rules": rules}) if rules != f.business_rules else f)
    return out, flagged
