"""틀 없는 층을 AI가 뼈대부터 만들 때 (LLM 없이): 합치기·빌드 오류 나누기·예상."""
import json

from test_develop import _spec

from app.agents.roles import scaffolder
from app.agents.tools import scaffold, stacks


def test_skeleton_is_merged_with_template_and_errors_go_to_the_failing_file(sample_doc):
    spec = _spec(sample_doc)
    spec.parts = {**spec.parts, "backend": "egov"}   # 틀이 없는 서버
    assert stacks.untemplated(spec.parts)[0]["layer"] == "backend" and not scaffold.has_backend(spec)
    files, _ = scaffold.render_app(spec)
    assert not [p for p in files if p.startswith("backend/")], "틀 없는 서버는 코드 틀이 만들지 않는다"
    fid = spec.functions[0].function_id
    result = {"files": {"backend/package.json": "{}", "Dockerfile": "FROM node:20\n"}, "app_env": {"DB_URL": "postgres://app:app@db:5432/app"},
              "filled": [fid]}
    merged = scaffolder.apply(files, result, spec, ["backend"])
    compose = merged["docker-compose.yml"]
    assert "  db:\n" in compose and 'DB_URL: "postgres://app:app@db:5432/app"' in compose and "${APP_HOST:-127.0.0.1}:${APP_PORT:-8080}:8080" in compose
    accounts = json.loads(merged["backend/accounts.json"])
    assert [a["loginId"] for a in accounts] == ["admin", "user"] and all(isinstance(a["userId"], str) for a in accounts)
    assert merged["Dockerfile"] == "FROM node:20\n" and "frontend/src/generated/screens.json" in merged
    blamed = scaffolder.blame([f"src/features/{scaffold.class_name(fid)}.ts(3,1): error TS2554", "src/app.ts(1,1): error TS1"], result, spec)
    assert set(blamed) == {fid, "backend"}, "기능 파일 오류는 그 기능만, 나머지는 뼈대를 다시 묻는다"
    plan = scaffolder.plan(spec, spec.parts, ["backend"], scaffold.app_dir("sample"))
    assert plan["skeletons"] == 1 and plan["output_tokens"] > 0
