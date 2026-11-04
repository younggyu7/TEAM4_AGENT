"""NestJS 서버 틀: Spring·FastAPI 틀과 같은 HTTP 계약의 TypeScript 코드를 스펙에서 만든다 (LLM 없음, 같은 스펙이면 같은 파일)."""
import json
import re

from test_develop import _spec

from app.agents.tools import scaffold, scaffold_fastapi as fa, scaffold_nestjs as ne
from app.agents.tools.screens import build_screens
from app.services import appfiles


def _render(spec, built=None):
    data = build_screens(spec, {})
    data["accounts"] = scaffold.demo_accounts(data)
    owned, _ = scaffold.owned_operations(spec)
    return ne.render_backend(spec, data, owned, built or {}), owned


def _nest(spec, database="postgresql"):
    spec.parts = {**scaffold.stack_parts(spec), "backend": "nestjs", "database": database}
    return spec


def test_same_spec_gives_same_typescript_files(sample_doc):
    spec = _spec(sample_doc)
    first, _ = _render(spec)
    second, _ = _render(spec)
    assert first == second, "같은 스펙이면 파일이 바이트까지 같다"
    assert all(p.startswith("backend/") for p in first)
    for path in ("backend/package.json", "backend/tsconfig.json", "backend/src/main.ts", "backend/src/app.module.ts", "backend/src/common/db.ts",
                 "backend/src/auth/accounts.ts", "backend/src/api/index.ts", "backend/src/api/lookups.ts", "backend/src/accounts.json"):
        assert path in first
    deps = json.loads(first["backend/package.json"])
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in {**deps["dependencies"], **deps["devDependencies"]}.values()), "버전은 정확히 고정"


def test_every_owned_operation_has_a_route_and_an_empty_hole(sample_doc):
    spec = _spec(sample_doc)
    files, owned = _render(spec)
    index = files["backend/src/api/index.ts"]
    for f in spec.functions:
        ops = owned[f.function_id]
        mod = ne.module_name(f.function_id)
        logic = files[f"backend/src/logic/{mod}.ts"]
        found = appfiles.holes(logic)
        assert all(h["empty"] for h in found), "기본 본문은 빈 로직으로 센다"
        if not ops:
            assert [h["name"] for h in found] == ["run"]
            continue
        api = files[f"backend/src/api/{mod}.ts"]
        assert f"import {{ {ne.controller_name(f.function_id)} }} from './{mod}';" in index
        assert f"export class {ne.controller_name(f.function_id)} " in api and "@Controller('api/v1')" in api
        for op, name in zip(ops, ne.func_names(ops)):
            assert f"@{ne.NEST[op['method']]}({ne._s(ne.nest_path(op['path']))})" in api, "API마다 경로 하나"
            assert f"logic.{name}(data, new Ctx(db, user), () => " in api
        assert api.count("@HttpCode(200)") == sum(op["method"] == "POST" for op in ops), "POST도 200 (Spring·FastAPI와 같게)"
        assert [h["name"] for h in found] == ne.func_names(ops)
        assert all(f"  // <hole {n}>\n  return crud();\n  // </hole>" in logic for n in ne.func_names(ops))


def test_crud_calls_match_fastapi_template(sample_doc):
    """기본 동작(등록·조회·수정·삭제·상태 전환)의 인자는 FastAPI 틀과 같다 (문법만 다르다)."""
    spec = _spec(sample_doc)
    files, owned = _render(spec)
    data = build_screens(spec, {})
    data["accounts"] = scaffold.demo_accounts(data)
    py = fa.render_backend(spec, data, owned)

    def norm(text: str) -> str:
        text = re.sub(r"data\.get\(('[^']*')\)", r"data[\1]", text).replace("auto_no", "autoNo").replace("move_to", "moveTo")
        text = text.replace("not_implemented", "notImplemented").replace("True", "true").replace("False", "false").replace('"', "'")
        return re.sub(r"\{\s*", "{", re.sub(r"\s*\}", "}", text))

    for f in spec.functions:
        mod = ne.module_name(f.function_id)
        if not owned[f.function_id]:
            continue
        a = [norm(x) for x in re.findall(r"lambda: (.*)\)$", py[f"backend/app/api/{mod}.py"], re.M)]
        b = [norm(x) for x in re.findall(r"new Ctx\(db, user\), \(\) => (.*)\)\);$", files[f"backend/src/api/{mod}.ts"], re.M)]
        assert a == b, f.function_id


