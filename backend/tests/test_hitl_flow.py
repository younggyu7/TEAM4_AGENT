"""화면 흐름 (HITL): 업로드 → 기능 명세 초안에서 멈춤 → 사람이 확정한 명세(--design)로 ERD(의미 검토·데이터 지적 보완)
→ ERD 화면에서 남은 지적 처리 → ERD 최종 확정(DDL 검증·추적 검토·API 설계, 의미 검토 없음)."""
import json

from fake_llm import FakeLLM
from app import cli
from app.agents.flow.state import finding, reconcile_issues
from app.agents.llm import client as llm_module
from app.agents.tools.versions import find_version
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
    assert fake.calls["CatalogOut"] == 1, "처음 내보내기는 ERD 전체 설계"

    # 내보낸 기능 명세를 더 고쳐 다시 내보내기: 고친 명세를 새 기능 명세 v2로 남기고(LLM 없음) 그 ERD v2.1을 만든다.
    # ERD는 앞 기능 명세의 최신 ERD(v1.1)를 이어받아 바뀐 기능의 테이블만 다시 설계한다 (처음부터 만들지 않음)
    confirmed["functions"][1]["business_rules"].append("나중에 추가한 규칙")
    path.write_text(json.dumps(confirmed, ensure_ascii=False), encoding="utf-8")
    fake = _use(FakeLLM(broken_fk=False))
    capsys.readouterr()
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--design", str(path)]) == 0
    assert "v1.1의 데이터 모델을 이어받아 바뀐 기능 1개" in capsys.readouterr().out
    assert fake.calls["CatalogOut"] == 0, "ERD 전체를 다시 설계하지 않는다"
    assert not any(fake.calls[k] for k in ("FunctionDesignOut", "FunctionPatchOut", "FunctionScopeOut")), \
        "새 기능 명세 버전은 화면 수정본 그대로 (LLM 없음)"
    spec, erd = find_version("sample", "2"), find_version("sample", "2.1")
    assert (spec["stage"], spec["parent"]) == ("function_designer", "1"), "새 기능 명세는 최상위 번호"
    assert (erd["stage"], erd["base"]) == ("data_modeler", "2"), "그 기능 명세의 ERD"
    for v in ("2", "2.1"):
        fns = _design(v)["functions"]
        assert fns[0]["name"] == "사람이 확정한 기능 이름" and "나중에 추가한 규칙" in fns[1]["business_rules"]
    assert not (_folder("2") / "data_model.json").exists()


def test_confirmed_answers_are_applied_by_code(tmp_path):
    """'기능 명세 최종 확정' → 내보내기: 새로 답한 확인 질문은 LLM 없이 코드로 반영한다.
    추천과 같은 답은 표시만 '결정'으로, 다른 답은 그 규칙을 '질문 → 답'으로 바꾸고, 요구사항에 결정을 남긴다."""
    from app.agents.flow.rerun import load_submitted_design

    req = {"requirement_id": "R-1", "type": "FUNCTIONAL", "scope": "SYSTEM", "category": "기능", "title": "결재",
           "description": "결재를 처리한다", "status": "ASSUMED"}
    fn = {"function_id": "FN-1", "group": "G", "name": "결재 상신", "description": "", "requirement_ids": ["R-1"],
          "business_rules": ["(확인 필요 FQ-001) 결재선은 최대 3단계로 한다", "(확인 필요 FQ-002) 반려하면 기안자에게 돌려보낸다",
                             "결재 문서는 삭제하지 않는다"]}
    q = lambda qid, text, rec: {"question_id": qid, "kind": "AMBIGUITY", "question": text, "context": "",   # noqa: E731
                                "requirement_ids": ["R-1"], "stage": "function_designer", "recommended": rec,
                                "status": "DEFERRED", "confirms": ["R-1"]}
    base = {"requirements": [req], "functions": [fn],
            "questions": [q("FQ-001", "결재선 최대 단계는?", "3단계"), q("FQ-002", "반려하면 누구에게?", "기안자")]}
    screen = {**base, "questions": [{**base["questions"][0], "status": "ANSWERED", "answer": "3단계"},
                                    {**base["questions"][1], "status": "ANSWERED", "answer": "바로 전 결재자"}]}
    path = tmp_path / "confirmed.json"
    path.write_text(json.dumps(screen, ensure_ascii=False), encoding="utf-8")

    out = load_submitted_design(path, base, "data_modeler")
    assert out["functions"][0]["business_rules"] == [
        "(결정 FQ-001) 결재선은 최대 3단계로 한다",            # 추천과 같은 답: 표시만
        "(결정 FQ-002) 반려하면 누구에게? → 바로 전 결재자",   # 다른 답: 규칙을 답으로
        "결재 문서는 삭제하지 않는다"]
    assert [x["status"] for x in out["questions"]] == ["ANSWERED", "ANSWERED"]
    r = out["requirements"][0]
    assert r["status"] == "CONFIRMED" and any("바로 전 결재자" in d for d in r["decisions"])
    assert "확인 질문 답 2건 코드로 반영 (추천과 다른 답 1건)" in out["note"]


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


