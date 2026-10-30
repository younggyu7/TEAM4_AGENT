"""개발 스펙 문서: 설계 산출물을 개발 AI(Claude Code 등)가 읽고 개발할 수 있는 고정 형식 문서로 묶는다 (LLM 없음).

AGENTS.md · PRD.md · ARCHITECTURE.md · DATABASE.md · API.md · openapi.yaml · ACCEPTANCE_CRITERIA.md · TASKS.md · DECISIONS.md
- 형식은 코드가 정한다 (SPEC_FORMAT). 칸 내용만 요구사항·기능 명세·데이터 모델·API 설계·질문에서 가져온다.
  같은 입력이면 항상 같은 문서가 나온다 (생성 시각도 넣지 않는다).
- ID도 코드가 붙인다: AC-<기능 ID>-<n>, AC-<요구사항 ID>-<n>, TASK-001 …, ADR-001 …
- 이름은 schema.sql과 같다 (설계 표준을 쓰면 표준 이름, PostgreSQL DDL은 소문자). 설계 이름 ↔ 표준 이름은 standard_names.csv.
- 데이터 설계까지 한 버전에서만 쓴다 (요구사항·기능 명세 단계 버전에는 데이터 모델·API가 없다).
"""
from __future__ import annotations

import re
from collections.abc import Callable

import yaml

from app.agents.roles.function_designer import SYSTEM_ACTOR
from app.schemas.data_model import DataModel
from app.schemas.function import FunctionSpec
from app.schemas.requirement import Question, Requirement
from app.agents.tools.convention import ConventionReport, Profile

SPEC_FORMAT = "spec-format v1"
FILES = ("AGENTS.md", "PRD.md", "ARCHITECTURE.md", "DATABASE.md", "API.md", "openapi.yaml",
         "ACCEPTANCE_CRITERIA.md", "TASKS.md", "DECISIONS.md")
QUESTION_ID = re.compile(r"(?:Q|FQ|DQ|RC|CF|TQ)-\d+")
STATUS_LABEL = {"CONFIRMED": "확정", "ASSUMED": "가정한 해석 (고객 확인 필요)", "OPEN": "결정 필요 (고객 확인 필요)",
                "EXCLUDED": "제외"}
PRIORITY_LABEL = {"MUST": "필수", "SHOULD": "권장", "COULD": "선택", "UNSPECIFIED": "미정"}
COMMON_ERRORS = {400: ("VALIDATION_FAILED", "요청 형식·필수 값 오류"), 401: ("UNAUTHORIZED", "인증 필요"),
                 403: ("FORBIDDEN", "권한 없음"), 500: ("INTERNAL_ERROR", "서버 오류")}
CHECK_METHOD = {"성능": "부하·응답 시간 측정", "보안": "보안 점검 (접근 통제·암호화·취약점)", "인터페이스": "연계 시험",
                "데이터": "데이터 검증 (이관·정합성)", "품질": "품질 점검", "테스트": "시험 계획에 포함", "제약": "준수 여부 점검",
                "장비구성": "설치·납품 검수"}
OPENAPI_TYPE = {"bigint": ("integer", "int64"), "integer": ("integer", "int32"), "decimal": ("number", None),
                "varchar": ("string", None), "text": ("string", None), "boolean": ("boolean", None),
                "date": ("string", "date"), "timestamp": ("string", "date-time"), "jsonb": ("object", None),
                "uuid": ("string", "uuid")}


def cell(text) -> str:
    """Markdown 표 칸 (| 와 줄바꿈을 바꾼다)."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", "<br>")


def table(headers: list[str], rows: list[list]) -> str:
    """Markdown 표. 행이 없으면 '없음' 한 줄."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows] or ["| " + " | ".join(["없음"] + [""] * (len(headers) - 1)) + " |"]
    return "\n".join(lines)


class Names:
    """설계 이름(data_model.json) → schema.sql 이름. 설계 표준이 없으면 그대로."""

    def __init__(self, conv: ConventionReport | None, profile: Profile | None):
        self.tables: dict[str, str] = {}
        self.columns: dict[tuple[str, str], str] = {}
        for row in (conv.rows if conv else []):
            if row.column:
                self.columns[(row.table, row.column)] = row.physical_column
            elif not row.physical_column:
                self.tables[row.table] = row.physical_table
        self.lower = bool(profile and profile.case == "upper")   # PostgreSQL DDL은 대문자 표준 이름을 소문자로 쓴다

    def table(self, name: str) -> str:
        value = self.tables.get(name, name)
        return value.lower() if self.lower else value

    def column(self, table: str, column: str) -> str:
        value = self.columns.get((table, column), column)
        return value.lower() if self.lower else value

    def ref(self, column_ref: str) -> str:
        """'테이블.컬럼' 설계 이름 → schema.sql 이름."""
        table, _, column = column_ref.partition(".")
        return f"{self.table(table)}.{self.column(table, column)}" if column else self.table(table)


