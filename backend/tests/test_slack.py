"""Slack 알림 (services/slack.py): 팀 워크스페이스 봇 토큰 하나로 프로젝트마다 비공개 채널을 자동으로 만들고 담당자를 초대,
알림을 채널에 하나씩 @멘션으로 올린다.

Slack API는 부르지 않는다 (_call을 가짜로 바꿔 채널 만들기·초대·내보내기·올린 메시지를 모은다).
"""
import itertools
import json
from dataclasses import replace

import pytest

from app.core import config
from app.core.config import get_settings
from app.services import drafts, jobs, slack, users
from conftest import docx_upload, signed_in_client

PROJECT = "team/demo"


class FakeSlack:
    """가짜 Slack Web API: 채널 만들기·초대·내보내기·메시지를 기록한다. U0FAKE…는 워크스페이스에 없는 멤버 ID"""

    def __init__(self):
        self.calls: list[str] = []
        self.posts: list[tuple[str, str]] = []      # (채널 ID, 글)
        self.invites: list[tuple[str, str]] = []    # (채널 ID, 멤버 ID) — 지금 채널에 있는 사람
        self.kicks: list[tuple[str, str]] = []
        self.channels: dict[str, str] = {}          # 이름 → ID
        self.taken: set[str] = set()                # 이미 있는 채널 이름
        self.ascii_only = False                     # 한글 채널 이름을 받지 않는 워크스페이스
        self.bad_token = False

    def __call__(self, method, **p):
        self.calls.append(method)
        if self.bad_token:
            raise slack.SlackError(slack.ERRORS["invalid_auth"], "invalid_auth")
        if method == "auth.test":
            return {"ok": True, "team": "Team4"}
        if method == "conversations.create":
            name = p["name"]
            assert p["is_private"] == "true", "담당자만 보는 비공개 채널"
            if name in self.taken or name in self.channels:
                raise slack.SlackError("이름 있음", "name_taken")
            if self.ascii_only and not name.isascii():
                raise slack.SlackError("이름 규칙", "invalid_name_specials")
            self.channels[name] = f"C{len(self.channels) + 1:03d}"
            return {"ok": True, "channel": {"id": self.channels[name], "name": name}}
        if method == "conversations.invite":
            if p["users"].startswith("U0FAKE"):
                raise slack.SlackError(slack.ERRORS["user_not_found"], "user_not_found")
            if (p["channel"], p["users"]) in self.invites:
                raise slack.SlackError("이미 있음", "already_in_channel")
            self.invites.append((p["channel"], p["users"]))
            return {"ok": True}
        if method == "conversations.kick":
            if (p["channel"], p["user"]) not in self.invites:
                raise slack.SlackError(slack.ERRORS["not_in_channel"], "not_in_channel")
            self.invites.remove((p["channel"], p["user"]))
            self.kicks.append((p["channel"], p["user"]))
            return {"ok": True}
        if method == "chat.postMessage":
            self.posts.append((p["channel"], p["text"]))
            return {"ok": True}
        raise AssertionError(method)

    def texts(self, channel="C001"):
        return [t for c, t in self.posts if c == channel]


@pytest.fixture
def fake(monkeypatch):
    """Slack을 켜고(.env SLACK_BOT_TOKEN 대신 가짜 토큰) Slack API를 가짜로 바꾼다"""
    config._settings = replace(config._settings, slack_bot_token="xoxb-test")
    f = FakeSlack()
    monkeypatch.setattr(slack, "_call", f)
    slack._retry_at.clear()
    while not slack._outbox.empty():
        slack._outbox.get_nowait()
    # 같은 초에 여러 번 요청해도 다른 알림이 되게 초안 시각을 1초씩 늘린다
    ticks = (f"2026-11-04T10:{i // 60:02d}:{i % 60:02d}" for i in itertools.count(1))
    monkeypatch.setattr(drafts, "_now", lambda: next(ticks))
    return f


