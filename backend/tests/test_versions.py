"""프로젝트 단위 설계 버전: 같은 문서를 다시 실행하면 실행 ID와 관계없이 v1 → v2로 이어진다 (API 불필요)."""
import json

from langgraph.checkpoint.memory import InMemorySaver

from fake_llm import FakeLLM
from app import cli
from app.agents.llm import client as llm_module
from app.core.config import get_settings
from app.agents.flow.graph import build_graph
from app.agents.tools.versions import claim_next_version, load_versions, project_name


def _run(sample_doc, run_id: str, **options):
    llm_module.set_llm(FakeLLM())
    graph = build_graph(InMemorySaver())
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 500}
    graph.invoke({"run_id": run_id, "source_path": str(sample_doc),
                  "options": {"ask_mode": "defer", "validate_db": True, **options}}, config)
    return graph.get_state(config).values


def test_rerun_of_same_document_gets_next_version(sample_doc, capsys):
    out = get_settings().output_dir
    first = _run(sample_doc, "run-a")
    second = _run(sample_doc, "run-b")
    assert (first["design_version"], second["design_version"]) == ("1", "2"), "실행이 달라도 같은 프로젝트면 다음 버전"
    assert (out / "sample" / "v1" / "schema.sql").exists() and (out / "sample" / "v2" / "schema.sql").exists()
    assert not (out / "run-a").exists(), "실행 ID 폴더는 만들지 않음"

    versions = load_versions("sample")
    assert [(v["design_version"], v["run_id"], v["parent"]) for v in versions] == [("1", "run-a", None), ("2", "run-b", "1")]
    assert all(v["db_validation"] == "PASSED" and v["document"] == "sample.txt" for v in versions)
    assert "> 프로젝트: **sample** · 설계 버전: **v2**" in (out / "sample" / "v2" / "review_report.md").read_text(
        encoding="utf-8")
    design = json.loads((out / "sample" / "v2" / "design.json").read_text(encoding="utf-8"))
    assert design["meta"]["project"] == "sample"
    assert "data_model" not in design and "trace_links" not in design, "다른 파일과 겹치는 내용은 넣지 않음"
    assert design["meta"]["files"] == {"data_model": "data_model.json", "trace_links": "traceability.csv"}
    assert (out / "sample" / "v2" / design["meta"]["files"]["data_model"]).exists()

    assert cli.main(["versions", "sample"]) == 0
    printed = capsys.readouterr().out
    assert "v1" in printed and "v2" in printed and "run-b" in printed


def test_project_option_and_name_cleanup(sample_doc):
    state = _run(sample_doc, "run-p", project="화물차 운행관리/1차")
    assert project_name("화물차 운행관리/1차") == "화물차_운행관리_1차"
    assert (get_settings().output_dir / "화물차_운행관리_1차" / "v1").exists() and state["design_version"] == "1"


def test_claim_next_version_skips_taken_numbers():
    root = get_settings().output_dir
    (root / "p" / "v1").mkdir(parents=True)
    (root / "p" / "v3").mkdir()
    assert claim_next_version("p") == "4", "가장 큰 번호 다음"
    assert claim_next_version("p") == "5", "이미 잡힌 번호는 다시 쓰지 않음"
    assert claim_next_version("p", "4") == "4.1", "기준 번호 아래"