class Spec:
    """문서를 쓰는 데 필요한 산출물을 한곳에 모은다."""

    def __init__(self, state: dict, project: str, version: str, model: DataModel, ddl: DataModel, names: Names,
                 stack: dict, profile: Profile | None):
        self.state, self.version, self.model, self.ddl, self.names, self.stack = state, version, model, ddl, names, stack
        self.profile, self.project = profile, project
        self.options = state.get("options") or {}
        self.requirements = [Requirement.model_validate(r) for r in state.get("requirements") or []]
        self.by_req = {r.requirement_id: r for r in self.requirements}
        self.functions = [FunctionSpec.model_validate(f) for f in state.get("functions") or []]
        self.questions = [Question.model_validate(q) for q in state.get("questions") or []]
        self.by_question = {q.question_id: q for q in self.questions}
        self.api = (state.get("api_design") or {}).get("functions") or {}
        self.arch = (state.get("api_design") or {}).get("architecture")
        self.common = state.get("function_common") or {}
        self.columns = {(e.entity_id, a.name): a for e in model.entities for a in e.attributes}
        self.linked = {f.function_id: [e.entity_id for e in model.entities if f.function_id in e.function_ids]
                       for f in self.functions}
        self.acceptance = {f.function_id: _acceptance(f, self.by_req, self.by_question) for f in self.functions}

    def head(self, title: str, purpose: str) -> str:
        return (f"# {title}\n\n> 프로젝트: **{self.project}** · 설계 버전: **v{self.version}** · {SPEC_FORMAT}  \n"
                f"> {purpose} 읽는 순서와 작업 규칙은 `AGENTS.md`.")

    def system_functional(self) -> list[Requirement]:
        return [r for r in self.requirements if r.designed]

    def system_nfrs(self) -> list[Requirement]:
        return [r for r in self.requirements if r.type == "NON_FUNCTIONAL" and r.scope == "SYSTEM" and r.status != "EXCLUDED"]

    def operations(self) -> list[tuple[FunctionSpec, dict]]:
        """만들 API (다른 기능의 API를 함께 쓰는 것은 빼고 한 번만)."""
        return [(f, op) for f in self.functions for op in (self.api.get(f.function_id) or {}).get("operations") or []
                if not op.get("shared_with")]

    def sharers(self) -> dict[str, list[str]]:
        """operation_id → 그 API를 함께 쓰는 다른 기능 ID (같은 테이블을 다뤄 API 설계가 하나로 합친 것)."""
        out: dict[str, list[str]] = {}
        for f in self.functions:
            for op in (self.api.get(f.function_id) or {}).get("operations") or []:
                if op.get("shared_with"):
                    out.setdefault(op["shared_with"], []).append(f.function_id)
        return out

    def stack_text(self) -> str:
        s = self.stack
        if not s["backend"]:
            return "정해지지 않음 (아키텍처 요약 없음)"
        if s["basis"] == "원문":
            return f"{s['backend']} (원문)"
        if s["basis"] == "확정":
            return f"{s['backend']} (확정 {s['question']})"
        return f"{s['backend']} (가정 — 추천안, 확인 필요 {s['question']})" if s["question"] else f"{s['backend']} (가정)"


def write_dev_spec(state: dict, write: Callable[[str, str], str], project: str, version: str, model: DataModel,
                   ddl: DataModel, conv: ConventionReport | None, profile: Profile | None, stack: dict) -> list[str]:
    """개발 스펙 문서 9개를 쓴다 (write(파일 이름, 내용) → 저장 위치, 산출물 저장소). ddl은 schema.sql을 만든 모델(표준 이름·소문자)이다."""
    spec = Spec(state, project, version, model, ddl, Names(conv, profile), stack, profile)
    texts = {
        "AGENTS.md": _agents_md(spec), "PRD.md": _prd_md(spec), "ARCHITECTURE.md": _architecture_md(spec),
        "DATABASE.md": _database_md(spec), "API.md": _api_md(spec), "openapi.yaml": _openapi_yaml(spec),
        "ACCEPTANCE_CRITERIA.md": _acceptance_md(spec), "TASKS.md": _tasks_md(spec), "DECISIONS.md": _decisions_md(spec),
    }
    return [write(name, texts[name]) for name in FILES]


# ------------------------------------------------------------------ 업무 규칙 표시
def rule_tag(rule: str) -> tuple[str, str, list[str]]:
    """업무 규칙 앞 표시 → (종류, 표시를 뗀 문장, 질문 ID). 종류: 확정·가정·보류·공통."""
    m = re.match(r"^\(([^)]*)\)\s*", rule)
    head = m.group(1) if m else ""
    text = rule[m.end():] if m else rule
    qids = QUESTION_ID.findall(head)
    if "확인 필요" in head or (not head and "(확인 필요" in rule):
        return "보류", text, qids
    if "가정" in head:
        return "가정", text, qids
    if head.startswith("공통"):
        return "공통", text, qids
    return "확정", text, qids


def _open_questions(f: FunctionSpec, questions: dict[str, Question]) -> list[str]:
    """기능 규칙에 걸린, 아직 답이 없는 확인 질문 ID."""
    out = []
    for rule in f.business_rules:
        kind, _, qids = rule_tag(rule)
        if kind == "보류":
            out += [q for q in qids if not (q in questions and questions[q].status == "ANSWERED")] or ([] if qids else ["미정"])
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ 수용 기준
def _acceptance(f: FunctionSpec, by_req: dict[str, Requirement], questions: dict[str, Question]) -> list[dict]:
    """기능의 수용 기준: 정상 흐름 1개 + 예외 흐름마다 1개 + 업무 규칙마다 1개 + 근거 요구사항의 수용 기준마다 1개."""
    given = "; ".join(f.preconditions) or "-"
    rows = [{"kind": "정상", "given": given, "when": " → ".join(f.normal_flow) or f.name,
             "then": ("출력: " + ", ".join(o.name for o in f.outputs)) if f.outputs else "정상 흐름이 끝까지 진행된다",
             "basis": "정상 흐름"}]
    rows += [{"kind": "예외", "given": given, "when": x.condition, "then": x.handling, "basis": "예외 흐름"}
             for x in f.exception_flows]
    for rule in f.business_rules:
        kind, text, qids = rule_tag(rule)
        label = {"확정": "규칙", "가정": "규칙 (가정)", "공통": "규칙 (공통)"}.get(kind) or \
            f"규칙 (보류 {', '.join(q for q in qids if not (q in questions and questions[q].status == 'ANSWERED')) or '확인 필요'})"
        rows.append({"kind": label, "given": "-", "when": f"{f.name} 수행", "then": text, "basis": rule.split(")")[0] + ")"
                     if rule.startswith("(") else "업무 규칙"})
    for rid in f.requirement_ids:
        for c in (by_req[rid].acceptance_criteria if rid in by_req else []):
            rows.append({"kind": "검수", "given": "-", "when": "-", "then": c, "basis": rid})
    for n, row in enumerate(rows, start=1):
        row["id"] = f"AC-{f.function_id}-{n}"
    return rows


