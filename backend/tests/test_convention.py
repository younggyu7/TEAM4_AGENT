"""회사별 설계 표준(프로필): 표준 이름 만들기, 프로필 형식 검사, 산출물·DB 검증 적용 (API 불필요)."""
import json

import pytest

from fake_llm import FakeLLM
from app import cli
from app.agents.llm import client as llm_module
from app.core.config import get_settings
from app.agents.flow.graph import build_graph, open_checkpointer
from app.schemas.design import Attribute, CheckRule, DataModel, Entity, Relation
from app.agents.tools.convention import (ConventionError, apply_profile, ddl_model, load_profile, standard_name)
from app.agents.tools.versions import find_version

WORDS = {"회원": "MBR", "승인": "APRV", "이력": "HST", "전": "BFR", "차량": "VHCL", "이름": "NM", "사용": "USE",
         "여부": "YN", "소유": "OWN", "생성": "CRT", "일시": "DT"}


def test_standard_name_joins_dictionary_words_and_reports_whole_unknown_words():
    assert standard_name("회원 승인 이력", WORDS) == ("MBR_APRV_HST", [])
    assert standard_name("차량ID", WORDS) == ("VHCL_ID", []), "영문·숫자 조각은 그대로 대문자"
    assert standard_name("회원승인", WORDS) == ("MBR_APRV", []), "붙여 쓴 낱말도 사전 단어로 나눈다"
    assert standard_name("전달 상태", WORDS) == (None, ["전달", "상태"]), \
        "'전'+'달'로 잘못 나누지 않고 사전에 넣을 낱말 전체를 보고"
    assert standard_name("승인자 ID", WORDS) == (None, ["승인자"])
    assert standard_name("승인자 ID", WORDS, {"자": "R"}) == ("APRVR_ID", []), "끝말 규칙: 승인 + 자 → APRVR"
    assert standard_name("자", WORDS, {"자": "R"}) == (None, ["자"]), "끝말만 있는 낱말은 규칙을 쓰지 않는다"


def _profile(tmp_path, rules: str, words: str = "") -> object:
    folder = tmp_path / "standards" / "test"
    folder.mkdir(parents=True)
    (folder / "rules.yaml").write_text(rules, encoding="utf-8")
    if words:
        (folder / "words.csv").write_text(words, encoding="utf-8")
    return load_profile("test", tmp_path / "standards")


PUBLIC_RULES = """name: 테스트
case: upper
table_prefix: TB_
max_length: 10
primary_key: "{table}_SN"
foreign_key: parent_pk
audit_columns:
  rename: {created_at: REG_DT}
  add: [{name: REG_ID, label: 등록자 아이디, type: varchar, length: 50}]
types:
  boolean: {type: varchar, length: 1, values: [Y, N]}
"""


def _model() -> DataModel:
    member = Entity(entity_id="member", label="회원", attributes=[
        Attribute(name="member_id", label="회원 ID", data_type="bigint", nullable=False, primary_key=True),
        Attribute(name="member_name", label="이름", data_type="varchar", length=50),
        Attribute(name="is_active", label="사용 여부", data_type="boolean"),
        Attribute(name="created_at", label="생성 일시", data_type="timestamp", nullable=False)])
    vehicle = Entity(entity_id="vehicle", label="차량", parents=["member"], attributes=[
        Attribute(name="vehicle_id", label="차량 ID", data_type="bigint", nullable=False, primary_key=True),
        Attribute(name="member_id", label="소유 회원 ID", data_type="bigint", references="member"),
        Attribute(name="approver_id", label="승인자", data_type="bigint", references="member"),
        Attribute(name="plate", label="차량 번호판", data_type="varchar", length=20)],
        unique_together=[["member_id", "plate"]], checks=[CheckRule(column="approver_id", op="<>", value="member_id")])
    return DataModel(entities=[member, vehicle], relations=[
        Relation(relation_id="R1", parent="member", child="vehicle", fk_attribute="member_id", cardinality="1:N",
                 optional=True)])


def test_apply_profile_renames_tables_columns_keys_and_adds_standard_columns(tmp_path):
    words = "한글명,영문약어\n" + "\n".join(f"{k},{v}" for k, v in WORDS.items()) + "\n"
    profile = _profile(tmp_path, PUBLIC_RULES, words)
    model = _model()
    physical, report = apply_profile(model, profile)
    member, vehicle = physical.entities
    assert (member.entity_id, vehicle.entity_id) == ("TB_MBR", "TB_VHCL")
    assert [a.name for a in member.attributes] == ["MBR_SN", "NM", "USE_YN", "REG_DT", "REG_ID"], \
        "기본키는 틀({table}_SN), 감사 컬럼은 표준 이름, 표준 컬럼 추가"
    assert [(a.name, a.references) for a in vehicle.attributes[:3]] == \
        [("VHCL_SN", ""), ("MBR_SN", "TB_MBR"), ("APPROVER_ID", "TB_MBR")], \
        "역할 없는 외래키는 참조 테이블 기본키 이름, '승인자'는 사전에 없어 설계 이름"
    use = member.attributes[2]
    assert (use.data_type, use.length, use.allowed_values) == ("varchar", 1, ["Y", "N"])
    assert vehicle.unique_together == [["MBR_SN", "PLATE"]] and vehicle.checks[0].value == "MBR_SN"
    assert physical.relations[0].model_dump(include={"parent", "child", "fk_attribute"}) == \
        {"parent": "TB_MBR", "child": "TB_VHCL", "fk_attribute": "MBR_SN"}
    assert set(report.missing) == {"승인자", "번호판"} and report.count("table") == (2, 2)
    assert any("APPROVER_ID" in v and "10자" in v for v in report.violations), "최대 길이를 넘는 이름은 위반으로 보고"
    assert model.entities[0].entity_id == "member", "원래 모델(설계 이름)은 그대로"
    lower = ddl_model(physical, profile)
    assert lower.entities[1].entity_id == "tb_vhcl" and lower.entities[1].attributes[1].references == "tb_mbr", \
        "대문자 표준도 PostgreSQL DDL에는 소문자로"


