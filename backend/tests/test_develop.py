"""개발 단계: 스펙 → 서비스 코드. 기본 실행은 LLM을 부르지 않고, 같은 스펙이면 같은 파일이 나온다 (API 불필요)."""
import json
import re

from test_function_designer import _base

from app import cli
from app.agents.llm import client as llm_module
from app.agents.roles import developer
from app.agents.tools import scaffold
from app.agents.tools.sample_data import sample_rows, table_order
from app.agents.tools.screens import _button, _pages, action_of, build_screens, op_kind, op_table, screen_id, short_name


class _NoLLM:
    """불리면 실패한다: 기본 실행이 토큰을 쓰지 않는지 확인한다."""

    def __getattr__(self, name):
        raise AssertionError(f"LLM을 부르면 안 됨: {name}")


def _app(out):
    return out / "sample" / "app"


def _snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


FINAL = "v1.1"   # 개발 스펙은 'ERD 최종 확정'으로 만든 최종본에만 있다


def _start(sample_doc):
    """기준 실행(ERD v1) → 'ERD 최종 확정'(최종본 v1.1) + Spring Boot 스펙 (가짜 LLM의 스택은 Python이고 규칙이 비어 있어 스펙만 바꾼다)."""
    out = _base(sample_doc)
    assert cli.main(["review", "sample", "v1"]) == 0
    path = out / "sample" / FINAL / "design.json"
    design = json.loads(path.read_text(encoding="utf-8"))
    design["api_design"]["architecture"]["stack_in_source"] = [{"layer": "프레임워크", "value": "Spring Boot 3", "requirement_ids": []}]
    for f in design["functions"]:
        f["business_rules"] = ["제목은 필수다", "(확인 필요 FQ-001) 승인자는 팀장이다"]
    path.write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
    llm_module.set_llm(_NoLLM())
    return out


def _spec(sample_doc):
    _start(sample_doc)
    return scaffold.load_spec("sample", FINAL)


def test_develop_makes_project_without_llm_and_same_spec_gives_same_files(sample_doc, tmp_path):
    out = _start(sample_doc)
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0
    assert cli.main(["develop", "sample", "--from", FINAL, "--out", str(tmp_path / "again")]) == 0
    first, second = _snapshot(_app(out)), _snapshot(tmp_path / "again")
    assert first == second, "같은 스펙이면 파일이 바이트까지 같다"
    for path in ("frontend/src/generated/screens.json", "frontend/src/generated/mock-data.json", "db/seed.sql", "db/schema.sql",
                 "spec/SCREENS.md", "spec/openapi.yaml", "spec/SPEC_VERSION", "backend/src/main/java/app/common/Db.java", "README.md"):
        assert path in first
    screens = json.loads(first["frontend/src/generated/screens.json"])
    spec = scaffold.load_spec("sample", FINAL)
    assert len(screens["screens"]) + len(screens["autos"]) == len(spec.functions), "기능 하나 = 화면 하나 (또는 자동 실행)"
    assert all(s["screen_id"] == screen_id(s["function_id"]) for s in screens["screens"]), "화면 ID는 기능 ID에서 나온다"


def test_plan_writes_nothing(sample_doc, capsys):
    out = _start(sample_doc)
    assert cli.main(["develop", "sample", "--from", FINAL, "--plan"]) == 0
    assert not _app(out).exists()
    text = capsys.readouterr().out
    assert "--logic 예상" in text and "Haiku 5.5" in text


def test_version_without_data_design_is_refused(sample_doc, capsys):
    out = _start(sample_doc)
    (out / "sample" / FINAL / "data_model.json").unlink()
    assert cli.main(["develop", "sample", "--from", FINAL]) == 2
    assert "데이터 설계" in capsys.readouterr().out


def test_other_stack_gets_screens_without_backend(sample_doc):
    spec = _spec(sample_doc)
    from app.agents.tools import stacks
    spec.stack = {**spec.stack, "backend": "Java 17 · 전자정부 표준프레임워크 4 · PostgreSQL 16"}
    spec.parts = stacks.parts_of(spec.stack["backend"])
    files, _ = scaffold.render_app(spec)
    assert "frontend/src/generated/screens.json" in files and "db/seed.sql" in files
    assert not [p for p in files if p.startswith("backend/")], "스펙과 다른 스택의 코드를 내지 않는다"
    assert developer.targets(spec) == [], "채울 자리도 없다"
    assert "뼈대는 아직 없어" in files["README.md"]
    assert not scaffold.has_backend(spec), "전자정부는 아직 틀이 없다 (Spring 틀과 같지 않음)"
    spec.parts = stacks.parts_of("TypeScript · NestJS")
    assert scaffold.has_backend(spec), "NestJS는 틀이 있다"
    spec.parts = stacks.parts_of("Java 17 · Spring Boot 3")
    assert scaffold.has_backend(spec)


def test_rerun_keeps_edited_files_and_regenerates_the_rest(sample_doc, capsys):
    out = _start(sample_doc)
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0
    app = _app(out)
    logic = next((app / "backend/src/main/java/app/logic").glob("*Logic.java"))
    readme = app / "README.md"
    logic.write_text(logic.read_text(encoding="utf-8") + "// 사람이 고침\n", encoding="utf-8")
    readme.unlink()
    capsys.readouterr()
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0
    assert "사람이 고침" in logic.read_text(encoding="utf-8"), "사람이 고친 파일은 덮어쓰지 않는다"
    assert readme.exists(), "지운 생성 파일은 다시 만든다"
    assert "사람이 고쳐서 그대로 둔 파일 1개" in capsys.readouterr().out
    assert cli.main(["develop", "sample", "--from", FINAL, "--force"]) == 0
    assert "사람이 고침" not in logic.read_text(encoding="utf-8")