def _acceptance_md(spec: Spec) -> str:
    out = [spec.head("수용 기준 (ACCEPTANCE_CRITERIA)", "작업이 끝났는지 판정하는 기준이다. 기준 하나가 테스트 하나다."), ""]
    out += ["## 1. 테스트 방침", "",
            "- 작업 완료 = 그 작업의 수용 기준(AC) 테스트가 모두 통과. 테스트 이름에 AC ID를 넣는다 (`test_AC_FN_SFR_001_01_1`).",
            "- 종류: 정상·예외 → API 테스트, 규칙 → 업무 규칙 단위 테스트, 검수 → 요구사항 수용 기준 확인, "
            "DB 제약 → `DATABASE.md` 4절 제약이 실제로 막는지 테스트, 비기능 → 3절 확인 방법.",
            "- '규칙 (가정)'은 설정값으로 구현하고 설정값 기준으로 테스트한다. '규칙 (보류 …)'은 고객 답이 올 때까지 테스트하지 않는다.",
            ""]
    tests = [r for r in spec.requirements if r.category == "테스트" and r.status != "EXCLUDED"]
    if tests:
        out += ["시험 관련 요구사항:", "", table(["요구사항", "내용"], [[r.requirement_id, f"{r.title} — {r.description}"] for r in tests]), ""]
    out += ["## 2. 기능별 수용 기준", ""]
    for f in spec.functions:
        out += [f"### {f.function_id} {f.name}", "", f"- 근거 요구사항: {', '.join(f.requirement_ids) or '없음'}", "",
                table(["AC ID", "종류", "조건 (Given)", "동작 (When)", "결과 (Then)", "근거"],
                      [[r["id"], r["kind"], r["given"], r["when"], r["then"], r["basis"]]
                       for r in spec.acceptance[f.function_id]]), ""]
    out += ["## 3. 비기능 수용 기준", ""]
    rows = []
    for r in spec.system_nfrs():
        criteria = r.acceptance_criteria or [r.description]
        rows += [[f"AC-{r.requirement_id}-{n}", r.requirement_id, r.category, c, CHECK_METHOD.get(r.category, "점검")]
                 for n, c in enumerate(criteria, start=1)]
    out += [table(["AC ID", "요구사항", "분류", "기준", "확인 방법"], rows), ""]
    return "\n".join(out)


# ------------------------------------------------------------------ AGENTS.md
def _agents_md(spec: Spec) -> str:
    standard = (f"설계 표준 `{spec.profile.name}` (설계 이름 ↔ 표준 이름은 `standard_names.csv`)" if spec.profile and spec.profile.renames
                else "설계 이름 그대로 (영문 snake_case)")
    ops = spec.operations()
    out = [spec.head("AGENTS.md — 개발 AI 작업 규칙", "개발 AI가 맨 먼저 읽는 문서다."), "",
           "## 1. 이 스펙으로 만들 것", "",
           f"- 사업: {spec.options.get('title') or spec.project}",
           f"- 개발 대상: 기능 {len(spec.functions)}개 · 테이블 {len(spec.ddl.entities)}개 · API {len(ops)}개",
           f"- 기술 스택: {spec.stack_text()} — `ARCHITECTURE.md` 2절", "",
           "## 2. 읽는 순서", "",
           table(["순서", "문서", "내용"], [
               ["1", "PRD.md", "무엇을 왜 만드는지, 범위, 비기능 요구사항, 미확정 사항"],
               ["2", "ARCHITECTURE.md", "기술 스택, 모듈, 외부 연계, 보안, 성능, 배포 제약"],
               ["3", "DATABASE.md + schema.sql", "테이블·관계·업무 제약 (schema.sql이 기준)"],
               ["4", "API.md + openapi.yaml", "기능별 API (openapi.yaml이 기준)"],
               ["5", "ACCEPTANCE_CRITERIA.md", "완료 판정 기준 (AC ID = 테스트 이름)"],
               ["6", "TASKS.md", "작업 순서와 작업별 완료 기준"],
               ["7", "DECISIONS.md", "결정의 이유와 아직 가정인 것"]]), "",
           "## 3. 지킬 원칙", "",
           "1. 스펙에 없는 기능·테이블·컬럼·API를 만들지 않는다. 필요해 보이면 만들지 말고 `TODO(질문)`으로 남겨 작업 보고에 적는다.",
           f"2. DB는 `schema.sql`을 그대로 만든다. 테이블·컬럼 이름을 바꾸지 않는다. 이름: {standard}.",
           "3. API는 `openapi.yaml`과 같게 만든다 (경로·메서드·필드 이름·타입·오류 코드). 필드의 `x-db-column`이 저장·조회할 컬럼이다.",
           "4. 업무 규칙 앞 표시에 따라 구현한다.",
           "",
           table(["표시", "뜻", "구현"], [
               ["표시 없음 · (결정 …)", "확정", "그대로 구현"],
               ["(가정)", "근거 없이 정한 값", "구현하되 값은 설정으로 빼고 주석에 '가정'"],
               ["(확인 필요 …)", "고객 답 대기", "구현하지 않고 `TODO(질문 ID)`. 그 규칙의 테스트도 보류"],
               ["(공통 P…)", "여러 기능이 같이 쓰는 규칙", "공통 모듈 한 곳에 구현하고 기능에서 불러 쓴다"]]),
           "",
           "5. 보안: `*_hash` 컬럼은 단방향 해시(비밀번호는 비밀번호 전용 해시), `*_enc` 컬럼은 암호화해 저장한다. "
           "평문 비밀값을 로그·응답에 남기지 않는다.",
           "6. 스펙 문서는 고치지 않는다. 스펙과 코드가 다르면 코드를 스펙에 맞추고, 스펙이 틀렸다고 보이면 질문으로 남긴다.", "",
           "## 4. 작업 방식", "",
           "1. `TASKS.md` 순서대로 한 작업씩 한다. 선행 작업이 끝나야 시작한다.",
           "2. 작업의 완료 기준(AC) 테스트를 먼저 쓰고 구현한다.",
           "3. 완료 = 그 작업의 AC 테스트와 기존 테스트가 모두 통과.",
           "4. 커밋 메시지와 테스트 이름에 작업·기능 ID를 붙인다 (예: `TASK-007 FN-SFR-001-01 서비스 요청 등록`).", "",
           "## 5. ID 규칙", "",
           table(["ID", "뜻"], [
               ["원문 번호 기반 (예: SFR-001-01)", "요구사항 (`PRD.md`)"], ["FN-…", "기능 (기능 명세 `functions.md`)"],
               ["AC-…", "수용 기준 (`ACCEPTANCE_CRITERIA.md`)"], ["TASK-…", "작업 (`TASKS.md`)"],
               ["ADR-…", "결정 기록 (`DECISIONS.md`)"],
               ["Q-·RC-·CF-·FQ-·DQ-·TQ-", "고객 확인 질문 (요구사항·요구사항 검토·충돌·기능 명세·데이터 설계·개발 스펙)"]]), ""]
    return "\n".join(out)


