"""화면 구성·작업 묶음·배정·제출·검토와 VS Code 확장 API (services/layout.py · work.py, FR-049~053): 실제 생성 코드·git으로 확인한다."""
import base64
import json
import subprocess

from app import cli
from app.services import develop, project_access, repo, users, work
from conftest import signed_in_client
from test_develop import FINAL, _start

P = "sample"


def _developed(sample_doc):
    _start(sample_doc)
    assert cli.main(["develop", P, "--from", FINAL]) == 0
    runner = type("M", (), {"id": 999, "name": "한개발", "role": "developer", "email": "d@x"})()
    repo.commit_develop(P, FINAL.removeprefix("v"), "최종본 개발", runner)
    ids = {m.name: m.id for m in users.list_members()}
    return ids


def _team():
    pm, dev, dev2 = signed_in_client("pm"), signed_in_client("developer", "dev"), signed_in_client("developer", "dev2")
    ids = {m.name: m.id for m in users.list_members()}
    project_access.set_members(P, [ids["dev"], ids["dev2"]])
    return pm, dev, dev2, ids


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_bundles_cover_every_file_once_in_four_kinds(sample_doc):
    _developed(sample_doc)
    pm, dev, _, _ = _team()
    data = dev.get("/api/v1/develop/work", params={"project": P}).json()
    kinds = {i["kind"] for i in data["items"]}
    assert kinds == {"common_ui", "ui", "common_server", "server"}
    files = [f for i in data["items"] for f in i["files"]]
    assert len(files) == len(set(files)), "파일은 한 묶음에만"
    assert sorted(files) == sorted(work._files(develop.app_dir(P))), "빠진 파일 없음"
    first_fn = next(i for i in data["items"] if i["kind"] == "server")
    assert all(f.startswith("backend/") for f in first_fn["files"]) and first_fn["functions"]
    ui = next(i for i in data["items"] if i["kind"] == "ui")
    assert any("/menus/" in f for f in ui["files"])
    common_ui = next(i for i in data["items"] if i["kind"] == "common_ui")
    assert "frontend/src/App.tsx" in common_ui["files"] and "frontend/src/generated/layout.json" in common_ui["files"]
    assert data["counts"]["unassigned"] == len(data["items"]) and [d["name"] for d in data["developers"]] == ["dev", "dev2"]
    assert pm.get("/api/v1/develop/work", params={"project": P}).status_code == 200, "PM도 진행 현황은 본다"
    detail = dev.get("/api/v1/develop/work/item", params={"project": P, "key": first_fn["key"]}).json()
    assert "## 어떻게 (개발 스펙)" in detail["brief"] and "ACCEPTANCE_CRITERIA" in detail["brief"]


def test_layout_saved_to_dev_by_developer(sample_doc):
    _developed(sample_doc)
    pm, dev, _, _ = _team()
    got = dev.get("/api/v1/develop/layout", params={"project": P}).json()
    layout = got["layout"]
    layout["menu"][0]["name"] = "고친 메뉴"
    layout["menu"].reverse()
    layout["home"]["areas"] = False
    layout["menu"].append({"group": "없는메뉴", "pages": []})
    assert pm.put("/api/v1/develop/layout", json={"project": P, "layout": layout}).status_code == 403
    saved = dev.put("/api/v1/develop/layout", json={"project": P, "layout": layout}).json()
    assert saved["commit"] and saved["layout"]["home"]["areas"] is False
    assert "고친 메뉴" in [m["name"] for m in saved["layout"]["menu"]] and "없는메뉴" not in [m["group"] for m in saved["layout"]["menu"]]
    log = subprocess.run(["git", "log", "-1", "--format=%an|%s", "dev"], cwd=develop.app_dir(P), capture_output=True, text=True).stdout
    assert log.strip() == "dev|화면 구성 저장 (dev)"
    assert dev.put("/api/v1/develop/layout", json={"project": P, "layout": saved["layout"]}).json()["commit"] is None, "같으면 커밋 안 함"


