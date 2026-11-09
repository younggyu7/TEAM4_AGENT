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

from app.services import develop, repo, work
from conftest import signed_in_client
from test_work import FINAL, P, _developed, _team

EXTENSION = Path(__file__).resolve().parents[3] / "TEAM4_AGENT_EXTENSION"


@pytest.mark.skipif(not (EXTENSION / "tests" / "e2e.js").is_file() or shutil.which("node") is None,
                    reason="확장 저장소(TEAM4_AGENT_EXTENSION)나 node가 없음")
@pytest.mark.parametrize("base", ["dev", "candidate-v2"])
def test_extension_downloads_edits_and_submits_over_http(sample_doc, base):
    import uvicorn

    from app.main import app

    _developed(sample_doc)
    _, dev, dev2, ids = _team()
    key = next(i["key"] for i in dev.get("/api/v1/develop/work", params={"project": P}).json()["items"]
               if i["kind"] == "server" and any(f.endswith((".java", ".py", ".ts")) for f in i["files"]))
    app_dir = develop.app_dir(P)
    if base != "dev":   # 다시 개발해 새 브랜치가 생긴 상태
        gen = repo.gen_tree(P)
        (gen / "CANDIDATE.txt").write_text("후보\n", encoding="utf-8")
        assert repo.commit_develop(P, FINAL.removeprefix("v"), "다시 개발", type("M", (), {"id": 999, "name": "한개발", "role": "developer", "email": "d@x"})())["branch"] == base
    dev_head = repo.git(app_dir, "rev-parse", "dev").strip()
    assert dev2.post("/api/v1/develop/work/assign", json={"project": P, "key": key, "user_id": ids["dev"], "base": base}).status_code == 200
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
    assert out["base"] == base and (out["has_candidate"] == (base != "dev")), "확장이 그 브랜치의 코드를 받고 파생 브랜치를 안다"
    assert [c["path"] for c in out["changed"]] == [out["target"]] and out["left"] == [] and out["task"] is True
    wt = develop.app_dir(P) / ".preview" / "worktrees" / work.branch_of(key).replace("/", "__")
    assert (wt / out["target"]).read_text(encoding="utf-8").endswith("// 확장에서 고침\n"), "작업 브랜치에 커밋됨"
    review = signed_in_client("developer", "dev2").get("/api/v1/develop/work/review", params={"project": P, "key": key}).json()
    assert [f["path"] for f in review["files"]] == [out["target"]]
    done = signed_in_client("developer", "dev2").post("/api/v1/develop/work/approve", json={"project": P, "key": key})
    assert done.status_code == 200, done.text
    assert work._merged_files(P, work._item(P, key)) == [out["target"]], "완료 작업은 합칠 때 바뀐 파일만"
    assert repo.git(repo.worktree(P, base), "show", f"HEAD:{out['target']}").endswith("// 확장에서 고침\n"), "파생 브랜치에 합쳐진다"
    if base != "dev":
        assert repo.git(app_dir, "rev-parse", "dev").strip() == dev_head, "dev는 그대로"