def test_accounts_and_built_typescript_replaces_generated_api_but_java_python_are_ignored(sample_doc):
    spec = _spec(sample_doc)
    owned, _ = scaffold.owned_operations(spec)
    f = next(f for f in spec.functions if owned[f.function_id])
    mod, cls = ne.module_name(f.function_id), ne.controller_name(f.function_id)
    files, _ = _render(spec)
    accounts = json.loads(files["backend/src/accounts.json"])
    assert [a["loginId"] for a in accounts] == ["admin", "user"] and accounts[0]["role"] == scaffold.ALL_ROLES
    assert all(isinstance(v, str) for a in accounts for v in a.values())
    code = f"import {{ Controller }} from '@nestjs/common';\n\n@Controller('api/v1')\nexport class {cls} {{}}\n"
    built, _ = _render(spec, {f.function_id: {"status": "채움", "controller": code, "page": "x"}})
    assert built[f"backend/src/feature/{mod}.ts"] == code
    assert f"backend/src/api/{mod}.ts" not in built and f"backend/src/logic/{mod}.ts" not in built
    assert f"import {{ {cls} }} from '../feature/{mod}';" in built["backend/src/api/index.ts"]
    for other in ("public class X {}", "from fastapi import APIRouter\n\nrouter = APIRouter(prefix='/api/v1')\n"):
        kept, _ = _render(spec, {f.function_id: {"status": "채움", "controller": other, "page": "x"}})
        assert f"backend/src/feature/{mod}.ts" not in kept and f"backend/src/api/{mod}.ts" in kept


def test_compose_and_dockerfile_run_node_server():
    assert "DB_URL: postgresql://app:app@db:5432/app" in ne.COMPOSE and "jdbc:" not in ne.COMPOSE and "LOCAL_DIR: /app/local" in ne.COMPOSE
    assert '"${APP_HOST:-127.0.0.1}:${APP_PORT:-8080}:8080"' in ne.COMPOSE and "./local:/app/local" in ne.COMPOSE
    assert '"127.0.0.1:${DB_PORT:-5433}:5432"' in ne.COMPOSE and "image: postgres:16" in ne.COMPOSE
    assert 'DB_URL: "mysql://app:app@db:3306/app"' in ne.COMPOSE_MYSQL and "image: mysql:8.4" in ne.COMPOSE_MYSQL and "jdbc:" not in ne.COMPOSE_MYSQL
    web = scaffold.DOCKERFILE[:scaffold.DOCKERFILE.index("RUN npm run build\n")]
    assert web.split("\n", 1)[1] in ne.DOCKERFILE, "화면 단계는 다른 틀과 같은 모양"
    for part in ("ENV VITE_API=server", "COPY --from=web /web/dist ./static", "RUN npm run build", 'CMD ["node", "dist/main.js"]',
                 "EXPOSE 8080", "ENV LOCAL_DIR=/app/local"):
        assert part in ne.DOCKERFILE


def test_render_app_with_nestjs_stack_makes_typescript_server_and_no_java(sample_doc):
    spec = _nest(_spec(sample_doc))
    assert scaffold.has_backend(spec)
    files, _ = scaffold.render_app(spec)
    assert "backend/src/main.ts" in files and "backend/src/accounts.json" in files
    assert not [p for p in files if p.endswith((".java", ".py")) or p == "backend/pom.xml"]
    assert files["docker-compose.yml"] == ne.COMPOSE and files["Dockerfile"] == ne.DOCKERFILE and files[".dockerignore"] == ne.DOCKERIGNORE
    assert "npm start" in files["README.md"] and "mvn" not in files["README.md"] and "`backend/src/logic/`" in files["README.md"]
    mysql, _ = scaffold.render_app(_nest(spec, "mysql"))
    assert mysql["docker-compose.yml"] == ne.COMPOSE_MYSQL and "ENGINE=InnoDB" in mysql["db/schema.sql"]
    assert mysql["backend/src/common/db.ts"] == files["backend/src/common/db.ts"], "DB는 DB_URL로 고른다 (틀 하나)"
    spec.parts = {**_nest(spec).parts, "frontend": "nextjs"}
    nextjs, _ = scaffold.render_app(spec)
    assert "COPY --from=web /web/out ./static" in nextjs["Dockerfile"]


def test_logic_fills_nestjs_and_build_prompt_is_typescript_with_same_page_rules(sample_doc):
    from app.agents.roles import builder, developer

    spec = _spec(sample_doc)
    spring = builder.system_prompt(spec)
    _nest(spec)
    nest = builder.system_prompt(spec)
    assert "NestJS" in nest and "@RestController" not in nest and "Python" not in nest
    assert nest[nest.index("[화면: page]"):] == spring[spring.index("[화면: page]"):]
    owned, _ = scaffold.owned_operations(spec)
    f = next(f for f in spec.functions if owned[f.function_id])
    head = f"- 모듈: src/feature/{ne.module_name(f.function_id)}.ts · 클래스 이름: {ne.controller_name(f.function_id)}"
    assert head in builder.build_request(spec, f, owned[f.function_id])
    assert developer.supports_logic(spec) and developer.targets(spec), "--logic은 이 틀의 자리도 채운다 (test_logic_stacks)"
    mysql = builder.system_prompt(_nest(spec, "mysql"))
    assert "CAST(:userId AS SIGNED)" in mysql and "LAST_INSERT_ID()" in mysql and "[DB: MySQL 8]" in mysql