def test_file_of_removed_function_is_deleted_unless_edited(tmp_path):
    scaffold.write_app({"a.txt": "1", "b.txt": "2", "c.txt": "3"}, tmp_path)
    (tmp_path / "c.txt").write_text("고침", encoding="utf-8")
    report = scaffold.write_app({"a.txt": "1"}, tmp_path)
    assert report["removed"] == ["b.txt"] and report["kept"] == ["c.txt"]
    assert not (tmp_path / "b.txt").exists() and (tmp_path / "c.txt").exists()


def test_operation_kind_comes_from_api_shape():
    assert op_kind({"method": "GET", "path": "/vouchers", "list_response": True}) == "list"
    assert op_kind({"method": "GET", "path": "/vouchers/{voucherId}"}) == "get"
    assert op_kind({"method": "GET", "path": "/budget-status"}) == "report"
    assert op_kind({"method": "POST", "path": "/vouchers", "operation_id": "createVoucher"}) == "create"
    assert op_kind({"method": "POST", "path": "/vouchers/{voucherId}/lines", "operation_id": "createVoucherLine"}) == "add"
    assert op_kind({"method": "POST", "path": "/vouchers/{voucherId}/approve", "operation_id": "approveVoucher"}) == "action"
    assert op_kind({"method": "PUT", "path": "/vouchers/{voucherId}"}) == "update"
    assert op_kind({"method": "PATCH", "path": "/vouchers/{voucherId}/status"}) == "action"
    assert op_kind({"method": "DELETE", "path": "/vouchers/{voucherId}"}) == "delete"
    chat = {"name": "body", "column": "chat_message.body"}
    assert op_kind({"method": "POST", "path": "/chat-messages", "operation_id": "sendChatMessage", "request": [chat]}) == "create", "등록 동사가 아니어도 자기 목록 경로면 등록"


def test_menu_groups_screens_of_same_table_into_tabs():
    screens = [{"screen_id": "S1", "name": "전표 입력", "table": "voucher", "table_label": "전표"},
               {"screen_id": "S2", "name": "예산 조회", "table": "budget", "table_label": "예산"},
               {"screen_id": "S3", "name": "전표 승인", "table": "voucher", "table_label": "전표"},
               {"screen_id": "S4", "name": "내보내기", "table": "", "table_label": ""}]
    assert _pages(screens) == [{"name": "전표 관리", "screens": ["S1", "S3"]}, {"name": "예산 조회", "screens": ["S2"]}, {"name": "내보내기", "screens": ["S4"]}]
    assert short_name("인사 근태 및 급여관리") == "인사 근태" and short_name("그룹웨어") == "그룹웨어"


def test_many_areas_and_split_groups_make_one_module_each(sample_doc):
    spec = _spec(sample_doc)
    for i, f in enumerate(spec.functions):
        f.group = "SFR-001.1" if i % 2 else "SFR-001.2"
    data = build_screens(spec, {"SFR-001": "서비스 요청 및 처리"})
    assert [m["group"] for m in data["menu"]] == ["SFR-001"], "큰 영역을 나눈 그룹은 한 영역"
    assert data["menu"][0]["name"] == "서비스 요청 및 처리" and data["menu"][0]["short"] == "서비스 요청"
    data = build_screens(spec)
    assert data["menu"][0]["name"] == spec.by_req[spec.functions[0].requirement_ids[0]].title, "이름이 없으면 요구사항 제목"


def test_sample_rows_follow_non_numeric_keys_and_project_words(tmp_path):
    from app.agents.tools.sample_data import load_words

    code = _entity("code_group", {"name": "group_code", "primary_key": True, "nullable": False, "length": 3},
                   {"name": "group_name", "label": "프로젝트명", "nullable": False})
    item = _entity("code_item", {"name": "item_id", "primary_key": True, "nullable": False, "data_type": "uuid"},
                   {"name": "group_code", "references": "code_group", "nullable": False, "length": 3})
    leaf = _entity("leaf", {"name": "leaf_id", "primary_key": True, "nullable": False, "data_type": "bigint"},
                   {"name": "item_id", "data_type": "uuid", "references": "code_item", "nullable": False},
                   {"name": "parent_id", "data_type": "bigint", "references": "leaf"})
    rows = sample_rows(_model(leaf, item, code))
    assert {r["group_code"] for r in rows["code_item"]} == {r["group_code"] for r in rows["code_group"]}, "문자 기본키도 부모 값을 가리킨다"
    assert rows["leaf"][1]["item_id"] == rows["code_item"][1]["item_id"], "uuid 기본키도 부모 값을 가리킨다"
    assert rows["leaf"][0]["parent_id"] is None and rows["leaf"][1]["parent_id"] == 1, "자기 참조는 앞 행"
    long_value = _entity("t", {"name": "t_id", "primary_key": True, "nullable": False, "data_type": "bigint"},
                         {"name": "result", "length": 5, "allowed_values": ["GOOD", "TOO_LONG_VALUE"], "nullable": False})
    assert {r["result"] for r in sample_rows(_model(long_value))["t"]} == {"GOOD"}, "칸 길이를 넘는 허용값은 고르지 않는다"
    assert len({r["group_code"] for r in rows["code_group"]}) == 6, "짧은 코드도 겹치지 않는다"

    assert load_words(None) == ()
    words = json.dumps([{"keys": ["프로젝트"], "words": ["가 사업", "나 사업"]}], ensure_ascii=False)
    assert sample_rows(_model(code), load_words(words))["code_group"][1]["group_name"] == "나 사업", "프로젝트 값 파일이 먼저"


