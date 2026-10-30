"""서비스 DB(PostgreSQL): Alembic 마이그레이션, PostgreSQL 저장소 어댑터·체크포인트·작업 기록, output 가져오기.

TEST_DATABASE_URL이 있을 때만 돈다 (없으면 건너뜀). 로컬: TEAM4_AGENT에서 docker compose up -d db 후
  TEST_DATABASE_URL=postgresql://reqagent:reqagent@localhost:55432/reqagent_test pytest tests/test_postgres.py
테스트마다 테이블을 지우고 다시 만든다 (테스트 전용 DB만 쓸 것).
"""
import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fake_llm import FakeLLM

TEST_URL = os.getenv("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL이 없어 PostgreSQL 테스트를 건너뜀")
BACKEND = Path(__file__).resolve().parents[1]


def _alembic(action: str, target: str):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    getattr(command, action)(cfg, target)


@pytest.fixture
def pg(tmp_path):
    """테스트 DB를 비우고 최신 테이블로 올린다. 설정은 DATABASE_URL=테스트 DB."""
    from sqlalchemy import create_engine, text

    from app.core import config
    from app.core.database import sqlalchemy_url

    config._settings = config.Settings(output_dir=tmp_path / "output", database_url=TEST_URL, validation_db_url="",
                                       llm_provider="anthropic")
    engine = create_engine(sqlalchemy_url(TEST_URL))
    with engine.begin() as conn:   # 이전 테스트가 남긴 것 지우기 (Alembic·LangGraph 테이블 포함)
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    engine.dispose()
    _alembic("upgrade", "head")
    from app.agents.flow import graph
    graph._PG_READY.clear()        # 체크포인트 테이블도 다시 만들게
    yield TEST_URL


