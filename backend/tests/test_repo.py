"""최종 개발물 코드 관리 (services/repo.py · gitpush.py): 실제 git으로 임시 폴더에서 확인한다."""
import json
import subprocess

from app.core.config import get_settings
from app.services import develop, directives, gitpush, jobs, repo, users
from conftest import signed_in_client

P = "sample"
LOGIC = "class L {\n    Object a() {\n        // <hole a>\n        return crud.get();\n        // </hole>\n    }\n}\n"


def _git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd or develop.app_dir(P), capture_output=True, text=True, check=True).stdout


AI_RUNNER = type("M", (), {"id": 999, "name": "한개발", "role": "developer", "email": "d@x"})()   # 개발하기를 누른 회원 (회원 ID와 겹치지 않게)


def _developed(version: str = "1.1.1") -> str:
    """처음 최종 개발하기를 흉내 낸다: 코드 폴더에 파일을 쓰고 개발 커밋 (gen·dev가 같은 커밋)"""
    app = develop.app_dir(P)
    for path, text in {"README.md": "# app\n", "backend/src/L.java": LOGIC, "frontend/src/App.tsx": "x\n",
                       ".gitignore": "node_modules/\nlocal/\n.preview/\n.req-agent/\n", "local/accounts.txt": "secret",
                       ".req-agent/manifest.json": json.dumps({"files": {}}), "frontend/node_modules/m.js": "skip"}.items():
        (app / path).parent.mkdir(parents=True, exist_ok=True)
        (app / path).write_text(text, encoding="utf-8")
    message = repo.develop_message(version, {"mode": "build", "model": "claude-haiku-5-5", "effort": "medium"}, "한개발")
    return repo.commit_develop(P, version, message, AI_RUNNER)["commit"]


def _regen(files: dict[str, str], version: str = "1.1.1", message: str = "다시 개발") -> dict:
    """다시 개발하기를 흉내 낸다: 생성기가 gen 작업 폴더에 쓰고 gen에 커밋 (+ gen → dev 합치기 요청)"""
    out = develop.gen_dir(P)
    assert out != develop.app_dir(P), "저장소가 있으면 gen 작업 폴더에 쓴다"
    for path, text in files.items():
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        (out / path).write_text(text, encoding="utf-8")
    return repo.commit_develop(P, version, message, AI_RUNNER)


def _member(role: str) -> users.Member:
    signed_in_client(role)
    return next(m for m in users.list_members() if m.email == ("admin@test.local" if role == "pm" else f"{role}@test.local"))


def test_first_develop_puts_gen_and_dev_on_same_commit_without_main():
    """처음 개발: gen에 커밋하고 dev도 같은 커밋 (자동 병합). main은 첫 릴리스 때 생긴다. 생성기 기록은 gen 작업 폴더로"""
    sha = _developed()
    assert sha and _git("log", "-1", "--format=%an|%s", "gen").strip() == "req-agent|최종본 v1.1.1 개발 · 기능 전체 AI · claude-haiku-5-5 · medium (개발: 한개발)"
    assert _git("rev-parse", "gen").strip() == _git("rev-parse", "dev").strip() == _git("rev-parse", "final-v1.1.1").strip() == sha
    assert _git("branch", "--show-current").strip() == "dev" and "main" not in _git("branch", "--format=%(refname:short)").split()
    files = _git("ls-tree", "-r", "--name-only", "gen").split()
    assert sorted(files) == [".gitignore", "README.md", "backend/src/L.java", "frontend/src/App.tsx"], "생성기 기록·시연 계정·패키지는 커밋 안 함"
    gen = develop.gen_dir(P)
    assert gen == develop.app_dir(P) / ".preview" / "worktrees" / "gen" and (gen / ".req-agent" / "manifest.json").is_file()
    assert _git("branch", "--show-current", cwd=gen).strip() == "gen"
    assert repo.requests(P) == [], "처음은 합치기 요청 없이 dev에 들어간다"
    assert repo.final_version(P) == "1.1.1"