def test_reserved_names_are_quoted_and_composite_key_is_not_guessed(sample_doc):
    from app.schemas.data_model import Attribute, Entity
    from app.agents.tools.sample_data import seed_sql

    spec = _spec(sample_doc)
    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    order = Entity(entity_id="order", label="주문", attributes=[Attribute(name="order_id", **pk), Attribute(name="user", nullable=False)])
    link = Entity(entity_id="order_tag", attributes=[Attribute(name="order_id", **pk), Attribute(name="tag_id", **pk)])
    spec.model.entities += [order, link]
    spec.columns.update({(e.entity_id, a.name): a for e in (order, link) for a in e.attributes})
    field = {"name": "user", "column": "order.user", "type": "string", "required": False, "description": ""}
    rows = {"method": "GET", "path": "/orders", "operation_id": "listOrders", "summary": "목록", "actors": [], "request": [], "response": [field],
            "list_response": True, "errors": []}
    tag = {"name": "tagId", "column": "order_tag.tag_id", "type": "integer", "required": False, "description": ""}
    gone = {"method": "DELETE", "path": "/order-tags/{orderId}", "operation_id": "deleteOrderTag", "summary": "삭제", "actors": [], "request": [tag],
            "response": [], "list_response": False, "errors": []}
    text = scaffold._controller(spec, spec.functions[0], [rows, gone])
    assert r'new Table("\"order\""' in text and r'\"order\".\"user\"' in text, "예약어는 DDL과 같게 따옴표"
    assert "db.delete" not in text and 'ApiError.notImplemented("deleteOrderTag")' in text, "복합 기본키를 ID 하나로 지우지 않는다"
    seed = seed_sql(spec.model, sample_rows(spec.model))
    assert 'INSERT INTO "order" (order_id, "user")' in seed


def test_standard_profile_values_follow_schema_not_design_types():
    from app.schemas.data_model import Attribute, DataModel, Entity
    from app.agents.tools.dev_spec import Names
    from app.agents.tools.sample_data import mock_data, seed_sql

    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    design = DataModel(entities=[Entity(entity_id="notice", attributes=[Attribute(name="notice_id", **pk), Attribute(name="is_open", data_type="boolean", nullable=False)])], relations=[])
    physical = DataModel(entities=[Entity(entity_id="notice", attributes=[Attribute(name="notice_id", **pk), Attribute(name="is_open", length=1, allowed_values=["Y", "N"], nullable=False)])], relations=[])
    rows = sample_rows(physical)
    assert "'Y'" in seed_sql(physical, rows), "DB에는 schema.sql 타입대로 넣는다"
    mock = mock_data(design, Names(None, None), rows)
    assert mock["rows"]["notice"][0]["is_open"] is True and mock["rows"]["notice"][1]["is_open"] is False, "화면에는 API처럼 참·거짓"


def test_status_uses_design_meaning_not_physical_name(sample_doc):
    from app.schemas.data_model import Attribute

    spec = _spec(sample_doc)
    entity = next(e for e in spec.model.entities if e.attributes)
    entity.attributes.append(Attribute(name="prgrs_cd", label="진행 상태", allowed_values=["READY", "DONE"]))
    spec.columns[(entity.entity_id, "prgrs_cd")] = entity.attributes[-1]
    op = {"operation_id": "completeTask", "method": "POST", "path": "/t/{id}/complete", "summary": "업무를 완료 처리한다"}
    assert action_of(spec, op, entity.entity_id)["sets"]["value"] == "DONE", "표준 이름이어도 라벨로 상태 컬럼을 찾는다"


def test_writing_api_runs_in_a_transaction_and_reading_api_does_not(sample_doc):
    spec = _spec(sample_doc)
    entity = next(e for e in spec.model.entities if e.pk())
    field = {"name": "id", "column": f"{entity.entity_id}.{entity.pk()[0].name}", "type": "string", "required": False, "description": ""}
    make = lambda oid, method, path: {"method": method, "path": path, "operation_id": oid, "summary": "처리", "actors": [], "request": [], "response": [field],   # noqa: E731
                                      "list_response": method == "GET", "errors": []}
    text = scaffold._controller(spec, spec.functions[0], [make("listThings", "GET", "/things"), make("createThing", "POST", "/things")])
    assert "@Transactional\n    @PostMapping" in text, "쓰기 API는 업무 로직까지 한 묶음"
    assert "@Transactional\n    @GetMapping" not in text


def test_screens_read_names_through_lookup_api_and_mock_shares_label(sample_doc):
    from app.schemas.data_model import Attribute, Entity
    from app.agents.tools.screens import label_column

    entity = Entity(entity_id="staff", attributes=[Attribute(name="staff_id", primary_key=True, nullable=False, data_type="bigint"), Attribute(name="password_hash"),
                                                   Attribute(name="staff_no"), Attribute(name="staff_name")])
    assert label_column(entity) == "staff_name", "이름 컬럼이 먼저, 비밀 컬럼은 쓰지 않는다"
    entity.attributes = [a for a in entity.attributes if a.name != "staff_name"]
    assert label_column(entity) == "staff_no"
    out = _start(sample_doc)
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0
    app = _app(out)
    text = (app / "backend/src/main/java/app/api/LookupController.java").read_text(encoding="utf-8")
    assert '@RequestMapping("/api/ui")' in text and "requireLogin()" in text, "업무 API와 나누고 로그인을 요구한다"
    assert 'AS \\"value\\"' in text, "Java 문자열 안의 따옴표는 이스케이프한다"
    assert '"labels"' in (app / "frontend/src/generated/mock-data.json").read_text(encoding="utf-8")


def test_path_variable_is_not_an_input_field(sample_doc):
    spec = _spec(sample_doc)
    entity = next(e for e in spec.model.entities if e.pk())
    fid = spec.functions[0].function_id
    pk = entity.pk()[0].name
    param = pk.split("_")[0] + "".join(w.capitalize() for w in pk.split("_")[1:])
    path = f"/things/{{{param}}}/approve"
    spec.api[fid] = {"function_id": fid, "no_api_reason": "", "operations": [
        _op("GET", "/things", "listThings", "목록을 조회한다", response=[_value(entity)], list_response=True),
        _op("POST", path, "approveThing", "승인", request=[{"name": param, "column": f"{entity.entity_id}.{pk}", "type": "string", "required": True, "description": ""},
                                                           {"name": "memo", "column": "", "type": "string", "required": False, "description": "의견"}])]}
    screen = next(s for s in build_screens(spec)["screens"] if s["function_id"] == fid)
    assert [f["name"] for f in screen["actions"][0]["fields"]] == ["memo"], "입력값이 경로의 ID를 덮어쓰지 않는다"


