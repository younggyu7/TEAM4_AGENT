"""산출물 저장소 포트: 설계 버전 기록(versions.json)과 버전별 산출물 파일을 어디에 두는지 감춘다.

Agent·흐름·API는 이 인터페이스만 쓰고, 실제 저장 위치(로컬 폴더, 나중에 S3·DB 등)는 어댑터가 정한다.
  - 프로젝트: 고객 문서 단위. ID는 'sample'처럼 이름 하나거나 'team/sample'처럼 묶음 아래 경로일 수 있다.
  - 버전: '1', '1.1', '1.1.1' 같은 번호. 번호를 잡은 뒤(claim_version) 그 버전의 산출물 파일을 쓴다.
  - 산출물: 버전마다 이름으로 읽고 쓰는 텍스트 파일 (design.json, schema.sql, functions.md …).
  - 프로젝트 파일: 버전에 속하지 않는 파일 (versions.json, 화면 버전 제목 titles.json, diff 결과).
번호 규칙·기록 정렬 같은 업무 규칙은 포트 밖(tools/versions.py)에 두고, 포트는 읽기·쓰기만 맡는다.
"""
from __future__ import annotations

from typing import Protocol


class ArtifactStore(Protocol):
    """설계 산출물 저장소."""

    #: 사람이 볼 저장 위치 (로그·화면·versions.json의 output_dir)
    location: str

    # ---------------------------------------------------------------- 프로젝트
    def list_projects(self) -> list[str]:
        """버전(design.json이 있는 버전)이 하나라도 있는 프로젝트 ID."""

    def has_project(self, project: str) -> bool:
        """프로젝트가 있는지 (버전 공간이나 프로젝트 파일이 하나라도 있으면 참)."""

    def read_project_file(self, project: str, name: str) -> str | None:
        """프로젝트 파일 (versions.json·titles.json …). 없으면 None."""

    def write_project_file(self, project: str, name: str, text: str) -> str:
        """프로젝트 파일을 쓰고 위치를 돌려준다."""

    # ---------------------------------------------------------------- 버전 공간
    def claim_version(self, project: str, base: str | None = None) -> str:
        """다음 버전 번호를 잡는다. base가 있으면 그 아래 번호(base.1, base.2 …), 없으면 다음 최상위 번호.
        동시에 실행해도 번호가 겹치지 않게 공간을 먼저 차지한다."""

    def version_labels(self, project: str) -> set[str]:
        """공간이 있는 버전 번호 (기록 전에 잡은 번호도 포함)."""

    def version_location(self, project: str, version: str) -> str:
        """그 버전 산출물의 위치 (사람이 볼 문자열)."""

    # ---------------------------------------------------------------- 산출물 파일
    def read(self, project: str, version: str, name: str) -> str | None:
        """버전의 산출물 파일. 없으면 None."""

    def write(self, project: str, version: str, name: str, text: str) -> str:
        """버전의 산출물 파일을 쓰고 위치를 돌려준다."""

    def exists(self, project: str, version: str, name: str) -> bool:
        """버전의 산출물 파일이 있는지."""
