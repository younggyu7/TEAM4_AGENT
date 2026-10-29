"""골든셋 설명 파일 4개를 문서별 폴더(evals/golden/doc*/)에서 다시 만든다.

    python -m evals.build_golden_docs

만드는 파일 (evals/golden/)
- 골든셋_목록.json : 문서별 폴더·파일·정답 수·버전·원본 문서
- 사용안내.md      : 무엇을 시험하는지, 사용 순서, 채점 기준, 문서별 특이점
- 정답_검토표.md   : 사람이 원문과 대조하며 검수할 표 (두 Agent의 정답)
- 검증결과.json    : 골든셋 자체 검사 결과 (인용문 존재·부재, 필수어, 파일 지문 등)
정답 데이터는 문서별 폴더가 원본이다. 정답을 고친 뒤 이 명령으로 네 파일을 다시 맞춘다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden"
DOC_ORDER = ["1", "2", "3", "5", "6"]

# 문서별 원본과 특이점 (사람이 확인한 사실. 정답 데이터에서 계산할 수 없는 것)
SOURCES = {
    "1": ("1-2. 제안요청서 1부.pdf", "한국관세무역개발원 정보시스템 통합유지보수",
          ["PDF는 파일 1쪽이 인쇄 10쪽이다. 파일 쪽수와 인쇄 쪽수를 구분한다.",
           "중첩 표 3개(MPR-001 점검방법, SER-001 패스워드 규칙, PMR-005 산출물)는 쪽 이미지로 구조를 확인해 행 단위로 정답을 적었다.",
           "S/W 원격점검 주기가 '매월, 분기'로 함께 적혀 있다 (확인 질문 후보).",
           "원본데이터 폴더의 정제본 PDF는 이미지로만 된 PDF라 쓰지 않는다. data/의 글자 있는 파일을 쓴다."]),
    "2": ("2. 제안요청서_KOBACO 정보시스템 통합유지운영.hwpx", "KOBACO 정보시스템 통합유지운영",
          ["MAR-038의 'AI 기능 개선(MPR-002 참조)'이 문서에 없는 요구사항 번호를 가리킨다 (Q09).",
           "요구사항 표 안에 중첩 표가 있다: MAR-031(공개SW 현황), MAR-036(AiSAC 대상), MHR-002(인력 규모·기술 요건), SER-005(정보보호 대책 목록).",
           "MAR-017~020 등은 칸 이름이 '요구사항 명칭'이 아니라 '요구사항 명'이다.",
           "인터페이스 요구사항 번호가 관례(SIR)와 다른 INR-001이지만 분류 안에 하나뿐이라 불일치가 아니다 (Q14)."]),
    "3": ("붙임 2. 제안요청서(ITSM 구축).hwpx", "대한적십자사 ITSM 구축",
          ["ECR-001, ECR-002, SER-001에 내용이 다른 요구사항이 둘씩 있다. ID를 딕셔너리 키로만 쓰면 내용이 덮어써진다.",
           "T17/T18은 (1/2)/(2/2)로 이어지는 한 요구사항이다. T0의 ECR-001과는 별도다.",
           "T35는 PMR-001 안의 산출물 표다. 별도 요구사항으로 세지 않는다.",
           "'라. 성능 요구사항' 아래에 장비구성 요구사항(ECR-001_2·ECR-002_2)이 있다. 원문 목차 문제로 보이나 정답 사례는 없다."]),
    "5": ("제안요청서_화물자동차 디지털 일상점검 및 차량관리 통합 플랫폼 구축 용역.pdf", "화물차 디지털 운행관리 플랫폼 구축",
          ["PDF는 파일 1쪽이 인쇄 8쪽이다.",
           "로그 관리 요구사항 ID는 실제로 SR-004다. SER-004로 자동 수정하지 않는다 (Q01).",
           "원본의 '요구사항 총괄표'(9쪽) 행은 요구사항이 아니다. 정제본에는 빠져 있다.",
           "응락 수준 칸이 없다. 우선순위는 원문 표현으로만 판단한다."]),
    "6": ("인천공항_요구사항.xlsx", "인천국제공항공사 시설관리시스템 구축",
          ["원본 엑셀의 상세요구사항 415건 중 앞 73건(SFT-001~073, 모두 기능 요구사항)이다. 정답 원문은 엑셀 셀 글자다.",
           "SFT-063: PDF로 바뀌며 셀이 잘려 세 번째 항목 뒷부분 7줄과 '* 입주자포털…' 문장이 PDF에 없다. 정답은 PDF에 남은 데까지다.",
           "SFT-068: 마지막 3줄 위에 다른 표(산출물 목록)가 겹쳐 있다. 글자는 파일에 있으므로 정답에 넣었다 (Q15).",
           "SFT-069: 첫 행(요구사항 분류)이 앞 쪽(14쪽) 맨 아래에 따로 떨어져 있다.",
           "원문에 '입주차포털'(SFT-059·065·066)과 '입주자포털'(SFT-063)이 섞여 있다. 원문 그대로 둔다."]),
}
ACTION = {"verified": "기존 위치 확인", "relinked": "다른 올바른 위치에 재연결", "unconfirmed": "⚠미확인 유지 (확인 완료로 만들면 실패)"}
EXPECT = {"required": "경고가 나와야 함", "review": "확인 요청 경고가 나와야 함", "negative": "경고가 나오면 안 됨"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    return re.sub(r"[\s\-·ㆍ‧∙○〇●□■◦•▪▶※*\"'“”‘’()\[\]{}<>〈〉「」.,:;/|~]+", "", text).lower()


def load(n: str) -> dict:
    folder = GOLDEN / f"doc{n}"
    return {"n": n, "folder": folder,
            "da": json.loads((folder / "document_analyst.json").read_text(encoding="utf-8")),
            "inputs": json.loads((folder / "ra_inputs.json").read_text(encoding="utf-8")),
            "ra": yaml.safe_load((folder / "requirements_analyst.yaml").read_text(encoding="utf-8"))}


def where(loc: dict) -> str:
    if "pdf_page_1based" in loc:
        return f"PDF {loc['pdf_page_1based']}쪽" + (f" / 인쇄 {loc['printed_page']}쪽" if loc.get("printed_page") else "")
    return f"{loc.get('section', '').split('/')[-1]} T{loc.get('table_index_0based')}"


# ------------------------------------------------------------------ 검증
def check(docs: list[dict]) -> dict:
    checks, problems = [], []
    all_ids = [c["id"] for d in docs for c in d["da"]["cases"]]
    dup = [i for i, k in Counter(all_ids).items() if k > 1]
    checks.append("사례 ID가 문서끼리 겹치지 않음")
    problems += [f"사례 ID 중복: {i}" for i in dup]
    for d in docs:
        n, da = d["n"], d["da"]
        units = {u["unit_key"]: u for u in da["reference_units"]}
        text = {k: norm(u["reference_text"]) for k, u in units.items()}
        whole = "".join(text.values())
        for doc in da["documents"]:
            path = ROOT / doc["source_path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
                problems.append(f"doc{n}: {doc['source_path']} 파일 지문이 정답을 만들 때와 다름")
        for c in da["cases"]:
            e, i = c["expected_output"], c["input"]
            keys = [e.get("unit_key")] + e.get("expected_unit_keys", []) + e.get("affected_unit_keys", []) \
                + e.get("allowed_target_unit_keys", [])
            problems += [f"{c['id']}: 없는 정답 단위 {k}" for k in keys if k and k not in units]
            if i["mode"] == "research":
                q = norm(i["quote"])
                claimed = next((k for k, u in units.items() if u["requirement_id"] == i["original_locator"]["requirement_id"]
                                and u["locations"] == i["original_locator"]["locations"]), None)
                if e["action"] == "unconfirmed" and q in whole:
                    problems.append(f"{c['id']}: 미확인이어야 할 인용문이 문서에 있음")
                if e["action"] != "unconfirmed" and any(q not in text[t] for t in e["allowed_target_unit_keys"]):
                    problems.append(f"{c['id']}: 정답 위치에 인용문이 없음")
                if e["action"] == "relinked" and claimed and q in text[claimed]:
                    problems.append(f"{c['id']}: 재연결 사례인데 주장한 위치에 인용문이 있음")
                if e["action"] == "verified" and claimed and q not in text[claimed]:
                    problems.append(f"{c['id']}: 정상 인용 사례인데 주장한 위치에 인용문이 없음")
            if c["metadata"]["type"] == "청킹 완결성":
                problems += [f"{c['id']}: 청킹 문구가 정답 원문에 없음 '{p}'" for p in e["must_include_together"]
                             if norm(p) not in text[e["unit_key"]]]
        # requirements_analyst: 정답 단위 = 고정 입력의 추출 단위, 필수어는 입력 원문에 있어야 함
        for doc_id, doc in d["inputs"]["documents"].items():
            bm = {b["source_block_id"]: b["text"] for b in doc["blocks"]}
            src = {s["source_key"]: norm("\n".join(bm[i] for i in s["block_ids"])) for s in doc["sources"]}
            gold = d["ra"]["documents"][doc_id]
            if set(gold["units"]) != set(src):
                problems.append(f"doc{n}: 정답지 단위와 고정 입력의 추출 단위가 다름 {sorted(set(gold['units']) ^ set(src))}")
            for unit, sts in gold["units"].items():
                for st in sts or []:
                    for grp in st["m"]:
                        if not any(norm(a) in src.get(unit, "") for a in str(grp).split("|")):
                            problems.append(f"doc{n} {unit}: 필수어 '{grp}'가 입력 원문에 없음")
            for q in gold["questions"].get("needed", []) + gold["questions"].get("optional", []):
                if q["unit"] not in gold["units"]:
                    problems.append(f"doc{n}: 질문 {q['id']}의 단위 {q['unit']}가 없음")
    checks += ["문서 파일 지문(sha256)이 정답을 만들 때와 같음", "사례가 가리키는 정답 단위가 모두 있음",
               "정상·재연결 인용문은 정답 위치 원문에 있음", "미확인 인용문은 문서 어디에도 없음",
               "재연결 인용문은 주장한 위치에 없음", "청킹 문구는 정답 원문에 있음",
               "requirements_analyst 정답 단위 = 고정 입력의 추출 단위", "requirements_analyst 필수어는 입력 원문에 있음",
               "확인 질문이 가리키는 단위가 있음"]
    return {"status": "passed" if not problems else "failed", "checked_at": date.today().isoformat(),
            "documents": [f"doc{d['n']}" for d in docs], "checks": checks, "problems": problems,
            "not_checked": ["실제 Agent 실행 결과 (evals.eval_document_analyst / eval_requirements_analyst로 채점)",
                            "담당자 정답 승인 (human_review_status: pending)",
                            "분류(t·s·c)·확인 질문 같은 판단의 타당성"]}


# ------------------------------------------------------------------ 목록
def index(docs: list[dict]) -> dict:
    out = []
    for d in docs:
        n, da = d["n"], d["da"]
        doc = da["documents"][0]
        ra_doc = next(iter(d["ra"]["documents"].values()))
        out.append({"folder": f"doc{n}", "document_id": doc["document_id"], "file": doc["source_path"],
                    "format": doc["format"], "sha256": doc["sha256"], "original_source": SOURCES[n][0], "project": SOURCES[n][1],
                    "split": da["meta"].get("split", "tuning"), "status": da["meta"].get("status"),
                    "document_analyst": {"file": f"doc{n}/document_analyst.json", "version": da["meta"]["version"],
                                         "reference_units": len(da["reference_units"]), "cases": len(da["cases"]),
                                         "case_counts": dict(Counter(c["metadata"]["type"] for c in da["cases"]))},
                    "requirements_analyst": {"answers": f"doc{n}/requirements_analyst.yaml", "inputs": f"doc{n}/ra_inputs.json",
                                             "version": d["ra"]["meta"]["version"],
                                             "units": len(ra_doc["units"]),
                                             "statements": sum(len(v or []) for v in ra_doc["units"].values()),
                                             "questions_needed": len(ra_doc["questions"].get("needed", [])),
                                             "questions_optional": len(ra_doc["questions"].get("optional", []))}})
    return {"name": "요구사항 분석 Agent 튜닝용 골든셋", "generated_at": date.today().isoformat(),
            "generated_by": "python -m evals.build_golden_docs", "documents": out,
            "totals": {"documents": len(out),
                       "reference_units": sum(x["document_analyst"]["reference_units"] for x in out),
                       "document_analyst_cases": sum(x["document_analyst"]["cases"] for x in out),
                       "requirements_analyst_statements": sum(x["requirements_analyst"]["statements"] for x in out)},
            "note": "모든 문서가 튜닝용이다. 튜닝에 쓰지 않은 문서(예: 고객요구사항4, 7~12)로 만든 평가용 골든셋이 따로 필요하다."}


# ------------------------------------------------------------------ 검토표
def review(docs: list[dict]) -> str:
    L = ["# 정답 검토표", "",
         "검수 상태: 담당자 확인 전 초안. 이 파일은 `python -m evals.build_golden_docs`로 문서별 폴더에서 만든다. "
         "정답을 고칠 때는 이 파일이 아니라 `doc<번호>/` 안의 파일을 고친다.", "",
         "HWPX 위치의 `T번호`는 XML 안 중첩 표를 포함한 0부터의 표 순번이다.", ""]
    L += ["## 전체 요구사항 목록", "", "| 정답 키 | 원문 명칭 | 원문 분류 | 출처 |", "|---|---|---|---|"]
    for d in docs:
        for u in d["da"]["reference_units"]:
            L.append(f"| {u['unit_key']} | {u['title']} | {u['category_original']} | {' + '.join(where(x) for x in u['locations'])} |")
    L += ["", "## 청킹 정답", ""]
    for d in docs:
        for c in d["da"]["cases"]:
            if c["metadata"]["type"] == "청킹 완결성":
                e = c["expected_output"]
                L += [f"### {c['id']} · {e['unit_key']}", "", c["metadata"].get("evaluation", ""), "", "함께 보존할 내용:"]
                L += [f"- {p}" for p in e["must_include_together"]] + [""]
    L += ["## 근거 검증 정답", "",
          "`strict_quote_integrity_v1`: 숫자·버전·부정 표현·대상이 바뀐 인용문은 비슷한 원문을 찾아도 확인 완료로 만들면 실패.", ""]
    for d in docs:
        for c in d["da"]["cases"]:
            if c["input"]["mode"] != "research":
                continue
            i, e = c["input"], c["expected_output"]
            loc = i["original_locator"]
            L += [f"### {c['id']} · {c['metadata']['type']}", "",
                  f"- 문서: {i['document_id']} / 기존 원문 ID: {loc['requirement_id']} / 기존 위치: {where(loc['locations'][0])}",
                  f"- 입력 인용문: {i['quote']}", f"- 정답: {ACTION[e['action']]}",
                  f"- 허용 정답 위치: {', '.join(e['allowed_target_unit_keys']) or '없음'}",
                  f"- 입력 변형 여부: {'시험용으로 만든 입력' if c['metadata'].get('synthetic_input') else '원문 인용'}",
                  f"- 채점: {c['metadata'].get('evaluation', '')}", ""]
    L += ["## 문서 품질 정답", ""]
    for d in docs:
        for c in d["da"]["cases"]:
            if c["metadata"]["type"] != "문서 품질 검사":
                continue
            e = c["expected_output"]
            L += [f"### {c['id']} · {c['input']['document_id']} · {e['issue_type']} ({EXPECT.get(e['expectation'], e['expectation'])})", "",
                  c["metadata"].get("evaluation", ""), ""]
            if e["affected_unit_keys"]:
                L += [f"- 대상: {', '.join(e['affected_unit_keys'])}", ""]
    L += ["## requirements_analyst 정답 (핵심 진술)", "",
          "진술마다 필수어(m)가 한 요구사항(제목+설명+검수 기준)에 모두 있으면 다룬 것으로 본다. "
          "type F=기능, N=비기능, FN=둘 다 인정 / scope S=시스템, P=수행업체 의무, SP=둘 다 인정 / 우선순위가 비어 있으면 원문에 근거 없음.", ""]
    for d in docs:
        for doc_id, gold in d["ra"]["documents"].items():
            L += [f"### {doc_id} (doc{d['n']}, 정답지 v{d['ra']['meta']['version']})", ""]
            for unit, sts in gold["units"].items():
                L += [f"#### {doc_id}:{unit}", "", "| # | 핵심 진술 | 필수어 | type | scope | category | 우선순위 |", "|---|---|---|---|---|---|---|"]
                for k, st in enumerate(sts or [], 1):
                    x = st["x"].replace("|", "\\|")
                    m = ", ".join(str(g) for g in st["m"]).replace("|", " 또는 ")
                    L.append(f"| {k} | {x} | {m} | {st['t']} | {st['s']} | {', '.join(st['c'])} | {st.get('p', '')} |")
                L.append("")
            qs = gold["questions"]
            L += [f"#### {doc_id} 확인 질문", ""]
            for kind, label in (("needed", "꼭 필요한 질문"), ("optional", "있으면 좋은 질문")):
                L += [f"- {label}: " + ("없음" if not qs.get(kind) else "")]
                L += [f"  - {q['id']} ({q['unit']}): {q['about']}" for q in qs.get(kind, [])]
            L += [f"- blocking 허용 주제: " + (", ".join(q["about"] for q in qs.get("blocking_ok", [])) or "없음"),
                  f"- 물을 필요 없는 질문: " + (", ".join(q["about"] for q in qs.get("not_needed", [])) or "없음"), ""]
    L += ["## 요구사항별 원문 대조 텍스트", ""]
    for d in docs:
        for u in d["da"]["reference_units"]:
            L += [f"### {u['unit_key']} · {u['title']}", "", ' + '.join(where(x) for x in u["locations"]), "",
                  "```text", u["reference_text"], "```", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ 사용안내
def guide(docs: list[dict], idx: dict) -> str:
    types = Counter(c["metadata"]["type"] for d in docs for c in d["da"]["cases"])
    research = [c for d in docs for c in d["da"]["cases"] if c["input"]["mode"] == "research"]
    acts = Counter(c["expected_output"]["action"] for c in research)
    quality = Counter(c["expected_output"]["expectation"] for d in docs for c in d["da"]["cases"] if c["metadata"]["type"] == "문서 품질 검사")
    t = idx["totals"]
    L = ["# 골든셋 사용안내", "",
         f"**검수용 초안입니다.** 원문을 확인해 만든 정답 후보이며, 담당자 검수를 마친 확정 골든셋은 아닙니다. "
         f"고객요구사항 {', '.join(d['n'] for d in docs)} 모두 **튜닝용**입니다.", "",
         f"문서 {t['documents']}건, document_analyst 정답 단위 {t['reference_units']}개·사례 {t['document_analyst_cases']}개, "
         f"requirements_analyst 핵심 진술 {t['requirements_analyst_statements']}개입니다.", "",
         "## 파일 구성", "",
         "| 위치 | 내용 |", "|---|---|",
         "| `doc<번호>/document_analyst.json` | document_analyst 정답: 원문·위치, 전체 추출·요구사항·청킹·근거 검증·문서 품질 사례 |",
         "| `doc<번호>/ra_inputs.json` | requirements_analyst 고정 입력 (문서분석 결과를 저장해 둔 것. 파서가 바뀌어도 그대로 둔다) |",
         "| `doc<번호>/requirements_analyst.yaml` | requirements_analyst 정답지: 핵심 진술·분류·우선순위·확인 질문 |",
         "| `골든셋_목록.json` | 문서별 폴더·원본 문서·정답 수 목록 |",
         "| `정답_검토표.md` | 사람이 원문과 대조하며 검수할 표 |",
         "| `검증결과.json` | 골든셋 자체 검사 결과 |", "",
         "정답은 `doc<번호>/` 안의 파일이 원본입니다. 정답을 고친 뒤 `python -m evals.build_golden_docs`로 나머지 세 파일(목록·검토표·검증결과)과 이 안내를 다시 만듭니다.", "",
         "| 폴더 | 문서 | 원본 | 정답 단위 / 사례 | 핵심 진술 |", "|---|---|---|---|---|"]
    for x in idx["documents"]:
        L.append(f"| `{x['folder']}` | {x['file'].split('/')[-1]} ({x['project']}) | {x['original_source']} | "
                 f"{x['document_analyst']['reference_units']} / {x['document_analyst']['cases']} | {x['requirements_analyst']['statements']} |")
    L += ["", "## 무엇을 시험하나요?", "", "### document_analyst", "", "| 시험 | 사례 수 | 확인할 내용 |", "|---|---:|---|",
          f"| 전체 추출 | {types['전체 추출 목록']} | 문서의 요구사항 목록이 빠짐없이, 중복 없이 구성되는가 |",
          f"| 개별 요구사항 | {types['요구사항 추출 및 내용 보존']} | ID·제목·분류·내용·출처가 보존되고 다른 요구사항과 섞이지 않는가 |",
          f"| 청킹 | {types['청킹 완결성']} | 중첩 표·조건·예외·이어지는 표가 같은 논리 단위에 유지되는가 |",
          f"| 근거 검증/복구 | {len(research)} | 정상 {acts['verified']}건·재연결 {acts['relinked']}건·미확인 {acts['unconfirmed']}건 |",
          f"| 문서 품질 | {types['문서 품질 검사']} | 경고가 나와야 함 {quality['required']}건·확인 요청 {quality['review']}건·오탐 방지 {quality['negative']}건 |",
          "", "### requirements_analyst", "",
          "| 묶음 | 확인할 내용 |", "|---|---|",
          "| 추출 | 핵심 진술을 빠뜨리지 않았나, 뜻을 바꾸지 않았나, 중복은 없나 |",
          "| 과장 방지 | 원문에 없는 수치로 확정하지 않았나, 근거 없이 MUST로 단정하지 않았나 |",
          "| 분류 | 기능/비기능, 시스템/수행업체 의무, 분류, 우선순위 |",
          "| 검수 기준 | 원문에 맞고 합격 여부를 판단할 수 있나 |",
          "| 근거 | 인용문이 원문에 있고 요구사항을 뒷받침하나 |",
          "| 질문 | 필요한 질문을 했나, 쓸데없이 진행을 막지 않았나 |", "",
          "## 사용 순서", "",
          "1. `정답_검토표.md`를 원문과 대조합니다. 아래 '문서별 특이점'을 먼저 확인합니다.",
          "2. 정답을 고쳤다면 `doc<번호>/` 안의 파일을 고치고 `human_review_status`를 갱신합니다. 그다음 `python -m evals.build_golden_docs`를 실행해 `검증결과.json`이 `passed`인지 봅니다.",
          "3. 채점합니다 (LLM 없음, 무료): `python -m evals.eval_document_analyst --docs all`",
          "4. requirements_analyst는 LLM을 부릅니다 (유료): `python -m evals.eval_requirements_analyst run --repeat 2 --docs 1,2`",
          "5. 규칙을 하나 바꾸고 같은 조건에서 다시 채점해 `--compare`로 비교합니다. 새로 맞힌 사례와 새로 틀린 사례를 모두 기록합니다.", "",
          "자세한 지표 정의와 결과 기록은 `evals/README.md`에 있습니다.", "",
          "## 채점 기준", "",
          "- 요구사항 추출 재현율 = 정확히 대응된 논리 요구사항 / 정답 요구사항 수. 정밀도 = 정확히 대응된 논리 요구사항 / 만든 추출 단위 수. 문서별로 보고합니다.",
          "- 같은 ID라도 제목과 위치가 다르면 별도 요구사항일 수 있습니다 (고객요구사항3). ID만으로 연결하지 않습니다.",
          "- 내용 보존은 정답 원문을 항목 기호·줄 단위 조각으로 나눠, NFC 정규화 후 공백·기호를 지우고 비교합니다.",
          "- 잘못된 검증 통과 = 미확인이어야 할 인용문 중 확인 완료로 처리한 비율. 가장 중요한 실패로 관리합니다.",
          "- 사례를 하나의 점수로만 합치지 않습니다. 추출 사례가 많아 근거 검증 오류를 가릴 수 있습니다.",
          "- requirements_analyst는 분해 개수·표현이 달라도, 핵심 진술의 필수어가 한 요구사항에 모두 있으면 다룬 것으로 봅니다.", "",
          "## 안전한 근거 복구의 목표", "",
          "정책은 `strict_quote_integrity_v1`입니다. 원문이 10초인데 입력 인용문이 5초라면 비슷한 원문을 후보로 보여줄 수는 있지만, "
          "5초 근거를 검증 완료로 만들면 실패입니다. 근거 검증 사례의 숫자·버전·금지·대상 변조 입력은 시험용으로 만든 것이며 원문 내용이 아닙니다.", "",
          "## 문서별 특이점", ""]
    for d in docs:
        L += [f"### doc{d['n']} · {d['da']['documents'][0]['file_name']} (원본: {SOURCES[d['n']][0]})", ""]
        L += [f"- {s}" for s in SOURCES[d["n"]][2]] + [""]
    L += ["## 범위와 검수 상태", "",
          "- 모든 문서가 튜닝용입니다. 이 문서들에 맞춰 규칙을 고치므로 일반 성능이라고 주장할 수 없습니다. "
          "튜닝에 쓰지 않은 문서(예: 고객요구사항4, 7~12)로 평가용 골든셋을 따로 만들어야 합니다.",
          "- doc3·doc5의 document_analyst 정답은 처음 받은 골든셋 v0.1(문서 3·5를 한 파일에 묶은 것)을 문서별로 나눈 것입니다. 받은 원본 파일은 2026-10-28에 지웠습니다.",
          "- doc1·doc2·doc6의 document_analyst 정답과 모든 requirements_analyst 정답지는 AI가 작성한 초안입니다 (각 파일의 `provenance` 참고).",
          "- TXT/DOCX 문서, 텍스트층 없는 PDF 양성 사례는 아직 없습니다.", "",
          "## 변경 전후 기록 양식", "",
          "| 항목 | 변경 전 | 변경 후 |", "|---|---|---|",
          "| 코드/규칙 버전 | | |", "| 골든셋 (`골든셋_목록.json`의 generated_at) | | |",
          "| 추출 재현율·정밀도 | | |", "| 청킹 통과 | | |", "| 기존 근거 확인 | | |", "| 재연결 정밀도·복구 | | |",
          "| 잘못된 검증 통과 | | |", "| 문서 품질 통과 | | |",
          "| requirements_analyst 재현율·근거 없는 확정·분류 | | |", "| 처리 시간·LLM 토큰 | | |", "| 새로 실패한 사례 ID | | |"]
    return "\n".join(L) + "\n"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    docs = [load(n) for n in DOC_ORDER if (GOLDEN / f"doc{n}").exists()]
    idx = index(docs)
    result = check(docs)
    (GOLDEN / "골든셋_목록.json").write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    (GOLDEN / "검증결과.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (GOLDEN / "정답_검토표.md").write_text(review(docs), encoding="utf-8")
    (GOLDEN / "사용안내.md").write_text(guide(docs, idx), encoding="utf-8")
    print(f"문서 {len(docs)}건 → 골든셋_목록.json · 사용안내.md · 정답_검토표.md · 검증결과.json")
    print(f"검증결과: {result['status']}" + (f" (문제 {len(result['problems'])}건)" if result["problems"] else ""))
    for p in result["problems"][:20]:
        print("  -", p)


if __name__ == "__main__":
    main()