def test_regenerate_commits_on_gen_and_requests_merge_to_dev(sample_doc):
    """다시 개발: gen에만 커밋하고 gen → dev 합치기 요청을 낸다. 열린 요청이 있으면 그 요청을 최신으로. 승인해야 dev에 들어간다"""
    from app.services import project_access
    from test_develop_screen import _approved_final
    _approved_final(sample_doc)
    first = _developed()
    assert _regen({})["commit"] is None and repo.requests(P) == [], "바뀐 것이 없으면 커밋·요청 없음"
    res = _regen({"README.md": "# app v2\n"})
    assert res["commit"] and not res["first"] and res["request"]
    assert _git("rev-parse", "dev").strip() == first and (develop.app_dir(P) / "README.md").read_text(encoding="utf-8") == "# app\n", "dev는 그대로"
    assert _git("rev-parse", "final-v1.1.1").strip() == res["commit"], "같은 최종본을 다시 개발하면 태그를 옮긴다"
    req = repo.requests(P)[0]
    assert (req["branch"], req["status"], req["generated"], req["by"]["name"]) == ("gen", "open", True, "한개발")
    dev, dev2 = signed_in_client("developer", "dev"), signed_in_client("developer", "dev2")
    asked = [n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "merge_request"]
    assert asked and asked[0]["title"].startswith("AI 재생성 결과를 dev에 합치기 요청")
    diff = dev.get("/api/v1/develop/repo/diff", params={"project": P, "branch": "gen"}).json()
    assert [(f["path"], f["status"]) for f in diff["files"]] == [("README.md", "M")]

    again = _regen({"frontend/src/App.tsx": "y\n"}, message="또 개발")
    assert again["request"] == res["request"] and len(repo.requests(P)) == 1, "열린 gen 요청은 하나 (최신 gen으로 바뀜)"
    assert repo.requests(P)[0]["note"] == "또 개발" and repo.requests(P)[0]["ahead"] == 2

    ids = {m.name: m.id for m in users.list_members()}
    project_access.set_members(P, [ids["dev"], ids["dev2"]])
    merged = dev2.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": res["request"]})
    assert merged.status_code == 200 and merged.json()["status"] == "merged", merged.text
    app = develop.app_dir(P)
    assert (app / "README.md").read_text(encoding="utf-8") == "# app v2\n" and (app / "frontend/src/App.tsx").read_text(encoding="utf-8") == "y\n"
    assert _git("log", "-1", "--format=%s", "dev").strip().startswith("합치기: gen (요청 한개발, 승인 dev2)")


def test_regenerate_conflict_with_human_change_is_409_and_gen_is_protected(sample_doc):
    """사람이 dev에서 고친 파일을 AI도 바꾸면 합칠 때 충돌로 드러난다 (dev는 그대로). gen은 사람이 고치거나 요청하지 않는다"""
    from test_develop_screen import _approved_final
    _approved_final(sample_doc)
    _developed()
    dev, dev2 = signed_in_client("developer", "dev"), signed_in_client("developer", "dev2")
    human = _branch_with_commit(dev, "feature/readme", "README.md", "# 사람이 고침\n")
    req = dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": human}).json()
    assert dev2.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": req["id"]}).status_code == 200
    res = _regen({"README.md": "# AI가 다시 씀\n"})
    before = _git("rev-parse", "dev").strip()
    clash = dev2.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": res["request"]})
    assert clash.status_code == 409 and clash.json()["conflicts"] == ["README.md"] and "AI가 바꾼 파일" in clash.json()["detail"]
    assert _git("rev-parse", "dev").strip() == before and (develop.app_dir(P) / "README.md").read_text(encoding="utf-8") == "# 사람이 고침\n"

    edit = {"project": P, "path": "README.md", "content": "x", "branch": "gen"}
    assert dev.put("/api/v1/develop/file", json=edit).status_code == 400, "gen은 직접 고치지 않는다"
    assert dev.post("/api/v1/develop/repo/commit", json={"project": P, "branch": "gen", "message": "x"}).status_code == 400
    assert dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": "gen"}).status_code == 400, "gen 요청은 개발하기만 낸다"
    assert dev.post("/api/v1/develop/repo/update-branch", json={"project": P, "branch": "gen"}).status_code == 400
    assert dev.post("/api/v1/develop/repo/branch", json={"project": P, "name": "gen"}).status_code == 400
    assert dev.delete("/api/v1/develop/repo/branch", params={"project": P, "name": "gen"}).status_code == 404
    listed = {b["name"]: b for b in dev.get("/api/v1/develop/repo", params={"project": P}).json()["branches"]}
    assert listed["gen"]["protected"] and listed["gen"]["ahead"] == 1


