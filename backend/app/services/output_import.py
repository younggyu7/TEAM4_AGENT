"""로컬 output 폴더 → 서비스 DB(PostgreSQL) 가져오기 (`python -m app import-output`).

DB로 옮기기 전에 만든 산출물을 화면·rerun에서 계속 쓰게 한다. 여러 번 실행해도 같은 내용으로 덮어쓴다.
  - 프로젝트마다: v<버전>/의 산출물 파일, versions.json(버전 기록, output_dir는 DB 위치로), titles.json(제목),
    그 밖의 프로젝트 파일(diff-*.md)
  - 실행 체크포인트: checkpoints.sqlite의 실행(run_id)마다 마지막 상태와 대기 중 쓰기를 PostgreSQL 체크포인트로
    (rerun·review·resume는 기준 버전을 만든 실행의 마지막 상태에서 시작한다)
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from app.agents.storage import get_store
from app.agents.storage.local import LocalArtifactStore
from app.core.config import get_settings
from app.core.database import psycopg_url

SKIP_PROJECT_FILES = {"versions.json", "titles.json"}


@dataclass
class ImportReport:
    projects: list[str] = field(default_factory=list)
    versions: int = 0
    files: int = 0
    runs: int = 0
    skipped_runs: int = 0


def import_output(source: Path, *, checkpoints: bool = True, overwrite_runs: bool = False) -> ImportReport:
    """source(로컬 output 폴더)의 산출물·기록·체크포인트를 지금 저장소(서비스 DB)로 옮긴다."""
    if not get_settings().database_url:
        raise RuntimeError("DATABASE_URL이 없습니다. 서비스 DB 주소를 .env에 넣고 실행하세요.")
    src, dst, report = LocalArtifactStore(source), get_store(), ImportReport()
    for project in src.list_projects():
        folder = source / project
        for label in sorted(src.version_labels(project), key=lambda v: tuple(map(int, v.split(".")))):
            files = [f for f in (folder / f"v{label}").iterdir() if f.is_file()]
            for f in files:
                dst.write(project, label, f.name, f.read_text(encoding="utf-8"))
            report.versions += 1
            report.files += len(files)
        records = src.load_version_records(project)
        dst.save_version_records(project, [{**r, "output_dir": dst.version_location(project, str(r["design_version"]))}
                                           for r in records])
        dst.save_titles(project, src.load_titles(project))
        for f in folder.iterdir():
            if f.is_file() and f.name not in SKIP_PROJECT_FILES and f.suffix in (".md", ".json", ".csv"):
                dst.write_project_file(project, f.name, f.read_text(encoding="utf-8"))
        report.projects.append(project)
    db_file = source / "checkpoints.sqlite"
    if checkpoints and db_file.exists():
        report.runs, report.skipped_runs = _import_checkpoints(db_file, overwrite_runs)
    return report


def _import_checkpoints(db_file: Path, overwrite: bool) -> tuple[int, int]:
    """실행마다 마지막 체크포인트와 대기 중 쓰기(질문 대기 등)를 PostgreSQL 체크포인트로 복사한다. (복사, 건너뜀)"""
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.checkpoint.sqlite import SqliteSaver

    copied = skipped = 0
    conn = sqlite3.connect(db_file, check_same_thread=False)
    try:
        threads = [r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")]
        source = SqliteSaver(conn)
        with PostgresSaver.from_conn_string(psycopg_url(get_settings().database_url)) as target:
            target.setup()
            for thread in threads:
                config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
                if not overwrite and target.get_tuple(config) is not None:
                    skipped += 1
                    continue
                last = source.get_tuple(config)
                if last is None:
                    continue
                saved = target.put(config, last.checkpoint, last.metadata, last.checkpoint.get("channel_versions") or {})
                writes = defaultdict(list)
                for task_id, channel, value in last.pending_writes or []:
                    writes[task_id].append((channel, value))
                for task_id, items in writes.items():
                    target.put_writes(saved, items, task_id)
                copied += 1
    finally:
        conn.close()
    return copied, skipped