def _rec(version, stage, parent=None, base=None, at="2000-01-01T00:00:00", by=None, project=PROJECT):
    """버전 하나 (산출물 폴더 + 버전 기록)"""
    from app.agents.tools.versions import save_version

    folder = get_settings().output_dir.joinpath(*project.split("/"), f"v{version}")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "design.json").write_text(json.dumps({"meta": {}, "requirements": [],
                                                    "data_model": {"entities": [{"entity_id": "t"}]}}), encoding="utf-8")
    record = {"design_version": version, "stage": stage, "parent": parent, "base": base, "created_at": at}
    if by:
        record["created_by"] = {"id": by.id, "name": by.name, "role": by.role}
    save_version(project, record)


def _member(email):
    return next(m for m in users.list_members() if m.email == email)


def _sid(m):
    return f"UMEMBER{m.id:03d}"


def _team(*roles):
    """역할마다 회원을 만들고 로그인한 클라이언트를 돌려준다. 모두 Slack 멤버 ID를 넣는다"""
    clients = {r: signed_in_client(r) for r in roles}
    for m in users.list_members():
        users.update_member(m.id, slack_id=_sid(m))
    return clients


def test_slack_is_off_without_a_workspace_token(monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "start_run", lambda **kw: started.append(kw) or jobs.Job(
        id="j", project=kw["project"], base="", source="run", status="running", message=""))
    planner = signed_in_client("planner")
    assert not slack.enabled() and planner.get("/api/v1/members").json()["slack_enabled"] is False
    r = planner.post("/api/v1/runs", files=docx_upload(), data={"slack_members": "1"})
    assert r.status_code == 202 and r.json()["slack"] is None and started
    _rec("1", "function_designer")
    slack.flush()   # 토큰이 없으면 채널도 만들지 않고 아무것도 보내지 않는다 (Slack을 부르면 실패한다)
    assert slack.connected() == []


def test_upload_makes_the_project_channel_with_the_chosen_members(fake, monkeypatch):
    """업로드 때 고른 담당자로 채널을 만든다 (토큰은 .env 하나). 초대하지 못한 회원은 담당자에 넣지 않는다.
    같은 프로젝트에 다시 올리면 채널을 새로 만들지 않고, Slack이 실패해도 분석은 시작한다."""
    started = []
    monkeypatch.setattr(jobs, "start_run", lambda **kw: started.append(kw) or jobs.Job(
        id="j", project=kw["project"], base="", source="run", status="running", message=""))
    c = _team("planner", "planner_head", "data")
    planner, head = _member("planner@test.local"), _member("planner_head@test.local")
    nobody = users.create_member("x@test.local", "Slack없음", "password123", "data")
    ghost = users.create_member("g@test.local", "없는ID", "password123", "data", slack_id="U0FAKE0001")
    assert c["planner"].get("/api/v1/members").json()["slack_enabled"] is True
    files = docx_upload()
    form = {"project": "회사A 포털", "slack_members": f"{planner.id},{head.id},{nobody.id},{ghost.id}"}
    r = c["planner"].post("/api/v1/runs", files=files, data=form)
    assert r.status_code == 202, r.text
    got = r.json()["slack"]
    assert (got["team"], got["channel_name"], got["existing"]) == ("Team4", "team4-회사a_포털", False)
    assert got["invited"] == ["planner", "planner_head"] and [f["name"] for f in got["failed"]] == ["Slack없음", "없는ID"]
    assert "xoxb" not in r.text
    assert slack.project_config("회사A_포털")["members"] == [planner.id, head.id], "담당자 = 채널에 들어간 회원"
    intro = fake.texts()[0]
    assert intro.startswith("*[연결]* planner님이 올린 프로젝트") and f"<@{_sid(planner)}> <@{_sid(head)}>" in intro

    r = c["planner"].post("/api/v1/runs", files=files, data=form)
    assert r.json()["slack"]["existing"] is True and len(fake.channels) == 1, "같은 프로젝트는 채널을 다시 만들지 않는다"

    started.clear()
    fake.bad_token = True
    r = c["planner"].post("/api/v1/runs", files=files, data={**form, "project": "회사B"})
    assert r.status_code == 202 and started, "Slack이 실패해도 분석은 시작한다"
    assert "봇 토큰이 올바르지 않습니다" in r.json()["slack"]["error"]
    assert slack.project_config("회사B") == {"project": "회사B", "team": "", "channel_id": "", "channel_name": "",
                                            "members": [planner.id, head.id, nobody.id, ghost.id]}, "고른 담당자는 남겨 둔다"


