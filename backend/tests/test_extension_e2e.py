"""VS Code 확장(별도 저장소 TEAM4_AGENT_EXTENSION)의 흐름을 실제 HTTP 서버로 확인한다 (FR-053·054).

서버를 이 프로세스에서 띄우고(uvicorn), 확장의 core.js로 연결 → 내 작업 → 코드 받기 → 고치기 → 올리기·제출을 node로 돌린다.
확장 저장소가 옆 폴더에 없거나 node가 없으면 건너뛴다."""
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.services import develop, work
from conftest import signed_in_client
from test_work import P, _developed, _team

EXTENSION = Path(__file__).resolve().parents[3] / "TEAM4_AGENT_EXTENSION"


@pytest.mark.skipif(not (EXTENSION / "tests" / "e2e.js").is_file() or shutil.which("node") is None,
                    reason="확장 저장소(TEAM4_AGENT_EXTENSION)나 node가 없음")
def test_extension_downloads_edits_and_submits_over_http(sample_doc):
    import uvicorn

    from app.main import app

    _developed(sample_doc)
    _, dev, dev2, ids = _team()
    key = next(i["key"] for i in dev.get("/api/v1/develop/work", params={"project": P}).json()["items"]
               if i["kind"] == "function" and any(f.endswith((".java", ".py", ".ts")) for f in i["files"]))
    assert dev2.post("/api/v1/develop/work/assign", json={"project": P, "key": key, "user_id": ids["dev"]}).status_code == 200
    token = dev.post("/api/v1/auth/tokens", json={"name": "e2e"}).json()["token"]

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        run = subprocess.run(["node", "tests/e2e.js"], cwd=EXTENSION, capture_output=True, text=True, timeout=120,
                             env={"PATH": shutil.os.environ["PATH"], "SERVER": f"http://127.0.0.1:{port}", "TOKEN": token})
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["key"] == key and out["status"] == "review" and out["statuses"] == ["review"]
    assert [c["path"] for c in out["changed"]] == [out["target"]] and out["left"] == [] and out["task"] is True
    wt = develop.app_dir(P) / ".preview" / "worktrees" / work.branch_of(key).replace("/", "__")
    assert (wt / out["target"]).read_text(encoding="utf-8").endswith("// 확장에서 고침\n"), "작업 브랜치에 커밋됨"
    review = signed_in_client("developer", "dev2").get("/api/v1/develop/work/review", params={"project": P, "key": key}).json()
    assert [f["path"] for f in review["files"]] == [out["target"]]
