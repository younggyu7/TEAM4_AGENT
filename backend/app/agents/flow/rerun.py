"""단계만 다시 만들기(rerun): 기준 버전의 앞 단계 결과는 그대로 두고 한 단계부터 다시 설계해 아래 번호 버전을 만든다.

팀 규칙: 한 단계에서 v1 … v6을 만들다 v6이 마음에 들면, 다음 단계는 v6.1부터 시작한다.
  - 기준 버전이 전체 실행 결과(v2)면 → v2.1, v2.2 …
  - 같은 단계를 다시 만든 버전(v2.1)에서 또 다시 만들면 → 형제 번호 v2.2 (같은 단계 안의 다음 시도)
새 실행은 원래 실행의 체크포인트(문서 블록·추출 단위 등)와 기준 버전 폴더의 design.json(요구사항·기능 명세·질문)으로
시작 상태를 만들고, 조정 Agent부터 진행한다. 다시 만드는 단계 이후의 산출물과 관련 이슈는 비운다.

데이터 설계는 기준 버전의 데이터 모델에서 출발해 필요한 엔티티만 다시 설계한다 (prepare_data_model).
  - 사용자 수정: --data-model 파일(없으면 기준 버전 폴더의 data_model.json)을 생성 당시 모델과 비교해 반영한다.
    고친·새 엔티티는 고정되고, 지우거나 이름을 바꾼 엔티티의 참조는 코드가 정리한다 (tools/model_edits.py).
  - 범위: --entities는 그 엔티티만, --feedback만 주면 의견과 관련된 엔티티만, --full은 전체 (고정 엔티티는 유지).
    사용자 수정만 있으면 LLM 없이 정리·검증한다. 범위도 수정도 없으면 진행하지 않는다 (실수로 전체 비용을 쓰지 않게).

기능 명세(rerun function_designer)는 기능 명세에서 멈춘다 (팀 규칙: 확정한 뒤 다음 단계).
  - 범위: --requirements는 그 요구사항의 기능 그룹만, --feedback만 주면 의견과 관련된 그룹만(바꿀 기능만 받음), --full은 전체.
  - 그 버전에서 데이터 설계를 하면(rerun data_modeler --from v2.1) 기준 버전(v2)의 데이터 모델을 이어받아
    바뀐 기능에 연결된 엔티티만 다시 설계하고, 새 기능은 연결 보완으로 처리한다.

요구사항(rerun requirements_analyst --feedback)은 다음 최상위 번호(v3 …)를 받고 요구사항에서 멈춘다.
  - 의견이 바꾸는·빼는·새로 만드는 요구사항만 요청 1회로 받는다 (다시 추출하지 않음, 원문 근거·ID 유지).
  - 그 버전에는 기능 명세가 없다. 그 버전에서 기능 명세를 하면 가장 가까운 기능 명세 버전(ancestor)을 이어받아
    바뀐 요구사항(changed_requirements)의 그룹만 다시 설계하고, 데이터 설계도 데이터 모델이 있는 가장 가까운 버전을 이어받는다.

지금 지원하는 단계: requirements_analyst (요구사항 + 의미 검토의 요구사항 부분),
function_designer (기능 명세 + 추적 검토 + 의미 검토의 기능 부분),
data_modeler (데이터 설계 = 데이터 모델 + 추적 검토 + 의미 검토의 모델 부분 + DB 검증).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.schemas.design import DataModel, FunctionSpec, Question, Requirement
from app.agents.flow.state import SEMANTIC_BASIS_INIT, basis, rev
from app.agents.roles.function_designer import apply_answers_by_code
from app.agents.roles.requirement_reviewer import record_answer as record_review_answer, settle
from app.agents.tools.model_edits import ReworkSummary, apply_user_edits
from app.agents.tools.model_validator import to_identifier
from app.agents.tools.trace_diff import REQ_FIELDS
from app.agents.tools.versions import load_versions

STAGES: dict[str, dict] = {
    "requirements_analyst": {
        "label": "요구사항",
        # 요구사항을 다시 만들면 뒤 단계(기능 명세·데이터 설계)와 그 검토가 모두 의미 없어진다
        "drop_issue_prefixes": ("REQ_WITHOUT_FUNCTION", "TRACE_", "SEM_FUNC_", "SEM_MODEL_", "MODEL_", "DB_", "DATA_"),
    },
    "function_designer": {
        "label": "기능 명세",
        # 기능 명세를 다시 하면 의미가 없어지는 이슈 (추적, 의미 검토의 기능·모델 부분, 이 버전에 없는 데이터 설계)
        "drop_issue_prefixes": ("REQ_WITHOUT_FUNCTION", "TRACE_", "SEM_FUNC_", "SEM_MODEL_", "MODEL_", "DB_", "DATA_"),
    },
    "data_modeler": {
        "label": "데이터 설계",
        # 데이터 설계를 다시 하면 의미가 없어지는 이슈 (모델·DB 검증, 추적, 의미 검토의 모델 부분, 속성 설계 실패)
        "drop_issue_prefixes": ("REQ_WITHOUT_FUNCTION", "MODEL_", "DB_", "TRACE_", "SEM_MODEL_", "DATA_"),
    },
}


class RerunError(ValueError):
    """다시 만들기를 진행할 수 없는 이유 (사용자에게 그대로 보여 준다)."""


def base_for(record: dict, stage: str) -> str:
    """새 번호의 기준: 같은 단계를 다시 만든 버전에서 출발하면 그 기준(→ 형제 번호), 아니면 그 버전(→ 아래 번호)."""
    if record.get("stage") == stage and record.get("base"):
        return str(record["base"])
    return str(record["design_version"])


def _read_model(path: Path) -> DataModel:
    """data_model.json을 읽는다 (형식이 틀리면 RerunError)."""
    try:
        return DataModel.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RerunError(f"데이터 모델 파일을 읽을 수 없습니다: {path}\n{e}") from e


def prepare_functions(design: dict, requirement_ids: str = "", full: bool = False, feedback: str = "") -> list[str]:
    """기능 명세 다시 만들기의 범위: ["*"](전체), 요구사항 ID 목록(그 그룹만), 빈 목록(의견과 관련된 그룹만)."""
    known = {r["requirement_id"] for r in design.get("requirements") or []
             if r.get("type") == "FUNCTIONAL" and r.get("scope") == "SYSTEM" and r.get("status") != "EXCLUDED"}
    targets = list(dict.fromkeys(x.strip() for x in requirement_ids.split(",") if x.strip()))
    unknown = [t for t in targets if t not in known]
    if unknown:
        raise RerunError(f"시스템 기능 요구사항에 없는 ID: {', '.join(unknown)} (requirements.md의 요구사항 ID)")
    if full and targets:
        raise RerunError("--full과 --requirements는 함께 쓸 수 없습니다.")
    if not (full or targets or feedback.strip()):
        raise RerunError("다시 설계할 범위가 없습니다. --requirements(지정 요구사항의 기능 그룹만), "
                         "--feedback(의견과 관련된 것만), --full(전체) 중 하나를 주세요.")
    return ["*"] if full else targets


def load_answers(path: Path, questions: list[dict]) -> dict[str, str]:
    """확인 질문 답 파일 {"질문 ID": "답"}을 읽는다. 기준 버전에 없는 질문 ID면 RerunError, 빈 답은 뺀다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RerunError(f"답 파일을 읽을 수 없습니다: {path} ({e})") from e
    if not isinstance(data, dict):
        raise RerunError('답 파일은 {"질문 ID": "답"} 형식의 JSON이어야 합니다.')
    known = {q["question_id"] for q in questions}
    unknown = [k for k in data if k not in known]
    if unknown:
        raise RerunError(f"기준 버전에 없는 질문 ID: {', '.join(unknown)} (review_report.md의 '확인 질문·가정')")
    return {k: str(v).strip() for k, v in data.items() if str(v).strip()}


