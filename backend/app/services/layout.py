"""생성 앱의 화면 구성 (frontend/src/generated/layout.json): 업무 영역(비슷한 메뉴 묶음)·메뉴 이름·숨김, 메뉴 안 업무 묶음, 홈 칸.

- 앱은 이 파일을 읽어 왼쪽 메뉴(영역 → 메뉴)·홈을 그린다 (templates/app/frontend/src/menu.tsx applyLayout). 기본값은 생성기가 만든다 (scaffold.default_layout).
- 업무 영역(sections)은 작업 묶음(화면 · <영역>, 서버 · <영역>)도 쓴다. 메뉴 순서는 영역 순서를 따른다.
  예전 파일(format 1, 영역 없음)은 읽을 때 규칙으로 영역을 채워 보여 주고, 저장하면 format 2로 쓴다.
- 웹 '화면 구성'에서 저장하면 코드 폴더(dev 작업 폴더)의 이 파일만 바꿔 저장한 회원 이름으로 dev에 커밋한다.
  사람이 고친 파일이라 다시 개발해도 덮어쓰지 않는다 (scaffold.write_app manifest). 새로 생긴 메뉴는 규칙으로 묶어 끝에 붙인다.
- 진행 중인 작업 묶음(배정됨~승인 대기·반려)의 영역은 메뉴를 넣거나 빼지 못한다 (작업 범위가 바뀌므로). 이름은 바꿀 수 있다.
- 영역 제안(suggest): 규칙으로 다시 묶거나 AI(LLM 한 번, 메뉴·업무 이름만 보냄)에게 묶어 달라고 한다. 저장하지 않고 보여 주기만 한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.tools.areas import clean_sections, group_menus
from app.core.exceptions import AppError, BadRequest
from app.services import preview, repo

FILES = ("frontend/src/generated/layout.json", "frontend/ui/generated/layout.json")   # React(Vite) 틀 · Next.js 틀
SCREENS = ("frontend/src/generated/screens.json", "frontend/ui/generated/screens.json")
ACTIVE = ("assigned", "in_progress", "review", "rejected")


def _pick(app: Path, names: tuple[str, ...]) -> str | None:
    return next((n for n in names if (app / n).is_file()), None)


def design_of(app: Path) -> dict:
    name = _pick(app, SCREENS)
    if name is None:
        raise BadRequest("화면 설계(screens.json)가 없습니다. '최종 개발하기'를 먼저 하세요.")
    return json.loads((app / name).read_text(encoding="utf-8"))


def read(app: Path, design: dict) -> dict:
    """코드 폴더의 화면 구성을 format 2로 맞춰 읽는다 (파일이 없으면 기본값)"""
    name = _pick(app, FILES)
    if name:
        return _clean(json.loads((app / name).read_text(encoding="utf-8")), design)
    from app.agents.tools.scaffold import default_layout   # 이 기능 전에 개발한 앱: 기본값을 보여 주고 저장하면 파일을 만든다
    return _clean(default_layout(design), design)


def get(project: str) -> dict:
    """{layout, menu(스펙이 정한 기본 메뉴), screens(화면 ID → 이름), path, locked(진행 중인 작업이 있어 메뉴를 못 옮기는 영역 id)}"""
    app = repo.ensure_repo(project)
    design = design_of(app)
    return {"layout": read(app, design), "menu": design.get("menu") or [], "path": _pick(app, FILES) or FILES[0],
            "screens": {s["screen_id"]: s["name"] for s in design.get("screens") or []}, "locked": sorted(_locked(project))}


def _clean(layout: dict, design: dict) -> dict:
    """모르는 메뉴·화면은 빼고 형식을 맞춘다 (빈 이름은 스펙 이름으로). 메뉴 순서는 영역 순서"""
    groups = {m["group"]: m for m in design.get("menu") or []}
    known = {s["screen_id"] for s in design.get("screens") or []}
    home = layout.get("home") or {}
    items: dict[str, dict] = {}
    for item in layout.get("menu") or []:
        group = str(item.get("group") or "")
        if group not in groups or group in items:
            continue
        pages = []
        for p in item.get("pages") or []:
            screens = [s for s in p.get("screens") or [] if s in known]
            if screens:
                pages.append({"name": str(p.get("name") or "").strip()[:60], "screens": screens, "hidden": bool(p.get("hidden"))})
        items[group] = {"group": group, "name": str(item.get("name") or "").strip()[:60] or groups[group]["name"],
                        "hidden": bool(item.get("hidden")), "pages": pages}
    raw = layout.get("sections")
    if not raw:   # 예전 파일: 지금 메뉴 순서대로 규칙 묶기
        listed = [groups[g] for g in items] + [m for g, m in groups.items() if g not in items]
        raw = group_menus({**design, "menu": listed})
    sections = clean_sections(raw, design)
    order = [g for s in sections for g in s["menus"]]
    menu = [items.get(g) or {"group": g, "name": groups[g]["name"], "hidden": False,
                             "pages": [{"name": p["name"], "screens": p["screens"], "hidden": False} for p in groups[g]["pages"]]}
            for g in order]
    return {"format": 2, "home": {k: bool(home.get(k, True)) for k in ("todo", "notices", "areas")}, "sections": sections, "menu": menu}


def _locked(project: str) -> set[str]:
    """진행 중인 작업 묶음이 있는 영역 id (화면 · / 서버 · 묶음 둘 다 본다)"""
    from app.services import work

    items = work._data(project)["items"]
    return {key.split("-", 1)[1] for key, v in items.items()
            if key.startswith(("screen-", "server-")) and v.get("status") in ACTIVE}


def _check_locked(project: str, before: dict, after: dict) -> None:
    locked = _locked(project)
    old = {s["id"]: set(s["menus"]) for s in before["sections"]}
    new = {s["id"]: set(s["menus"]) for s in after["sections"]}
    for sid in sorted(locked):
        if sid in old and old[sid] != new.get(sid, set()):
            name = next(s["name"] for s in before["sections"] if s["id"] == sid)
            raise BadRequest(f"영역 '{name}'은(는) 진행 중인 작업이 있어 메뉴를 넣거나 뺄 수 없습니다. 작업이 끝난 뒤(완료) 옮기세요.")


def save(project: str, layout: dict, member) -> dict:
    """화면 구성을 저장하고 dev에 커밋한다 (바뀐 것이 없으면 커밋하지 않음)"""
    if not isinstance(layout, dict):
        raise BadRequest("화면 구성 형식이 올바르지 않습니다.")
    app = repo.ensure_repo(project)
    design = design_of(app)
    name = _pick(app, FILES) or (FILES[1] if _pick(app, SCREENS) == SCREENS[1] else FILES[0])
    cleaned = _clean(layout, design)
    _check_locked(project, read(app, design), cleaned)
    text = json.dumps(cleaned, ensure_ascii=False, indent=1) + "\n"
    with repo._lock:
        target = app / name
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            return {**get(project), "commit": None}
        target.write_text(text, encoding="utf-8", newline="\n")
        repo.git(app, "add", "--", name)
        repo.git(app, "commit", "-q", "-m", f"화면 구성 저장 ({member.name})", "--", name, who=(member.name, member.email))
        sha = repo.git(app, "rev-parse", "HEAD").strip()
    preview.mark_stale(project)
    return {**get(project), "commit": sha}


# ---------------------------------------------------------------- 영역 제안 (저장하지 않음)

class SectionOut(BaseModel):
    name: str = Field(description="영역 이름 (짧은 한국어, 예: 회원·인증)")
    menus: list[str] = Field(description="이 영역에 넣을 메뉴 그룹 ID")


class SectionsOut(BaseModel):
    sections: list[SectionOut]


SUGGEST_PROMPT = """너는 업무 시스템의 메뉴를 정리하는 화면 설계자다.
메뉴 목록을 보고 업무가 비슷한 메뉴끼리 최대한 묶어 '업무 영역'(대메뉴)을 만든다.
- 영역 개수는 정해져 있지 않다. 사용자가 찾기 쉽도록 비슷한 것은 최대한 같은 영역에 넣되, 성격이 전혀 다른 메뉴는 섞지 않는다.
- 모든 메뉴는 정확히 한 영역에 들어간다. 메뉴 ID는 주어진 그대로 쓴다.
- 영역 이름은 그 안의 업무를 대표하는 짧은 한국어(2~8자 정도)로 짓는다.
- 영역 순서는 사용자가 자주 쓰는 업무가 앞에 오도록, 메뉴 순서는 원래 순서를 따른다."""


class SuggestFailed(AppError):
    status_code = 502


def suggest(project: str, mode: str, current: dict | None = None) -> dict:
    """영역 다시 묶기 제안: mode 'rules'(규칙, LLM 없음) · 'ai'(LLM 한 번). 지금 화면 구성(current, 없으면 저장된 것)에 영역만 바꿔 돌려준다.
    AI가 실패하면 502로 그대로 알린다 (규칙 결과로 바꿔치지 않는다)"""
    app = repo.ensure_repo(project)
    design = design_of(app)
    base = _clean(current, design) if isinstance(current, dict) else read(app, design)
    usage = None
    if mode == "rules":
        sections = group_menus({**design, "menu": [next(m for m in design["menu"] if m["group"] == x["group"]) for x in base["menu"]]})
    elif mode == "ai":
        from app.agents.llm.client import get_llm, model_label

        names = {m["group"]: m["name"] for m in base["menu"]}
        lines = [f"- {m['group']}: {m['name']} (업무: {', '.join(p['name'] for p in m['pages'][:12])})" for m in base["menu"]]
        llm = get_llm()
        llm.pop_usage()
        try:
            out = llm.structured(system=SUGGEST_PROMPT, user="메뉴 목록:\n" + "\n".join(lines), schema=SectionsOut,
                                 max_tokens=8000, purpose="업무 영역 묶기")
        except Exception as e:  # noqa: BLE001 - 실패는 숨기지 않고 알린다
            raise SuggestFailed(f"AI 영역 제안에 실패했습니다: {e}") from e
        usage = {**llm.pop_usage(), "model": model_label()}
        sections = [{"id": "", "name": s.name, "menus": [g for g in s.menus if g in names]} for s in out.sections]
        old = {g: s["id"] for s in base["sections"] for g in s["menus"]}
        for s in sections:   # 영역 id는 예전 영역과 첫 메뉴가 같으면 이어 쓴다 (작업 묶음 키 유지)
            s["id"] = old.get(s["menus"][0], "") if s["menus"] else ""
    else:
        raise BadRequest("mode는 rules 또는 ai입니다.")
    proposal = _clean({**base, "sections": sections}, design)
    return {"layout": proposal, "usage": usage, "mode": mode}
