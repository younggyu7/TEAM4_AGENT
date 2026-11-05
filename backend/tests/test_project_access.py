"""프로젝트 담당자 (services/project_access.py): 담당자로 고른 회원만 그 프로젝트를 보고, 팀장은 모든 프로젝트를 본다.
담당자가 아니면 목록·알림에서 빠지고 API는 없는 프로젝트처럼 404. 담당자는 업로드할 때 정하고 팀장만 바꾼다."""
import json

import pytest

from app.agents.tools.versions import save_version
from app.core.config import get_settings
from app.services import directives, jobs, project_access, users
from conftest import docx_upload, signed_in_client

pytestmark = pytest.mark.assignees   # 담당자 규칙을 그대로 둔다 (conftest)


def _project(project: str) -> None:
    """기능 명세 v1 하나 있는 프로젝트 (산출물 폴더 + 버전 기록)"""
    folder = get_settings().output_dir / project / "v1"
    folder.mkdir(parents=True)
    (folder / "design.json").write_text(json.dumps({"meta": {}, "requirements": []}), encoding="utf-8")
    (folder / "schema.sql").write_text("CREATE TABLE t (id bigint);", encoding="utf-8")
    save_version(project, {"design_version": "1", "stage": "function_designer", "created_at": "2026-11-05T10:00:00"})


def _member(email: str) -> users.Member:
    return next(m for m in users.list_members() if m.email == email)


def _ids(client) -> list[str]:
    return sorted(p["id"] for p in client.get("/api/v1/projects").json()["projects"])


@pytest.fixture
def team(monkeypatch):
    """팀장·기획 파트원·데이터 파트원과 프로젝트 둘 (one: 기획 파트원만 담당자, two: 담당자 없음 = CLI로 만든 프로젝트)"""
    monkeypatch.setattr(jobs, "start_run", lambda **kw: jobs.Job(id="j", project=kw["project"], base="", source="run",
                                                                 status="running", message=""))
    clients = {r: signed_in_client(r) for r in ("lead", "planner", "data")}
    _project("one")
    _project("two")
    project_access.set_members("one", [_member("planner@test.local").id])
    return clients


def test_members_see_only_their_projects_and_the_lead_sees_all(team):
    assert _ids(team["lead"]) == ["one", "two"], "팀장은 모든 프로젝트"
    assert _ids(team["planner"]) == ["one"], "담당자인 프로젝트만"
    assert _ids(team["data"]) == [], "담당자가 아니면 목록에 없다"
    assert team["planner"].get("/api/v1/design?project=one&version=1").status_code == 200


@pytest.mark.parametrize("call", [
    ("get", "/api/v1/design?project=two&version=1"),
    ("get", "/api/v1/file?project=two&version=1&name=schema.sql"),
    ("get", "/api/v1/drafts?project=two&version=1"),
    ("post", "/api/v1/drafts", {"project": "two", "version": "1", "state": {}, "rev": 0}),
    ("post", "/api/v1/drafts/request", {"project": "two", "version": "1"}),
    ("get", "/api/v1/projects/directives?project=two"),
    ("get", "/api/v1/projects/members?project=two"),
    ("post", "/api/v1/projects/title", {"project": "two", "version": "1", "title": "x"}),
    ("get", "/api/v1/develop/repo?project=two"),
    ("get", "/api/v1/preview/two/status"),
])
def test_other_projects_answer_as_if_they_did_not_exist(team, call):
    method, url, *body = call
    r = getattr(team["planner"], method)(url, **({"json": body[0]} if body else {}))
    assert r.status_code == 404 and "담당자가 아닙니다" in r.json()["detail"], (url, r.status_code, r.text)


def test_notices_and_jobs_of_other_projects_are_hidden(team):
    for project in ("one", "two"):
        directives.add_event(project, "uploaded", "분석 완료", "", "spec", "1", directives.EVERYONE)
    jobs._jobs["job-two"] = jobs.Job(id="job-two", project="two", base="", source="run", status="running", message="")
    try:
        notices = lambda c: sorted({n["project"] for n in c.get("/api/v1/notices").json()["notices"]})   # noqa: E731
        assert notices(team["planner"]) == ["one"] and notices(team["lead"]) == ["one", "two"]
        assert notices(team["data"]) == []
        assert team["planner"].get("/api/v1/jobs?id=job-two").status_code == 404
        assert team["lead"].get("/api/v1/jobs?id=job-two").status_code == 200
    finally:
        jobs._jobs.pop("job-two", None)


def test_upload_makes_the_uploader_and_the_chosen_members_assignees(team):
    planner, data = _member("planner@test.local"), _member("data@test.local")
    r = team["planner"].post("/api/v1/runs", files=docx_upload(), data={"project": "three", "members": str(data.id)})
    assert r.status_code == 202, r.text
    assert project_access.member_ids("three") == [planner.id, data.id], "올린 사람은 고르지 않아도 담당자"
    _project("three")
    assert "three" in _ids(team["data"]) and "three" in _ids(team["lead"])
    # 같은 이름의 남의 프로젝트에는 올릴 수 없다 (담당자만 보는 프로젝트를 덮어쓰지 않게)
    r = team["planner"].post("/api/v1/runs", files=docx_upload(), data={"project": "two"})
    assert r.status_code == 409 and "같은 이름의 프로젝트" in r.json()["detail"]
    assert team["lead"].post("/api/v1/runs", files=docx_upload(), data={"project": "two"}).status_code == 202
    assert project_access.member_ids("two") == [], "있던 프로젝트에 다시 올려도 담당자는 그대로 (바꾸기는 팀장이)"


def test_only_the_lead_changes_assignees_and_slack_is_not_needed(team):
    data = _member("data@test.local")
    body = {"project": "two", "members": [data.id]}
    assert team["planner"].post("/api/v1/projects/members", json=body).status_code == 403
    r = team["lead"].post("/api/v1/projects/members", json=body)
    assert r.status_code == 200 and r.json()["result"] == {"added": ["data"], "removed": [], "failed": []}
    assert r.json()["slack"] is None and r.json()["slack_enabled"] is False
    assert [(m["name"], m["always"]) for m in r.json()["members"]] == [("관리자", True), ("data", False)], "팀장은 늘 담당자"
    assert _ids(team["data"]) == ["two"]
    r = team["lead"].post("/api/v1/projects/members", json={"project": "two", "members": []})
    assert r.json()["result"]["removed"] == ["data"] and _ids(team["data"]) == [], "빼면 바로 못 본다"


def test_deleting_a_project_forgets_its_assignees(team):
    assert team["lead"].delete("/api/v1/projects?project=one&confirm=one").status_code == 200
    assert project_access.member_ids("one") == []