def test_runner_records_develop_commit(monkeypatch):
    """_DevelopRunner: 처음엔 코드 폴더에 쓰고 자동 확인 전에 gen·dev에 커밋한다.
    다시 개발하면 gen 작업 폴더에 쓰고 gen 커밋 + 합치기 요청, 자동 확인(dev를 띄움)은 건너뛴다"""
    recorded = {}
    order = []
    monkeypatch.setattr(develop, "find_version", lambda p, v: {})
    monkeypatch.setattr(develop, "update_version", lambda p, v, **k: recorded.update(k))

    def run(text: str) -> develop._DevelopRunner:
        out = repo.gen_tree(P) if (develop.app_dir(P) / "README.md").is_file() else develop.app_dir(P)   # start_develop과 같게
        job = jobs.Job(id="d", project=P, base="1.1.1", source="develop", status="running", message="")
        runner = develop._DevelopRunner(job, P, "1.1.1", False, {}, AI_RUNNER, out)

        def exec_(args):
            out.mkdir(parents=True, exist_ok=True)
            (out / "README.md").write_text(text, encoding="utf-8")
            return 0
        monkeypatch.setattr(runner, "_exec", exec_)
        monkeypatch.setattr(runner, "build_mock", lambda: False)
        monkeypatch.setattr(runner, "summary", lambda: {})
        monkeypatch.setattr(runner, "check", lambda: order.append(_git("rev-list", "--count", "dev").strip()) or None)
        runner._run([])
        assert job.status == "done", job.log
        return runner

    assert run("# app\n").first
    assert recorded["develop"]["commit"] == _git("rev-parse", "dev").strip() == _git("rev-parse", "gen").strip() and order == ["1"], "자동 확인 전에 커밋"
    assert _git("log", "-1", "--format=%s").strip() == "최종본 v1.1.1 개발 · AI 안 씀 (개발: 한개발)"
    assert recorded["develop"]["request"] is None

    runner = run("# app v2\n")
    assert not runner.first and order == ["1"], "다시 개발하면 자동 확인을 건너뛴다"
    assert recorded["develop"]["commit"] == _git("rev-parse", "gen").strip() != _git("rev-parse", "dev").strip()
    assert recorded["develop"]["request"] == repo.requests(P)[0]["id"] and "합치기 요청" in runner.job.message


def test_branch_worktree_save_commit_and_permissions():
    _developed()
    dev, pm, other = signed_in_client("developer", "dev"), signed_in_client("pm"), signed_in_client("developer", "dev2")
    body = {"project": P, "name": "feature/dev 로그인!", "from": "dev"}
    assert pm.post("/api/v1/develop/repo/branch", json=body).status_code == 403, "PM은 브랜치를 만들지 않는다 (개발자만)"
    made = dev.post("/api/v1/develop/repo/branch", json=body)
    assert made.status_code == 200 and made.json()["name"] == "feature/dev-로그인", made.text
    branch = made.json()["name"]
    wt = develop.app_dir(P) / ".preview" / "worktrees" / "feature__dev-로그인"
    assert (wt / "backend/src/L.java").is_file() and _git("branch", "--show-current", cwd=wt).strip() == branch

    edit = {"project": P, "path": "backend/src/L.java", "content": LOGIC.replace("crud.get()", "crud.list()")}
    assert dev.put("/api/v1/develop/file", json=edit).status_code == 400, "dev는 직접 고치지 않는다"
    assert dev.put("/api/v1/develop/file", json={**edit, "branch": "main"}).status_code == 400
    assert pm.put("/api/v1/develop/file", json={**edit, "branch": branch}).status_code == 403
    saved = dev.put("/api/v1/develop/file", json={**edit, "branch": branch}).json()
    assert saved["changed"] == "M" and "crud.list()" in (wt / "backend/src/L.java").read_text(encoding="utf-8")
    assert "crud.get()" in (develop.app_dir(P) / "backend/src/L.java").read_text(encoding="utf-8"), "dev는 그대로"
    tree = dev.get("/api/v1/develop/files", params={"project": P, "branch": branch}).json()
    assert tree["changes"] == 1 and {f["path"]: f["changed"] for f in tree["files"]}["backend/src/L.java"] == "M"
    assert dev.get("/api/v1/develop/files", params={"project": P}).json()["changes"] == 0
    assert dev.get("/api/v1/develop/files", params={"project": P, "branch": "nope"}).status_code == 404
    assert dev.get("/api/v1/develop/file", params={"project": P, "branch": branch, "path": "../../../README.md"}).status_code == 404

    c = {"project": P, "branch": branch, "message": "목록 조회로 바꿈"}
    assert pm.post("/api/v1/develop/repo/commit", json=c).status_code == 403
    assert dev.post("/api/v1/develop/repo/commit", json={**c, "message": " "}).status_code == 400
    assert dev.post("/api/v1/develop/repo/commit", json={**c, "branch": "main"}).status_code == 400
    done = dev.post("/api/v1/develop/repo/commit", json=c).json()
    assert done["files"] == ["backend/src/L.java"]
    assert _git("log", "-1", "--format=%an|%ae|%s", branch).strip() == "dev|dev@test.local|목록 조회로 바꿈", "작성자 = 로그인한 회원"
    listed = {b["name"]: b for b in dev.get("/api/v1/develop/repo", params={"project": P}).json()["branches"]}
    assert listed[branch]["ahead"] == 1 and listed[branch]["changes"] == 0 and listed["dev"]["protected"] and listed["gen"]["protected"]
    assert "main" not in listed, "main은 첫 릴리스 때 생긴다"
    assert listed[branch]["owner"]["name"] == "dev"

    assert other.delete("/api/v1/develop/repo/branch", params={"project": P, "name": "main"}).status_code == 404
    assert pm.delete("/api/v1/develop/repo/branch", params={"project": P, "name": branch}).status_code == 403, "만든 사람·개발자만 지운다"
    assert other.delete("/api/v1/develop/repo/branch", params={"project": P, "name": branch}).json()["ok"], "다른 개발자도 지울 수 있다"
    assert not wt.exists() and branch not in _git("branch")


