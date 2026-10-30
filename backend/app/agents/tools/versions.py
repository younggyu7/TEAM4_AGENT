"""프로젝트 단위 설계 버전: 번호 규칙과 버전 기록(versions.json). 읽기·쓰기는 산출물 저장소 포트(agents/storage)가 한다.

- 같은 고객 문서(프로젝트)를 다시 실행하면 실행 ID와 관계없이 다음 최상위 번호(v2, v3 …)를 받는다.
- 단계별 버전(팀 규칙): 어떤 버전을 기준으로 한 단계만 다시 만들면 그 아래 번호를 받는다.
  예) v2의 요구사항·기능 명세를 그대로 두고 데이터 설계만 다시 → v2.1, v2.2 …
- 번호는 문자열이다 ("2", "2.1", "2.1.3"). 정렬은 점으로 나눈 숫자 순서.
- 실행 ID(run_id)는 체크포인트·재개(resume)용 내부 식별자로만 쓴다.
- 프로젝트 이름은 run --project로 정하고, 없으면 문서 파일 이름(확장자 제외)을 쓴다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.agents.storage import get_store

INDEX = "versions.json"
_LABEL = re.compile(r"\d+(\.\d+)*")


def project_name(name: str) -> str:
    """폴더 이름으로 쓸 수 있게 프로젝트 이름을 정리한다 (Windows 금지 문자·공백 → _)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "_", (name or "").strip()).strip("._")
    return cleaned[:80] or "project"


def project_of(state: dict) -> str:
    """실행 상태의 프로젝트 이름: run --project 값, 없으면 문서 파일 이름 (예전 실행도 같은 규칙)."""
    options = state.get("options") or {}
    file_name = (state.get("document") or {}).get("file_name") or Path(state.get("source_path") or "").name
    return project_name(options.get("project") or Path(file_name).stem or state.get("run_id", ""))


def version_label(value) -> str:
    """버전 번호를 문자열로 맞춘다 ('v2.1', 2, '2.1' → '2.1'). 형식이 맞지 않으면 ValueError."""
    label = str(value).strip().lstrip("vV")
    if not _LABEL.fullmatch(label):
        raise ValueError(f"버전 번호 형식이 아님: {value} (예: v2, v2.1)")
    return label


def version_key(label) -> tuple[int, ...]:
    """정렬용 키: '2.10' > '2.9'."""
    return tuple(int(x) for x in str(label).split("."))


def load_versions(project: str) -> list[dict]:
    """프로젝트의 버전 기록 (번호 순). 예전 기록의 숫자 번호도 문자열로 맞춘다."""
    text = get_store().read_project_file(project_name(project), INDEX)
    if text is None:
        return []
    versions = json.loads(text).get("versions", [])
    for v in versions:
        v["design_version"] = str(v["design_version"])
        if v.get("parent") is not None:
            v["parent"] = str(v["parent"])
    return sorted(versions, key=lambda v: version_key(v["design_version"]))


def find_version(project: str, label) -> dict | None:
    """버전 번호로 기록을 찾는다."""
    label = version_label(label)
    return next((v for v in load_versions(project) if v["design_version"] == label), None)


def claim_next_version(project: str, base: str | None = None) -> str:
    """다음 버전 번호를 잡는다. base가 있으면 그 아래 번호(base.1, base.2 …), 없으면 다음 최상위 번호.
    번호를 먼저 차지하므로(저장소가 공간을 만든다) 같은 프로젝트를 동시에 실행해도 번호가 겹치지 않는다."""
    return get_store().claim_version(project_name(project), version_label(base) if base else None)


def latest_top_level(versions: list[dict]) -> str | None:
    """최상위 번호(점 없는 번호) 중 가장 큰 것."""
    tops = [v["design_version"] for v in versions if "." not in v["design_version"]]
    return max(tops, key=version_key) if tops else None


def save_version(project: str, record: dict) -> None:
    """버전 기록 하나를 versions.json에 넣는다 (같은 번호가 있으면 바꾼다). output_dir는 저장소가 정한 위치."""
    project, label = project_name(project), str(record["design_version"])
    record = {**record, "design_version": label, "output_dir": get_store().version_location(project, label)}
    versions = [v for v in load_versions(project) if v["design_version"] != label]
    versions = sorted([*versions, record], key=lambda v: version_key(v["design_version"]))
    get_store().write_project_file(project, INDEX, json.dumps({"project": project_name(project), "versions": versions},
                                                              ensure_ascii=False, indent=2))


def update_version(project: str, version, **fields) -> None:
    """버전 기록의 일부 항목을 고친다 (예: DB 검증 결과)."""
    label = str(version)
    for record in load_versions(project):
        if record["design_version"] == label:
            save_version(project, {**record, **fields})
            return