def _two_tables(sample_doc):
    spec = _spec(sample_doc)
    entities = [e for e in spec.model.entities if len(e.pk()) == 1]
    return spec, entities[0], entities[1], spec.functions[0].function_id


def _op(method, path, operation_id, summary, request=(), response=(), list_response=False):
    return {"method": method, "path": path, "operation_id": operation_id, "summary": summary, "actors": [], "list_response": list_response, "errors": [],
            "request": list(request), "response": list(response)}


def _value(entity, name="memo"):
    attr = next(a for a in entity.attributes if not a.primary_key)
    return {"name": name, "column": f"{entity.entity_id}.{attr.name}", "type": "string", "required": False, "description": ""}


def test_update_of_another_table_picks_its_target(sample_doc):
    spec, rows, other, fid = _two_tables(sample_doc)
    pk = other.pk()[0].name
    param = pk.split("_")[0] + "".join(w.capitalize() for w in pk.split("_")[1:])
    path = f"/others/{{{param}}}"
    spec.api[fid] = {"function_id": fid, "no_api_reason": "", "operations": [
        _op("GET", "/rows", "listRows", "목록을 조회한다", response=[_value(rows)], list_response=True),
        _op("PATCH", path, "updateOther", "증빙을 수정한다", request=[_value(other)]),
        _op("DELETE", path, "deleteOther", "증빙을 삭제한다")]}
    screen = next(s for s in build_screens(spec)["screens"] if s["function_id"] == fid)
    assert screen["update"] is None and screen["delete"] is None, "행의 ID로 다른 테이블을 고치면 안 된다"
    update, delete = screen["actions"]
    assert update["label"] == "증빙 수정" and delete["kind"] == "delete"
    assert [f["name"] for f in update["fields"]] == [param, "memo"] and update["fields"][0]["ref"] == other.entity_id and update["fields"][0]["required"]


def test_update_without_rows_picks_its_target(sample_doc):
    spec, _, other, fid = _two_tables(sample_doc)
    for entry in spec.api.values():
        entry["operations"] = [op for op in entry["operations"] if op_table(spec, op) != other.entity_id]
    pk = other.pk()[0].name
    param = pk.split("_")[0] + "".join(w.capitalize() for w in pk.split("_")[1:])
    spec.api[fid] = {"function_id": fid, "no_api_reason": "", "operations": [_op("PATCH", f"/others/{{{param}}}", "updateOther", "증빙을 수정한다", request=[_value(other)])]}
    screen = next(s for s in build_screens(spec)["screens"] if s["function_id"] == fid)
    assert screen["update"] is None and [f["name"] for f in screen["actions"][0]["fields"]] == [param, "memo"], "목록이 없으면 대상을 골라야 수정할 수 있다"


def test_update_form_reads_only_its_fields(sample_doc):
    spec, rows, _, fid = _two_tables(sample_doc)
    pk = rows.pk()[0].name
    param = pk.split("_")[0] + "".join(w.capitalize() for w in pk.split("_")[1:])
    spec.api[fid] = {"function_id": fid, "no_api_reason": "", "operations": [
        _op("GET", "/rows", "listRows", "목록을 조회한다", response=[_value(rows)], list_response=True),
        {**_op("PATCH", f"/rows/{{{param}}}", "updateRow", "수정한다", request=[_value(rows)]), "actors": ["사원"]}]}
    data = build_screens(spec)
    screen = next(s for s in data["screens"] if s["function_id"] == fid)
    assert [f["name"] for f in screen["update"]["fields"]] == ["memo"]
    tables, reads = scaffold._form_reads(spec, data)
    read = next(r for r in reads if screen["screen_id"] in r)
    assert rows.entity_id in tables and '"memo"' in read and read.count("new Col(") == 1, "폼에 있는 칸만 읽는다"
    assert '"사원"' in read, "수정 API와 같은 역할로만 읽는다"


def test_path_variable_of_list_is_chosen_from_parent(sample_doc):
    spec, rows, other, fid = _two_tables(sample_doc)
    spec.columns[(rows.entity_id, "owner_id")] = next(a for a in rows.attributes if not a.primary_key).model_copy(update={"name": "owner_id", "label": "담당", "references": other.entity_id})
    spec.api[fid] = {"function_id": fid, "no_api_reason": "", "operations": [
        _op("GET", "/owners/{ownerId}/rows", "listByOwner", "목록을 조회한다", response=[_value(rows)], list_response=True)]}
    screen = next(s for s in build_screens(spec)["screens"] if s["function_id"] == fid)
    field = screen["list"]["filters"][0]
    assert field["name"] == "ownerId" and field["label"] == "담당" and field["ref"] == other.entity_id, "경로의 ID는 글자로 넣지 않고 목록에서 고른다"


def test_table_of_api_without_fields_comes_from_path(sample_doc):
    spec = _spec(sample_doc)
    entity = next(e for e in spec.model.entities if e.pk())
    pk = entity.pk()[0].name
    param = pk.split("_")[0] + "".join(w.capitalize() for w in pk.split("_")[1:])
    assert op_table(spec, {"method": "DELETE", "path": f"/things/{{{param}}}", "operation_id": "deleteThing"}) == entity.entity_id