def _branch_with_commit(client, name: str, path: str, content: str) -> str:
    branch = client.post("/api/v1/develop/repo/branch", json={"project": P, "name": name}).json()["name"]
    assert client.put("/api/v1/develop/file", json={"project": P, "path": path, "content": content, "branch": branch}).status_code == 200
    assert client.post("/api/v1/develop/repo/commit", json={"project": P, "branch": branch, "message": f"{name} 고침"}).status_code == 200
    return branch


def test_merge_request_merge_and_notices(sample_doc):
    from test_develop_screen import _approved_final
    _approved_final(sample_doc)   # 알림은 버전이 있는 프로젝트에서만 모은다
    _developed()
    dev, head, pm = signed_in_client("developer", "dev"), signed_in_client("developer", "dev2"), signed_in_client("pm")
    branch = _branch_with_commit(dev, "feature/dev-readme", "README.md", "# app 고침\n")
    assert dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": "dev"}).status_code == 400
    req = dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": branch, "note": "README 고침"}).json()
    assert req["status"] == "open" and req["by"]["name"] == "dev"
    assert dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": branch}).status_code == 400, "열린 요청은 하나"

    kinds = lambda c, k: [n for n in c.get("/api/v1/notices").json()["notices"] if n["kind"] == k]   # noqa: E731
    asked = kinds(head, "merge_request")
    assert len(asked) == 1 and asked[0]["title"] == f"dev님의 합치기 요청 ({branch})" and asked[0]["version"] == "1.1.1"
    assert not kinds(dev, "merge_request") and not kinds(pm, "merge_request"), "요청 알림은 다른 개발자에게 (요청한 본인·PM은 아님)"

    diff = head.get("/api/v1/develop/repo/diff", params={"project": P, "branch": branch}).json()
    assert diff["ahead"] == 1 and [(f["path"], f["status"]) for f in diff["files"]] == [("README.md", "M")] and "+# app 고침" in diff["files"][0]["diff"]
    assert pm.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": req["id"]}).status_code == 403, "PM은 합치지 않는다 (개발자만)"
    merged = head.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": req["id"]})
    assert merged.status_code == 200 and merged.json()["status"] == "merged", merged.text
    assert (develop.app_dir(P) / "README.md").read_text(encoding="utf-8") == "# app 고침\n"
    assert _git("log", "-1", "--format=%an|%s|%P", "dev").strip().startswith(f"dev2|합치기: {branch} (요청 dev, 승인 dev2)|")
    assert len(_git("log", "-1", "--format=%P", "dev").split()) == 2, "--no-ff 합친 커밋"
    got = kinds(dev, "merged")
    assert len(got) == 1 and got[0]["title"] == f"dev2님이 합치기 요청을 반영했습니다 ({branch})"
    assert not kinds(head, "merged") and not kinds(pm, "merged"), "합친 알림은 요청한 사람에게만"
    assert head.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": req["id"]}).status_code == 400, "이미 처리"

    # Slack: 회원을 정한 알림은 그 회원만 부른다 (그 회원이 프로젝트 담당자일 때 — 담당자가 아니면 Slack으로 부르지 않는다)
    from app.services import project_access, slack, users
    assert not [m for n, ms in slack._pending({P}).values() if n["kind"] == "merged" for m in ms], "담당자가 아니면 안 부른다"
    project_access.set_members(P, [m.id for m in users.list_members() if m.name == "dev"])
    pending = slack._pending({P})
    who = [m.name for n, ms in pending.values() if n["kind"] == "merged" for m in ms]
    assert who == ["dev"]


