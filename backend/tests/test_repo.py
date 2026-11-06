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


def _developed(version: str = "1.1.1") -> str:
    """최종 개발하기를 흉내 낸다: 코드 폴더에 파일을 쓰고 개발 커밋"""
    app = develop.app_dir(P)
    for path, text in {"README.md": "# app\n", "backend/src/L.java": LOGIC, "frontend/src/App.tsx": "x\n",
                       ".gitignore": "node_modules/\nlocal/\n.preview/\n.req-agent/\n", "local/accounts.txt": "secret",
                       ".req-agent/manifest.json": json.dumps({"files": {}}), "frontend/node_modules/m.js": "skip"}.items():
        (app / path).parent.mkdir(parents=True, exist_ok=True)
        (app / path).write_text(text, encoding="utf-8")
    return repo.commit_develop(P, version, repo.develop_message(version, {"mode": "build", "model": "claude-haiku-5-5", "effort": "medium"}, "한개발"))


def _member(role: str) -> users.Member:
    signed_in_client(role)
    return next(m for m in users.list_members() if m.email == ("admin@test.local" if role == "pm" else f"{role}@test.local"))


def test_develop_commits_on_main_with_tag_and_ignores_generator_state():
    sha = _developed()
    assert sha and _git("log", "-1", "--format=%an|%s").strip() == "req-agent|최종본 v1.1.1 개발 · 기능 전체 AI · claude-haiku-5-5 · medium (개발: 한개발)"
    assert _git("rev-parse", "final-v1.1.1").strip() == sha and _git("branch", "--show-current").strip() == "main"
    files = _git("ls-tree", "-r", "--name-only", "main").split()
    assert sorted(files) == [".gitignore", "README.md", "backend/src/L.java", "frontend/src/App.tsx"], "생성기 기록·시연 계정·패키지는 커밋 안 함"
    assert repo.commit_develop(P, "1.1.1", "같은 개발") is None, "바뀐 것이 없으면 커밋하지 않는다"
    (develop.app_dir(P) / "README.md").write_text("# app v2\n", encoding="utf-8")
    sha2 = repo.commit_develop(P, "1.1.1", "다시 개발")
    assert sha2 != sha and _git("rev-parse", "final-v1.1.1").strip() == sha2, "같은 최종본을 다시 개발하면 태그를 옮긴다"
    assert repo.final_version(P) == "1.1.1"


def test_runner_records_develop_commit(monkeypatch):
    """_DevelopRunner: 코드를 쓴 뒤 자동 확인 전에 main에 커밋하고 develop.commit에 남긴다"""
    recorded = {}

    def exec_(args):
        app = develop.app_dir(P)
        (app / "README.md").parent.mkdir(parents=True, exist_ok=True)
        (app / "README.md").write_text("# app\n", encoding="utf-8")
        return 0

    member = type("M", (), {"id": 1, "name": "한개발", "role": "developer", "email": "d@x"})()
    job = jobs.Job(id="d", project=P, base="1.1.1", source="develop", status="running", message="")
    runner = develop._DevelopRunner(job, P, "1.1.1", False, {}, member)
    order = []
    monkeypatch.setattr(runner, "_exec", exec_)
    monkeypatch.setattr(runner, "build_mock", lambda: False)
    monkeypatch.setattr(runner, "summary", lambda: {})
    monkeypatch.setattr(runner, "check", lambda: order.append(_git("rev-list", "--count", "main").strip()) or None)
    monkeypatch.setattr(develop, "find_version", lambda p, v: {})
    monkeypatch.setattr(develop, "update_version", lambda p, v, **k: recorded.update(k))
    runner._run([])
    assert job.status == "done", job.log
    assert recorded["develop"]["commit"] == _git("rev-parse", "main").strip() and order == ["1"], "자동 확인 전에 커밋"
    assert _git("log", "-1", "--format=%s").strip() == "최종본 v1.1.1 개발 · AI 안 씀 (개발: 한개발)"