def test_assign_extension_upload_submit_approve_flow(sample_doc):
    """배정 → (토큰으로) 코드 받기 → 묶음 밖 파일 거부 → 올리기 → 제출 → 본인 승인 금지 → 다른 개발자 승인 → 완료"""
    _developed(sample_doc)
    pm, dev, dev2, ids = _team()
    items = dev.get("/api/v1/develop/work", params={"project": P}).json()["items"]
    fn = next(i for i in items if i["kind"] == "server" and any("logic/" in f or "feature/" in f or "api/" in f for f in i["files"]))
    key = fn["key"]
    assert pm.post("/api/v1/develop/work/assign", json={"project": P, "key": key, "user_id": ids["dev"]}).status_code == 403
    assert dev.post("/api/v1/develop/work/assign", json={"project": P, "key": key, "user_id": ids["관리자"]}).status_code == 400, "개발자 담당자만"
    got = dev2.post("/api/v1/develop/work/assign", json={"project": P, "key": key, "user_id": ids["dev"]}).json()
    assert got["status"] == "assigned" and got["branch"] == work.branch_of(key) and got["assignee"]["name"] == "dev"
    notes = [n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "task_assigned"]
    assert notes and fn["title"] in notes[0]["title"]

    # 개인 접속 토큰 (확장)
    made = dev.post("/api/v1/auth/tokens", json={"name": "내 노트북"}).json()
    token = made["token"]
    assert token.startswith("t4_") and made["info"]["prefix"] == token[:10]
    assert token not in json.dumps(dev.get("/api/v1/auth/tokens").json()), "원문은 다시 보이지 않는다"
    from fastapi.testclient import TestClient
    from app.main import app
    ext = TestClient(app)
    ext.headers["Authorization"] = f"Bearer {token}"
    assert ext.get("/api/v1/version").json() == {"api": 1}
    mine = ext.get("/api/v1/develop/work/mine").json()
    assert [i["key"] for p in mine["projects"] for i in p["items"]] == [key]
    pack = ext.get("/api/v1/develop/work/archive", params={"project": P, "key": key}).json()
    assert pack["branch"] == work.branch_of(key) and set(pack["task"]) == {"TASK.md", "files.txt", "RULES.md"}
    paths = {f["path"] for f in pack["files"]}
    assert "README.md" in paths and not any(p.startswith(("local/", ".req-agent/", ".preview/")) for p in paths)
    target = next(p for p in fn["files"] if p.endswith((".java", ".py", ".ts")))
    original = base64.b64decode(next(f["content"] for f in pack["files"] if f["path"] == target)).decode("utf-8")

    bad = ext.post("/api/v1/develop/work/upload", json={"project": P, "key": key, "base": pack["head"],
                                                         "files": [{"path": "frontend/src/App.tsx", "content": _b64("x")}]})
    assert bad.status_code == 400 and "묶음 밖" in bad.json()["detail"]
    up = ext.post("/api/v1/develop/work/upload", json={"project": P, "key": key, "base": pack["head"], "message": "로직 채움",
                                                        "files": [{"path": target, "content": _b64(original + "\n// 채움\n")}]})
    assert up.status_code == 200 and up.json()["files"] == [target], up.text
    stale = ext.post("/api/v1/develop/work/upload", json={"project": P, "key": key, "base": pack["head"],
                                                           "files": [{"path": target, "content": _b64("y")}]})
    assert stale.status_code == 409, "받은 뒤 브랜치가 바뀌면 다시 받는다"
    state = next(i for i in dev.get("/api/v1/develop/work", params={"project": P}).json()["items"] if i["key"] == key)
    assert state["status"] == "in_progress" and state["ahead"] == 1

    assert dev2.post("/api/v1/develop/work/submit", json={"project": P, "key": key}).status_code == 403, "담당자만 제출"
    sub = ext.post("/api/v1/develop/work/submit", json={"project": P, "key": key, "note": "확인 부탁"}).json()
    assert sub["status"] == "review" and sub["request_id"]
    review = dev2.get("/api/v1/develop/work/review", params={"project": P, "key": key}).json()
    assert [f["path"] for f in review["files"]] == [target] and review["outside"] == []
    assert base64.b64decode(review["files"][0]["after"]).decode("utf-8").endswith("// 채움\n")
    assert [i["key"] for p in ext.get("/api/v1/develop/work/mine").json()["projects"] for i in p["reviews"]] == [], "자기 작업은 검토 목록에 없음"
    assert dev.post("/api/v1/develop/work/approve", json={"project": P, "key": key}).status_code == 403, "본인 승인 금지"
    done = dev2.post("/api/v1/develop/work/approve", json={"project": P, "key": key}).json()
    assert done["status"] == "done"
    assert (develop.app_dir(P) / target).read_text(encoding="utf-8").endswith("// 채움\n"), "dev에 합쳐짐"
    data = dev.get("/api/v1/develop/work", params={"project": P}).json()
    assert data["counts"]["done"] == 1 and data["people"] == [{"id": ids["dev"], "name": "dev", "total": 1, "done": 1, "review": 0}]

    assert dev.delete("/api/v1/auth/tokens", params={"id": made["info"]["id"]}).json() == {"ok": True}
    assert ext.get("/api/v1/version").status_code == 401, "지운 토큰은 못 쓴다"