def test_projects_without_a_channel_get_one_automatically(fake):
    """채널이 없는 기존 프로젝트: 서버가 만든다 (담당자 = Slack 멤버 ID가 있는 승인된 회원 모두, 초대된 사람만).
    업로드 때 만들지 못한 프로젝트는 그때 고른 담당자로. 실패하면 RETRY_SECONDS 동안 다시 부르지 않는다."""
    _team("planner", "data")
    p, d, lead = _member("planner@test.local"), _member("data@test.local"), _member("admin@test.local")
    users.update_member(lead.id, slack_id="U0FAKE0002")   # 워크스페이스에 없는 ID
    users.create_member("x@test.local", "Slack없음", "password123", "data")
    _rec("1", "function_designer")
    _rec("1", "function_designer", project="회사B")
    slack._save("회사B", members=[p.id])   # 업로드 때 채널을 못 만든 프로젝트 (고른 담당자만 남음)
    slack._save("고아", members=[p.id])    # 업로드했지만 버전이 생기지 않은 프로젝트: 만들지 않는다

    fake.bad_token = True
    assert slack.ensure_channels() == 0
    calls = len(fake.calls)
    assert slack.ensure_channels() == 0 and len(fake.calls) == calls, "실패한 프로젝트는 잠시 다시 부르지 않는다"

    fake.bad_token = False
    slack._retry_at.clear()
    assert slack.ensure_channels() == 2
    demo, b = slack.project_config(PROJECT), slack.project_config("회사B")
    assert demo["channel_name"] == "team4-demo" and demo["members"] == [p.id, d.id], "Slack ID가 맞는 회원 모두"
    assert b["channel_name"] == "team4-회사b" and b["members"] == [p.id], "업로드 때 고른 담당자"
    assert slack.project_config("고아")["channel_id"] == ""
    assert slack.ensure_channels() == 0, "이미 있으면 다시 만들지 않는다"


def test_channel_name_falls_back_when_taken_or_hangul_is_refused(fake):
    signed_in_client()
    fake.taken.add("team4-고객-요구사항")
    assert slack.create_channel("고객 요구사항", [])["channel_name"] == "team4-고객-요구사항-2"
    fake.ascii_only = True
    name = slack.create_channel("회사 b", [])["channel_name"]
    assert name.startswith("team4-b-") and name.isascii(), "한글 이름을 받지 않으면 영문 이름 + 해시"


def test_each_notice_is_one_channel_message_mentioning_the_members_who_get_it(fake):
    """파트원의 승인 요청 → 채널에 알림마다 하나씩 (묶지 않음), 그 알림을 받는 담당자(기획 파트장·팀장)를 멘션.
    담당자가 아닌 회원은 멘션하지 않고, 채널이 없는 프로젝트의 알림은 올리지 않는다."""
    _team("planner", "planner_head", "lead")
    p, head, lead = _member("planner@test.local"), _member("planner_head@test.local"), _member("admin@test.local")
    for v in range(1, 8):
        _rec(str(v), "function_designer")
        drafts.request(PROJECT, str(v), p, note=f"요청 {v}")
    slack.create_channel(PROJECT, [p.id, head.id])   # 팀장은 담당자가 아님
    fake.posts.clear()
    slack.flush()
    texts = fake.texts()
    assert len(texts) == 7, "알림마다 메시지 하나 (묶지 않음)"
    assert texts[0].startswith(f"<@{_sid(head)}> *[승인 요청]* planner님의 수정 완료 승인 요청 (기능 명세 v1)")
    assert "요청 1" in texts[0] and "http://localhost:5173/#/p/team%2Fdemo/1/spec|화면에서 열기>" in texts[0]
    assert all(f"<@{_sid(lead)}>" not in t for t in texts), "채널 담당자가 아닌 팀장은 멘션하지 않는다"
    assert [t.split("(기능 명세 v")[1][0] for t in texts] == list("1234567"), "오래된 것부터"
    slack.flush()
    assert len(fake.texts()) == 7, "같은 알림은 다시 올리지 않는다"
    drafts.request(PROJECT, "3", p, note="다시 요청")
    slack.flush()
    assert len(fake.texts()) == 8 and "다시 요청" in fake.texts()[-1], "새 요청은 새 알림"


