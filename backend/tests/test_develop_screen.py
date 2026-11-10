"""화면의 최종 개발하기·미리보기·git 올리기 (최종 승인한 최종본, 개발자). 하위 프로세스·Docker·npm 없이."""
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
    dev, pm = signed_in_client("developer"), signed_in_client("pm")
    q = f"/api/v1/develop/plan?project=sample&version={final}"
    assert dev.get(q).status_code == 400, "승인 전에는 개발하지 않는다"
    update_version("sample", final, approval={"decision": "approve", "at": "2999-01-01T00:00:00", "by": {"name": "PM"}})
    plan = dev.get(q).json()
    assert plan["screens"] > 0 and plan["files"] > 0 and "Haiku 5.5" in plan["logic"]["cost"], "개발 전 화면·파일 수와 AI 예상 비용"
    assert pm.get(q).status_code == 403, "개발은 개발자만 (PM은 아님)"
    body = {"project": "sample", "version": final, "logic": False}
    assert pm.post("/api/v1/develop", json=body).status_code == 403
    assert dev.post("/api/v1/develop", json=body).status_code == 202
    notices = [n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "develop"]
    assert notices and notices[0]["version"] == final, "승인하면 개발자에게 '개발 대기' 알림"
    update_version("sample", final, develop={"at": "2999-01-01T00:00:01", "version": final})
    assert not [n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "develop"], "개발하면 알림이 사라진다"


def test_open_items_are_answered_at_final_approval(sample_doc):
    from app.agents.storage import get_store
    from app.agents.tools import open_items

    final = _approved_final(sample_doc)
    store = get_store()
    design = json.loads(store.read("sample", final, "design.json"))
    assert (design.get("api_design") or {}).get("functions")
    fn = design["functions"][0]
    fn["business_rules"] = [*fn["business_rules"], "(확인 필요 FQ-901) 추천 규칙", "(확인 필요 FQ-902) 바뀔 규칙", "(확인 필요) 질문 없는 규칙"]
    design["questions"] = [*design.get("questions", []),
                           {"question_id": "FQ-901", "kind": "MISSING", "question": "추천?", "context": "", "recommended": "예", "options": ["예", "아니오"]},
                           {"question_id": "FQ-902", "kind": "MISSING", "question": "답 필요?", "context": "", "recommended": "", "options": []}]
    store.write("sample", final, "design.json", json.dumps(design, ensure_ascii=False))
    pm = signed_in_client("pm")
    found = pm.get(f"/api/v1/projects/open-items?project=sample&version={final}").json()["items"]
    ids = {i["id"]: i for i in found}
    assert {"FQ-901", "FQ-902"} <= set(ids) and any(i["title"] == "질문 없는 규칙" for i in found)
    body = {"project": "sample", "version": final, "decision": "approve"}
    refused = pm.post("/api/v1/projects/approval", json=body)
    assert refused.status_code == 400 and "FQ-902" in refused.text, "답도 추천도 없으면 승인하지 않는다"
    assert not (find_version("sample", final).get("approval") or {}), "거절되면 아무것도 바뀌지 않는다"
    assert pm.post("/api/v1/projects/approval", json={**body, "answers": {i["id"]: "직접 적은 답" for i in found if not i["recommended"]}}).status_code == 200
    after = json.loads(store.read("sample", final, "design.json"))
    rules = next(f for f in after["functions"] if f["function_id"] == fn["function_id"])["business_rules"]
    assert "(결정 FQ-901) 추천 규칙" in rules and "(결정 FQ-902) 직접 적은 답" in rules and "(결정) 질문 없는 규칙" in rules
    assert open_items.items(after) == [], "승인 뒤에는 남은 항목이 없다"
    assert "FQ-901" in str(find_version("sample", final)["approval"]["resolved"])
    assert "보류 FQ-90" not in store.read("sample", final, "ACCEPTANCE_CRITERIA.md"), "개발 스펙에 반영"

    # 승인 뒤: 승인한 항목을 그대로 보고, '수정'으로 답을 바꾸면 승인은 그대로 두고 개발 스펙을 다시 쓴다
    shown = pm.get(f"/api/v1/projects/open-items?project=sample&version={final}&approved=true").json()
    assert shown["editable"] and {i["id"] for i in shown["items"]} == set(ids), "승인 직전 항목 그대로"
    changed = pm.post("/api/v1/projects/approval/answers", json={"project": "sample", "version": final,
                                                                   "answers": {**{i["id"]: "직접 적은 답" for i in found if not i["recommended"]},
                                                                               "FQ-901": "아니오", "FQ-902": "바꾼 답"}})
    assert changed.status_code == 200
    record = find_version("sample", final)
    assert record["approval"]["decision"] == "approve" and len(record["approval"]["edits"]) == 1
    assert {"id": "FQ-901", "title": "추천?", "answer": "아니오"} in record["approval"]["resolved"]
    rules = next(f for f in json.loads(store.read("sample", final, "design.json"))["functions"]
                 if f["function_id"] == fn["function_id"])["business_rules"]
    assert "(결정 FQ-902) 바꾼 답" in rules and "(결정 FQ-902) 직접 적은 답" not in rules, "승인 직전 상태에서 새 답으로"
    assert signed_in_client("developer").post("/api/v1/projects/approval/answers", json={"project": "sample", "version": final,
                                              "answers": {i["id"]: "다른 답" for i in found if not i["recommended"]}}).status_code == 200, "PM·개발자 모두"


