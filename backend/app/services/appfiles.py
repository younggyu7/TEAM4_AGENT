"""최종 개발물 코드 보기·고치기 (최종 개발물 1단계의 파일 탐색기).

- 서버 코드 폴더(OUTPUT_DIR/<프로젝트>/app = dev)나 브랜치 작업 폴더(services/repo.py)의 파일 목록과 내용을 보여 준다.
  고쳐 저장하기는 브랜치에서만 (개발자, repo_write). dev는 합치기로, gen은 최종 개발하기로, main은 릴리스로만 바뀐다.
- 커밋하지 않은 파일은 changed('M'·'A'·'D')로 표시한다 (그 작업 폴더의 git status).
- 업무 로직 자리(// <hole …>)가 코드가 넣은 기본값('return crud.get();' · '// 구현 전') 그대로면 '빈 로직'으로 센다.
- 생성기 기록(gen 작업 폴더의 .req-agent/manifest.json = 마지막 생성 결과)의 해시와 다르면 '사람이 고침' (또는 아직 합치지 않은 생성 결과).
- 빌드 결과·패키지·미리보기·비밀번호 폴더는 목록에 넣지 않고 열거나 고칠 수 없다. 새 파일 만들기·지우기는 하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.core.exceptions import BadRequest, NotFound
from app.services import repo
from app.services.develop import PREVIEW, app_dir, gen_dir

HIDDEN = {"node_modules", "dist", "target", PREVIEW, ".req-agent", ".git", "local"}
MAX_BYTES = 1_000_000
HOLE = re.compile(r"(?://|#) <hole (\w+)>\n(.*?)\n[ \t]*(?://|#) </hole>", re.S)   # Java·TypeScript(//)·Python(#) 틀
EMPTY_BODIES = {"return crud.get();", "// 구현 전", "return crud()", "# 구현 전", "return crud();"}


def _manifest(app: Path) -> dict[str, str]:
    path = app / ".req-agent" / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")).get("files", {}) if path.is_file() else {}


def _norm(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip()


def _ai_work(app: Path) -> tuple[set[str], set[str]]:
    """AI가 쓴 것: (빈 로직만 AI가 채운 자리 본문들, 기능 전체 AI가 쓴 파일 경로들). 생성기 기록(.req-agent)에서 읽는다"""
    bodies, files = set(), set()
    for p in (app / ".req-agent" / "logic").glob("*.json"):
        for body in (json.loads(p.read_text(encoding="utf-8")).get("hooks") or {}).values():
            bodies.add(_norm(body))
    for p in (app / ".req-agent" / "scratch").glob("*.json"):   # 틀 없는 층: AI가 뼈대부터 쓴 파일
        files |= {f["path"] for f in json.loads(p.read_text(encoding="utf-8")).get("files") or []}
    return bodies, files


def _ai_file(rel: str, ai_files: set[str]) -> bool:
    """기능 전체 AI(--build)가 쓴 파일: 기능 구현 폴더(feature/)와 기능 화면(pages/, 목록 index 빼고), 또는 AI 뼈대 파일"""
    parts = rel.split("/")
    return rel in ai_files or "feature" in parts[:-1] or ("pages" in parts[:-1] and not parts[-1].startswith("index."))


def holes(text: str, ai_bodies: set[str] | None = None) -> list[dict]:
    """업무 로직 자리 [{name, line, empty, ai}] (line은 1부터). ai: 빈 로직만 AI가 채운 본문 그대로 (사람이 고치면 아님)"""
    out = []
    for m in HOLE.finditer(text):
        body = m.group(2).strip()
        empty = body in EMPTY_BODIES
        out.append({"name": m.group(1), "line": text.count("\n", 0, m.start()) + 1, "empty": empty,
                    "ai": not empty and bool(ai_bodies) and _norm(body) in ai_bodies})
    return out


def _info(app: Path, path: Path, manifest: dict[str, str], ai: tuple[set[str], set[str]], text: str | None = None,
          changed: dict[str, str] | None = None) -> dict:
    """app = 보는 작업 폴더(main 또는 브랜치), manifest·ai = main의 생성기 기록"""
    rel = path.relative_to(app).as_posix()
    ai_bodies, ai_files = ai
    info = {"path": rel, "size": path.stat().st_size, "generated": rel in manifest, "edited": False, "holes": 0, "empty_holes": 0,
            "ai_holes": 0, "ai_file": _ai_file(rel, ai_files), "changed": (changed or {}).get(rel)}
    if rel in manifest or path.suffix in (".java", ".py", ".ts"):
        try:
            text = text if text is not None else path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return info
        if rel in manifest:
            info["edited"] = hashlib.sha256(text.encode("utf-8")).hexdigest() != manifest[rel]
        found = holes(text, ai_bodies)
        info["holes"], info["empty_holes"], info["ai_holes"] = len(found), sum(h["empty"] for h in found), sum(h["ai"] for h in found)
    return info


def _target(project: str, rel: str, branch: str | None = None) -> tuple[Path, Path]:
    app = repo.worktree(project, branch).resolve()
    target = (app / rel).resolve()
    if app not in target.parents or any(part in HIDDEN for part in target.relative_to(app).parts):
        raise NotFound("열 수 없는 파일입니다.")
    if not target.is_file():
        raise NotFound(f"파일이 없습니다: {rel}")
    return app, target


def tree(project: str, branch: str | None = None) -> dict:
    """파일 목록 (경로 순). 빈 로직 합계와 사람이 고친 파일 수, 커밋하지 않은 변경도 같이"""
    if not (app_dir(project) / "README.md").is_file():
        raise BadRequest("아직 개발한 코드가 없습니다. 산출물 보기에서 '최종 개발하기'를 먼저 하세요.")
    app = repo.worktree(project, branch)
    manifest = _manifest(gen_dir(project))
    ai = _ai_work(gen_dir(project))
    changed = repo.changes(app)
    files = []

    def walk(folder: Path) -> None:
        for child in sorted(folder.iterdir(), key=lambda p: p.name):
            if child.name in HIDDEN or child.name == ".DS_Store":
                continue
            if child.is_dir():
                walk(child)
            elif child.is_file():
                files.append(_info(app, child, manifest, ai, changed=changed))

    walk(app)
    return {"branch": branch or repo.DEV, "changes": len(changed), "deleted": sorted(p for p, c in changed.items() if c == "D"), "files": files, "empty_holes": sum(f["empty_holes"] for f in files), "holes": sum(f["holes"] for f in files),
            "edited": sum(f["edited"] for f in files), "ai_holes": sum(f["ai_holes"] for f in files), "ai_files": sum(f["ai_file"] for f in files)}


def read(project: str, rel: str, branch: str | None = None) -> dict:
    app, target = _target(project, rel, branch)
    if target.stat().st_size > MAX_BYTES:
        raise BadRequest("파일이 너무 커서 화면에서 열지 않습니다 (1MB 넘음).")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise BadRequest("글자 파일이 아니라 화면에서 열 수 없습니다.") from None
    ai = _ai_work(gen_dir(project))
    return {**_info(app, target, _manifest(gen_dir(project)), ai, text, repo.changes(app)), "content": text, "hole_list": holes(text, ai[0])}


def save(project: str, rel: str, content: str, branch: str | None = None) -> dict:
    """브랜치 작업 폴더의 있는 글자 파일만 고친다 (dev·main·gen은 거부). 커밋은 repo.commit('저장하기')"""
    if not branch or branch in repo.PROTECTED:
        raise BadRequest("dev·main·gen은 직접 고칠 수 없습니다 — 브랜치를 만들어 고치세요.")
    app, target = _target(project, rel, branch)
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise BadRequest("1MB가 넘는 내용은 저장하지 않습니다.")
    try:
        target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise BadRequest("글자 파일이 아니라 고칠 수 없습니다.") from None
    target.write_text(content, encoding="utf-8", newline="\n")
    return read(project, rel, branch)
