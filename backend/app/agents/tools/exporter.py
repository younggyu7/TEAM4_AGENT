"""산출물 파일 출력: 요구사항 정의서·기능 명세서·ERD·DDL·추적표·검토 보고서·DB 검증 보고서 (Markdown/SQL/JSON/CSV),
API 설계를 한 최종본('ERD 최종 확정')은 개발 스펙 문서 9개도 (tools/dev_spec.py)."""
from __future__ import annotations

import csv
import io
import json
import re
from collections import OrderedDict
from datetime import datetime

from app.schemas.data_model import DataModel, DbValidationResult
from app.schemas.document import DocumentBlock, RequirementSource
from app.schemas.function import FunctionSpec
from app.schemas.requirement import QUESTION_STAGES, Question, Requirement
from app.schemas.review import Issue, TraceLink
from app.agents.roles.api_designer import answer_stack, tech_stack
from app.agents.roles.function_designer import assumption_share
from app.agents.tools.ddl import render_script
from app.agents.tools.dev_spec import FILES as DEV_SPEC_FILES, SPEC_FORMAT, write_dev_spec
from app.agents.tools.erd import render_mermaid
from app.agents.tools.model_edits import ReworkSummary
from app.agents.flow.rerun import STAGES
from app.agents.tools.convention import ConventionReport, apply_profile, ddl_model, names_csv, profile_of
from app.agents.tools.trace_diff import compare_versions, load_previous, version_changes
from app.agents.llm.client import model_label
from app.agents.storage import get_store
from app.agents.tools.versions import project_of, update_version

SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}


