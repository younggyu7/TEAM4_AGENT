"""프로젝트 담당자와 보기 권한: 담당자로 고른 회원만 그 프로젝트를 목록·알림·화면(API)에서 본다. PM은 모든 프로젝트를 본다.

- 담당자(project_members 테이블): 업로드할 때 고른 회원과 올린 사람. 프로젝트 화면 '담당자' 카드에서 PM만 바꾼다.
- PM(lead)은 고르지 않아도 모든 프로젝트를 보고 늘 담당자로 친다 (담당자 목록·Slack 채널 초대).
- 담당자를 정한 적이 없는 프로젝트(CLI로 만든 프로젝트 등)는 PM만 본다 → PM이 담당자를 정하면 그 회원도 본다.
- 담당자가 아닌 회원에게는 없는 프로젝트처럼 보인다: 목록·'나에게 온 알림'에서 빠지고 API는 404 (api/v1/deps._project),
  Slack 알림도 담당자만 받는다 (services/slack).
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select

from app.core.auth import PM, role_of
from app.models.assignee import ProjectMember
from app.services import users

ALWAYS = PM   # 모든 프로젝트를 보고 늘 담당자인 역할


def sees_all(member: users.Member) -> bool:
    """모든 프로젝트를 보는 회원 (PM)"""
    return role_of(member.role) == ALWAYS


def always(member: users.Member) -> bool:
    """고르지 않아도 늘 담당자인 회원 (PM)"""
    return role_of(member.role) == ALWAYS


def member_ids(project: str) -> list[int]:
    """고른 담당자 회원 ID (고른 순서). PM은 여기 없어도 담당자다 (assignees)"""
    with users.service_db() as db:
        return list(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project == project)
                               .order_by(ProjectMember.created_at, ProjectMember.user_id)))


def projects_of(member_id: int) -> set[str]:
    """그 회원이 담당자인 프로젝트"""
    with users.service_db() as db:
        return set(db.scalars(select(ProjectMember.project).where(ProjectMember.user_id == member_id)))


def can_see(member: users.Member, project: str) -> bool:
    return sees_all(member) or member.id in member_ids(project)


def visible(member: users.Member, projects: Iterable[str]) -> list[str]:
    """그 회원이 볼 수 있는 프로젝트만 (순서 그대로)"""
    projects = list(projects)
    if sees_all(member):
        return projects
    mine = projects_of(member.id)
    return [p for p in projects if p in mine]


def assignees(project: str) -> list[users.Member]:
    """담당자: 승인된 회원 중 PM과 고른 회원 (회원 목록 순서)"""
    chosen = set(member_ids(project))
    return [m for m in users.list_members() if m.status == "active" and (always(m) or m.id in chosen)]


def set_members(project: str, member_ids: Iterable[int]) -> list[users.Member]:
    """담당자를 고른 회원 전체로 바꾼다 (승인된 회원만, 이미 있던 담당자는 그대로 두고 더하거나 뺀다). 바뀐 담당자"""
    active = {m.id for m in users.list_members() if m.status == "active"}
    chosen = [i for i in dict.fromkeys(int(x) for x in member_ids) if i in active]
    with users.service_db() as db:
        have = set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project == project)))
        db.execute(delete(ProjectMember).where(ProjectMember.project == project, ProjectMember.user_id.not_in(chosen)))
        db.add_all(ProjectMember(project=project, user_id=i) for i in chosen if i not in have)
    return assignees(project)