def test_migration_matches_models_and_downgrades(pg):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    from app.core.database import sqlalchemy_url
    from app.models import Base

    engine = create_engine(sqlalchemy_url(pg))
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], "app/models와 마이그레이션 결과가 같다"
    _alembic("downgrade", "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    _alembic("upgrade", "head")
    assert {"projects", "design_versions", "artifacts", "project_files", "jobs"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_postgres_store_reads_writes_and_blocks_outside(pg):
    from app.agents.storage import get_store
    from app.agents.storage.postgres import PostgresArtifactStore

    store = get_store()
    assert isinstance(store, PostgresArtifactStore) and "secret" not in store.location
    assert store.claim_version("p") == "1" and store.claim_version("p", "1") == "1.1" and store.claim_version("p") == "2"
    store.write("p", "1", "design.json", "{}")
    store.write("team/q", "1", "design.json", "{}")
    assert store.read("p", "1", "design.json") == "{}" and store.read("p", "1", "schema.sql") is None
    assert store.list_projects() == ["p", "team/q"]
    assert store.version_labels("p") == {"1", "1.1", "2"}
    store.save_version_records("p", [{"design_version": "1", "parent": None}, {"design_version": "1.1", "parent": "1"}])
    assert [r["design_version"] for r in store.load_version_records("p")] == ["1", "1.1"]
    store.save_titles("p", {"1.1": "확정"})
    assert store.load_titles("p") == {"1.1": "확정"}
    store.write_project_file("p", "diff-v1-v1.1.md", "# 비교")
    assert store.read_project_file("p", "diff-v1-v1.1.md") == "# 비교"
    for bad in ("../outside", "/abs", "a/../b"):
        with pytest.raises(ValueError):
            store.read(bad, "1", "design.json")
        assert not store.has_project(bad)
    with pytest.raises(ValueError):
        store.write("p", "1", "../escape", "x")


def test_claim_version_is_unique_under_concurrency(pg):
    from app.agents.storage import get_store

    store, got, errors = get_store(), [], []

    def claim():
        try:
            got.append(store.claim_version("race"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sorted(got, key=int) == [str(i) for i in range(1, 9)], "동시에 잡아도 번호가 겹치지 않는다"


def test_full_flow_on_postgres(pg, sample_doc):
    """기능 명세 초안 → 확정 명세로 ERD → 최종본 → 화면 API. 산출물·기록·체크포인트 모두 서비스 DB."""
    from sqlalchemy import create_engine, text

    from app import cli
    from app.agents.llm import client as llm_module
    from app.core.config import get_settings, override_settings
    from app.core.database import sqlalchemy_url
    from app.main import app

    override_settings(semantic_review_mode="final")
    llm_module.set_llm(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--until", "function_designer"]) == 0
    client = TestClient(app)
    design = client.get("/api/v1/design?project=sample&version=1").json()
    path = get_settings().output_dir / "confirmed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--design", str(path)]) == 0
    assert cli.main(["review", "sample", "v1.1"]) == 0

    body = client.get("/api/v1/projects").json()
    assert body["output_dir"].startswith("postgresql://") and "reqagent:reqagent" not in body["output_dir"]
    assert [v["design_version"] for v in body["projects"][0]["versions"]] == ["1.1.1", "1.1", "1"]
    assert client.get("/api/v1/file?project=sample&version=1.1&name=schema.sql").text.count("CREATE TABLE") >= 1
    assert client.get("/api/v1/file?project=sample&version=1.1.1&name=semantic_review.json").status_code == 200
    assert client.post("/api/v1/projects/title", json={"project": "sample", "version": "1.1", "title": "확정"}).json()["ok"]
    assert client.get("/api/v1/projects").json()["projects"][0]["versions"][1]["title"] == "확정"
    assert not (get_settings().output_dir / "sample").exists(), "산출물 폴더를 만들지 않는다"
    assert not (get_settings().output_dir / "checkpoints.sqlite").exists(), "체크포인트도 서비스 DB"
    engine = create_engine(sqlalchemy_url(pg))
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from artifacts where project_id='sample'")).scalar() > 20
        assert conn.execute(text("select count(*) from checkpoints")).scalar() > 0
    engine.dispose()


def test_jobs_survive_restart_and_orphans_close(pg):
    from app.services import jobs

    job = jobs._new_job(project="p", base="1", source="erd", message="ERD v1에서 새 버전을 만드는 중")
    jobs._jobs.clear()   # 서버를 다시 띄운 것처럼 메모리 기록을 비운다
    saved = jobs.get_job(job.id)
    assert saved and saved.status == "running" and saved.message.startswith("ERD v1")
    assert jobs.close_orphaned_jobs() == 1
    closed = jobs.get_job(job.id)
    assert closed.status == "failed" and "서버가 다시 시작" in closed.message and closed.finished_at


def test_import_output_moves_local_results_and_rerun_continues(pg, sample_doc, tmp_path):
    """DB로 옮기기 전에 로컬 폴더에 만든 결과를 가져오면 화면에 같이 보이고, 가져온 체크포인트에서 rerun이 이어진다."""
    from app import cli
    from app.agents.llm import client as llm_module
    from app.agents.tools.versions import find_version
    from app.core import config
    from app.main import app

    local = tmp_path / "local-output"
    config._settings = config.Settings(output_dir=local, database_url="", validation_db_url="", llm_provider="anthropic")
    llm_module.set_llm(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer"]) == 0
    (local / "sample" / "titles.json").write_text(json.dumps({"1": "로컬 제목"}, ensure_ascii=False), encoding="utf-8")
    local_files = sorted(p.name for p in (local / "sample" / "v1").iterdir())

    config._settings = config.Settings(output_dir=tmp_path / "db-run", database_url=pg, validation_db_url="",
                                       llm_provider="anthropic")
    assert cli.main(["import-output", "--from", str(local)]) == 0
    assert cli.main(["import-output", "--from", str(local)]) == 0, "여러 번 실행해도 된다"
    record = find_version("sample", "1")
    assert record["output_dir"].startswith("postgresql://")
    client = TestClient(app)
    versions = client.get("/api/v1/projects").json()["projects"][0]["versions"]
    assert versions[0]["design_version"] == "1" and versions[0]["title"] == "로컬 제목"
    for name in ("design.json", "schema.sql", "functions.md"):
        assert name in local_files and client.get(f"/api/v1/file?project=sample&version=1&name={name}").status_code == 200

    fake = FakeLLM(broken_fk=False)
    llm_module.set_llm(fake)
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--full"]) == 0, "가져온 체크포인트에서 이어진다"
    assert find_version("sample", "1.1")["parent"] == "1" and fake.calls["CatalogOut"] == 1
