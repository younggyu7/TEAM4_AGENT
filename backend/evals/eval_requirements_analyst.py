"""요구사항 Agent 평가: 고정 입력(ra_inputs)을 실제 LLM으로 추출하고 정답지로 채점한다.

    python -m evals.eval_requirements_analyst run --label base --repeat 2 --model claude-haiku-5-5   # LLM 호출(유료), 고객요구사항3·5
    python -m evals.eval_requirements_analyst run --label base --repeat 2 --docs 1,2                 # 고를 문서 번호 (all = 모든 문서)
    python -m evals.eval_requirements_analyst score output/eval/ra/<실행 폴더>                        # 저장된 결과만 다시 채점(무료)
    python -m evals.eval_requirements_analyst score output/eval/ra/<새 폴더> --compare output/eval/ra/<이전 폴더>/score.json

입력은 문서분석 결과를 저장해 둔 것이라 앞 Agent(파서)가 바뀌어도 그대로다. 채점 방법과 지표 정의는 evals/README.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden"
# 문서별 고정 입력·정답지 (evals/golden/doc<번호>/ra_inputs.json, requirements_analyst.yaml)
DOC_ORDER = ["5", "3", "1", "2", "6"]     # 결과의 문서 순서 (이전 결과와 같게, 새 문서는 뒤에)
DEFAULT_DOCS = "3,5"


def doc_numbers(spec: str) -> list[str]:
    """'3,5' · 'all' → 문서 번호 목록 (항상 DOC_ORDER 순서)."""
    wanted = DOC_ORDER if spec == "all" else [n.strip() for n in spec.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in DOC_ORDER]
    if unknown:
        raise SystemExit(f"없는 문서 번호: {unknown} (쓸 수 있는 번호: {', '.join(sorted(DOC_ORDER))}, all)")
    return [n for n in DOC_ORDER if n in wanted]


def load_inputs(spec: str) -> dict:
    docs = {}
    for n in doc_numbers(spec):
        docs.update(json.loads((GOLDEN / f"doc{n}" / "ra_inputs.json").read_text(encoding="utf-8"))["documents"])
    return docs


def load_answers(spec: str) -> dict:
    docs = {}
    for n in doc_numbers(spec):
        docs.update(yaml.safe_load((GOLDEN / f"doc{n}" / "requirements_analyst.yaml").read_text(encoding="utf-8"))["documents"])
    return {"documents": docs}


OUT = ROOT / "output" / "eval" / "ra"
TYPE = {"F": {"FUNCTIONAL"}, "N": {"NON_FUNCTIONAL"}, "FN": {"FUNCTIONAL", "NON_FUNCTIONAL"}}
SCOPE = {"S": {"SYSTEM"}, "P": {"PROJECT"}, "SP": {"SYSTEM", "PROJECT"}}
# 검수 기준에 이 말만 있고 숫자·대상이 없으면 합격 여부를 판단하기 어렵다 (검증 가능성 대용 지표)
VAGUE = re.compile(r"원활|적절|충분|신속|빠르|효율|최적|편리|직관|원만|우수|만족|안정적|용이|최대한|잘\s")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def norm(text: str) -> str:
    """비교용 정규화: NFC, 공백·기호 제거, 소문자 (document_analyst 평가와 같은 규칙)."""
    text = unicodedata.normalize("NFC", text or "")
    return re.sub(r"[\s\-·ㆍ‧∙○〇●□■◦•▪▶※*\"'“”‘’()\[\]{}<>〈〉「」.,:;/|~]+", "", text).lower()


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUMBER.findall(text or "")}


def has_all(text: str, groups: list) -> bool:
    """필수어 묶음이 모두 들어 있나 ('a|b'는 둘 중 하나)."""
    return all(any(norm(a) in text for a in str(g).split("|")) for g in groups)


# ------------------------------------------------------------------ 실행 (LLM 호출)
def run(label: str, repeat: int, model: str | None, effort: str | None, docs: str = DEFAULT_DOCS) -> Path:
    from app.agents.roles import requirements_analyst
    from app.core.config import override_settings
    from app.agents.llm.client import get_llm

    override_settings(llm_provider="anthropic" if model else None, anthropic_model=model, anthropic_effort=effort)
    llm = get_llm()
    inputs = {"documents": load_inputs(docs)}
    tag = "" if doc_numbers(docs) == doc_numbers(DEFAULT_DOCS) else "-docs" + "".join(sorted(doc_numbers(docs)))
    folder = OUT / (datetime.now().strftime("%Y%m%d-%H%M%S") + tag + (f"-{label}" if label else ""))
    folder.mkdir(parents=True, exist_ok=True)
    for r in range(1, repeat + 1):
        result = {"label": label, "docs": ",".join(doc_numbers(docs)), "model": llm.model,
                  "effort": getattr(llm, "effort", ""), "repeat": r,
                  "at": datetime.now().isoformat(timespec="seconds"), "documents": {}}
        for doc_id, doc in inputs["documents"].items():
            state = {"blocks": doc["blocks"], "sources": doc["sources"], "options": {"title": doc["title"]},
                     "next_action": {"mode": "full"}}
            t0 = time.perf_counter()
            out = requirements_analyst.run(state)
            result["documents"][doc_id] = {
                "seconds": round(time.perf_counter() - t0, 1), "usage": out["llm_usage"],
                "failed_batches": (out.get("failures") or {}).get("requirements_analyst", 0),
                "requirements": out["requirements"], "questions": out["questions"]}
            u = out["llm_usage"]
            print(f"[{r}/{repeat}] {doc_id}: 요구사항 {len(out['requirements'])}건, 질문 {len(out['questions'])}건, "
                  f"호출 {u.get('calls')}회, 입력 {u.get('input_tokens'):,} / 출력 {u.get('output_tokens'):,} 토큰")
        (folder / f"run{r}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return folder


# ------------------------------------------------------------------ 채점
class Context:
    def __init__(self):
        # 채점은 모든 문서의 정답을 읽어 두고, 실행 결과에 있는 문서만 채점한다
        self.inputs = load_inputs("all")
        self.answers = load_answers("all")
        self.source_text: dict[str, dict[str, str]] = {}
        self.source_blocks: dict[str, dict[str, set[str]]] = {}
        self.block_text: dict[str, dict[str, str]] = {}
        for d, doc in self.inputs.items():
            bm = {b["source_block_id"]: b["text"] for b in doc["blocks"]}
            self.block_text[d] = bm
            self.source_text[d] = {s["source_key"]: "\n".join(bm[i] for i in s["block_ids"]) for s in doc["sources"]}
            self.source_blocks[d] = {s["source_key"]: set(s["block_ids"]) for s in doc["sources"]}


def req_text(r: dict) -> str:
    return norm(" ".join([r["title"], r["description"], *r.get("acceptance_criteria", [])]))


def score_document(ctx: Context, doc_id: str, data: dict) -> dict:
    gold = ctx.answers["documents"][doc_id]
    reqs, questions = data["requirements"], data["questions"]
    by_source = defaultdict(list)
    for r in reqs:
        by_source[r["source_key"]].append(r)
    default_p = gold.get("default_priority")

    # 1) 핵심 진술 ↔ 요구사항 (같은 추출 단위 안에서, 필수어가 한 요구사항에 모두 있으면 다룬 것)
    statements, used = [], set()
    for unit, items in gold["units"].items():
        for i, st in enumerate(items, 1):
            sid = f"{doc_id}:{unit}#{i}"
            covering = [r for r in by_source.get(unit, []) if has_all(req_text(r), st["m"])]
            # 아직 다른 진술에 연결되지 않은 요구사항을 먼저 쓴다 (한 요구사항이 여러 진술을 다뤄도 인정)
            match = next((r for r in covering if r["requirement_id"] not in used), covering[0] if covering else None)
            row = {"id": sid, "unit": unit, "x": st["x"], "covered": match is not None}
            if match:
                used.add(match["requirement_id"])
                text = req_text(match)
                p = st.get("p", default_p) if "p" in st else default_p
                row.update({
                    "requirement_id": match["requirement_id"],
                    "distorted": [w for w in st.get("f", []) if norm(w) in text],
                    "type_ok": match["type"] in TYPE[st["t"]], "scope_ok": match["scope"] in SCOPE[st["s"]],
                    "category_ok": match["category"] in st["c"],
                    "type": match["type"], "scope": match["scope"], "category": match["category"],
                    "priority": match["priority"], "priority_expected": p,
                    "priority_ok": match["priority"] == p if p else None,
                    "priority_unfounded": p is None and match["priority"] == "MUST",
                })
            statements.append(row)

    # 2) 요구사항별 검사 (정답과 무관하게 원문과 대조)
    rows = []
    for r in reqs:
        src = ctx.source_text[doc_id].get(r["source_key"], "")
        src_nums = numbers(src)
        body_nums = numbers(" ".join([r["title"], r["description"]]))
        crit_nums = numbers(" ".join(r.get("acceptance_criteria", [])))
        quotes = [e for e in r.get("evidence", [])]
        quote_text = " ".join(e["quote"] for e in quotes)
        verified = [e for e in quotes if norm(e["quote"]) and
                    norm(e["quote"]) in norm(ctx.block_text[doc_id].get(e["source_block_id"], ""))]
        in_source = [e for e in quotes if e["source_block_id"] in ctx.source_blocks[doc_id].get(r["source_key"], set())]
        unsupported = sorted((body_nums | crit_nums) - src_nums)
        criteria = r.get("acceptance_criteria", [])
        rows.append({
            "requirement_id": r["requirement_id"], "source_key": r["source_key"], "status": r["status"],
            "matched": r["requirement_id"] in used,
            "unsupported_numbers": unsupported,
            "unfounded_confirmed": bool(unsupported) and r["status"] == "CONFIRMED",
            "criteria": len(criteria),
            "criteria_unfaithful": sorted(crit_nums - src_nums),
            "criteria_vague": [c for c in criteria if VAGUE.search(c) and not NUMBER.search(c)],
            "quote_verified": bool(verified) and len(verified) == len(quotes),
            "quote_in_source": bool(quotes) and len(in_source) == len(quotes),
            # 뒷받침: 요구사항의 (원문에 있는) 숫자가 인용문에도 있어야 인용문이 그 요구사항을 설명한다고 본다
            "supported": bool(verified) and ((body_nums | crit_nums) & src_nums) <= numbers(quote_text),
        })

    # 3) 중복: 같은 단위 안에서 설명이 거의 같은 요구사항
    dups = []
    for unit, items in by_source.items():
        seen = {}
        for r in items:
            key = norm(r["title"] + r["description"])
            if key in seen:
                dups.append((seen[key], r["requirement_id"]))
            seen.setdefault(key, r["requirement_id"])

    # 4) 질문
    qtext = [norm(q["question"] + " " + q.get("context", "")) for q in questions]
    qs = gold.get("questions", {})
    needed = [{"id": q["id"], "found": any(has_all(t, q["m"]) for t in qtext)} for q in qs.get("needed", [])]
    optional = [{"id": q["id"], "found": any(has_all(t, q["m"]) for t in qtext)} for q in qs.get("optional", [])]
    unneeded = [q["question"] for q, t in zip(questions, qtext) if any(has_all(t, n["m"]) for n in qs.get("not_needed", []))]
    excess_blocking = [q["question"] for q, t in zip(questions, qtext)
                       if q.get("blocking") and not any(has_all(t, b["m"]) for b in qs.get("blocking_ok", []))]

    expected_sources = set(gold["units"])
    return {"statements": statements, "requirements": rows, "duplicates": dups,
            "questions": {"total": len(questions), "blocking": sum(q.get("blocking", False) for q in questions),
                          "needed": needed, "optional": optional, "unneeded": unneeded,
                          "excess_blocking": excess_blocking},
            "empty_sources": sorted(expected_sources - set(by_source)),
            "usage": data.get("usage", {}), "seconds": data.get("seconds"), "failed_batches": data.get("failed_batches", 0)}


def _rate(rows, key, cond=lambda r: True):
    pool = [r for r in rows if cond(r)]
    return round(sum(bool(r[key]) for r in pool) / len(pool), 4) if pool else None


def summarize(run_scores: list[dict]) -> dict:
    """여러 번 실행한 결과의 평균과 일관성."""
    def per_run(fn):
        vals = [fn(s) for s in run_scores]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def all_(s, part):
        return [x for d in s.values() for x in d[part]]

    cov = lambda s: [x for x in all_(s, "statements") if x["covered"]]  # noqa: E731
    summary = {
        "요구사항 재현율 (핵심 진술)": per_run(lambda s: _rate(all_(s, "statements"), "covered")),
        "의미 왜곡 (금지어 포함)": per_run(lambda s: _rate(cov(s), "distorted")),
        "근거 없는 확정 비율 (원문에 없는 수치로 CONFIRMED)": per_run(lambda s: _rate(all_(s, "requirements"), "unfounded_confirmed")),
        "원문에 없는 수치가 든 요구사항": per_run(lambda s: _rate(all_(s, "requirements"), "unsupported_numbers")),
        "분류: type": per_run(lambda s: _rate(cov(s), "type_ok")),
        "분류: scope": per_run(lambda s: _rate(cov(s), "scope_ok")),
        "분류: category": per_run(lambda s: _rate(cov(s), "category_ok")),
        "우선순위 정확 (원문에 근거 있는 것)": per_run(lambda s: _rate(cov(s), "priority_ok", lambda r: r["priority_ok"] is not None)),
        "근거 없는 MUST (원문에 우선순위 없음)": per_run(lambda s: _rate(cov(s), "priority_unfounded", lambda r: r["priority_expected"] is None)),
        "검수 기준 원문 충실 (원문에 없는 수치 없음)": per_run(lambda s: 1 - _rate(all_(s, "requirements"), "criteria_unfaithful")),
        "검수 기준 모호 (수치 없이 '원활·신속' 등)": per_run(lambda s: _rate(all_(s, "requirements"), "criteria_vague")),
        "검수 기준 없음": per_run(lambda s: sum(r["criteria"] == 0 for r in all_(s, "requirements")) / max(1, len(all_(s, "requirements")))),
        "인용문 원문 일치": per_run(lambda s: _rate(all_(s, "requirements"), "quote_verified")),
        "인용문이 해당 추출 단위 블록": per_run(lambda s: _rate(all_(s, "requirements"), "quote_in_source")),
        "근거 뒷받침 (요구사항 수치가 인용문에 있음)": per_run(lambda s: _rate(all_(s, "requirements"), "supported")),
        "정답에 없는 요구사항 (검토 대상)": per_run(lambda s: 1 - _rate(all_(s, "requirements"), "matched")),
        "중복 요구사항": per_run(lambda s: sum(len(d["duplicates"]) for d in s.values()) / max(1, len(all_(s, "requirements")))),
        "필요한 질문 포착": per_run(lambda s: _rate([q for d in s.values() for q in d["questions"]["needed"]], "found")),
        "질문 수 / blocking": " · ".join(f"{sum(d['questions']['total'] for d in s.values())}/"
                                        f"{sum(d['questions']['blocking'] for d in s.values())}" for s in run_scores),
        "불필요한 질문 후보": " · ".join(str(sum(len(d["questions"]["unneeded"]) for d in s.values())) for s in run_scores),
        "과다 blocking (허용 주제 밖)": " · ".join(str(sum(len(d["questions"]["excess_blocking"]) for d in s.values()))
                                               for s in run_scores),
        "요구사항 수": " · ".join(str(len(all_(s, "requirements"))) for s in run_scores),
        "결과 없는 추출 단위": " · ".join(str(sum(len(d["empty_sources"]) for d in s.values())) for s in run_scores),
        "실패 배치": " · ".join(str(sum(d["failed_batches"] for d in s.values())) for s in run_scores),
        "LLM 호출 (재시도 포함)": " · ".join(str(sum(d["usage"].get("calls", 0) for d in s.values())) for s in run_scores),
        "입력/출력 토큰": " · ".join(f"{sum(d['usage'].get('input_tokens', 0) for d in s.values()):,}/"
                                    f"{sum(d['usage'].get('output_tokens', 0) for d in s.values()):,}" for s in run_scores),
        "처리 시간(s)": " · ".join(str(round(sum(d["seconds"] or 0 for d in s.values()), 1)) for s in run_scores),
    }
    if len(run_scores) > 1:     # 같은 입력을 여러 번: 진술마다 매번 같은 결과(다룸/못 다룸, 분류)인가
        ids = [x["id"] for x in all_(run_scores[0], "statements")]
        per = [{x["id"]: x for x in all_(s, "statements")} for s in run_scores]
        same_cov = sum(len({p[i]["covered"] for p in per}) == 1 for i in ids)
        both = [i for i in ids if all(p[i]["covered"] for p in per)]
        same_cls = sum(len({(p[i]["type"], p[i]["scope"]) for p in per}) == 1 for i in both)
        summary["일관성: 진술 다룸 여부 같음"] = round(same_cov / len(ids), 4)
        summary["일관성: 분류(type·scope) 같음"] = round(same_cls / len(both), 4) if both else None
        summary["매번 놓친 진술"] = sum(all(not p[i]["covered"] for p in per) for i in ids)
    return summary


def score(folder: Path, compare: Path | None) -> dict:
    ctx = Context()
    runs = sorted(folder.glob("run*.json"))
    run_scores, meta = [], {}
    for path in runs:
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = {k: data[k] for k in ("label", "model", "effort")}
        run_scores.append({d: score_document(ctx, d, v) for d, v in data["documents"].items()})
    result = {"folder": str(folder), **meta, "runs": len(runs), "summary": summarize(run_scores), "detail": run_scores}
    (folder / "score.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    previous = json.loads(compare.read_text(encoding="utf-8")) if compare else None
    text = report(result, previous)
    (folder / "score.md").write_text(text, encoding="utf-8")
    print(text)
    return result


def report(result: dict, previous: dict | None) -> str:
    lines = [f"# requirements_analyst 평가 ({result.get('model')}, effort {result.get('effort')}, {result['runs']}회 실행)",
             "", "| 지표 | 값 |" + (" 이전 |" if previous else ""), "| --- | --- |" + (" --- |" if previous else "")]
    for k, v in result["summary"].items():
        lines.append(f"| {k} | {v} |" + (f" {previous['summary'].get(k, '-')} |" if previous else ""))
    first = result["detail"][0]
    lines += ["", "## 첫 실행에서 놓친 핵심 진술", ""]
    for d in first.values():
        lines += [f"- {s['id']} {s['x']}" for s in d["statements"] if not s["covered"]]
    lines += ["", "## 첫 실행의 분류 불일치 (상위 30)", ""]
    n = 0
    for d in first.values():
        for s in d["statements"]:
            if s["covered"] and not (s["type_ok"] and s["scope_ok"] and s["category_ok"]) and n < 30:
                lines.append(f"- {s['id']} {s['x'][:40]} → {s['type']}/{s['scope']}/{s['category']}")
                n += 1
    lines += ["", "## 첫 실행의 원문에 없는 수치", ""]
    for d in first.values():
        lines += [f"- {r['requirement_id']} ({r['status']}): {r['unsupported_numbers']}"
                  for r in d["requirements"] if r["unsupported_numbers"]]
    lines += ["", "## 첫 실행의 질문", ""]
    for doc_id, d in first.items():
        q = d["questions"]
        lines.append(f"- {doc_id}: 필요 {[x['id'] for x in q['needed'] if x['found']]} 포착 / "
                     f"{[x['id'] for x in q['needed'] if not x['found']]} 놓침, 불필요 후보 {q['unneeded']}")
        lines += [f"  - 과다 blocking: {x}" for x in q["excess_blocking"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m evals.eval_requirements_analyst")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="LLM으로 추출하고 채점 (유료)")
    r.add_argument("--label", default="")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--model")
    r.add_argument("--effort")
    r.add_argument("--docs", default=DEFAULT_DOCS, help="실행할 문서 번호 (예: 3,5 · 1,2 · all). 기본 3,5")
    r.add_argument("--compare")
    s = sub.add_parser("score", help="저장된 실행 결과만 다시 채점 (무료)")
    s.add_argument("folder")
    s.add_argument("--compare")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        folder = run(args.label, args.repeat, args.model, args.effort, args.docs)
    else:
        folder = Path(args.folder)
    score(folder, Path(args.compare) if args.compare else None)


if __name__ == "__main__":
    main()
