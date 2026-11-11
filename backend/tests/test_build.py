"""기능 구현 Agent (develop --build, 실험): 스펙 문서를 기능별로 잘라 넣고, 계약(경로·권한·스키마)을 벗어난 코드는 걸러 다시 묻는다 (API 불필요)."""
import re
from pathlib import Path

from test_develop import FINAL, _app, _start

from app import cli
from app.agents.llm import client as llm_module
from app.agents.roles import builder
from app.core.config import get_settings
from app.agents.tools import scaffold
from app.agents.tools.scaffold import class_name


def _controller(cls, ops, extra=""):
    methods = "\n".join(f'    @{op["method"].capitalize()}Mapping("{op["path"]}")\n    public Object m{i}() {{\n'
                        f'        CurrentUser.fromRequest().require("임직원");\n        return db.query("SELECT 1 FROM service_request", Db.values());\n    }}\n'
                        for i, op in enumerate(ops))
    return (f"package app.feature;\n\nimport app.common.*;\nimport org.springframework.web.bind.annotation.*;\n\n"
            f'@RestController\n@RequestMapping("/api/v1")\npublic class {cls}Controller {{\n    private final Db db;\n'
            f"    public {cls}Controller(Db db) {{ this.db = db; }}\n{methods}{extra}}}\n")


def _page(cls, path, method="GET"):
    return (f"import {{ call }} from '../kit'\nimport type {{ PageProps }} from '../kit'\n\n"
            f"export default function {cls}Page({{ role }}: PageProps) {{\n  void call('{method}', '{path}', role)\n  return <div />\n}}\n")


class _BuildLLM:
    """요청의 '만들 API'대로 컨트롤러·화면을 낸다. bad면 첫 응답에 계약 위반을 넣는다."""

    model = "fake"

    def __init__(self, bad=False):
        self.bad, self.calls, self.users = bad, 0, []

    def structured(self, *, system, user, schema, shared="", effort="", purpose="", **kw):
        self.calls += 1
        self.users.append(user)
        cls = re.search(r"클래스 이름: (\w+)Controller", user).group(1)
        ops = [{"method": m, "path": p} for m, p in re.findall(r"^- (GET|POST|PUT|PATCH|DELETE) (\S+) \(", user.split("## TASKS.md")[0], re.M)]
        first = ops[0] if ops else {"method": "GET", "path": "/nowhere"}
        if self.bad and "이전 응답의 오류" not in user:
            return schema(controller=_controller(cls, ops, '    @GetMapping("/extra")\n    public Object extra() { return Map.of(); }\n'),
                          page=_page(cls, "/not-in-spec"))
        return schema(controller=_controller(cls, ops), page=_page(cls, first["path"], first["method"]))

    def pop_usage(self):
        return {"calls": self.calls, "input_tokens": 10, "output_tokens": 20}


def _spec(sample_doc):
    out = _start(sample_doc)
    return out, scaffold.load_spec("sample", FINAL)


def test_request_is_cut_from_spec_documents_by_function(sample_doc):
    _, spec = _spec(sample_doc)
    (f, ops, request), *_ = builder.targets(spec, None)
    assert f"기능 {f.function_id}" in request and "## ACCEPTANCE_CRITERIA.md" in request and "CREATE TABLE" in request
    assert all(f"- {op['method']} {op['path']} ({op['operation_id']})" in request for op in ops), "만들 API는 이 기능이 만드는 것만"
    other = next(x for x in spec.functions if x.function_id != f.function_id)
    assert f"### {other.function_id} " not in request.split("## 다른 기능")[0], "다른 기능의 API 절은 넣지 않는다"
    assert "## 화면 부품" not in request and "export function DataTable" in builder._shared(spec), "공통 앞부분(캐시)에 kit 사용법"


def test_spec_documents_are_read_once_for_all_functions(sample_doc):
    """기능마다 같은 개발 스펙 문서를 자르지만 저장소에서는 문서마다 한 번만 읽는다 (개발 예상이 기능 수만큼 느려지지 않게)."""
    _, spec = _spec(sample_doc)
    reads, read = [], spec.read
    spec.read = lambda name: reads.append(name) or read(name)
    before = [r for _, _, r in builder.targets(spec, None)]
    assert len(before) > 1 and len(reads) == len(set(reads))
    assert [r for _, _, r in builder.targets(spec, None)] == before and len(reads) == len(set(reads)), "다시 계산해도 다시 읽지 않음"