def test_request_rejected_and_approved_messages_mention_the_requester(fake, monkeypatch):
    monkeypatch.setattr(jobs, "start_rerun", lambda **kw: jobs.Job(id="j", project=PROJECT, base="1", source="spec",
                                                                    status="running", message=""))
    _rec("1", "function_designer")
    _rec("1.1", "data_modeler", parent="1", base="1")
    c = _team("planner", "planner_head", "data_head")
    p, head = _member("planner@test.local"), _member("planner_head@test.local")
    slack.create_channel(PROJECT, [p.id, head.id])
    body = {"project": PROJECT, "version": "1"}
    assert c["planner"].post("/api/v1/drafts/request", json=body).status_code == 200
    slack.flush()
    fake.posts.clear()

    assert c["planner_head"].post("/api/v1/drafts/request/reject", json={**body, "reason": " "}).status_code == 400, "사유 필수"
    assert c["planner"].post("/api/v1/drafts/request/reject", json={**body, "reason": "x"}).status_code == 403
    assert c["data_head"].post("/api/v1/drafts/request/reject", json={**body, "reason": "x"}).status_code == 403
    r = c["planner_head"].post("/api/v1/drafts/request/reject", json={**body, "reason": "FN-3 결재선 다시"})
    assert r.status_code == 200 and r.json()["rejection"]["reason"] == "FN-3 결재선 다시"
    assert c["planner_head"].post("/api/v1/drafts/request/reject", json={**body, "reason": "x"}).status_code == 404
    slack.flush()
    assert fake.texts() == [f"<@{_sid(p)}> *[반려]* planner_head님이 기능 명세 v1 수정 완료 요청을 반려했습니다\n"
                            "사유: FN-3 결재선 다시\n고친 뒤 다시 '수정 완료 승인 요청'을 보내 주세요.\n"
                            "<http://localhost:5173/#/p/team%2Fdemo/1/spec|화면에서 열기>"]

    fake.posts.clear()
    assert c["planner"].post("/api/v1/drafts/request", json=body).status_code == 200
    slack.flush()
    assert len(fake.texts()) == 1 and fake.texts()[0].startswith(f"<@{_sid(head)}> *[승인 요청]*"), "다시 요청하면 새 알림"
    assert c["planner_head"].post("/api/v1/projects/versions", json={"project": PROJECT, "base": "1", "source": "spec"}).status_code == 202
    slack.flush()
    assert len(fake.texts()) == 2
    assert fake.texts()[1].startswith(f"<@{_sid(p)}> *[승인]* planner_head님이 기능 명세 v1 수정 완료 요청을 승인했습니다")