def test_controller_writes_each_column_once_and_reads_parent_values(sample_doc):
    from app.schemas.data_model import Attribute, Entity

    spec = _spec(sample_doc)
    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    parent = Entity(entity_id="voucher", label="전표", attributes=[Attribute(name="voucher_id", **pk), Attribute(name="voucher_no", nullable=False)])
    child = Entity(entity_id="voucher_line", attributes=[
        Attribute(name="voucher_line_id", **pk), Attribute(name="voucher_id", data_type="bigint", references="voucher", nullable=False),
        Attribute(name="writer_id", data_type="bigint", references="app_user", nullable=False), Attribute(name="line_no", nullable=False),
        Attribute(name="created_at", data_type="timestamp", nullable=False)])
    spec.model.entities += [parent, child]
    spec.columns.update({(e.entity_id, a.name): a for e in (parent, child) for a in e.attributes})
    field = lambda name, column: {"name": name, "column": column, "type": "string", "required": False, "description": ""}   # noqa: E731
    add = {"method": "POST", "path": "/vouchers/{voucherId}/lines", "operation_id": "createVoucherLine", "summary": "행 추가", "actors": [],
           "request": [field("voucherId", "voucher_line.voucher_id"), field("createdAt", "voucher_line.created_at")],
           "response": [field("voucherLineId", "voucher_line.voucher_line_id")], "list_response": False, "errors": []}
    rows = {"method": "GET", "path": "/voucher-lines", "operation_id": "listVoucherLines", "summary": "목록", "actors": [], "request": [],
            "response": [field("voucherLineId", "voucher_line.voucher_line_id"), field("voucherNo", "voucher.voucher_no")], "list_response": True, "errors": []}
    text = scaffold._controller(spec, spec.functions[0], [add, rows])
    assert text.count('new Col("voucherId", "voucher_id"') == 1, "경로 변수와 요청 필드가 같은 컬럼이면 한 번만"
    assert '"created_at"' not in text.split("IN_CREATE_VOUCHER_LINE =")[1].split(";")[0], "시각 컬럼은 Db가 넣는다"
    assert 'input.putIfAbsent("writerId", user.id());' in text, "작성자 외래키는 현재 사용자"
    assert "LEFT JOIN voucher j1 ON j1.voucher_id = voucher_line.voucher_id" in text and 'new Col("voucherNo", "j1.voucher_no"' in text, "부모 값은 함께 읽는다"


def _model(*entities):
    from app.schemas.data_model import DataModel

    return DataModel(entities=list(entities), relations=[])


def _entity(name, *attrs, **extra):
    from app.schemas.data_model import Attribute, Entity

    return Entity(entity_id=name, attributes=[Attribute(**a) for a in attrs], **extra)


def test_sample_rows_keep_constraints():
    from app.schemas.data_model import CheckRule, RequiredWhen

    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    child = _entity(
        "permission", {"name": "permission_id", **pk},
        {"name": "target_type", "allowed_values": ["EMPLOYEE", "TEAM"], "nullable": False},
        {"name": "employee_id", "data_type": "bigint", "references": "employee"},
        {"name": "team_id", "data_type": "bigint", "references": "team"},
        {"name": "start_date", "data_type": "date", "nullable": False}, {"name": "end_date", "data_type": "date", "nullable": False},
        {"name": "approver_id", "data_type": "bigint", "references": "employee", "nullable": False},
        {"name": "requester_id", "data_type": "bigint", "references": "employee", "nullable": False},
        {"name": "amount", "data_type": "decimal", "nullable": False},
        {"name": "title", "data_type": "varchar", "length": 5, "nullable": False},
        exactly_one_of=[["employee_id", "team_id"]],
        required_when=[RequiredWhen(column="employee_id", when_column="target_type", when_values=["EMPLOYEE"]),
                       RequiredWhen(column="team_id", when_column="target_type", when_values=["TEAM"])],
        checks=[CheckRule(column="end_date", op=">=", value="start_date"), CheckRule(column="approver_id", op="<>", value="requester_id"),
                CheckRule(column="amount", op=">", value="0")])
    model = _model(child, _entity("employee", {"name": "employee_id", **pk}), _entity("team", {"name": "team_id", **pk}))
    assert [e.entity_id for e in table_order(model)] == ["employee", "team", "permission"], "부모 먼저"
    rows = sample_rows(model)
    assert rows == sample_rows(model), "같은 모델이면 같은 값"
    for r in rows["permission"]:
        assert (r["employee_id"] is None) != (r["team_id"] is None), "정확히 하나만"
        assert r["employee_id" if r["target_type"] == "EMPLOYEE" else "team_id"] is not None, "조건부 필수가 고른 컬럼을 채운다"
        assert r["end_date"] >= r["start_date"] and r["approver_id"] != r["requester_id"] and r["amount"] > 0 and len(r["title"]) <= 5


def test_sample_rows_skip_rows_that_break_unique_constraints():
    """허용값이 행 수보다 적은 unique 컬럼·부모가 적은 1:1 외래키·unique_together는 겹치는 행을 넣지 않는다
    (seed.sql이 duplicate key로 실패하면 앱 DB 컨테이너가 종료 코드 3으로 꺼져 실행 미리보기가 뜨지 않았다)."""
    pk = {"primary_key": True, "nullable": False, "data_type": "bigint"}
    setting = _entity("setting", {"name": "setting_id", **pk},
                      {"name": "target_type", "allowed_values": ["A", "B", "C"], "unique": True, "nullable": False})
    parent = _entity("parent", {"name": "parent_id", **pk}, {"name": "kind", "allowed_values": ["X", "Y"], "unique": True, "nullable": False})
    child = _entity("child", {"name": "child_id", **pk}, {"name": "parent_id", "data_type": "bigint", "references": "parent", "unique": True, "nullable": False},
                    {"name": "prev_id", "data_type": "bigint", "references": "child"})
    pair = _entity("pair", {"name": "pair_id", **pk}, {"name": "a", "allowed_values": ["1", "2"], "nullable": False},
                   {"name": "b", "data_type": "boolean", "nullable": False}, unique_together=[["a", "b"]])
    rows = sample_rows(_model(setting, parent, child, pair))
    assert [r["target_type"] for r in rows["setting"]] == ["A", "B", "C"]
    assert [r["parent_id"] for r in rows["child"]] == [r["parent_id"] for r in rows["parent"]] == [1, 2], "남은 부모만 가리킨다"
    assert rows["child"][1]["prev_id"] == rows["child"][0]["child_id"], "자기 참조는 남은 앞 행"
    assert len({(r["a"], r["b"]) for r in rows["pair"]}) == len(rows["pair"]) == 2
    assert len(sample_rows(_model(_entity("free", {"name": "free_id", **pk}, {"name": "k", "allowed_values": ["A"]})))["free"]) == 6, \
        "유일 조건이 없으면 행을 줄이지 않는다"