def load_submitted_design(path: Path, base: dict, stage: str) -> dict:
    """화면에서 고치고 확정한 기능 명세(design.json)를 읽는다 (rerun data_modeler --design).

    functions·common과, 화면에서 새로 답한 확인 질문을 쓴다. 답은 LLM 없이 코드로 반영한다 (apply_answers_by_code):
    기능 규칙 표시·문장, 공통 결정, 요구사항 결정·확정. 이슈 등 나머지는 기준 버전 것을 그대로 둔다.
    {"functions", "common"(없으면 None), "requirements", "questions", "note": 안내 줄}. 형식·ID가 틀리면 RerunError.
    """
    if stage != "data_modeler":
        raise RerunError("--design은 data_modeler 단계 옵션입니다 (확정한 기능 명세로 데이터 설계).")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RerunError(f"기능 명세 파일을 읽을 수 없습니다: {path} ({e})") from e
    raw = data.get("functions") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise RerunError("기능 명세 파일에 functions 목록이 없습니다.")
    try:
        functions = [FunctionSpec.model_validate(f) for f in raw]
    except ValueError as e:
        raise RerunError(f"기능 명세 형식이 올바르지 않습니다: {str(e).splitlines()[0]}") from e
    duplicated = sorted(i for i, n in Counter(f.function_id for f in functions).items() if n > 1)
    if duplicated:
        raise RerunError(f"기능 ID가 겹칩니다: {', '.join(duplicated)}")
    known = {r["requirement_id"] for r in base.get("requirements") or []}
    unknown = sorted({i for f in functions for i in [*f.requirement_ids, *f.nfr_ids] if i not in known})
    if unknown:
        raise RerunError(f"기준 버전에 없는 요구사항 ID: {', '.join(unknown[:10])}")
    common = data.get("common") if isinstance(data.get("common"), dict) and data.get("common") else base.get("common")
    requirements = [Requirement.model_validate(r) for r in base.get("requirements") or []]
    questions = [Question.model_validate(q) for q in base.get("questions") or []]
    answered = _submitted_answers(questions, data.get("questions"))
    if answered:
        functions, common = apply_answers_by_code(functions, common or {}, answered)
        _record_answers(answered, requirements)
    dumped = [f.model_dump(mode="json") for f in functions]
    changed, added, removed = changed_functions(base.get("functions") or [], dumped)
    differ = sum(q.answer.strip() != q.recommended.strip() for q in answered)
    return {"functions": dumped, "common": common or None,
            "requirements": [r.model_dump(mode="json") for r in requirements],
            "questions": [q.model_dump(mode="json") for q in questions],
            "note": f"기능 명세: 화면에서 확정한 명세 사용 (기능 {len(dumped)}개 · 기준 대비 바뀜 {len(changed)} · "
                    f"새 {len(added)} · 지움 {len(removed)})"
                    + (f" · 확인 질문 답 {len(answered)}건 코드로 반영 (추천과 다른 답 {differ}건)" if answered else "")}


