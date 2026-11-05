"""Slack 알림: 팀 워크스페이스 하나(.env SLACK_BOT_TOKEN)에 프로젝트마다 비공개 채널을 자동으로 만들고, 담당자를 초대해
그 채널에 알림을 @멘션으로 올린다.

- 워크스페이스: 이 웹의 회원이 모두 들어 있는 팀 워크스페이스 하나. 봇 토큰(xoxb-…)은 .env SLACK_BOT_TOKEN에만 둔다.
  Slack 앱 권한(Bot Token Scopes): chat:write, groups:write (비공개 채널 만들기·초대·내보내기·메시지).
- 채널 만들기(create_channel): 업로드할 때 고른 담당자로 비공개 채널(team4-<프로젝트>)을 만들어 초대한다.
  채널이 없는 기존 프로젝트는 백그라운드 스레드가 알아서 만든다 (ensure_channels: 담당자 = Slack 멤버 ID가 있는 승인된 회원 모두).
  만들지 못하면(Slack 오류) 업로드는 그대로 하고, RETRY_SECONDS 뒤에 다시 만들어 본다. 채널·담당자는 slack_projects 테이블.
- 담당자 = 채널에 실제로 초대된 회원. Slack 멤버 ID가 없거나 Slack에 없는 ID면 담당자에 넣지 않는다 (화면에 이유를 보여 준다).
- 담당자 바꾸기(update_members): 프로젝트 화면에서 팀장이 담당자를 더하거나 빼면, 더한 회원은 채널에 초대하고
  뺀 회원은 채널에서 내보낸다 (고른 담당자는 모두 다시 초대해 보므로, 전에 초대하지 못한 담당자도 이때 들어간다).
- 회원의 Slack 멤버 ID(U…)는 회원가입·관리자 페이지에서 넣는다. 초대와 멘션에 쓴다.
- '나에게 온 알림'(services/directives.notices: 승인 요청·최종 승인 대기·수정 지시·수정 대기·개발 대기와
  일 알림 업로드 분석 완료(모든 회원)·ERD 초안(데이터 파트)·개발 초안(개발 파트))의 새 항목은 그 프로젝트
  채널에 알림마다 메시지 하나로 올린다 (그 알림을 받는 회원 중 담당자를 멘션). 같은 알림은 한 번만: 올린 알림 키를
  slack_notifications에 남긴다. 알림은 저장된 상태에서 계산되므로 승인 요청·결정·작업 완료 때 바로 확인하고(kick)
  SLACK_SYNC_SECONDS마다도 확인한다 (CLI로 만든 버전처럼 서버가 모르는 변화도 잡는다).
- 승인·반려 결과는 결정할 때 바로 그 채널에 올린다:
    파트장·팀장이 파트원의 수정 완료 요청을 확정(= 승인)·반려 → 요청한 파트원을 멘션
    팀장이 최종본을 최종 승인 → 그 최종본·ERD·기능 명세를 만들거나 고친 회원을 멘션
    팀장이 수정 지시(= 반려) → 위 회원과 지시를 처리할 파트를 멘션한 메시지 하나 (같은 지시의 '수정 지시' 알림은 따로 안 올림)
- 보내기는 서버 안의 백그라운드 스레드 하나가 한다: Slack이 느리거나 실패해도 화면 요청은 기다리지 않고, 실패는 로그만 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from sqlalchemy import delete, select

from app.core.auth import ROLES
from app.core.config import get_settings
from app.models.notification import SlackNotification, SlackProject
from app.services import directives, drafts, output_store, users

log = logging.getLogger(__name__)

API = "https://slack.com/api"
STAGE = {"spec": "기능 명세", "erd": "ERD", "final": "최종본"}
KIND = {"request": "승인 요청", "decide": "최종 승인 대기", "directive": "수정 지시", "waiting": "수정 대기", "develop": "개발 대기",
        "uploaded": "분석 완료", "erd_draft": "ERD 초안", "developed": "개발 초안",
        "merge_request": "합치기 요청", "merged": "합치기 완료", "merge_rejected": "합치기 반려"}
SENT_DAYS = 60       # 올린 알림 기록을 이만큼 둔다 (키에 시각이 들어 있어 그 뒤 같은 알림이 다시 생기지 않는다)
RETRY_SECONDS = 600  # 채널을 만들지 못한 프로젝트는 이만큼 뒤에 다시 만들어 본다
ERRORS = {           # Slack 오류 코드 → 화면에 보일 말
    "invalid_auth": "Slack 봇 토큰이 올바르지 않습니다 (.env SLACK_BOT_TOKEN).",
    "not_authed": "Slack 봇 토큰이 없습니다 (.env SLACK_BOT_TOKEN).",
    "token_revoked": "Slack 봇 토큰이 취소됐습니다. Slack 앱을 다시 설치하고 .env SLACK_BOT_TOKEN을 바꾸세요.",
    "account_inactive": "Slack 봇 토큰의 계정이 꺼져 있습니다.",
    "missing_scope": "Slack 앱 권한이 부족합니다 (Bot Token Scopes에 chat:write, groups:write를 넣고 앱을 다시 설치).",
    "restricted_action": "이 워크스페이스에서는 봇이 채널을 만들 수 없습니다 (워크스페이스 설정 확인).",
    "user_not_found": "Slack 워크스페이스에 없는 멤버 ID입니다.",
    "user_is_restricted": "게스트 계정이라 초대할 수 없습니다.",
    "channel_not_found": "프로젝트 채널을 찾지 못했습니다 (보관·삭제됐는지 확인).",
    "is_archived": "프로젝트 채널이 보관 처리됐습니다.",
    "not_in_channel": "봇이 프로젝트 채널에 없습니다.",
    "ratelimited": "Slack 요청 한도를 넘었습니다. 잠시 뒤 다시 보냅니다.",
}


class SlackError(RuntimeError):
    """Slack이 거절했거나 연결하지 못함. message는 화면에 보일 말, code는 Slack 오류 코드"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def enabled() -> bool:
    """.env에 Slack 봇 토큰이 있는지 (없으면 채널을 만들지 않고 아무것도 보내지 않는다)"""
    return bool(get_settings().slack_bot_token)


