"""develop --logic의 FastAPI·NestJS 틀: 그 틀의 언어로 자리만 채우고(가짜 LLM), 검사·기록 형식·같은 입력이면 같은 파일을 본다."""
import ast
import re

from test_develop import _app, _spec, _start, FINAL

from app import cli
from app.agents.llm import client as llm_module
from app.agents.roles import developer
from app.agents.tools import scaffold, scaffold_fastapi as fa, scaffold_nestjs as ne

PY_BODY = 'if not data.get("title"):\n    raise ctx.fail(400, "EMPTY", "값이 없습니다.")\nresult = crud()\nreturn result'
TS_BODY = "if (!data['title']) {\n  throw ctx.fail(400, 'EMPTY', '값이 없습니다.');\n}\nconst result = (await crud()) as Data;\nreturn result;"


class _HoleLLM:
    """'채울 메서드'의 첫 자리에 정해진 본문을 낸다. bad를 주면 첫 응답은 bad 본문이다."""

    model = "fake"

    def __init__(self, body, bad=None):
        self.body, self.bad, self.calls, self.users, self.systems = body, bad, 0, [], []

    def structured(self, *, system, user, schema, shared="", effort="", purpose="", **kw):
        self.calls += 1
        self.users.append(user)
        self.systems.append(system)
        method = re.search(r"^- (\w+): ", user.split("## 채울 메서드")[1], re.M).group(1)
        body = self.bad if self.bad and "이전 응답의 오류" not in user else self.body
        return schema(hooks=[{"method": method, "body": body}])

    def pop_usage(self):
        return {"calls": self.calls, "input_tokens": 10, "output_tokens": 20}


def _stack(spec, backend, database="postgresql"):
    spec.parts = {**scaffold.stack_parts(spec), "backend": backend, "database": database}
    return spec


def _logic_files(files, ext):
    return {p: t for p, t in files.items() if re.match(rf"backend/(app|src)/logic/\w+\.{ext}$", p) and "__init__" not in p}


def test_prompts_and_plan_follow_the_stack_and_spring_stays_the_same(sample_doc, tmp_path):
    spec = _spec(sample_doc)
    assert developer.system_prompt(spec) == developer.SYSTEM_PROMPT, "Spring · PostgreSQL 프롬프트는 그대로"
    spring = {f.function_id: r for f, _, r in developer.targets(spec)}
    for backend, word in (("fastapi", "Python 3.12 · FastAPI"), ("nestjs", "TypeScript · NestJS")):
        _stack(spec, backend)
        assert word in developer.system_prompt(spec) and "Java 17" not in developer.system_prompt(spec)
        plan = developer.plan(spec, tmp_path)
        assert plan["functions"] == len(spring) and plan["input_tokens"] > 0 and plan["cost"]
        assert "MySQL 8" in developer.system_prompt(_stack(spec, backend, "mysql")), "MySQL이면 같은 문장을 바꾼다"
    _stack(spec, "fastapi")
    owned, _ = scaffold.owned_operations(spec)
    for f, ops, request in developer.targets(spec):
        assert all(f"- {name}: " in request for name in fa.func_names(ops)), "자리 이름은 FastAPI 함수 이름"


def test_fastapi_holes_are_filled_with_python_and_records_keep_the_format(sample_doc, tmp_path):
    spec = _stack(_spec(sample_doc), "fastapi")
    fake = _HoleLLM(PY_BODY)
    llm_module.set_llm(fake)
    result = developer.fill_logic(spec, tmp_path)
    assert result["filled"] and not result["failed"] and fake.calls == len(result["filled"])
    assert all("Python 3.12" in s for s in fake.systems)
    records = scaffold.load_logic(tmp_path)
    owned, _ = scaffold.owned_operations(spec)
    for fid, record in records.items():
        assert record["status"] == "채움" and record["stack"] == "fastapi"
        assert set(record["hooks"]) <= set(fa.func_names(owned[fid]) or ["run"]), "hooks는 {자리 이름: 본문}"
    files, _ = scaffold.render_app(spec, {}, developer.hooks_of(spec, tmp_path))
    again, _ = scaffold.render_app(spec, {}, developer.hooks_of(spec, tmp_path))
    assert files == again, "같은 기록이면 파일이 바이트까지 같다"
    filled = [t for t in _logic_files(files, "py").values() if "EMPTY" in t]
    assert len(filled) == len(records)
    for text in filled:
        ast.parse(text)
        assert '    # <hole ' in text and '        raise ctx.fail(400, "EMPTY", "값이 없습니다.")\n    result = crud()\n    return result\n    # </hole>' in text
    developer.fill_logic(spec, tmp_path)
    assert fake.calls == len(records), "입력이 같으면 다시 묻지 않는다"
    errors, skipped = developer.verify(spec, _written(files, tmp_path / "app"), sorted(records))
    assert errors == {} and skipped == [], "채운 모듈을 불러올 수 있다"