def _submitted_answers(questions: list[Question], submitted) -> list[Question]:
    """화면에서 새로 답한 확인 질문 (기준 버전에서 아직 답하지 않은 것). questions의 상태·답을 고치고 그 목록을 돌려준다."""
    by_id = {q.get("question_id"): q for q in submitted or [] if isinstance(q, dict)}
    answered = []
    for q in questions:
        s = by_id.get(q.question_id) or {}
        answer = str(s.get("answer") or "").strip()
        if q.status != "ANSWERED" and s.get("status") == "ANSWERED" and answer:
            q.status, q.answer = "ANSWERED", answer
            answered.append(q)
    return answered


def _record_answers(answered: list[Question], requirements: list[Requirement]) -> None:
    """답을 관련 요구사항에 결정으로 남긴다 (사용자 답변 기록 노드와 같은 뜻, 다시 설계하지 않음).
    분류·포함 여부(RC)는 답대로 요구사항을 고치고, 충돌(CF)·요구사항 질문은 검토 표시를 지우고 확정한다."""
    by_id = {r.requirement_id: r for r in requirements}
    for q in answered:
        if q.stage == "requirement_reviewer":
            record_review_answer(q, by_id)
            continue
        if q.stage == "data_modeler":
            continue
        for rid in q.requirement_ids:
            if rid not in by_id:
                continue
            if q.stage != "function_designer":
                settle(by_id[rid], q.question_id)
                by_id[rid].status = "CONFIRMED"
            by_id[rid].decisions.append(f"[{q.question_id}] {q.question} → {q.answer}")
        for rid in q.confirms:
            if rid in by_id:
                by_id[rid].status = "CONFIRMED"