def cell(text) -> str:
    """Markdown 표 칸에 넣을 수 있게 | 와 줄바꿈을 바꾼다."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", "<br>")


def table(headers: list[str], rows: list[list]) -> str:
    """머리글과 행으로 Markdown 표를 만든다."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def export_design(state: dict, version: str) -> list[str]:
    """설계 버전 하나의 산출물 파일(요구사항·기능·ERD·DDL·추적표·검토·JSON)을 산출물 저장소에 모두 쓰고 위치 목록을 돌려준다."""
    project = project_of(state)

    def _write(name: str, text: str) -> str:
        return get_store().write(project, version, name, text)
    reqs = [Requirement.model_validate(r) for r in state.get("requirements") or []]
    questions = [Question.model_validate(q) for q in state.get("questions") or []]
    funcs = [FunctionSpec.model_validate(f) for f in state.get("functions") or []]
    model = DataModel.model_validate(state["data_model"]) if state.get("data_model") else None
    links = [TraceLink.model_validate(t) for t in state.get("trace_links") or []]
    issues = [Issue.model_validate(i) for i in state.get("issues") or []]
    blocks = {b["source_block_id"]: DocumentBlock.model_validate(b) for b in state.get("blocks") or []}
    sources = [RequirementSource.model_validate(s) for s in state.get("sources") or []]
    doc = state.get("document") or {}
    options = state.get("options") or {}
    meta = {
        "project": project_of(state), "run_id": state.get("run_id"), "design_version": version, "document": doc,
        "stage": options.get("stage", "full"), "base_version": options.get("base_version"),
        "feedback": (options.get("feedback") or {}).get(options.get("stage", ""), ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    head = (f"> 문서: {doc.get('file_name', '')} (`{doc.get('document_id', '')}` / {doc.get('document_version', '')})  \n"
            f"> 프로젝트: **{meta['project']}** · 설계 버전: **v{version}** · 실행: `{state.get('run_id')}` · "
            f"생성: {meta['generated_at']}\n")
    rerun_from = meta["base_version"] or options.get("parent_version")   # 요구사항 다시 만들기는 기준 번호 없이 다음 최상위 번호
    if meta["stage"] != "full" and rerun_from:   # 한 단계만 다시 만든 버전
        label = "최종 확정 (추적·의미 검토)" if meta["stage"] == "final" else STAGES.get(meta["stage"], {}).get("label", meta["stage"])
        head += (f"> 다시 만든 단계: **{label}** · 기준 버전: v{rerun_from}"
                 f" (앞 단계 결과는 기준 버전 그대로)" + (f" · 의견: {meta['feedback']}" if meta["feedback"] else "") + "\n")
        if meta["stage"] == "data_modeler" and state.get("model_rework"):
            meta["rework"] = state["model_rework"]
            head += _rework_line(ReworkSummary.model_validate(state["model_rework"]))
        if meta["stage"] == "function_designer" and state.get("function_rework"):
            meta["rework"] = state["function_rework"]
            head += _function_rework_line(state["function_rework"])
    until = options.get("stop_after")
    if until == "requirements_analyst" or (until == "function_designer" and meta["stage"] != "function_designer"):
        nxt = "function_designer" if until == "requirements_analyst" else "data_modeler"
        head += (f"> 이 버전은 **{STAGES[until]['label']}**까지 만들었다. 다음 단계는 이 버전을 확정(`confirm`)한 뒤 "
                 f"`rerun {nxt} --from v{version}`\n")
    # 이전 버전 대비 변경 (추적표 4절). 기능 명세 단계 버전은 데이터 설계 전이라 엔티티 대신 데이터 설계 영향을 보여 준다
    stage_only = until == "function_designer"
    requirements_only = until == "requirements_analyst"
    previous = load_previous(project, state.get("parent_version"))
    changes = None
    if previous and requirements_only:   # 요구사항만 비교하고, 영향은 기준 버전의 기능·엔티티로 보여 준다
        changes = compare_versions({"requirements": reqs, "functions": [], "model": None}, previous, False)
    elif previous:
        changes = version_changes(reqs, funcs, model, previous)
    if changes:
        meta["changes"] = {k: changes[k] for k in ("from", "model_from", "counts")}

    # 요구사항 단계 버전: 기능 명세·추적표는 아직 없으므로 쓰지 않고, 이전 버전 대비 변경은 요구사항 정의서 끝에 둔다
    req_text = _requirements_md(head, reqs, questions, blocks, sources)
    if requirements_only and changes:
        req_text += "\n" + "\n".join(changes_md(changes, f"## 5. 이전 버전(v{changes['from']}) 대비 변경")) + "\n"
    files = [_write("requirements.md", req_text)]
    if not requirements_only:
        files.append(_write("functions.md", _functions_md(head, funcs, reqs, state.get("function_common"),
                                                                    questions, state.get("function_confirm"))))
    # 설계 표준(--standard): ERD·테이블 정의서·DDL만 표준 이름, data_model.json은 설계 이름 그대로 (다시 설계·사용자 수정의 기준)
    profile = profile_of(options)
    conv = None
    if model:
        physical = model
        if profile and profile.renames:
            physical, conv = apply_profile(model, profile)
            meta["standard"] = {"profile": profile.key, "name": profile.name,
                                "tables": conv.count("table"), "columns": conv.count("column"),
                                "missing_words": len(conv.missing), "violations": len(conv.violations)}
        files += [
            _write("erd.mmd", render_mermaid(physical)),
            _write("erd.md", _erd_md(head, physical, version, conv)),
            _write("schema.sql", render_script(ddl_model(physical, profile), version, "postgresql")),
            _write("data_model.json", model.model_dump_json(indent=2)),
        ]
        if conv:
            files.append(_write("standard_names.csv", names_csv(conv)))
        if (state.get("api_design") or {}).get("functions"):
            # 개발 스펙: 개발 AI가 읽을 고정 형식 문서 (API 설계를 한 최종본만, 이름은 schema.sql과 같게)
            stack = tech_stack(state.get("api_design"), questions)
            files += write_dev_spec(state, _write, meta["project"], version, model, ddl_model(physical, profile), conv,
                                    profile, stack)
            meta["dev_spec"] = {"format": SPEC_FORMAT, "files": list(DEV_SPEC_FILES), "stack": stack["backend"],
                                "stack_basis": stack["basis"]}
    if not requirements_only:
        files += [
            _write("traceability.md", _trace_md(head, reqs, funcs, model, questions, stage_only, changes)),
            _write("traceability.csv", _trace_csv(links, version)),
        ]
    files += [
        _write("review_report.md", _review_md(head, state, issues, questions, conv)),
        # 데이터 모델·추적 링크는 data_model.json·traceability.csv와 겹치므로 넣지 않고 위치만 적는다.
        # 사람이 읽는 파일이 아니라서 들여쓰기 없이 쓴다 (크기 약 60% 감소).
        _write("design.json", json.dumps({
            "meta": {**meta, "until": until, "files": {"data_model": "data_model.json" if model else None,
                                                       "trace_links": None if requirements_only else "traceability.csv"}},
            "requirements": state.get("requirements"), "questions": state.get("questions"),
            "common": state.get("function_common") or {}, "functions": state.get("functions"),
            "api_design": state.get("api_design") or {}, "issues": state.get("issues"),
            "completion": state.get("completion"), "decisions": state.get("decisions"),
        }, ensure_ascii=False, separators=(",", ":"))),
    ]
    return files


def _trace_csv(links: list[TraceLink], version: str) -> str:
    """추적 링크 CSV 내용 (Excel에서 한글이 깨지지 않게 BOM 포함)."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["from_id", "to_id", "link_type", "design_version"])
    for l in links:
        w.writerow([l.from_id, l.to_id, l.link_type, version])
    return "\ufeff" + out.getvalue()


# ------------------------------------------------------------------ 요구사항 정의서
def _evidence_text(r: Requirement, blocks: dict[str, DocumentBlock]) -> str:
    """요구사항 근거를 '블록ID: "인용문" (위치)' 텍스트로 만든다."""
    parts = []
    for ev in r.evidence:
        b = blocks.get(ev.source_block_id)
        where = b.location.label() if b else "위치 미확인"
        mark = "" if ev.verified else " ⚠미확인"
        parts.append(f"{ev.source_block_id}{mark}: \"{ev.quote}\" ({where})")
    return "\n".join(parts) or "⚠근거 없음"


def _req_rows(reqs: list[Requirement], blocks) -> list[list]:
    """요구사항 정의서 표의 행 목록을 만든다."""
    rows = []
    for r in reqs:
        desc = r.description
        if r.acceptance_criteria:
            desc += "\n수용 기준: " + "; ".join(r.acceptance_criteria)
        if r.decisions:
            desc += "\n결정: " + "; ".join(r.decisions)
        for n in r.review:   # 요구사항 검토 표시 (확인 대기는 질문 ID와 함께)
            desc += f"\n검토{' (확인 대기 ' + n.question_id + ')' if n.question_id else ''}: {n.note}"
        priority = "미정(원문에 없음)" if r.priority == "UNSPECIFIED" else r.priority
        rows.append([r.requirement_id, r.title, desc, priority, r.status, _evidence_text(r, blocks)])
    return rows


def _requirements_md(head, reqs, questions, blocks, sources) -> str:
    """요구사항 정의서: 기능 / 비기능 / 사업 수행 의무 / 확인 질문."""
    names = {s.source_key: f"[{s.source_key}] {s.name}" for s in sources}
    count = lambda t, sc: sum(1 for r in reqs if r.type == t and r.scope == sc)  # noqa: E731
    out = ["# 요구사항 정의서", "", head, "## 요약", "", table(
        ["구분", "시스템 요구 (설계 대상)", "사업 수행 의무", "합계"],
        [["기능", count("FUNCTIONAL", "SYSTEM"), count("FUNCTIONAL", "PROJECT"),
          sum(r.type == "FUNCTIONAL" for r in reqs)],
         ["비기능", count("NON_FUNCTIONAL", "SYSTEM"), count("NON_FUNCTIONAL", "PROJECT"),
          sum(r.type == "NON_FUNCTIONAL" for r in reqs)],
         ["합계", sum(r.scope == "SYSTEM" for r in reqs), sum(r.scope == "PROJECT" for r in reqs), len(reqs)]]),
        "", "- 사업 수행 의무(PROJECT): 수행업체의 보안서약·교육·산출물 제출 등. 시스템 기능·데이터 설계 대상에서 제외한다.", ""]
    headers = ["ID", "요구사항", "설명", "우선순위", "상태", "원문 근거"]

    out += ["## 1. 기능 요구사항", ""]
    by_src: OrderedDict[str, list] = OrderedDict()
    for r in reqs:
        if r.type == "FUNCTIONAL" and r.scope == "SYSTEM":
            by_src.setdefault(r.source_key, []).append(r)
    for key, items in by_src.items():
        out += [f"### {names.get(key, key)}", "", table(headers, _req_rows(items, blocks)), ""]

    out += ["## 2. 비기능 요구사항", ""]
    by_cat: OrderedDict[str, list] = OrderedDict()
    for r in reqs:
        if r.type == "NON_FUNCTIONAL" and r.scope == "SYSTEM":
            by_cat.setdefault(r.category, []).append(r)
    for cat, items in by_cat.items():
        out += [f"### {cat}", "", table(headers, _req_rows(items, blocks)), ""]

    project = [r for r in reqs if r.scope == "PROJECT"]
    out += ["## 3. 사업 수행 의무 (설계 대상 외)", "",
            table(["ID", "유형", "분류", "요구사항", "설명"],
                  [[r.requirement_id, "기능" if r.type == "FUNCTIONAL" else "비기능", r.category, r.title, r.description]
                   for r in project]) if project else "- 없음", ""]

    out += ["## 4. 확인 질문·결정", ""]
    out.append(table(["ID", "유형", "차단", "질문", "선택지", "관련 요구사항", "상태", "답변"],
                     [[q.question_id, q.kind, "예" if q.blocking else "", q.question + "\n(" + q.context + ")",
                       "\n".join(q.options), ", ".join(q.requirement_ids), q.status, q.answer] for q in questions])
               if questions else "- 없음")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ 기능 명세서
REQ_STATUS = {"ASSUMED": "가정한 해석", "OPEN": "결정 필요"}


def _unconfirmed(f: FunctionSpec, status: dict[str, str]) -> list[str]:
    """기능의 근거 요구사항 중 확정되지 않은 것."""
    return [rid for rid in f.requirement_ids if status.get(rid) in REQ_STATUS]


def _related_questions(rid: str, questions: list[Question]) -> str:
    """요구사항에 걸린 질문 ID와 상태 (예: Q-003 답 없음)."""
    return ", ".join(f"{q.question_id} {'답: ' + q.answer if q.status == 'ANSWERED' else '답 없음'}"
                     for q in questions if rid in q.requirement_ids or rid in q.confirms) or "-"


def _functions_md(head, funcs: list[FunctionSpec], reqs: list[Requirement], common: dict | None = None,
                  questions: list[Question] | None = None, confirm: dict | None = None) -> str:
    """기능 명세서: 고객 확인, 공통 결정, 기능 목록과 기능별 상세."""
    questions = questions or []
    titles = {r.requirement_id: r.title for r in reqs}
    status = {r.requirement_id: r.status for r in reqs}
    out = ["# 기능 명세서", "", head, *_confirm_md(funcs, status, questions, confirm or {}),
           *_common_md(common or {}, funcs), "## 기능 목록", "",
           table(["기능 ID", "기능명", "근거 요구사항", "액터", "데이터 객체"],
                 [[f.function_id, f.name + (" (요구사항 미확정)" if f.requirement_ids and
                                            len(_unconfirmed(f, status)) == len(f.requirement_ids) else ""),
                   ", ".join(f.requirement_ids), ", ".join(f.actors), ", ".join(f.data_objects)]
                  for f in funcs]), ""]
    for f in funcs:
        pending = _unconfirmed(f, status)
        out += [f"## {f.function_id} {f.name}", "", f.description, "",
                "- 근거 요구사항: " + ", ".join(f"{rid}({titles.get(rid, '')})" for rid in f.requirement_ids),
                *(["- ⚠ 확정되지 않은 근거 요구사항: " + ", ".join(
                    f"{rid} {REQ_STATUS[status[rid]]} (질문: {_related_questions(rid, questions)})" for rid in pending)]
                  if pending else []),
                "- 관련 비기능 요구사항: " + (", ".join(f.nfr_ids) or "없음"),
                "- 액터: " + (", ".join(f.actors) or "-"),
                "- 사전 조건: " + ("; ".join(f.preconditions) or "-"),
                "- 데이터 객체: " + (", ".join(f.data_objects) or "없음"), ""]
        if f.inputs:
            out += ["**입력**", "", table(["항목", "설명", "필수"],
                                          [[i.name, i.description, "Y" if i.required else "N"] for i in f.inputs]), ""]
        if f.outputs:
            out += ["**출력**", "", table(["항목", "설명"], [[o.name, o.description] for o in f.outputs]), ""]
        if f.business_rules:
            out += ["**업무 규칙**", "", *[f"- {b}" for b in f.business_rules], ""]
        if f.normal_flow:
            # LLM이 단계마다 '1. '을 붙여 오기도 하므로 지우고 다시 번호를 매긴다 ('1. 1. …' 방지)
            steps = [re.sub(r"^\s*\d+\s*[.)]\s*", "", step) for step in f.normal_flow]
            out += ["**정상 흐름**", "", *[f"{i}. {step}" for i, step in enumerate(steps, start=1)], ""]
        if f.exception_flows:
            out += ["**예외 흐름**", "", table(["조건", "처리"], [[e.condition, e.handling] for e in f.exception_flows]), ""]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ ERD · 테이블 정의서
def _confirm_md(funcs: list[FunctionSpec], status: dict[str, str], questions: list[Question], confirm: dict) -> list[str]:
    """기능 명세서의 고객 확인 절: '(가정)' 비율과 한도, 고객에게 묻는 질문, 묻지 않고 추천안으로 정한 수, 확정되지 않은 요구사항에 기댄 기능."""
    asked = [q for q in questions if q.stage == "function_designer"]
    pending = [(f, _unconfirmed(f, status)) for f in funcs]
    pending = [(f, ids) for f, ids in pending if ids]
    if not asked and not pending and not confirm.get("candidates"):
        return []
    out = ["## 고객 확인", ""]
    if confirm.get("candidates"):
        n, total = assumption_share(funcs)
        share = f"'(가정)' 규칙은 업무 규칙 {total}개 중 {n}개({round(100 * n / max(1, total))}%)" + (
            f", 한도 {round(100 * confirm['limit'])}%" if "limit" in confirm else "")
        out.append(f"- {share}. 확인 대상 후보 {confirm['candidates']}개 중 질문 {len(confirm.get('questions') or [])}개를 "
                   f"고객에게 묻고, 묻지 않은 '(확인 필요)' {confirm.get('assumed', 0)}개는 추천안으로 정해 '(가정)'으로 표시했다"
                   + (f" (한도를 넘어 {confirm['kept']}개는 추천안을 '(확인 필요)'로 남김: 화면의 '확인 필요 규칙'에서 확인하거나 의견으로 정한다)"
                      if confirm.get("kept") else "")
                   + (f" (정하지 못한 {confirm['unresolved']}개는 '(확인 필요)'로 남음)" if confirm.get("unresolved") else "")
                   + ".")
    out += ["- 질문은 답이 없으면 추천안으로 설계해 두었다. 업무 규칙의 '(확인 필요 FQ-…)'가 그 질문으로 정해지는 규칙이며, "
            "추천과 같은 답은 '(결정 FQ-…)'로 바뀌고 다른 답이면 관련 기능만 고친다.", ""]
    if asked:
        out += [table(["ID", "질문", "선택지", "추천 (답이 없으면 이 안으로 설계)", "관련 요구사항", "상태", "답변"],
                      [[q.question_id, f"{q.question}\n({q.context})", "\n".join(q.options), q.recommended,
                        ", ".join(q.requirement_ids), q.status, q.answer] for q in asked]), ""]
    if pending:
        out += ["**확정되지 않은 요구사항에 기댄 기능** (요구사항이 바뀌면 이 기능도 바뀐다)", "",
                table(["기능 ID", "기능명", "확정되지 않은 근거 요구사항", "관련 질문"],
                      [[f.function_id, f.name, ", ".join(f"{rid} ({REQ_STATUS[status[rid]]})" for rid in ids),
                        "; ".join(f"{rid}: {_related_questions(rid, questions)}" for rid in ids)]
                       for f, ids in pending]), ""]
    return out


def _basis_cell(item: dict) -> str:
    """공통 결정 항목의 근거 칸 (질문 중이면 질문 ID, 추천과 다른 답이면 그 답)."""
    basis = item.get("basis") or "원문"
    if item.get("question") and basis in ("확인 필요", "결정"):
        basis += f" {item['question']}"
    return basis + (f" → {item['answer']}" if item.get("answer") else "")


def _common_md(common: dict, funcs: list[FunctionSpec] | None = None) -> list[str]:
    """기능 명세서의 공통 결정 절 (여러 기능 영역에 같게 적용하는 사용자 역할·업무 객체 이름·분류·상태 값·공통 규칙·공통 기능)."""
    if not any(common.get(k) for k in ("roles", "terms", "code_sets", "policies", "shared_functions")):
        return []
    out = ["## 공통 결정", "", "여러 기능 영역에 같게 적용한다. 기능 업무 규칙의 '(공통 P번호)'는 아래 공통 규칙을 붙인 것이다.", ""]
    if common.get("roles"):
        out += ["**사용자 역할** (기능의 사람 행위자는 이 이름. 같은 뜻의 표현은 시스템이 이 이름으로 맞춘다)", "",
                table(["역할", "하는 일", "같은 뜻의 표현"],
                      [[r["name"], r.get("scope", ""), ", ".join(r.get("aliases") or [])] for r in common["roles"]]), ""]
    if common.get("terms"):
        out += ["**업무 객체 표준 이름**", "", table(["이름", "같은 뜻의 표현"],
                                                  [[t["name"], ", ".join(t.get("synonyms") or [])] for t in common["terms"]]), ""]
    if common.get("code_sets"):
        out += ["**분류·상태 값**", "", table(["이름", "값", "근거", "요구사항"],
                                            [[c["name"], " / ".join(c["values"]), _basis_cell(c), ", ".join(c["requirement_ids"])]
                                             for c in common["code_sets"]]), ""]
    if common.get("policies"):
        out += ["**공통 규칙**", "", table(["번호", "규칙", "근거", "요구사항"],
                                         [[f"P{i}", p["rule"], _basis_cell(p), ", ".join(p["requirement_ids"])]
                                          for i, p in enumerate(common["policies"], start=1)]), ""]
    if common.get("shared_functions"):
        def made(ids: list[str]) -> str:   # 공통 기능 요구사항을 둘 이상 연결한 기능 = 그 공통 기능을 만든 것
            return ", ".join(f.function_id for f in funcs or [] if len(set(f.requirement_ids) & set(ids)) >= 2) or "-"
        out += ["**공통 기능** (여러 영역의 요구사항을 기능 하나로 만들어 영역마다 같은 기능을 만들지 않는다)", "",
                table(["기능", "범위", "요구사항", "만든 영역", "기능 ID"],
                      [[s["name"], s.get("scope", ""), ", ".join(s["requirement_ids"]), s.get("owner", ""),
                        made(s["requirement_ids"])] for s in common["shared_functions"]]), ""]
    return out


def _rework_line(rework: ReworkSummary) -> str:
    """데이터 설계 다시 만들기 범위와 반영한 사용자 수정 (머리말 한 줄)."""
    scope = ("전체 (사용자가 고친 엔티티는 유지)" if rework.full else
             f"엔티티 {len(rework.redesigned)}개 ({', '.join(rework.redesigned)})" if rework.redesigned else
             "없음 (사용자 수정만 정리·검증)")
    if rework.inherited_from:
        scope += (f" · v{rework.inherited_from}의 데이터 모델에서 이어받음 (바뀐 기능 {len(rework.changed_functions)}개, "
                  f"새 기능 {len(rework.added_functions)}개, 지운 기능 {len(rework.removed_functions)}개)")
    edits = f" · 사용자 수정: {rework.edit_text()}" if rework.has_edits() else ""
    return f"> 다시 설계한 범위: {scope}{edits}\n"


def _function_rework_line(rework: dict) -> str:
    """기능 명세 다시 만들기 범위와 바뀐 기능 수 (머리말 한 줄)."""
    groups = rework.get("groups") or []
    scope = "전체" if rework.get("full") else f"그룹 {len(groups)}개 ({', '.join(groups) or '없음'})"
    counts = " · ".join(f"{label} {len(rework.get(key) or [])}개" for key, label in
                        (("added", "새 기능"), ("changed", "바뀐 기능"), ("removed", "지운 기능")))
    return f"> 다시 설계한 범위: {scope} · {counts} · 데이터 설계는 이 버전 확정 후 (rerun data_modeler --from 이 버전)\n"


def _erd_md(head, model: DataModel, version: int, conv: ConventionReport | None = None) -> str:
    """ERD(Mermaid)와 테이블 정의서. 설계 표준을 적용했으면 표준 이름으로 쓰고 설계 이름을 옆에 둔다."""
    logical = conv.logical() if conv else {}
    standard = (f"설계 표준 **{conv.profile}**을 적용한 표준 이름이다 (설계 이름은 `data_model.json`, 대응표는 `standard_names.csv`). "
                if conv else "")
    out = ["# ERD · 테이블 정의서", "", head,
           f"엔티티 {len(model.entities)}개 · 관계 {len(model.relations)}개. " + standard +
           "DDL(`schema.sql`)과 같은 구조화 데이터 모델(`data_model.json`)에서 생성되었다.", "",
           "```mermaid", render_mermaid(model).rstrip(), "```", "", "## 테이블 정의서", ""]
    for e in model.entities:
        design = logical.get((e.entity_id, ""), ("", ""))[0]
        columns = ["컬럼", *(["설계 이름"] if conv else []), "한글명", "타입", "NULL", "키", "참조", "허용값", "설명"]
        out += [f"### {e.entity_id} ({e.label}) — {e.kind}" + (f" · 설계 이름 `{design}`" if design else "")
                + (" · 사용자 수정 (고정)" if e.user_edited else ""), "",
                e.description, "",
                "- 관련 기능: " + (", ".join(e.function_ids) or "없음"),
                "- 관련 요구사항: " + (", ".join(e.requirement_ids) or "없음"),
                *_constraint_lines(e), "",
                table(columns,
                      [[a.name, *([logical.get((e.entity_id, a.name), ("", "(표준 컬럼)"))[1] or "(표준 컬럼)"] if conv else []),
                        a.label, a.data_type + (f"({a.length})" if a.data_type == "varchar" else ""),
                        "Y" if a.nullable else "N",
                        ",".join(k for k, on in (("PK", a.primary_key), ("FK", bool(a.references)), ("UK", a.unique)) if on),
                        a.references, ", ".join(a.allowed_values), a.description] for a in e.attributes]), ""]
    return "\n".join(out) + "\n"


def _standard_md(conv: ConventionReport | None) -> list[str]:
    """검토 보고서의 표준 준수 절: 표준 단어로 만든 이름 비율, 표준 단어 미등록, 위반."""
    if conv is None:
        return []
    (t, tt), (c, ct) = conv.count("table"), conv.count("column")
    out = [f"## 표준 준수 (설계 표준: {conv.profile})", "",
           f"- 표준 이름으로 만든 테이블 {t}/{tt}개, 컬럼 {c}/{ct}개. 나머지는 사전에 없는 단어가 있어 설계 이름(영문)을 썼다.",
           f"- 표준 단어 미등록 {len(conv.missing)}개 — `standards/{conv.profile_key}/words.csv`에 넣으면 다음 실행부터 표준 이름이 된다.",
           "- 대응표: `standard_names.csv` (설계 이름 ↔ 표준 이름, 만든 방법)", ""]
    if conv.missing:
        out += [table(["미등록 단어", "횟수", "쓰인 곳 (예)"],
                      [[w, n, ", ".join(conv.missing_examples[w])] for w, n in conv.missing.most_common(30)]), ""]
        if len(conv.missing) > 30:
            out += [f"(나머지 {len(conv.missing) - 30}개는 `standard_names.csv`의 '설계 이름' 줄에서 확인)", ""]
    out += ["**표준 위반**", "", *([f"- {v}" for v in conv.violations] or ["- 없음"]), ""]
    return out


def _constraint_lines(e) -> list[str]:
    """테이블 정의서의 여러 컬럼 제약 줄."""
    lines = [f"- 조합 유일: ({', '.join(g)})" for g in e.unique_together]
    lines += [f"- 정확히 하나만 채움: ({', '.join(g)})" for g in e.exactly_one_of]
    lines += [f"- 조건부 필수: {r.when_column} = {'/'.join(r.when_values)} 이면 {r.column} 필수" for r in e.required_when]
    lines += [f"- 값 조건: {c.column} {c.op} {c.value}" for c in e.checks]
    return lines


# ------------------------------------------------------------------ 추적표
def _trace_md(head, reqs: list[Requirement], funcs: list[FunctionSpec], model: DataModel | None,
              questions: list[Question] | None = None, stage_only: bool = False, changes: dict | None = None) -> str:
    """추적표: 요구사항→기능→엔티티(확정 상태 포함), 비기능→기능, 엔티티→기능, 이전 버전 대비 변경.

    stage_only: 기능 명세 단계 버전 (데이터 설계 전이라 엔티티가 없는 것이 정상이다)
    """
    questions = questions or []
    fn_by_req: dict[str, list[str]] = {}
    for f in funcs:
        for rid in f.requirement_ids:
            fn_by_req.setdefault(rid, []).append(f.function_id)
    ent_by_fn: dict[str, list[str]] = {}
    if model:
        for e in model.entities:
            for fid in e.function_ids:
                ent_by_fn.setdefault(fid, []).append(e.entity_id)
    data_fn = {f.function_id for f in funcs if f.data_objects}
    rows = []
    for r in reqs:
        if not (r.type == "FUNCTIONAL" and r.scope == "SYSTEM"):
            continue
        fns = fn_by_req.get(r.requirement_id, [])
        ents = sorted({e for fid in fns for e in ent_by_fn.get(fid, [])})
        if r.status == "EXCLUDED":
            status = "범위 제외 (사용자 의견)"
        elif not fns:
            status = "❌ 기능 누락"
        elif stage_only and model is None:
            status = "✅ 기능 연결 (데이터 설계 전)"
        elif any(fid in data_fn and not ent_by_fn.get(fid) for fid in fns):
            status = "⚠ 데이터 누락"
        else:
            status = "✅"
        confirmed = ("제외" if r.status == "EXCLUDED" else "확정") if r.status not in REQ_STATUS else \
            f"⚠ {REQ_STATUS[r.status]} (질문: {_related_questions(r.requirement_id, questions)})"
        rows.append([r.requirement_id, r.title, confirmed, "\n".join(fns), "\n".join(ents), status])
    nfr_rows = []
    nfr_fn: dict[str, list[str]] = {}
    for f in funcs:
        for nid in f.nfr_ids:
            nfr_fn.setdefault(nid, []).append(f.function_id)
    for r in reqs:
        if r.type == "NON_FUNCTIONAL" and r.scope == "SYSTEM":
            linked = nfr_fn.get(r.requirement_id, [])
            nfr_rows.append([r.requirement_id, r.category, r.title, "\n".join(linked),
                             "" if linked else "설계 고려사항·검증 계획에서 다룸"])
    unconfirmed = sum(1 for row in rows if row[2].startswith("⚠"))
    if stage_only and model is None:
        summary = (f"시스템 기능 요구사항 {len(rows)}건 중 기능 연결 {sum(1 for row in rows if row[3])}건. "
                   "기능 명세 단계 버전이라 엔티티는 이 버전을 확정한 뒤 데이터 설계(`rerun data_modeler --from 이 버전`)에서 연결한다.")
    else:
        summary = f"시스템 기능 요구사항 {len(rows)}건 중 기능·데이터까지 연결 {sum(1 for row in rows if row[5] == '✅')}건."
    if unconfirmed:
        summary += f" 확정되지 않은 요구사항 {unconfirmed}건 (확정 상태 열, 답에 따라 기능·엔티티가 바뀔 수 있다)."
    out = ["# 추적표", "", head, summary, "",
           "## 1. 기능 요구사항 → 기능 → 엔티티", "",
           table(["요구사항 ID", "요구사항", "확정 상태", "기능", "엔티티", "상태"], rows), "",
           "## 2. 비기능 요구사항 → 기능", "",
           table(["요구사항 ID", "분류", "요구사항", "연결 기능", "비고"], nfr_rows) if nfr_rows else "- 없음", ""]
    if model:
        out += ["## 3. 엔티티 → 기능", "",
                table(["엔티티", "한글명", "관련 기능"], [[e.entity_id, e.label, ", ".join(e.function_ids) or "⚠ 없음"]
                                                  for e in model.entities]), ""]
    if changes:
        out += changes_md(changes)
    return "\n".join(out) + "\n"


def _count_text(counts: list[int] | None) -> str:
    """새 n · 바뀜 n · 지움 n."""
    return " · ".join(f"{label} {n}" for label, n in zip(("새", "바뀜", "지움"), counts)) if counts else "-"


def changes_md(changes: dict, heading: str = "") -> list[str]:
    """이전 버전 대비 변경 절 (추적표 4절, 요구사항 단계 버전, diff 명령): 무엇이 바뀌었고 어디까지 영향을 주는지."""
    c = changes["counts"]
    out = [heading or f"## 4. 이전 버전(v{changes['from']}) 대비 변경", "",
           f"- 요구사항: {_count_text(c['requirements'])}"]
    if changes.get("requirements_only"):   # 요구사항 단계 버전: 영향은 기준 버전의 기능·엔티티 (다음 기능 명세 단계에서 다시 설계될 곳)
        out.append(f"- 기능: 요구사항 단계 버전이라 비교하지 않음. 아래 '영향 기능·엔티티'는 v{changes['from']} 기준으로, "
                   "다음 기능 명세 단계(rerun function_designer)에서 이 기능들의 그룹만 다시 설계한다")
    else:
        out.append(f"- 기능: {_count_text(c['functions'])}")
    if c["entities"] is not None:
        out.append(f"- 엔티티 (v{changes['model_from']} 데이터 모델 대비): {_count_text(c['entities'])}")
    pending = changes.get("data_design")
    if pending:
        out.append(f"- 데이터 설계 영향 (v{pending['model_version']} 데이터 모델 기준, 이 버전에서 데이터 설계를 하면): "
                   f"다시 설계할 엔티티 {len(pending['redesign'])}개, 연결 보완할 새 기능 {len(pending['cover'])}개, "
                   f"지운 기능의 연결을 정리할 엔티티 {len(pending['unlinked'])}개"
                   + (f" — 다시 설계: {', '.join(pending['redesign'])}" if pending["redesign"] else ""))
    out.append("")
    if changes["requirements"]:
        out += ["**바뀐 요구사항과 영향**", "",
                table(["요구사항 ID", "요구사항", "변경", "바뀐 항목", "영향 기능", "영향 엔티티"],
                      [[r["id"], r["title"], r["change"], ", ".join(r["fields"]), "\n".join(r["functions"]),
                        "\n".join(r["entities"])] for r in changes["requirements"]]), ""]
    if changes["functions"]:
        out += ["**기능 변경**", "",
                table(["기능 ID", "기능명", "변경", "바뀐 항목", "근거 요구사항", "연결 엔티티", "같은 요구사항의 새 기능"],
                      [[f["id"], f["name"], f["change"], ", ".join(f["fields"]), ", ".join(f["requirement_ids"]),
                        ", ".join(f["entities"]), ", ".join(f["replaced_by"])] for f in changes["functions"]]), ""]
    if changes["entities"]:
        out += ["**엔티티 변경**", "",
                table(["엔티티", "한글명", "변경", "바뀐 컬럼 (+새 -지움 ~구조 바뀜)", "바뀐 항목"],
                      [[e["id"], e["label"], e["change"], ", ".join(e["columns"]), ", ".join(e["fields"])]
                       for e in changes["entities"]]), ""]
    return out


# ------------------------------------------------------------------ 검토 보고서
def _review_md(head, state: dict, issues: list[Issue], questions: list[Question],
               conv: ConventionReport | None = None) -> str:
    """검토 보고서: 완료 조건, 이슈, 표준 준수, 문서 품질 경고, 질문, 조정 판단 기록, LLM 사용량."""
    completion = state.get("completion") or {}
    out = ["# 검토 보고서", "", head, "## 완료 조건", ""]
    verdict = "충족" if completion.get("satisfied") else "부분 충족 (미해결 항목 보존)"
    out += [f"**판정: {verdict}**" + (" · 단계 한도 도달" if completion.get("forced") else ""), "",
            table(["조건", "결과", "상세"], [[c["name"], "✅" if c["passed"] else "❌", c.get("detail", "")]
                                         for c in completion.get("checks", [])]), ""]
    active = sorted([i for i in issues if i.status != "RESOLVED" and not i.kind.startswith(("DOC_", "SEM_"))],
                    key=lambda i: (SEVERITY_ORDER[i.severity], i.issue_id))
    out += ["## 설계 이슈 (미해결·보고)", "",
            table(["ID", "심각도", "종류", "대상", "내용", "상태", "수정 담당", "시도"],
                  [[i.issue_id, i.severity, i.kind, i.target_id, i.message, i.status, i.target_agent, i.attempts]
                   for i in active]) if active else "- 없음", ""]
    out += _standard_md(conv)
    out += _semantic_md(state, issues)
    # 의미 검토 지적은 다시 검토했을 때 나오지 않으면 RESOLVED가 되지만, 누가 고친 것은 아니므로 해결 목록에 넣지 않는다
    resolved = [i for i in issues if i.status == "RESOLVED" and not i.kind.startswith("SEM_")]
    if resolved:
        out += [f"## 해결된 이슈 ({len(resolved)}건)", "",
                table(["ID", "종류", "대상", "내용", "시도"],
                      [[i.issue_id, i.kind, i.target_id, i.message, i.attempts] for i in resolved]), ""]
    doc = sorted([i for i in issues if i.kind.startswith("DOC_")], key=lambda i: (SEVERITY_ORDER[i.severity], i.issue_id))
    out += ["## 원문 문서 품질 경고 (고객 확인 권장)", "",
            table(["ID", "심각도", "종류", "내용"], [[i.issue_id, i.severity, i.kind, i.message] for i in doc])
            if doc else "- 없음", ""]
    out += ["## 확인 질문·가정", "",
            table(["ID", "단계", "질문", "상태", "답변", "추천 (답이 없으면 이 선택지로 설계)"],
                  [[q.question_id, QUESTION_STAGES.get(q.stage, "요구사항"), q.question, q.status,
                    q.answer, q.recommended] for q in questions])
            if questions else "- 없음", ""]
    out += _design_notes_md(_model_of(state))
    out += ["## 조정 Agent 판단 기록", "",
            table(["단계", "행동", "모드", "범위", "선택 이유 (시작 시 상태)", "결과"],
                  [[d["step"], d["action"], d.get("mode", ""), _short(d.get("scope", [])), d["reason"],
                    d.get("result", "")]
                   for d in state.get("decisions") or []]), ""]
    usage = state.get("llm_usage") or {}
    out += ["## LLM 사용량", "",
            f"- 호출 {usage.get('calls', 0)}회 · 입력 토큰 {usage.get('input_tokens', 0):,} · 출력 토큰 {usage.get('output_tokens', 0):,}"
            + (f" · 캐시 읽기 {usage.get('cache_read_tokens', 0):,} / 쓰기 {usage.get('cache_write_tokens', 0):,} 토큰"
               if usage.get("cache_read_tokens") or usage.get("cache_write_tokens") else ""),
            *([f"- 모델: {model}"] if (model := model_label()) else []),
            ""]
    return "\n".join(out) + "\n"


SEMANTIC_KINDS = {   # 종류 → (보고서 표기, 확인 담당)
    "SEM_FUNC_GAP": ("기능 누락", "기능 설계 담당"),
    "SEM_FUNC_CONFLICT": ("기능·요구사항 불일치", "기능 설계 담당"),
    "SEM_FUNC_UNMARKED_ASSUMPTION": ("표시 없는 가정", "기능 설계 담당 → 고객 확인"),
    "SEM_MODEL_GAP": ("데이터 누락", "데이터 설계 담당"),
    "SEM_MODEL_INTEGRITY": ("제약 누락", "데이터 설계 담당"),
}


def _semantic_md(state: dict, issues: list[Issue]) -> list[str]:
    """의미 검토 절: LLM 지적(내용·근거·제안)과 확인 담당. 자동으로 고치지 않는 보고용이다."""
    head = ["## 의미 검토 (AI 지적 · 담당자 확인 필요)", "",
            "> 의미 검토 Agent(LLM)가 기능 명세와 데이터 모델의 내용을 요구사항·기능 명세에 대조해 검토한 결과다. "
            "자동으로 고치지 않으며, 담당자가 확인해 반영 여부를 정한다.", ""]
    options = state.get("options") or {}
    if options.get("semantic_review") is False:
        if options.get("semantic_review_mode") == "final":
            return head + ["- 이 실행에서는 하지 않음: 'ERD 최종 확정' 때 한 번 검토한다 (SEMANTIC_REVIEW_MODE=final → semantic_review.md)", ""]
        return head + ["- 실행 안 함 (--no-semantic-review 또는 SEMANTIC_REVIEW_MODE=off)", ""]
    meta = state.get("semantic_review") or {}
    if not meta:
        return head + ["- 실행 안 함", ""]
    remarks = sorted([i for i in issues if i.kind.startswith("SEM_") and i.status != "RESOLVED"],
                     key=lambda i: (list(SEMANTIC_KINDS).index(i.kind) if i.kind in SEMANTIC_KINDS else 99, i.issue_id))
    rows = []
    for i in remarks:
        label, owner = SEMANTIC_KINDS.get(i.kind, (i.kind, ""))
        for n, chunk in enumerate(i.message.split("\n\n")):   # 같은 대상의 지적이 합쳐져 있으면 나눠서 보여준다
            what, evidence, suggestion = _split_remark(chunk)
            rows.append([i.issue_id if n == 0 else "", label, i.target_id, what, evidence, suggestion, owner])
    notes = []
    if meta.get("failed"):
        labels = {"functions": "기능 명세", "model": "데이터 모델"}
        notes.append(f"- ⚠ 검토 실패(미검증): {', '.join(labels.get(p, p) for p in meta['failed'])}")
    if meta.get("dropped"):
        notes.append(f"- 대상 ID를 산출물에서 찾을 수 없는 지적 {meta['dropped']}건은 근거를 확인할 수 없어 제외했다.")
    body = table(["ID", "종류", "대상", "내용", "근거", "제안", "확인 담당"], rows) if rows else "- 지적 없음"
    return head + [body, "", *notes, *([""] if notes else [])]


def confirm_tech_stack(project: str, version: str, stack: str) -> dict:
    """최종본의 기술 스택을 사람이 정한 값으로 확정하고 개발 스펙 9개를 다시 쓴다 (LLM 없음, 화면 '개발 스펙 파일'에서 선택).

    같은 최종본 안에서 개발 스펙 문서와 design.json(TQ-001 답·meta.dev_spec)만 바꾼다. 설계 산출물(기능 명세·ERD·DDL·API 설계)은
    그대로다. 정한 값은 버전 기록에도 tech_stack으로 남긴다 (개발 AI에 넘길 때 서버에 명시된 값).
    원문이 스택을 정한 버전, 개발 스펙이 없는 버전이면 ValueError."""
    store = get_store()
    text = store.read(project, version, "design.json")
    design = json.loads(text) if text else {}
    api = design.get("api_design") or {}
    if not api.get("functions"):
        raise ValueError(f"v{version}에는 개발 스펙이 없습니다 ('ERD 최종 확정'으로 만든 최종본에서 정하세요).")
    meta = design.get("meta") or {}
    model = DataModel.model_validate_json(store.read(project, version, "data_model.json") or "{}")
    questions = answer_stack([Question.model_validate(q) for q in design.get("questions") or []], stack)
    result = tech_stack(api, questions)
    if result["basis"] == "원문":
        raise ValueError(f"원문이 기술 스택을 정했습니다 ({result['backend']}). 원문과 다른 스택은 정할 수 없습니다.")
    options = {"standard": (meta.get("standard") or {}).get("profile")} if meta.get("standard") else {}
    profile = profile_of(options)
    physical, conv = apply_profile(model, profile) if profile and profile.renames else (model, None)
    state = {"requirements": design.get("requirements"), "functions": design.get("functions"),
             "questions": [q.model_dump(mode="json") for q in questions], "function_common": design.get("common") or {},
             "api_design": api, "options": options, "document": meta.get("document") or {}}
    files = write_dev_spec(state, lambda name, body: store.write(project, version, name, body), project, version, model,
                           ddl_model(physical, profile), conv, profile, result)
    meta["dev_spec"] = {**(meta.get("dev_spec") or {}), "format": SPEC_FORMAT, "files": list(DEV_SPEC_FILES),
                        "stack": result["backend"], "stack_basis": result["basis"]}
    design.update(meta=meta, questions=state["questions"])
    store.write(project, version, "design.json", json.dumps(design, ensure_ascii=False, separators=(",", ":")))
    update_version(project, version, tech_stack={"stack": result["backend"], "basis": result["basis"]})
    return {**meta["dev_spec"], "written": len(files)}


def export_semantic_review(state: dict, project: str, version: str, source: bool) -> list[str]:
    """ERD 최종 확정 때의 의미 검토 결과(review 명령)를 최종본에 쓴다: semantic_review.md(보고서)·json(화면용).
    산출물 내용은 바뀌지 않으므로 새 버전을 만들지 않고, 다시 확정하면 덮어쓴다."""
    issues = [Issue.model_validate(i) for i in state.get("issues") or []]
    meta = state.get("semantic_review") or {}
    usage = state.get("llm_usage") or {}
    scope = "데이터 모델 ↔ 기능 명세, 기능 명세 ↔ 요구사항" if source else "데이터 모델 ↔ 기능 명세"
    reviewed_at = datetime.now().isoformat(timespec="seconds")
    head = [f"# 의미 검토 (최종 확정) — {project} v{version}", "",
            f"- 검토 시각: {reviewed_at} · 범위: {scope}",
            f"- LLM 호출 {usage.get('calls', 0)}회 · 입력 토큰 {usage.get('input_tokens', 0):,} · "
            f"출력 토큰 {usage.get('output_tokens', 0):,}", ""]
    remarks = []
    for i in issues:
        if not i.kind.startswith("SEM_") or i.status == "RESOLVED":
            continue
        label, owner = SEMANTIC_KINDS.get(i.kind, (i.kind, ""))
        for chunk in i.message.split("\n\n"):
            what, evidence, suggestion = _split_remark(chunk)
            remarks.append({"issue_id": i.issue_id, "kind": i.kind, "label": label, "target": i.target_id,
                            "message": what, "evidence": evidence, "suggestion": suggestion, "owner": owner})
    data = {"project": project, "version": version, "reviewed_at": reviewed_at, "scope": scope,
            "parts": meta.get("parts") or [], "failed": meta.get("failed") or [], "remarks": remarks, "usage": usage}
    store = get_store()
    return [store.write(project, version, "semantic_review.md", "\n".join(head + _semantic_md(state, issues))),
            store.write(project, version, "semantic_review.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")]


def _split_remark(text: str) -> tuple[str, str, str]:
    """'내용 / 근거: … / 제안: …' 세 줄 형식의 지적을 세 칸으로 나눈다."""
    what, evidence, suggestion = [], "", ""
    for line in text.splitlines():
        if line.startswith("근거: "):
            evidence = line[4:]
        elif line.startswith("제안: "):
            suggestion = line[4:]
        else:
            what.append(line)
    return " ".join(what).strip(), evidence, suggestion


DESIGN_TAGS = ("(카탈로그 수정 필요)", "(확인 필요)", "(가정)")


def _design_notes_md(model: DataModel | None) -> list[str]:
    """데이터 설계 설명(description)에 남은 표시를 모은다.

    (카탈로그 수정 필요)·(확인 필요)는 담당자가 처리할 목록으로 모두 보여 주고, (가정)은 개수만 알린다 (많기 때문).
    """
    if model is None:
        return []
    found = {tag: [] for tag in DESIGN_TAGS}
    for e in model.entities:
        for where, text in [(e.entity_id, e.description), *((f"{e.entity_id}.{a.name}", a.description) for a in e.attributes)]:
            for tag in DESIGN_TAGS:
                if tag in (text or ""):
                    found[tag].append([where, text])
    act = found["(카탈로그 수정 필요)"] + found["(확인 필요)"]
    out = []
    if act:
        out += ["### 데이터 설계 확인 사항 (담당자 처리 필요)", "",
                table(["위치", "내용"], act), ""]
    if found["(가정)"]:
        out += [f"데이터 설계에서 명세 근거 없이 정한 판단 {len(found['(가정)'])}건은 `erd.md` 테이블 정의서 설명에 "
                "'(가정)'으로 표시되어 있다 (고객 확인 권장).", ""]
    return out


def _model_of(state: dict) -> DataModel | None:
    """상태의 데이터 모델."""
    return DataModel.model_validate(state["data_model"]) if state.get("data_model") else None


def _short(items: list[str], n: int = 6) -> str:
    """목록을 n개까지만 보여주고 나머지는 '외 N'으로 줄인다."""
    return ", ".join(items[:n]) + (f" 외 {len(items) - n}" if len(items) > n else "")


def export_db_report(result: DbValidationResult, project: str) -> str:
    """DB 구조·검증 보고서를 그 설계 버전에 쓰고 위치를 돌려준다."""
    verdict = "✅ 통과" if result.passed else "❌ 실패"
    lines = ["# DB 구조·검증 보고서", "",
             f"- 설계 버전: v{result.design_version}",
             f"- 검증 엔진: {result.engine}",
             f"- 판정: **{verdict}**",
             f"- 실행 문장: {result.statements}",
             f"- 비고: {result.note}", "",
             "## 검사 항목", "", *[f"- {c}" for c in result.checks], "",
             "## 생성된 테이블", "", ", ".join(result.tables_created) or "-", ""]
    if result.errors:
        lines += ["## 오류", "", *[f"- {e}" for e in result.errors], ""]
    lines += ["> 검사 통과는 스키마 생성과 기본 제약(PK·FK·CHECK) 존재를 뜻하며, 업무 정확성이나 운영 성능을 보장하지 않는다.", ""]
    return get_store().write(project, result.design_version, "db_validation.md", "\n".join(lines))