def _written(files, folder):
    for path, text in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_text(text, encoding="utf-8")
    return folder


def test_nestjs_holes_are_filled_with_typescript_and_spring_records_are_not_used(sample_doc, tmp_path):
    spec = _stack(_spec(sample_doc), "nestjs")
    fid = developer.targets(spec)[0][0].function_id
    scaffold.save_logic(tmp_path, fid, {"fingerprint": "x", "status": "채움", "hooks": {"x": "return crud.get();"}})   # Spring 기록
    assert developer.hooks_of(spec, tmp_path) == {}, "다른 틀의 기록은 넣지 않는다"
    fake = _HoleLLM("  " + TS_BODY.replace("\n", "\n  "))   # 통째로 들여 쓴 답도 자리 들여쓰기에 맞춘다
    llm_module.set_llm(fake)
    result = developer.fill_logic(spec, tmp_path)
    assert fid in result["filled"] and all("TypeScript · NestJS" in s for s in fake.systems)
    assert all("await crud()" in u for u in fake.users if "자동 처리" in u)
    records = scaffold.load_logic(tmp_path)
    assert records[fid]["stack"] == "nestjs" and set(records[fid]["hooks"]) <= set(ne.func_names(scaffold.owned_operations(spec)[0][fid]) or ["run"])
    files, _ = scaffold.render_app(spec, {}, developer.hooks_of(spec, tmp_path))
    assert files == scaffold.render_app(spec, {}, developer.hooks_of(spec, tmp_path))[0]
    text = files[f"backend/src/logic/{ne.module_name(fid)}.ts"]
    assert "  if (!data['title']) {\n    throw ctx.fail(400, 'EMPTY', '값이 없습니다.');\n  }\n  const result = (await crud()) as Data;\n  return result;\n  // </hole>" in text
    assert fid not in developer.hooks_of(_stack(spec, "spring"), tmp_path), "NestJS 기록은 Spring 자리에 넣지 않는다"


def test_generators_ignore_bodies_of_other_languages(sample_doc):
    spec = _stack(_spec(sample_doc), "nestjs")
    owned, _ = scaffold.owned_operations(spec)
    f = next(f for f in spec.functions if owned[f.function_id])
    name = ne.func_names(owned[f.function_id])[0]
    for body in ("return crud.get();", "if not data:\n    return None\nreturn crud()", "if (x) {\nreturn crud();"):
        files, _ = scaffold.render_app(spec, {}, {f.function_id: {name: body}})
        assert f"  // <hole {name}>\n  return crud();\n  // </hole>" in files[f"backend/src/logic/{ne.module_name(f.function_id)}.ts"], body
    _stack(spec, "fastapi")
    py = fa.func_names(owned[f.function_id])[0]
    dict_body = 'params = {\n    "id": data.get("id"),\n}\nreturn crud()'
    files, _ = scaffold.render_app(spec, {}, {f.function_id: {py: dict_body}})
    assert '        "id": data.get("id"),\n    }\n    return crud()' in files[f"backend/app/logic/{fa.module_name(f.function_id)}.py"], "}로 끝나는 dict 줄은 Python이다"


def test_python_check_rejects_bad_bodies():
    allowed, tables, reads = {"create_voucher", "list_vouchers"}, {"voucher"}, {"list_vouchers"}
    check = lambda body, method="create_voucher": developer.check_hook_py(method, body, allowed, tables, reads)   # noqa: E731
    assert check(PY_BODY) == []
    sql = 'rows = ctx.db.query("SELECT 1 FROM voucher WHERE voucher_id = :id", {"id": data.get("id")})\nimport json\nreturn crud()'
    assert check(sql) == []
    assert check("result = crud()\nreturn result", "list_vouchers") == []
    bad = {
        "import os\nreturn crud()": "쓸 수 없는 import os",
        "open('x')\nreturn crud()": "쓸 수 없는 표현 'open'",
        "return crud.get();": "문장 끝에 ;",
        "result = crud()": "return이 없음",
        "return foo(crud())": "정의하지 않은 이름 'foo'",
        sql.replace("FROM voucher", "FROM secret_table"): "스키마에 없는 테이블 'secret_table'",
        'if ctx.user.role == "관리자":\n    return crud()\nreturn crud()': "역할로 권한을 다시 검사함",
        "x = data.get('d').strptime('%Y')\nreturn crud()": "파싱함",
        "async def f():\n    pass\nreturn crud()": "쓸 수 없는 문장",
        "return data.__class__": "'__class__'",
        "if x\n    return 1": "문법 오류",
        "# 할 일만 적음": "주석만 있고",
    }
    for body, message in bad.items():
        assert any(message in e for e in check(body)), (body, check(body))
    assert any("crud()를 부르지 않음" in e for e in check("return {}", "list_vouchers"))
    assert any("API 목록에 없는 함수" in e for e in check("return crud()", "other"))


