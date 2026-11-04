"""FastAPI 서버 틀: Spring 틀과 같은 HTTP 계약의 Python 코드를 스펙에서 만든다 (LLM 없음, 같은 스펙이면 같은 파일)."""
import ast
import json
import re

from test_develop import _spec

from app.agents.tools import scaffold, scaffold_fastapi as fa
from app.agents.tools.screens import build_screens


def _render(spec, logic=None, built=None):
    data = build_screens(spec, {})
    data["accounts"] = scaffold.demo_accounts(data)
    owned, _ = scaffold.owned_operations(spec)
    return fa.render_backend(spec, data, owned, logic or {}, built or {}), owned


def test_same_spec_gives_same_python_files_that_parse(sample_doc):
    spec = _spec(sample_doc)
    first, _ = _render(spec)
    second, _ = _render(spec)
    assert first == second, "같은 스펙이면 파일이 바이트까지 같다"
    assert all(p.startswith("backend/") for p in first)
    for path, text in first.items():
        if path.endswith(".py"):
            ast.parse(text, filename=path)   # 만든 Python이 문법에 맞다
    for path in ("backend/app/main.py", "backend/app/common/db.py", "backend/app/auth/accounts.py", "backend/requirements.txt",
                 "backend/app/api/__init__.py", "backend/app/api/lookups.py"):
        assert path in first


def test_every_owned_operation_has_a_route_and_a_hole(sample_doc):
    spec = _spec(sample_doc)
    files, owned = _render(spec)
    for f in spec.functions:
        ops = owned[f.function_id]
        mod = fa.module_name(f.function_id)
        logic = files[f"backend/app/logic/{mod}.py"]
        holes = re.findall(r"# <hole (\w+)>\n(.*?)\n[ \t]*# </hole>", logic, re.S)
        if not ops:
            assert [h[0] for h in holes] == ["run"]
            continue
        api = files[f"backend/app/api/{mod}.py"]
        assert f"from app.api import {mod}" in files["backend/app/api/__init__.py"]
        for op, name in zip(ops, fa.func_names(ops)):
            assert f"@router.{op['method'].lower()}({op['path']!r})" in api, "API마다 경로 하나"
            assert f"return logic.{name}(data, Ctx(db, user), lambda: " in api
        assert [h[0] for h in holes] == fa.func_names(ops)
        assert all(body.strip() == "return crud()" for _, body in holes), "기본 본문은 기본 동작 그대로"


def test_accounts_are_two_and_filled_python_hooks_are_used_but_java_is_not(sample_doc):
    spec = _spec(sample_doc)
    owned, _ = scaffold.owned_operations(spec)
    f = next(f for f in spec.functions if owned[f.function_id])
    name = fa.func_names(owned[f.function_id])[0]
    files, _ = _render(spec, {f.function_id: {name: "result = crud()\nreturn result"}})
    accounts = json.loads(files["backend/app/accounts.json"])
    assert [a["loginId"] for a in accounts] == ["admin", "user"] and accounts[0]["role"] == scaffold.ALL_ROLES
    logic = files[f"backend/app/logic/{fa.module_name(f.function_id)}.py"]
    assert f"    # <hole {name}>\n    result = crud()\n    return result\n    # </hole>" in logic
    java, _ = _render(spec, {f.function_id: {name: "return crud.get();"}})
    assert "return crud.get();" not in java[f"backend/app/logic/{fa.module_name(f.function_id)}.py"], "Java 본문은 쓰지 않는다"


def test_python_built_module_replaces_generated_api_and_java_is_ignored(sample_doc):
    spec = _spec(sample_doc)
    owned, _ = scaffold.owned_operations(spec)
    f = next(f for f in spec.functions if owned[f.function_id])
    mod = fa.module_name(f.function_id)
    code = "from fastapi import APIRouter\n\nrouter = APIRouter(prefix='/api/v1')\n"
    files, _ = _render(spec, built={f.function_id: {"status": "채움", "controller": code, "page": "x"}})
    assert files[f"backend/app/feature/{mod}.py"] == code
    assert f"backend/app/api/{mod}.py" not in files and f"from app.feature import {mod}" in files["backend/app/api/__init__.py"]
    java, _ = _render(spec, built={f.function_id: {"status": "채움", "controller": "public class X {}", "page": "x"}})
    assert f"backend/app/feature/{mod}.py" not in java and f"backend/app/api/{mod}.py" in java


def test_compose_and_dockerfile_run_python_server():
    assert "postgresql://app:app@db:5432/app" in fa.COMPOSE and "jdbc:" not in fa.COMPOSE
    assert '"${APP_HOST:-127.0.0.1}:${APP_PORT:-8080}:8080"' in fa.COMPOSE and "./local:/app/local" in fa.COMPOSE
    assert "python:3.12-slim" in fa.DOCKERFILE and "uvicorn" in fa.DOCKERFILE and "VITE_API=server" in fa.DOCKERFILE


def test_java_values_become_python_values():
    assert [fa._py(v) for v in ("Db.NOW", "user.id()", "Db.autoNo(20)", "true", "5L", "1.5", '"접수"')] == \
        ["NOW", "user.id", "auto_no(20)", "True", "5", "1.5", "'접수'"]


def _fastapi(spec, database="postgresql"):
    spec.parts = {**scaffold.stack_parts(spec), "backend": "fastapi", "database": database}
    return spec


