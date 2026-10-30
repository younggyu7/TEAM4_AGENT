"""개발 스펙 (API 설계 Agent + 고정 형식 문서 9개): 'ERD 최종 확정'(최종본)에만 생기고, 기술 스택 질문은 추천안으로 가정,
바뀐 기능만 다시 설계, 형식 고정 (API 불필요)."""
import json
import re
from pathlib import Path

import yaml

from fake_llm import FakeLLM
from app import cli
from app.agents.llm import client as llm_module
from app.agents.roles import api_designer
from app.agents.roles.api_designer import ArchitectureOut, ApiDesignOut, STACK_OPTIONS, STACK_QUESTION
from app.agents.tools.dev_spec import FILES
from app.core.config import get_settings, override_settings
from app.schemas.data_model import Attribute, DataModel, Entity
from app.schemas.function import FunctionSpec


def _use(fake):
    override_settings(llm_provider="anthropic", semantic_review_mode="final")
    llm_module.set_llm(fake)
    return fake


def _folder(version):
    return get_settings().output_dir / "sample" / f"v{version}"


def _final(sample_doc, fake, base_fake=None) -> Path:
    """기능 명세·ERD까지 만든 v1에서 'ERD 최종 확정' → 최종본 v1.1."""
    _use(base_fake or FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer"]) == 0
    _use(fake)
    assert cli.main(["review", "sample", "v1"]) == 0
    return _folder("1.1")


def test_dev_spec_files_are_written_only_in_the_final_version(sample_doc):
    fake = FakeLLM(broken_fk=False)
    out = _final(sample_doc, fake)
    assert all((out / name).exists() for name in FILES), "최종본에 개발 스펙 9개"
    assert not any((_folder("1") / name).exists() for name in FILES), "ERD 버전에는 없다 (API 설계는 최종 확정 때만)"
    design = json.loads((out / "design.json").read_text(encoding="utf-8"))
    assert design["meta"]["dev_spec"]["format"] == "spec-format v1"

    spec = yaml.safe_load((out / "openapi.yaml").read_text(encoding="utf-8"))
    ops = [op for item in spec["paths"].values() for op in item.values()]
    assert len(ops) == len(design["functions"]), "기능마다 API"
    assert all(op["x-function-id"].startswith("FN-") and op["operationId"] for op in ops)
    columns = [p["x-db-column"] for op in ops
               for p in op["requestBody"]["content"]["application/json"]["schema"]["properties"].values()]
    ddl = (out / "schema.sql").read_text(encoding="utf-8")
    assert columns and all(f"CREATE TABLE {c.split('.')[0]}" in ddl for c in columns), "필드의 DB 컬럼은 schema.sql 이름"

    agents = (out / "AGENTS.md").read_text(encoding="utf-8")
    assert "spec-format v1" in agents and "Python (원문)" in agents
    tasks = (out / "TASKS.md").read_text(encoding="utf-8")
    assert "TASK-001 · 프로젝트 뼈대" in tasks and "TASK-002 · DB 생성" in tasks and "FN-C01-01" in tasks
    acceptance = (out / "ACCEPTANCE_CRITERIA.md").read_text(encoding="utf-8")
    assert re.search(r"\| AC-FN-C01-01-1 \| 정상 \|", acceptance)
    assert re.search(r"완료 기준: AC-FN-C01-01-1( ~ AC-FN-C01-01-\d+)? \(\d+개\) 테스트 통과", tasks), "작업의 완료 기준은 AC ID"
    assert "ADR-001" in (out / "DECISIONS.md").read_text(encoding="utf-8")


def test_format_document_lists_every_section_the_code_writes(sample_doc):
    """SPEC_FORMAT.md(사용자가 보는 형식 정의)와 실제로 쓰는 문서의 절 제목이 어긋나지 않게 한다."""
    out = _final(sample_doc, FakeLLM(broken_fk=False))
    format_doc = (Path(__file__).resolve().parents[1] / "SPEC_FORMAT.md").read_text(encoding="utf-8")
    assert "spec-format v1" in format_doc
    for name in FILES:
        if name.endswith(".md"):
            for heading in re.findall(r"^## (.+)$", (out / name).read_text(encoding="utf-8"), re.M):
                assert heading in format_doc, f"{name}의 절 '{heading}'이 SPEC_FORMAT.md에 없음"


def test_reconfirming_reuses_api_design_and_writes_the_same_documents(sample_doc):
    out = _final(sample_doc, FakeLLM(broken_fk=False))
    fake = _use(FakeLLM(broken_fk=False))
    assert cli.main(["review", "sample", "v1"]) == 0
    assert fake.calls["ApiDesignOut"] == fake.calls["ArchitectureOut"] == 0, "바뀐 기능이 없으면 LLM 없이 이전 최종본 것을 그대로"
    for name in FILES:
        a = (out / name).read_text(encoding="utf-8").replace("v1.1", "vX")
        b = (_folder("1.2") / name).read_text(encoding="utf-8").replace("v1.2", "vX")
        assert a == b, f"{name}이 같은 입력에서 달라짐"


class _NoStackLLM(FakeLLM):
    """원문이 개발 언어·프레임워크를 정하지 않은 문서 (추천은 2번)."""

    def _ArchitectureOut(self, user):
        return ArchitectureOut(summary="웹 서버와 DB.", recommended_stack=2, recommend_reason="데이터 분석 기능이 많다")


def test_stack_question_is_assumed_with_the_recommendation_without_stopping(sample_doc):
    out = _final(sample_doc, _NoStackLLM(broken_fk=False))
    design = json.loads((out / "design.json").read_text(encoding="utf-8"))
    q = next(q for q in design["questions"] if q["question_id"] == STACK_QUESTION)
    assert (q["status"], q["stage"], q["recommended"]) == ("DEFERRED", "api_designer", STACK_OPTIONS[1]), \
        "최종 확정은 질문에서 멈추지 않는다 (추천안으로 가정)"
    architecture = (out / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert f"{STACK_OPTIONS[1]} (가정 — 추천안, 확인 필요 {STACK_QUESTION})" in architecture


def _fn(fid, name="기능"):
    return FunctionSpec(function_id=fid, group="G1", name=name, description="", requirement_ids=["R-1"],
                        inputs=[], outputs=[])


def test_only_functions_whose_spec_or_tables_changed_are_redesigned():
    model = DataModel(entities=[
        Entity(entity_id="request", label="요청", function_ids=["FN-1"], attributes=[Attribute(name="title", label="제목")]),
        Entity(entity_id="member", label="회원", function_ids=["FN-2"], attributes=[Attribute(name="name", label="이름")]),
    ])
    functions = [_fn("FN-1"), _fn("FN-2")]
    prints = {f.function_id: api_designer.fingerprint(f, model) for f in functions}
    state = {"functions": [f.model_dump() for f in functions], "data_model": model.model_dump(),
             "api_design": {"fingerprints": prints}, "options": {}}
    assert api_designer.pending(state) == []
    model.entities[0].attributes.append(Attribute(name="memo", label="메모"))
    state["data_model"] = model.model_dump()
    assert api_designer.pending(state) == ["FN-1"], "바뀐 테이블을 쓰는 기능만"
    marked = functions[1].model_copy(update={"business_rules": ["(결정 FQ-001) 이름은 필수"]})
    functions[1] = functions[1].model_copy(update={"business_rules": ["(확인 필요 FQ-001) 이름은 필수"]})
    assert api_designer.fingerprint(marked, model) == api_designer.fingerprint(functions[1], model), \
        "규칙 앞 표시만 바뀐 것은 다시 설계하지 않음"


def _op(method, path, op_id, *columns):
    return {"method": method, "path": path, "operation_id": op_id, "summary": op_id, "actors": [],
            "request": [{"name": c.split(".")[1], "column": c, "type": "string", "required": True, "description": ""}
                        for c in columns], "response": [], "list_response": False, "errors": []}


def test_same_route_of_different_tables_is_split_and_of_same_table_is_shared():
    tables = {"approval_document", "approval_document_instance"}
    stored = {
        "FN-1": {"operations": [_op("POST", "/approval-documents", "createDoc", "approval_document.title"),
                                _op("POST", "/approval-documents/{approvalDocumentId}/approve", "approveDoc")]},
        "FN-2": {"operations": [_op("POST", "/approval-documents", "submitDoc", "approval_document_instance.title"),
                                _op("POST", "/approval-documents/{approvalDocumentInstanceId}/process", "processDoc")]},
        "FN-3": {"operations": [_op("POST", "/approval-documents", "createDoc2", "approval_document.title")]},
    }
    moved = api_designer._split_routes(stored, ["FN-1", "FN-2", "FN-3"], tables)
    assert [op["path"] for op in stored["FN-2"]["operations"]] == \
        ["/approval-document-instances", "/approval-document-instances/{approvalDocumentInstanceId}/process"], \
        "다른 테이블의 API는 그 테이블 이름 경로로 (같은 조각 아래 같은 테이블 API도 함께)"
    assert [op["path"] for op in stored["FN-1"]["operations"]] == \
        ["/approval-documents", "/approval-documents/{approvalDocumentId}/approve"], "경로 이름과 맞는 테이블은 그대로"
    assert len(moved) == 2
    assert stored["FN-3"]["operations"][0]["shared_with"] == "createDoc", "같은 테이블이면 앞 기능의 API를 함께 씀"
    assert api_designer._split_routes(stored, ["FN-1", "FN-2", "FN-3"], tables) == [], "다시 돌려도 같다"


class _BadColumnLLM(FakeLLM):
    """첫 API 설계는 없는 컬럼을 쓰고, 검사 오류를 받아 다시 설계할 때 고친다."""

    def _ApiDesignOut(self, user):
        out: ApiDesignOut = super()._ApiDesignOut(user)
        if "반드시 해결할 검사 오류" not in user:
            for f in out.functions:
                for op in f.operations:
                    for fld in op.request:
                        fld.column = fld.column.split(".")[0] + ".ghost_column"
        return out


def test_unknown_column_is_fixed_once_only_for_that_function(sample_doc):
    fake = _BadColumnLLM(broken_fk=False)
    out = _final(sample_doc, fake)
    assert fake.calls["ApiDesignOut"] == 2, "검사 오류가 난 기능만 API_FIX_ATTEMPTS(1)번 다시"
    issues = [i for i in json.loads((out / "design.json").read_text(encoding="utf-8"))["issues"]
              if i["kind"] == "API_UNKNOWN_COLUMN"]
    assert issues and all(i["status"] == "RESOLVED" for i in issues)


def test_api_design_off_keeps_the_final_confirmation_as_before(sample_doc):
    override_settings(api_design=False)
    fake = FakeLLM(broken_fk=False)
    _use(FakeLLM(broken_fk=False))
    assert cli.main(["run", str(sample_doc), "--project", "sample", "--defer"]) == 0
    _use(fake)
    override_settings(api_design=False)
    assert cli.main(["review", "sample", "v1"]) == 0
    assert fake.calls["ApiDesignOut"] == fake.calls["ArchitectureOut"] == 0 and fake.calls["ModelReviewOut"] == 1
    assert not (_folder("1.1") / "API.md").exists()