def test_rejection_reaches_part_editors_in_slack_and_stays_in_home_notices(fake):
    """반려: 요청한 파트원 + 그 초안을 고친 같은 파트 담당자를 멘션 (담당자가 아니면 멘션 안 함).
    홈 '나에게 온 알림'에는 요청·고친 파트원에게 '반려'가 남고(파트장·팀장은 아님, Slack에는 다시 안 올림),
    다시 요청하거나 그 뒤 새 기능 명세 버전이 생기면 사라진다."""
    from fastapi.testclient import TestClient

    from app.main import app

    c = _team("planner", "planner_head", "lead")
    p, head = _member("planner@test.local"), _member("planner_head@test.local")
    p2 = users.create_member("p2@test.local", "기획2", "password123", "planner", slack_id="UPLAN20002")
    p3 = users.create_member("p3@test.local", "기획3", "password123", "planner", slack_id="UPLAN30003")
    clients = {}
    for m in (p2, p3):
        clients[m.id] = TestClient(app)
        assert clients[m.id].post("/api/v1/auth/login", json={"email": m.email, "password": "password123"}).status_code == 200
    _rec("1", "function_designer")
    slack.create_channel(PROJECT, [p.id, p2.id, head.id])   # 기획3은 담당자가 아님
    draft = {"project": PROJECT, "version": "1"}
    assert clients[p2.id].post("/api/v1/drafts", json={**draft, "state": {"answers": {"FQ-001": "예"}}, "rev": 0}).status_code == 200
    assert clients[p3.id].post("/api/v1/drafts", json={**draft, "state": {"answers": {"FQ-001": "예", "FQ-002": "아니오"}},
                                                       "rev": 1}).status_code == 200
    assert c["planner"].post("/api/v1/drafts/request", json=draft).status_code == 200
    slack.flush()
    fake.posts.clear()

    assert c["planner_head"].post("/api/v1/drafts/request/reject", json={**draft, "reason": "질문 답 다시"}).status_code == 200
    slack.flush()
    assert len(fake.texts()) == 1, "반려 알림은 [반려] 메시지 하나 (홈 알림을 Slack에 다시 올리지 않음)"
    assert fake.texts()[0].startswith(f"<@{p.slack_id}> <@{p2.slack_id}> *[반려]* planner_head님이 기능 명세 v1")
    assert p3.slack_id not in fake.texts()[0], "채널 담당자가 아니면 멘션하지 않는다"
    assert "⚠️ 알림 받을 기획 파트(기획3)이(가) 이 채널 담당자가 아니어서" in fake.texts()[0], "빠진 사람은 안내로 알린다"

    def rejected(client):
        return [n for n in client.get("/api/v1/notices").json()["notices"] if n["kind"] == "rejected"]

    got = rejected(c["planner"])
    assert len(got) == 1 and (got[0]["version"], got[0]["page"]) == ("1", "spec")
    assert got[0]["title"] == "planner_head님이 승인 요청을 반려했습니다 (기능 명세 v1)" and "사유: 질문 답 다시" in got[0]["detail"]
    assert len(rejected(clients[p2.id])) == 1 and len(rejected(clients[p3.id])) == 1, "초안을 고친 같은 파트 회원도"
    assert rejected(c["planner_head"]) == [] and rejected(c["lead"]) == [], "반려한 파트장·팀장에게는 없음"

    assert c["planner"].post("/api/v1/drafts/request", json=draft).status_code == 200
    assert rejected(clients[p2.id]) == [], "다시 요청하면 사라진다"
    assert c["planner_head"].post("/api/v1/drafts/request/reject", json={**draft, "reason": "또"}).status_code == 200
    assert len(rejected(c["planner"])) == 1
    _rec("2", "function_designer", parent="1", at="2999-01-01T00:00:00")   # 고쳐서 새 기능 명세 버전을 만듦
    assert rejected(c["planner"]) == [], "그 뒤 새 버전이 생기면 처리된 것으로 본다"


