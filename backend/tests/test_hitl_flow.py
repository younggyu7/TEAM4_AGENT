"""화면 흐름 (HITL): 업로드 → 기능 명세 초안에서 멈춤 → 사람이 확정한 명세(--design)로 ERD → ERD 최종 확정 때 의미 검토 1회."""
import json

from fake_llm import FakeLLM
from app import cli
from app.agents.flow.state import finding, reconcile_issues
from app.agents.llm import client as llm_module
from app.core.config import Settings, get_settings, override_settings


def _use(fake):
    override_settings(llm_provider="anthropic")   # 키 없이 CLI를 지나게 (실제 호출은 가짜 LLM)
    llm_module.set_llm(fake)
    return fake


def _folder(version):
    return get_settings().output_dir / "sample" / f"v{version}"


def _design(version):
    return json.loads((_folder(version) / "design.json").read_text(encoding="utf-8"))


def test_fix_attempts_per_stage_fall_back_to_max():
    s = Settings(max_fix_attempts=2, func_fix_attempts=1, semantic_review_attempts=1)
    assert s.fix_attempts("function_designer") == 1 and s.fix_attempts("semantic_reviewer") == 1
    assert s.fix_attempts("data_modeler") == 2, "0이면 MAX_FIX_ATTEMPTS"
    assert s.fix_attempts("requirements_analyst") == 2

    owns = lambda k: k.startswith("TRACE_")   # noqa: E731
    first = reconcile_issues([], [finding("TRACE_X", "ERROR", "x", "function_designer", "R-1")], owns, s.fix_attempts)
    tried = [first[0].model_copy(update={"attempts": 1})]
    again = reconcile_issues(tried, [finding("TRACE_X", "ERROR", "x", "function_designer", "R-1")], owns, s.fix_attempts)
    assert again[0].status == "UNRESOLVED", "기능 명세 한도(1회)에 닿으면 더 고치지 않는다"


def test_draft_stops_at_functions_and_erd_uses_confirmed_spec(sample_doc, capsys):
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--until", "function_designer"]) == 0
    assert not (_folder("1") / "data_model.json").exists(), "초안은 기능 명세에서 멈춘다"

    # 화면에서 고치고 확정한 명세: 첫 기능 이름을 바꾼다
    confirmed = _design("1")
    confirmed["functions"][0]["name"] = "사람이 확정한 기능 이름"
    path = get_settings().output_dir / "confirmed.json"
    path.write_text(json.dumps(confirmed, ensure_ascii=False), encoding="utf-8")

    fake = _use(FakeLLM(broken_fk=False))
    capsys.readouterr()
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--design", str(path)]) == 0
    assert "화면에서 확정한 명세 사용 (기능" in capsys.readouterr().out
    v11 = _design("1.1")
    assert v11["functions"][0]["name"] == "사람이 확정한 기능 이름"
    assert (_folder("1.1") / "data_model.json").exists()
    assert fake.calls["FunctionDesignOut"] == 0, "ERD 단계는 확정한 기능 명세를 다시 설계하지 않는다"


def test_design_option_rejects_unknown_requirement(sample_doc, capsys):
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--until", "function_designer"]) == 0
    broken = _design("1")
    broken["functions"][0]["requirement_ids"] = ["REQ-NOPE"]
    path = get_settings().output_dir / "broken.json"
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--design", str(path)]) == 2
    assert "기준 버전에 없는 요구사항 ID: REQ-NOPE" in capsys.readouterr().out
    assert cli.main(["rerun", "function_designer", "sample", "--from", "v1", "--design", str(path), "--full"]) == 2


def test_semantic_final_mode_reviews_only_when_confirmed(sample_doc):
    override_settings(semantic_review_mode="final")
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer"]) == 0
    assert fake.calls["FunctionReviewOut"] == fake.calls["ModelReviewOut"] == 0, "실행 중에는 의미 검토를 하지 않는다"
    assert "'ERD 최종 확정' 때 한 번 검토" in (_folder("1") / "review_report.md").read_text(encoding="utf-8")

    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["review", "sample", "v1"]) == 0
    assert fake.calls["ModelReviewOut"] == 1 and fake.calls["FunctionReviewOut"] == 0, "기본은 ERD ↔ 기능 명세만"
    data = json.loads((_folder("1") / "semantic_review.json").read_text(encoding="utf-8"))
    assert data["parts"] == ["model"] and data["failed"] == [] and data["usage"]["calls"] == 1
    assert (_folder("1") / "semantic_review.md").read_text(encoding="utf-8").startswith("# 의미 검토 (최종 확정)")

    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["review", "sample", "v1", "--source"]) == 0
    assert fake.calls["FunctionReviewOut"] == 1, "--source면 기능 명세 ↔ 요구사항도"


def test_review_needs_data_model(sample_doc, capsys):
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--until", "function_designer"]) == 0
    capsys.readouterr()
    assert cli.main(["review", "sample", "v1"]) == 2
    assert "데이터 모델(ERD)이 함께 있어야" in capsys.readouterr().out