def test_conflict_returns_409_and_keeps_dev_clean_then_reject_and_update_branch(sample_doc):
    from test_develop_screen import _approved_final
    _approved_final(sample_doc)
    _developed()
    dev, head, pm = signed_in_client("developer", "dev"), signed_in_client("developer", "dev2"), signed_in_client("pm")
    a = _branch_with_commit(dev, "feature/a", "README.md", "# A\n")
    b = _branch_with_commit(dev, "feature/b", "README.md", "# B\n")
    ra = dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": a}).json()
    rb = dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": b}).json()
    assert head.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": ra["id"]}).status_code == 200
    before = _git("rev-parse", "dev").strip()
    clash = head.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": rb["id"]})
    assert clash.status_code == 409 and clash.json()["conflicts"] == ["README.md"], clash.text
    assert _git("rev-parse", "dev").strip() == before and _git("status", "--porcelain") == "", "충돌이면 되돌려 dev는 깨끗"
    open_b = next(r for r in dev.get("/api/v1/develop/repo", params={"project": P}).json()["requests"] if r["id"] == rb["id"])
    assert open_b["status"] == "open" and open_b["conflicts"] == ["README.md"]

    upd = dev.post("/api/v1/develop/repo/update-branch", json={"project": P, "branch": b})
    assert upd.status_code == 409 and upd.json()["conflicts"] == ["README.md"]
    wt = repo.worktree(P, b)
    assert _git("status", "--porcelain", cwd=wt) == "" and (wt / "README.md").read_text(encoding="utf-8") == "# B\n", "브랜치도 되돌림"

    assert head.post("/api/v1/develop/repo/reject", json={"project": P, "request_id": rb["id"], "reason": ""}).status_code == 400
    assert pm.post("/api/v1/develop/repo/reject", json={"project": P, "request_id": rb["id"], "reason": "x"}).status_code == 403
    rej = head.post("/api/v1/develop/repo/reject", json={"project": P, "request_id": rb["id"], "reason": "충돌 풀고 다시"}).json()
    assert rej["status"] == "rejected"
    notes = [n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "merge_rejected"]
    assert len(notes) == 1 and notes[0]["detail"] == "사유: 충돌 풀고 다시"

    c = _branch_with_commit(dev, "feature/c", "frontend/src/App.tsx", "y\n")
    head.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": c}).json()["id"]})
    up = dev.post("/api/v1/develop/repo/update-branch", json={"project": P, "branch": a}).json()
    assert up["merged"] and (repo.worktree(P, a) / "frontend/src/App.tsx").read_text(encoding="utf-8") == "y\n", "dev 가져오기"


def test_log_and_commit_diff():
    _developed()
    dev = signed_in_client("developer", "dev")
    branch = _branch_with_commit(dev, "feature/log", "README.md", "# log\n")
    commits = dev.get("/api/v1/develop/repo/log", params={"project": P}).json()["commits"]
    assert [c["ai"] for c in commits] == [False, True] and commits[0]["author"] == "dev"
    assert {"type": "branch", "name": branch} in commits[0]["refs"]
    assert {"type": "tag", "name": "final-v1.1.1"} in commits[1]["refs"] and {"type": "head", "name": "HEAD"} in commits[1]["refs"]
    assert commits[0]["parents"] == [commits[1]["sha"]]
    shown = dev.get("/api/v1/develop/repo/commit", params={"project": P, "sha": commits[0]["sha"]}).json()
    assert [(f["path"], f["status"]) for f in shown["files"]] == [("README.md", "M")] and "-# app" in shown["files"][0]["diff"]
    first = dev.get("/api/v1/develop/repo/commit", params={"project": P, "sha": commits[1]["sha"]}).json()
    assert {f["status"] for f in first["files"]} == {"A"} and first["ai"]
    assert dev.get("/api/v1/develop/repo/commit", params={"project": P, "sha": "zz; rm"}).status_code == 400


