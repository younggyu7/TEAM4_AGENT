"""최종 개발물 작업 묶음·배정·제출·검토 (FR-050·051·053).

묶음 (파일 경로로 정한다, classify). <영역>은 화면 구성의 업무 영역(비슷한 메뉴 묶음, layout.json sections)의 id:
  common-ui        공통 화면 틀: frontend의 앱 틀·부품·테마·화면 구성·생성기 관리 파일 (아래에 속하지 않는 frontend 파일)
  screen-<영역>    화면: 그 영역 메뉴들의 화면 파일 menus/<메뉴>.tsx(+ menus/<메뉴>/ 아래 새 파일)와 그 기능들의 AI 화면 pages/<기능>.tsx
  common-server    공통 서버·DB: 인증·공통 코드·조회 API·schema·seed·Docker·배포·문서, 메뉴가 없는 자동 실행 기능 (아래에 속하지 않는 파일)
  server-<영역>    서버: 그 영역 기능들의 API·업무 로직·인수 테스트 (파일 이름이 기능 ID에서 정해진다: FN-SFR-001-05 → FnSfr00105·fn_sfr00105)

작업 기록은 프로젝트 파일 work.json {items: {묶음 키: {assignee, branch, status, request_id, reason, history}}}.
상태: 요청 옴(assigned) → 작업중(브랜치에 dev보다 앞선 커밋) → 승인 대기(review, 합치기 요청) → 완료(done) / 반려(rejected).
제출은 담당자만, 바뀐 파일이 모두 그 묶음이어야 한다. 승인·반려는 repo.merge·reject (본인 승인 금지·1인 예외). LLM은 쓰지 않는다.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from app.agents.storage import get_store
from app.core.auth import allowed
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound
from app.services import layout, repo, users
from app.services.develop import app_dir

WORK_FILE = "work.json"
FRONT_ROOTS = ("frontend/src/", "frontend/ui/")
SKIP = {"node_modules", "dist", "target", ".preview", ".req-agent", "local", ".git", "out", ".next"}
ARCHIVE_MAX = 40_000_000          # 작업 코드 받기 응답 한도 (바이트, 원문 기준)
STATUS_LABELS = {"unassigned": "미배정", "assigned": "요청 옴", "in_progress": "작업중", "review": "승인 대기", "done": "완료", "rejected": "반려"}
_lock = threading.RLock()


# ---------------------------------------------------------------- 화면 설계 · 기능 이름

def _design(app: Path) -> dict:
    for name in ("frontend/src/generated/screens.json", "frontend/ui/generated/screens.json"):
        if (app / name).is_file():
            return json.loads((app / name).read_text(encoding="utf-8"))
    return {"menu": [], "screens": [], "autos": []}


class _Index:
    """기능 ID ↔ 파일 이름 (클래스·모듈), 기능 → 메뉴, 메뉴 파일 이름, 메뉴 → 업무 영역"""

    def __init__(self, design: dict, sections: list[dict] | None = None):
        from app.agents.tools.scaffold import class_name, menu_file
        from app.agents.tools.scaffold_fastapi import module_name

        self.design = design
        items = [*design.get("screens", []), *design.get("autos", [])]
        self.functions = {s["function_id"]: s for s in items}
        self.by_class = {class_name(fid): fid for fid in self.functions}
        self.by_module = {module_name(fid): fid for fid in self.functions}
        self.menus = {m["group"]: m for m in design.get("menu", [])}
        self.menu_of = {s["function_id"]: s["group"].split(".")[0] for s in design.get("screens", [])}
        self.menu_by_file = {menu_file(g): g for g in self.menus}
        self.menu_file = menu_file
        self.screens = {s["screen_id"]: s for s in design.get("screens", [])}
        self.sections = {s["id"]: s for s in sections or []}
        self.area_of = {g: s["id"] for s in sections or [] for g in s["menus"]}

    def function_of(self, name: str) -> str | None:
        stem = name.split(".")[0]
        for suffix in ("AcceptanceTest", "Controller", "Logic", "Page"):
            if stem.endswith(suffix) and stem[: -len(suffix)] in self.by_class:
                return self.by_class[stem[: -len(suffix)]]
        if stem in self.by_class:
            return self.by_class[stem]
        stem = stem.removeprefix("test_")
        return self.by_module.get(stem)


def _index(app: Path) -> _Index:
    design = _design(app)
    return _Index(design, layout.read(app, design)["sections"] if design.get("menu") else [])


def classify(path: str, index: _Index) -> str:
    """파일 경로 → 묶음 키"""
    front = next((r for r in FRONT_ROOTS if path.startswith(r)), None)
    if front:
        rest = path[len(front):]
        parts = rest.split("/")
        group = None
        if parts[0] == "menus" and len(parts) >= 2:
            group = index.menu_by_file.get(parts[1].split(".")[0] if len(parts) == 2 else parts[1])
        elif parts[0] == "pages" and len(parts) == 2 and (fid := index.function_of(parts[1])):
            group = index.menu_of.get(fid)
        return f"screen-{index.area_of[group]}" if group in index.area_of else "common-ui"
    if path.startswith("frontend/"):
        return "common-ui"
    if path.startswith("backend/"):
        name = path.rsplit("/", 1)[-1]
        if ((fid := index.function_of(name)) and any(seg in path.split("/") for seg in ("feature", "api", "logic", "acceptance"))
                and index.menu_of.get(fid) in index.area_of):
            return f"server-{index.area_of[index.menu_of[fid]]}"
    return "common-server"


def _files(root: Path) -> list[str]:
    """작업 폴더의 파일 (빌드 결과·패키지·생성기 기록·시연 비밀번호 제외, 경로 순)"""
    out = []

    def walk(folder: Path, prefix: str) -> None:
        for child in sorted(folder.iterdir(), key=lambda p: p.name):
            if child.name in SKIP or child.name == ".DS_Store":
                continue
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                walk(child, rel + "/")
            elif child.is_file():
                out.append(rel)

    walk(root, "")
    return out


def _tracked(app: Path, ref: str) -> list[str]:
    """그 브랜치에 커밋된 파일 (작업 폴더에 남은 커밋 안 한 파일·옛 생성물은 넣지 않는다, 경로 순)"""
    names = repo.git(app, "ls-tree", "-r", "-z", "--name-only", ref).split("\0")
    return sorted(p for p in names if p and not any(part in SKIP or part == ".DS_Store" for part in p.split("/")))


def _bundle_meta(key: str, index: _Index) -> dict:
    if key == "common-ui":
        return {"kind": "common_ui", "layer": "frontend", "title": "공통 화면 틀", "functions": [], "area": None,
                "summary": "앱 틀(메뉴·홈·로그인)·화면 부품·테마·화면 구성. 홈(대시보드) 모양처럼 모든 메뉴에 걸친 화면을 바꾼다."}
    if key == "common-server":
        return {"kind": "common_server", "layer": "backend", "title": "공통 서버·DB", "functions": [], "area": None,
                "summary": "인증·공통 코드·조회 API·DB(schema·seed)·Docker·배포 설정, 메뉴가 없는 자동 실행 기능. 여러 기능이 같이 쓰는 서버 코드."}
    kind, sid = key.split("-", 1)
    sec = index.sections.get(sid) or {"id": sid, "name": sid, "menus": []}
    menus = [{"group": g, "name": index.menus[g]["name"]} for g in sec["menus"] if g in index.menus]
    fids = [fid for fid, g in index.menu_of.items() if g in sec["menus"]]
    area = {"id": sid, "name": sec["name"], "menus": menus}
    names = ", ".join(m["name"] for m in menus[:6]) + (f" 외 {len(menus) - 6}" if len(menus) > 6 else "")
    if kind == "screen":
        return {"kind": "ui", "layer": "frontend", "title": f"화면 · {sec['name']}", "functions": fids, "area": area,
                "summary": f"업무 영역 '{sec['name']}'의 메뉴 화면 {len(menus)}개 설계·구현 ({names})"}
    return {"kind": "server", "layer": "backend", "title": f"서버 · {sec['name']}", "functions": fids, "area": area,
            "summary": f"업무 영역 '{sec['name']}'의 기능 {len(fids)}개 API·업무 로직·인수 테스트 (메뉴: {names})"}


def bundles(project: str) -> list[dict]:
    """작업 묶음 (공통 화면 틀 → 화면(영역 순) → 공통 서버·DB → 서버(영역 순)). 파일은 모두 한 묶음에 들어간다.
    파일은 dev에 커밋된 것으로 정한다 (담당자가 받는 코드와 같게. 서버 작업 폴더에 남은 파일로 정하면 목록에만 있는 파일이 생긴다)"""
    app = repo.ensure_repo(project)
    index = _index(app)
    by_key: dict[str, list[str]] = {}
    for path in _tracked(app, repo.DEV):
        by_key.setdefault(classify(path, index), []).append(path)
    keys = ["common-ui", *[f"screen-{s}" for s in index.sections], "common-server", *[f"server-{s}" for s in index.sections]]
    return [{"key": k, **_bundle_meta(k, index), "files": by_key.get(k, [])} for k in keys if by_key.get(k)]


# ---------------------------------------------------------------- 작업 기록 (work.json)

def _data(project: str) -> dict:
    text = get_store().read_project_file(project, WORK_FILE)
    data = json.loads(text) if text else {}
    return {"items": data.get("items") or {}, "briefs": data.get("briefs") or {}}   # briefs: 사람이 고친 작업 설명 (묶음 키 → {text, by, at})


def _write(project: str, data: dict) -> None:
    get_store().write_project_file(project, WORK_FILE, json.dumps(data, ensure_ascii=False))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def branch_of(key: str) -> str:
    return "task/" + re.sub(r"[^\w.-]", "-", key)


def _base(item: dict) -> str:
    """작업이 파생된 브랜치 (승인하면 여기로 합친다). 예전 작업은 dev"""
    return item.get("base") or repo.DEV


def based_on(project: str, branch: str) -> bool:
    """이 브랜치에서 파생된 진행 중 작업이 있는가"""
    return any(_base(v) == branch and v.get("status") in ("assigned", "in_progress", "review", "rejected") for v in _data(project)["items"].values())


def base_of(project: str, branch: str) -> str:
    """브랜치의 기준: 작업 브랜치면 파생 브랜치, 아니면 dev"""
    return next((_base(v) for v in _data(project)["items"].values() if v.get("branch") == branch), repo.DEV)


def bases(project: str) -> list[str]:
    """작업을 보낼 수 있는 브랜치: dev, 다시 개발한 candidate-v<n> (최신이 앞), 사람이 만든 브랜치 (main·gen·작업 브랜치 제외)"""
    names = [b["name"] for b in repo.branches(project)]
    number = lambda n: int(repo.CANDIDATE.match(n)[1])   # noqa: E731
    return [repo.DEV, *sorted(filter(repo.CANDIDATE.match, names), key=number, reverse=True),
            *[n for n in names if n not in repo.PROTECTED and not repo.CANDIDATE.match(n) and not n.startswith("task/")]]


def _sync(project: str, item: dict, branches: dict[str, dict], requests: dict[int, dict]) -> dict:
    """합치기 요청 결과로 작업 기록을 맞추고(item을 고침, 웹의 합치기·반려 화면에서 처리한 것도), 화면에 보일 상태를 돌려준다.
    요청 옴인데 브랜치에 dev보다 앞선 커밋이 있으면 작업중으로 보인다 (기록은 그대로)"""
    req = requests.get(item.get("request_id") or -1)
    if item.get("status") == "review" and req:
        if req["status"] == "merged":
            item.update(status="done", done_at=req.get("done_at"), done_by=req.get("done_by"), self_approved=req.get("self_approved", False))
        elif req["status"] == "rejected":
            item.update(status="rejected", reason=req.get("reason", ""), done_by=req.get("done_by"))
    branch = branches.get(item.get("branch") or "")
    view = dict(item)
    base = _base(item)
    behind, ahead = (0, 0) if not branch else (branch["behind"], branch["ahead"]) if base == repo.DEV or base not in branches \
        else repo._counts(repo.app_dir(project), base, item["branch"])
    if view.get("status") == "assigned" and ahead > 0:
        view["status"] = "in_progress"
    view.update(base=base, ahead=ahead, behind=behind, changes=branch["changes"] if branch else 0)
    return view


_todo_cache: dict[str, tuple] = {}


def _todos(project: str) -> dict[str, list[dict]]:
    """기능 ID → 개발 AI가 끝까지 구현하지 못했다고 남긴 항목 (생성기 기록, 파일이 바뀌었을 때만 다시 읽는다)"""
    from app.agents.roles import builder
    from app.agents.tools import scaffold
    from app.services.develop import gen_dir

    out_dir = gen_dir(project)
    stamp = tuple((p.name, p.stat().st_mtime_ns) for d in (builder.BUILD_DIR, scaffold.LOGIC_DIR) for p in sorted((out_dir / d).glob("*.json")))
    cached = _todo_cache.get(project)
    if cached and cached[0] == stamp:
        return cached[1]
    found: dict[str, list[dict]] = {}
    for fid, r in builder.load_built(out_dir).items():
        if r.get("status") == "채움":
            found.setdefault(fid, []).extend(r.get("todos") or [])
    for fid, r in scaffold.load_logic(out_dir).items():
        found.setdefault(fid, []).extend(r.get("todos") or [])
    found = {fid: rows for fid, rows in found.items() if rows}
    _todo_cache[project] = (stamp, found)
    return found


def overview(project: str) -> dict:
    """작업 묶음 + 상태 + 묶음별 빈 로직·AI 수 + 진행 현황 (상태별·담당자별)"""
    from app.services import appfiles, project_access

    items = bundles(project)
    tree = {f["path"]: f for f in appfiles.tree(project)["files"]}
    with _lock:
        data = _data(project)
        before = json.dumps(data, sort_keys=True)
        branches = {b["name"]: b for b in repo.branches(project)}
        requests = {r["id"]: r for r in repo.requests(project)}
        views = {k: _sync(project, v, branches, requests) for k, v in data["items"].items()}
        if json.dumps(data, sort_keys=True) != before:
            _write(project, data)
    todos = _todos(project)
    served = {fid for b in items if b["kind"] == "server" for fid in b["functions"]}
    out = []
    for b in items:
        stats = [tree[p] for p in b["files"] if p in tree]
        state = views.get(b["key"]) or {"status": "unassigned"}
        fids = b["functions"] if b["kind"] == "server" else [fid for fid in todos if fid not in served] if b["kind"] == "common_server" else []
        rows = [{"function": fid, **t} for fid in fids for t in todos.get(fid, [])]
        out.append({**b, **{k: v for k, v in state.items() if k != "history"}, "history": state.get("history", [])[-10:],
                    "status_label": STATUS_LABELS[state["status"]], "file_count": len(b["files"]), "todos": rows, "todo_count": len(rows),
                    "empty_holes": sum(f["empty_holes"] for f in stats), "ai_holes": sum(f["ai_holes"] for f in stats),
                    "ai_files": sum(f["ai_file"] for f in stats), "edited": sum(f["edited"] for f in stats)})
    counts = {s: sum(1 for b in out if b["status"] == s) for s in STATUS_LABELS}
    people: dict[int, dict] = {}
    for b in out:
        if a := b.get("assignee"):
            p = people.setdefault(a["id"], {**a, "total": 0, "done": 0, "review": 0})
            p["total"] += 1
            p["done"] += b["status"] == "done"
            p["review"] += b["status"] == "review"
    devs = [{"id": m.id, "name": m.name} for m in project_access.assignees(project) if allowed(m.role, "repo_write")]
    areas: dict[str, dict] = {}
    for b in out:   # 영역마다 한 줄: 화면 묶음·서버 묶음 키 (웹 표·일괄 배정)
        if b["area"]:
            a = areas.setdefault(b["area"]["id"], {**b["area"], "screen": None, "server": None})
            a["screen" if b["kind"] == "ui" else "server"] = b["key"]
    return {"items": out, "counts": counts, "people": list(people.values()), "developers": devs, "areas": list(areas.values()),
            "labels": STATUS_LABELS, "version": repo.final_version(project), "bases": bases(project)}


def _item(project: str, key: str) -> dict:
    found = next((b for b in overview(project)["items"] if b["key"] == key), None)
    if found is None:
        raise NotFound(f"작업 묶음이 없습니다: {key}")
    return found


def _history(entry: dict, member, what: str) -> None:
    entry.setdefault("history", []).append({"at": _now(), "by": {"id": member.id, "name": member.name}, "what": what})


def assign(project: str, key: str, user_id: int, member, notify: bool = True, base: str | None = None) -> dict:
    """담당자 배정: 작업 브랜치 task/<묶음>을 base(기본 dev)에서 만들고(있으면 담당자만 바꿈) 담당자에게 알린다. 승인 대기 중이면 409.
    받을 때 base의 서버 최신 내용이 들어가고, 승인하면 base로 합친다"""
    from app.services import directives, project_access

    _item(project, key)
    target = next((m for m in project_access.assignees(project) if m.id == user_id), None)
    if target is None or not allowed(target.role, "repo_write"):
        raise BadRequest("이 프로젝트의 개발자 담당자에게만 배정합니다.")
    branch = branch_of(key)
    base = base or repo.DEV
    if base not in bases(project):
        raise BadRequest(f"이 브랜치로는 보낼 수 없습니다: {base}")
    with _lock:
        data = _data(project)
        entry = data["items"].get(key) or {}
        if entry.get("status") == "review":
            raise Conflict("승인 대기 중인 작업은 담당자를 바꾸지 않습니다 (승인·반려 뒤에 바꾸세요).")
        app = repo.ensure_repo(project)
        exists = repo._exists(app, branch)
        if exists and entry.get("status") == "done":   # 끝난 작업을 다시 맡기면 dev에서 새로 시작
            repo.delete_branch(project, branch, member)
            exists = False
        if exists and _base(entry) != base:
            raise Conflict(f"이 작업은 {_base(entry)}에서 파생되어 진행 중입니다. 끝난 뒤 {base}로 보내세요.")
        if not exists:
            repo.create_branch(project, branch, target, base)
        else:
            rd = repo._data(project)
            rd["branches"].setdefault(branch, {})["owner"] = {"id": target.id, "name": target.name}
            repo._write(project, rd)
        entry.update(assignee={"id": target.id, "name": target.name}, branch=branch, base=base, status="assigned",
                     request_id=None, reason="", assigned_at=_now())
        _history(entry, member, f"{target.name}님에게 배정 ({base}에서 파생)")
        data["items"][key] = entry
        _write(project, data)
    meta = _item(project, key)
    if notify:
        directives.add_event(project, "task_assigned", f"{member.name}님이 작업을 맡겼습니다: {meta['title']}",
                             f"{base}에서 파생 · 브랜치 {branch} · 파일 {meta['file_count']}개", "develop", repo.final_version(project), None,
                             member_ids={target.id}, by=member.id)
    return meta


def assign_many(project: str, keys: list[str], user_id: int, member, base: str | None = None) -> dict:
    """여러 묶음을 한 사람에게 (일괄 배정). 묶음마다 결과를 돌려주고 하나가 실패해도 나머지는 배정한다. 알림은 한 번에 하나"""
    from app.core.exceptions import AppError
    from app.services import directives

    if not keys:
        raise BadRequest("배정할 작업 묶음을 고르세요.")
    results, done = [], []
    for key in dict.fromkeys(keys):
        try:
            item = assign(project, key, user_id, member, notify=False, base=base)
            done.append(item)
            results.append({"key": key, "ok": True, "title": item["title"]})
        except AppError as e:
            results.append({"key": key, "ok": False, "error": e.detail})
    if done:
        target = done[0]["assignee"]
        titles = ", ".join(i["title"] for i in done[:5]) + (f" 외 {len(done) - 5}개" if len(done) > 5 else "")
        directives.add_event(project, "task_assigned", f"{member.name}님이 작업 {len(done)}개를 맡겼습니다", titles,
                             "develop", repo.final_version(project), None, member_ids={target["id"]}, by=member.id)
    return {"results": results, "assigned": len(done), "failed": len(results) - len(done)}


def outside(project: str, key: str, paths: list[str]) -> list[str]:
    """그 묶음 밖의 파일 (제출·올리기 검사)"""
    index = _index(app_dir(project))
    return [p for p in paths if classify(p, index) != key]


def _changed(project: str, branch: str, base: str = repo.DEV) -> list[str]:
    app = repo.ensure_repo(project)
    out = repo.git(app, "diff", "--name-only", "-z", f"{base}...{branch}")
    return [p for p in out.split("\0") if p]


def submit(project: str, key: str, note: str, member) -> dict:
    """제출 (담당자만): 묶음 밖 파일을 바꿨으면 400, 아니면 합치기 요청을 만들고 승인 대기로"""
    item = _item(project, key)
    if (item.get("assignee") or {}).get("id") != member.id:
        raise Forbidden("이 작업의 담당자만 제출합니다.")
    if item["status"] not in ("assigned", "in_progress", "rejected"):
        raise BadRequest(f"지금 상태({item['status_label']})에서는 제출하지 않습니다.")
    bad = outside(project, key, _changed(project, item["branch"], _base(item)))
    if bad:
        raise BadRequest(f"이 작업 묶음 밖의 파일을 바꿨습니다 ({len(bad)}개): {', '.join(bad[:10])}. "
                         "되돌리거나 그 파일의 묶음 담당자에게 요청하세요.")
    req = repo.request_merge(project, item["branch"], note or f"작업 제출: {item['title']}", member, target=_base(item))
    with _lock:
        data = _data(project)
        entry = data["items"][key]
        entry.update(status="review", request_id=req["id"], reason="", submitted_at=_now())
        _history(entry, member, "제출 (합치기 요청)")
        _write(project, data)
    return _item(project, key)


def approve(project: str, key: str, member) -> dict:
    item = _item(project, key)
    if item["status"] != "review" or not item.get("request_id"):
        raise BadRequest("승인 대기 중인 작업이 아닙니다.")
    req = repo.merge(project, item["request_id"], member)
    with _lock:
        data = _data(project)
        entry = data["items"][key]
        entry.update(status="done", done_at=req.get("done_at"), done_by=req.get("done_by"), self_approved=req.get("self_approved", False))
        _history(entry, member, f"승인 ({_base(item)}에 합침)" + (" · 본인 승인" if req.get("self_approved") else ""))
        _write(project, data)
    return _item(project, key)


def reject(project: str, key: str, reason: str, member) -> dict:
    item = _item(project, key)
    if item["status"] != "review" or not item.get("request_id"):
        raise BadRequest("승인 대기 중인 작업이 아닙니다.")
    req = repo.reject(project, item["request_id"], reason, member)
    with _lock:
        data = _data(project)
        entry = data["items"][key]
        entry.update(status="rejected", reason=req["reason"], done_by=req.get("done_by"))
        _history(entry, member, f"반려: {req['reason']}")
        _write(project, data)
    return _item(project, key)


# ---------------------------------------------------------------- 무엇을 어떻게 (작업 설명) · 확장용

RULES = """# 작업 규칙 (모든 작업 공통)