def test_reject_then_resubmit_and_outside_file_blocks_submit(sample_doc):
    _developed(sample_doc)
    pm, dev, dev2, ids = _team()
    ui = next(i for i in dev.get("/api/v1/develop/work", params={"project": P}).json()["items"] if i["kind"] == "ui")
    dev2.post("/api/v1/develop/work/assign", json={"project": P, "key": ui["key"], "user_id": ids["dev"]})
    menu_file = next(f for f in ui["files"] if "/menus/" in f)
    wt = repo.worktree(P, work.branch_of(ui["key"]))
    (wt / "README.md").write_text("# 밖\n", encoding="utf-8")
    assert dev.post("/api/v1/develop/repo/commit", json={"project": P, "branch": work.branch_of(ui["key"]), "message": "밖"}).status_code == 200
    blocked = dev.post("/api/v1/develop/work/submit", json={"project": P, "key": ui["key"]})
    assert blocked.status_code == 400 and "README.md" in blocked.json()["detail"]

    (wt / "README.md").write_text(subprocess.run(["git", "show", "dev:README.md"], cwd=develop.app_dir(P), capture_output=True, text=True).stdout,
                                  encoding="utf-8")
    new_file = menu_file.rsplit(".", 1)[0] + "/Board.tsx"   # 메뉴 폴더 아래 새 파일도 그 화면 묶음
    (wt / new_file).parent.mkdir(parents=True, exist_ok=True)
    (wt / new_file).write_text("export const Board = 1\n", encoding="utf-8")
    assert dev.post("/api/v1/develop/repo/commit", json={"project": P, "branch": work.branch_of(ui["key"]), "message": "메뉴 화면"}).status_code == 200
    assert dev.post("/api/v1/develop/work/submit", json={"project": P, "key": ui["key"]}).json()["status"] == "review"
    assert dev2.post("/api/v1/develop/work/reject", json={"project": P, "key": ui["key"], "reason": ""}).status_code == 400
    rej = dev2.post("/api/v1/develop/work/reject", json={"project": P, "key": ui["key"], "reason": "이름 고쳐 주세요"}).json()
    assert rej["status"] == "rejected" and rej["reason"] == "이름 고쳐 주세요"
    again = dev.post("/api/v1/develop/work/submit", json={"project": P, "key": ui["key"]})
    assert again.status_code == 200 and again.json()["status"] == "review", again.text
    assert dev2.post("/api/v1/develop/work/assign", json={"project": P, "key": ui["key"], "user_id": ids["dev2"]}).status_code == 409, \
        "승인 대기 중에는 담당자를 바꾸지 않는다"


