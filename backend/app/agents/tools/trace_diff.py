"""버전 간 변경 추적 (추적 검토 Agent의 도구, LLM 없음).

이전 버전(versions.json의 parent) 대비 새·바뀐·지운 요구사항·기능·엔티티와 그 영향을 정리한다.
  - 바뀐 요구사항 → 지금 연결된 기능 → 그 기능의 엔티티
  - 바뀐·지운 기능 → 연결된 엔티티 (이 버전에 데이터 모델이 없으면 이전 모델 기준: 데이터 설계 때 다시 설계할 엔티티)
  - 지운 기능 → 같은 요구사항을 맡은 새 기능 (전체 다시 설계로 이름·ID가 바뀐 기능을 찾기 쉽게)
데이터 모델은 parent를 따라 올라가며 data_model.json이 있는 가장 가까운 버전의 것을 쓴다
(기능 명세 단계 버전에는 데이터 모델이 없다).
"""
from __future__ import annotations

import json
from pathlib import Path

from app.schemas.design import DataModel, FunctionSpec, Requirement
from app.agents.tools.versions import load_versions

REQ_FIELDS = {"type": "유형", "scope": "범위", "category": "분류", "title": "제목", "description": "설명",
              "priority": "우선순위", "acceptance_criteria": "수용 기준", "status": "확정 상태", "decisions": "결정"}
FUNCTION_FIELDS = {"name": "이름", "description": "설명", "requirement_ids": "요구사항", "nfr_ids": "비기능",
                   "actors": "행위자", "preconditions": "사전 조건", "inputs": "입력", "outputs": "출력",
                   "business_rules": "업무 규칙", "normal_flow": "정상 흐름", "exception_flows": "예외 흐름",
                   "data_objects": "데이터"}
ENTITY_FIELDS = {"label": "이름", "description": "설명", "kind": "종류", "function_ids": "연결 기능",
                 "unique_together": "복합 유일", "exactly_one_of": "둘 중 하나", "required_when": "조건부 필수",
                 "checks": "값 조건"}


def load_previous(project_dir: Path, parent: str | None) -> dict | None:
    """이전 버전의 요구사항·기능 명세와 가장 가까운 데이터 모델. 이전 버전이 없거나 폴더가 없으면 None.

    {"version", "requirements", "functions", "model", "model_version"} (모델이 없으면 model None)
    """
    if not parent or not (project_dir / f"v{parent}" / "design.json").exists():
        return None
    design = json.loads((project_dir / f"v{parent}" / "design.json").read_text(encoding="utf-8"))
    parents = {v["design_version"]: v.get("parent") for v in load_versions(project_dir.parent, project_dir.name)}
    label, seen = parent, set()
    while label and label not in seen:
        seen.add(label)
        path = project_dir / f"v{label}" / "data_model.json"
        if path.exists():
            model = DataModel.model_validate_json(path.read_text(encoding="utf-8"))
            break
        label = parents.get(label)
    else:
        model, label = None, None
    return {"version": parent, "requirements": [Requirement.model_validate(r) for r in design.get("requirements") or []],
            "functions": [FunctionSpec.model_validate(f) for f in design.get("functions") or []],
            "model": model, "model_version": label}


def compare_versions(current: dict, previous: dict, own_model: bool) -> dict:
    """load_previous로 읽은 두 버전을 비교한다 (diff 명령·요구사항 단계 버전).

    비교할 버전이 요구사항 단계 버전(기능 명세 없음)이면 요구사항만 비교하고, 영향은 기준 버전의 기능·엔티티로 보여 준다
    (다음 기능 명세 단계에서 다시 설계될 곳). own_model: 비교할 버전 폴더에 자기 데이터 모델이 있는지.
    """
    requirements_only = not current["functions"]
    functions = previous["functions"] if requirements_only else current["functions"]
    changes = version_changes(current["requirements"], functions, current["model"] if own_model else None, previous)
    if requirements_only:
        changes["data_design"] = None
        changes["requirements_only"] = True
    return changes


def _split(old: dict, new: dict) -> tuple[list[str], list[str], list[str]]:
    """(새, 바뀜, 지움) 키. 순서는 새 쪽(지움은 옛 쪽) 순서를 따른다."""
    return ([k for k in new if k not in old], [k for k in new if k in old and new[k] != old[k]],
            [k for k in old if k not in new])


def _fields(old: dict, new: dict, labels: dict[str, str]) -> list[str]:
    """바뀐 항목 이름."""
    return [label for key, label in labels.items() if old.get(key) != new.get(key)]


TEXT_ONLY = {"label", "description"}   # 한글명·설명만 바뀐 컬럼은 구조 변화와 따로 센다


def _columns(old, new) -> list[str]:
    """엔티티의 컬럼 변화: +새 컬럼, -지운 컬럼, ~구조(타입·길이·필수·키·참조·허용값)가 바뀐 컬럼, 설명만 바뀐 컬럼 수."""
    a = {c.name: c.model_dump() for c in old.attributes}
    b = {c.name: c.model_dump() for c in new.attributes}
    both = [n for n in b if n in a and b[n] != a[n]]
    structural = [n for n in both if {k for k in b[n] if b[n][k] != a[n].get(k)} - TEXT_ONLY]
    text = len(both) - len(structural)
    return ([f"+{n}" for n in b if n not in a] + [f"-{n}" for n in a if n not in b] + [f"~{n}" for n in structural]
            + ([f"설명만 {text}개"] if text else []))