def test_recheck_replaces_old_result_and_keeps_failure_log(sample_doc, monkeypatch):
    from app.services import preview

    final = _approved_final(sample_doc)
    update_version("sample", final, approval={"decision": "approve", "at": "2999-01-01T00:00:00", "by": {"name": "PM"}})
    dev, pm = signed_in_client("developer"), signed_in_client("pm")
    body = {"project": "sample", "version": final}
    assert pm.post("/api/v1/develop/check", json=body).status_code == 403
    assert dev.post("/api/v1/develop/check", json=body).status_code == 400, "개발 전에는 확인할 코드가 없다"
    update_version("sample", final, develop={"at": "2999-01-01T00:00:01", "version": final, "commit": "abc", "check": {"ok": False, "message": "옛날 실패"}})
    calls = []
    monkeypatch.setattr(preview, "runnable", lambda p: True)
    monkeypatch.setattr(preview, "status", lambda p: {"docker": True})
    monkeypatch.setattr(preview, "mark_stale", lambda p: calls.append("stale"))
    monkeypatch.setattr(preview, "start", lambda p: calls.append("start"))
    monkeypatch.setattr(preview, "wait", lambda p: {"status": "failed", "message": "컨테이너 실패", "log": ["port is already allocated"]})

    def run() -> tuple[dict, str]:
        job = jobs.Job(id="c", project="sample", base=final, source="check", status="running", message="")
        develop._CheckRunner(job, "sample", final)._run([])
        return find_version("sample", final)["develop"], job.message

    record, message = run()
    assert record["check"]["log"] == ["port is already allocated"] and record["check"]["at"] and "컨테이너 실패" in message, "실패 기록을 남긴다"
    assert record["commit"] == "abc" and calls == ["stale", "start"], "결과만 바꾼다"
    monkeypatch.setattr(preview, "wait", lambda p: {"status": "running", "message": ""})
    monkeypatch.setattr(preview, "smoke", lambda p: {"ok": True, "login": True, "apis": 2, "counts": {"ok": 2, "not_implemented": 0, "errors": 0, "other": 0}})
    record, message = run()
    assert record["check"]["ok"] and record["check"]["apis"] == 2 and "log" not in record["check"] and "통과" in message


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
    member = signed_in_client("developer")
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
    for role in ("pm", "developer"):   # 끄기는 PM·개발자 (로그인한 회원 모두)
        assert signed_in_client(role).post("/api/v1/preview/sample/stop").status_code == 200, role


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