def test_single_remote_push_refuses_non_fast_forward_and_migrates_old_settings(tmp_path, monkeypatch):
    _developed()
    out = get_settings().output_dir / P
    out.mkdir(parents=True, exist_ok=True)
    # 예전 두 저장소 설정 → 백엔드 저장소 하나로 (토큰 유지)
    old_token = gitpush._fernet().encrypt(b"ghp_old").decode()
    (out / "git.json").write_text(json.dumps({"frontend": {"url": "https://example.com/f.git", "branch": "", "token": ""},
                                              "backend": {"url": "https://example.com/b.git", "branch": "", "token": old_token}}), encoding="utf-8")
    assert gitpush.settings(P) == {"url": "https://example.com/b.git", "branch": "main", "has_token": True}
    assert gitpush._token(P) == "ghp_old"
    saved = gitpush.save_settings(P, {"url": "https://example.com/one.git", "token": "ghp_secret"})
    assert saved == {"url": "https://example.com/one.git", "branch": "main", "has_token": True}
    assert "ghp_secret" not in (out / "git.json").read_text(encoding="utf-8")

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(bare)], check=True)
    conf = json.loads((out / "git.json").read_text(encoding="utf-8"))
    conf["url"] = str(bare)   # 테스트는 로컬 저장소로 (화면 입력은 https만 받는다)
    (out / "git.json").write_text(json.dumps(conf), encoding="utf-8")
    monkeypatch.setattr(gitpush, "find_version", lambda p, v: {})
    recorded = {}
    monkeypatch.setattr(gitpush, "update_version", lambda p, v, **k: recorded.update(k))
    member = type("M", (), {"id": 1, "name": "한개발", "email": "dev@team4.local"})()

    def push():
        job = jobs.Job(id="g", project=P, base="1.1.1", source="git", status="running", message="")
        gitpush._GitRunner(job, P, "1.1.1", member, "dev")._run([])
        return job

    job = push()
    assert job.status == "done", job.log
    remote = lambda *a: subprocess.run(["git", "--git-dir", str(bare), *a], capture_output=True, text=True).stdout.strip()   # noqa: E731
    assert remote("rev-parse", "dev") == _git("rev-parse", "dev").strip() and remote("tag") == "final-v1.1.1"
    assert recorded["git"]["commit"] == _git("rev-parse", "dev").strip()
    assert not any("ghp_secret" in line for line in job.log), "기록에 토큰이 남지 않는다"
    status = gitpush.remote_status(P)
    assert (status["ahead"], status["behind"]) == (0, 0)

    res = _regen({"README.md": "# 새 개발\n"})
    repo.merge(P, res["request"], _member("developer"))
    assert gitpush.remote_status(P)["ahead"] == 2, "올리기 전: 원격보다 2개 앞섬 (gen 커밋 + 합친 커밋, fetch 없이)"

    # 원격에 서버에 없는 커밋이 생기면 올리지 않는다
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    (other / "x.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=o", "-c", "user.email=o@x", "-c", "commit.gpgsign=false", "add", "."], cwd=other, check=True)
    subprocess.run(["git", "-c", "user.name=o", "-c", "user.email=o@x", "-c", "commit.gpgsign=false", "commit", "-qm", "밖에서"], cwd=other, check=True)
    subprocess.run(["git", "push", "-q", "origin", "dev"], cwd=other, check=True)
    checked = signed_in_client("developer").post("/api/v1/develop/repo/fetch", json={"project": P}).json()
    assert (checked["ahead"], checked["behind"]) == (2, 1), checked
    job = push()
    assert job.status == "failed" and "서버에 없는 커밋" in job.message
    assert remote("log", "-1", "--format=%s", "dev") == "밖에서", "강제로 덮어쓰지 않는다"
    assert signed_in_client("pm").post("/api/v1/develop/repo/fetch", json={"project": P}).status_code == 403