def version_changes(requirements: list[Requirement], functions: list[FunctionSpec], model: DataModel | None,
                    previous: dict) -> dict:
    """이전 버전 대비 변경과 영향 (추적표·design.json에 쓴다)."""
    old_reqs = {r.requirement_id: r.model_dump(include=set(REQ_FIELDS)) for r in previous["requirements"]}
    new_reqs = {r.requirement_id: r.model_dump(include=set(REQ_FIELDS)) for r in requirements}
    old_fns = {f.function_id: f for f in previous["functions"]}
    new_fns = {f.function_id: f for f in functions}
    old_model, stage_model = previous["model"], model or previous["model"]
    ents_of: dict[str, list[str]] = {}
    for e in (stage_model.entities if stage_model else []):
        for fid in e.function_ids:
            ents_of.setdefault(fid, []).append(e.entity_id)
    old_ents_of: dict[str, list[str]] = {}
    for e in (old_model.entities if old_model else []):
        for fid in e.function_ids:
            old_ents_of.setdefault(fid, []).append(e.entity_id)

    req_added, req_changed, req_removed = _split(old_reqs, new_reqs)
    fn_by_req: dict[str, list[str]] = {}
    for f in functions:
        for rid in f.requirement_ids:
            fn_by_req.setdefault(rid, []).append(f.function_id)
    requirement_rows = [{"id": rid, "change": change, "title": (new_reqs.get(rid) or old_reqs[rid])["title"],
                         "fields": _fields(old_reqs[rid], new_reqs[rid], REQ_FIELDS) if change == "바뀜" else [],
                         "functions": fn_by_req.get(rid, []),
                         "entities": sorted({e for fid in fn_by_req.get(rid, []) for e in ents_of.get(fid, [])})}
                        for change, ids in (("새", req_added), ("바뀜", req_changed), ("지움", req_removed)) for rid in ids]

    dumped_old = {k: f.model_dump() for k, f in old_fns.items()}
    dumped_new = {k: f.model_dump() for k, f in new_fns.items()}
    fn_added, fn_changed, fn_removed = _split(dumped_old, dumped_new)
    function_rows = []
    for change, ids in (("새", fn_added), ("바뀜", fn_changed), ("지움", fn_removed)):
        for fid in ids:
            f = new_fns.get(fid) or old_fns[fid]
            # 지운 기능: 같은 요구사항을 맡은 새 기능 (전체 다시 설계로 ID가 바뀐 기능을 찾기 쉽게)
            replaced = [x for x in fn_added if set(new_fns[x].requirement_ids) & set(f.requirement_ids)] \
                if change == "지움" else []
            function_rows.append({
                "id": fid, "change": change, "name": f.name, "requirement_ids": f.requirement_ids,
                "fields": _fields(dumped_old[fid], dumped_new[fid], FUNCTION_FIELDS) if change == "바뀜" else [],
                "entities": (old_ents_of if change == "지움" else ents_of).get(fid, []), "replaced_by": replaced})

    entity_rows = []
    if model is not None and old_model is not None:
        a = {e.entity_id: e for e in old_model.entities}
        b = {e.entity_id: e for e in model.entities}
        e_added, e_changed, e_removed = _split({k: v.model_dump() for k, v in a.items()},
                                               {k: v.model_dump() for k, v in b.items()})
        for change, ids in (("새", e_added), ("바뀜", e_changed), ("지움", e_removed)):
            for eid in ids:
                e = b.get(eid) or a[eid]
                entity_rows.append({"id": eid, "change": change, "label": e.label,
                                    "columns": _columns(a[eid], b[eid]) if change == "바뀜" else [],
                                    "fields": _fields(a[eid].model_dump(), b[eid].model_dump(), ENTITY_FIELDS)
                                    if change == "바뀜" else []})
    # 이 버전에 데이터 모델이 없으면(기능 명세 단계 버전) 데이터 설계 때 다시 설계할 엔티티를 미리 보여 준다
    pending = None
    if model is None and old_model is not None:
        pending = {"model_version": previous["model_version"],
                   "redesign": sorted({e for fid in fn_changed for e in old_ents_of.get(fid, [])}),
                   "cover": fn_added, "unlinked": sorted({e for fid in fn_removed for e in old_ents_of.get(fid, [])})}
    return {"from": previous["version"], "model_from": previous["model_version"],
            "counts": {"requirements": [len(req_added), len(req_changed), len(req_removed)],
                       "functions": [len(fn_added), len(fn_changed), len(fn_removed)],
                       "entities": [sum(r["change"] == c for r in entity_rows) for c in ("새", "바뀜", "지움")]
                       if entity_rows or (model is not None and old_model is not None) else None},
            "requirements": requirement_rows, "functions": function_rows, "entities": entity_rows, "data_design": pending}
