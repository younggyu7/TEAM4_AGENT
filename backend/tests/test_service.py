"""서비스로 묶기: 회사 이름, 시연 계정·로그인, 한 번에 실행(Docker), 구현 범위 보고 (LLM 없음, API 불필요)."""
import json

from test_build import _BuildLLM
from test_develop import FINAL, _app, _start

from app import cli
from app.agents.llm import client as llm_module
from app.core.config import get_settings
from app.agents.tools import scaffold
from app.agents.tools.sample_data import ROWS
from app.agents.tools.versions import load_versions, project_info, save_version
from app.agents.tools.scaffold import class_name

TEMPLATES = scaffold.TEMPLATES


def _design(app):
    return json.loads((app / "frontend/src/generated/screens.json").read_text(encoding="utf-8"))


def test_company_name_is_kept_with_the_project_and_shown_in_the_service(sample_doc):
    out = _start(sample_doc)
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0
    app = _app(out)
    assert _design(app)["company"] == "sample", "회사 이름을 따로 정하지 않으면 업로드할 때 넣은 프로젝트 이름"
    assert cli.main(["develop", "sample", "--from", FINAL, "--company", "누리솔루션"]) == 0
    app = _app(out)
    assert _design(app)["company"] == "누리솔루션", "로그인 화면·메뉴의 회사 이름"
    assert (app / "README.md").read_text(encoding="utf-8").startswith("# 누리솔루션 업무 포털")
    assert project_info("sample")["company"] == "누리솔루션", "프로젝트에 저장해 다음 develop도 같은 이름"
    versions = load_versions("sample")
    save_version("sample", {**versions[-1], "summary": ["다시 저장"]})
    assert project_info("sample")["company"] == "누리솔루션", "버전 기록을 고쳐도 회사 이름은 남는다"
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0
    assert _design(app)["company"] == "누리솔루션", "주지 않으면 저장한 이름"
    assert cli.main(["develop", "sample", "--from", FINAL, "--company", ""]) == 0
    assert _design(app)["company"] == "sample" and "company" not in project_info("sample"), "빈 값이면 지우고 프로젝트 이름으로"


def test_demo_accounts_are_admin_and_one_general_user_and_login_does_not_trust_headers(sample_doc):
    out = _start(sample_doc)
    assert cli.main(["develop", "sample", "--from", FINAL]) == 0
    app = _app(out)
    design = _design(app)
    accounts = json.loads((app / "backend/src/main/resources/accounts.json").read_text(encoding="utf-8"))
    assert accounts == design["accounts"], "서버 계정과 로그인 화면의 계정 목록은 같은 값"
    general = next(r["name"] for r in design["roles"] if "관리자" not in r["name"])
    assert [(a["loginId"], a["role"]) for a in accounts] == [("admin", "전체 보기"), ("user", general)], "임시 관리자 + 첫 일반 역할 계정"
    assert len({a["loginId"] for a in accounts}) == len(accounts)
    assert all(1 <= int(a["userId"]) <= ROWS for a in accounts), "예시 데이터에 있는 사용자 ID"
    assert all("password" not in a for a in accounts), "비밀번호는 실행할 때 만든다 (파일에 없다)"
    current = (TEMPLATES / "backend/src/main/java/app/common/CurrentUser.java").read_text(encoding="utf-8")
    assert "X-User-Role" not in current and "ATTRIBUTE" in current, "역할은 헤더가 아니라 로그인 세션에서"
    for front in ("frontend/src/api.ts", "frontend/src/kit.tsx"):
        assert "X-User" not in (TEMPLATES / front).read_text(encoding="utf-8")
    accounts_java = (TEMPLATES / "backend/src/main/java/app/auth/Accounts.java").read_text(encoding="utf-8")
    assert "app_auth" in accounts_java and "accounts.txt" in accounts_java, "업무 테이블과 섞지 않고, 비밀번호는 파일에만"


def test_one_command_run_files_and_implementation_report(sample_doc):
    out = _start(sample_doc)
    spec = scaffold.load_spec("sample", FINAL)
    fid = spec.functions[0].function_id
    llm_module.set_llm(_BuildLLM())
    assert cli.main(["develop", "sample", "--from", FINAL, "--build", "--only", fid]) == 0
    app = _app(out)
    compose = (app / "docker-compose.yml").read_text(encoding="utf-8")
    assert "build: ." in compose and "service_healthy" in compose and "${APP_HOST:-127.0.0.1}:${APP_PORT:-8080}:8080" in compose and "./local:/app/local" in compose
    docker = (app / "Dockerfile").read_text(encoding="utf-8")
    assert "VITE_API=server" in docker and "src/main/resources/static" in docker, "화면을 서버 이미지에 함께 담는다"
    assert "node_modules" in (app / ".dockerignore").read_text(encoding="utf-8") and "local/" in (app / ".gitignore").read_text(encoding="utf-8")
    readme = (app / "README.md").read_text(encoding="utf-8")
    assert "docker compose up --build" in readme and "local/accounts.txt" in readme and "| admin |" in readme and "| user |" in readme
    report = (app / "IMPLEMENTATION.md").read_text(encoding="utf-8")
    assert f"| {fid} |" in report and "LLM 구현" in report, "기능마다 누가 만들었나"
    assert "FQ-001" in report and "고객 답을 기다리는 규칙" in report, "확인 필요 규칙은 질문 ID와 함께"
    others = [f.function_id for f in spec.functions if f.function_id != fid]
    assert all(f"| {o} |" in report for o in others)
    assert (app / f"frontend/src/pages/{class_name(fid)}.tsx").exists()


def test_frontend_shell_has_login_and_brand():
    app = (TEMPLATES / "frontend/src/App.tsx").read_text(encoding="utf-8")
    login = (TEMPLATES / "frontend/src/Login.tsx").read_text(encoding="utf-8")
    assert "design.company || design.title" in app, "회사 이름이 없으면 사업 이름"
    assert "<Login" in app and "session-expired" in app, "로그인하지 않으면 로그인 화면, 세션이 끝나면 다시 로그인"
    assert "SERVER ?" in login and "design.accounts" in login, "예시 데이터 모드는 역할만 고른다"