def test_self_approval_is_blocked_unless_only_developer_and_release_moves_main(sample_doc):
    """자기 요청은 다른 개발자가 승인·반려한다 (개발자 담당자가 한 명이면 본인 승인 + 기록).
    첫 릴리스가 main을 만들고, 그 뒤 릴리스는 dev를 main으로 앞으로 감는다"""
    from app.services import project_access
    from test_develop_screen import _approved_final
    _approved_final(sample_doc)
    _developed()
    dev, dev2 = signed_in_client("developer", "dev"), signed_in_client("developer", "dev2")
    ids = {m.name: m.id for m in users.list_members()}
    project_access.set_members(P, [ids["dev"], ids["dev2"]])
    branch = _branch_with_commit(dev, "feature/self", "README.md", "# 내가 고침\n")
    req = dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": branch}).json()
    for path in ("merge", "reject"):
        r = dev.post(f"/api/v1/develop/repo/{path}", json={"project": P, "request_id": req["id"], "reason": "x"})
        assert r.status_code == 403 and "다른 개발자" in r.json()["detail"], path
    project_access.set_members(P, [ids["dev"]])   # 개발자 담당자가 한 명이면 본인 승인
    merged = dev.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": req["id"]}).json()
    assert merged["status"] == "merged" and merged["self_approved"] is True
    assert "본인 승인" in _git("log", "-1", "--format=%s", "dev")

    over = dev.get("/api/v1/develop/repo", params={"project": P}).json()
    assert over["released"] is False and over["pending_release"] == 3, "main 없음: 릴리스할 커밋 = dev 전체"
    unreleased = dev.get("/api/v1/develop/repo/diff", params={"project": P, "branch": "main"}).json()
    assert unreleased["ahead"] == 3 and {f["status"] for f in unreleased["files"]} == {"A"}, "첫 릴리스 차이 = dev 전체"
    assert signed_in_client("pm").post("/api/v1/develop/repo/release", json={"project": P}).status_code == 403
    rel = dev.post("/api/v1/develop/repo/release", json={"project": P}).json()
    assert rel["tag"].startswith("release-") and rel["commits"] == 3
    assert _git("rev-parse", "main").strip() == _git("rev-parse", "dev").strip() == rel["commit"]
    assert _git("rev-parse", rel["tag"]).strip() == rel["commit"]
    assert dev.post("/api/v1/develop/repo/release", json={"project": P}).status_code == 400, "릴리스할 커밋 없음"

    more = _branch_with_commit(dev, "feature/more", "frontend/src/App.tsx", "z\n")
    dev.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": more}).json()["id"]})
    assert dev.get("/api/v1/develop/repo/diff", params={"project": P, "branch": "main"}).json()["ahead"] == 2
    rel2 = dev.post("/api/v1/develop/repo/release", json={"project": P}).json()
    assert rel2["commits"] == 2 and rel2["tag"] != rel["tag"] and _git("rev-parse", "main").strip() == rel2["commit"]


def test_old_main_only_repo_gets_dev_branch(tmp_path):
    """이 기능 전의 저장소(main만, 작업 폴더가 main)는 처음 열 때 main에서 dev, dev에서 gen을 만들고 작업 폴더를 dev로 바꾼다"""
    app = develop.app_dir(P)
    app.mkdir(parents=True, exist_ok=True)
    (app / "README.md").write_text("# old\n", encoding="utf-8")
    _git("init", "-q", "-b", "main")
    _git("-c", "user.name=o", "-c", "user.email=o@x", "-c", "commit.gpgsign=false", "add", "-A")
    _git("-c", "user.name=o", "-c", "user.email=o@x", "-c", "commit.gpgsign=false", "commit", "-qm", "예전")
    repo.ensure_repo(P)
    assert _git("branch", "--show-current").strip() == "dev"
    assert _git("rev-parse", "dev").strip() == _git("rev-parse", "main").strip() == _git("rev-parse", "gen").strip()
    names = [b["name"] for b in repo.branches(P)]
    assert names[:3] == ["dev", "gen", "main"]
    (app / ".req-agent").mkdir()
    (app / ".req-agent" / "manifest.json").write_text("{}", encoding="utf-8")
    assert (repo.gen_tree(P) / ".req-agent" / "manifest.json").is_file(), "생성기 기록을 gen 작업 폴더로 복사해 온다"
    assert develop.gen_dir(P) == repo.gen_tree(P)