def _call(method: str, **params: str) -> dict:
    """Slack Web API 호출 (봇 토큰은 .env). 모든 메서드가 받는 form 형식으로 보낸다. ok가 아니면 SlackError"""
    req = urllib.request.Request(
        f"{API}/{method}", data=urllib.parse.urlencode(params).encode("utf-8"),
        headers={"Authorization": f"Bearer {get_settings().slack_bot_token}",
                 "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code = "ratelimited" if e.code == 429 else f"http_{e.code}"
        raise SlackError(ERRORS.get(code, f"Slack 응답 오류 ({e.code})"), code) from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SlackError(f"Slack에 연결하지 못했습니다: {e}", "connection") from e
    if not body.get("ok"):
        code = body.get("error", "unknown_error")
        raise SlackError(ERRORS.get(code, f"Slack 오류: {code}"), code)
    return body


def check() -> str:
    """봇 토큰이 맞는지 확인하고 워크스페이스 이름을 돌려준다 (틀리면 SlackError)"""
    return _call("auth.test").get("team", "")


# ---------------------------------------------------------------- 프로젝트 채널 (slack_projects)

def _config(row: SlackProject) -> dict:
    return {"project": row.project, "team": row.team, "channel_id": row.channel_id,
            "channel_name": row.channel_name, "members": list(row.members or [])}


def project_config(project: str) -> dict | None:
    """그 프로젝트의 Slack 채널·담당자 (채널을 아직 만들지 못했으면 channel_id가 빈 값). 없으면 None"""
    with users.service_db() as db:
        row = db.get(SlackProject, project)
        return _config(row) if row else None


def connected() -> list[dict]:
    with users.service_db() as db:
        return [_config(r) for r in db.scalars(select(SlackProject))]


def project_info(project: str) -> dict | None:
    """화면에 보여 줄 채널 정보: 워크스페이스·채널 이름·담당자. 채널이 없으면 None"""
    config = project_config(project)
    if not config or not config["channel_id"]:
        return None
    people = {m.id: m for m in users.list_members()}
    return {"team": config["team"], "channel_name": config["channel_name"],
            "members": [{"id": i, "name": people[i].name, "role": people[i].role, "role_label": ROLES.get(people[i].role, "역할 없음"),
                         "has_slack": bool(people[i].slack_id)} for i in config["members"] if i in people]}


def channel_name(project: str) -> str:
    """프로젝트 채널 이름: team4-<프로젝트 이름> (Slack 규칙: 소문자·숫자·한글·-·_, 80자 이하)"""
    base = re.sub(r"[^0-9a-z가-힣_-]+", "-", PurePosixPath(project).name.lower()).strip("-_")[:60]
    return f"team4-{base or 'project'}"


def _create(project: str) -> dict:
    """비공개 채널을 만든다. 이름이 있으면 -2·-3, Slack이 한글 이름을 받지 않으면 영문 이름(+짧은 해시)으로"""
    name = channel_name(project)
    ascii_base = re.sub(r"[^0-9a-z_-]+", "-", PurePosixPath(project).name.lower()).strip("-_")[:50] or "project"
    fallback = f"team4-{ascii_base}-{hashlib.sha1(project.encode('utf-8')).hexdigest()[:4]}"
    for candidate in dict.fromkeys([name, f"{name}-2", f"{name}-3", fallback, f"{fallback}-2"]):
        try:
            channel = _call("conversations.create", name=candidate, is_private="true")["channel"]
            return {"id": channel["id"], "name": channel["name"]}
        except SlackError as e:
            if e.code == "name_taken" or (e.code.startswith("invalid_name") and candidate != fallback):
                continue
            raise
    raise SlackError("같은 이름의 Slack 채널이 이미 있어 프로젝트 채널을 만들지 못했습니다.", "name_taken")


def _active(member_ids: Iterable[int]) -> list[users.Member]:
    """고른 회원 ID 중 승인된 회원 (고른 순서, 같은 ID는 한 번)"""
    people = {m.id: m for m in users.list_members() if m.status == "active"}
    return [people[i] for i in dict.fromkeys(int(x) for x in member_ids) if i in people]


def _invite(channel_id: str, members: list[users.Member], project: str) -> tuple[list[users.Member], list[dict]]:
    """채널에 초대한다 (이미 있으면 초대한 것으로). (초대한 회원, [{name, reason}] 초대하지 못한 회원)"""
    invited, failed = [], []
    for m in members:
        if not m.slack_id:
            failed.append({"name": m.name, "reason": "Slack 멤버 ID가 없습니다 (회원가입·관리자 페이지에서 넣기)."})
            continue
        try:
            _call("conversations.invite", channel=channel_id, users=m.slack_id)
            invited.append(m)
        except SlackError as e:
            if e.code == "already_in_channel":
                invited.append(m)
            else:
                failed.append({"name": m.name, "reason": str(e)})
                log.warning("Slack 채널 초대 실패 (%s, %s): %s", project, m.email, e)
    return invited, failed


def _save(project: str, **fields) -> None:
    with users.service_db() as db:
        row = db.get(SlackProject, project)
        if row is None:
            row = SlackProject(project=project, members=[])
            db.add(row)
        for k, v in fields.items():
            setattr(row, k, v)


def create_channel(project: str, member_ids: Iterable[int], by: users.Member | None = None) -> dict:
    """프로젝트 비공개 채널을 만들고 담당자를 초대한다. 이미 채널이 있으면 그대로 둔다 (existing: true).
    돌려주는 것: {team, channel_name, invited: [이름], failed: [{name, reason}], existing}.
    Slack이 실패하면 SlackError — 고른 담당자는 남겨 두어 백그라운드가 나중에 다시 만든다 (ensure_channels)."""
    current = project_config(project)
    if current and current["channel_id"]:
        return {"team": current["team"], "channel_name": current["channel_name"], "invited": [], "failed": [], "existing": True}
    chosen = _active(member_ids)
    _save(project, members=[m.id for m in chosen], created_by=by.id if by else None)   # 실패해도 다시 만들 때 쓸 담당자
    team = check()
    channel = _create(project)
    invited, failed = _invite(channel["id"], chosen, project)
    _save(project, team=team, channel_id=channel["id"], channel_name=channel["name"], members=[m.id for m in invited],
          created_at=datetime.now(timezone.utc))
    config = project_config(project)
    who = " ".join(_mention(config, m) for m in invited) or "(없음)"
    try:
        _post(config, f"*[연결]* {_esc(by.name) + '님이 올린 ' if by else ''}프로젝트 *{_esc(_project_name(project))}* 의 알림 채널입니다.\n"
                      f"담당자: {who}\n승인 요청·승인·반려·수정 지시 알림이 여기로 옵니다.")
    except SlackError as e:
        log.warning("Slack 채널 첫 메시지 실패 (%s): %s", project, e)
    log.info("Slack 채널 #%s 만듦 (%s, 담당자 %d명, 초대 못 함 %d명)", channel["name"], project, len(invited), len(failed))
    kick()   # 이미 쌓인 알림이 있으면 (기존 프로젝트) 바로 올린다
    return {"team": team, "channel_name": channel["name"], "invited": [m.name for m in invited], "failed": failed, "existing": False}


_retry_at: dict[str, float] = {}   # 채널을 만들지 못한 프로젝트 → 다시 만들어 볼 시각 (time.monotonic)


def ensure_channels() -> int:
    """채널이 없는 프로젝트의 채널을 만든다 (업로드 때 만들지 못했거나 Slack을 켜기 전에 만든 프로젝트).
    담당자: 업로드 때 고른 회원, 고른 적이 없으면 Slack 멤버 ID가 있는 승인된 회원 모두. 만든 채널 수"""
    if not enabled():
        return 0
    configs = {c["project"]: c for c in connected()}
    made = 0
    for p in output_store.find_projects():
        project = p["id"]
        config = configs.get(project)
        if (config and config["channel_id"]) or time.monotonic() < _retry_at.get(project, 0):
            continue
        members = config["members"] if config else [m.id for m in users.list_members() if m.status == "active" and m.slack_id]
        try:
            create_channel(project, members)
            made += 1
            _retry_at.pop(project, None)
        except SlackError as e:
            _retry_at[project] = time.monotonic() + RETRY_SECONDS
            log.warning("Slack 채널을 만들지 못함 (%s, %d초 뒤 다시): %s", project, RETRY_SECONDS, e)
    return made


def update_members(project: str, member_ids: Iterable[int], by: users.Member) -> dict:
    """담당자 바꾸기: 고른 회원을 채널에 초대하고(이미 있으면 그대로), 뺀 회원은 채널에서 내보낸 뒤 담당자 목록을 바꾼다.
    돌려주는 것: {added: [새 담당자 이름], removed: [뺀 담당자 이름], failed: [{name, reason}]}. 채널이 없으면 SlackError"""
    config = project_config(project)
    if not config or not config["channel_id"]:
        raise SlackError("이 프로젝트의 Slack 채널이 아직 없습니다.", "not_connected")
    chosen = _active(member_ids)
    before = set(config["members"])
    invited, failed = _invite(config["channel_id"], chosen, project)
    people = {m.id: m for m in users.list_members()}
    gone = [people[i] for i in config["members"] if i in people and i not in {m.id for m in chosen}]
    for m in gone:
        if not m.slack_id:
            continue
        try:
            _call("conversations.kick", channel=config["channel_id"], user=m.slack_id)
        except SlackError as e:
            if e.code not in ("not_in_channel", "user_not_found"):
                failed.append({"name": m.name, "reason": f"채널에서 내보내지 못했습니다: {e}"})
                log.warning("Slack 채널 내보내기 실패 (%s, %s): %s", project, m.email, e)
    _save(project, members=[m.id for m in invited])
    added = [m for m in invited if m.id not in before]
    config = project_config(project)
    lines = [f"*[담당자 변경]* {_esc(by.name)}님이 이 프로젝트의 담당자를 바꿨습니다"]
    if added:
        lines.append("추가: " + " ".join(_mention(config, m) for m in added))
    if gone:
        lines.append("제외: " + ", ".join(_esc(m.name) for m in gone))
    lines.append("담당자: " + (" ".join(_mention(config, m) for m in invited) or "(없음)"))
    try:
        _post(config, "\n".join(lines))
    except SlackError as e:
        log.warning("Slack 담당자 변경 메시지 실패 (%s): %s", project, e)
    return {"added": [m.name for m in added], "removed": [m.name for m in gone], "failed": failed}


# ---------------------------------------------------------------- 메시지

def _esc(text: object) -> str:
    """Slack mrkdwn에서 특수한 글자 (&, <, >)"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(project: str, version: str, page: str) -> str:
    """작업실 화면 주소 (프론트 lib/route.ts toHash와 같은 형식: #/p/<프로젝트>/<버전>/<화면>)"""
    parts = "/".join(urllib.parse.quote(str(p), safe="") for p in ("p", project, version, page))
    return f"{get_settings().app_base_url}/#/{parts}"


def _project_name(project: str) -> str:
    return PurePosixPath(project).name


def _mention(config: dict, member: users.Member) -> str:
    """채널에 초대한 담당자면 @멘션, 아니면 이름만 (채널에 없는 사람을 멘션하면 Slack이 초대하라고 묻는다)"""
    if member.slack_id and member.id in config["members"]:
        return f"<@{member.slack_id}>"
    return _esc(member.name)


def _mentions(config: dict, members: Iterable[users.Member]) -> str:
    """담당자만 멘션 (담당자가 아닌 회원은 적지 않는다)"""
    return " ".join(dict.fromkeys(f"<@{m.slack_id}>" for m in members if m.slack_id and m.id in config["members"]))


PART_LABEL = {"planner": "기획 파트", "planner_head": "기획 파트", "data": "데이터 파트", "data_head": "데이터 파트",
              "dev": "개발 파트", "dev_head": "개발 파트", "lead": "팀장"}


def _missing(config: dict, members: Iterable[users.Member]) -> list[users.Member]:
    """멘션할 수 없는 회원: 이 채널 담당자가 아니거나 Slack 멤버 ID가 없다"""
    return list({m.id: m for m in members if not (m.slack_id and m.id in config["members"])}.values())


def _missing_hint(members: list[users.Member]) -> str:
    """멘션하지 못한 회원 안내 (파트별 이름): 팀장이 채널 담당자로 더하면 다음부터 멘션된다"""
    if not members:
        return ""
    parts: dict[str, list[str]] = {}
    for m in members:
        parts.setdefault(PART_LABEL.get(m.role, ROLES.get(m.role, "회원")), []).append(m.name)
    who = ", ".join(f"{part}({'·'.join(_esc(n) for n in names)})" for part, names in parts.items())
    return (f"⚠️ 알림 받을 {who}이(가) 이 채널 담당자가 아니어서 멘션하지 못했습니다. "
            "팀장이 프로젝트 화면 'Slack 채널 → 담당자 바꾸기'에서 추가하세요.")


def notice_mentions(config: dict, who: list[users.Member], people: dict[int, users.Member]) -> tuple[str, str]:
    """알림을 받을 회원(who) 중 채널 담당자를 멘션한다. 아무도 멘션할 수 없으면 팀장을 대신 멘션하고 안내를 붙인다
    (알림이 아무도 부르지 않고 묻히지 않게). (멘션, 안내)"""
    mentions = _mentions(config, who)
    if mentions or not who:
        return mentions, ""
    leads = [m for m in people.values() if m.role == "lead"]
    return _mentions(config, leads), _missing_hint(_missing(config, who))


def notice_key(n: dict) -> str:
    """알림 하나의 키: 같은 알림이면 같다 (새 요청·새 지시·새 버전이면 시각이나 버전이 달라 새 알림)"""
    return "|".join(str(n.get(k) or "") for k in ("kind", "project", "page", "version", "at"))


def notice_text(n: dict, mentions: str = "", note: str = "") -> str:
    detail = f"\n{_esc(n['detail'])}" if n.get("detail") else ""
    head = f"{mentions} " if mentions else ""
    tail = f"\n{note}" if note else ""
    return (f"{head}*[{KIND.get(n['kind'], '알림')}]* {_esc(n['title'])}{detail}{tail}\n"
            f"<{link(n['project'], n['version'], n['page'])}|화면에서 열기>")


def _post(config: dict, text: str) -> None:
    _call("chat.postMessage", channel=config["channel_id"], text=text, unfurl_links="false", unfurl_media="false")


# ---------------------------------------------------------------- 올린 알림 기록 (slack_notifications)

def _sent(keys: Iterable[str]) -> set[str]:
    keys = list(dict.fromkeys(k[:400] for k in keys))
    if not keys:
        return set()
    with users.service_db() as db:
        return set(db.scalars(select(SlackNotification.key).where(SlackNotification.key.in_(keys))))


def _mark_sent(keys: Iterable[str]) -> None:
    keys = set(k[:400] for k in keys)
    if not keys:
        return
    have = _sent(keys)
    with users.service_db() as db:
        db.add_all(SlackNotification(key=k) for k in keys - have)
        db.execute(delete(SlackNotification).where(
            SlackNotification.sent_at < datetime.now(timezone.utc) - timedelta(days=SENT_DAYS)))


def _pending(projects: set[str]) -> dict[str, tuple[dict, list[users.Member]]]:
    """연결된 프로젝트의 알림 키 → (알림, 그 알림을 받는 회원). 반려 밖의 알림은 역할로만 정해져 역할마다 한 번 계산한다"""
    by_role: dict[str, list[dict]] = {}
    out: dict[str, tuple[dict, list[users.Member]]] = {}
    for m in users.list_members():
        if m.status != "active" or not m.role:
            continue
        if m.role not in by_role:
            # 화면에서 '확인'(나에게서 지움)을 눌러도 Slack은 보낸다: 역할마다 한 회원의 목록으로 정하므로 확인 여부를 빼고 계산한다
            by_role[m.role] = directives.notices(m, include_dismissed=True)
        for n in by_role[m.role]:
            # 반려 알림은 회원마다 다르고(역할로 정해지지 않음) 반려할 때 [반려] 메시지로 이미 올렸다
            # 회원을 정한 알림(합치기 결과)은 아래에서 그 회원만 부른다
            if n["project"] in projects and n["kind"] != "rejected" and not n.get("members"):
                out.setdefault(notice_key(n), (n, []))[1].append(m)
    people = {m.id: m for m in users.list_members() if m.status == "active"}
    for project in projects:
        for n in directives.member_events(project):
            out.setdefault(notice_key(n), (n, []))[1].extend(people[i] for i in n["members"] if i in people)
    return out


# ---------------------------------------------------------------- 나에게 온 알림 → 프로젝트 채널

def sync_notices() -> int:
    """연결된 프로젝트마다 '나에게 온 알림'의 새 항목을 채널에 하나씩 올린다 (오래된 것부터). 올린 메시지 수"""
    configs = {c["project"]: c for c in connected() if c["channel_id"]}
    if not configs:
        return 0
    pending = _pending(set(configs))
    done = _sent(pending)
    people = {m.id: m for m in users.list_members() if m.status == "active"}
    count = 0
    for key, (n, who) in sorted(pending.items(), key=lambda kv: kv[1][0]["at"]):
        if key in done:
            continue
        config = configs[n["project"]]
        try:
            _post(config, notice_text(n, *notice_mentions(config, who, people)))
        except SlackError as e:
            log.warning("Slack 알림을 올리지 못함 (%s · %s): %s", n["project"], n["title"], e)
            continue
        _mark_sent([key])
        count += 1
    return count


# ---------------------------------------------------------------- 승인·반려 결과

def request_decided(project: str, version: str, stage: str, request: dict | None, decider: users.Member,
                    approved: bool, reason: str = "") -> None:
    """파트장·팀장이 파트원의 수정 완료 승인 요청을 확정(승인)하거나 반려했을 때 → 채널에 요청한 파트원을 멘션.
    반려면 그 초안을 고친 같은 파트 담당자도 멘션한다 (directives.rejected_people)"""
    who = ((request or {}).get("by") or {}).get("id")
    if not who or who == decider.id:
        return
    # 반려 바로 뒤의 초안으로 정한다 (스레드가 보낼 때는 파트원이 벌써 다시 요청해 반려 기록이 지워졌을 수 있다)
    editors = set() if approved else directives.rejected_people(drafts.read(project, version), stage) - {who}

    def post(config: dict) -> None:
        requester = users.get_member(who)
        head = _mention(config, requester) if requester else _esc(request["by"].get("name", ""))
        people = {m.id: m for m in users.list_members() if m.status == "active"}
        targets = [people[i] for i in sorted(editors) if i in people]
        others = _mentions(config, targets)
        head = f"{head} {others}" if others else head
        # 요청한 파트원(·고친 파트원)이 채널 담당자가 아니면 멘션이 안 가므로 알려 둔다
        hint = _missing_hint(_missing(config, ([requester] if requester else []) + targets))
        what = f"{STAGE.get(stage, '')} v{version} 수정 완료 요청"
        if approved:
            after = ("이 기능 명세로 ERD를 만들고 있습니다." if stage == "spec"
                     else "최종본을 만들고 있습니다. 팀장 최종 승인을 기다립니다.")
            text = f"{head} *[승인]* {_esc(decider.name)}님이 {what}을 승인했습니다\n{after}"
        else:
            text = (f"{head} *[반려]* {_esc(decider.name)}님이 {what}을 반려했습니다\n사유: {_esc(reason)}\n"
                    "고친 뒤 다시 '수정 완료 승인 요청'을 보내 주세요.")
        if hint:
            text += f"\n{hint}"
        _post(config, f"{text}\n<{link(project, version, stage)}|화면에서 열기>")

    _enqueue(project, post)


def related(project: str, final: str, tree: directives.Tree | None = None) -> set[int]:
    """최종본에 관여한 회원: 그 최종본과 바탕 ERD·기능 명세 버전을 만든 사람, 초안을 고치거나 승인 요청·확정한 사람"""
    tree = tree or directives.Tree(project)
    record = tree.records.get(final) or {}
    versions = {final, tree.spec_of(final), str(record.get("base") or record.get("parent") or "")} - {""}
    ids: set = set()
    for v in versions:
        ids.add(((tree.records.get(v) or {}).get("created_by") or {}).get("id"))
        ids.update((e.get("by") or {}).get("id") for e in drafts.read(project, v)["log"])
    return {i for i in ids if isinstance(i, int)}


def final_decided(project: str, final: str, approval: dict, decider: users.Member) -> None:
    """팀장이 최종본을 최종 승인하거나 수정 지시(반려)했을 때 → 채널에 관여한 회원(수정 지시면 처리할 파트도)을 멘션"""

    def post(config: dict) -> None:
        tree = directives.Tree(project)
        people = {m.id: m for m in users.list_members() if m.status == "active"}
        who = [people[i] for i in sorted(related(project, final, tree)) if i in people and i != decider.id]
        comment = (approval.get("comment") or "").strip()
        if approval.get("decision") == "approve":
            hint = "" if _mentions(config, who) else _missing_hint(_missing(config, who))
            text = (f"{_mentions(config, who)} *[최종 승인]* {_esc(decider.name)}님이 최종본 v{final}을 승인했습니다".lstrip()
                    + (f"\n의견: {_esc(comment)}" if comment else "")
                    + (f"\n{hint}" if hint else "")
                    + f"\n<{link(project, final, 'final')}|화면에서 열기>")
            _post(config, text)
            return
        # 수정 지시: 지시를 처리할 파트(이 지시의 '수정 지시·수정 대기' 알림을 받는 회원)도 멘션하고, 그 알림은 올린 것으로 친다
        directive = {k: v for k, v in _pending({project}).items()
                     if v[0]["kind"] in ("directive", "waiting") and v[0]["at"] == approval.get("at")}
        who += [m for _, members in directive.values() for m in members if m.id != decider.id]
        targets = approval.get("targets") or []
        record = tree.records.get(final) or {}
        page = "spec" if "spec" in targets else "erd"
        version = tree.spec_of(final) if page == "spec" else str(record.get("base") or record.get("parent") or final)
        lines = [f"{_mentions(config, who)} *[반려 · 수정 지시]* {_esc(decider.name)}님이 최종본 v{final}을 반려하고 "
                 f"{' · '.join(STAGE[t] for t in targets if t in STAGE)} 단계로 돌려보냈습니다".lstrip()]
        if comment:
            lines.append(f"지시: {_esc(comment)}")
        items = approval.get("items") or []
        lines += [f"• {STAGE.get(i.get('stage'), '')} `{_esc(i.get('target_id', ''))}` {_esc(i.get('label') or '')}"
                  + (f" — {_esc(i['note'])}" if i.get("note") else "") for i in items[:10]]
        if len(items) > 10:
            lines.append(f"외 {len(items) - 10}건")
        if not _mentions(config, who) and (hint := _missing_hint(_missing(config, who))):
            lines.append(hint)
        lines.append(f"<{link(project, version, page)}|화면에서 열기>")
        _post(config, "\n".join(lines))
        _mark_sent(directive)

    _enqueue(project, post)


# ---------------------------------------------------------------- 백그라운드 보내기

_outbox: queue.SimpleQueue = queue.SimpleQueue()   # (프로젝트, 채널에 올리는 함수)
_wake = threading.Event()
_stop = threading.Event()
_thread: threading.Thread | None = None


def _enqueue(project: str, post: Callable[[dict], None]) -> None:
    _outbox.put((project, post))
    _wake.set()


def kick() -> None:
    """알림이 바뀌었을 수 있다 (승인 요청·결정·작업 완료·새 프로젝트): 주기를 기다리지 않고 바로 확인"""
    _wake.set()


def flush() -> None:
    """채널이 없는 프로젝트의 채널을 만들고, 쌓인 결과 메시지를 올리고, '나에게 온 알림'을 확인한다
    (백그라운드 스레드가 한 번 도는 일. 테스트는 바로 부른다)"""
    if not enabled():
        return
    ensure_channels()
    while True:
        try:
            project, post = _outbox.get_nowait()
        except queue.Empty:
            break
        config = project_config(project)
        if not config or not config["channel_id"]:
            continue   # Slack을 연결하지 않은 프로젝트
        try:
            post(config)
        except SlackError as e:
            log.warning("Slack 결과 알림을 올리지 못함 (%s): %s", project, e)
    sync_notices()


def _loop() -> None:
    try:
        log.info("Slack 워크스페이스 '%s'에 연결함", check())
    except SlackError as e:
        log.error("Slack 봇 토큰을 확인하지 못함 (.env SLACK_BOT_TOKEN): %s", e)
    while not _stop.is_set():
        _wake.clear()
        try:
            flush()
        except Exception:  # noqa: BLE001 — 알림 실패로 스레드가 멈추지 않게 (다음 주기에 다시)
            log.exception("Slack 알림 처리 중 오류")
        _wake.wait(get_settings().slack_sync_seconds)


def start() -> bool:
    """서버 시작 때 봇 토큰이 있으면 백그라운드 스레드를 띄운다"""
    global _thread
    if not enabled() or (_thread and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="slack-notifier", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
    _wake.set()
    if _thread:
        _thread.join(timeout=5)
