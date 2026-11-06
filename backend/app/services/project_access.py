"""프로젝트 담당자와 보기 권한: 담당자로 고른 회원만 그 프로젝트를 목록·알림·화면(API)에서 본다.

- 담당자(project_members 테이블): 업로드할 때 고른 회원과 올린 사람. PM도 고른 PM만 담당자다 — 담당자에는 PM이 최소 한 명
  있어야 한다 (업로드·담당자 바꾸기 때 검사, NEED_PM). 프로젝트 화면 '담당자' 카드에서 PM만 바꾼다.
- PM을 고른 적이 없는 프로젝트(이 규칙 전에 만든 프로젝트, CLI로 만든 프로젝트, 고른 PM이 모두 사용 중지됨)는 승인된 PM 모두가
  보고 담당자로 친다 → PM이 담당자를 다시 저장하면 고른 PM만 남는다.
- 담당자가 아닌 회원에게는 없는 프로젝트처럼 보인다: 목록·'나에게 온 알림'에서 빠지고 API는 404 (api/v1/deps._project),
  Slack 알림도 담당자만 받는다 (services/slack).
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select

from app.core.auth import PM, role_of
from app.models.assignee import ProjectMember
from app.services import users

NEED_PM = "PM 최소 한 명은 필요합니다."


def is_pm(member: users.Member) -> bool:
    return role_of(member.role) == PM


def _active() -> list[users.Member]:
    return [m for m in users.list_members() if m.status == "active"]


def _pm_ids() -> set[int]:
    """승인된 PM 회원 ID"""
    return {m.id for m in _active() if is_pm(m)}


def has_pm(member_ids: Iterable[int]) -> bool:
    """고른 회원 중 승인된 PM이 있는지 (담당자는 PM 최소 한 명)"""
    pms = _pm_ids()
    return any(int(i) in pms for i in member_ids)


def member_ids(project: str) -> list[int]:
    """고른 담당자 회원 ID (고른 순서). PM을 고른 적이 없으면 PM 모두가 담당자다 (assignees)"""
    with users.service_db() as db:
        return list(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project == project)
                               .order_by(ProjectMember.created_at, ProjectMember.user_id)))


def projects_of(member_id: int) -> set[str]:
    """그 회원을 담당자로 고른 프로젝트"""
    with users.service_db() as db:
        return set(db.scalars(select(ProjectMember.project).where(ProjectMember.user_id == member_id)))


def _projects_with_pm() -> set[str]:
    """승인된 PM을 담당자로 고른 프로젝트 (나머지 프로젝트는 PM 모두가 본다)"""
    pms = _pm_ids()
    if not pms:
        return set()
    with users.service_db() as db:
        return set(db.scalars(select(ProjectMember.project).where(ProjectMember.user_id.in_(pms)).distinct()))


def can_see(member: users.Member, project: str) -> bool:
    chosen = set(member_ids(project))
    return member.id in chosen or (is_pm(member) and not chosen & _pm_ids())


def visible(member: users.Member, projects: Iterable[str]) -> list[str]:
    """그 회원이 볼 수 있는 프로젝트만 (순서 그대로)"""
    projects = list(projects)
    mine = projects_of(member.id)
    if not is_pm(member):
        return [p for p in projects if p in mine]
    with_pm = _projects_with_pm()
    return [p for p in projects if p in mine or p not in with_pm]


def assignees(project: str) -> list[users.Member]:
    """담당자: 승인된 회원 중 고른 회원 (PM을 고른 적이 없으면 PM 모두도). 회원 목록 순서"""
    chosen = set(member_ids(project))
    active = _active()
    any_pm = any(m.id in chosen and is_pm(m) for m in active)
    return [m for m in active if m.id in chosen or (not any_pm and is_pm(m))]


def set_members(project: str, member_ids: Iterable[int]) -> list[users.Member]:
    """담당자를 고른 회원 전체로 바꾼다 (승인된 회원만, 이미 있던 담당자는 그대로 두고 더하거나 뺀다). 바뀐 담당자.
    PM이 있는지는 부르는 쪽(API)이 has_pm으로 먼저 본다"""
    active = {m.id for m in _active()}
    chosen = [i for i in dict.fromkeys(int(x) for x in member_ids) if i in active]
    with users.service_db() as db:
        have = set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project == project)))
        db.execute(delete(ProjectMember).where(ProjectMember.project == project, ProjectMember.user_id.not_in(chosen)))
        db.add_all(ProjectMember(project=project, user_id=i) for i in chosen if i not in have)
    return assignees(project)
