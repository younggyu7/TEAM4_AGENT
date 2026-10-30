"""PostgreSQL 어댑터: 산출물·버전 기록·제목을 서비스 DB 테이블(app/models)에 둔다. 트랜잭션은 저장소(app/repositories).

포트 메서드 하나가 트랜잭션 하나다. 버전 번호 잡기는 프로젝트 행을 잠가 동시에 실행해도 번호가 겹치지 않는다.
프로젝트 ID 규칙은 로컬 폴더 어댑터와 같다 ('team/sample'처럼 묶음 아래 경로 가능, '..'·절대 경로는 ValueError).
"""
from __future__ import annotations

from app.core.database import psycopg_url, session_scope
from app.repositories import artifact_repository, project_repository, version_repository


def _check(project: str) -> str:
    parts = project.split("/") if project else []
    if not parts or project.startswith("/") or "\\" in project or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"프로젝트 이름이 올바르지 않습니다: {project!r}")
    return project


def _check_name(name: str) -> str:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"산출물 이름이 올바르지 않습니다: {name!r}")
    return name


class PostgresArtifactStore:
    """설계 산출물을 서비스 DB(PostgreSQL)에 둔다."""

    def __init__(self, url: str):
        # 화면·로그에 보일 위치 (접속 비밀번호는 빼고)
        address = psycopg_url(url).split("@", 1)[-1]
        self.location = f"postgresql://{address}"

    # ---------------------------------------------------------------- 프로젝트
    def list_projects(self) -> list[str]:
        with session_scope() as s:
            return project_repository.with_design(s)

    def has_project(self, project: str) -> bool:
        try:
            _check(project)
        except ValueError:
            return False
        with session_scope() as s:
            return project_repository.exists(s, project)

    def read_project_file(self, project: str, name: str) -> str | None:
        with session_scope() as s:
            return project_repository.read_file(s, _check(project), _check_name(name))

    def write_project_file(self, project: str, name: str, text: str) -> str:
        with session_scope() as s:
            project_repository.write_file(s, _check(project), _check_name(name), text)
        return f"{self.location}/{project}/{name}"

    # ---------------------------------------------------------------- 버전 기록·제목
    def load_version_records(self, project: str) -> list[dict]:
        with session_scope() as s:
            return version_repository.load_records(s, _check(project))

    def save_version_records(self, project: str, records: list[dict]) -> None:
        with session_scope() as s:
            version_repository.save_records(s, _check(project), records)

    def load_titles(self, project: str) -> dict[str, str]:
        with session_scope() as s:
            return version_repository.load_titles(s, _check(project))

    def save_titles(self, project: str, titles: dict[str, str]) -> None:
        with session_scope() as s:
            version_repository.save_titles(s, _check(project), titles)

    # ---------------------------------------------------------------- 버전 공간
    def claim_version(self, project: str, base: str | None = None) -> str:
        with session_scope() as s:
            return version_repository.claim(s, _check(project), base)

    def version_labels(self, project: str) -> set[str]:
        try:
            _check(project)
        except ValueError:
            return set()
        with session_scope() as s:
            return version_repository.labels(s, project)

    def version_location(self, project: str, version: str) -> str:
        return f"{self.location}/{project}/v{version}"

    # ---------------------------------------------------------------- 산출물 파일
    def read(self, project: str, version: str, name: str) -> str | None:
        with session_scope() as s:
            return artifact_repository.read(s, _check(project), version, _check_name(name))

    def write(self, project: str, version: str, name: str, text: str) -> str:
        with session_scope() as s:
            artifact_repository.write(s, _check(project), version, _check_name(name), text)
        return f"{self.version_location(project, version)}/{name}"

    def exists(self, project: str, version: str, name: str) -> bool:
        try:
            _check(project), _check_name(name)
        except ValueError:
            return False
        with session_scope() as s:
            return artifact_repository.exists(s, project, version, name)
