"""프로젝트 삭제 (왼쪽 프로젝트 목록 오른쪽 클릭 → '프로젝트 삭제', 팀장만, 되돌릴 수 없음).

지우는 것: 산출물 저장소의 모든 버전·산출물·프로젝트 파일(초안·알림·저장소 설정 등), OUTPUT_DIR/<프로젝트> 폴더
(최종 개발물 코드·git 저장소·미리보기), 실행 미리보기 컨테이너와 DB 볼륨, 작업 기록, 담당자, Slack 연결 기록.
Slack 채널 자체는 지우지 않는다 (Slack에서 직접 보관).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

from app.agents.storage import get_store
from app.core.config import get_settings
from app.core.exceptions import Conflict, NotFound

log = logging.getLogger(__name__)


def delete_project(project: str, member) -> dict:
    from app.agents.tools import scaffold
    from app.services import jobs, preview
    store = get_store()
    if not store.has_project(project):
        raise NotFound(f"프로젝트가 없습니다: {project}")
    if jobs.running_job(project):
        raise Conflict("이 프로젝트에서 작업이 진행 중입니다. 끝난 뒤 지우세요.")
    removed = []
    # 실행 미리보기: 컨테이너와 DB 볼륨까지 지운다
    try:
        s = preview._get(project)
        if preview.runnable(project) and shutil.which("docker"):
            env = {**os.environ, **(preview._env(s) if s.get("port") and s.get("db_port") else {})}
            subprocess.run([*preview._compose_cmd(project), "down", "-v"], cwd=preview.app_dir(project), capture_output=True, timeout=180, env=env)
            removed.append("실행 미리보기 컨테이너·DB")
        preview._state.pop(project, None)
    except Exception as e:  # noqa: BLE001 - 미리보기 정리 실패가 삭제를 막지 않는다
        log.warning("[삭제] 미리보기를 정리하지 못함 %s: %s", project, e)
    store.delete_project(project)
    removed.append("버전·산출물·초안·알림")
    root = get_settings().output_dir.resolve()
    folder = (root / scaffold.project_name(project)).resolve()
    if root in folder.parents and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
        removed.append("코드 폴더(최종 개발물·git)")
    from sqlalchemy import delete

    from app.models.assignee import ProjectMember
    from app.models.job import JobRecord
    from app.models.notification import SlackNotification, SlackProject
    from app.services import users
    try:   # 담당자·Slack 연결 기록 (회원과 같은 DB: 서비스 DB가 없으면 OUTPUT_DIR의 service.sqlite)
        with users.service_db() as db:
            db.execute(delete(ProjectMember).where(ProjectMember.project == project))
            db.execute(delete(SlackProject).where(SlackProject.project == project))
            db.execute(delete(SlackNotification).where(SlackNotification.key.like(f"%|{project}|%")))
        removed.append("담당자·Slack 연결 기록")
    except Exception as e:  # noqa: BLE001
        log.warning("[삭제] 담당자·Slack 기록을 지우지 못함 %s: %s", project, e)
    for job_id in [k for k, j in jobs._jobs.items() if j.project == project]:
        jobs._jobs.pop(job_id, None)
    try:   # 서비스 DB의 작업 기록 (jobs 테이블은 PostgreSQL 서비스 DB에만 있다 → 없으면 건너뜀)
        with users.service_db() as db:
            db.execute(delete(JobRecord).where(JobRecord.project == project))
        removed.append("작업 기록")
    except Exception as e:  # noqa: BLE001
        log.info("[삭제] 작업 기록을 지우지 못함 (서비스 DB 없음?) %s: %s", project, e)
    log.info("[삭제] %s님이 프로젝트 %s를 지움: %s", member.name, project, ", ".join(removed))
    return {"project": project, "removed": removed}
