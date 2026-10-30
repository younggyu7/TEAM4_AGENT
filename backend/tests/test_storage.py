"""산출물 저장소 포트(agents/storage): 로컬 폴더 어댑터, 그리고 다른 어댑터로 바꿔 끼워도 흐름이 그대로 도는지."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from fake_llm import FakeLLM
from app import cli
from app.agents.llm import client as llm_module
from app.agents.storage import LocalArtifactStore, get_store, set_store
from app.agents.tools.versions import find_version, load_versions
from app.core.config import get_settings, override_settings
from app.main import app


class MemoryArtifactStore:
    """메모리에 두는 어댑터 (포트만 맞추면 저장 위치를 바꿀 수 있는지 확인용)."""

    def __init__(self):
        self.location = "memory://"
        self.files: dict[tuple[str, str | None, str], str] = {}   # (프로젝트, 버전 또는 None, 이름) → 내용
        self.versions: dict[str, set[str]] = {}

    def list_projects(self):
        return sorted(p for p, vs in self.versions.items() if any((p, v, "design.json") in self.files for v in vs))

    def has_project(self, project):
        return project in self.versions or any(k[0] == project for k in self.files)

    def read_project_file(self, project, name):
        return self.files.get((project, None, name))

    def write_project_file(self, project, name, text):
        self.files[(project, None, name)] = text
        return f"memory://{project}/{name}"

    def load_version_records(self, project):
        return json.loads(self.files.get((project, None, "versions.json"), "[]"))

    def save_version_records(self, project, records):
        self.files[(project, None, "versions.json")] = json.dumps(records, ensure_ascii=False)

    def load_titles(self, project):
        return json.loads(self.files.get((project, None, "titles.json"), "{}"))

    def save_titles(self, project, titles):
        self.files[(project, None, "titles.json")] = json.dumps(titles, ensure_ascii=False)

    def claim_version(self, project, base=None):
        prefix = f"{base}." if base else ""
        taken = [int(v[len(prefix):]) for v in self.versions.get(project, set())
                 if v.startswith(prefix) and v[len(prefix):].isdigit()]
        label = f"{prefix}{max(taken, default=0) + 1}"
        self.versions.setdefault(project, set()).add(label)
        return label

    def version_labels(self, project):
        return set(self.versions.get(project, set()))

    def version_location(self, project, version):
        return f"memory://{project}/v{version}"

    def read(self, project, version, name):
        return self.files.get((project, version, name))

    def write(self, project, version, name, text):
        self.versions.setdefault(project, set()).add(version)
        self.files[(project, version, name)] = text
        return f"memory://{project}/v{version}/{name}"

    def exists(self, project, version, name):
        return (project, version, name) in self.files


def test_local_store_reads_writes_and_blocks_outside(tmp_path):
    store = LocalArtifactStore(tmp_path)
    assert store.claim_version("p") == "1" and store.claim_version("p", "1") == "1.1"
    store.write("p", "1", "design.json", "{}")
    store.write("team/q", "1", "design.json", "{}")
    assert store.read("p", "1", "design.json") == "{}" and store.read("p", "1", "schema.sql") is None
    assert store.list_projects() == ["p", "team/q"], "묶음 폴더 아래 프로젝트도 찾는다"
    assert store.version_labels("p") == {"1", "1.1"}
    store.write_project_file("p", "titles.json", "{}")
    assert store.read_project_file("p", "titles.json") == "{}"
    with pytest.raises(ValueError):
        store.read("../outside", "1", "design.json")
    with pytest.raises(ValueError):
        store.write("p", "1", "../../escape.txt", "x")
    assert not store.has_project("../outside")


def test_flow_runs_on_another_store_adapter(sample_doc):
    """메모리 어댑터로 바꿔 끼우면 기능 명세 → ERD(--design) → 최종본 → 화면 API까지 디스크에 산출물을 쓰지 않고 돈다."""
    memory = MemoryArtifactStore()
    set_store(memory)
    override_settings(llm_provider="anthropic", semantic_review_mode="final")
    llm_module.set_llm(FakeLLM(broken_fk=False))
    assert get_store() is memory

    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--until", "function_designer"]) == 0
    assert find_version("sample", "1")["output_dir"] == "memory://sample/v1"
    design = json.loads(memory.read("sample", "1", "design.json"))
    path = get_settings().output_dir / "confirmed.json"   # 화면이 보낸 수정본은 저장소 밖 입력 파일
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--design", str(path)]) == 0
    assert cli.main(["review", "sample", "v1.1"]) == 0
    assert [v["design_version"] for v in load_versions("sample")] == ["1", "1.1", "1.1.1"]
    assert memory.exists("sample", "1.1", "schema.sql") and memory.exists("sample", "1.1.1", "semantic_review.json")
    assert not any(p.name.startswith("v") for p in (get_settings().output_dir / "sample").glob("*")) \
        if (get_settings().output_dir / "sample").exists() else True, "산출물 폴더를 만들지 않는다"

    client = TestClient(app)
    body = client.get("/api/v1/projects").json()
    assert body["output_dir"] == "memory://" and body["projects"][0]["id"] == "sample"
    assert client.get("/api/v1/design?project=sample&version=1.1").json()["data_model"]["entities"]
    sql = client.get("/api/v1/file?project=sample&version=1.1&name=schema.sql")
    assert sql.status_code == 200 and re.search(r"CREATE TABLE", sql.text)
    assert client.post("/api/v1/projects/title", json={"project": "sample", "version": "1.1", "title": "확정"}).json()["ok"]
    assert json.loads(memory.read_project_file("sample", "titles.json")) == {"1.1": "확정"}