def test_button_name_is_object_and_verb_of_first_action():
    assert _button("결재문서를 결재선과 함께 상신한다", "") == "결재문서 상신"
    assert _button("예약을 취소한다 (예약자 본인과 시스템 관리자만 가능)", "") == "예약 취소", "괄호 설명은 넣지 않는다"
    assert _button("전표를 승인 또는 승인해제한다", "") == "전표 승인·승인해제"
    assert _button("확정된 연차를 조정하고 변경이력을 저장한다", "") == "연차 조정", "앞 동작"
    assert _button("임직원 재직 상태 변경 및 이력 저장", "") == "상태 변경", "명사구 설명의 앞 동작"
    assert _button("문서 및 파일을 등록한다", "") == "파일 등록", "목적어가 있으면 '및'에서 자르지 않는다"
    assert _button("임직원 인사정보 등록", "") == "인사정보 등록"
    assert _button("주문 승인을 요청한다", "") == "승인 요청"
    assert _button("전표를 승인 처리한다", "") == "승인 처리", "붙여 쓰는 동사"
    assert _button("결재선에 지정된 결재자가 결재문서를 승인한다", "") == "결재문서 승인"
    assert _button("웹오피스에서 편집한 내용을 대상 결재문서에 저장한다", "") == "내용 저장"
    assert _button("", "승인") == "승인"


def test_action_button_sets_status_from_allowed_values(sample_doc):
    spec = _spec(sample_doc)
    entity = next(e for e in spec.model.entities if e.attributes)
    from app.schemas.data_model import Attribute

    entity.attributes.append(Attribute(name="approval_status", allowed_values=["DRAFT", "APPROVED", "REJECTED"]))
    spec.columns[(entity.entity_id, "approval_status")] = entity.attributes[-1]
    op = {"operation_id": "approveVoucher", "method": "POST", "path": "/v/{id}/approve", "summary": "전표를 승인 처리한다"}
    action = action_of(spec, op, entity.entity_id)
    assert action["label"] == "승인 처리" and action["sets"]["value"] == "APPROVED" and action["sets"]["label"] == "승인"
    assert "sets" not in action_of(spec, {**op, "operation_id": "printVoucher"}, entity.entity_id), "상태와 무관한 처리는 기본 동작이 없다"


def test_secret_columns_never_reach_screens(sample_doc):
    spec = _spec(sample_doc)
    fid = spec.functions[0].function_id
    table = spec.linked[fid][0]
    from app.schemas.data_model import Attribute

    next(e for e in spec.model.entities if e.entity_id == table).attributes.append(Attribute(name="password_hash"))
    spec.columns[(table, "password_hash")] = Attribute(name="password_hash")
    field = {"name": "passwordHash", "column": f"{table}.password_hash", "type": "string", "required": False, "description": ""}
    spec.api[fid] = {"function_id": fid, "no_api_reason": "", "operations": [{
        "method": "GET", "path": "/things", "operation_id": "listThings", "summary": "목록", "actors": [], "request": [],
        "response": [field], "list_response": True, "errors": []}]}
    assert "passwordHash" not in json.dumps(build_screens(spec), ensure_ascii=False)
    files, _ = scaffold.render_app(spec)
    assert "password_hash" not in files[f"backend/src/main/java/app/api/{scaffold.class_name(fid)}Controller.java"]


# ------------------------------------------------------------------ 업무 로직 자리
class _LogicLLM:
    """요청마다 정해진 답을 낸다. 첫 응답은 bad로 줄 수 있다."""

    model = "fake"

    def __init__(self, body="if (input.isEmpty()) {\n    throw ctx.fail(400, \"EMPTY\", \"값이 없습니다.\");\n}\nreturn crud.get();", bad=False):
        self.body, self.bad, self.calls, self.users = body, bad, 0, []

    def structured(self, *, system, user, schema, shared="", effort="", purpose="", **kw):
        self.calls += 1
        self.users.append(user)
        method = re.search(r"^- (\w+): ", user.split("## 채울 메서드")[1], re.M).group(1)
        body = 'return Runtime.getRuntime();' if self.bad and "이전 응답의 오류" not in user else self.body
        return schema(hooks=[{"method": method, "body": body}])

    def pop_usage(self):
        return {"calls": self.calls, "input_tokens": 10, "output_tokens": 20}


def _with_rule(spec):
    return [f for f in spec.functions if developer._rules(spec, f)[0] or f.exception_flows]


def test_logic_fills_only_holes_and_is_not_asked_twice(sample_doc):
    out = _start(sample_doc)
    spec = scaffold.load_spec("sample", FINAL)
    fake = _LogicLLM()
    llm_module.set_llm(fake)
    app = _app(out)
    expected = len(_with_rule(spec))
    assert expected
    assert cli.main(["develop", "sample", "--from", FINAL, "--logic"]) == 0
    assert fake.calls == expected
    logic = next(p for p in (app / "backend/src/main/java/app/logic").glob("*Logic.java") if "EMPTY" in p.read_text(encoding="utf-8"))
    text = logic.read_text(encoding="utf-8")
    assert "// <hole " in text and "throw ctx.fail(400" in text, "채운 코드는 자리 안에 들어간다"
    before = _snapshot(app)
    assert cli.main(["develop", "sample", "--from", FINAL, "--logic"]) == 0
    assert fake.calls == expected, "입력이 같으면 다시 묻지 않는다"
    assert _snapshot(app) == before