def test_erd_stage_reviews_and_fixes_data_and_final_only_records_dispositions(sample_doc):
    override_settings(semantic_review_mode="erd")
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer"]) == 0
    # ERD를 만들 때: 데이터 모델 검토 → 지적과 관련된 테이블만 다시 설계 → 고친 모델을 다시 검토 (기능 명세는 다시 보지 않음)
    assert (fake.calls["ModelReviewOut"], fake.calls["RedesignScopeOut"], fake.calls["FunctionReviewOut"]) == (2, 1, 0)
    assert fake.calls["ApiDesignOut"] == fake.calls["ArchitectureOut"] == 0, "실행 중에는 API 설계를 하지 않는다 (최종 확정 때만)"
    first, again = fake.users["ModelReviewOut"]
    assert "- app_user (" in first and "- app_user (" not in again and "- service_request (" in again, \
        "다시 검토는 고친 테이블만 (다른 테이블은 이름만)"
    v1 = _design("1")
    assert [d["mode"] for d in v1["decisions"] if d["action"] == "data_modeler"] == ["full", "semantic"]
    remark = next(i for i in v1["issues"] if i["kind"] == "SEM_MODEL_INTEGRITY")
    assert (remark["status"], remark["attempts"], remark["resolution"]) == ("OPEN", 1, ""), "한 번 고쳐도 남은 지적은 사람이 처리"
    assert "처리 전" in (_folder("1") / "review_report.md").read_text(encoding="utf-8")

    # ERD 저장(사람이 고친 ERD + 지적 처리): 의미 검토를 다시 하지 않고 지적과 처리를 새 ERD 버전에 이어받는다 (LLM 없음)
    edited = json.loads((_folder("1") / "data_model.json").read_text(encoding="utf-8"))
    edited["entities"][0]["description"] += " (담당자 수정)"
    model_path = get_settings().output_dir / "edited_model.json"
    model_path.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
    later = get_settings().output_dir / "later.json"
    later.write_text(json.dumps({remark["issue_id"]: {"choice": "later", "note": "고객 확인 후"}}), encoding="utf-8")
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["rerun", "data_modeler", "sample", "--from", "v1", "--data-model", str(model_path),
                     "--dispositions", str(later)]) == 0
    assert fake.calls["ModelReviewOut"] == 0, "사람이 고친 ERD는 다시 검토하지 않는다"
    kept = next(i for i in _design("1.1")["issues"] if i["issue_id"] == remark["issue_id"])
    assert (kept["status"], kept["resolution"]) == ("UNRESOLVED", "미해결로 둠: 고객 확인 후")

    # ERD 최종 확정: 의미 검토는 다시 하지 않고 처리를 남긴 뒤 DDL 검증·추적 검토(코드)와 API 설계(LLM)만
    choice = get_settings().output_dir / "choice.json"
    choice.write_text(json.dumps({remark["issue_id"]: {"choice": "not_issue", "note": "앱에서 처리"}}), encoding="utf-8")
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["review", "sample", "v1", "--dispositions", str(choice)]) == 0
    assert fake.calls["ModelReviewOut"] == fake.calls["FunctionReviewOut"] == 0, "최종 확정에서는 의미 검토를 다시 하지 않는다"
    assert (fake.calls["ApiDesignOut"], fake.calls["ArchitectureOut"]) == (1, 1), "API 설계는 최종 확정 때만"
    record = find_version("sample", "1.2")
    assert (record["stage"], record["base"], record["parent"], record["db_validation"]) == ("final", "1", "1", "PASSED")
    final = _design("1.2")
    done = next(i for i in final["issues"] if i["issue_id"] == remark["issue_id"])
    assert (done["status"], done["resolution"]) == ("RESOLVED", "문제 아님: 앱에서 처리")
    assert [(d["action"], d["mode"]) for d in final["decisions"][-4:]] == [
        ("check_completion", "final"), ("db_validate", "final"), ("trace_reviewer", "final"), ("api_designer", "final")]
    assert "문제 아님 1건" in final["decisions"][-4]["result"], "판단 기록에 사람이 정한 처리"
    data = json.loads((_folder("1.2") / "semantic_review.json").read_text(encoding="utf-8"))
    assert [r["resolution"] for r in data["remarks"]] == ["문제 아님: 앱에서 처리"] and data["usage"] == {}
    assert (_folder("1.2") / "db_validation.md").exists()
    report = (_folder("1.2") / "review_report.md").read_text(encoding="utf-8")
    assert f"- 모델: {get_settings().anthropic_model}" in report, "보고서에 그 버전을 만든 모델 (화면 토큰 카드 제목 옆)"
    assert "호출 2회" in report, "최종본의 LLM 사용량은 최종 확정 단계에서 쓴 양 (API 설계 2, 화면 토큰 카드가 읽는 줄)"
    for name in ("API.md", "openapi.yaml", "ARCHITECTURE.md", "TASKS.md"):
        assert (_folder("1.2") / name).exists(), "최종본에 개발 스펙"
    assert not (_folder("1") / "API.md").exists(), "ERD 버전에는 개발 스펙이 없다"
    model = lambda v: json.loads((_folder(v) / "data_model.json").read_text(encoding="utf-8"))   # noqa: E731
    assert model("1.2") == model("1"), "ERD·DDL은 확정한 ERD 그대로"
    assert final["functions"] == _design("1")["functions"]

    # 최종본에서 다시 확정하면 같은 ERD 아래 다음 최종본 (바뀐 기능이 없으면 API 설계도 이어받음)
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["review", "sample", "v1.2"]) == 0
    assert fake.calls["ApiDesignOut"] == fake.calls["ArchitectureOut"] == 0
    assert find_version("sample", "1.3")["base"] == "1"


def test_final_confirmation_is_blocked_by_model_errors(sample_doc, capsys):
    override_settings(semantic_review_mode="off")
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer"]) == 0
    design = _design("1")
    design["issues"].append({"issue_id": "ISS-9999", "kind": "MODEL_NO_PK", "severity": "ERROR", "message": "PK 없음",
                             "target_agent": "data_modeler", "target_id": "service_request", "status": "UNRESOLVED"})
    (_folder("1") / "design.json").write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["review", "sample", "v1"]) == 2
    assert "모델 오류 1건이 남아 최종 확정할 수 없습니다" in capsys.readouterr().out
    assert find_version("sample", "1.1") is None, "최종본을 만들지 않는다"


def test_review_needs_data_model(sample_doc, capsys):
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer", "--until", "function_designer"]) == 0
    capsys.readouterr()
    assert cli.main(["review", "sample", "v1"]) == 2
    assert "데이터 모델(ERD)이 함께 있어야" in capsys.readouterr().out