def test_archive_brings_in_latest_dev_merged_by_others(sample_doc):
    """받을 때 작업 브랜치에 최신 dev를 넣는다: 올린 것이 없으면 dev로 앞으로 감고, 있으면 합친다. 충돌이면 그대로 두고 알린다"""
    _developed(sample_doc)
    _, dev, dev2, ids = _team()
    items = dev.get("/api/v1/develop/work", params={"project": P}).json()["items"]
    mine = next(i for i in items if i["kind"] == "server" and any(f.endswith((".java", ".py", ".ts")) for f in i["files"]))
    other = next(i for i in items if i["kind"] == "ui")
    for item, who in ((mine, "dev"), (other, "dev2")):
        assert dev.post("/api/v1/develop/work/assign", json={"project": P, "key": item["key"], "user_id": ids[who]}).status_code == 200
    git = lambda *a: subprocess.run(["git", *a], cwd=develop.app_dir(P), capture_output=True, text=True).stdout.strip()   # noqa: E731

    # 다른 사람(dev2)이 자기 작업을 올려 dev에 합친다
    theirs = next(f for f in other["files"] if f.endswith(".tsx"))
    pack2 = dev2.get("/api/v1/develop/work/archive", params={"project": P, "key": other["key"]}).json()
    body = base64.b64decode(next(f["content"] for f in pack2["files"] if f["path"] == theirs)).decode("utf-8")
    assert dev2.post("/api/v1/develop/work/upload", json={"project": P, "key": other["key"], "base": pack2["head"], "submit": True,
                                                           "files": [{"path": theirs, "content": _b64(body + "\n// dev2\n")}]}).status_code == 200
    assert dev.post("/api/v1/develop/work/approve", json={"project": P, "key": other["key"]}).json()["status"] == "done"

    pack = dev.get("/api/v1/develop/work/archive", params={"project": P, "key": mine["key"]}).json()
    got = {f["path"]: base64.b64decode(f["content"]).decode("utf-8") for f in pack["files"]}
    assert got[theirs].endswith("// dev2\n"), "배정 뒤 다른 사람이 dev에 합친 내용이 들어 있다"
    assert pack["sync"]["merged"] and pack["head"] == git("rev-parse", "dev"), "올린 것이 없으면 dev로 앞으로 감는다 (합치기 커밋 없음)"

    # 내가 올린 뒤 dev가 또 바뀌면 합쳐서 준다 (내 변경도 그대로)
    target = next(f for f in mine["files"] if f.endswith((".java", ".py", ".ts")))
    assert dev.post("/api/v1/develop/work/upload", json={"project": P, "key": mine["key"], "base": pack["head"],
                                                          "files": [{"path": target, "content": _b64(got[target] + "\n// dev\n")}]}).status_code == 200
    side = repo.create_branch(P, "feature/side", users.get_member(ids["dev2"]))["name"]
    (repo.worktree(P, side) / "README.md").write_text("# 옆에서 고침\n", encoding="utf-8")
    repo.commit(P, side, "옆", users.get_member(ids["dev2"]))
    repo.merge(P, repo.request_merge(P, side, "", users.get_member(ids["dev2"]))["id"], users.get_member(ids["dev"]))
    pack = dev.get("/api/v1/develop/work/archive", params={"project": P, "key": mine["key"]}).json()
    got = {f["path"]: base64.b64decode(f["content"]).decode("utf-8") for f in pack["files"]}
    assert pack["sync"]["merged"] and got["README.md"] == "# 옆에서 고침\n" and got[target].endswith("// dev\n")

    # 충돌하면 합치지 않고 지금 브랜치 그대로 주며 충돌 파일을 알린다
    wt = repo.worktree(P, work.branch_of(mine["key"]))
    (wt / "README.md").write_text("# 내 쪽\n", encoding="utf-8")
    repo.commit(P, work.branch_of(mine["key"]), "내 쪽", users.get_member(ids["dev"]))
    side2 = repo.create_branch(P, "feature/side2", users.get_member(ids["dev2"]))["name"]
    (repo.worktree(P, side2) / "README.md").write_text("# 저쪽\n", encoding="utf-8")
    repo.commit(P, side2, "저쪽", users.get_member(ids["dev2"]))
    repo.merge(P, repo.request_merge(P, side2, "", users.get_member(ids["dev2"]))["id"], users.get_member(ids["dev"]))
    pack = dev.get("/api/v1/develop/work/archive", params={"project": P, "key": mine["key"]}).json()
    got = {f["path"]: base64.b64decode(f["content"]).decode("utf-8") for f in pack["files"]}
    assert not pack["sync"]["merged"] and pack["sync"]["conflicts"] == ["README.md"] and got["README.md"] == "# 내 쪽\n"