def test_changed_function_only_is_asked_again(sample_doc):
    out = _start(sample_doc)
    spec = scaffold.load_spec("sample", FINAL)
    fake = _LogicLLM()
    llm_module.set_llm(fake)
    developer.fill_logic(spec, _app(out))
    first = fake.calls
    spec.functions[0].business_rules.append("새로 생긴 규칙")
    assert developer.plan(spec, _app(out))["functions"] == 1
    developer.fill_logic(spec, _app(out))
    assert fake.calls == first + 1, "바뀐 기능만 다시 묻는다"


def test_bad_code_is_retried_with_errors_then_reported(sample_doc):
    out = _start(sample_doc)
    spec = scaffold.load_spec("sample", FINAL)
    fake = _LogicLLM(bad=True)
    llm_module.set_llm(fake)
    fid = _with_rule(spec)[0].function_id
    result = developer.fill_logic(spec, _app(out), {fid})
    assert fake.calls == 2 and "쓸 수 없는 표현 'Runtime'" in fake.users[1], "오류를 붙여 한 번 더 묻는다"
    assert result["filled"] == [fid]

    never = _LogicLLM(body="return Runtime.getRuntime();")
    llm_module.set_llm(never)
    result = developer.fill_logic(spec, tmp := _app(out) / "other", {fid})
    assert result["failed"] == [fid] and never.calls == 2
    assert scaffold.load_logic(tmp)[fid]["hooks"] == {}, "실패하면 기본 동작을 둔다"


def test_check_hook_rejects_unknown_table_and_bad_shape():
    allowed, tables = {"createVoucher"}, {"voucher"}
    assert developer.check_hook("createVoucher", "return crud.get();", allowed, tables) == []
    sql = 'ctx.db().query("SELECT 1 FROM voucher WHERE voucher_id = :id", Map.of("id", 1));\nreturn crud.get();'
    assert developer.check_hook("createVoucher", sql, allowed, tables) == []
    assert "없는 테이블" in developer.check_hook("createVoucher", sql.replace("FROM voucher", "FROM secret_table"), allowed, tables)[0]
    assert "목록에 없는 메서드" in developer.check_hook("deleteAll", "return crud.get();", allowed, tables)[0]
    assert "return이 없음" in developer.check_hook("createVoucher", "input.clear();", allowed, tables)[0]
    assert "괄호" in developer.check_hook("createVoucher", "if (true) { return crud.get();", allowed, tables)[0]
    assert developer.check_hook("createVoucher", 'return "class Runtime {";', allowed, tables) == [], "문자열 안은 코드가 아니다"
    one_line = 'final int LIMIT = 3; // 가정: 한도 3회. if (input.isEmpty()) { throw ctx.fail(400, "X", "y"); } return crud.get();'
    assert "return이 없음" in " ".join(developer.check_hook("createVoucher", one_line, allowed, tables)), \
        "줄 끝 주석 뒤에 이어 쓴 코드는 주석이다 (컴파일하면 return이 없다)"
    assert "주석만" in developer.check_hook("createVoucher", "// return crud.get();", allowed, tables)[0]
    assert developer.check_hook("createVoucher", "final int LIMIT = 3; // 가정: 한도\nreturn crud.get();", allowed, tables) == []
    role = 'if (!"인사 담당자".equals(ctx.user().role())) { throw ctx.fail(403, "X", "y"); }\nreturn crud.get();'
    assert "권한" in developer.check_hook("createVoucher", role, allowed, tables)[0], "권한 검사는 코드가 이미 한다"
    parse = 'java.time.LocalDateTime at = java.time.LocalDateTime.parse(String.valueOf(input.get("at")));\nreturn crud.get();'
    assert "파싱" in developer.check_hook("createVoucher", parse, allowed, tables)[0], "날짜 글자는 SQL의 CAST로 다룬다"
    raw = 'return ctx.db().query("SELECT voucher_id FROM voucher", java.util.Map.of());'
    assert "crud.get()" in developer.check_hook("listVouchers", raw, {"listVouchers"}, tables, {"listVouchers"})[0], \
        "조회는 crud.get() 응답 모양(items·page…)을 화면이 그대로 쓴다"
    assert developer.check_hook("createVoucher", raw, allowed, tables) == [], "조회가 아니면 직접 쿼리를 돌려줘도 된다"