def test_stack_status_reads_the_source_from_the_final_architecture_and_korean_words():
    """개발 범위 확정 화면: 최종 확정의 아키텍처 요약(api_design 안)이 찾은 원문 스택을 '미정'으로 보이지 않는다.
    한글 표기·수용 기준도 원문으로 보고 근거를 돌려준다. 기능 명세에서 다르게 고르면 업로드 문서 값을 경고한다."""
    pending = {"question_id": "TQ-001", "status": "PENDING"}
    arch = {"summary": "", "stack_in_source": [{"layer": "프레임워크", "value": "Spring Framework", "requirement_ids": ["COR-1"]},
                                                {"layer": "DBMS", "value": "MySQL", "requirement_ids": ["COR-1"]}]}
    final = stacks.status({"questions": [pending], "requirements": [], "api_design": {"architecture": arch}})
    assert final["basis"] == "원문" and final["parts"]["database"] == "mysql" and final["source"]["stack"] == "Spring Framework · MySQL"
    answered = {**pending, "status": "ANSWERED", "answer": stacks.compose({"frontend": "react", "backend": "fastapi", "database": "postgresql"})}
    kept = stacks.status({"questions": [answered], "requirements": [], "api_design": {"architecture": arch}})
    assert kept["basis"] == "원문" and "업로드 문서에는" in kept["conflict"] and "따릅니다" in kept["conflict"]

    korean = [{"requirement_id": "SFR-9", "title": "개발 환경", "description": "화면은 리액트", "acceptance_criteria": ["서버는 스프링 부트로 개발"]}]
    found = stacks.status({"questions": [pending], "requirements": korean})
    assert found["basis"] == "원문" and found["parts"]["backend"] == "spring" and found["source"]["evidence"][0]["id"] == "SFR-9"
    from app.agents.roles.api_designer import stack_question
    from app.schemas.requirement import Requirement
    req = Requirement.model_validate({"requirement_id": "SFR-9", "type": "NON_FUNCTIONAL", "scope": "SYSTEM", "category": "제약", "title": "개발 환경",
                                      "description": "화면은 리액트", "acceptance_criteria": ["서버는 스프링 부트로 개발"]})
    assert stack_question([req], [], ask=True) is None, "원문이 한글로 정했으면 TQ-001을 묻지 않는다"

    only_screen = [{"requirement_id": "N1", "title": "화면", "description": "화면은 Vue.js"}]
    asked = stacks.status({"questions": [pending], "requirements": only_screen})
    assert asked["basis"] == "미정" and asked["source"]["parts"] == {"frontend": "vue"}, "서버가 없으면 묻되 화면 값은 알려 준다"
    picked = stacks.status({"questions": [answered], "requirements": only_screen})
    assert picked["basis"] == "확정" and "화면 Vue 3" in picked["conflict"]
    web = [{"title": "웹 품질", "description": "전자정부 웹사이트 품질관리 지침 준수, 자바스크립트 화면"}]
    assert stacks.status({"questions": [pending], "requirements": web})["source"] is None, "지침·자바스크립트는 서버 스택이 아니다"