def _controller(cls, ops, body="    return transaction(async (db) => db.query('SELECT 1', {}));", imports=""):
    methods = "\n\n".join(f"  @{ne.NEST[op['method']]}({ne._s(ne.nest_path(op['path']))})\n  async op{i}(@Req() req: Request) {{\n"
                          f"    const user = currentUser(req);\n    user.require('담당자');\n{body}\n  }}" for i, op in enumerate(ops))
    return ("import { Controller, Delete, Get, HttpCode, Patch, Post, Put, Req } from '@nestjs/common';\nimport type { Request } from 'express';\n\n"
            "import { transaction } from '../common/db';\nimport { currentUser } from '../common/user';\n"
            f"{imports}\n@Controller('api/v1')\nexport class {cls}Controller {{\n{methods}\n}}\n")


def test_typescript_controller_checks_routes_permission_imports_tables_and_forbidden(sample_doc):
    from app.agents.roles import builder

    spec = _nest(_spec(sample_doc))
    owned, _ = scaffold.owned_operations(spec)
    f = next(f for f in spec.functions if owned[f.function_id])
    ops = owned[f.function_id]
    cls = scaffold.class_name(f.function_id)
    tables = {e.entity_id for e in spec.ddl.entities}
    table = sorted(tables)[0]
    check = lambda code, o=ops: builder.check_server(spec, code, cls, o, tables)   # noqa: E731
    good = _controller(cls, ops, f"    return transaction(async (db) => db.query('SELECT 1 FROM {table} WHERE a = :a', {{ a: 1 }}));")
    assert check(good) == []
    assert any("만들 API가 없음: GET /nope/{id}" in e for e in check(good, [*ops, {"method": "GET", "path": "/nope/{id}"}]))
    assert any("만들 API가 아님" in e for e in check(good, []))
    assert any("권한 검사가 없음" in e for e in check(good.replace("    user.require('담당자');\n", "", 1)))
    assert any("export class" in e for e in check(good.replace(f"{cls}Controller", "Other")))
    assert any("쓸 수 없는 import 'fs'" in e for e in check(_controller(cls, ops, imports="import { readFileSync } from 'fs';\n")))
    assert any("스키마에 없는 테이블 'nope'" in e for e in check(_controller(cls, ops, "    return transaction(async (db) => db.query(`SELECT * FROM nope`));")))
    for bad in ("process.env.X", "eval('1')", "require('child_process')", "new Function('x')", "Date.parse(data.at)"):
        assert check(_controller(cls, ops, f"    return {bad};")), bad
    assert check(_controller(cls, ops, "    // process.env는 쓰지 않는다\n    return 'eval(1) fs';")) == [], "주석·문자열 안은 괜찮다"


def test_verify_typechecks_feature_files_without_npm_reports_skipped(sample_doc, tmp_path, monkeypatch):
    """npm·tsc가 없으면 하지 못한 검사로 둔다 (성공으로 보이지 않는다). tsc 출력의 기능 파일 오류는 그 기능으로 나눈다."""
    import subprocess

    from app.agents.roles import builder_nestjs

    spec = _nest(_spec(sample_doc))
    owned, _ = scaffold.owned_operations(spec)
    a = next(f.function_id for f in spec.functions if owned[f.function_id])
    cls = scaffold.class_name(a)
    records = {a: {"status": "채움", "page": "export default function X() { return null }\n",
                   "controller": f"import {{ Controller }} from '@nestjs/common';\n\n@Controller('api/v1')\nexport class {cls}Controller {{}}\n"}}
    files, _ = scaffold.render_app(spec, built=records)
    scaffold.write_app(files, tmp_path)
    monkeypatch.setattr(builder_nestjs.shutil, "which", lambda _: None)
    errors, skipped = builder_nestjs.verify(tmp_path, {ne.module_name(a): a})
    assert errors == {} and skipped and "서버 타입 검사" in skipped[0]
    tsc = tmp_path / "backend/node_modules/.bin/tsc"
    tsc.parent.mkdir(parents=True)
    tsc.write_text("")
    out = f"src/feature/{ne.module_name(a)}.ts(3,7): error TS2304: Cannot find name 'x'.\n".encode()
    monkeypatch.setattr(builder_nestjs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 2, out, b""))
    errors, skipped = builder_nestjs.verify(tmp_path, {ne.module_name(a): a})
    assert errors == {a: ["타입 오류 3행: TS2304: Cannot find name 'x'."]} and skipped == []