# ------------------------------------------------------------------ PRD.md
def _prd_md(spec: Spec) -> str:
    reqs = spec.requirements
    functional = spec.system_functional()
    nfrs = spec.system_nfrs()
    project_duty = [r for r in reqs if r.scope == "PROJECT" and r.status != "EXCLUDED"]
    excluded = [r for r in reqs if r.status == "EXCLUDED"]
    doc = (spec.state.get("document") or {}).get("file_name", "")
    by_req_fn: dict[str, list[str]] = {}
    for f in spec.functions:
        for rid in f.requirement_ids:
            by_req_fn.setdefault(rid, []).append(f.function_id)
    out = [spec.head("PRD — 제품 요구사항", "무엇을 왜 만드는지 정한 문서다. 모든 항목에 요구사항 ID를 붙인다."), "",
           "## 1. 사업 개요", "",
           f"- 사업명: {spec.options.get('title') or spec.project}",
           f"- 원문: {doc}",
           f"- 요구사항 {len(reqs)}건: 시스템 기능 {len(functional)} · 시스템 비기능 {len(nfrs)} · 사업 수행 의무 {len(project_duty)}"
           f" · 범위 제외 {len(excluded)}",
           f"- 개발 대상: 시스템 기능 요구사항 {len(functional)}건 → 기능 {len(spec.functions)}개 (`functions.md`)", ""]
    actors: dict[str, list[str]] = {}
    for f in spec.functions:
        for a in f.actors or ["(행위자 없음)"]:
            actors.setdefault(a, []).append(f.function_id)
    out += ["## 2. 사용자 (행위자)", "",
            table(["사용자", "기능 수", "기능"], [[a, len(fids), ", ".join(fids)] for a, fids in sorted(actors.items())]), ""]
    out += ["## 3. 기능 범위", "", "### 3-1. 개발 대상 (시스템 기능 요구사항)", "",
            table(["요구사항", "원문 번호", "요구사항 명", "우선순위", "상태", "기능"],
                  [[r.requirement_id, r.source_ref, r.title, PRIORITY_LABEL.get(r.priority, r.priority),
                    STATUS_LABEL.get(r.status, r.status),
                    ", ".join(by_req_fn.get(r.requirement_id, []))] for r in functional]), "",
            "### 3-2. 범위에서 뺀 요구사항", "",
            table(["요구사항", "요구사항 명", "이유"],
                  [[r.requirement_id, r.title, "; ".join(r.decisions) or "-"] for r in excluded]), "",
            "### 3-3. 개발 대상이 아닌 요구사항 (사업 수행 의무: 인력·교육·보고·산출물 등)", "",
            table(["요구사항", "분류", "요구사항 명"], [[r.requirement_id, r.category, r.title] for r in project_duty]), ""]
    out += ["## 4. 비기능 요구사항", "",
            table(["요구사항", "분류", "요구사항 명", "내용", "수용 기준"],
                  [[r.requirement_id, r.category, r.title, r.description, "; ".join(r.acceptance_criteria) or "-"] for r in nfrs]), ""]
    rows = [[r.requirement_id, r.title, STATUS_LABEL[r.status]] for r in reqs if r.status in ("OPEN", "ASSUMED")]
    rows += [[q.question_id, q.question, (f"답 대기 (추천: {q.recommended})" if q.recommended else "답 대기")]
             for q in spec.questions if q.status != "ANSWERED"]
    out += ["## 5. 미확정 사항", "", table(["대상", "내용", "상태"], rows), ""]
    return "\n".join(out)


# ------------------------------------------------------------------ ARCHITECTURE.md
def _items(rows: list[dict]) -> list[list]:
    return [[x.get("name", ""), x.get("detail", ""), ", ".join(x.get("requirement_ids") or [])] for x in rows]


def _architecture_md(spec: Spec) -> str:
    arch = spec.arch or {}
    out = [spec.head("ARCHITECTURE — 시스템 구성", "기술 스택과 구성, 비기능 요구사항을 어떻게 지킬지 정리한 문서다."), ""]
    out += ["## 1. 구성 요약", "", arch.get("summary") or "(아키텍처 요약 없음 — API 설계 Agent 요청 실패. 4~7절은 비기능 요구사항 원문 목록)", ""]
    rows = [[layer, value, ", ".join(ids) or "원문"] for layer, value, ids in spec.stack["source"]]
    if spec.stack["basis"] != "원문":
        rows.insert(0, ["백엔드 (언어·프레임워크·DB)", spec.stack_text(), spec.stack["question"] or "-"])
    out += ["## 2. 기술 스택", "", table(["구분", "내용", "근거"], rows), "",
            "- DDL(`schema.sql`)은 PostgreSQL 16 기준으로 검증했다. 원문이 다른 DBMS를 정했으면 DDL을 그 DBMS 문법으로 옮긴다.", ""]
    groups: dict[str, list[FunctionSpec]] = {}
    for f in spec.functions:
        groups.setdefault(f.group, []).append(f)
    out += ["## 3. 모듈 (기능 영역)", "",
            table(["모듈", "기능", "요구사항"],
                  [[g, ", ".join(f"{f.function_id} {f.name}" for f in fs),
                    ", ".join(dict.fromkeys(r for f in fs for r in f.requirement_ids))] for g, fs in groups.items()]), ""]
    fallback = {cat: [{"name": r.title, "detail": r.description, "requirement_ids": [r.requirement_id]}
                      for r in spec.system_nfrs() if r.category in cats] for cat, cats in
                (("integrations", ("인터페이스",)), ("security", ("보안",)), ("performance", ("성능", "품질")),
                 ("constraints", ("제약", "장비구성", "데이터")))}
    for n, (key, title) in enumerate((("integrations", "외부 연계"), ("security", "보안"), ("performance", "성능·가용성"),
                                     ("constraints", "배포·운영 제약")), start=4):
        rows = _items(arch.get(key) if spec.arch else fallback[key])
        out += [f"## {n}. {title}", "", table(["항목", "내용", "근거 요구사항"], rows), ""]
        if key == "security":
            secured = [[spec.names.ref(f"{e.entity_id}.{a.name}"), "단방향 해시" if a.name.endswith("_hash") else "암호화"]
                       for e in spec.model.entities for a in e.attributes if a.name.endswith(("_hash", "_enc"))]
            out += ["보안 컬럼 (DB에 평문으로 두지 않는 값):", "", table(["테이블.컬럼", "저장 방식"], secured), ""]
    return "\n".join(out)