def test_regenerate_with_real_generator_writes_gen_worktree_only(sample_doc):
    """실제 생성기를 gen 작업 폴더(--out)로 다시 돌리면: 같은 스펙이면 바뀐 것 없음, 생성 파일을 지우면 gen에만 되살리고 dev는 그대로"""
    _developed(sample_doc)
    runner = type("M", (), {"id": 999, "name": "한개발", "role": "developer", "email": "d@x"})()
    gen = repo.gen_tree(P)
    assert (gen / ".req-agent" / "manifest.json").is_file()
    assert cli.main(["develop", P, "--from", FINAL, "--out", str(gen)]) == 0
    assert repo.commit_develop(P, FINAL.removeprefix("v"), "같은 스펙", runner)["commit"] is None, "같은 스펙이면 생성 결과가 같다"
    (gen / "README.md").unlink()
    repo.git(gen, "commit", "-qam", "지움", who=("t", "t@x"))
    assert cli.main(["develop", P, "--from", FINAL, "--out", str(gen)]) == 0
    res = repo.commit_develop(P, FINAL.removeprefix("v"), "다시 개발", runner)
    assert res["commit"] and res["request"] and (gen / "README.md").is_file()
    assert not (develop.app_dir(P) / ".git" / "MERGE_HEAD").exists() and repo.changes(develop.app_dir(P)) == {}, "dev 작업 폴더는 그대로"


def test_group_menus_merges_similar_menus_without_fixed_count():
    """규칙 묶기: 같은 테이블·겹치는 이름 낱말이면 한 영역, 거의 모두가 쓰는 테이블은 무시, 메뉴는 정확히 한 번씩"""
    from app.agents.tools.areas import clean_sections, group_menus

    names = [("A", "사업자 로그인"), ("B", "관리자 로그인"), ("C", "예산 편성"), ("D", "예산 집행"), ("E", "급여 계산"), ("F", "출장 신청")]
    tables = {"A": ["member"], "B": ["member"], "C": ["budget", "member"], "D": ["budget", "member"], "E": ["payroll", "member"], "F": ["trip", "member"]}
    design = {"menu": [{"group": g, "name": n, "pages": []} for g, n in names],
              "screens": [{"group": g, "table": t} for g, ts in tables.items() for t in ts]}
    got = group_menus(design)
    assert [s["menus"] for s in got] == [["A", "B"], ["C", "D"], ["E"], ["F"]], "member(모두 씀)로는 잇지 않는다"
    assert [s["name"] for s in got[:2]] == ["로그인", "예산"] and got[2]["name"] == "급여 계산"
    fixed = clean_sections([{"id": "x", "name": "", "menus": ["A", "A", "없음"]}, {"id": "x", "menus": ["B"]}], design)
    assert [(s["id"], s["menus"]) for s in fixed[:2]] == [("x", ["A"]), ("B", ["B"])], "겹친 메뉴·id는 정리"
    assert sorted(g for s in fixed for g in s["menus"]) == list("ABCDEF"), "빠진 메뉴는 규칙으로 묶어 끝에"


