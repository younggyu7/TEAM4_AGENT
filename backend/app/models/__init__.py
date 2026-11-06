"""서비스 DB 테이블 (SQLAlchemy ORM, 기능별 파일). Alembic이 Base.metadata를 보도록 모두 불러 둔다.

  project.py   projects · project_files      프로젝트, 버전에 속하지 않는 프로젝트 파일
  version.py   design_versions               설계 버전 번호·버전 기록·제목
  artifact.py  artifacts                     버전별 산출물 파일 내용
  job.py       jobs                          작업 진행 기록
  user.py      users · user_sessions         회원·로그인 세션 (역할·권한은 core/auth.py)
  assignee.py  project_members               프로젝트 담당자 (담당자만 그 프로젝트를 봄, PM도 고른 PM만)
  notification.py  slack_projects · slack_notifications  프로젝트별 Slack 채널·채널에 초대한 회원, 채널에 올린 알림
실행 체크포인트 테이블(checkpoints …)은 LangGraph PostgresSaver가 직접 만들고 관리한다 (여기·Alembic 밖).
"""
from app.models.artifact import Artifact
from app.models.assignee import ProjectMember
from app.models.base import Base
from app.models.job import JobRecord
from app.models.notification import SlackNotification, SlackProject
from app.models.project import Project, ProjectFile
from app.models.user import User, UserSession
from app.models.version import DesignVersion

__all__ = ["Artifact", "Base", "DesignVersion", "JobRecord", "Project", "ProjectFile", "ProjectMember", "SlackNotification", "SlackProject",
           "User", "UserSession"]
