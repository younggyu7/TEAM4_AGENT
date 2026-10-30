"""로컬 폴더 어댑터: output/<프로젝트>/versions.json, output/<프로젝트>/v<번호>/<산출물>.

프로젝트 ID가 'team/sample'처럼 경로면 그 아래 폴더다. output 밖을 가리키는 ID·이름('..', 절대 경로)은 막는다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from app.core.guards import inside

VERSION_DIR = re.compile(r"^v(\d+(?:\.\d+)*)$")
MAX_DEPTH = 3                                   # 프로젝트를 찾을 때 내려갈 폴더 깊이 (output/묶음/프로젝트)
SKIP_DIRS = ("llm_exchange",)                   # 프로젝트가 아닌 작업 폴더
INDEX = "versions.json"                         # 버전 기록
TITLES = "titles.json"                          # 화면에서 붙인 버전 제목


class LocalArtifactStore:
    """설계 산출물을 로컬 폴더에 둔다."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.location = str(self.root)

    # ---------------------------------------------------------------- 경로
    def _project(self, project: str) -> Path:
        folder = inside(self.root, project) if project else None
        if folder is None:
            raise ValueError(f"프로젝트 이름이 올바르지 않습니다: {project!r}")
        return folder

    def _file(self, project: str, *parts: str) -> Path:
        path = inside(self._project(project), *parts)
        if path is None:
            raise ValueError(f"산출물 이름이 올바르지 않습니다: {'/'.join(parts)!r}")
        return path

    @staticmethod
    def _read(path: Path) -> str | None:
        return path.read_text(encoding="utf-8") if path.is_file() else None

    @staticmethod
    def _write(path: Path, text: str) -> str:
        """받은 내용을 그대로 쓴다 (줄바꿈을 OS에 맞게 바꾸지 않는다. CSV의 \\r\\n이 Windows에서 \\r\\r\\n이 되지 않게)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return str(path)

    # ---------------------------------------------------------------- 프로젝트
    def list_projects(self) -> list[str]:
        projects: list[str] = []

        def walk(folder: Path, depth: int) -> None:
            if any(VERSION_DIR.match(d.name) and (d / "design.json").exists() for d in folder.iterdir() if d.is_dir()):
                projects.append(folder.relative_to(self.root).as_posix())
                return
            if depth >= MAX_DEPTH:
                return
            for d in sorted(folder.iterdir()):
                if d.is_dir() and d.name not in SKIP_DIRS and not d.name.startswith("parse-"):
                    walk(d, depth + 1)

        if self.root.is_dir():
            walk(self.root, 0)
        return projects

    def has_project(self, project: str) -> bool:
        try:
            return self._project(project).is_dir()
        except ValueError:
            return False

    def read_project_file(self, project: str, name: str) -> str | None:
        return self._read(self._file(project, name))

    def write_project_file(self, project: str, name: str, text: str) -> str:
        return self._write(self._file(project, name), text)

    # ---------------------------------------------------------------- 버전 기록·제목 (versions.json·titles.json)
    def load_version_records(self, project: str) -> list[dict]:
        text = self.read_project_file(project, INDEX)
        return json.loads(text).get("versions", []) if text else []

    def save_version_records(self, project: str, records: list[dict]) -> None:
        self.write_project_file(project, INDEX, json.dumps({"project": PurePosixPath(project).name, "versions": records},
                                                          ensure_ascii=False, indent=2))

    def load_titles(self, project: str) -> dict[str, str]:
        try:
            return json.loads(self.read_project_file(project, TITLES) or "{}") or {}
        except ValueError:
            return {}

    def save_titles(self, project: str, titles: dict[str, str]) -> None:
        self.write_project_file(project, TITLES, json.dumps(titles, ensure_ascii=False, indent=2) + "\n")

    # ---------------------------------------------------------------- 버전 공간
    def claim_version(self, project: str, base: str | None = None) -> str:
        folder = self._project(project)
        folder.mkdir(parents=True, exist_ok=True)
        prefix = f"{base}." if base else ""
        pattern = re.compile(rf"v{re.escape(prefix)}(\d+)")
        taken = [int(m.group(1)) for p in folder.iterdir() if p.is_dir() and (m := pattern.fullmatch(p.name))]
        number = max(taken, default=0) + 1
        while True:   # 폴더를 먼저 만들어 번호를 차지한다 (이미 있으면 다음 번호)
            label = f"{prefix}{number}"
            try:
                (folder / f"v{label}").mkdir()
                return label
            except FileExistsError:
                number += 1

    def version_labels(self, project: str) -> set[str]:
        try:
            folder = self._project(project)
        except ValueError:
            return set()
        if not folder.is_dir():
            return set()
        return {m[1] for d in folder.iterdir() if d.is_dir() and (m := VERSION_DIR.match(d.name))}

    def version_location(self, project: str, version: str) -> str:
        return str(self._file(project, f"v{version}"))

    # ---------------------------------------------------------------- 산출물 파일
    def read(self, project: str, version: str, name: str) -> str | None:
        return self._read(self._file(project, f"v{version}", name))

    def write(self, project: str, version: str, name: str, text: str) -> str:
        return self._write(self._file(project, f"v{version}", name), text)

    def exists(self, project: str, version: str, name: str) -> bool:
        try:
            return self._file(project, f"v{version}", name).is_file()
        except ValueError:
            return False
