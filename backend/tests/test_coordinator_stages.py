"""단계별 진행 (팀 규칙 3): 요구사항 버전 → 확정 → 기능 명세 v?.1 → 확정 → 데이터 설계 v?.?.1, 비교·대화형 진행 (API 불필요)."""
import json

from fake_llm import FakeLLM
from app import cli
from app.agents.llm import client as llm_module
from app.core.config import get_settings, override_settings
from app.agents.flow.graph import build_graph, open_checkpointer
from app.agents.tools.versions import find_version, load_versions


def _use(fake):
    override_settings(llm_provider="anthropic")   # 키 없이 CLI를 지나게 (실제 호출은 가짜 LLM)
    llm_module.set_llm(fake)
    return fake


def _files(out, version):
    return sorted(p.name for p in (out / "sample" / f"v{version}").iterdir())


def _design(out, version):
    return json.loads((out / "sample" / f"v{version}" / "design.json").read_text(encoding="utf-8"))


def test_requirements_stage_feedback_then_functions_and_data(sample_doc, capsys):
    out = get_settings().output_dir
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--until", "requirements_analyst"]) == 0
    assert fake.calls["FunctionDesignOut"] == 0 and fake.calls["CatalogOut"] == 0, "요구사항에서 멈춤"
    assert _files(out, "1") == ["design.json", "requirements.md", "review_report.md"], "기능 명세·추적표는 아직 없음"
    v1 = _design(out, "1")
    assert v1["completion"]["satisfied"] and v1["meta"]["until"] == "requirements_analyst"
    assert "이 버전은 **요구사항**까지 만들었다" in (out / "sample" / "v1" / "requirements.md").read_text(encoding="utf-8")

    assert cli.main(["confirm", "sample", "v1", "--note", "고객 회의"]) == 0
    assert find_version("sample", "1")["confirmed"] and "rerun function_designer sample --from v1" in capsys.readouterr().out

    # 요구사항 의견 반영 → 다음 최상위 번호 v2 (다시 추출하지 않고 요청 1회)
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "requirements_analyst", "sample", "--from", "v1",
                     "--feedback", "담당자 배정은 이번 범위에서 제외하고 요청 취소 기능을 추가"]) == 0
    assert fake.calls["RequirementFeedbackOut"] == 1 and fake.calls["ExtractionOut"] == 0
    record = find_version("sample", "2")
    assert (record["parent"], record["stage"], record["until"]) == ("1", "requirements_analyst", "requirements_analyst")
    reqs = {r["requirement_id"]: r for r in _design(out, "2")["requirements"]}
    excluded = [r for r in reqs.values() if r["status"] == "EXCLUDED"]
    assert len(excluded) == 1 and "이번 범위에서 제외" in excluded[0]["decisions"][0]
    assert reqs["USR-01"]["source_key"] == "USER" and reqs["USR-01"]["status"] == "CONFIRMED"
    v2 = _design(out, "2")
    assert v2["completion"]["satisfied"], "사용자가 추가한 요구사항은 원문 근거가 없어도 된다"
    req_md = (out / "sample" / "v2" / "requirements.md").read_text(encoding="utf-8")
    assert "## 5. 이전 버전(v1) 대비 변경" in req_md and "- 요구사항: 새 1 · 바뀜 1 · 지움 0" in req_md

    # 요구사항 버전에서 기능 명세: 이어받을 기능 명세가 없으면 처음부터 → v2.1, 뺀 요구사항은 설계하지 않음
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v2"]) == 0
    assert "아직 확정되지 않았습니다" in capsys.readouterr().out
    v21 = _design(out, "2.1")
    linked = {rid for f in v21["functions"] for rid in f["requirement_ids"]}
    assert excluded[0]["requirement_id"] not in linked and "USR-01" in linked
    trace = (out / "sample" / "v2.1" / "traceability.md").read_text(encoding="utf-8")
    assert "범위 제외 (사용자 의견)" in trace and v21["completion"]["satisfied"]

    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v2.1"]) == 0
    assert fake.calls["CatalogOut"] == 1, "이어받을 데이터 모델이 없으면 전체 설계"
    assert (out / "sample" / "v2.1.1" / "schema.sql").exists()
    cli.main(["confirm", "sample", "v2.1"])
    capsys.readouterr()
    assert cli.main(["versions", "sample"]) == 0
    listing = capsys.readouterr().out
    assert "전체→요구사항까지" in listing and "요구사항 단계" in listing and "확정 ✔" in listing