def test_render_app_with_fastapi_stack_makes_python_server_and_no_java(sample_doc):
    spec = _fastapi(_spec(sample_doc))
    assert scaffold.has_backend(spec)
    files, _ = scaffold.render_app(spec)
    assert "backend/app/main.py" in files and "backend/app/accounts.json" in files
    assert not [p for p in files if p.endswith(".java") or p == "backend/pom.xml"]
    assert files["docker-compose.yml"] == fa.COMPOSE and files["Dockerfile"] == fa.DOCKERFILE
    assert "uvicorn app.main:app" in files["README.md"] and "mvn" not in files["README.md"] and "`backend/app/logic/`" in files["README.md"]
    mysql, _ = scaffold.render_app(_fastapi(spec, "mysql"))
    assert mysql["docker-compose.yml"] == fa.COMPOSE_MYSQL and "mysql+pymysql://app:app@db:3306/app" in fa.COMPOSE_MYSQL
    assert "image: mysql:8.4" in fa.COMPOSE_MYSQL and "jdbc:" not in fa.COMPOSE_MYSQL
    assert "ENGINE=InnoDB" in mysql["db/schema.sql"]


def test_logic_fills_fastapi_and_build_prompt_is_python_with_same_page_rules(sample_doc):
    from app.agents.roles import builder, developer

    spec = _spec(sample_doc)
    spring = builder.system_prompt(spec)
    assert spring == builder.SYSTEM_PROMPT, "Spring · PostgreSQL 프롬프트는 그대로"
    _fastapi(spec)
    python = builder.system_prompt(spec)
    assert "Python 3.12 · FastAPI" in python and "@RestController" not in python
    assert python[python.index("[화면: page]"):] == spring[spring.index("[화면: page]"):]
    owned, _ = scaffold.owned_operations(spec)
    f = next(f for f in spec.functions if owned[f.function_id])
    assert f"- 모듈: app/feature/{fa.module_name(f.function_id)}.py" in builder.build_request(spec, f, owned[f.function_id])
    assert developer.supports_logic(spec) and developer.targets(spec), "--logic은 이 틀의 자리도 채운다 (test_logic_stacks)"


def _module(ops, body="    return {}", imports=""):
    routes = "\n\n\n".join(f"@router.{op['method'].lower()}({op['path']!r})\ndef op{i}(request: Request):\n"
                           f"    user = current_user(request)\n    user.require('담당자')\n{body}"
                           for i, op in enumerate(ops))
    return ("from fastapi import APIRouter, Request\n\nfrom app.common.db import transaction\nfrom app.common.user import current_user\n"
            f"{imports}\nrouter = APIRouter(prefix=\"/api/v1\")\n\n\n{routes}\n")


def test_python_module_checks_routes_permission_imports_tables_and_names(sample_doc):
    from app.agents.roles import builder

    spec = _fastapi(_spec(sample_doc))
    owned, _ = scaffold.owned_operations(spec)
    f = next(f for f in spec.functions if owned[f.function_id])
    ops = owned[f.function_id]
    tables = {e.entity_id for e in spec.ddl.entities}
    table = sorted(tables)[0]
    check = lambda code, o=ops: builder.check_server(spec, code, "X", o, tables)   # noqa: E731
    good = _module(ops, f"    with transaction() as db:\n        return db.query('SELECT 1 FROM {table}', {{}})")
    assert check(good) == []
    assert any("만들 API가 없음" in e for e in check(_module(ops[1:])) ) if len(ops) > 1 else True
    assert any("만들 API가 아님" in e for e in check(good, ops[:0]))
    assert any("권한 검사가 없음" in e for e in check(good.replace("    user.require('담당자')\n", "")))
    assert any("쓸 수 없는 import os" in e for e in check(_module(ops, imports="import os\n")))
    assert any("스키마에 없는 테이블 'nope'" in e for e in check(_module(ops, "    with transaction() as db:\n        return db.query('SELECT * FROM nope')")))
    assert any("정의하지 않은 이름 'missing'" in e for e in check(_module(ops, "    return missing")))
    assert any("쓸 수 없는 표현 'eval'" in e for e in check(_module(ops, "    return eval('1')")))
    assert any("문법 오류" in e for e in check("def broken(:\n"))


def test_verify_imports_python_feature_modules(sample_doc, tmp_path):
    from app.agents.roles import builder_fastapi

    spec = _fastapi(_spec(sample_doc))
    owned, _ = scaffold.owned_operations(spec)
    a, b = [f.function_id for f in spec.functions if owned[f.function_id]][:2]
    page = "export default function X() { return null }\n"
    records = {a: {"status": "채움", "page": page, "controller": "from fastapi import APIRouter\n\nrouter = APIRouter(prefix='/api/v1')\n"},
               b: {"status": "채움", "page": page, "controller": "from app.common.nothing import x\n\nrouter = None\n"}}
    files, _ = scaffold.render_app(spec, built=records)
    scaffold.write_app(files, tmp_path)
    errors, _ = builder_fastapi.verify(tmp_path, {fa.module_name(a): a, fa.module_name(b): b})   # 화면 타입 검사(npm)는 하지 않는다
    assert a not in errors and "ModuleNotFoundError" in errors[b][0], errors