def test_lead_decision_is_one_message_for_related_members_and_the_part_that_must_fix(fake):
    """수정 지시(반려): 관여한 회원 + 지시를 처리할 데이터 파트를 멘션한 메시지 하나 (같은 지시의 '수정 지시' 알림은 따로 안 올림).
    팀장 자신은 멘션하지 않는다. 최종 승인은 관여한 회원에게."""
    c = _team("planner", "data", "data_head", "lead")
    users.create_member("data2@test.local", "data2", "password123", "data", slack_id="UDATA20002")
    p, d, dh, lead = (_member(f"{r}@test.local") for r in ("planner", "data", "data_head", "admin"))
    d2 = _member("data2@test.local")
    _rec("1", "function_designer", by=p)
    _rec("1.1", "data_modeler", parent="1", base="1", by=d)
    _rec("1.1.1", "final", parent="1.1", base="1.1", at="2000-01-01T00:00:02", by=dh)
    slack.create_channel(PROJECT, [p.id, d.id, dh.id, d2.id, lead.id])
    fake.posts.clear()
    slack.flush()
    assert [t.split("\n")[0] for t in fake.texts()] == [f"<@{lead.slack_id}> *[최종 승인 대기]* 최종본 v1.1.1 승인 대기"]

    fake.posts.clear()
    body = {"project": PROJECT, "version": "1.1.1", "decision": "changes", "targets": ["erd"], "comment": "결재선 테이블 추가",
            "items": [{"stage": "erd", "target_id": "approval_line", "label": "결재선", "note": "단계 컬럼"}]}
    assert c["lead"].post("/api/v1/projects/approval", json=body).status_code == 200
    slack.flush()
    assert len(fake.texts()) == 1, "반려 메시지 하나 (수정 지시 알림을 따로 올리지 않음)"
    text = fake.texts()[0]
    first = text.split("\n")[0]
    for m in (p, d, dh, d2):
        assert f"<@{m.slack_id}>" in first
    assert f"<@{lead.slack_id}>" not in text, "팀장 자신은 멘션하지 않는다"
    assert "*[반려 · 수정 지시]* 관리자님이 최종본 v1.1.1을 반려하고 ERD 단계로 돌려보냈습니다" in first
    assert "지시: 결재선 테이블 추가" in text and "`approval_line` 결재선 — 단계 컬럼" in text
    assert "#/p/team%2Fdemo/1.1/erd|" in text
    slack.flush()
    assert len(fake.texts()) == 1

    fake.posts.clear()
    assert c["lead"].post("/api/v1/projects/approval", json={**body, "decision": "approve", "comment": ""}).status_code == 200
    slack.flush()
    assert len(fake.texts()) == 1 and "*[최종 승인]* 관리자님이 최종본 v1.1.1을 승인했습니다" in fake.texts()[0]
    assert all(f"<@{m.slack_id}>" in fake.texts()[0] for m in (p, d, dh)) and f"<@{d2.slack_id}>" not in fake.texts()[0]


def test_heads_change_assignees_later_invite_new_and_remove_old(fake):
    """중간에 담당자가 바뀌면: 팀장이 담당자를 다시 고른다 → 더한 회원 초대, 뺀 회원 채널에서 내보냄, 채널에 알림.
    그 뒤 알림 멘션은 새 담당자 기준. 파트원·파트장은 못 바꾸고, 채널이 없는 프로젝트는 404."""
    c = _team("planner", "planner_head", "data", "lead")
    p, head, d = (_member(f"{r}@test.local") for r in ("planner", "planner_head", "data"))
    new = users.create_member("new@test.local", "신규", "password123", "planner", slack_id="UNEWMEM001")
    _rec("1", "function_designer")
    slack.create_channel(PROJECT, [p.id, d.id])
    fake.posts.clear()
    body = {"project": PROJECT, "members": [p.id, new.id, head.id]}
    for role in ("planner", "planner_head"):
        r = c[role].post("/api/v1/projects/slack/members", json=body)
        assert r.status_code == 403 and "팀장만" in r.json()["detail"], role
    r = c["lead"].post("/api/v1/projects/slack/members", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"added": ["신규", "planner_head"], "removed": ["data"], "failed": []}
    assert [m["name"] for m in r.json()["slack"]["members"]] == ["planner", "신규", "planner_head"]
    assert fake.kicks == [("C001", d.slack_id)], "뺀 담당자는 채널에서 내보낸다"
    assert {u for _, u in fake.invites} == {p.slack_id, new.slack_id, head.slack_id}, "이미 있던 담당자는 그대로"
    msg = fake.texts()[0]
    assert msg.startswith("*[담당자 변경]* 관리자님이") and f"추가: <@{new.slack_id}> <@{head.slack_id}>" in msg
    assert "제외: data" in msg and f"<@{d.slack_id}>" not in msg, "뺀 사람은 멘션하지 않는다"

    got = c["data"].get(f"/api/v1/projects/slack?project={PROJECT}").json()["slack"]
    assert got["channel_name"] == "team4-demo" and "xoxb" not in json.dumps(got), "보기는 누구나, 토큰은 없음"
    fake.posts.clear()
    drafts.request(PROJECT, "1", p)
    slack.flush()
    assert fake.texts()[0].startswith(f"<@{head.slack_id}> *[승인 요청]*"), "새 담당자를 멘션"

    slack._save("other/unlinked", members=[])
    assert c["planner_head"].get("/api/v1/projects/slack?project=team/demo").status_code == 200
    _rec("1", "function_designer", project="other/unlinked")
    assert c["planner_head"].get("/api/v1/projects/slack?project=other/unlinked").json()["slack"] is None, "채널이 아직 없음"
    assert c["lead"].post("/api/v1/projects/slack/members", json={"project": "other/unlinked", "members": []}).status_code == 404