def test_read_api_without_response_shows_its_table_columns():
    from app.schemas.data_model import Attribute, DataModel, Entity

    pk = {"data_type": "bigint", "primary_key": True, "nullable": False}
    model = DataModel(entities=[
        Entity(entity_id="leave_request", function_ids=["FN-1", "FN-2"], attributes=[
            Attribute(name="leave_request_id", **pk), Attribute(name="reason", label="사유"),
            Attribute(name="days", data_type="integer", nullable=False), Attribute(name="password_hash"),
            Attribute(name="created_at", data_type="timestamp")]),
        Entity(entity_id="employee", function_ids=["FN-2"], attributes=[Attribute(name="employee_id", **pk)])])
    api = {"functions": {
        "FN-1": {"operations": [{"method": "POST", "path": "/leave-requests", "operation_id": "createLeaveRequest",
                                 "request": [{"name": "reason", "column": "leave_request.reason"}], "response": []}]},
        "FN-2": {"operations": [
            {"method": "GET", "path": "/leave-requests", "operation_id": "listLeaveRequests", "request": [], "response": []},
            {"method": "GET", "path": "/leave-request-summaries", "operation_id": "getLeaveRequestSummaries", "request": [], "response": []},
            {"method": "GET", "path": "/menus/{menuId}/exports", "operation_id": "getMenuExports", "request": [],
             "response": [{"name": "exportFile", "type": "string"}]}]}}}
    filled = scaffold.fill_read_responses(api, model)
    assert filled == ["GET /leave-requests"], "같은 모음 경로의 등록 API가 쓰는 테이블로 찾는다 (기능 테이블이 둘이라도)"
    listing = api["functions"]["FN-2"]["operations"][0]["response"]
    assert [f["column"] for f in listing] == ["leave_request.leave_request_id", "leave_request.reason", "leave_request.days"], \
        "비밀·감사 컬럼은 빼고 그 테이블 컬럼을 보여 준다"
    assert listing[2] == {"name": "days", "column": "leave_request.days", "type": "integer", "required": True, "description": ""}
    assert api["functions"]["FN-2"]["operations"][1]["response"] == [], "테이블을 정할 수 없는 요약 조회는 그대로 (업무 로직 몫)"
    assert api["functions"]["FN-2"]["operations"][2]["response"] == [{"name": "exportFile", "type": "string"}], "컬럼 없는 필드가 있으면 손대지 않는다"
    assert scaffold._named_table("/leave-requests/{id}", {"leave_request"}) == "leave_request"
    assert scaffold._named_table("/categories", {"category"}) == "category"


def test_unstored_inputs_are_optional_only_when_editing():
    api = {"functions": {"FN-1": {"operations": [
        {"method": "POST", "path": "/evidences", "request": [{"name": "kind", "required": True}, {"name": "no", "column": "evidence.no", "required": True}]},
        {"method": "PUT", "path": "/evidences/{evidenceId}", "request": [{"name": "kind", "required": True, "description": "증빙 종류"},
                                                                      {"name": "password", "required": True}]},
        {"method": "GET", "path": "/evidences", "request": [{"name": "keyword", "required": False}]}]}}}
    found = scaffold.relax_unstored_updates(api)
    post, put, _ = api["functions"]["FN-1"]["operations"]
    assert post["request"][0]["required"] is True, "등록에서는 스펙대로 필수"
    assert put["request"][0]["required"] is False, "저장하지 않는 값은 수정 화면이 불러올 수 없어 수정에서는 필수가 아니다"
    assert put["request"][1]["required"] is True, "바꿔 저장하는 값(비밀번호·파일)은 그대로"
    assert found == ["POST /evidences kind", "PUT /evidences/{evidenceId} kind(증빙 종류)"], "조회 조건은 저장하는 값이 아니다"


def test_sql_function_after_from_is_not_a_table():
    sql = 'ctx.db().query("SELECT EXTRACT(YEAR FROM CAST(:at AS timestamptz)) FROM voucher", java.util.Map.of("at", input.get("at")));\nreturn crud.get();'
    assert developer.check_hook("createVoucher", sql, {"createVoucher"}, {"voucher"}) == []


def test_logic_bodies_use_null_safe_parameter_maps():
    body = 'var rows = ctx.db().query("SELECT 1 FROM t WHERE d >= CAST(:from AS date)", java.util.Map.of("from", input.get("from")));\nreturn crud.get();'
    out = scaffold.hole("listT", body)
    assert "Map.of(" not in out and 'app.common.Db.values("from", input.get("from"))' in out, "비워 둔 조건(null)도 받는다"


def test_nextjs_frontend_uses_same_screens_under_ui_and_static_export(sample_doc):
    """화면 Next.js: React 틀의 화면 부품을 ui/로 옮기고(Next.js는 src/pages를 라우트로 본다) 정적 내보내기(out/)를 서버가 보여 준다."""
    spec = _spec(sample_doc)
    spec.parts = {**spec.parts, "frontend": "nextjs"}
    files, _ = scaffold.render_app(spec)
    assert not [p for p in files if p.startswith("frontend/src/")] and "frontend/ui/App.tsx" in files
    assert "frontend/next.config.mjs" in files and "frontend/app/page.tsx" in files and "frontend/vite.config.ts" not in files
    assert "process.env.NEXT_PUBLIC_API === 'server'" in files["frontend/ui/api.ts"]
    assert "NEXT_PUBLIC_API=server" in files["Dockerfile"] and "/web/out" in files["Dockerfile"] and "/web/dist" not in files["Dockerfile"]
    assert '"next"' in files["frontend/package.json"] and ".next/" in files[".gitignore"]
    assert files == scaffold.render_app(spec)[0], "같은 스펙이면 같은 결과"


def test_theme_changes_only_color_tokens_and_is_kept_for_next_develop(sample_doc):
    """화면 스타일: styles.css의 색 토큰만 바꾸는 theme.css (기본은 주석만). --theme은 프로젝트에 저장해 다음 개발도 같은 스타일"""
    from app.agents.tools import themes
    out = _start(sample_doc)
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0
    app = _app(out)
    assert "import './theme.css'" in (app / "frontend/src/main.tsx").read_text(encoding="utf-8")
    assert "--accent" not in (app / "frontend/src/theme.css").read_text(encoding="utf-8"), "기본 스타일은 바꾸는 토큰 없음"
    assert cli.main(["develop", "sample", "--from", FINAL, "--theme", "navy"]) == 0
    css = (app / "frontend/src/theme.css").read_text(encoding="utf-8")
    assert "--side: #1b2a4a;" in css and ':root[data-theme="dark"]' in css
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0 and "--side: #1b2a4a;" in (app / "frontend/src/theme.css").read_text(encoding="utf-8")
    assert cli.main(["develop", "sample", "--from", FINAL, "--theme", "nope"]) == 2
    assert len(themes.choices()) == 9 and all(set(t["preview"]) >= {"bg", "side", "accent"} for t in themes.choices())