def test_contract_violations_are_caught():
    ops = [{"method": "POST", "path": "/requests"}, {"method": "GET", "path": "/requests/{id}"}]
    good = _controller("FnA", ops)
    assert builder.check_controller(good, "FnA", ops, {"service_request"}) == []
    bad = _controller("FnA", ops[:1], '    @GetMapping("/extra")\n    public Object x() { return db.query("SELECT * FROM ghost", Map.of()); }\n')
    errors = builder.check_controller(bad, "FnA", ops, {"service_request"})
    assert any("만들 API가 없음: GET /requests/{id}" in e for e in errors)
    assert any("만들 API가 아님" in e for e in errors) and any("Map.of" in e for e in errors) and any("ghost" in e for e in errors)
    assert any("권한 검사가 없음" in e for e in errors), "권한 검사 없는 API"
    page = "import x from 'axios'\nexport default function FnAPage() { fetch('/x'); call('DELETE', `/requests/${id}`, role); return null }"
    errors = builder.check_page(page, "FnA", {("GET", "/requests/{id}")})
    assert any("axios" in e for e in errors) and any("fetch" in e for e in errors) and any("DELETE /requests/${id}" in e for e in errors)
    assert builder.check_page(_page("FnA", "/requests/7").replace("'/requests/7'", "`/requests/${id}`"), "FnA", {("GET", "/requests/{id}")}) == []
    one_line = good.replace("\n", " ") + " " * 1500
    assert any("몰아 씀" in e for e in builder.check_controller(one_line, "FnA", ops, {"service_request"})), "사람이 이어서 고칠 수 있게 줄을 바꾼다"
    dynamic = _page("FnA", "x", "POST").replace("'x'", "`/lines/${id}/${kind}`")
    assert builder.check_page(dynamic, "FnA", {("POST", "/lines/{lineId}/approve"), ("POST", "/lines/{lineId}/reject")}) == [], \
        "승인·반려를 변수 하나로 부르는 경로는 스펙 경로와 맞는다"


def test_code_that_always_fails_at_runtime_is_caught():
    """EC2 mock_회사: 형 없는 ':id IS NULL'(PostgreSQL 42P08)과 구현을 미루고 던진 500은 실행하면 늘 500이다"""
    untyped = "db.query(`SELECT 1 FROM budget_item bi WHERE (:budgetItemId IS NULL OR bi.budget_item_id = CAST(:budgetItemId AS bigint))`, { budgetItemId })"
    errors = builder.runtime_traps(untyped)
    assert len(errors) == 1 and "CAST(:budgetItemId AS bigint) IS NULL" in errors[0]
    assert builder.runtime_traps(untyped.replace("(:budgetItemId IS NULL", "(CAST(:budgetItemId AS bigint) IS NULL")) == []
    assert builder.runtime_traps("WHERE :x::bigint IS NULL OR x IS NOT NULL AND a.b IS NULL") == [], "형을 적었거나 컬럼이면 괜찮다"
    assert builder.runtime_traps("WHERE (:status is not null)") != [], "소문자·IS NOT NULL도"
    later = "throw new ApiError(500, 'INTERNAL_ERROR', '연차정보 양식을 만들 수 없습니다.');"
    assert any("구현을 미루지 않는다" in e for e in builder.runtime_traps(later))
    assert builder.runtime_traps("throw new ApiError(409, 'INVALID_STATE', '…')") == []


def test_db_crypto_functions_and_uninterpolated_placeholders_in_sql_are_caught():
    """EC2 자동 확인 500: 앱 DB에 없는 pgcrypto decrypt()(42883)와 따옴표 문자열 안의 ${…}('$' 문법 오류 42601)"""
    crypto = "const rows = await db.query(`SELECT c.card_id, decrypt(c.card_no, :key, 'aes') AS no FROM credit_card c`, { key });"
    errors = builder.runtime_traps(crypto)
    assert len(errors) == 1 and "decrypt()" in errors[0] and "pgcrypto" in errors[0]
    assert builder.runtime_traps('sql = """\n  SELECT pgp_sym_decrypt(card_no, :k) FROM credit_card\n"""') != [], "파이썬 세 따옴표도"
    assert builder.runtime_traps("const hash = createHash('sha256').update(x).digest('hex');") == [], "SQL이 아닌 코드의 digest()는 괜찮다"
    quoted = "await db.query('SELECT * FROM asset_inspection_plan WHERE plan_year = ${year}', {});"
    assert any(":이름 바인딩" in e for e in builder.runtime_traps(quoted))
    assert builder.runtime_traps("await db.query(`SELECT * FROM asset_inspection_plan p WHERE ${where}`, params);") == [], \
        "템플릿 문자열의 ${…}는 값이 들어간다"