def test_slack_id_at_signup_and_the_member_picker():
    admin = signed_in_client()
    anon = admin.__class__(admin.app)
    r = anon.post("/api/v1/auth/signup", json={"email": "n@co.kr", "name": "신입", "password": "password123", "slack_id": "bad"})
    assert r.status_code == 400 and "멤버 ID" in r.json()["detail"]
    r = anon.post("/api/v1/auth/signup", json={"email": "n@co.kr", "name": "신입", "password": "password123", "slack_id": " u0new0001 "})
    assert r.status_code == 200 and r.json()["user"]["slack_id"] == "U0NEW0001"
    r = admin.post("/api/v1/users/create", json={"email": "d@co.kr", "name": "데이터", "password": "password123", "role": "data",
                                                 "slack_id": "U0DATA0001"})
    assert r.json()["user"]["slack_id"] == "U0DATA0001"
    planner = signed_in_client("planner")
    picker = planner.get("/api/v1/members").json()["members"]
    assert {m["name"]: m["has_slack"] for m in picker} == {"관리자": False, "데이터": True, "planner": False}, \
        "승인된 회원만 (가입 대기는 빼고), 이메일·Slack ID 원문은 주지 않는다"
    assert "slack_id" not in picker[0] and "email" not in picker[0]


def test_message_text_escapes_slack_markup():
    n = {"kind": "request", "project": "a/b c", "page": "erd", "version": "1.2", "at": "t", "title": "<!channel> & 요청",
         "detail": ""}
    text = slack.notice_text(n, "<@U1>")
    assert text.startswith("<@U1> *[승인 요청]* &lt;!channel&gt; &amp; 요청") and "#/p/a%2Fb%20c/1.2/erd|화면에서 열기>" in text
    assert slack.notice_key(n) == "request|a/b c|erd|1.2|t"
    assert slack.channel_name("회사/고객 요구사항.v2") == "team4-고객-요구사항-v2"