def test_git_settings_hide_tokens_and_push_one_repo(tmp_path, monkeypatch):
    """원격은 하나 (화면·백엔드·DB를 한 저장소에, 로컬 bare 저장소로). 토큰은 암호화해 두고 화면에는 있는지만 보낸다.
    올리는 것은 고른 브랜치 dev·main (빌드 결과·패키지·생성기 기록은 커밋되지 않음). 자세한 경우는 test_repo.py"""
    project = "sample"
    app = develop.app_dir(project)
    for path, text in {"README.md": "# app", "frontend/src/App.tsx": "x", "frontend/node_modules/m.js": "skip",
                       "backend/pom.xml": "<p/>", "backend/target/a.jar": "skip", "db/schema.sql": "create table t();",
                       "spec/API.md": "api", ".gitignore": "node_modules/\ntarget/\n.preview/\n.req-agent/\n",
                       ".req-agent/manifest.json": "{}"}.items():
        (app / path).parent.mkdir(parents=True, exist_ok=True)
        (app / path).write_text(text, encoding="utf-8")
    from app.services import repo
    repo.commit_develop(project, "1.1", "최종본 v1.1 개발", type("M", (), {"id": 999, "name": "한개발", "email": "d@x"})())
    saved = gitpush.save_settings(project, {"url": "https://example.com/app.git", "branch": "", "token": "ghp_secret"})
    assert saved == {"url": "https://example.com/app.git", "branch": "main", "has_token": True}
    raw = (get_settings().output_dir / project / "git.json").read_text(encoding="utf-8")
    assert "ghp_secret" not in raw and gitpush._token(project) == "ghp_secret", "토큰은 암호화해 저장"
    assert signed_in_client("pm").get("/api/v1/develop/git?project=sample").json()["has_token"] is True
    assert signed_in_client("pm").post("/api/v1/develop/git", json={"project": project, "url": "https://x/y.git"}).status_code == 403, \
        "원격 설정은 개발자만"

    remote = tmp_path / "app.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    store = json.loads(raw)
    store["url"] = str(remote)   # 테스트는 로컬 저장소로 (화면 입력은 https만 받는다)
    (get_settings().output_dir / project / "git.json").write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(gitpush, "find_version", lambda p, v: {})
    monkeypatch.setattr(gitpush, "update_version", lambda *a, **k: None)
    member = type("M", (), {"id": 1, "name": "한개발", "email": "dev@team4.local"})()
    job = jobs.Job(id="g", project=project, base="1.1", source="git", status="running", message="")
    gitpush._GitRunner(job, project, "1.1", member, "dev")._run([])
    assert job.status == "done", job.log
    heads = lambda: subprocess.run(["git", "--git-dir", str(remote), "branch", "--format=%(refname:short)"],   # noqa: E731
                                   capture_output=True, text=True).stdout.split()
    assert heads() == ["dev"], "고른 브랜치만 같은 이름으로 올린다"
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "dev"], capture_output=True, text=True).stdout.split()
    assert sorted(files) == [".gitignore", "README.md", "backend/pom.xml", "db/schema.sql", "frontend/src/App.tsx", "spec/API.md"]
    early = jobs.Job(id="m", project=project, base="1.1", source="git", status="running", message="")
    gitpush._GitRunner(early, project, "1.1", member, "main")._run([])
    assert early.status == "failed" and "릴리스" in early.message and gitpush.remote_status(project)["targets"]["main"]["missing"], \
        "main은 첫 릴리스 전에는 없어 올리지 않는다"
    repo.release(project, member)
    gitpush._GitRunner(jobs.Job(id="m", project=project, base="1.1", source="git", status="running", message=""), project, "1.1", member, "main")._run([])
    assert heads() == ["dev", "main"] and gitpush.remote_status(project)["targets"]["main"]["exists"] is True
    assert not any("ghp_secret" in line for line in job.log), "기록에 토큰이 남지 않는다"


def test_code_files_show_empty_logic_and_only_developers_save():
    """최종 개발물 1단계 파일 탐색기: 빈 업무 로직을 세고, 고친 파일을 표시하고, 개발자만 저장한다 (PM은 보기만)."""
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

    dev, pm = signed_in_client("developer"), signed_in_client("pm")
    tree = pm.get("/api/v1/develop/files?project=sample").json()
    assert [f["path"] for f in tree["files"]] == ["README.md", "backend/src/main/java/app/logic/L.java"], "패키지·생성기 기록·비밀번호 폴더는 숨김"
    assert (tree["holes"], tree["empty_holes"], tree["edited"]) == (2, 1, 0), "기본값 그대로인 업무 로직 자리만 빈 로직"
    opened = pm.get("/api/v1/develop/file", params={"project": "sample", "path": "backend/src/main/java/app/logic/L.java"}).json()
    assert opened["content"] == logic and [(h["name"], h["line"], h["empty"]) for h in opened["hole_list"]] == [("a", 3, True), ("b", 8, False)]
    for hidden in ("local/accounts.txt", "frontend/node_modules/x.js", "../../etc/passwd"):
        assert pm.get("/api/v1/develop/file", params={"project": "sample", "path": hidden}).status_code == 404, hidden

    branch = dev.post("/api/v1/develop/repo/branch", json={"project": "sample", "name": "feature/dev-readme"}).json()["name"]
    body = {"project": "sample", "path": "README.md", "content": "# 고친 앱\n", "branch": branch}
    assert pm.put("/api/v1/develop/file", json=body).status_code == 403, "PM은 코드를 고치지 않는다 (개발자만)"
    saved = dev.put("/api/v1/develop/file", json=body).json()
    assert saved["edited"] and saved["changed"] == "M", "개발자는 브랜치에서 고치고 '사람이 고침'·커밋 안 한 변경으로 표시"
    assert (app / "README.md").read_text(encoding="utf-8") == "# app", "main은 그대로"
    assert dev.put("/api/v1/develop/file", json={**body, "path": "local/accounts.txt"}).status_code == 404
    assert pm.get("/api/v1/preview/sample/terminal").json()["app_log"] == [], "실행 전에는 앱 서버 로그 없음"


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
    from app.services import repo
    branch = repo.create_branch("sample", "feature/fix", type("M", (), {"id": 1, "name": "한개발"})())["name"]
    appfiles.save("sample", "backend/src/main/java/app/logic/L.java", logic.replace("if (x)", "if (y)"), branch)
    assert appfiles.read("sample", "backend/src/main/java/app/logic/L.java", branch)["ai_holes"] == 0, "사람이 고치면 AI 표시가 빠진다"