def test_build_replaces_generated_code_only_for_built_functions_and_is_not_asked_twice(sample_doc):
    out, spec = _spec(sample_doc)
    fid = spec.functions[0].function_id
    fake = _BuildLLM()
    llm_module.set_llm(fake)
    assert cli.main(["develop", "sample", "--from", FINAL, "--build", "--only", fid]) == 0
    app, cls = _app(out), class_name(fid)
    assert (app / f"backend/src/main/java/app/feature/{cls}Controller.java").exists()
    assert not (app / f"backend/src/main/java/app/api/{cls}Controller.java").exists(), "코드가 만든 컨트롤러는 빼서 경로가 겹치지 않는다"
    assert not (app / f"backend/src/main/java/app/logic/{cls}Logic.java").exists()
    index = (app / "frontend/src/pages/index.ts").read_text(encoding="utf-8")
    assert f"'{fid}': {cls}Page" in index, "App은 이 기능을 공통 틀 대신 이 화면으로 그린다"
    others = [f for f in spec.functions if f.function_id != fid]
    assert all((app / f"backend/src/main/java/app/logic/{class_name(f.function_id)}Logic.java").exists() for f in others), "다른 기능은 그대로"
    calls = fake.calls
    assert cli.main(["develop", "sample", "--from", FINAL, "--build", "--only", fid]) == 0
    assert fake.calls == calls, "입력이 같으면 다시 묻지 않는다"


def test_bad_code_is_retried_and_failed_function_keeps_generated_code(sample_doc):
    out, spec = _spec(sample_doc)
    fid = spec.functions[0].function_id
    fake = _BuildLLM(bad=True)
    llm_module.set_llm(fake)
    result = builder.build(spec, _app(out), {fid})
    assert fake.calls == 2 and "이전 응답의 오류" in fake.users[1] and result["filled"] == [fid], "오류를 붙여 한 번 더 묻는다"

    class Broken(_BuildLLM):
        def structured(self, **kw):
            self.calls += 1
            return kw["schema"](controller="class X {}", page="")

    llm_module.set_llm(Broken())
    other = spec.functions[1].function_id
    assert builder.build(spec, _app(out), {other})["failed"] == [other]
    files, _ = scaffold.render_app(spec, built=builder.load_built(_app(out)))
    assert f"backend/src/main/java/app/api/{class_name(other)}Controller.java" in files or not scaffold.owned_operations(spec)[0][other], \
        "실패한 기능은 코드가 만든 틀로 둔다"
    assert f"frontend/src/pages/{class_name(other)}.tsx" not in files


def test_records_of_functions_not_in_this_final_are_ignored(sample_doc):
    """예전 최종본에만 있던 기능의 구현 기록이 남아 있어도, 화면 목록(pages/index.ts)·화면·컨트롤러에 넣지 않는다
    (넣으면 index.ts가 쓰지 않은 화면 파일을 불러 화면 빌드가 'Cannot find module'로 실패한다)."""
    out, spec = _spec(sample_doc)
    fid = spec.functions[0].function_id
    record = {"status": "채움", "controller": "class X {}", "page": "export default function P() { return null }"}
    files, _ = scaffold.render_app(spec, built={fid: record, "FN-OLD-999": record})
    index = files["frontend/src/pages/index.ts"]
    assert class_name(fid) in index and class_name("FN-OLD-999") not in index
    assert not any(class_name("FN-OLD-999") in path for path in files)


def test_without_build_nothing_changes(sample_doc):
    out, spec = _spec(sample_doc)
    files, _ = scaffold.render_app(spec)
    assert "frontend/src/pages/index.ts" in files and "export const pages: Record<string, ComponentType<PageProps>> = {}" in files["frontend/src/pages/index.ts"]
    assert "frontend/src/kit.tsx" in files and not any("/feature/" in p for p in files)
    assert get_settings().dev_effort


def test_type_check_runs_with_relative_output_dir(tmp_path, monkeypatch):
    """화면에서 시작한 개발은 --output-dir가 상대 경로(output)다. cwd를 frontend로 바꿔 tsc를 실행해도 찾아야 한다
    (상대 경로 그대로 넘기면 FileNotFoundError로 개발 작업 전체가 실패했다)."""
    import os
    import pytest

    if os.name == "nt":
        pytest.skip("가짜 tsc를 셸 스크립트로 만든다")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(builder.exe, "which", lambda name: None)   # mvn·docker·npm 없음: 화면 타입 검사만
    cls = class_name("FN-001")
    bin_dir = tmp_path / "output/p/app/frontend/node_modules/.bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tsc").write_text(f"#!/bin/sh\necho 'src/pages/{cls}.tsx(3,5): error TS2322: 타입이 맞지 않음'\nexit 2\n")
    (bin_dir / "tsc").chmod(0o755)
    errors, skipped = builder.verify(Path("output/p/app"), {"FN-001": {"status": "채움"}})
    assert errors == {"FN-001": ["타입 오류 3행: TS2322: 타입이 맞지 않음"]}
    assert not any("화면 타입 검사" in s for s in skipped)
