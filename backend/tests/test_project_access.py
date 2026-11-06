"""프로젝트 담당자 (services/project_access.py): 담당자로 고른 회원만 그 프로젝트를 본다. PM도 고른 PM만 보고(PM 최소 한 명),
PM을 고른 적이 없는 프로젝트는 PM 모두가 본다. 담당자가 아니면 목록·알림에서 빠지고 API는 없는 프로젝트처럼 404.
담당자는 업로드할 때 정하고 PM만 바꾼다."""
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
    """PM·개발자 둘과 프로젝트 둘 (one: 개발자1만 담당자, two: 담당자 없음 = CLI로 만든 프로젝트)"""
    monkeypatch.setattr(jobs, "start_run", lambda **kw: jobs.Job(id="j", project=kw["project"], base="", source="run",
                                                                 status="running", message=""))
    clients = {"pm": signed_in_client("pm"), "dev1": signed_in_client("developer", "dev1"), "dev2": signed_in_client("developer", "dev2")}
    _project("one")
    _project("two")
    project_access.set_members("one", [_member("dev1@test.local").id])
    return clients


def test_members_see_only_their_projects_and_the_pm_sees_projects_without_a_chosen_pm(team):
    assert _ids(team["pm"]) == ["one", "two"], "PM을 고른 적이 없는 프로젝트는 PM이 본다"
    assert _ids(team["dev1"]) == ["one"], "담당자인 프로젝트만"
    assert _ids(team["dev2"]) == [], "담당자가 아니면 목록에 없다"
    assert team["dev1"].get("/api/v1/design?project=one&version=1").status_code == 200


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
    r = getattr(team["dev1"], method)(url, **({"json": body[0]} if body else {}))
    assert r.status_code == 404 and "담당자가 아닙니다" in r.json()["detail"], (url, r.status_code, r.text)


def test_notices_and_jobs_of_other_projects_are_hidden(team):
    for project in ("one", "two"):
        directives.add_event(project, "uploaded", "분석 완료", "", "spec", "1", directives.EVERYONE)
    jobs._jobs["job-two"] = jobs.Job(id="job-two", project="two", base="", source="run", status="running", message="")
    try:
        notices = lambda c: sorted({n["project"] for n in c.get("/api/v1/notices").json()["notices"]})   # noqa: E731
        assert notices(team["dev1"]) == ["one"] and notices(team["pm"]) == ["one", "two"]
        assert notices(team["dev2"]) == []
        assert team["dev1"].get("/api/v1/jobs?id=job-two").status_code == 404
        assert team["pm"].get("/api/v1/jobs?id=job-two").status_code == 200
        assert team["dev1"].get("/api/v1/jobs/running?project=two").status_code == 404
        assert team["pm"].get("/api/v1/jobs/running?project=two").json()["job"]["id"] == "job-two"
    finally:
        jobs._jobs.pop("job-two", None)


def test_only_the_chosen_pms_see_a_project(team):
    """PM도 고른 PM만 그 프로젝트를 본다. PM을 고른 적이 없거나 고른 PM이 모두 사용 중지되면 PM 모두가 본다"""
    pm2 = signed_in_client("pm", "pm2")
    p1, p2, dev1 = _member("admin@test.local"), _member("pm2@test.local"), _member("dev1@test.local")
    assert _ids(pm2) == ["one", "two"], "PM을 고른 적이 없으면 PM 모두"
    project_access.set_members("one", [p2.id, dev1.id])
    assert _ids(team["pm"]) == ["two"] and _ids(pm2) == ["one", "two"], "PM을 고르면 고른 PM만"
    assert team["pm"].get("/api/v1/design?project=one&version=1").status_code == 404
    assert {m.name for m in project_access.assignees("one")} == {"pm2", "dev1"}
    assert {m.name for m in project_access.assignees("two")} == {"관리자", "pm2"}, "PM을 고른 적이 없으면 PM 모두가 담당자"
    users.update_member(p2.id, status="disabled")
    assert _ids(team["pm"]) == ["one", "two"], "고른 PM이 모두 사용 중지되면 다시 PM 모두"
    assert p1.id not in project_access.member_ids("one")


def test_upload_needs_a_pm_and_makes_the_uploader_and_the_chosen_members_assignees(team):
    pm, dev1, dev2 = _member("admin@test.local"), _member("dev1@test.local"), _member("dev2@test.local")
    r = team["dev1"].post("/api/v1/runs", files=docx_upload(), data={"project": "three", "members": str(dev2.id)})
    assert r.status_code == 400 and "PM 최소 한 명은 필요합니다" in r.json()["detail"], "새 프로젝트는 PM을 골라야 한다"
    assert project_access.member_ids("three") == []
    r = team["dev1"].post("/api/v1/runs", files=docx_upload(), data={"project": "three", "members": f"{pm.id},{dev2.id}"})
    assert r.status_code == 202, r.text
    assert sorted(project_access.member_ids("three")) == sorted([pm.id, dev1.id, dev2.id]), "올린 사람은 고르지 않아도 담당자"
    _project("three")
    assert "three" in _ids(team["dev2"]) and "three" in _ids(team["pm"])
    # 같은 이름의 남의 프로젝트에는 올릴 수 없다 (담당자만 보는 프로젝트를 덮어쓰지 않게)
    r = team["dev1"].post("/api/v1/runs", files=docx_upload(), data={"project": "two"})
    assert r.status_code == 409 and "같은 이름의 프로젝트" in r.json()["detail"]
    assert team["pm"].post("/api/v1/runs", files=docx_upload(), data={"project": "two"}).status_code == 202
    assert project_access.member_ids("two") == [], "있던 프로젝트에 다시 올리면 담당자는 그대로 (PM 검사도 없음, 바꾸기는 PM이)"


def test_only_the_pm_changes_assignees_with_at_least_one_pm_and_slack_is_not_needed(team):
    pm, dev2 = _member("admin@test.local"), _member("dev2@test.local")
    body = {"project": "two", "members": [pm.id, dev2.id]}
    assert team["dev1"].post("/api/v1/projects/members", json=body).status_code == 403
    r = team["pm"].post("/api/v1/projects/members", json={"project": "two", "members": [dev2.id]})
    assert r.status_code == 400 and r.json()["detail"] == "PM 최소 한 명은 필요합니다."
    r = team["pm"].post("/api/v1/projects/members", json=body)
    assert r.status_code == 200 and r.json()["result"] == {"added": ["dev2"], "removed": [], "failed": []}
    assert r.json()["slack"] is None and r.json()["slack_enabled"] is False
    assert [(m["name"], m["role"]) for m in r.json()["members"]] == [("관리자", "pm"), ("dev2", "developer")]
    assert _ids(team["dev2"]) == ["two"]
    r = team["pm"].post("/api/v1/projects/members", json={"project": "two", "members": [pm.id]})
    assert r.json()["result"]["removed"] == ["dev2"] and _ids(team["dev2"]) == [], "빼면 바로 못 본다"


def test_deleting_a_project_forgets_its_assignees(team):
    assert team["pm"].delete("/api/v1/projects?project=one&confirm=one").status_code == 200
    assert project_access.member_ids("one") == []
