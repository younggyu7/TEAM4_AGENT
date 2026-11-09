"""업무 영역: 비슷한 메뉴를 묶은 대메뉴 (화면 구성 layout.json의 sections). LLM 없음.

- 생성 앱의 왼쪽 메뉴(영역 → 메뉴)와 홈 영역 카드, 작업 묶음(화면 · <영역>, 서버 · <영역>)이 같은 영역을 쓴다.
- 규칙 묶기(group_menus): 같은 테이블을 다루거나 이름 낱말이 겹치는 메뉴를 하나씩 합친다. 개수는 정하지 않고 비슷한 것끼리 최대한 묶는다.
  거의 모든 메뉴가 쓰는 테이블·낱말(회원·공통 코드 같은 것)은 비슷함을 따질 때 뺀다 — 넣으면 모두 한 영역으로 이어진다.
- 영역 id는 만들 때 첫 메뉴의 그룹으로 정하고, 이름을 바꾸거나 메뉴를 옮겨도 그대로 둔다 (작업 묶음 키가 이 id를 쓴다).
"""
from __future__ import annotations

import re
from collections import Counter

SIMILAR = 0.2               # 이 이상 비슷하면 합친다 (테이블·낱말 자카드 중 큰 값)
STOP_WORDS = {"기능", "관리", "제공", "정보", "서비스", "시스템", "조회", "등록", "처리", "지원", "구축", "운영", "화면", "및", "등"}


def _words(name: str) -> set[str]:
    return {w for w in re.split(r"[\s·,/()\[\]\-_.]+|및", name) if len(w) >= 2 and w not in STOP_WORDS}


def _tables(design: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {m["group"]: set() for m in design.get("menu") or []}
    for s in design.get("screens") or []:
        group = str(s.get("group") or "").split(".")[0]
        if group in out and s.get("table"):
            out[group].add(s["table"])
    return out


def _common(sets: list[set[str]]) -> set[str]:
    """메뉴 셋 중 하나 넘게(4개 이상일 때) 나오는 것: 비슷함을 따질 때 뺀다"""
    if len(sets) < 4:
        return set()
    counts = Counter(x for s in sets for x in s)
    return {x for x, n in counts.items() if n > len(sets) / 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def section_name(names: list[str]) -> str:
    """영역 이름 (규칙): 메뉴가 하나면 그 이름. 여럿이면 둘 이상의 메뉴에 나오는 낱말(앞 두 개), 없으면 앞 두 메뉴의 짧은 이름 + 외 n"""
    if len(names) == 1:
        return names[0]
    counts = Counter(w for n in names for w in _words(n))
    shared = [w for w in dict.fromkeys(w for n in names for w in re.split(r"[\s·,/()\[\]\-_.]+|및", n)) if counts.get(w, 0) >= 2]
    if shared:
        return " · ".join(shared[:2])
    short = [re.split(r"\s+및\s+", n)[0] for n in names]
    head = " · ".join(short[:2])
    return head if len(short) == 2 else f"{head} 외 {len(short) - 2}"


def group_menus(design: dict) -> list[dict]:
    """규칙으로 묶은 영역 [{id, name, menus: [그룹]}] (메뉴 순서를 지킨다)"""
    menus = design.get("menu") or []
    if not menus:
        return []
    tables, words = _tables(design), {m["group"]: _words(m["name"]) for m in menus}
    common_t, common_w = _common(list(tables.values())), _common(list(words.values()))
    clusters = [{"menus": [m["group"]], "t": tables[m["group"]] - common_t, "w": words[m["group"]] - common_w} for m in menus]
    while True:
        best, pair = SIMILAR, None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                score = max(_jaccard(a["t"], b["t"]), _jaccard(a["w"], b["w"]))
                if score >= best and (pair is None or score > best):
                    best, pair = score, (i, j)
        if pair is None:
            break
        i, j = pair
        a, b = clusters[i], clusters.pop(j)
        a["menus"] += b["menus"]
        a["t"] |= b["t"]
        a["w"] |= b["w"]
    order = {m["group"]: n for n, m in enumerate(menus)}
    names = {m["group"]: m["name"] for m in menus}
    out = []
    for c in clusters:
        groups = sorted(c["menus"], key=order.__getitem__)
        out.append({"id": groups[0], "name": section_name([names[g] for g in groups]), "menus": groups})
    return sorted(out, key=lambda s: order[s["menus"][0]])


def clean_sections(sections, design: dict) -> list[dict]:
    """영역 형식 맞추기: 모르는 메뉴·겹친 메뉴는 빼고, 빠진 메뉴는 규칙으로 묶어 끝에 붙이고, 빈 영역은 지운다.
    id는 [\\w.-]만, 없거나 겹치면 첫 메뉴 그룹으로 (그것도 겹치면 -2, -3…). 이름은 60자까지, 비면 규칙 이름"""
    groups = {m["group"]: m["name"] for m in design.get("menu") or []}
    seen: set[str] = set()
    used: set[str] = set()
    out: list[dict] = []

    def unique(base: str) -> str:
        sid, n = base, 2
        while sid in used:
            sid, n = f"{base}-{n}", n + 1
        used.add(sid)
        return sid

    for s in sections if isinstance(sections, list) else []:
        if not isinstance(s, dict):
            continue
        menus = []
        for g in s.get("menus") or []:
            if isinstance(g, str) and g in groups and g not in seen:
                seen.add(g)
                menus.append(g)
        if not menus:
            continue
        raw = str(s.get("id") or "")
        sid = unique(raw if re.fullmatch(r"[\w.-]{1,80}", raw) and raw not in used else re.sub(r"[^\w.-]", "-", menus[0]))
        name = str(s.get("name") or "").strip()[:60] or section_name([groups[g] for g in menus])
        out.append({"id": sid, "name": name, "menus": menus})
    missing = [m for m in design.get("menu") or [] if m["group"] not in seen]
    if missing:
        for s in group_menus({**design, "menu": missing}):
            out.append({**s, "id": unique(re.sub(r"[^\w.-]", "-", s["id"]))})
    return out