def prepare_requirements(feedback: str = "") -> list[str]:
    """요구사항 다시 만들기의 범위: 의견 반영만 한다 (빈 목록 = 의견이 바꾸는·빼는·새 요구사항만)."""
    if not feedback.strip():
        raise RerunError("요구사항 다시 만들기는 --feedback(반영할 의견)이 필요합니다. "
                         "예: --feedback \"고도화 기능(SFR-006)은 이번 범위에서 제외\"")
    return []


def ancestor(root: Path, project: str, label: str, has) -> dict | None:
    """label부터 이전 버전(parent)을 따라 올라가며 has(버전 폴더)가 참인 가장 가까운 버전 기록 (자기 자신 포함)."""
    records = {v["design_version"]: v for v in load_versions(root, project)}
    seen: set[str] = set()
    while label and label in records and label not in seen:
        seen.add(label)
        if has(Path(records[label]["output_dir"])):
            return records[label]
        label = records[label].get("parent")
    return None


def has_functions(folder: Path) -> bool:
    """버전 폴더에 기능 명세가 있는지 (요구사항 단계 버전에는 없다)."""
    path = folder / "design.json"
    return path.exists() and bool(json.loads(path.read_text(encoding="utf-8")).get("functions"))


def changed_requirements(before: list[dict], after: list[dict]) -> list[str]:
    """두 버전에서 내용이 달라진 요구사항 ID (새·바뀜·지움·범위 제외 모두). 기능 명세를 다시 설계할 범위가 된다."""
    old = {r["requirement_id"]: {k: r.get(k) for k in REQ_FIELDS} for r in before}
    new = {r["requirement_id"]: {k: r.get(k) for k in REQ_FIELDS} for r in after}
    return sorted(i for i in set(old) | set(new) if old.get(i) != new.get(i))