def test_typescript_check_rejects_bad_bodies():
    allowed, tables, reads = {"createVoucher", "listVouchers"}, {"voucher"}, {"listVouchers"}
    check = lambda body, method="createVoucher": developer.check_hook_ts(method, body, allowed, tables, reads)   # noqa: E731
    assert check(TS_BODY) == []
    sql = "const rows = await ctx.db.query('SELECT 1 FROM voucher WHERE voucher_id = :id', { id: data['id'] });\nreturn crud();"
    assert check(sql) == []
    bad = {
        "const fs = require('fs');\nreturn crud();": "'require'",
        "const k = process.env.KEY;\nreturn crud();": "'process'",
        "eval('1');\nreturn crud();": "'eval'",
        "import x from 'y';\nreturn crud();": "'import'",
        "if (data['x']) {\nreturn crud();": "괄호가 맞지 않음",
        "return crud.get();": "Java 문법",
        "if not data:\n    raise ctx.fail(400, 'E', 'x')\nreturn None": "Python 문법",
        "const r = await crud();": "return이 없음",
        sql.replace("FROM voucher", "FROM secret_table"): "스키마에 없는 테이블 'secret_table'",
        "if (ctx.user.role === '관리자') {\n  return crud();\n}\nreturn crud();": "역할로 권한을 다시 검사함",
        "const d = new Date(data['at']);\nreturn crud();": "파싱함",
        "// 할 일만 적음": "주석만 있고",
    }
    for body, message in bad.items():
        assert any(message in e for e in check(body)), (body, check(body))
    assert any("crud()를 부르지 않음" in e for e in check("return {};", "listVouchers"))


def test_bad_body_is_retried_then_default_is_kept(sample_doc, tmp_path):
    spec = _stack(_spec(sample_doc), "fastapi")
    fid = developer.targets(spec)[0][0].function_id
    fake = _HoleLLM(PY_BODY, bad="import os\nreturn crud()")
    llm_module.set_llm(fake)
    assert developer.fill_logic(spec, tmp_path, {fid})["filled"] == [fid]
    assert fake.calls == 2 and "쓸 수 없는 import os" in fake.users[1], "오류를 붙여 한 번 더 묻는다"
    never = _HoleLLM("const fs = require('fs');\nreturn crud();")
    llm_module.set_llm(never)
    _stack(spec, "nestjs")
    result = developer.fill_logic(spec, tmp_path / "ts", {fid})
    assert result["failed"] == [fid] and never.calls == 2 and scaffold.load_logic(tmp_path / "ts")[fid]["hooks"] == {}


def test_body_that_breaks_type_check_goes_back_to_default(sample_doc, monkeypatch):
    out = _start(sample_doc)
    spec = _stack(scaffold.load_spec("sample", FINAL), "nestjs")
    fid = developer.targets(spec)[0][0].function_id
    llm_module.set_llm(_HoleLLM(TS_BODY))
    seen = []
    monkeypatch.setattr(developer, "verify", lambda spec, out_dir, fids: (seen.append(fids), ({fid: ["타입 오류 3행: TS2339: x"]}, []))[1])
    assert cli.main(["develop", "sample", "--from", FINAL, "--stack", "backend=nestjs", "--logic", "--only", fid]) == 0
    assert seen == [[fid]]
    record = scaffold.load_logic(_app(out))[fid]
    assert record["status"] == "실패" and record["hooks"] == {} and record["rejected"] and record["errors"] == ["타입 오류 3행: TS2339: x"]
    text = (_app(out) / f"backend/src/logic/{ne.module_name(fid)}.ts").read_text(encoding="utf-8")
    assert "EMPTY" not in text and "  return crud();\n  // </hole>" in text, "틀린 기능은 기본 동작으로 되돌린다"