def _full_base(sample_doc):
    out = get_settings().output_dir
    _use(FakeLLM(broken_fk=False))
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "base", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": False}}, cli._config("base"))
    return out


def test_function_stage_from_requirements_version_redesigns_only_changed_groups(sample_doc, capsys):
    out = _full_base(sample_doc)
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "requirements_analyst", "sample", "--from", "v1", "--feedback", "제목 조건을 바꿔 주세요"]) == 0
    assert find_version("sample", "2")["parent"] == "1"
    req_md = (out / "sample" / "v2" / "requirements.md").read_text(encoding="utf-8")
    assert "다음 기능 명세 단계(rerun function_designer)에서 이 기능들의 그룹만 다시 설계" in req_md

    fake = _use(FakeLLM(broken_fk=False))
    capsys.readouterr()
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v2"]) == 0
    printed = capsys.readouterr().out
    assert "v1의 기능 명세를 이어받음 (바뀐 요구사항 1건)" in printed
    assert fake.calls["CommonDecisionsOut"] == 0 and fake.calls["FunctionDesignOut"] == 1, "바뀐 요구사항의 그룹만"
    v21 = _design(out, "2.1")
    assert any("(승인 반영)" in f["name"] for f in v21["functions"]), "결정이 들어간 요구사항으로 다시 설계"

    # 데이터 설계는 기준(v2, 요구사항 버전)에서 위로 올라가 데이터 모델이 있는 v1을 이어받는다
    fake = _use(FakeLLM(broken_fk=False))
    capsys.readouterr()
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v2.1"]) == 0
    assert "v1의 데이터 모델을 이어받아" in capsys.readouterr().out and fake.calls["CatalogOut"] == 0


def test_rerun_requirements_needs_feedback_and_diff_compares_any_two_versions(sample_doc, capsys):
    out = _full_base(sample_doc)
    assert cli.main(["rerun", "requirements_analyst", "sample", "--from", "v1"]) == 2
    assert "--feedback(반영할 의견)이 필요합니다" in capsys.readouterr().out
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--feedback", "배정할 때 사유를 남긴다"]) == 0
    capsys.readouterr()
    assert cli.main(["diff", "sample", "v1", "v1.1"]) == 0
    printed = capsys.readouterr().out
    assert "v1 → v1.1: 요구사항 새 0 · 바뀜 0 · 지움 0 · 기능 새 0 · 바뀜 1 · 지움 0" in printed
    text = (out / "sample" / "diff-v1-v1.1.md").read_text(encoding="utf-8")
    assert text.startswith("# 버전 비교: v1 → v1.1") and "다시 설계: service_request" in text


def test_by_stage_asks_at_each_stage_and_continues_with_reruns(sample_doc, monkeypatch, capsys):
    out = get_settings().output_dir
    _use(FakeLLM(broken_fk=False))
    answers = iter(["1", "2", "배정할 때 사유를 남긴다", "1", "1"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--by-stage"]) == 0
    made = [(v["design_version"], v.get("confirmed", False)) for v in load_versions("sample")]
    assert made == [("1", True), ("1.1", False), ("1.2", True), ("1.2.1", True)], \
        "요구사항 v1 확정 → 기능 명세 v1.1 → 의견으로 v1.2 확정 → 데이터 설계 v1.2.1 확정"
    assert "모든 단계를 확정했습니다" in capsys.readouterr().out


def test_by_stage_needs_terminal(sample_doc, capsys):
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--by-stage"]) == 2
    assert "터미널에서 실행하세요" in capsys.readouterr().out