def changed_functions(before: list[dict], after: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """두 버전의 기능 명세 차이: (내용이 바뀐 기능, 새 기능, 지운 기능) ID."""
    old = {f["function_id"]: f for f in before}
    new = {f["function_id"]: f for f in after}
    return (sorted(i for i in new if i in old and new[i] != old[i]), sorted(set(new) - set(old)),
            sorted(set(old) - set(new)))


def _inherit_model(base_dir: Path, base_functions: list[dict], functions: list[dict], base_label: str,
                   targets: list[str], full: bool) -> tuple[dict, list[str] | None, ReworkSummary]:
    """기능 명세 단계 버전에서 데이터 설계를 시작할 때: 기준 버전의 모델을 이어받고 바뀐 기능에 연결된 엔티티만 다시 설계한다.

    새 기능은 추적 검토 → 연결 보완(cover)이 기존 엔티티에 연결하거나 새 엔티티를 만들고, 지운 기능의 연결은 정규화가 지운다.
    사용자가 고친 엔티티는 기능이 바뀌어도 다시 설계하지 않고 확인 필요로 남긴다.
    """
    model = _read_model(base_dir / "data_model.json")
    changed, added, removed = changed_functions(base_functions, functions)
    linked = [e for e in model.entities if set(e.function_ids) & set(changed)]
    summary = ReworkSummary(inherited_from=base_label, changed_functions=changed, added_functions=added,
                            removed_functions=removed, full=full,
                            skipped=[e.entity_id for e in linked if e.user_edited and e.entity_id not in targets])
    names = {e.entity_id for e in model.entities}
    unknown = [t for t in targets if t not in names]
    if unknown:
        raise RerunError(f"데이터 모델(v{base_label})에 없는 엔티티: {', '.join(unknown)}")
    if full:
        return model.model_dump(mode="json"), None, summary
    auto = [e.entity_id for e in linked if not e.user_edited]
    return model.model_dump(mode="json"), list(dict.fromkeys(auto + targets)), summary


def prepare_data_model(values: dict, version_dir: Path, edited_path: Path | None, same_version: bool,
                       entities: str = "", full: bool = False, feedback: str = "",
                       inherited: tuple[Path, list[dict], str] | None = None, functions: list[dict] | None = None) \
        -> tuple[dict | None, list[str] | None, ReworkSummary]:
    """데이터 설계 다시 만들기의 시작 모델, 컬럼을 다시 설계할 엔티티, 사용자 수정 요약을 정한다.

    values: 기준 버전을 만든 실행의 체크포인트 상태
    version_dir: 기준 버전 폴더 / edited_path: 사용자가 고친 data_model.json (없으면 기준 버전 폴더의 것)
    same_version: 기준 버전이 그 실행의 마지막 버전인지. 그러면 체크포인트의 모델이 생성 당시 원본이다
      (DB 검증 실패로 한 실행이 여러 버전을 만든 경우 앞 버전은 폴더의 파일이 원본).
    inherited: 기준 버전이 기능 명세 단계 버전(데이터 모델 없음)일 때 그 기준 버전의 (폴더, 기능 명세, 번호).
      functions는 이번 기준 버전의 기능 명세다.
    (시작 모델 — 없으면 None으로 처음부터 전체 설계, 다시 설계할 엔티티 — None이면 전체, 요약)
    """
    folder = version_dir / "data_model.json"
    source = edited_path or folder
    targets = list(dict.fromkeys(to_identifier(x) for x in entities.split(",") if x.strip()))
    if not source.exists():
        if edited_path is None and inherited and (inherited[0] / "data_model.json").exists():
            return _inherit_model(inherited[0], inherited[1], functions or [], inherited[2], targets, full)
        if targets or edited_path:
            raise RerunError(f"데이터 모델 파일이 없습니다: {source}")
        return None, None, ReworkSummary(full=True)   # 이어받을 모델이 없으면 처음부터 전체 설계
    edited = _read_model(source)
    if same_version and values.get("data_model"):
        original = DataModel.model_validate(values["data_model"])
    elif edited_path and folder.exists():
        original = _read_model(folder)
    else:
        original = None   # 비교할 원본이 없으면 사용자 수정으로 보지 않는다
    model, summary = apply_user_edits(original, edited)

    names = {e.entity_id for e in model.entities}
    unknown = [t for t in targets if t not in names]
    if unknown:
        raise RerunError(f"데이터 모델에 없는 엔티티: {', '.join(unknown)} (data_model.json의 entity_id)")
    if full and targets:
        raise RerunError("--full과 --entities는 함께 쓸 수 없습니다.")
    if not (full or targets or feedback.strip() or summary.has_edits()):
        raise RerunError("다시 설계할 범위가 없습니다. --entities(지정 엔티티만), --feedback(의견과 관련된 엔티티만), "
                         "--full(전체), 또는 고친 data_model.json(--data-model) 중 하나를 주세요.")
    summary.full = full
    return model.model_dump(mode="json"), (None if full else targets), summary


def seed_state(values: dict, design: dict, stage: str, run_id: str, base: str, project: str,
               feedback: str = "", validate_db: bool | None = None, model: dict | None = None,
               targets: list[str] | None = None, rework: ReworkSummary | None = None) -> dict:
    """원래 실행의 마지막 상태 + 기준 버전의 산출물(design.json)로 새 실행의 시작 상태를 만든다.

    values: 기준 버전을 만든 실행의 체크포인트 상태 (문서 블록·추출 단위·변경 횟수 등)
    design: 기준 버전 폴더의 design.json (그 버전의 요구사항·기능 명세·질문·이슈)
    데이터 설계: model·targets·rework는 prepare_data_model 결과. model이 없으면 데이터 모델을 처음부터 설계하고,
      있으면 그 모델에서 targets만 다시 설계한다 (targets가 None이면 고정 엔티티를 뺀 전체 → model_targets ["*"]).
    기능 명세: targets는 prepare_functions 결과. 기준 기능 명세를 두고(ID 유지·부분 재설계) 데이터 설계 전에 멈춘다.
    요구사항: 의견을 반영해 다음 최상위 번호(v3 …)를 받고 요구사항에서 멈춘다. 이전 버전은 기준 버전이다.
      뒤 단계 산출물은 비운다 (다음 기능 명세 단계가 가장 가까운 기능 명세 버전에서 바뀐 요구사항의 그룹만 다시 설계한다).
    """
    spec = STAGES[stage]
    b, r = basis(values), rev(values)
    options = dict(values.get("options") or {})
    # 단계 버전에서 이어 가도 그 버전의 멈춤 설정·이전 버전 지정은 물려받지 않는다
    options.pop("stop_after", None)
    options.pop("parent_version", None)
    options.update({
        "project": project, "base_version": base, "stage": stage,
        "feedback": {stage: feedback.strip()} if feedback.strip() else {},
        "validate_db": options.get("validate_db", False) if validate_db is None else validate_db,
    })
    issues = [i for i in design.get("issues") or [] if not i["kind"].startswith(spec["drop_issue_prefixes"])]
    requirements_stage = stage == "requirements_analyst"
    functions_stage = stage == "function_designer"
    if requirements_stage:   # 요구사항 버전은 최상위 번호 (팀 규칙: 요구사항 v1 … v6 → 확정한 v6에서 기능 명세 v6.1)
        options.update(stop_after="requirements_analyst", base_version=None, parent_version=base)
    if functions_stage:
        options["stop_after"] = "function_designer"
    keep = model is not None and not requirements_stage
    semantic = {**SEMANTIC_BASIS_INIT, **b["semantic"], "model": [-1, -1]}
    if functions_stage or requirements_stage:
        semantic["functions"] = [-1, -1]
    return {
        **values,
        "run_id": run_id,
        "options": options,
        # 기준 버전의 앞 단계 산출물 (기능 명세 단계는 기준 기능 명세를 다시 설계할 출발점으로 둔다)
        "requirements": design.get("requirements") or [],
        "functions": [] if requirements_stage else design.get("functions") or [],
        "questions": design.get("questions") or [],
        "issues": issues,
        "requirement_targets": targets if requirements_stage else None,
        "requirement_rework": {},
        "function_targets": targets if functions_stage else None,
        "function_rework": {}, "function_answers": [],
        # 다시 만들 단계와 그 뒤 (기준 모델을 두면 그 모델은 기능 명세와 맞는 상태다)
        "data_model": None if functions_stage or requirements_stage else model,
        "model_redesign": False,
        "model_targets": (["*"] if targets is None else targets) if keep and not functions_stage else None,
        "model_rework": (rework or ReworkSummary()).model_dump(),
        "trace_links": [],
        "dirty_requirements": [], "dirty_functions": [],
        "basis": {**b, "functions": r["requirements"], "model": r["functions"] if keep else -1, "review": [-1, -1],
                  "semantic": semantic},
        # 실행 기록은 새로
        "decisions": [], "step": 0, "failures": {}, "completion": {}, "versions": [], "design_version": "",
        "db_validation": None, "db_attempts": 0, "llm_usage": {}, "pending_answers": {}, "semantic_review": {},
        "next_action": {}, "last_action": {},
    }