def test_event_notices_go_to_roles_and_disappear_when_confirmed(sample_doc):
    """일 알림: 업로드 분석 완료 → 모든 회원, ERD 초안·개발 초안 → 개발자. '확인'하면 그 회원에게서만 사라진다.
    최종 개발하기는 개발자만 (PM은 아님)."""
    from app.services import directives
    _approved_final(sample_doc)   # 프로젝트가 있어야 알림을 모은다
    for source, base, new in (("run", "", "1"), ("spec", "1", "1.1"), ("develop", "1.1.1", "1.1.1")):
        directives.job_event(jobs.Job(id="j", project="sample", base=base, source=source, status="done", message="", new_version=new))
    kinds = lambda c: sorted(n["kind"] for n in c.get("/api/v1/notices").json()["notices"] if n["kind"] in ("uploaded", "erd_draft", "developed"))  # noqa: E731
    dev1, dev2, pm = signed_in_client("developer", "dev1"), signed_in_client("developer", "dev2"), signed_in_client("pm")
    assert kinds(dev1) == kinds(dev2) == ["developed", "erd_draft", "uploaded"], "개발자는 파트를 나누지 않고 모두 받는다"
    assert kinds(pm) == ["uploaded"]
    first = next(n for n in dev1.get("/api/v1/notices").json()["notices"] if n["kind"] == "erd_draft")
    assert dev1.post("/api/v1/notices/dismiss", json={"project": "sample", "id": first["id"]}).json()["ok"]
    assert kinds(dev1) == ["developed", "uploaded"], "확인한 알림은 사라진다"
    assert kinds(dev2) == ["developed", "erd_draft", "uploaded"], "다른 회원에게는 그대로"
    assert pm.get("/api/v1/develop/plan?project=sample&version=1.1").status_code == 403, "개발은 개발자만"


def test_pm_deletes_project_with_its_data(sample_doc):
    """프로젝트 삭제: PM만, 확인 칸에 이름을 그대로 적어야 한다. 버전·프로젝트 파일·코드 폴더가 모두 지워진다."""
    from app.agents.storage import get_store
    from app.services import directives
    _approved_final(sample_doc)
    directives.add_event("sample", "uploaded", "t", "", "spec", "1", None)
    folder = get_settings().output_dir / "sample" / "app"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "README.md").write_text("x", encoding="utf-8")
    lead = signed_in_client("pm")
    assert signed_in_client("developer").delete("/api/v1/projects?project=sample&confirm=sample").status_code == 403, "PM만"
    assert lead.delete("/api/v1/projects?project=sample&confirm=sampl").status_code == 400, "이름이 다르면 안 지운다"
    assert get_store().has_project("sample")
    r = lead.delete("/api/v1/projects?project=sample&confirm=sample")
    assert r.status_code == 200, r.text
    assert not get_store().has_project("sample") and get_store().read_project_file("sample", directives.NOTICE_FILE) is None
    assert not (get_settings().output_dir / "sample").exists()
    assert all(p["id"] != "sample" for p in lead.get("/api/v1/projects").json()["projects"])
    assert lead.delete("/api/v1/projects?project=sample&confirm=sample").status_code == 400, "없는 프로젝트"


