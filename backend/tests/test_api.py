"""/api/v1: 산출물 폴더를 읽는 API와 작업 API의 입력 검사 (LLM·하위 프로세스 없이)."""
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services import jobs


@pytest.fixture
def client():
    return TestClient(app)


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
    jobs.start_run(output_dir=tmp_path, document=tmp_path / "a.pdf", project="p")
    args = seen["args"]
    assert args[args.index("--until") + 1] == "function_designer"
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
    job = jobs.start_rerun(output_dir=tmp_path, project="p", base="2", source="spec")
    assert seen["args"][1] == "data_modeler" and (seen["args"][-1] == "--full" or jobs.capabilities()["design"])
    for f in seen["runner"].temp:
        f.unlink(missing_ok=True)
    jobs._jobs.clear()