# ------------------------------------------------------------------ DATABASE.md
def _database_md(spec: Spec) -> str:
    model, names = spec.model, spec.names
    out = [spec.head("DATABASE — 데이터베이스", "테이블·관계·업무 제약을 정리한 문서다. 기준은 `schema.sql`이다."), "",
           "## 1. 원칙", "",
           "- `schema.sql`을 그대로 실행해 만든다 (PostgreSQL 16 문법. DB 검증을 했으면 결과는 `db_validation.md`).",
           "- 컬럼 상세(타입·길이·필수·설명)는 `erd.md` 테이블 정의서, 관계도는 `erd.mmd`.",
           "- 기본키는 테이블마다 하나(대리키), 감사 컬럼(등록·수정 시각)은 시스템이 채운다.", ""]
    used = {e.entity_id: ", ".join(e.function_ids) for e in model.entities}
    out += ["## 2. 테이블", "",
            table(["테이블", "이름", "종류", "한 행의 의미", "사용 기능"],
                  [[names.table(e.entity_id), e.label, e.kind, e.description, used.get(e.entity_id, "")] for e in model.entities]), ""]
    rels = [[f"{names.table(r.child)}.{names.column(r.child, r.fk_attribute)}", names.table(r.parent), r.cardinality,
             "선택" if r.optional else "필수"] for r in model.relations]
    out += ["## 3. 관계", "", table(["자식 테이블.외래키", "부모 테이블", "관계", "참여"], rels), ""]
    rows = []
    for e in model.entities:
        t = names.table(e.entity_id)
        col = lambda c, t0=e.entity_id: names.column(t0, c)   # noqa: E731
        rows += [[t, "조합 유일", ", ".join(col(c) for c in g)] for g in e.unique_together]
        rows += [[t, "정확히 하나만", ", ".join(col(c) for c in g)] for g in e.exactly_one_of]
        rows += [[t, "조건부 필수", f"{col(r.when_column)}이 {', '.join(r.when_values)}이면 {col(r.column)} 필수"]
                 for r in e.required_when]
        rows += [[t, "값 조건", f"{col(c.column)} {c.op} {col(c.value) if (e.entity_id, c.value) in spec.columns else c.value}"]
                 for c in e.checks]
        rows += [[t, "허용값", f"{col(a.name)}: {', '.join(a.allowed_values)}"] for a in e.attributes if a.allowed_values]
        rows += [[t, "유일", col(a.name)] for a in e.attributes if a.unique and not a.primary_key]
    out += ["## 4. 업무 제약 (DDL에 들어간 것)", "", table(["테이블", "제약", "내용"], rows), ""]
    secured = [[names.ref(f"{e.entity_id}.{a.name}"), "단방향 해시" if a.name.endswith("_hash") else "암호화", a.label]
               for e in model.entities for a in e.attributes if a.name.endswith(("_hash", "_enc"))]
    out += ["## 5. 보안 컬럼", "", table(["테이블.컬럼", "저장 방식", "이름"], secured), ""]
    if spec.profile and spec.profile.renames:
        out += ["## 6. 설계 표준", "", f"- 표준: `{spec.profile.name}`. 설계 이름(`data_model.json`) ↔ 표준 이름은 `standard_names.csv`.", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ API.md · openapi.yaml
def _field_info(spec: Spec, fld: dict) -> dict:
    """필드의 타입·형식·길이·허용값. 컬럼에 연결했으면 컬럼에서, 아니면 설계한 타입."""
    attr = spec.columns.get(tuple(fld["column"].split(".", 1))) if fld["column"] else None
    if attr is None:
        return {"type": fld["type"], "format": None, "max": None, "enum": [], "db": "", "label": ""}
    kind, fmt = OPENAPI_TYPE.get(attr.data_type, ("string", None))
    return {"type": kind, "format": fmt, "max": attr.length if attr.data_type == "varchar" and attr.length else None,
            "enum": list(attr.allowed_values), "db": spec.names.ref(fld["column"]), "label": attr.label}


def _type_text(info: dict) -> str:
    text = info["type"] + (f"({info['max']})" if info["max"] else "") + (f" {info['format']}" if info["format"] else "")
    return text + (f" [{'|'.join(info['enum'])}]" if info["enum"] else "")


def _field_rows(spec: Spec, fields: list[dict], with_required: bool) -> list[list]:
    """API.md 필드 표의 행: 필드, 타입, (필수), DB 컬럼, 설명."""
    rows = []
    for x in fields:
        info = _field_info(spec, x)
        row = [x["name"], _type_text(info)] + (["예" if x["required"] else "아니오"] if with_required else [])
        rows.append(row + [info["db"] or "-", x["description"] or info["label"]])
    return rows


def _path_params(path: str) -> list[str]:
    return re.findall(r"\{([A-Za-z][A-Za-z0-9]*)\}", path)


def _api_md(spec: Spec) -> str:
    ops = spec.operations()
    sharers = spec.sharers()
    out = [spec.head("API — 기능별 API", "기능을 구현할 API다. 기계가 읽는 정의는 같은 내용의 `openapi.yaml`이다."), "",
           "## 1. 공통 규칙", "",
           "- 기본 경로 `/api/v1` · JSON(UTF-8) · 인증 `Authorization: Bearer <토큰>` (`ARCHITECTURE.md` 보안)",
           "- 목록 조회: 쿼리 `page`(0부터)·`size`(기본 20), 응답 `{items, page, size, total}`",
           "- 오류 응답 `{code, message}`. 모든 API 공통 오류: "
           + ", ".join(f"{s} `{c}` ({d})" for s, (c, d) in COMMON_ERRORS.items()),
           "- 필드의 DB 컬럼은 `schema.sql` 이름이다. 타입·길이·허용값은 그 컬럼과 같다.", "",
           "## 2. API 목록", "",
           table(["메서드", "경로", "operationId", "기능", "권한"],
                 [[op["method"], op["path"], op["operation_id"],
                   f"{f.function_id} {f.name}" + (f" (함께 씀: {', '.join(sharers[op['operation_id']])})"
                                                  if op["operation_id"] in sharers else ""), ", ".join(op["actors"])]
                  for f, op in ops]), "",
           "## 3. 기능별 API", ""]
    for f in spec.functions:
        entry = spec.api.get(f.function_id) or {}
        if not entry.get("operations"):
            continue
        out += [f"### {f.function_id} {f.name}", "",
                f"- 요구사항: {', '.join(f.requirement_ids) or '없음'} · 테이블: "
                f"{', '.join(spec.names.table(t) for t in spec.linked[f.function_id]) or '없음'}", ""]
        for op in entry["operations"]:
            if op.get("shared_with"):   # 같은 테이블을 다루는 다른 기능의 API를 함께 쓴다 (한 번만 만든다)
                out += [f"#### {op['method']} {op['path']} — {op['summary']} (`{op['shared_with']}`를 함께 씀)", "",
                        "- 요청·응답·오류는 그 API(먼저 나온 기능의 절)와 같다. 따로 만들지 않는다.", ""]
                continue
            out += [f"#### {op['method']} {op['path']} — {op['summary']} (`{op['operation_id']}`)", "",
                    f"- 권한: {', '.join(op['actors']) or '로그인 사용자'}"
                    + (f" · 경로 변수: {', '.join(_path_params(op['path']))}" if _path_params(op['path']) else "")
                    + (" · 목록 (페이지)" if op["list_response"] else ""), ""]
            where = "쿼리" if op["method"] in ("GET", "DELETE") else "본문"
            out += [f"요청 ({where}):", "", table(["필드", "타입", "필수", "DB 컬럼", "설명"], _field_rows(spec, op["request"], True)), ""]
            out += ["응답" + (" (items 한 건)" if op["list_response"] else "") + ":", "",
                    table(["필드", "타입", "DB 컬럼", "설명"], _field_rows(spec, op["response"], False)), ""]
            if op["errors"]:
                out += ["오류:", "", table(["상태", "코드", "조건"], [[e["status"], e["code"], e["condition"]] for e in op["errors"]]), ""]
            else:
                out += ["오류: 공통 오류만 (1절)", ""]
    batch = [[f.function_id, f.name, (spec.api.get(f.function_id) or {}).get("no_api_reason", "")]
             for f in spec.functions if not (spec.api.get(f.function_id) or {}).get("operations")]
    out += ["## 4. API가 없는 기능 (배치·자동 실행·내부 처리)", "", table(["기능", "기능명", "이유"], batch), ""]
    return "\n".join(out)


def _schema(spec: Spec, fields: list[dict]) -> dict:
    """필드 목록 → OpenAPI 객체 스키마."""
    props = {}
    for x in fields:
        info = _field_info(spec, x)
        prop: dict = {"type": info["type"]}
        if info["format"]:
            prop["format"] = info["format"]
        if info["max"]:
            prop["maxLength"] = info["max"]
        if info["enum"]:
            prop["enum"] = info["enum"]
        if x["description"] or info["label"]:
            prop["description"] = x["description"] or info["label"]
        if info["db"]:
            prop["x-db-column"] = info["db"]
        props[x["name"]] = prop
    schema: dict = {"type": "object", "properties": props}
    required = [x["name"] for x in fields if x["required"]]
    if required:
        schema["required"] = required
    return schema


def _openapi_yaml(spec: Spec) -> str:
    paths: dict = {}
    sharers = spec.sharers()
    for f, op in spec.operations():
        params = [{"name": p, "in": "path", "required": True,
                   "schema": {"type": "integer", "format": "int64"} if p.endswith("Id") else {"type": "string"}}
                  for p in _path_params(op["path"])]
        item: dict = {"operationId": op["operation_id"], "summary": op["summary"], "tags": [f.group],
                      "x-function-id": f.function_id, "x-requirement-ids": f.requirement_ids, "x-actors": op["actors"]}
        if op["operation_id"] in sharers:
            item["x-shared-function-ids"] = sharers[op["operation_id"]]
        if op["method"] in ("GET", "DELETE"):
            for x in op["request"]:
                params.append({"name": x["name"], "in": "query", "required": x["required"],
                               "schema": _schema(spec, [x])["properties"][x["name"]]})
            if op["list_response"]:
                params += [{"name": "page", "in": "query", "required": False, "schema": {"type": "integer", "default": 0}},
                           {"name": "size", "in": "query", "required": False, "schema": {"type": "integer", "default": 20}}]
        elif op["request"]:
            item["requestBody"] = {"required": True, "content": {"application/json": {"schema": _schema(spec, op["request"])}}}
        if params:
            item["parameters"] = params
        body = _schema(spec, op["response"])
        if op["list_response"]:
            body = {"type": "object", "required": ["items", "page", "size", "total"],
                    "properties": {"items": {"type": "array", "items": body}, "page": {"type": "integer"},
                                   "size": {"type": "integer"}, "total": {"type": "integer"}}}
        ok = "204" if op["method"] == "DELETE" and not op["response"] else "200"
        responses: dict = {ok: {"description": "성공"} if ok == "204" else
                           {"description": "성공", "content": {"application/json": {"schema": body}}}}
        errors: dict[int, list[str]] = {}
        for e in op["errors"]:
            errors.setdefault(e["status"], []).append(f"{e['code']}: {e['condition']}")
        for status, (code, desc) in COMMON_ERRORS.items():
            errors.setdefault(status, []).append(f"{code}: {desc}")
        for status in sorted(errors):
            responses[str(status)] = {"description": "; ".join(errors[status]),
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
        item["responses"] = responses
        paths.setdefault(op["path"], {})[op["method"].lower()] = item
    doc = {
        "openapi": "3.1.0",
        "info": {"title": str(spec.options.get("title") or spec.project), "version": f"v{spec.version}",
                 "description": f"{SPEC_FORMAT}. 요구사항 분석·설계 Agent가 만든 API 정의 (API.md와 같은 내용)."},
        "servers": [{"url": "/api/v1"}],
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {"Error": {"type": "object", "required": ["code", "message"],
                                  "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}},
        },
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=1000)


# ------------------------------------------------------------------ TASKS.md
def _ranks(model: DataModel) -> dict[str, int]:
    """외래키 깊이: 부모가 없는 테이블 0, 부모가 있으면 부모 깊이 + 1 (부모 테이블을 쓰는 작업을 먼저 한다)."""
    by_name = {e.entity_id: e for e in model.entities}
    memo: dict[str, int] = {}

    def rank(name: str, seen: frozenset) -> int:
        if name in memo:
            return memo[name]
        parents = {a.references for a in by_name[name].attributes if a.references in by_name and a.references != name}
        value = 0 if not parents else 1 + max((rank(p, seen | {name}) for p in parents if p not in seen), default=0)
        memo[name] = value
        return value

    return {name: rank(name, frozenset()) for name in by_name}


def _phase(f: FunctionSpec, by_req: dict[str, Requirement]) -> str:
    """1차(MVP): 근거 요구사항에 MUST(또는 우선순위 근거 없음)가 하나라도 있으면, 2차: 모두 SHOULD·COULD."""
    priorities = {by_req[r].priority for r in f.requirement_ids if r in by_req}
    return "1차" if not priorities or priorities & {"MUST", "UNSPECIFIED"} else "2차"


def _tasks(spec: Spec) -> list[dict]:
    """작업 목록: 뼈대·DB·인증·공통 규칙 다음에 기능마다 한 작업 (1차 먼저, 부모 테이블을 쓰는 기능 먼저)."""
    ranks = _ranks(spec.model)
    by_name = {e.entity_id: e for e in spec.model.entities}
    policies = spec.common.get("policies") or []
    code_sets = spec.common.get("code_sets") or []
    nfr_security = [r.requirement_id for r in spec.system_nfrs() if r.category == "보안"]
    roles = spec.common.get("roles") or []
    human = sorted({a for f in spec.functions for a in f.actors if not SYSTEM_ACTOR.match(a)})
    systems = sorted({a for f in spec.functions for a in f.actors if SYSTEM_ACTOR.match(a)})
    other = [a for a in human if a not in {r["name"] for r in roles}] if roles else []
    access = [("사용자 역할: " + ", ".join(f"{r['name']}({r['scope']})" if r.get("scope") else r["name"] for r in roles))
              if roles else f"사용자(행위자) 권한: {', '.join(human) or '없음'}"]
    access += [f"역할 목록에 없는 행위자 (가까운 역할에 맞추고 작업 보고에 적는다): {', '.join(other)}"] if other else []
    access += [f"자동 실행·연계 주체 (사용자 역할 아님 — 시스템 계정·연계 인증): {', '.join(systems)}"] if systems else []
    tasks = [
        {"id": "TASK-001", "name": "프로젝트 뼈대", "phase": "1차", "after": [], "status": "진행 가능",
         "make": [f"기술 스택 {spec.stack_text()}으로 프로젝트 생성", "공통 오류 응답 `{code, message}`·설정 파일·테스트 실행 환경"],
         "done": ["빌드와 빈 테스트 실행 성공"], "refs": "ARCHITECTURE.md 2절, API.md 1절"},
        {"id": "TASK-002", "name": "DB 생성", "phase": "1차", "after": ["TASK-001"], "status": "진행 가능",
         "make": [f"`schema.sql` 적용 (테이블 {len(spec.ddl.entities)}개)", "마이그레이션 도구에 등록"],
         "done": ["`DATABASE.md` 4절 업무 제약이 실제로 막는지 테스트 통과"], "refs": "DATABASE.md"},
        {"id": "TASK-003", "name": "인증·권한", "phase": "1차", "after": ["TASK-002"], "status": "진행 가능",
         "make": [*access, "Bearer 토큰 인증, API별 권한 확인 (API.md 권한)",
                  "비밀번호 등 `*_hash` 컬럼 해시 저장"],
         "done": ([f"보안 요구사항 수용 기준 ({', '.join(f'AC-{r}-*' for r in nfr_security)}) 확인"] if nfr_security else [])
         + ["권한 없는 호출은 403"], "refs": "ARCHITECTURE.md 5절"},
    ]
    if policies or code_sets:
        tasks.append({"id": "TASK-004", "name": "공통 규칙·코드값", "phase": "1차", "after": ["TASK-002"], "status": "진행 가능",
                      "make": [f"공통 규칙 {_span('P1', f'P{len(policies)}')} (DECISIONS.md)" if policies else "",
                               f"코드값 {len(code_sets)}종: {', '.join(c.get('name', '') for c in code_sets)}" if code_sets else ""],
                      "done": ["공통 규칙을 쓰는 기능의 '규칙 (공통)' 수용 기준이 통과"], "refs": "DECISIONS.md"})
    base = "TASK-004" if policies or code_sets else None
    order = {g: i for i, g in enumerate(dict.fromkeys(f.group for f in spec.functions))}
    ordered = sorted(spec.functions, key=lambda f: (_phase(f, spec.by_req), max((ranks[t] for t in spec.linked[f.function_id]), default=0),
                                                    order[f.group], f.function_id))
    first_task: dict[str, str] = {}   # 테이블 → 그 테이블을 처음 쓰는 작업
    for n, f in enumerate(ordered, start=len(tasks) + 1):
        tid = f"TASK-{n:03d}"
        linked = spec.linked[f.function_id]
        after = ["TASK-002", "TASK-003"]
        if base and any(rule_tag(r)[0] == "공통" or r.startswith("(공통") for r in f.business_rules):
            after.append(base)
        for t in linked:
            for a in by_name[t].attributes:
                parent = a.references
                if parent in first_task and parent not in linked and first_task[parent] not in after:
                    after.append(first_task[parent])
        for t in linked:
            first_task.setdefault(t, tid)
        entry = spec.api.get(f.function_id) or {}
        make = [f"API `{op['method']} {op['path']}` (`{op['shared_with']}` — 다른 기능과 함께 쓰는 API, 먼저 하는 작업에서 한 번만 만든다)"
                if op.get("shared_with") else f"API `{op['method']} {op['path']}` (`{op['operation_id']}`)"
                for op in entry.get("operations") or []]
        if not make:
            make = [f"배치·자동 실행: {entry.get('no_api_reason') or '(API 설계 없음)'}"]
        make.append("테이블: " + (", ".join(spec.names.table(t) for t in linked) or "없음"))
        acs = [r["id"] for r in spec.acceptance[f.function_id]]
        open_q = _open_questions(f, spec.by_question)
        tasks.append({"id": tid, "name": f.name, "phase": _phase(f, spec.by_req), "after": after,
                      "status": f"일부 보류 ({', '.join(open_q)} 답 대기 — 그 규칙만 TODO)" if open_q else "진행 가능",
                      "make": make, "done": [f"{_span(acs[0], acs[-1])} ({len(acs)}개) 테스트 통과" if acs else "-"],
                      "refs": f"기능 {f.function_id} · 요구사항 {', '.join(f.requirement_ids)}"})
    return tasks


def _span(first: str, last: str) -> str:
    """범위 표기: 'AC-…-1 ~ AC-…-5' (하나면 그 하나만)."""
    return first if first == last else f"{first} ~ {last}"


def _tasks_md(spec: Spec) -> str:
    tasks = _tasks(spec)
    out = [spec.head("TASKS — 작업 목록", "개발 AI에게 줄 작업 순서다. 작업 하나 = 기능 하나."), "",
           "## 1. 진행 규칙", "",
           "- 1차(MVP): 근거 요구사항에 MUST(또는 우선순위 근거 없음)가 있는 기능. 2차: 모두 SHOULD·COULD인 기능.",
           "- 선행 작업이 끝난 뒤 시작한다. 같은 단계 안에서는 부모 테이블을 쓰는 작업이 먼저다.",
           "- '일부 보류'는 고객 답을 기다리는 규칙만 `TODO(질문 ID)`로 두고 나머지를 구현한다.", "",
           "## 2. 작업 목록", "",
           table(["작업", "이름", "단계", "선행 작업", "상태"],
                 [[t["id"], t["name"], t["phase"], ", ".join(t["after"]) or "-", t["status"]] for t in tasks]), "",
           "## 3. 작업 상세", ""]
    for t in tasks:
        out += [f"### {t['id']} · {t['name']} ({t['phase']})", "",
                f"- 근거: {t['refs']}",
                f"- 선행 작업: {', '.join(t['after']) or '없음'}",
                "- 만들 것: " + "; ".join(x for x in t["make"] if x),
                "- 완료 기준: " + "; ".join(t["done"]),
                f"- 상태: {t['status']}", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ DECISIONS.md
def _decisions(spec: Spec) -> list[dict]:
    """결정 기록: 확인 질문(답·추천안) → 기능 명세 공통 결정 → 요구사항 결정(의견·분류 검토) → 이번 실행의 사용자 의견."""
    out = []
    for q in sorted(spec.questions, key=lambda q: (q.stage, q.question_id)):
        if q.status == "ANSWERED":
            status, decision = "확정 (고객 답)", q.answer
        elif q.recommended:
            status, decision = "가정 (추천안, 답 대기)", q.recommended
        else:
            status, decision = "미정 (답 대기)", "-"
        fns = [f.function_id for f in spec.functions
               if set(f.requirement_ids) & set(q.requirement_ids) or any(q.question_id in r for r in f.business_rules)]
        out.append({"title": q.question, "status": status, "source": f"질문 {q.question_id}", "context": q.context,
                    "decision": decision, "impact": _impact(q.requirement_ids, fns, q.entity_ids, spec.names)})
    for c in spec.common.get("code_sets") or []:
        out.append({"title": f"분류·상태 값: {c.get('name', '')}", "status": _basis_status(c.get("basis", "")),
                    "source": "기능 명세 공통 결정", "context": "여러 기능이 같은 값 목록을 쓴다",
                    "decision": ", ".join(c.get("values") or []), "impact": _impact(c.get("requirement_ids") or [], [], [], spec.names)})
    for n, p in enumerate(spec.common.get("policies") or [], start=1):
        fns = [f.function_id for f in spec.functions if any(r.startswith(f"(공통 P{n}") for r in f.business_rules)]
        out.append({"title": f"공통 규칙 P{n}", "status": _basis_status(p.get("basis", "")), "source": "기능 명세 공통 결정",
                    "context": "여러 기능 영역에 같게 적용하는 규칙", "decision": p.get("rule", ""),
                    "impact": _impact(p.get("requirement_ids") or [], fns, [], spec.names)})
    terms = spec.common.get("terms") or []
    if terms:
        out.append({"title": "업무 객체 표준 이름", "status": "확정 (기능 명세 공통 결정)", "source": "기능 명세 공통 결정",
                    "context": "같은 대상을 여러 이름으로 부르지 않는다",
                    "decision": "; ".join(t.get("name", "") + (f" (= {', '.join(t.get('synonyms') or [])})" if t.get("synonyms") else "")
                                          for t in terms), "impact": "-"})
    for r in spec.requirements:
        for d in r.decisions:
            if QUESTION_ID.match(d.lstrip("[")):
                continue   # 질문 답은 위에서 질문으로 기록했다
            out.append({"title": f"{r.requirement_id} {r.title}", "status": "확정 (요구사항 결정)", "source": "요구사항 결정",
                        "context": r.description, "decision": d, "impact": _impact([r.requirement_id], [], [], spec.names)})
    labels = {"requirements_analyst": "요구사항", "function_designer": "기능 명세", "data_modeler": "데이터 설계"}
    for stage, text in sorted((spec.options.get("feedback") or {}).items()):
        if text.strip():
            out.append({"title": f"사용자 의견 ({labels.get(stage, stage)})", "status": "확정 (사용자 의견)",
                        "source": f"rerun {stage} --feedback", "context": "이번 버전을 만들 때 준 의견", "decision": text.strip(),
                        "impact": "-"})
    for n, x in enumerate(out, start=1):
        x["id"] = f"ADR-{n:03d}"
    return out


def _basis_status(basis: str) -> str:
    return {"원문": "확정 (원문)", "결정": "확정 (결정)", "가정": "가정", "확인 필요": "미정 (확인 필요)"}.get(basis, "가정")


def _impact(req_ids: list[str], fn_ids: list[str], entity_ids: list[str], names: Names) -> str:
    parts = []
    if req_ids:
        parts.append("요구사항 " + ", ".join(req_ids))
    if fn_ids:
        parts.append("기능 " + ", ".join(fn_ids))
    if entity_ids:
        parts.append("테이블 " + ", ".join(names.table(e) for e in entity_ids))
    return " · ".join(parts) or "-"


def _decisions_md(spec: Spec) -> str:
    items = _decisions(spec)
    out = [spec.head("DECISIONS — 결정 기록 (ADR)", "설계에서 정한 것과 그 이유, 아직 가정인 것을 모은 문서다."), "",
           "- 상태: 확정(원문·고객 답·결정·사용자 의견) / 가정(근거 없이 정했거나 추천안으로 진행, 고객 답 대기) / 미정.",
           "- '가정'은 `AGENTS.md` 3절 4번 규칙대로 설정값으로 구현한다.", "",
           "## 결정 목록", "",
           table(["ADR", "결정", "상태", "출처"], [[x["id"], x["title"], x["status"], x["source"]] for x in items]), ""]
    for x in items:
        out += [f"### {x['id']} · {x['title']}", "",
                f"- 상태: {x['status']}", f"- 출처: {x['source']}", f"- 배경: {_one_line(x['context'])}",
                f"- 결정: {_one_line(x['decision'])}", f"- 영향: {x['impact']}", ""]
    return "\n".join(out)


def _one_line(text: str) -> str:
    return re.sub(r"\s*\n\s*", " / ", str(text or "-").strip()) or "-"