def test_table_prefix_by_kind(tmp_path):
    words = "한글명,영문약어\n회원,MBR\n이력,HST\n코드,CD\n"
    profile = _profile(tmp_path, "name: 종류별\ncase: upper\ntable_prefix: TB_\n"
                                 "table_prefix_by_kind: {CODE: TC_, HISTORY: TH_}\n", words)
    model = DataModel(entities=[Entity(entity_id="member", label="회원", kind="MASTER"),
                                Entity(entity_id="member_history", label="회원 이력", kind="HISTORY"),
                                Entity(entity_id="member_code", label="회원 코드", kind="CODE")])
    assert [e.entity_id for e in apply_profile(model, profile)[0].entities] == ["TB_MBR", "TH_MBR_HST", "TC_MBR_CD"]


def test_profile_format_errors_are_reported_together(tmp_path):
    with pytest.raises(ConventionError) as e:
        _profile(tmp_path, "name: 틀림\ncase: camel\nprimary_key: ID\n", "한글명,영문약어\n회원,MBR\n회원,MEM\n차량,\n")
    message = str(e.value)
    assert "case는 upper 또는 lower" in message and "{table}" in message
    assert "'회원'이 다른 약어로 두 번" in message and "4행" in message


def _run(sample_doc, standard="public", validate_db=True):
    out = get_settings().output_dir
    fake = _Prompts()
    llm_module.set_llm(fake)
    with open_checkpointer(out) as saver:
        build_graph(saver).invoke({"run_id": "std", "source_path": str(sample_doc),
                                   "options": {"ask_mode": "defer", "validate_db": validate_db,
                                               "standard": standard}}, cli._config("std"))
    return out / "sample" / "v1", fake


class _Prompts(FakeLLM):
    def __init__(self):
        super().__init__(broken_fk=False)
        self.prompts: dict[str, list[str]] = {}

    def _call(self, system, user, schema, max_tokens, shared="", **kw):
        self.prompts.setdefault(schema.__name__, []).append(f"{shared}\n\n{user}")
        return super()._call(system, user, schema, max_tokens, shared=shared, **kw)


def test_run_with_standard_writes_standard_names_and_validates_that_ddl(sample_doc):
    folder, fake = _run(sample_doc)
    erd = (folder / "erd.md").read_text(encoding="utf-8")
    assert "### TB_SRVC_REQ (서비스 요청)" in erd and "설계 이름 `service_request`" in erd
    assert "| SRVC_REQ_SN | service_request_id |" in erd and "| REG_ID | (표준 컬럼) |" in erd
    sql = (folder / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE tb_srvc_req (" in sql and "REFERENCES tb_user (user_sn)" in sql
    model = json.loads((folder / "data_model.json").read_text(encoding="utf-8"))
    assert {e["entity_id"] for e in model["entities"]} == {"service_request", "app_user"}, \
        "data_model.json은 설계 이름 (다시 설계·사용자 수정의 기준)"
    report = (folder / "review_report.md").read_text(encoding="utf-8")
    assert "## 표준 준수 (설계 표준: 공공기관형)" in report and "| RQSTR | requester_id |" in erd, \
        "역할이 있는 외래키는 라벨('요청자')로 표준 이름"
    assert (folder / "standard_names.csv").exists()
    design = json.loads((folder / "design.json").read_text(encoding="utf-8"))
    assert design["meta"]["standard"]["profile"] == "public" and design["completion"]["satisfied"]
    db = (folder / "db_validation.md").read_text(encoding="utf-8")
    assert "tb_srvc_req" in db and design["meta"]["standard"]["missing_words"] == 0, "DB 검증은 표준 이름 DDL로"
    assert find_version(get_settings().output_dir, "sample", "1")["db_validation"] == "PASSED"
    assert find_version(get_settings().output_dir, "sample", "1")["standard"] == "public"
    assert all("## 설계 표준 지침 (공공기관형" in p for p in fake.prompts["CatalogOut"]), \
        "서술형 지침은 데이터 모델 Agent에 넘긴다"


def test_without_standard_nothing_changes(sample_doc):
    folder, fake = _run(sample_doc, standard="", validate_db=False)
    assert "CREATE TABLE service_request (" in (folder / "schema.sql").read_text(encoding="utf-8")
    assert not (folder / "standard_names.csv").exists()
    assert "설계 표준 지침" not in fake.prompts["CatalogOut"][0]


def test_cli_lists_profiles_and_rejects_unknown(sample_doc, capsys):
    assert cli.main(["standards"]) == 0
    printed = capsys.readouterr().out
    assert "✔ public (공공기관형)" in printed and "✔ default (기본)" in printed
    assert cli.main(["run", str(sample_doc), "--defer", "--standard", "없는표준"]) == 2
    assert "설계 표준 프로필을 찾을 수 없습니다: 없는표준" in capsys.readouterr().out
