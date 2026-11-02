"""/api/v1: 산출물 폴더를 읽는 API와 작업 API의 입력 검사 (LLM·하위 프로세스 없이)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services import jobs
from conftest import signed_in_client


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
    assert client.post("/api/v1/projects/versions", json={"project": project, "base": "1"}).status_code == 409
    assert client.get("/api/v1/jobs", params={"id": "없음"}).status_code == 404
    jobs._jobs.clear()


def test_run_rejects_unsupported_document(client, monkeypatch, tmp_path):
    from app.core import config

    monkeypatch.setattr(jobs, "start_run", lambda **kw: pytest.fail("지원하지 않는 형식은 실행하지 않는다"))
    config._settings = config.Settings(output_dir=tmp_path / "output", data_dir=tmp_path / "data", validation_db_url="")
    r = client.post("/api/v1/runs", files={"file": ("req.hwp", b"x", "application/octet-stream")})
    assert r.status_code == 400
    assert not list((tmp_path / "data" / "uploads").iterdir()), "거부한 문서는 남기지 않는다"


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
    assert r.status_code == 200 and r.text.startswith("# 개발 스펙 문서 형식 (spec-format v1)")
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
    files = {"file": ("a.txt", b"hello", "text/plain")}
    assert client.post("/api/v1/runs", files=files, data={"model": "gpt-unknown"}).status_code == 400
    assert client.post("/api/v1/runs", files=files, data={"effort": "max"}).status_code == 400


def test_signup_login_and_admin_approval():
    """처음 가입자는 관리자·팀장, 그 뒤는 승인 대기 → 관리자가 역할을 정해 승인해야 로그인된다."""
    anon = TestClient(app)
    assert anon.get("/api/v1/projects").status_code == 401, "로그인 없이 API를 쓰지 못한다"
    first = anon.post("/api/v1/auth/signup", json={"email": "Boss@Co.kr", "name": "팀장", "password": "password123"}).json()
    assert (first["user"]["role"], first["user"]["is_admin"], first["pending"]) == ("lead", True, False)
    r = anon.post("/api/v1/auth/signup", json={"email": "p@co.kr", "name": "기획", "password": "password123",
                                               "requested_role": "planner"})
    assert r.json()["pending"] and anon.post("/api/v1/auth/login", json={"email": "p@co.kr", "password": "password123"}).status_code == 403
    assert anon.post("/api/v1/auth/signup", json={"email": "p@co.kr", "name": "x", "password": "password123"}).status_code == 409
    assert anon.post("/api/v1/auth/login", json={"email": "boss@co.kr", "password": "wrong-pass"}).status_code == 401

    admin = TestClient(app)
    assert admin.post("/api/v1/auth/login", json={"email": "boss@co.kr", "password": "password123"}).status_code == 200
    planner = next(u for u in admin.get("/api/v1/users").json()["users"] if u["email"] == "p@co.kr")
    assert planner["status"] == "pending" and planner["requested_role"] == "planner"
    assert admin.post("/api/v1/users/update", json={"id": planner["id"], "status": "active"}).status_code == 400, "역할 먼저"
    assert admin.post("/api/v1/users/update", json={"id": planner["id"], "role": "planner", "status": "active"}).status_code == 200
    me = anon.post("/api/v1/auth/login", json={"email": "p@co.kr", "password": "password123"})
    assert me.status_code == 200 and me.json()["user"]["permissions"] == ["upload", "spec_edit"]
    assert anon.get("/api/v1/users").status_code == 403, "회원 관리는 관리자만"
    boss = next(u for u in admin.get("/api/v1/users").json()["users"] if u["is_admin"])
    assert admin.post("/api/v1/users/update", json={"id": boss["id"], "is_admin": False}).status_code == 400, "관리자 한 명은 남는다"
    anon.post("/api/v1/auth/logout")
    assert anon.get("/api/v1/auth/me").status_code == 401


def test_roles_limit_who_confirms_each_stage(project, monkeypatch):
    """기능 명세 확정은 기획 파트장, ERD 저장은 데이터 파트, ERD 최종 확정은 데이터 파트장, 최종 결정은 팀장."""
    monkeypatch.setattr(jobs, "start_rerun", lambda **kw: jobs.Job(id="j", project="team/demo", base="1", source="spec",
                                                                    status="running", message=""))
    monkeypatch.setattr(jobs, "start_review", lambda **kw: jobs.Job(id="r", project="team/demo", base="1", source="review",
                                                                    status="running", message=""))
    spec = {"project": "team/demo", "base": "1", "source": "spec"}
    planner, head, data = signed_in_client("planner"), signed_in_client("planner_head"), signed_in_client("data")
    r = planner.post("/api/v1/projects/versions", json=spec)
    assert r.status_code == 403 and "기획 파트장" in r.json()["detail"]
    assert planner.post("/api/v1/projects/versions", json={**spec, "source": "answers"}).status_code == 202, "수정은 파트원도"
    assert head.post("/api/v1/projects/versions", json=spec).status_code == 202
    assert head.post("/api/v1/projects/versions", json={**spec, "source": "erd"}).status_code == 403
    assert data.post("/api/v1/projects/versions", json={**spec, "source": "erd"}).status_code == 202
    assert data.post("/api/v1/projects/review", json={"project": "team/demo", "version": "1"}).status_code == 403
    assert signed_in_client("data_head").post("/api/v1/projects/review", json={"project": "team/demo", "version": "1"}).status_code == 202
    files = {"file": ("a.txt", b"hello", "text/plain")}
    assert data.post("/api/v1/runs", files=files).status_code == 403, "업로드는 기획 파트·팀장"
    assert data.get("/api/v1/projects").status_code == 200, "보기는 누구나"


def test_shared_draft_and_lead_decision(project):
    """수정 초안은 서버에 버전별로 공유하고, 늦게 저장하면 409. 최종본 승인·수정 지시는 팀장만."""
    from app.agents.tools.versions import find_version, save_version

    a, b = signed_in_client("planner"), signed_in_client("planner_head")
    assert a.get("/api/v1/drafts?project=team/demo&version=1").json() == {"state": None, "rev": 0}
    saved = a.post("/api/v1/drafts", json={"project": "team/demo", "version": "1", "state": {"functions": {"F": 1}}, "rev": 0})
    assert saved.status_code == 200 and saved.json()["rev"] == 1
    got = b.get("/api/v1/drafts?project=team/demo&version=1").json()
    assert got["state"] == {"functions": {"F": 1}} and got["updated_by"]["name"] == "planner", "다른 파트원이 같은 초안을 본다"
    late = b.post("/api/v1/drafts", json={"project": "team/demo", "version": "1", "state": {}, "rev": 0})
    assert late.status_code == 409 and "planner님이 먼저 저장" in late.json()["detail"]
    data = signed_in_client("data")
    save_version("team/demo", {"design_version": "1.1", "stage": "data_modeler", "base": "1", "parent": "1"})
    assert data.post("/api/v1/drafts", json={"project": "team/demo", "version": "1.1", "state": {}, "rev": 0}).status_code == 200
    assert a.post("/api/v1/drafts", json={"project": "team/demo", "version": "1.1", "state": {}, "rev": 1}).status_code == 403

    save_version("team/demo", {"design_version": "1.1", "stage": "final", "base": "1", "parent": "1"})
    body = {"project": "team/demo", "version": "1.1", "decision": "changes", "target": "erd", "comment": "결재선 테이블 추가"}
    assert b.post("/api/v1/projects/approval", json=body).status_code == 403
    lead = signed_in_client()
    assert lead.post("/api/v1/projects/approval", json={**body, "comment": ""}).status_code == 400
    assert lead.post("/api/v1/projects/approval", json=body).status_code == 200
    assert lead.post("/api/v1/projects/approval", json={**body, "decision": "approve"}).status_code == 200
    record = find_version("team/demo", "1.1")
    assert record["approval"]["decision"] == "approve" and [x["decision"] for x in record["approvals"]] == ["changes", "approve"]
    assert lead.post("/api/v1/projects/approval", json={**body, "version": "1"}).status_code == 400, "최종본에만"


def test_admin_creates_accounts_and_resets_passwords():
    """관리자 페이지: 역할을 정한 계정을 바로 만들고(승인된 상태), 비밀번호를 초기화한다 (관리자만)."""
    admin = signed_in_client()
    r = admin.post("/api/v1/users/create", json={"email": "d1@co.kr", "name": "데이터1", "password": "password123", "role": "data"})
    assert r.status_code == 200 and (r.json()["user"]["status"], r.json()["user"]["role"]) == ("active", "data")
    user = TestClient(app)
    assert user.post("/api/v1/auth/login", json={"email": "d1@co.kr", "password": "password123"}).status_code == 200
    assert user.post("/api/v1/users/create", json={"email": "x@co.kr", "name": "x", "password": "password123", "role": "lead"}).status_code == 403
    uid = r.json()["user"]["id"]
    assert admin.post("/api/v1/users/password", json={"id": uid, "password": "short"}).status_code == 400
    assert admin.post("/api/v1/users/password", json={"id": uid, "password": "newpassword1"}).status_code == 200
    assert user.get("/api/v1/auth/me").status_code == 401, "초기화하면 기존 로그인은 끊긴다"
    assert user.post("/api/v1/auth/login", json={"email": "d1@co.kr", "password": "newpassword1"}).status_code == 200
