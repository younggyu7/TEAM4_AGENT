"""app/core: 요청 값 검사(guards), 비밀 값 가리기(security), 오류 응답 형태(exceptions)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import guards
from app.core.exceptions import BadRequest, NotFound, register_exception_handlers
from app.core.security import mask_url, redact


def test_inside_blocks_paths_outside_root(tmp_path):
    assert guards.inside(tmp_path, "a", "b.txt") == (tmp_path / "a" / "b.txt").resolve()
    for parts in (("..",), ("a", "..", "..", "x"), ("/etc/passwd",), ("",)):
        assert guards.inside(tmp_path, *parts) is None, parts


def test_version_and_names():
    assert guards.version("v2.1") == "2.1" and guards.version("3") == "3"
    for bad in ("", "v", "2.", "2..1", "../1"):
        with pytest.raises(BadRequest):
            guards.version(bad)
    assert guards.project_name(" 고객요구사항4 ") == "고객요구사항4"
    for bad in ("", "a/b", "..\\x", ".hidden"):
        with pytest.raises(BadRequest):
            guards.project_name(bad)
    assert guards.upload_name("../../etc/요구사항.pdf") == "요구사항.pdf"


def test_secrets_are_hidden_in_job_log():
    assert mask_url("postgresql://user:pw@db:5432/x") == "postgresql://***@db:5432/x"
    line = "key=sk-ant-api03-abcdefghijklmnop1234 url=postgresql://a:b@h/x"
    assert redact(line) == "key=sk-*** url=postgresql://***@h/x"


def test_errors_use_detail_shape_and_hide_internals():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/missing")
    def missing():
        raise NotFound("없습니다.")

    @app.get("/boom")
    def boom():
        raise RuntimeError("/secret/path 내부 정보")

    client = TestClient(app, raise_server_exceptions=False)
    assert (r := client.get("/missing")).status_code == 404 and r.json() == {"detail": "없습니다."}
    r = client.get("/boom")
    assert r.status_code == 500 and "secret" not in r.text
