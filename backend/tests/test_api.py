"""/api/v1: 산출물 폴더를 읽는 API와 작업 API의 입력 검사 (LLM·하위 프로세스 없이)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services import jobs
from conftest import docx_upload, signed_in_client


@pytest.fixture
def client():
    return signed_in_client()


@pytest.fixture
def project():
    """output/team/demo/v1(새 형식: data_model·추적은 파일로), v1.1(예전 형식)"""
    root = get_settings().output_dir / "team" / "demo"
    v1, v11 = root / "v1", root / "v1.1"
    v1.mkdir(parents=True)
    v11.mkdir()
    (v1 / "design.json").write_text(json.dumps({
        "meta": {"generated_at": "2026-10-01T10:00:00", "run_id": "r1", "document": {"file_name": "a.pdf"},
                 "files": {"data_model": "data_model.json", "trace_links": "traceability.csv"}},
        "requirements": []}), encoding="utf-8")
    (v1 / "data_model.json").write_text(json.dumps({"entities": [{"entity_id": "t"}], "relations": []}),
                                        encoding="utf-8")
    (v1 / "traceability.csv").write_text("﻿from_id,to_id,link_type,design_version\nR-1,FN-1,IMPLEMENTS,1\n",
                                         encoding="utf-8")
    (v1 / "schema.sql").write_text("CREATE TABLE t (id bigint);", encoding="utf-8")
    (v11 / "design.json").write_text(json.dumps({"meta": {"generated_at": "2026-10-02T10:00:00"},
                                                 "data_model": {"entities": [], "relations": []},
                                                 "trace_links": []}), encoding="utf-8")
    return "team/demo"


def test_projects_lists_nested_project_with_versions_newest_first(client, project):
    body = client.get("/api/v1/projects").json()
    [p] = body["projects"]
    assert p["id"] == project and p["name"] == "demo"
    assert [v["design_version"] for v in p["versions"]] == ["1.1", "1"]
    assert p["versions"][1]["document"] == "a.pdf"


def test_design_merges_data_model_and_trace_links(client, project):
    d = client.get("/api/v1/design", params={"project": project, "version": "1"}).json()
    assert d["data_model"]["entities"] == [{"entity_id": "t"}]
    assert d["trace_links"] == [{"from_id": "R-1", "to_id": "FN-1", "link_type": "IMPLEMENTS"}]


def test_file_get_head_and_errors(client, project):
    q = {"project": project, "version": "1"}
    assert client.get("/api/v1/file", params={**q, "name": "schema.sql"}).text.startswith("CREATE TABLE")
    assert client.head("/api/v1/file", params={**q, "name": "schema.sql"}).status_code == 200
    assert client.head("/api/v1/file", params={**q, "name": "erd.mmd"}).status_code == 404
    assert client.get("/api/v1/file", params={**q, "name": "../../secret"}).status_code == 400
    r = client.get("/api/v1/design", params={"project": "../..", "version": "1"})
    assert r.status_code in (400, 404) and "detail" in r.json()


def test_title_is_saved_and_listed(client, project):
    assert client.post("/api/v1/projects/title", json={"project": project, "version": "1", "title": "고객 회의 반영"}).json() == {"ok": True}
    versions = client.get("/api/v1/projects").json()["projects"][0]["versions"]
    assert next(v for v in versions if v["design_version"] == "1")["title"] == "고객 회의 반영"
    assert client.post("/api/v1/projects/title", json={"project": "없음", "version": "1"}).status_code == 400


def test_new_version_checks_input_before_starting(client, project, monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "start_rerun", lambda **kw: started.append(kw) or jobs._new_job(
        project=kw["project"], base=kw["base"], source=kw["source"], message="-"))
    assert client.post("/api/v1/projects/versions", json={"project": project, "base": "x"}).status_code == 400
    assert client.post("/api/v1/projects/versions", json={"project": project, "base": "9"}).status_code == 404
    r = client.post("/api/v1/projects/versions", json={"project": project, "base": "v1", "source": "spec",
                                                       "answers": {"Q-001": "예"}})
    assert r.status_code == 202 and started[0]["base"] == "1" and started[0]["answers"] == {"Q-001": "예"}
    job = r.json()["job"]
    assert client.get("/api/v1/jobs", params={"id": job["id"]}).json()["job"]["status"] == "running"
    running = client.get("/api/v1/jobs/running", params={"project": project}).json()["job"]
    assert running["id"] == job["id"], "로그아웃·새로고침 뒤에도 프로젝트로 진행 중인 작업을 찾는다"
    assert client.post("/api/v1/projects/versions", json={"project": project, "base": "1"}).status_code == 409
    assert client.get("/api/v1/jobs", params={"id": "없음"}).status_code == 404
    jobs._jobs.clear()
    assert client.get("/api/v1/jobs/running", params={"project": project}).json() == {"job": None}


def test_run_rejects_unsupported_document(client, monkeypatch, tmp_path):
    from app.core import config

    monkeypatch.setattr(jobs, "start_run", lambda **kw: pytest.fail("지원하지 않는 형식은 실행하지 않는다"))
    config._settings = config.Settings(output_dir=tmp_path / "output", data_dir=tmp_path / "data", validation_db_url="")
    r = client.post("/api/v1/runs", files={"file": ("req.hwp", b"x", "application/octet-stream")})
    assert r.status_code == 400 and "PDF·DOCX·HWPX만" in r.json()["detail"]
    # 웹 업로드는 PDF·DOCX·HWPX만: TXT·이미지 파일, 글자가 없는(이미지로 된) DOCX, 깨진 DOCX는 막는다
    for files, said in ((docx_upload(name="req.txt"), "PDF·DOCX·HWPX만"), ({"file": ("scan.png", b"x", "image/png")}, "스캔본·이미지"),
                        (docx_upload(""), "글자가 거의 없는 문서"), ({"file": ("bad.docx", b"not a zip", "application/octet-stream")}, "문서를 열지 못했습니다")):
        r = client.post("/api/v1/runs", files=files)
        assert r.status_code == 400 and said in r.json()["detail"], r.text
    uploads = tmp_path / "data" / "uploads"
    assert not uploads.exists() or not list(uploads.iterdir()), "거부한 문서는 남기지 않는다"


def test_upload_run_stops_at_function_spec_and_review_runs_review(monkeypatch, tmp_path):
    """업로드는 기능 명세 초안에서 멈추고, ERD 최종 확정은 review 명령 (작업 시작 전 인자만 확인)"""
    seen = {}
    monkeypatch.setattr(jobs._Runner, "start", lambda self, args: seen.update(args=args, runner=self))
    jobs.start_run(output_dir=tmp_path, document=tmp_path / "a.pdf", project="p", model="claude-opus-5-5", effort="low")
    args = seen["args"]
    assert args[args.index("--until") + 1] == "function_designer"
    assert args[args.index("--model") + 1] == "claude-opus-5-5" and args[args.index("--effort") + 1] == "low"
    assert "--validate-db" not in args, "DDL 검증은 항상 (기본 켬)"
    job = jobs.start_review(output_dir=tmp_path, project="team/p", version="1.1")
    assert job.source == "review" and seen["args"][:3] == ["review", "p", "v1.1"]
    assert seen["runner"].result == "semantic_review.json"
    jobs._jobs.clear()


def test_review_request_needs_erd(client, project, monkeypatch):
    monkeypatch.setattr(jobs, "start_review", lambda **kw: jobs.Job(id="job-x", project=kw["project"], base=kw["version"],
                                                                  source="review", status="running", message=""))
    assert client.post("/api/v1/projects/review", json={"project": project, "version": "1"}).status_code == 202
    assert client.post("/api/v1/projects/review", json={"project": project, "version": "1.1"}).status_code == 400
    assert client.post("/api/v1/projects/review", json={"project": project, "version": "9"}).status_code == 404
    assert client.get(f"/api/v1/file?project={project}&version=1&name=semantic_review.json").status_code == 404


def test_rerun_args_follow_screen_request(monkeypatch, tmp_path):
    """기능 명세 화면의 답 → rerun function_designer --answers, 수정 요약은 --feedback (작업 시작 전 인자만 확인)"""
    seen = {}
    monkeypatch.setattr(jobs._Runner, "start", lambda self, args: seen.update(args=args, runner=self))
    (tmp_path / "p" / "v2").mkdir(parents=True)
    job = jobs.start_rerun(output_dir=tmp_path, project="p", base="2", source="spec", answers={"CF-001": "A안"},
                           spec_summary="FN-1 이름 변경")
    args = seen["args"]
    assert job.source == "answers" and args[:5] == ["rerun", "function_designer", "p", "--from", "v2"]
    assert "--answers" in args and args[args.index("--feedback") + 1] == "FN-1 이름 변경"
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    # 사람이 고친 기능은 파일로 넘겨 고정하고, 변경 요약은 의견으로 보내지 않는다
    jobs.start_rerun(output_dir=tmp_path, project="p", base="2", source="answers", functions=[{"function_id": "FN-1"}],
                     spec_summary="FN-1 이름 변경")
    assert "--functions" in seen["args"] and "--feedback" not in seen["args"]
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    jobs._jobs.clear()
    job = jobs.start_rerun(output_dir=tmp_path, project="p", base="2", source="spec")
    assert seen["args"][1] == "data_modeler" and (seen["args"][-1] == "--full" or jobs.capabilities()["design"])
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    jobs._jobs.clear()


def test_confirmed_rules_follow_the_screen_request(monkeypatch, tmp_path):
    """'추천안 맞음'으로 확인한 규칙: 'AI로 답 반영'이면 --confirm-rules, 내보내기면 확정한 design.json의 confirm_rules (LLM 없이 '(결정)')"""
    import json

    seen = {}
    monkeypatch.setattr(jobs._Runner, "start", lambda self, args: seen.update(args=args, runner=self))
    (tmp_path / "p" / "v2").mkdir(parents=True)
    rules = [{"function_id": "FN-1", "rule": "(확인 필요) 기한은 7일"}]
    job = jobs.start_rerun(output_dir=tmp_path, project="p", base="2", source="spec", answers={"FQ-001": "예"},
                           confirm_rules=rules)
    args = seen["args"]
    assert job.source == "answers" and "추천안 확인 1건" in job.message
    assert json.loads(Path(args[args.index("--confirm-rules") + 1]).read_text(encoding="utf-8")) == rules
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    jobs.start_rerun(output_dir=tmp_path, project="p", base="2", source="spec", design={"functions": []},
                     confirm_rules=rules)
    args = seen["args"]
    assert args[1] == "data_modeler" and "--confirm-rules" not in args
    assert json.loads(Path(args[args.index("--design") + 1]).read_text(encoding="utf-8"))["confirm_rules"] == rules
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    jobs._jobs.clear()


def test_spec_format_and_dev_spec_files_are_served(client, project):
    r = client.get("/api/v1/spec-format")
    assert r.status_code == 200 and r.text.startswith("# 개발 스펙 문서 형식 (spec-format v2)")
    assert client.get(f"/api/v1/file?project={project}&version=1&name=openapi.yaml").status_code == 404, \
        "허용된 이름이지만 ERD 버전에는 없다"
    assert client.get(f"/api/v1/file?project={project}&version=1&name=secret.yaml").status_code in (400, 404)


def test_dispositions_and_spec_redo_follow_the_screen_request(monkeypatch, tmp_path):
    """ERD 화면에서 정한 검토 지적 처리: ERD 저장·최종 확정 모두 --dispositions 파일로. 확정한 기능 명세의 '다시 만들기'는
    답 없이 의견만으로 rerun function_designer"""
    import json

    seen = {}
    monkeypatch.setattr(jobs._Runner, "start", lambda self, args: seen.update(args=args, runner=self))
    (tmp_path / "p" / "v2").mkdir(parents=True)
    choices = {"ISS-0001": {"choice": "not_issue", "note": "두 경로라 정상"}}
    jobs.start_rerun(output_dir=tmp_path, project="p", base="2.1", source="erd", data_model={"entities": []},
                     dispositions=choices)
    args = seen["args"]
    assert args[1] == "data_modeler" and json.loads(Path(args[args.index("--dispositions") + 1]).read_text(encoding="utf-8")) == choices
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    jobs.start_review(output_dir=tmp_path, project="p", version="2.1", dispositions=choices)
    args = seen["args"]
    assert args[:3] == ["review", "p", "v2.1"]
    assert json.loads(Path(args[args.index("--dispositions") + 1]).read_text(encoding="utf-8")) == choices
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    job = jobs.start_rerun(output_dir=tmp_path, project="p", base="2", source="answers", feedback="신청 취소 기능 추가")
    args = seen["args"]
    assert job.source == "answers" and args[1] == "function_designer" and "--answers" not in args
    assert args[args.index("--feedback") + 1] == "신청 취소 기능 추가" and "새 기능 명세 버전" in job.message
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    jobs._jobs.clear()


def test_llm_options_list_models_of_providers_with_keys(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    body = client.get("/api/v1/llm-options").json()
    ids = [m["id"] for m in body["models"]]
    assert "claude-haiku-5-5" in ids and all(m["provider"] == "anthropic" for m in body["models"])
    assert next(m for m in body["models"] if m["id"] == "claude-haiku-4-5")["effort"] is False, "Haiku 4.5는 effort 없음"
    assert body["efforts"] == ["low", "medium", "high"]
    files = docx_upload()
    assert client.post("/api/v1/runs", files=files, data={"model": "gpt-unknown"}).status_code == 400
    assert client.post("/api/v1/runs", files=files, data={"effort": "max"}).status_code == 400


def test_signup_login_and_admin_approval():
    """처음 가입자는 관리자·PM, 그 뒤는 승인 대기 → 관리자가 역할을 정해 승인해야 로그인된다."""
    anon = TestClient(app)
    assert anon.get("/api/v1/projects").status_code == 401, "로그인 없이 API를 쓰지 못한다"
    first = anon.post("/api/v1/auth/signup", json={"email": "Boss@Co.kr", "name": "PM", "password": "password123"}).json()
    assert (first["user"]["role"], first["user"]["is_admin"], first["pending"]) == ("pm", True, False)
    r = anon.post("/api/v1/auth/signup", json={"email": "p@co.kr", "name": "개발", "password": "password123",
                                               "requested_role": "developer"})
    assert r.json()["pending"] and anon.post("/api/v1/auth/login", json={"email": "p@co.kr", "password": "password123"}).status_code == 403
    assert anon.post("/api/v1/auth/signup", json={"email": "p@co.kr", "name": "x", "password": "password123"}).status_code == 409
    assert anon.post("/api/v1/auth/signup", json={"email": "q@co.kr", "name": "x", "password": "password123",
                                                  "requested_role": "planner"}).status_code == 400, "예전 역할은 고를 수 없다"
    assert anon.post("/api/v1/auth/login", json={"email": "boss@co.kr", "password": "wrong-pass"}).status_code == 401

    admin = TestClient(app)
    assert admin.post("/api/v1/auth/login", json={"email": "boss@co.kr", "password": "password123"}).status_code == 200
    dev = next(u for u in admin.get("/api/v1/users").json()["users"] if u["email"] == "p@co.kr")
    assert dev["status"] == "pending" and dev["requested_role"] == "developer"
    assert admin.post("/api/v1/users/update", json={"id": dev["id"], "status": "active"}).status_code == 400, "역할 먼저"
    assert admin.post("/api/v1/users/update", json={"id": dev["id"], "role": "developer", "status": "active"}).status_code == 200
    me = anon.post("/api/v1/auth/login", json={"email": "p@co.kr", "password": "password123"})
    assert me.status_code == 200 and me.json()["user"]["permissions"] == [
        "upload", "spec_edit", "erd_edit", "erd_confirm", "final_decide", "develop", "preview_stop", "repo_write", "app_accounts"], \
        "개발자: 기능 명세 확정·담당자·프로젝트 관리 밖의 모든 일 (최종 승인·수정 지시도)"
    assert me.json()["user"]["role_label"] == "개발자"
    assert anon.get("/api/v1/users").status_code == 403, "회원 관리는 관리자만"
    boss = next(u for u in admin.get("/api/v1/users").json()["users"] if u["is_admin"])
    assert admin.post("/api/v1/users/update", json={"id": boss["id"], "is_admin": False}).status_code == 400, "관리자 한 명은 남는다"
    anon.post("/api/v1/auth/logout")
    assert anon.get("/api/v1/auth/me").status_code == 401


def test_roles_limit_who_confirms_each_stage(project, monkeypatch):
    """기능 명세 확정(내보내기)은 PM만 (개발자는 PM에게 승인 요청), ERD 저장·최종 확정은 개발자·PM이 승인 요청 없이,
    ERD는 승인 요청을 받지 않는다. 최종 결정은 PM."""
    from app.agents.tools.versions import save_version

    monkeypatch.setattr(jobs, "start_rerun", lambda **kw: jobs.Job(id="j", project="team/demo", base="1", source="spec",
                                                                    status="running", message=""))
    monkeypatch.setattr(jobs, "start_review", lambda **kw: jobs.Job(id="r", project="team/demo", base="1", source="review",
                                                                    status="running", message=""))
    spec = {"project": "team/demo", "base": "1", "source": "spec"}
    dev, pm = signed_in_client("developer"), signed_in_client("pm")
    r = dev.post("/api/v1/projects/versions", json=spec)
    assert r.status_code == 403 and "PM만" in r.json()["detail"] and "승인 요청" in r.json()["detail"]
    assert dev.post("/api/v1/projects/versions", json={**spec, "source": "answers"}).status_code == 202, "수정은 개발자도"
    assert dev.post("/api/v1/drafts/request", json={"project": "team/demo", "version": "1"}).status_code == 200, "개발자는 PM에게 승인 요청"
    assert pm.post("/api/v1/projects/versions", json=spec).status_code == 202
    assert pm.post("/api/v1/projects/versions", json=spec).status_code == 202, "PM은 승인 요청이 없어도 확정한다"
    assert dev.post("/api/v1/projects/versions", json={**spec, "source": "erd"}).status_code == 202, "ERD 저장은 개발자"
    assert dev.post("/api/v1/projects/review", json={"project": "team/demo", "version": "1"}).status_code == 202, \
        "ERD 최종 확정은 개발자가 승인 요청 없이"
    assert pm.post("/api/v1/projects/review", json={"project": "team/demo", "version": "1"}).status_code == 202
    save_version("team/demo", {"design_version": "1.1", "stage": "data_modeler", "base": "1", "parent": "1"})
    r = dev.post("/api/v1/drafts/request", json={"project": "team/demo", "version": "1.1"})
    assert r.status_code == 400 and "승인 요청 없이" in r.json()["detail"], "ERD는 승인 요청을 받지 않는다"
    assert dev.get("/api/v1/projects").status_code == 200, "보기는 누구나"


def test_shared_draft_and_final_decision(project):
    """수정 초안은 서버에 버전별로 공유하고, 늦게 저장하면 409. 개발자는 기능 명세·ERD 모두 고친다. 최종본 승인·수정 지시는 PM·개발자."""
    from app.agents.tools.versions import find_version, save_version

    a, b = signed_in_client("developer", "dev_a"), signed_in_client("developer", "dev_b")
    empty = a.get("/api/v1/drafts?project=team/demo&version=1").json()
    assert (empty["state"], empty["rev"], empty["log"], empty["request"]) == (None, 0, [], None)
    saved = a.post("/api/v1/drafts", json={"project": "team/demo", "version": "1", "state": {"functions": {"F": 1}}, "rev": 0})
    assert saved.status_code == 200 and saved.json()["rev"] == 1
    got = b.get("/api/v1/drafts?project=team/demo&version=1").json()
    assert got["state"] == {"functions": {"F": 1}} and got["updated_by"]["name"] == "dev_a", "다른 개발자가 같은 초안을 본다"
    late = b.post("/api/v1/drafts", json={"project": "team/demo", "version": "1", "state": {}, "rev": 0})
    assert late.status_code == 409 and "dev_a님이 먼저 저장" in late.json()["detail"]
    save_version("team/demo", {"design_version": "1.1", "stage": "data_modeler", "base": "1", "parent": "1"})
    assert b.post("/api/v1/drafts", json={"project": "team/demo", "version": "1.1", "state": {}, "rev": 0}).status_code == 200
    assert a.post("/api/v1/drafts", json={"project": "team/demo", "version": "1.1", "state": {}, "rev": 1}).status_code == 200, \
        "기획·데이터를 나누지 않는다"

    save_version("team/demo", {"design_version": "1.1", "stage": "final", "base": "1", "parent": "1"})
    body = {"project": "team/demo", "version": "1.1", "decision": "changes", "target": "erd", "comment": "결재선 테이블 추가"}
    assert b.post("/api/v1/projects/approval", json={**body, "comment": ""}).status_code == 400
    assert b.post("/api/v1/projects/approval", json=body).status_code == 200, "개발자도 수정 지시"
    pm = signed_in_client()
    assert pm.post("/api/v1/projects/approval", json={**body, "decision": "approve"}).status_code == 200
    assert b.post("/api/v1/projects/approval", json={**body, "decision": "approve"}).status_code == 400, "두 번 승인하지 않는다"
    record = find_version("team/demo", "1.1")
    assert record["approval"]["decision"] == "approve" and [x["decision"] for x in record["approvals"]] == ["changes", "approve"]
    assert pm.post("/api/v1/projects/approval", json={**body, "version": "1"}).status_code == 400, "최종본에만"


def test_admin_creates_accounts_and_resets_passwords():
    """관리자 페이지: 역할을 정한 계정을 바로 만들고(승인된 상태), 비밀번호를 초기화한다 (관리자만)."""
    admin = signed_in_client()
    r = admin.post("/api/v1/users/create", json={"email": "d1@co.kr", "name": "개발1", "password": "password123", "role": "developer"})
    assert r.status_code == 200 and (r.json()["user"]["status"], r.json()["user"]["role"]) == ("active", "developer")
    user = TestClient(app)
    assert user.post("/api/v1/auth/login", json={"email": "d1@co.kr", "password": "password123"}).status_code == 200
    assert user.post("/api/v1/users/create", json={"email": "x@co.kr", "name": "x", "password": "password123", "role": "pm"}).status_code == 403
    uid = r.json()["user"]["id"]
    assert admin.post("/api/v1/users/password", json={"id": uid, "password": "short"}).status_code == 400
    assert admin.post("/api/v1/users/password", json={"id": uid, "password": "newpassword1"}).status_code == 200
    assert user.get("/api/v1/auth/me").status_code == 401, "초기화하면 기존 로그인은 끊긴다"
    assert user.post("/api/v1/auth/login", json={"email": "d1@co.kr", "password": "newpassword1"}).status_code == 200


def test_upload_project_name_matches_the_cli_folder_name(monkeypatch, tmp_path):
    """'요구사항 test'처럼 공백이 있는 이름: 작업이 CLI가 저장하는 이름(요구사항_test)으로 새 버전을 찾는다"""
    seen = {}
    monkeypatch.setattr(jobs, "start_run", lambda **kw: seen.update(kw) or jobs.Job(id="j", project=kw["project"], base="",
                                                                                     source="run", status="running", message=""))
    r = signed_in_client().post("/api/v1/runs", files=docx_upload(), data={"project": "요구사항 test"})
    assert r.status_code == 202 and seen["project"] == "요구사항_test"


def test_change_log_shows_who_changed_what_and_member_edits_cancel_the_request(project):
    """변경 내역: 누가·무엇을 고쳤는지 서버가 남긴다. 개발자가 요청한 뒤 다시 고치면 요청이 취소된다 (PM이 고치면 그대로)."""
    dev, pm = signed_in_client("developer", "dev_a"), signed_in_client("pm")
    fn = {"function_id": "FN-1", "name": "회원가입", "business_rules": ["a"], "description": "d"}
    state = {"functions": {"FN-1": fn}, "answers": {"FQ-001": "3단계"}}
    r = dev.post("/api/v1/drafts", json={"project": "team/demo", "version": "1", "state": state, "rev": 0}).json()
    entry = r["log"][-1]
    assert entry["by"]["name"] == "dev_a"
    assert {(c["target"], c["what"]) for c in entry["changes"]} >= {("FN-1", "수정"), ("FQ-001", "답: 3단계")}
    state2 = {**state, "functions": {"FN-1": {**fn, "business_rules": ["a", "b"]}}}
    r = dev.post("/api/v1/drafts", json={"project": "team/demo", "version": "1", "state": state2, "rev": 1}).json()
    assert len(r["log"]) == 1 and any(c["what"] == "업무 규칙 수정" for c in r["log"][-1]["changes"]), "이어 고치면 한 묶음"
    assert dev.post("/api/v1/drafts/request", json={"project": "team/demo", "version": "1", "note": "검토 부탁"}).json()["request"]["note"] == "검토 부탁"
    r = pm.post("/api/v1/drafts", json={"project": "team/demo", "version": "1", "state": {**state2, "checked": {"x": True}}, "rev": 2}).json()
    assert r["request"] is not None, "PM이 고치는 것은 요청을 취소하지 않는다"
    r = dev.post("/api/v1/drafts", json={"project": "team/demo", "version": "1", "state": state, "rev": 3}).json()
    assert r["request"] is None and r["log"][-1]["event"] == "request_cancelled"


def test_change_log_text_names_fields_and_columns():
    from app.services.drafts import diff

    base = {"functions": {"FN-1": {"function_id": "FN-1", "name": "가입", "business_rules": ["a"], "description": "d"}},
            "entities": {"t": {"entity_id": "t", "label": "표", "attributes": [{"name": "id"}, {"name": "x", "nullable": True}]}}}
    new = {"functions": {"FN-1": {**base["functions"]["FN-1"], "business_rules": ["a", "b"], "name": "회원 가입"}},
           "entities": {"t": {**base["entities"]["t"], "attributes": [{"name": "id"}, {"name": "x", "nullable": False}, {"name": "memo"}]}},
           "dispositions": {"ISS-1": {"choice": "not_issue", "note": "앱에서 처리"}}}
    got = {c["target"]: c["what"] for c in diff({}, new, base)}
    assert got == {"FN-1": "이름 수정 · 업무 규칙 수정", "t": "컬럼 추가 memo · 컬럼 수정 x", "ISS-1": "지적 처리: 문제 아님 (앱에서 처리)"}
    assert diff(new, {}, base)[0]["what"] == "원본으로 되돌림"
    checks = diff({"checked": {"i:SI-1": True}}, {"checked": {"a:FN-1:한도는 3회 (가정)": True}})
    assert {c["target"]: c["what"] for c in checks} == {"a:FN-1:한도는 3회 (가정)": "확인함 — FN-1 규칙: 한도는 3회 (가정)",
                                                       "i:SI-1": "확인 해제 — 지적 SI-1"}, "확인 표시는 항목별로 남긴다"


def test_change_request_can_send_both_stages_with_items(project):
    """수정 지시: 기능 명세·ERD 둘 다, 고른 기능·테이블마다 메모. 항목만 골라도 그 단계로 돌려보낸다."""
    from app.agents.tools.versions import find_version, save_version

    save_version("team/demo", {"design_version": "1.1", "stage": "final", "base": "1", "parent": "1"})
    pm = signed_in_client()
    body = {"project": "team/demo", "version": "1.1", "decision": "changes", "comment": "",
            "items": [{"stage": "spec", "target_id": "FN-3", "label": "결재", "note": "결재선 3단계"},
                      {"stage": "erd", "target_id": "budget", "note": "집행일 컬럼 추가"}]}
    assert pm.post("/api/v1/projects/approval", json=body).status_code == 200
    got = find_version("team/demo", "1.1")["approval"]
    assert got["targets"] == ["spec", "erd"] and [i["target_id"] for i in got["items"]] == ["FN-3", "budget"]
    assert pm.post("/api/v1/projects/approval", json={**body, "items": []}).status_code == 400, "단계도 내용도 없음"
    one = pm.post("/api/v1/projects/approval", json={**body, "items": [], "targets": ["erd"], "comment": "전체 검토"})
    assert one.status_code == 200 and one.json()["approval"]["target"] == "erd"


def test_spec_first_directive_locks_old_erd_and_moves_erd_items_to_the_new_erd(project, monkeypatch):
    """기능 명세+ERD 수정 지시: 기능 명세 먼저 → 옛 ERD 잠금 → 새 기능 명세 v2의 ERD v2.1에서 ERD 항목 처리(필수, LLM 없음) → 새 최종본이면 끝.
    지시 알림은 개발자에게 기능 명세·ERD 모두 간다 (파트를 나누지 않는다)."""
    from app.agents.tools.versions import save_version
    from app.services import directives

    monkeypatch.setattr(jobs, "start_rerun", lambda **kw: jobs.Job(id="j", project="team/demo", base="1", source="erd",
                                                                    status="running", message=""))
    def rec(v, stage, parent, at, base=None):
        folder = get_settings().output_dir / "team" / "demo" / f"v{v}"
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / "design.json").exists():
            (folder / "design.json").write_text(json.dumps({"meta": {}, "requirements": []}), encoding="utf-8")
        save_version("team/demo", {"design_version": v, "stage": stage, "parent": parent, "base": base, "created_at": at})
    rec("1", "full", None, "2000-01-01T00:00:00")
    rec("1.1", "data_modeler", "1", "2000-01-01T00:00:01", "1")
    rec("1.1.1", "final", "1.1", "2000-01-01T00:00:02", "1.1")
    pm, dev = signed_in_client(), signed_in_client("developer", "dev_b")
    body = {"project": "team/demo", "version": "1.1.1", "decision": "changes", "comment": "",
            "items": [{"stage": "spec", "target_id": "FN-3", "label": "결재", "note": "3단계"},
                      {"stage": "erd", "target_id": "budget", "label": "예산", "note": "집행일 컬럼"}]}
    assert pm.post("/api/v1/projects/approval", json=body).status_code == 200

    d = dev.get("/api/v1/projects/directives?project=team/demo").json()["directives"][0]
    assert (d["spec_done"], d["erd_waiting"], d["erd_version"]) == (False, True, None)
    r = dev.post("/api/v1/projects/versions", json={"project": "team/demo", "base": "1.1", "source": "erd"})
    assert r.status_code == 403 and "잠겼습니다" in r.json()["detail"], "기능 명세가 고쳐질 때까지 옛 ERD는 잠금"
    kinds = {(n["kind"], n["page"]) for n in dev.get("/api/v1/notices").json()["notices"]}
    assert ("waiting", "erd") in kinds and ("directive", "spec") in kinds, "개발자는 두 단계 지시를 모두 받는다"

    rec("2", "function_designer", "1", "2999-01-01T00:00:00")   # 개발자가 고치고 PM이 내보냄 → v2, v2.1
    rec("2.1", "data_modeler", "2", "2999-01-01T00:00:01", "2")
    d = directives.open_directives("team/demo")[0]
    assert (d["spec_done"], d["erd_waiting"], d["erd_version"], d["new_spec"]) == (True, False, "2.1", "2")
    assert directives.locked("team/demo", "2.1") == "" and directives.locked("team/demo", "1.1"), "새 ERD는 풀리고 옛 ERD는 그대로 잠금"
    notice = next(n for n in dev.get("/api/v1/notices").json()["notices"] if n["kind"] == "directive")
    assert (notice["version"], notice["page"]) == ("2.1", "erd") and "미처리 1건" in notice["detail"]

    resolve = {"project": "team/demo", "final": "1.1.1", "stage": "erd", "target_id": "budget", "choice": "kept", "note": ""}
    assert dev.post("/api/v1/projects/directives/resolve", json=resolve).status_code == 400, "그대로 둠은 이유 필수"
    assert dev.post("/api/v1/projects/directives/resolve", json={**resolve, "choice": "applied"}).status_code == 200
    item = next(i for i in directives.open_directives("team/demo")[0]["items"] if i["target_id"] == "budget")
    assert item["resolution"]["choice"] == "applied" and item["resolution"]["by"]["name"] == "dev_b"

    rec("2.1.1", "final", "2.1", "2999-01-01T00:00:02", "2.1")
    assert directives.open_directives("team/demo") == [], "새 최종본이 나오면 지시는 끝"
    assert any(n["kind"] == "decide" and n["version"] == "2.1.1" for n in pm.get("/api/v1/notices").json()["notices"])


def test_temporary_test_accounts_are_seeded_and_missing_ones_added(capsys):
    """[임시] 새 PC(회원 없음)에 시연용 계정 8개 (PM 2·개발자 6). 서버 시작 때(only_if_empty=False)는 빠진 계정만 더한다."""
    from app import cli
    from app.services import users

    assert len(users.seed_test_accounts()) == len(users.TEST_ACCOUNTS) == 8
    roles = {m.email: (m.role, m.is_admin, m.status) for m in users.list_members()}
    assert roles["pm1@team4.local"] == ("pm", True, "active") and roles["pm2@team4.local"] == ("pm", False, "active")
    assert [e for e, (r, *_) in roles.items() if r == "developer"] == [f"developer{i}@team4.local" for i in range(1, 7)]
    assert users.seed_test_accounts() == [], "회원이 있으면 만들지 않는다"
    users.update_member(next(m.id for m in users.list_members() if m.email == "developer6@team4.local"), status="disabled")
    assert users.seed_test_accounts(only_if_empty=False) == [], "있는 계정(중지 포함)은 건드리지 않는다"
    assert TestClient(app).post("/api/v1/auth/login", json={"email": "developer1@team4.local", "password": users.TEST_PASSWORD}).status_code == 200
    assert cli.main(["seed-accounts"]) == 0 and "이미 있음" in capsys.readouterr().out


def test_old_test_accounts_are_retired_once_new_ones_exist():
    """팀장·파트장·파트원 시절의 시연 계정은 새 시연 계정을 만든 뒤 사용 중지한다 (지우지 않음, 관리자는 새 PM1로 넘어감)."""
    from app.services import users

    users.signup("admin@team4.local", "김관리", "password123")                     # 예전 관리자 (처음 가입자)
    users.create_member("plan1@team4.local", "박기획", "password123", "developer")
    users.create_member("someone@co.kr", "다른 회원", "password123", "developer")
    made = users.seed_test_accounts(only_if_empty=False)
    assert len(made) == 8
    status = {m.email: (m.status, m.is_admin) for m in users.list_members()}
    assert status["admin@team4.local"] == ("disabled", False) and status["plan1@team4.local"] == ("disabled", False)
    assert status["pm1@team4.local"] == ("active", True) and status["someone@co.kr"] == ("active", False), "다른 회원은 그대로"


def test_pm_renames_a_project_and_only_the_shown_name_changes(project):
    """프로젝트 이름 바꾸기 (PM만): 목록·Slack에 보이는 이름만 바뀌고 프로젝트 ID·주소는 그대로. 비우면 처음 이름."""
    from app.agents.tools.versions import project_info, save_version

    other = get_settings().output_dir / "other" / "v1"
    other.mkdir(parents=True)
    (other / "design.json").write_text(json.dumps({"meta": {}, "requirements": []}), encoding="utf-8")
    save_version("other", {"design_version": "1", "stage": "function_designer"})
    pm, dev = signed_in_client(), signed_in_client("developer")
    body = {"project": "team/demo", "name": "  새  회사 포털 "}
    r = dev.post("/api/v1/projects/name", json=body)
    assert r.status_code == 403 and "PM만" in r.json()["detail"]
    r = pm.post("/api/v1/projects/name", json=body)
    assert r.status_code == 200 and r.json() == {"project": "team/demo", "name": "새 회사 포털"}, "공백은 하나로"
    names = {p["id"]: p["name"] for p in dev.get("/api/v1/projects").json()["projects"]}
    assert names == {"team/demo": "새 회사 포털", "other": "other"}, "ID는 그대로, 보이는 이름만"
    assert dev.get("/api/v1/design?project=team/demo&version=1").status_code == 200, "주소(ID)는 그대로 쓴다"
    assert pm.post("/api/v1/projects/name", json={**body, "name": "x" * 81}).status_code == 400
    r = pm.post("/api/v1/projects/name", json={"project": "other", "name": "새 회사 포털"})
    assert r.status_code == 409 and "같은 이름" in r.json()["detail"]
    r = pm.post("/api/v1/projects/name", json={"project": "team/demo", "name": ""})
    assert r.json()["name"] == "demo" and "name" not in project_info("team/demo"), "비우면 처음 이름으로"
    assert "project_rename" in pm.get("/api/v1/auth/me").json()["user"]["permissions"]


def test_unmet_completion_is_a_check_not_a_failure():
    """완료 조건 미충족은 실패가 아니라 '필수 확인'으로, 판단 기록의 실패만 실패로 알린다"""
    from app.services.jobs.common import findings

    design = {"decisions": [{"step": 3, "action": "semantic_reviewer", "result": "완료 → 미해결 오류 7건"}],
              "completion": {"checks": [{"name": "원문 근거 연결", "passed": False, "detail": "근거 미확인 2건"},
                                        {"name": "데이터 모델 무결성", "passed": True, "detail": "모델 오류 0건"}]}}
    assert findings(design) == ([], ["원문 근거 연결: 근거 미확인 2건"])
    design["decisions"].append({"step": 4, "action": "data_modeler", "result": "LLM 호출 실패 (재시도 소진)"})
    assert findings(design)[0] == ["#4 data_modeler: LLM 호출 실패 (재시도 소진)"]

