"""생성 앱의 화면 구성 (frontend/src/generated/layout.json): 메뉴 이름·순서·숨김, 메뉴 안 업무 묶음, 홈 칸.

- 앱은 이 파일을 읽어 메뉴·홈을 그린다 (templates/app/frontend/src/menu.tsx applyLayout). 기본값은 생성기가 만든다 (scaffold.default_layout).
- 웹 '화면 구성'에서 저장하면 코드 폴더(dev 작업 폴더)의 이 파일만 바꿔 저장한 회원 이름으로 dev에 커밋한다.
  사람이 고친 파일이라 다시 개발해도 덮어쓰지 않는다 (scaffold.write_app manifest). 새로 생긴 화면은 앱이 원래 메뉴 끝에 붙인다.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.exceptions import BadRequest
from app.services import preview, repo
from app.services.develop import app_dir

FILES = ("frontend/src/generated/layout.json", "frontend/ui/generated/layout.json")   # React(Vite) 틀 · Next.js 틀
SCREENS = ("frontend/src/generated/screens.json", "frontend/ui/generated/screens.json")


def _pick(app: Path, names: tuple[str, ...]) -> str | None:
    return next((n for n in names if (app / n).is_file()), None)


def _design(app: Path) -> dict:
    name = _pick(app, SCREENS)
    if name is None:
        raise BadRequest("화면 설계(screens.json)가 없습니다. '최종 개발하기'를 먼저 하세요.")
    return json.loads((app / name).read_text(encoding="utf-8"))


def get(project: str) -> dict:
    """{layout, menu(스펙이 정한 기본 메뉴), screens(화면 ID → 이름), path}"""
    app = repo.ensure_repo(project)
    design = _design(app)
    name = _pick(app, FILES)
    if name:
        layout = json.loads((app / name).read_text(encoding="utf-8"))
    else:   # 이 기능 전에 개발한 앱: 기본값을 보여 주고 저장하면 파일을 만든다
        from app.agents.tools.scaffold import default_layout
        layout = default_layout(design)
    return {"layout": layout, "menu": design.get("menu") or [], "path": name or FILES[0],
            "screens": {s["screen_id"]: s["name"] for s in design.get("screens") or []}}


def _clean(layout: dict, design: dict) -> dict:
    """모르는 메뉴·화면은 빼고 형식을 맞춘다 (빈 이름은 스펙 이름으로)"""
    groups = {m["group"]: m for m in design.get("menu") or []}
    known = {s["screen_id"] for s in design.get("screens") or []}
    home = layout.get("home") or {}
    menu, seen = [], set()
    for item in layout.get("menu") or []:
        group = str(item.get("group") or "")
        if group not in groups or group in seen:
            continue
        seen.add(group)
        pages = []
        for p in item.get("pages") or []:
            screens = [s for s in p.get("screens") or [] if s in known]
            if screens:
                pages.append({"name": str(p.get("name") or "").strip()[:60], "screens": screens, "hidden": bool(p.get("hidden"))})
        menu.append({"group": group, "name": str(item.get("name") or "").strip()[:60] or groups[group]["name"],
                     "hidden": bool(item.get("hidden")), "pages": pages})
    return {"format": 1, "home": {k: bool(home.get(k, True)) for k in ("todo", "notices", "areas")}, "menu": menu}


def save(project: str, layout: dict, member) -> dict:
    """화면 구성을 저장하고 dev에 커밋한다 (바뀐 것이 없으면 커밋하지 않음)"""
    if not isinstance(layout, dict):
        raise BadRequest("화면 구성 형식이 올바르지 않습니다.")
    app = repo.ensure_repo(project)
    design = _design(app)
    name = _pick(app, FILES) or (FILES[1] if _pick(app, SCREENS) == SCREENS[1] else FILES[0])
    text = json.dumps(_clean(layout, design), ensure_ascii=False, indent=1) + "\n"
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