def test_brief_edit_is_shared_with_download_and_guards_overwrite(sample_doc):
    """웹에서 고친 작업 설명이 코드 받기(TASK.md)에 나오고, 그 사이 다른 사람이 고쳤으면 409, 비우면 자동 설명"""
    _developed(sample_doc)
    pm, dev, dev2, ids = _team()
    key = next(i["key"] for i in work.overview(P)["items"] if i["kind"] == "server")
    auto = work.brief(P, key)
    assert pm.post("/api/v1/develop/work/brief", json={"project": P, "key": key, "text": "x"}).status_code == 403
    saved = dev.post("/api/v1/develop/work/brief", json={"project": P, "key": key, "text": "# 사람이 고침", "base": auto})
    assert saved.status_code == 200 and saved.json()["edit"]
    assert dev.post("/api/v1/develop/work/assign", json={"project": P, "key": key, "user_id": ids["dev"]}).status_code == 200
    pack = dev.get("/api/v1/develop/work/archive", params={"project": P, "key": key}).json()
    assert pack["task"]["TASK.md"] == "# 사람이 고침"
    stale = dev2.post("/api/v1/develop/work/brief", json={"project": P, "key": key, "text": "늦게 고침", "base": auto})
    assert stale.status_code == 409 and work.brief(P, key) == "# 사람이 고침"
    assert dev2.post("/api/v1/develop/work/brief", json={"project": P, "key": key, "text": "이어서 고침", "base": "# 사람이 고침"}).status_code == 200
    assert dev.post("/api/v1/develop/work/brief", json={"project": P, "key": key, "text": ""}).json()["edit"] is None
    assert "# 사람이 고침" not in work.brief(P, key)


def test_todos_of_ai_show_on_area_bundle_and_task_brief(sample_doc):
    """개발 AI가 구현하지 못한 항목은 기능이 속한 영역의 서버 묶음에, 영역 없는 기능은 공통 서버에 붙는다"""
    from app.agents.roles import builder

    _developed(sample_doc)
    items = work.overview(P)["items"]
    server = next(i for i in items if i["kind"] == "server" and i["functions"])
    out = develop.gen_dir(P)
    builder._save(out, server["functions"][0], {"status": "채움", "todos": [{"rule": "AC-1", "reason": "값의 출처가 없음"}]})
    builder._save(out, "FN-NO-MENU", {"status": "채움", "todos": [{"rule": "AC-2", "reason": "메뉴 없는 기능"}]})
    builder._save(out, "FN-FAILED", {"status": "실패", "todos": [{"rule": "AC-3", "reason": "실패한 기능은 코드 틀"}]})
    items = {i["key"]: i for i in work.overview(P)["items"]}
    assert items[server["key"]]["todo_count"] == 1 and items[server["key"]]["todos"][0]["function"] == server["functions"][0]
    assert [t["reason"] for t in items["common-server"]["todos"]] == ["메뉴 없는 기능"]
    assert not any(i["todo_count"] for k, i in items.items() if k not in (server["key"], "common-server"))
    assert "값의 출처가 없음" in work.brief(P, server["key"])