def test_old_final_without_behavior_is_filled_once(sample_doc, monkeypatch):
    """업무 동작 분석 전에 만든 예전 최종본(BEHAVIOR.md 없음): 채우기는 업무 동작 설계를 한 번 묻고 개발 스펙을 다시 쓴다.
    다시 누르면 묻지 않는다. PM·개발자 ('ERD 최종 확정'과 같은 권한)."""
    from app.agents.storage import get_store
    final = _approved_final(sample_doc)
    store = get_store()
    design = json.loads(store.read("sample", final, "design.json"))
    design["api_design"].pop("behavior", None)
    design["meta"]["dev_spec"]["files"].remove("BEHAVIOR.md")
    store.write("sample", final, "design.json", json.dumps(design, ensure_ascii=False))
    (get_settings().output_dir / "sample" / f"v{final}" / "BEHAVIOR.md").unlink()   # 테스트 저장소는 로컬 폴더
    from app.agents.roles import behavior
    monkeypatch.setattr(behavior, "request", lambda *a: "상태 컬럼 요청")   # 예시 문서에는 상태 컬럼이 없어 요청이 빈다
    fake = FakeLLM(broken_fk=False)
    llm_module.set_llm(fake)
    body = {"project": "sample", "version": final}
    r = signed_in_client("developer").post("/api/v1/projects/dev-spec/fill", json=body)
    assert r.status_code == 200, r.text
    assert fake.calls["BehaviorOut"] == 1 and store.exists("sample", final, "BEHAVIOR.md")
    after = json.loads(store.read("sample", final, "design.json"))
    assert after["api_design"]["behavior"]["fingerprint"] and "BEHAVIOR.md" in after["meta"]["dev_spec"]["files"]
    again = signed_in_client("pm").post("/api/v1/projects/dev-spec/fill", json=body)
    assert again.status_code == 400 and fake.calls["BehaviorOut"] == 1, "이미 채운 최종본은 다시 묻지 않는다"


def test_preview_reports_why_the_app_server_stopped(monkeypatch):
    """실행 미리보기: 앱 서버 컨테이너가 시작하다 꺼지면 3분을 기다리지 않고, 앱 로그의 원인 줄(Caused by …)을 기록과 메시지에 남긴다"""
    from app.services import preview

    log = ("app-1  | 2026 ERROR 1 --- [main] o.s.boot.SpringApplication : Application run failed\n"
           "app-1  | org.springframework.beans.factory.BeanDefinitionStoreException: Failed to parse configuration class [app.Application]\n"
           "app-1  |     at org.springframework.Foo.bar(Foo.java:1)\n"
           "app-1  | Caused by: ConflictingBeanDefinitionException: bean name 'fnA' for [app.api.FnA] conflicts with [app.feature.FnA]\n")

    def run(cmd, **kw):
        out = "exited\n" if cmd[:3] == ["docker", "ps", "-a"] else log if "logs" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(preview.subprocess, "run", run)
    pushed = []
    log_obj = type("L", (), {"_push": lambda self, line: pushed.append(line)})()
    assert preview._app_exited("sample")
    reason = preview._exit_reason("sample", log_obj)
    assert reason.startswith("Caused by: ConflictingBeanDefinitionException") and "app.feature.FnA" in reason
    assert any("Application run failed" in p for p in pushed) and not any(p.strip().endswith("(Foo.java:1)") for p in pushed), \
        "원인 줄만 (스택 줄 at … 은 뺀다)"


def test_demo_accounts_use_the_latest_password(tmp_path, monkeypatch):
    """앱 DB를 새로 만들면 앱이 시연 계정 비밀번호를 accounts.txt 뒤에 덧붙인다: 같은 아이디가 여러 번이면 마지막(현재 DB) 것을 쓴다"""
    from app.services import preview

    monkeypatch.setattr(preview, "app_dir", lambda project: tmp_path)
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "accounts.txt").write_text(
        "# 시연 계정\nadmin\t전체 보기\told-a\nuser\t임직원\told-u\nadmin\t전체 보기\tnew-a\nuser\t임직원\tnew-u\n", encoding="utf-8")
    assert [(a["loginId"], a["password"]) for a in preview.accounts("x")] == [("admin", "new-a"), ("user", "new-u")]