def test_slack_sends_the_agreed_notices_to_the_agreed_roles(sample_doc):
    """합친 뒤 알림 규칙: 업로드 분석 완료 → 모든 회원, ERD 초안 → 데이터 파트, 개발 초안 → 개발 파트(파트장 포함),
    승인 요청 → 그 파트장·팀장. 화면에서 '확인'해도 Slack 대상은 그대로다."""
    from app import cli
    from app.agents.llm import client as llm_module
    from app.core.config import override_settings
    from app.services import directives, drafts, jobs, slack, users
    from fake_llm import FakeLLM
    override_settings(llm_provider="anthropic")
    llm_module.set_llm(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer"]) == 0
    people = {role: users.create_member(f"{role}@t.local", role, "pw-123456", role, False, None)
              for role in ("lead", "planner", "planner_head", "data", "data_head", "dev", "dev_head")}
    for source, base, new in (("run", "", "1"), ("spec", "1", "1.1"), ("develop", "1.1.1", "1.1.1")):
        directives.job_event(jobs.Job(id="j", project="sample", base=base, source=source, status="done", message="", new_version=new))
    drafts.request("sample", "1", people["planner"], "확인 부탁")
    directives.dismiss("sample", next(n["id"] for n in directives.notices(people["data"]) if n["kind"] == "erd_draft"), people["data"])
    who = {}
    for n, members in slack._pending({"sample"}).values():
        who.setdefault(n["kind"], set()).update(m.role for m in members)
    assert who["uploaded"] >= set(people), "업로드 분석 완료 → 모든 회원"
    assert who["erd_draft"] == {"data", "data_head"}, "ERD 초안 → 데이터 파트 (확인해도 Slack은 보낸다)"
    assert who["developed"] == {"dev", "dev_head"}, "개발 초안 → 개발 파트(파트장 포함)"
    assert who["request"] == {"planner_head", "lead"}, "기획 승인 요청 → 기획 파트장·팀장"


def test_notice_mentions_the_members_who_get_it_or_the_lead_with_a_hint(fake):
    """알림 받을 회원 중 채널 담당자를 멘션한다. 받을 회원이 아무도 채널에 없으면(예: 개발 파트를 담당자로 안 넣음)
    묻히지 않게 팀장을 대신 멘션하고 누가 빠졌는지 알린다. 받을 회원이 채널에 있으면 안내는 없다."""
    from app.services import directives

    _team("lead", "data", "data_head", "dev", "dev_head")
    lead, d, dh, dev, devh = (_member(f"{r}@test.local") for r in ("admin", "data", "data_head", "dev", "dev_head"))
    _rec("1", "function_designer")
    slack.create_channel(PROJECT, [lead.id, d.id, dh.id])   # 개발 파트는 담당자가 아님
    fake.posts.clear()
    directives.add_event(PROJECT, "erd_draft", "ERD 초안 v1.1", "데이터 파트가 ERD를 검토합니다", "erd", "1.1", {"data", "data_head"})
    directives.add_event(PROJECT, "developed", "개발 초안 완료", "결과를 확인하세요", "develop", "1.1.1", {"dev", "dev_head"})
    slack.flush()
    erd, developed = fake.texts()
    assert erd.startswith(f"<@{d.slack_id}> <@{dh.slack_id}> *[ERD 초안]*") and "⚠️" not in erd, "데이터 파트를 멘션"
    assert f"<@{lead.slack_id}>" not in erd, "받을 회원이 채널에 있으면 팀장을 대신 부르지 않는다"
    assert developed.startswith(f"<@{lead.slack_id}> *[개발 초안]*"), "개발 파트가 채널에 없으면 팀장을 대신 멘션"
    assert "⚠️ 알림 받을 개발 파트(dev·dev_head)이(가) 이 채널 담당자가 아니어서 멘션하지 못했습니다" in developed
    assert dev.slack_id not in developed and devh.slack_id not in developed


def test_result_message_says_when_the_requester_is_not_in_the_channel(fake):
    """승인·반려 결과: 요청한 파트원이 채널 담당자가 아니면 멘션 대신 이름과 안내를 남긴다"""
    _team("planner", "planner_head")
    p, head = _member("planner@test.local"), _member("planner_head@test.local")
    _rec("1", "function_designer")
    slack.create_channel(PROJECT, [head.id])   # 요청한 파트원은 담당자가 아님
    drafts.request(PROJECT, "1", p)
    req = drafts.reject(PROJECT, "1", head, "다시")
    slack.flush()
    fake.posts.clear()
    slack.request_decided(PROJECT, "1", "spec", req, head, approved=False, reason="다시")
    slack.flush()
    text = fake.texts()[0]
    assert text.startswith("planner *[반려]*") and f"<@{p.slack_id}>" not in text
    assert "⚠️ 알림 받을 기획 파트(planner)이(가) 이 채널 담당자가 아니어서" in text