1. 고칠 수 있는 파일은 `files.txt`에 있는 파일과, 같은 묶음에 속하는 새 파일뿐이다. 다른 파일은 읽기만 한다.
   - 화면 묶음은 그 영역 메뉴들의 `menus/<메뉴>.tsx`와 `menus/<메뉴>/` 아래 새 파일·AI 화면 `pages/<기능>.tsx`,
     서버 묶음은 그 영역 기능들의 Controller·Logic·인수 테스트.
   - 묶음 밖 파일을 바꾸면 제출이 거부된다. 공통 코드가 필요하면 공통 묶음 담당자에게 요청한다.
2. 이름은 생성기가 정한 것을 쓴다: 기능 `FN-SFR-001-05` → 클래스 `FnSfr00105…`, 모듈 `fn_sfr00105`. 새 이름을 만들 때도 이 접두사를 붙인다.
3. 계약을 바꾸지 않는다: API 경로·요청·응답은 `spec/openapi.yaml`·`spec/API.md`, 테이블·컬럼은 `db/schema.sql`을 따른다.
   계약이 틀렸으면 코드를 바꾸지 말고 담당자(PM)에게 알려 스펙(기능 명세·ERD)을 고친 뒤 다시 개발한다.
4. 생성기가 관리하는 파일(`generated/*`, `pages/index.ts`, `menus/index.ts`)은 공통 화면 틀 묶음에서만 고친다.
5. 업무 로직 자리 `// <hole 이름> … // </hole>`는 표시를 지우지 말고 그 안만 채운다 (사람이 채운 자리는 초록으로 보인다).
6. 제출 전에 실행(Docker)으로 확인하고, 바뀐 곳을 비교 화면에서 확인한다.
"""


def brief_edit(project: str, key: str) -> dict | None:
    """사람이 작업 설명을 고쳤으면 {by, at}"""
    saved = _data(project)["briefs"].get(key)
    return {"by": saved["by"], "at": saved["at"]} if saved else None


def save_brief(project: str, key: str, text: str, base: str | None, member) -> dict:
    """작업 설명을 고친다 (웹·확장이 같은 내용을 본다). 비우면 자동 설명으로 되돌린다.
    base: 고치기 시작한 내용. 그 사이 다른 사람이 고쳤으면 409 (자동 설명은 상태에 따라 글자가 바뀌므로 사람이 고친 것만 비교한다)"""
    _item(project, key)
    with _lock:
        data = _data(project)
        saved = data["briefs"].get(key)
        if saved and base is not None and saved["text"].strip() != base.strip():
            raise Conflict(f"{saved['by']['name']}님이 먼저 고쳤습니다. 다시 읽거나 다시 받은 뒤 고치세요.")
        if text.strip():
            data["briefs"][key] = {"text": text, "by": {"id": member.id, "name": member.name}, "at": _now()}
        else:
            data["briefs"].pop(key, None)
        _write(project, data)
    return {"brief": brief(project, key), "edit": brief_edit(project, key)}


def brief(project: str, key: str) -> str:
    """TASK.md: 무엇을 어떻게 (사람이 고쳤으면 그 내용. 아니면 서버 기능은 개발 스펙에서 그 기능 절을 잘라 모은 것 — 기능 구현 AI 요청과 같은 내용)"""
    item = _item(project, key)
    if saved := _data(project)["briefs"].get(key):
        return saved["text"]
    lines = [f"# 작업: {item['title']}", "", f"- 묶음 키: `{key}` · 브랜치: `{item.get('branch') or branch_of(key)}` · 파생 브랜치: `{_base(item)}` (승인하면 여기로 합침)",
             f"- 담당: {(item.get('assignee') or {}).get('name', '미배정')} · 상태: {item['status_label']}",
             f"- 고칠 파일: {item['file_count']}개 (`files.txt`) · 빈 업무 로직 {item['empty_holes']}곳", "", "## 무엇을", "", item["summary"], ""]
    if item.get("reason"):
        lines += ["## 반려 사유 (고쳐서 다시 제출)", "", item["reason"], ""]
    if item["todos"]:
        lines += ["## 채울 것 (개발 AI가 끝까지 구현하지 못한 항목)", "", *[f"- {t['function']} {t['rule']}: {t['reason']}" for t in item["todos"]], ""]
    version = repo.final_version(project)
    try:
        from app.agents.roles import builder
        from app.agents.tools import scaffold

        spec = scaffold.load_spec(project, version) if version else None
    except Exception:  # noqa: BLE001 - 스펙을 못 읽어도 작업 설명은 준다
        spec = None
    if spec is not None and item["functions"]:
        owned, _ = scaffold.owned_operations(spec)
        by_id = {f.function_id: f for f in spec.functions}
        lines += ["## 어떻게 (개발 스펙)", ""]
        if item["kind"] == "ui":
            lines += _screen_design(project, item)
        for fid in item["functions"]:
            f = by_id.get(fid)
            if f is None:
                continue
            if item["kind"] == "server":
                lines += [builder.build_request(spec, f, owned.get(fid) or []), ""]
            else:
                ac = builder._section(builder._doc(spec, "ACCEPTANCE_CRITERIA.md"), 3, fid + " ")
                lines += [f"### {fid} {f.name}", "", f.description or "", "", ac or "", ""]
    elif item["kind"] == "ui":
        lines += _screen_design(project, item)
    elif item["kind"] == "common_ui":
        lines += ["## 어떻게", "", "- 앱 틀은 `App.tsx`(왼쪽 메뉴·홈), 메뉴 화면 공통 부분은 `menu.tsx`, 부품은 `kit.tsx`·`Screen.tsx`, 색은 `theme.css`.",
                  "- 홈(대시보드)은 `App.tsx`의 Home이 그린다. 업무 영역 카드·처리할 일·공지를 업무에 맞게 설계한다.",
                  "- 업무 영역 묶음·메뉴 이름·순서·숨김은 코드 대신 웹 '화면 구성'으로 바꾼다 (`generated/layout.json`).", ""]
    elif item["kind"] == "common_server":
        lines += ["## 어떻게", "", "- 공통 코드·인증·조회 API·DB 파일. 기능별 서버 코드는 각 기능 묶음이 맡는다.",
                  "- DB를 바꾸면 `db/schema.sql`·`db/seed.sql`을 함께 고치고 스펙(ERD)과 맞는지 담당자에게 확인한다.", ""]
    return "\n".join(lines)


def _screen_design(project: str, item: dict) -> list[str]:
    """화면 묶음의 '화면 설계': 메뉴마다 화면 파일·업무·화면·API (screens.json에서)"""
    index = _index(app_dir(project))
    lines = ["## 화면 설계 (이 영역의 메뉴 화면)", "",
             "- 메뉴마다 화면 파일 `frontend/src/menus/<메뉴>.tsx`를 고쳐 그 메뉴의 화면을 만든다. 처음에는 공통 메뉴 화면(`DefaultMenuPage`)을 그대로 쓴다.",
             "- 업무를 카드나 목록으로 늘어놓지 말고 업무 흐름에 맞게 한 화면으로 설계한다: 탭, 목록 + 상세, 검색 조건, 상태별 보기, 처리 버튼 배치 등.",
             "- 데이터·버튼은 props(`screens`·`screen`·`built`)와 `kit.tsx`·`Screen.tsx` 부품, `api.ts`를 쓴다. API 경로·필드는 바꾸지 않는다.",
             "- 메뉴가 많아 한 화면에 다 들어가지 않으면 메뉴 폴더(`menus/<메뉴>/`) 아래에 부품 파일을 나눠 만든다.", ""]
    for m in (item.get("area") or {}).get("menus", []):
        menu = index.menus.get(m["group"]) or {"pages": []}
        lines += [f"### 메뉴 {m['name']} (`menus/{index.menu_file(m['group'])}.tsx`)", ""]
        for page in menu["pages"]:
            lines.append(f"- 업무 **{page['name']}**")
            for sid in page["screens"]:
                sc = index.screens.get(sid)
                if not sc:
                    continue
                ops = ", ".join(f"{o['method']} {o['path']}" for o in sc.get("operations") or [])
                lines.append(f"  - {sc['name']} ({sc.get('pattern_label') or sc.get('pattern') or ''}, 테이블 {sc.get('table_label') or sc.get('table') or '-'}) — {ops}")
        lines.append("")
    return lines


def _catch_up(project: str, item: dict, member) -> dict:
    """받기 전에 작업 브랜치에 파생 브랜치의 최신 내용을 넣는다 (다른 사람이 그 사이 합친 것). 승인 대기·완료면 그대로 둔다.
    아직 올린 것이 없으면 dev로 앞으로 감고, 있으면 합친다. 충돌이면 합치지 않고 충돌 파일을 알린다"""
    if item["status"] in ("review", "done"):
        return {"merged": False, "message": "", "conflicts": []}
    try:
        res = repo.update_branch(project, item["branch"], member, _base(item))
    except repo.MergeConflict as e:
        return {"merged": False, "message": e.detail, "conflicts": e.extra["conflicts"]}
    return {"merged": res["merged"], "message": res["message"] if res["merged"] else "", "conflicts": []}


def archive(project: str, key: str, member) -> dict:
    """작업 코드 받기 (확장): 파생 브랜치의 서버 최신 내용을 넣은 작업 브랜치의 파일 전체(base64) + .task/TASK.md·files.txt·RULES.md. 담당자·개발자만"""
    item = _item(project, key)
    if not item.get("branch"):
        raise BadRequest("아직 배정하지 않은 작업입니다.")
    if (item.get("assignee") or {}).get("id") != member.id and not allowed(member.role, "develop"):
        raise Forbidden("이 작업의 담당자나 개발자만 받습니다.")
    sync = _catch_up(project, item, member)
    root = repo.worktree(project, item["branch"])
    files, size = [], 0
    for rel in _files(root):
        raw = (root / rel).read_bytes()
        size += len(raw)
        if size > ARCHIVE_MAX:
            raise BadRequest("코드가 너무 커서 한 번에 받지 못합니다.")
        files.append({"path": rel, "content": base64.b64encode(raw).decode("ascii")})
    head = repo.git(root, "rev-parse", "HEAD").strip()
    index = _index(app_dir(project))
    editable = [f["path"] for f in files if classify(f["path"], index) == key]   # 받는 코드에 실제로 있는 이 묶음의 파일
    task = {"TASK.md": brief(project, key), "files.txt": "\n".join(editable) + "\n", "RULES.md": RULES}
    return {"project": project, "key": key, "branch": item["branch"], "base": _base(item), "head": head, "files": files, "task": task,
            "editable": editable, "sync": sync}


def upload(project: str, key: str, base: str, message: str, changes: list[dict], member, submit_after: bool = False, note: str = "") -> dict:
    """바뀐 파일 올리기 (확장): 담당자만. 받은 커밋(base)이 지금 브랜치 끝과 다르면 409, 묶음 밖 파일이면 400. 작업 브랜치에 커밋"""
    item = _item(project, key)
    if (item.get("assignee") or {}).get("id") != member.id:
        raise Forbidden("이 작업의 담당자만 올립니다.")
    if item["status"] == "review":
        raise BadRequest("승인 대기 중에는 올리지 않습니다 (반려되면 고쳐서 다시 올리세요).")
    if not changes:
        raise BadRequest("올릴 변경이 없습니다.")
    paths = [str(c.get("path") or "") for c in changes]
    if bad := outside(project, key, paths):
        raise BadRequest(f"이 작업 묶음 밖의 파일입니다 ({len(bad)}개): {', '.join(bad[:10])}")
    root = repo.worktree(project, item["branch"])
    with repo._lock:
        head = repo.git(root, "rev-parse", "HEAD").strip()
        if base and base != head:
            raise Conflict("받은 뒤에 작업 브랜치가 바뀌었습니다. 코드를 다시 받아 바뀐 곳을 합친 뒤 올리세요.")
        for c in changes:
            rel = c["path"]
            target = (root / rel).resolve()
            if root.resolve() not in target.parents or any(part in SKIP for part in Path(rel).parts):
                raise BadRequest(f"올릴 수 없는 경로입니다: {rel}")
            if c.get("content") is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(c["content"]))
    result = repo.commit(project, item["branch"], message or f"{item['title']} 작업", member, paths)
    out = {"commit": result["commit"], "files": result["files"]}
    if submit_after:
        out["item"] = submit(project, key, note, member)
    return out


def _merged_files(project: str, item: dict) -> list[str]:
    """완료 작업이 dev에 합쳐질 때 실제로 바뀐 파일 (합치기 커밋 기준, 기록이 없으면 빈 목록)"""
    req = next((r for r in repo.requests(project) if r["id"] == item.get("request_id")), None)
    if not req or not req.get("commit"):
        return []
    code, _, out = repo.git(repo.ensure_repo(project), "diff", "--name-only", "-z", f"{req['commit']}^1", req["commit"], check=False).partition("\n")
    return [p for p in out.split("\0") if p] if code == "0" else []


def mine(member) -> dict:
    """확장 사이드바: 내가 맡은 작업과 내가 검토할 작업 (볼 수 있는 프로젝트 전체)"""
    from app.services import project_access

    out = []
    for project in project_access.visible(member, get_store().list_projects()):
        if not repo.ready(project):
            continue
        try:
            data = overview(project)
        except Exception:  # noqa: BLE001 - 코드가 없거나 깨진 프로젝트는 건너뛴다
            continue
        mine_items = [{**i, "changed_files": _merged_files(project, i)} if i["status"] == "done" else i
                      for i in data["items"] if (i.get("assignee") or {}).get("id") == member.id]
        alone = len(data["developers"]) <= 1
        reviews = [i for i in data["items"] if i["status"] == "review" and allowed(member.role, "develop")
                   and ((i.get("assignee") or {}).get("id") != member.id or alone)]
        if mine_items or reviews:
            out.append({"project": project, "version": data["version"], "items": mine_items, "reviews": reviews})
    return {"projects": out, "labels": STATUS_LABELS}


def review_files(project: str, key: str) -> dict:
    """검토 비교: dev와 작업 브랜치에서 바뀐 파일 목록과 양쪽 내용(base64, 없으면 null)"""
    item = _item(project, key)
    if not item.get("branch"):
        raise BadRequest("아직 배정하지 않은 작업입니다.")
    app = repo.ensure_repo(project)
    base = repo.git(app, "merge-base", _base(item), item["branch"]).strip()
    out = []
    for rel in _changed(project, item["branch"], _base(item)):
        def show(ref: str) -> str | None:
            res = repo.git(app, "show", f"{ref}:{rel}", check=False)
            code, _, text = res.partition("\n")
            return base64.b64encode(text.encode("utf-8")).decode("ascii") if code == "0" else None
        out.append({"path": rel, "before": show(base), "after": show(item["branch"])})
    return {"key": key, "branch": item["branch"], "base": _base(item), "files": out, "outside": outside(project, key, [f["path"] for f in out])}
