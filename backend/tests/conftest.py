import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SAMPLE_TEXT = """IT 서비스 요청 관리 시스템 요구사항

1. 사용자는 IT 서비스 요청을 등록할 수 있어야 한다. 요청에는 제목과 내용이 포함된다.

2. 서비스데스크 담당자는 접수된 요청에 처리 담당자를 배정한다.

3. 모든 화면은 사용자 요청 후 3초 이내에 응답해야 한다.
"""


@pytest.fixture
def sample_doc(tmp_path) -> Path:
    path = tmp_path / "sample.txt"
    path.write_text(SAMPLE_TEXT, encoding="utf-8")
    return path


def _sample(name: str) -> Path:
    """원본 RFP 샘플 위치: data/ 또는 samples/ (data/는 정제본 입력용으로 바뀔 수 있어 원본은 samples/에 둬도 된다)."""
    for folder in ("data", "samples"):
        if (ROOT / folder / name).exists():
            return ROOT / folder / name
    return ROOT / "samples" / name


@pytest.fixture
def rfp_path() -> Path:
    return _sample("붙임 2. 제안요청서(ITSM 구축).hwpx")


@pytest.fixture
def kobaco_path() -> Path:
    return _sample("2. 제안요청서_KOBACO 정보시스템 통합유지운영.hwpx")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from app.core import config
    from app.agents.llm import client as llm
    from app.agents.storage import set_store

    # 테스트는 가짜 LLM을 쓰지만 공급자 선택이 키 존재를 확인하므로, .env 키가 없는 환경(CI)에서도 돌게 한다
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # auto_assume·ask_choices_only=False: 기존 테스트는 확인 질문 흐름('(가정)' 한도 방식, 영역 묶음 질문, 분류·설계 질문에
    # 답하기)을 본다. 운영 기본값(둘 다 켬)은 tests/test_auto_assume.py·test_ask_choices.py가 켜고 본다
    config._settings = config.Settings(output_dir=tmp_path / "output", validation_db_url="", auto_assume=False,
                                       ask_choices_only=False)
    yield
    config._settings = None
    llm.set_llm(None)
    set_store(None)   # 저장소를 바꿔 끼운 테스트 뒤에는 설정의 로컬 폴더로 되돌린다


@pytest.fixture(autouse=True)
def every_member_sees_every_project(request, monkeypatch):
    """담당자 규칙(services/project_access: 고른 담당자만 그 프로젝트를 봄)은 @pytest.mark.assignees 테스트가 본다.
    다른 테스트는 저장소에 바로 만든 프로젝트를 여러 역할로 쓰므로 모든 회원이 모든 프로젝트를 보게 둔다."""
    if request.node.get_closest_marker("assignees") is None:
        from app.services import project_access
        monkeypatch.setattr(project_access, "can_see", lambda member, project: True)
        monkeypatch.setattr(project_access, "visible", lambda member, projects: list(projects))


def signed_in_client(role: str = "pm", name: str = ""):
    """로그인한 화면 요청 (처음 가입자는 관리자·PM, 다른 회원은 관리자가 승인한 회원). API는 로그인한 회원만 쓴다.
    role은 pm·developer. 같은 역할의 다른 회원이 필요하면 name을 준다 (이메일 <name>@test.local, 이름도 name).
    name 없이 pm이면 처음 가입자(관리자), developer면 developer@test.local."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import users

    if not users.list_members():
        users.signup("admin@test.local", "관리자", "password123")
    email = f"{name}@test.local" if name else "admin@test.local" if role == "pm" else f"{role}@test.local"
    if not any(m.email == email for m in users.list_members()):
        member = users.signup(email, name or role, "password123")
        users.update_member(member.id, role=role, status="active")
    client = TestClient(app)
    assert client.post("/api/v1/auth/login", json={"email": email, "password": "password123"}).status_code == 200
    return client


def docx_upload(text: str = "사용자는 IT 서비스 요청을 등록할 수 있어야 한다. 요청에는 제목과 내용이 포함된다.", name: str = "a.docx"):
    """웹 업로드(PDF·DOCX·HWPX만)에 쓸 작은 DOCX 파일 (TestClient files= 값)"""
    import io

    from docx import Document
    doc, buf = Document(), io.BytesIO()
    if text:
        doc.add_paragraph(text)
    doc.save(buf)
    return {"file": (name, buf.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
