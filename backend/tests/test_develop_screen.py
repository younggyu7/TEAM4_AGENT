"""화면의 최종 개발하기·미리보기·git 올리기 (팀장이 승인한 최종본, 개발 파트장·팀장). 하위 프로세스·Docker·npm 없이."""
import json
import subprocess

from fake_llm import FakeLLM
from app import cli
from app.agents.llm import client as llm_module
from app.agents.tools import stacks
from app.agents.tools.versions import find_version, update_version
from app.core.config import get_settings, override_settings
from app.services import develop, gitpush, jobs
from conftest import signed_in_client


def _approved_final(sample_doc) -> str:
    override_settings(llm_provider="anthropic")
    llm_module.set_llm(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer"]) == 0
    assert cli.main(["review", "sample", "v1"]) == 0
    return "1.1"


def test_develop_needs_approved_final_and_dev_role(sample_doc, monkeypatch):
    final = _approved_final(sample_doc)
    monkeypatch.setattr(develop, "start_develop", lambda **kw: jobs.Job(id="d", project="sample", base=final, source="develop",
                                                                       status="running", message=""))
    dev, dev_head, data_head = signed_in_client("dev"), signed_in_client("dev_head"), signed_in_client("data_head")
    q = f"/api/v1/develop/plan?project=sample&version={final}"
    assert dev_head.get(q).status_code == 400, "승인 전에는 개발하지 않는다"
    update_version("sample", final, approval={"decision": "approve", "at": "2999-01-01T00:00:00", "by": {"name": "팀장"}})
    plan = dev_head.get(q).json()
    assert plan["screens"] > 0 and plan["files"] > 0 and "Haiku 5.5" in plan["logic"]["cost"], "개발 전 화면·파일 수와 AI 예상 비용"
    assert dev.get(q).status_code == 403 and data_head.get(q).status_code == 403, "개발은 개발 파트장·팀장만"
    body = {"project": "sample", "version": final, "logic": False}
    assert dev.post("/api/v1/develop", json=body).status_code == 403
    assert dev_head.post("/api/v1/develop", json=body).status_code == 202
    notices = [n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "develop"]
    assert notices and notices[0]["version"] == final, "승인하면 개발 파트에 '개발 대기' 알림"
    update_version("sample", final, develop={"at": "2999-01-01T00:00:01", "version": final})
    assert not [n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "develop"], "개발하면 알림이 사라진다"


def test_preview_files_stay_inside_and_need_login(sample_doc):
    from fastapi.testclient import TestClient

    from app.main import app
    mock = develop.app_dir("sample") / develop.PREVIEW / "mock"
    (mock / "assets").mkdir(parents=True)
    (mock / "index.html").write_text("<p>preview</p>", encoding="utf-8")
    (mock / "assets" / "a.js").write_text("1", encoding="utf-8")
    (get_settings().output_dir / "sample").mkdir(exist_ok=True)
    (get_settings().output_dir / "sample" / "v1").mkdir(exist_ok=True)
    (get_settings().output_dir / "sample" / "v1" / "design.json").write_text("{}", encoding="utf-8")
    assert TestClient(app).get("/api/v1/preview/sample/mock/").status_code == 401, "로그인한 회원만"
    member = signed_in_client("dev")
    assert member.get("/api/v1/preview/sample/mock/").text == "<p>preview</p>"
    assert member.get("/api/v1/preview/sample/mock/assets/a.js").text == "1"
    assert "root:" not in member.get("/api/v1/preview/sample/mock/..%2F..%2F..%2F..%2Fetc%2Fpasswd").text, "폴더 밖은 열지 않는다"
    from app.api.v1.develop import _inside
    from app.core.exceptions import NotFound
    try:
        _inside(mock, "../../x")
        raise AssertionError("폴더 밖 경로는 404")
    except NotFound:
        pass
    page = member.get("/api/v1/preview/sample/")
    assert page.status_code == 200 and "최종 개발물 미리보기" in page.text
    status = member.get("/api/v1/preview/sample/status").json()
    assert (status["status"], status["backend"], status["mock"]) == ("stopped", False, True)
    assert member.post("/api/v1/preview/sample/start").json()["status"] == "no_backend", "백엔드가 없으면 실행하지 않는다"


def test_stack_is_chosen_in_three_parts_and_warns_when_there_is_no_template():
    """기능 명세에서 화면·서버·DB를 하나씩 고른다. 틀이 없는 값도 고를 수 있지만 경고한다. 원문 스택은 그대로 따르고 경고만."""
    assert stacks.compose({"frontend": "react", "backend": "spring", "database": "postgresql"}) \
        == "Java 17 · Spring Boot 3 · PostgreSQL 16 · React 18 · TypeScript · Vite"
    vue = {"frontend": "vue", "backend": "spring", "database": "postgresql"}
    assert stacks.compose(vue).endswith("Vue 3 · TypeScript · Vite"), "틀이 없어도 고객 요구라 고를 수 있다"
    try:
        stacks.compose({"frontend": "react", "backend": "spring"})
        raise AssertionError("빠진 칸은 거부")
    except ValueError:
        pass
    assert stacks.guess("Java 17 · 전자정부 표준프레임워크 · PostgreSQL 16") == {"frontend": "", "backend": "egov", "database": "postgresql"}
    assert stacks.parts_of("Java 17 · 전자정부 표준프레임워크 · PostgreSQL 16")["frontend"] == "react", "못 찾은 층은 기본 틀"

    question = {"question_id": "TQ-001", "status": "PENDING"}
    assert stacks.status({"questions": [question]})["basis"] == "미정"
    answered = stacks.status({"questions": [{**question, "status": "ANSWERED", "answer": stacks.compose(vue)}]})
    assert answered["basis"] == "확정" and [m["layer"] for m in answered["untemplated"]] == ["frontend"] and "비용" in answered["warning"]
    source = stacks.status({"questions": [], "requirements": [{"title": "개발 환경", "description": "서버는 Python Django로 개발한다"}]})
    assert source["basis"] == "원문" and source["untemplated"][0]["layer"] == "backend" and "뼈대" in source["warning"], \
        "원문이 틀 없는 스택을 정했으면 경고만"
    assert stacks.status({"questions": [], "requirements": [{"title": "로그인", "description": "회원 로그인"}]})["basis"] == "없음"


def test_git_settings_hide_tokens_and_push_both_sides_to_their_repos(tmp_path, monkeypatch):
    """화면·백엔드 저장소에 따로 올린다 (로컬 bare 저장소로). 토큰은 암호화해 두고 화면에는 있는지만 보낸다."""
    project = "sample"
    app = develop.app_dir(project)
    for path, text in {"README.md": "# app", "frontend/src/App.tsx": "x", "frontend/node_modules/m.js": "skip",
                       "backend/pom.xml": "<p/>", "backend/target/a.jar": "skip", "db/schema.sql": "create table t();",
                       "spec/API.md": "api", "spec/SCREENS.md": "s", "spec/PRD.md": "prd", ".gitignore": "node_modules/\n"}.items():
        (app / path).parent.mkdir(parents=True, exist_ok=True)
        (app / path).write_text(text, encoding="utf-8")
    saved = gitpush.save_settings(project, {"frontend": {"url": "https://example.com/f.git", "branch": "main", "token": "ghp_secret"},
                                            "backend": {"url": "https://example.com/b.git", "token": ""}})
    assert saved["frontend"] == {"url": "https://example.com/f.git", "branch": "main", "has_token": True}
    raw = (get_settings().output_dir / project / "git.json").read_text(encoding="utf-8")
    assert "ghp_secret" not in raw and gitpush._token(project, "frontend") == "ghp_secret", "토큰은 암호화해 저장"

    remotes = {}
    for side in ("frontend", "backend"):
        remotes[side] = tmp_path / f"{side}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remotes[side])], check=True)
    store = json.loads(raw)
    for side in remotes:   # 테스트는 로컬 저장소로 (화면 입력은 https만 받는다)
        store[side]["url"] = str(remotes[side])
    (get_settings().output_dir / project / "git.json").write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(gitpush, "find_version", lambda p, v: {"approval": {"by": {"name": "김관리"}}, "develop": {"by": {"name": "한개발"}}})
    monkeypatch.setattr(gitpush, "update_version", lambda *a, **k: None)
    member = type("M", (), {"id": 1, "name": "한개발", "email": "dev@team4.local"})()
    job = jobs.Job(id="g", project=project, base="1.1", source="git", status="running", message="")
    runner = gitpush._GitRunner(job, project, "1.1", member)
    runner._run([])
    assert job.status == "done", job.log
    files = lambda side, branch: subprocess.run(["git", "--git-dir", str(remotes[side]), "ls-tree", "-r", "--name-only", branch],   # noqa: E731
                                                capture_output=True, text=True).stdout.split()
    assert sorted(files("frontend", "main")) == [".gitignore", "frontend/src/App.tsx", "spec/API.md", "spec/SCREENS.md"]
    assert sorted(files("backend", "req-agent/v1.1")) == [".gitignore", "README.md", "backend/pom.xml", "db/schema.sql",
                                                          "spec/API.md", "spec/PRD.md", "spec/SCREENS.md"]
    assert not any("ghp_secret" in line for line in job.log), "기록에 토큰이 남지 않는다"
    runner2 = gitpush._GitRunner(jobs.Job(id="g2", project=project, base="1.1", source="git", status="running", message=""), project, "1.1", member)
    runner2._run([])
    assert runner2.job.status == "done" and any("바뀐 파일이 없어" in line for line in runner2.job.log), "다시 올리면 바뀐 것만"


def test_code_files_show_empty_logic_and_only_dev_part_saves():
    """최종 개발물 1단계 파일 탐색기: 빈 업무 로직을 세고, 고친 파일을 표시하고, 개발 파트·팀장만 저장한다."""
    import hashlib

    app = develop.app_dir("sample")
    logic = "class L {\n    Object a() {\n        // <hole a>\n        return crud.get();\n        // </hole>\n    }\n" \
            "    Object b() {\n        // <hole b>\n        if (x) throw new RuntimeException();\n        return crud.get();\n        // </hole>\n    }\n}\n"
    files = {"README.md": "# app", "backend/src/main/java/app/logic/L.java": logic, "frontend/node_modules/x.js": "skip",
             "local/accounts.txt": "secret", ".req-agent/logic/x.json": "{}"}
    for path, text in files.items():
        (app / path).parent.mkdir(parents=True, exist_ok=True)
        (app / path).write_text(text, encoding="utf-8")
    manifest = {"files": {p: hashlib.sha256(t.encode()).hexdigest() for p, t in files.items() if p in ("README.md", "backend/src/main/java/app/logic/L.java")}}
    (app / ".req-agent" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    dev, data_head, lead = signed_in_client("dev"), signed_in_client("data_head"), signed_in_client("lead")
    tree = data_head.get("/api/v1/develop/files?project=sample").json()
    assert [f["path"] for f in tree["files"]] == ["README.md", "backend/src/main/java/app/logic/L.java"], "패키지·생성기 기록·비밀번호 폴더는 숨김"
    assert (tree["holes"], tree["empty_holes"], tree["edited"]) == (2, 1, 0), "기본값 그대로인 업무 로직 자리만 빈 로직"
    opened = data_head.get("/api/v1/develop/file", params={"project": "sample", "path": "backend/src/main/java/app/logic/L.java"}).json()
    assert opened["content"] == logic and [(h["name"], h["line"], h["empty"]) for h in opened["hole_list"]] == [("a", 3, True), ("b", 8, False)]
    for hidden in ("local/accounts.txt", "frontend/node_modules/x.js", "../../etc/passwd"):
        assert data_head.get("/api/v1/develop/file", params={"project": "sample", "path": hidden}).status_code == 404, hidden

    body = {"project": "sample", "path": "README.md", "content": "# 고친 앱\n"}
    assert data_head.put("/api/v1/develop/file", json=body).status_code == 403, "개발 파트·팀장만 고친다"
    saved = dev.put("/api/v1/develop/file", json=body).json()
    assert saved["edited"] and (app / "README.md").read_text(encoding="utf-8") == "# 고친 앱\n", "개발 파트원도 고칠 수 있고 '사람이 고침'으로 표시"
    assert lead.put("/api/v1/develop/file", json={**body, "path": "local/accounts.txt"}).status_code == 404
    assert lead.get("/api/v1/preview/sample/terminal").json()["app_log"] == [], "실행 전에는 앱 서버 로그 없음"


def test_oracle_is_design_only_and_develop_refuses(sample_doc):
    """Oracle은 AI 뼈대로도 실행되는 앱을 못 만든다: 경고는 '최종 개발하기 지원 안 함(설계 산출물까지만)', 개발은 거부"""
    from test_develop import _start
    parts = {"frontend": "react", "backend": "spring", "database": "oracle"}
    warn = stacks.warning(stacks.untemplated(parts))
    assert "최종 개발하기는 지원하지 않습니다 (설계 산출물까지만)" in warn and "뼈대" not in warn
    assert next(o for o in stacks.catalog()["database"] if o["id"] == "oracle")["develop"] is False
    _start(sample_doc)
    assert cli.main(["develop", "sample", "--from", "v1.1", "--stack", "database=oracle"]) == 2, "개발하지 않는다"


def test_code_browser_marks_what_ai_wrote():
    """빈 로직만 AI가 채운 자리(기록과 본문이 같음)와 기능 전체 AI가 쓴 파일(feature/·pages/)을 따로 표시한다. 사람이 고치면 AI 표시가 빠진다."""
    from app.services import appfiles
    app = develop.app_dir("sample")
    logic = "class L {\n    Object a() {\n        // <hole a>\n        return crud.get();\n        // </hole>\n    }\n" \
            "    Object b() {\n        // <hole b>\n        if (x) throw new RuntimeException();\n        return crud.get();\n        // </hole>\n    }\n}\n"
    for path, text in {"README.md": "# app", "backend/src/main/java/app/logic/L.java": logic,
                       "backend/src/main/java/app/feature/FnAController.java": "class A {}", "frontend/src/pages/FnA.tsx": "x",
                       "frontend/src/pages/index.ts": "x"}.items():
        (app / path).parent.mkdir(parents=True, exist_ok=True)
        (app / path).write_text(text, encoding="utf-8")
    (app / ".req-agent/logic").mkdir(parents=True, exist_ok=True)
    (app / ".req-agent/logic/FN-A.json").write_text(json.dumps({"hooks": {"b": "if (x) throw new RuntimeException();\nreturn crud.get();"}}), encoding="utf-8")
    tree = {f["path"]: f for f in appfiles.tree("sample")["files"]}
    assert tree["backend/src/main/java/app/logic/L.java"]["ai_holes"] == 1 and tree["backend/src/main/java/app/logic/L.java"]["empty_holes"] == 1
    assert tree["backend/src/main/java/app/feature/FnAController.java"]["ai_file"] and tree["frontend/src/pages/FnA.tsx"]["ai_file"]
    assert not tree["frontend/src/pages/index.ts"]["ai_file"] and not tree["README.md"]["ai_file"]
    assert [(h["name"], h["empty"], h["ai"]) for h in appfiles.read("sample", "backend/src/main/java/app/logic/L.java")["hole_list"]] \
        == [("a", True, False), ("b", False, True)]
    appfiles.save("sample", "backend/src/main/java/app/logic/L.java", logic.replace("if (x)", "if (y)"))
    assert appfiles.read("sample", "backend/src/main/java/app/logic/L.java")["ai_holes"] == 0, "사람이 고치면 AI 표시가 빠진다"