def test_layout_areas_bulk_assign_lock_and_suggest(sample_doc):
    """예전 화면 구성(영역 없음)도 영역으로 읽고, 영역 단위 묶음을 한꺼번에 배정, 진행 중인 영역은 메뉴 이동 400, 영역 제안(규칙·AI·AI 실패)"""
    from app.agents.llm import client as llm
    from app.agents.llm.client import LLMClient
    from app.services import layout as layout_service

    _developed(sample_doc)
    pm, dev, dev2, ids = _team()
    app = develop.app_dir(P)
    path = app / "frontend/src/generated/layout.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    assert old["format"] == 2 and old["sections"], "새로 만든 앱은 영역이 있다"
    path.write_text(json.dumps({"format": 1, "home": {}, "menu": old["menu"]}, ensure_ascii=False), encoding="utf-8")
    got = dev.get("/api/v1/develop/layout", params={"project": P}).json()
    assert got["layout"]["format"] == 2 and [g for s in got["layout"]["sections"] for g in s["menus"]] == [m["group"] for m in old["menu"]]
    path.write_text(json.dumps(old, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    data = dev.get("/api/v1/develop/work", params={"project": P}).json()
    area = data["areas"][0]
    assert area["screen"] == f"screen-{area['id']}" and area["server"] == f"server-{area['id']}" and area["menus"]
    keys = [area["screen"], area["server"], "없는-묶음"]
    assert pm.post("/api/v1/develop/work/assign-many", json={"project": P, "keys": keys, "user_id": ids["dev"]}).status_code == 403
    res = dev2.post("/api/v1/develop/work/assign-many", json={"project": P, "keys": keys, "user_id": ids["dev"]}).json()
    assert (res["assigned"], res["failed"]) == (2, 1) and not res["results"][2]["ok"] and "없는-묶음" in res["results"][2]["error"]
    notes = [n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "task_assigned"]
    assert len(notes) == 1 and "2개" in notes[0]["title"], "알림은 한 번에"
    brief = dev.get("/api/v1/develop/work/item", params={"project": P, "key": area["screen"]}).json()["brief"]
    assert "## 화면 설계" in brief and "카드나 목록으로 늘어놓지 말고" in brief and "menus/" in brief

    lay = dev.get("/api/v1/develop/layout", params={"project": P}).json()
    assert lay["locked"] == [area["id"]]
    renamed = {**lay["layout"], "sections": [{**s, "name": "새 이름"} for s in lay["layout"]["sections"]]}
    assert dev.put("/api/v1/develop/layout", json={"project": P, "layout": renamed}).json()["layout"]["sections"][0]["name"] == "새 이름"
    moved = {**lay["layout"], "sections": [{"id": "other", "name": "옮김", "menus": area_menus}
                                           for area_menus in [[g for s in lay["layout"]["sections"] for g in s["menus"]]]]}
    bad = dev.put("/api/v1/develop/layout", json={"project": P, "layout": moved})
    assert bad.status_code == 400 and "진행 중인 작업" in bad.json()["detail"]

    rules = dev.post("/api/v1/develop/layout/suggest", json={"project": P, "mode": "rules"}).json()
    assert rules["usage"] is None and rules["layout"]["sections"]
    menus = [m["group"] for m in lay["layout"]["menu"]]

    class AreaLLM(LLMClient):
        provider = "fake"

        def __init__(self, fail=False):
            super().__init__("fake-model")
            self.fail = fail

        def _call(self, system, user, schema, max_tokens, **_):
            if self.fail:
                raise RuntimeError("모델 오류")
            return schema(sections=[{"name": "AI 영역", "menus": menus}]), 120, 30

    llm.set_llm(AreaLLM())
    ai = dev.post("/api/v1/develop/layout/suggest", json={"project": P, "mode": "ai", "layout": lay["layout"]}).json()
    assert [s["name"] for s in ai["layout"]["sections"]] == ["AI 영역"] and ai["layout"]["sections"][0]["id"] == area["id"], "첫 메뉴가 같으면 id 유지"
    assert ai["usage"]["input_tokens"] == 120
    assert layout_service.get(P)["layout"]["sections"][0]["name"] == "새 이름", "제안은 저장하지 않는다"
    llm.set_llm(AreaLLM(fail=True))
    failed = dev.post("/api/v1/develop/layout/suggest", json={"project": P, "mode": "ai"})
    assert failed.status_code == 502 and "AI 영역 제안에 실패" in failed.json()["detail"]
    assert pm.post("/api/v1/develop/layout/suggest", json={"project": P, "mode": "rules"}).status_code == 403