def test_branch_worktree_save_commit_and_permissions():
    _developed()
    dev, pm, other = signed_in_client("developer", "dev"), signed_in_client("pm"), signed_in_client("developer", "dev2")
    body = {"project": P, "name": "feature/dev 로그인!", "from": "main"}
    assert pm.post("/api/v1/develop/repo/branch", json=body).status_code == 403, "PM은 브랜치를 만들지 않는다 (개발자만)"
    made = dev.post("/api/v1/develop/repo/branch", json=body)
    assert made.status_code == 200 and made.json()["name"] == "feature/dev-로그인", made.text
    branch = made.json()["name"]
    wt = develop.app_dir(P) / ".preview" / "worktrees" / "feature__dev-로그인"
    assert (wt / "backend/src/L.java").is_file() and _git("branch", "--show-current", cwd=wt).strip() == branch

    edit = {"project": P, "path": "backend/src/L.java", "content": LOGIC.replace("crud.get()", "crud.list()")}
    assert dev.put("/api/v1/develop/file", json=edit).status_code == 400, "main은 직접 고치지 않는다"
    assert dev.put("/api/v1/develop/file", json={**edit, "branch": "main"}).status_code == 400
    assert pm.put("/api/v1/develop/file", json={**edit, "branch": branch}).status_code == 403
    saved = dev.put("/api/v1/develop/file", json={**edit, "branch": branch}).json()
    assert saved["changed"] == "M" and "crud.list()" in (wt / "backend/src/L.java").read_text(encoding="utf-8")
    assert "crud.get()" in (develop.app_dir(P) / "backend/src/L.java").read_text(encoding="utf-8"), "main은 그대로"
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
    assert listed[branch]["ahead"] == 1 and listed[branch]["changes"] == 0 and listed["main"]["protected"]
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
    assert dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": "main"}).status_code == 400
    req = dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": branch, "note": "README 고침"}).json()
    assert req["status"] == "open" and req["by"]["name"] == "dev"
    assert dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": branch}).status_code == 400, "열린 요청은 하나"

    kinds = lambda c, k: [n for n in c.get("/api/v1/notices").json()["notices"] if n["kind"] == k]   # noqa: E731
    asked = kinds(head, "merge_request")
    assert len(asked) == 1 and asked[0]["title"] == f"dev님의 합치기 요청 ({branch})" and asked[0]["version"] == "1.1.1"
    assert kinds(dev, "merge_request") and not kinds(pm, "merge_request"), "요청 알림은 개발자에게 (PM은 아님)"

    diff = head.get("/api/v1/develop/repo/diff", params={"project": P, "branch": branch}).json()
    assert diff["ahead"] == 1 and [(f["path"], f["status"]) for f in diff["files"]] == [("README.md", "M")] and "+# app 고침" in diff["files"][0]["diff"]
    assert pm.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": req["id"]}).status_code == 403, "PM은 합치지 않는다 (개발자만)"
    merged = head.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": req["id"]})
    assert merged.status_code == 200 and merged.json()["status"] == "merged", merged.text
    assert (develop.app_dir(P) / "README.md").read_text(encoding="utf-8") == "# app 고침\n"
    assert _git("log", "-1", "--format=%an|%s|%P", "main").strip().startswith(f"dev2|합치기: {branch} (요청 dev, 승인 dev2)|")
    assert len(_git("log", "-1", "--format=%P", "main").split()) == 2, "--no-ff 합친 커밋"
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


def test_conflict_returns_409_and_keeps_main_clean_then_reject_and_update_branch(sample_doc):
    from test_develop_screen import _approved_final
    _approved_final(sample_doc)
    _developed()
    dev, head, pm = signed_in_client("developer", "dev"), signed_in_client("developer", "dev2"), signed_in_client("pm")
    a = _branch_with_commit(dev, "feature/a", "README.md", "# A\n")
    b = _branch_with_commit(dev, "feature/b", "README.md", "# B\n")
    ra = dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": a}).json()
    rb = dev.post("/api/v1/develop/repo/request", json={"project": P, "branch": b}).json()
    assert head.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": ra["id"]}).status_code == 200
    before = _git("rev-parse", "main").strip()
    clash = head.post("/api/v1/develop/repo/merge", json={"project": P, "request_id": rb["id"]})
    assert clash.status_code == 409 and clash.json()["conflicts"] == ["README.md"], clash.text
    assert _git("rev-parse", "main").strip() == before and _git("status", "--porcelain") == "", "충돌이면 되돌려 main은 깨끗"
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
    assert up["merged"] and (repo.worktree(P, a) / "frontend/src/App.tsx").read_text(encoding="utf-8") == "y\n", "메인 가져오기"


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
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    conf = json.loads((out / "git.json").read_text(encoding="utf-8"))
    conf["url"] = str(bare)   # 테스트는 로컬 저장소로 (화면 입력은 https만 받는다)
    (out / "git.json").write_text(json.dumps(conf), encoding="utf-8")
    monkeypatch.setattr(gitpush, "find_version", lambda p, v: {})
    recorded = {}
    monkeypatch.setattr(gitpush, "update_version", lambda p, v, **k: recorded.update(k))
    member = type("M", (), {"id": 1, "name": "한개발", "email": "dev@team4.local"})()

    def push():
        job = jobs.Job(id="g", project=P, base="1.1.1", source="git", status="running", message="")
        gitpush._GitRunner(job, P, "1.1.1", member)._run([])
        return job

    job = push()
    assert job.status == "done", job.log
    remote = lambda *a: subprocess.run(["git", "--git-dir", str(bare), *a], capture_output=True, text=True).stdout.strip()   # noqa: E731
    assert remote("rev-parse", "main") == _git("rev-parse", "main").strip() and remote("tag") == "final-v1.1.1"
    assert recorded["git"]["commit"] == _git("rev-parse", "main").strip()
    assert not any("ghp_secret" in line for line in job.log), "기록에 토큰이 남지 않는다"
    status = gitpush.remote_status(P)
    assert (status["ahead"], status["behind"]) == (0, 0)

    (develop.app_dir(P) / "README.md").write_text("# 새 개발\n", encoding="utf-8")
    repo.commit_develop(P, "1.1.1", "다시 개발")
    assert gitpush.remote_status(P)["ahead"] == 1, "올리기 전: 원격보다 1개 앞섬 (fetch 없이)"

    # 원격에 서버에 없는 커밋이 생기면 올리지 않는다
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    (other / "x.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=o", "-c", "user.email=o@x", "-c", "commit.gpgsign=false", "add", "."], cwd=other, check=True)
    subprocess.run(["git", "-c", "user.name=o", "-c", "user.email=o@x", "-c", "commit.gpgsign=false", "commit", "-qm", "밖에서"], cwd=other, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=other, check=True)
    checked = signed_in_client("developer").post("/api/v1/develop/repo/fetch", json={"project": P}).json()
    assert (checked["ahead"], checked["behind"]) == (1, 1), checked
    job = push()
    assert job.status == "failed" and "서버에 없는 커밋" in job.message
    assert remote("log", "-1", "--format=%s", "main") == "밖에서", "강제로 덮어쓰지 않는다"
    assert signed_in_client("pm").post("/api/v1/develop/repo/fetch", json={"project": P}).status_code == 403
