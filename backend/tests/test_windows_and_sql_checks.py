"""Windows 실행 파일 찾기와 SQL 테이블 검사 (AI 코드 검사 오탐)."""
import shutil

from app.agents.roles.developer import sql_tables
from app.core import exe


def test_sql_tables_ignores_keywords_and_cte_names():
    assert sql_tables("UPDATE budget SET a = 1") == ["budget"]
    assert "SET" not in sql_tables("INSERT INTO budget (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET a = 2"), "DO UPDATE SET의 SET은 테이블이 아니다"
    assert sql_tables("WITH base AS (SELECT * FROM budget) SELECT * FROM base JOIN users u ON 1=1") == ["budget", "users"], "WITH 이름은 테이블이 아니다"


def test_windows_prefers_exe_and_cmd_over_extensionless_scripts(monkeypatch):
    """확장자 없는 셸 스크립트를 고르면 WinError 193: Windows는 .exe·.cmd·.bat을 먼저, .cmd·.bat은 shell로 돌린다"""
    found = {"docker": r"C:\Docker\bin\docker", "docker.exe": r"C:\Docker\bin\docker.exe", "npm.cmd": r"C:\node\npm.cmd"}
    monkeypatch.setattr(exe.os, "name", "nt")
    monkeypatch.setattr(shutil, "which", lambda name: found.get(name))
    assert exe.which("docker").endswith("docker.exe") and exe.which("npm").endswith("npm.cmd")
    assert exe.run_shell(r"C:\node\npm.cmd") and not exe.run_shell(r"C:\Docker\bin\docker.exe")
