"""문서분석 Agent 평가: 골든셋으로 full·research 모드를 채점한다.

    python -m evals.eval_document_analyst                               # 고객요구사항3·5 채점, 결과는 output/eval/에 저장
    python -m evals.eval_document_analyst --docs 1,2                    # 고르고 싶은 문서 번호
    python -m evals.eval_document_analyst --docs all                    # 모든 문서
    python -m evals.eval_document_analyst --compare output/eval/<이전>.json   # 변경 전후 비교 (새로 맞힘/새로 틀림)

골든셋의 정답 키(unit_key)는 평가용이다. 여기서 실제 추출 단위(source)·블록 ID로 연결한다.
- 정답 단위 ↔ source: 원문 ID가 같은 source 중 제목이 맞는 것 (ID만으로 연결하지 않음)
- 비교는 NFC + 공백·기호 제거 뒤 부분 문자열 (골든셋 normalization 정책)
채점 기준은 evals/golden/사용안내.md를 따른다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tracemalloc
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.schemas.document import DocumentBlock, RequirementSource
from app.schemas.requirement import Evidence, Requirement
from app.agents.tools.doc_quality import build_sources, check_document
from app.agents.tools.document_parser import document_codes, parse_document, textless_pages

ROOT = Path(__file__).resolve().parents[1]
# 문서별 골든셋 (evals/golden/doc<번호>/document_analyst.json)
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
DOC_ORDER = ["5", "3", "1", "2", "6"]     # 결과 표의 문서 순서 (이전 결과와 같게, 새 문서는 뒤에)
DEFAULT_DOCS = "3,5"


def doc_numbers(spec: str) -> list[str]:
    """'3,5' · 'all' → 문서 번호 목록 (항상 DOC_ORDER 순서)."""
    wanted = DOC_ORDER if spec == "all" else [n.strip() for n in spec.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in DOC_ORDER]
    if unknown:
        raise SystemExit(f"없는 문서 번호: {unknown} (쓸 수 있는 번호: {', '.join(sorted(DOC_ORDER))}, all)")
    return [n for n in DOC_ORDER if n in wanted]


def load_golden(spec: str) -> dict:
    """문서별 골든셋 파일들을 하나로 합친다 (문서·정답 단위·사례 목록을 이어 붙임)."""
    parts = [json.loads((GOLDEN_DIR / f"doc{n}" / "document_analyst.json").read_text(encoding="utf-8"))
             for n in doc_numbers(spec)]
    merged = {"meta": {"version": "+".join(g["meta"]["version"] for g in parts)},
              "documents": [], "reference_units": [], "cases": []}
    for g in parts:
        for key in ("documents", "reference_units", "cases"):
            merged[key] += g[key]
    ids = [c["id"] for c in merged["cases"]]
    assert len(ids) == len(set(ids)), "골든셋끼리 사례 ID가 겹침"
    return merged


OUT = ROOT / "output" / "eval"
REQ_BATCH_SIZE = 3            # 다음 Agent(요구사항 추출) 배치 크기: 비교를 위해 고정
SEGMENT_MIN = 6               # 이보다 짧은 조각은 내용 보존 채점에서 뺀다 (기호·단어 조각)
CRITICAL = re.compile(r"\d|단[,\s]|다만|경우|제외|이내|이상|이하|초과|미만|불가|금지|아니|않|없")
# 원문 조각 경계: 항목 기호, 줄바꿈, 두 칸 이상 공백
SPLIT = re.compile(r"\n|[○〇●□■◦•▪▶※]|\s{2,}|(?<=\s)-\s")


def norm(text: str) -> str:
    """비교용 정규화: NFC, 공백·기호 제거, 소문자."""
    text = unicodedata.normalize("NFC", text or "")
    return re.sub(r"[\s\-·ㆍ‧∙○〇●□■◦•▪▶※*\"'“”‘’()\[\]{}<>〈〉「」.,:;/|~]+", "", text).lower()


def segments(reference_text: str) -> list[str]:
    """정답 원문을 내용 보존 채점용 조각으로 나눈다 (칸 이름 '산출정보:'·'[계속: 2/2]' 표식은 뺀다)."""
    text = re.sub(r"^산출정보:\s*", "", reference_text, flags=re.M)
    text = re.sub(r"\[계속:[^\]]*\]", "\n", text)
    return [s.strip() for s in SPLIT.split(text) if len(norm(s)) >= SEGMENT_MIN]


def _doc_path(name: str) -> Path:
    for folder in (ROOT / "data", ROOT / "samples"):
        if (folder / name).exists():
            return folder / name
    raise FileNotFoundError(name)


# ------------------------------------------------------------------ 문서 처리
class Doc:
    """한 문서의 full 모드 결과와, 정답 단위 ↔ source 연결."""

    def __init__(self, meta: dict, units: list[dict], memory: bool = False):
        self.meta, self.units = meta, units
        self.path = _doc_path(meta["file_name"])
        t0 = time.perf_counter()
        self.blocks, self.sources, self.findings = self._analyze()
        self.seconds = time.perf_counter() - t0
        self.peak_mb = None
        if memory:      # tracemalloc은 처리를 몇 배 느리게 하므로 시간과 따로 잰다
            tracemalloc.start()
            self._analyze()
            self.peak_mb = tracemalloc.get_traced_memory()[1] / 2**20
            tracemalloc.stop()
        self.bmap = {b.source_block_id: b for b in self.blocks}
        self.src_text = [norm("\n".join(self.bmap[b].text for b in s.block_ids)) for s in self.sources]
        self.match = self._match_units()

    def _analyze(self):
        """document_analyst._analyze와 같은 순서: 파싱 → 추출 단위 → 문서 품질 검사."""
        blocks = parse_document(self.path)
        sources = build_sources(blocks)
        return blocks, sources, check_document(blocks, sources, document_codes(self.path), textless_pages(self.path))

    def _match_units(self) -> dict[str, int | None]:
        """정답 단위 → source 번호. 같은 원문 ID 중 제목이 맞는 것, 없으면 내용이 가장 많이 겹치는 것."""
        out, taken = {}, set()
        for u in self.units:
            segs = [norm(s) for s in segments(u["reference_text"])]
            def overlap(i):
                return sum(s in self.src_text[i] for s in segs)
            same_id = [i for i, s in enumerate(self.sources) if s.code == u["requirement_id"] and i not in taken]
            titled = [i for i in same_id if _same_title(self.sources[i].name, u["title"])]
            pool = titled or same_id
            best = max(pool, key=overlap, default=None)
            if best is None:          # 원문 ID를 못 읽은 경우: 내용으로 찾되 절반 이상 겹칠 때만
                cand = max(range(len(self.sources)), key=overlap, default=None)
                best = cand if cand is not None and segs and overlap(cand) >= len(segs) / 2 else None
            out[u["unit_key"]] = best
            if best is not None:
                taken.add(best)
        return out

    def source_of(self, unit_key: str) -> RequirementSource | None:
        i = self.match.get(unit_key)
        return self.sources[i] if i is not None else None

    def blocks_of(self, unit_keys: list[str]) -> set[str]:
        return {b for k in unit_keys if (s := self.source_of(k)) for b in s.block_ids}


def _same_title(a: str, b: str) -> bool:
    x, y = norm(a), norm(b)
    return bool(x) and bool(y) and (x == y or x in y or y in x)


# ------------------------------------------------------------------ full 모드 채점
def score_extraction(doc: Doc, unit: dict) -> dict:
    """정답 단위 하나: 원문 ID·제목·분류·내용·출처 보존과 다른 단위 섞임."""
    i = doc.match.get(unit["unit_key"])
    segs = segments(unit["reference_text"])
    if i is None:
        return {"unit_key": unit["unit_key"], "found": False, "content": 0.0, "critical": 0.0, "missing": segs[:5],
                "ok": False}
    src, text = doc.sources[i], doc.src_text[i]
    missing = [s for s in segs if norm(s) not in text]
    crit = [s for s in segs if CRITICAL.search(s)]
    crit_missing = [s for s in crit if norm(s) not in text]
    # 섞임: 다른 정답 단위의 조각이 절반 이상 이 source에 들어 있으면 잘못 병합
    mixed = []
    for other in doc.units:
        if other["unit_key"] == unit["unit_key"]:
            continue
        own = {norm(s) for s in segs}
        oseg = [norm(s) for s in segments(other["reference_text"]) if norm(s) not in own and len(norm(s)) >= 15]
        if oseg and sum(s in text for s in oseg) >= max(2, len(oseg) / 2):
            mixed.append(other["unit_key"])
    loc_ok = _location_ok(doc, unit, src)
    title_ok = _same_title(src.name, unit["title"])
    category_ok = _same_title(src.category, unit.get("category_original", ""))
    row = {"unit_key": unit["unit_key"], "found": True, "source": src.source_key,
           "id_ok": src.code == unit["requirement_id"], "title_ok": title_ok, "category_ok": category_ok,
           "content": 1 - len(missing) / (len(segs) or 1), "critical": 1 - len(crit_missing) / (len(crit) or 1),
           "missing": missing, "mixed_with": mixed, "location_ok": loc_ok}
    row["ok"] = row["id_ok"] and title_ok and category_ok and not missing and not mixed and loc_ok is not False
    return row


def _location_ok(doc: Doc, unit: dict, src: RequirementSource) -> bool | None:
    """PDF: source 첫 블록의 쪽이 정답 첫 위치의 쪽과 같고, 정답 쪽이 모두 source 블록 쪽에 들어 있는가.
    HWPX: 쪽 정보가 없어 표 순서로 본다 (정답 단위의 표 순번 순서 = source 순서)."""
    locs = unit.get("locations") or []
    pages = [l["pdf_page_1based"] for l in locs if "pdf_page_1based" in l]
    if pages:
        got = [doc.bmap[b].location.page for b in src.block_ids]
        return got[0] == pages[0] and set(pages) <= set(got)
    if any("table_index_0based" in l for l in locs):
        order = sorted(doc.units, key=lambda u: min(l.get("table_index_0based", 1e9) for l in u["locations"]))
        rank = [u["unit_key"] for u in order].index(unit["unit_key"])
        found = sorted((i for i in doc.match.values() if i is not None))
        return doc.sources.index(src) == found[rank] if rank < len(found) else False
    return None


def score_chunk(doc: Doc, case: dict) -> dict:
    exp = case["expected_output"]
    i = doc.match.get(exp["unit_key"])
    phrases = exp["must_include_together"]
    missing = [p for p in phrases if i is None or norm(p) not in doc.src_text[i]]
    holders = {doc.sources[j].source_key for p in phrases for j, t in enumerate(doc.src_text) if norm(p) in t}
    return {"id": case["id"], "ok": i is not None and not missing, "missing": missing, "spread_over": sorted(holders)}


QUALITY_KINDS = {"duplicate_id_distinct_requirements": "DOC_DUPLICATE_REQ_CODE",
                 "identifier_category_inconsistency": "DOC_CODE_CATEGORY_MISMATCH",
                 "identifier_prefix_inconsistency": "DOC_CODE_PREFIX_INCONSISTENT"}


def score_quality(doc: Doc, case: dict, unit_by_key: dict) -> dict:
    """문서 품질 사례. required/review는 해당 경고가 나와야, negative는 나오지 않아야 통과."""
    exp = case["expected_output"]
    kind, units = exp["issue_type"], exp["affected_unit_keys"]
    codes = {unit_by_key[k]["requirement_id"] for k in units}
    keys = {s.source_key for k in units if (s := doc.source_of(k))}
    fired = [f for f in doc.findings if f.kind.startswith("DOC_")]
    if kind in QUALITY_KINDS:
        hit = any(f.kind == QUALITY_KINDS[kind] and (f.target_id in codes or f.target_id in keys) for f in fired)
    elif kind == "continuation_is_not_duplicate":          # 이어지는 표가 별도 source·중복 경고가 되면 안 됨
        code = next(iter(codes))
        hit = sum(s.code == code for s in doc.sources) > sum(u["requirement_id"] == code for u in doc.units)
    elif kind == "dangling_reference":                    # 문서에 없는 요구사항 번호를 참조: 그 번호를 짚는 경고가 있어야 함
        hit = any(exp["referenced_id"] in f.message for f in fired)
    elif kind == "no_duplicate_ids":
        hit = any(f.kind == "DOC_DUPLICATE_REQ_CODE" for f in fired)
    elif kind == "overlapping_content":                   # 다른 표·그림이 글자를 덮음: 그 요구사항을 짚는 경고가 있어야 함
        hit = any(f.target_id in codes | keys or any(c in f.message for c in codes) for f in fired)
    elif kind == "no_text_layer_absent_pages":
        hit = any(f.kind in ("DOC_TEXTLESS_PAGES", "DOC_NO_TEXT") for f in fired)
    elif kind == "nested_table_is_not_requirement":
        hit = len(doc.sources) > len(doc.units) or len(keys) != len(units)
    else:
        hit = None
    expect_hit = exp["expectation"] in ("required", "review")
    return {"id": case["id"], "issue_type": kind, "expectation": exp["expectation"], "fired": hit,
            "ok": None if hit is None else hit == expect_hit}


# ------------------------------------------------------------------ research 모드 채점
def score_research(docs: dict[str, Doc], cases: list[dict], unit_by_key: dict) -> list[dict]:
    """research 사례마다 요구사항 1건(근거 1개)을 만들어 document_analyst.run(mode=research)에 넣는다."""
    from app.agents.roles import document_analyst
    from app.agents.tools.search import search_blocks

    rows = []
    for case in cases:
        inp, exp = case["input"], case["expected_output"]
        doc = docs[inp["document_id"]]
        claimed_unit = _unit_at(inp["original_locator"], doc.units)
        claimed_src = doc.source_of(claimed_unit) if claimed_unit else None
        claimed = claimed_src.block_ids[0] if claimed_src else doc.blocks[0].source_block_id
        req = Requirement(requirement_id="T-0001", type="FUNCTIONAL", scope="SYSTEM", category="기타",
                          title=inp["quote"][:30], description="",
                          evidence=[Evidence(source_block_id=claimed, quote=inp["quote"])])
        state = {"blocks": [b.model_dump() for b in doc.blocks], "requirements": [req.model_dump()],
                 "next_action": {"mode": "research", "scope": ["T-0001"]}}
        ev = document_analyst.run(state)["requirements"][0]["evidence"][0]
        if not ev["verified"]:
            action = "unconfirmed"
        elif ev["source_block_id"] == claimed and norm(ev["quote"]) == norm(inp["quote"]):
            action = "verified"
        else:
            action = "relinked"
        allowed = doc.blocks_of(exp["allowed_target_unit_keys"])
        top = [b.source_block_id for _, b in search_blocks(doc.blocks, inp["quote"], top_k=3)]
        if exp["action"] == "unconfirmed":
            ok = action == "unconfirmed" and (not exp.get("preserve_original_quote") or ev["quote"] == inp["quote"])
        else:
            ok = (action == exp["action"] and ev["source_block_id"] in allowed
                  and norm(exp["expected_quote"]) in norm(ev["quote"]))
        rows.append({"id": case["id"], "type": case["metadata"]["type"], "expected": exp["action"], "action": action,
                     "ok": ok, "quote": inp["quote"], "final_quote": ev["quote"],
                     "final_unit": next((k for k in unit_by_key if ev["source_block_id"] in doc.blocks_of([k])), None),
                     "hit1": bool(allowed) and top[:1] and top[0] in allowed,
                     "hit3": bool(allowed) and any(b in allowed for b in top)})
    return rows


def _unit_at(locator: dict, units: list[dict]) -> str | None:
    """사례의 original_locator(원문 ID + 위치)에 해당하는 정답 단위."""
    for u in units:
        if u["requirement_id"] == locator["requirement_id"] and any(l in u["locations"] for l in locator["locations"]):
            return u["unit_key"]
    return next((u["unit_key"] for u in units if u["requirement_id"] == locator["requirement_id"]), None)


# ------------------------------------------------------------------ 후속 Agent 전달 비용
def delivery_cost(doc: Doc) -> dict:
    """요구사항 Agent가 보낼 사용자 프롬프트 (requirements_analyst._extract와 같은 구성, 배치 크기 고정)."""
    from app.agents.roles.requirements_analyst import PREFIX_HINT
    prompts = []
    for i in range(0, len(doc.sources), REQ_BATCH_SIZE):
        parts = [f"문서: {doc.meta['file_name']}", PREFIX_HINT, "", "추출 대상 원문:"]
        for src in doc.sources[i:i + REQ_BATCH_SIZE]:
            parts.append(f'<source key="{src.source_key}" heading="{src.heading}">')
            parts += [f'<block id="{bid}">\n{doc.bmap[bid].text}\n</block>' for bid in src.block_ids]
            parts.append("</source>")
        prompts.append("\n".join(parts))
    uses = Counter(b for s in doc.sources for b in s.block_ids)
    return {"seconds": round(doc.seconds, 2), "peak_mb": round(doc.peak_mb, 1) if doc.peak_mb is not None else None, "llm_calls": len(prompts),
            "prompt_chars": sum(map(len, prompts)), "repeated_blocks": sum(n > 1 for n in uses.values())}


# ------------------------------------------------------------------ 실행·보고
def evaluate(memory: bool = False, spec: str = DEFAULT_DOCS) -> dict:
    g = load_golden(spec)
    unit_by_key = {u["unit_key"]: u for u in g["reference_units"]}
    docs = {d["document_id"]: Doc(d, [u for u in g["reference_units"] if u["document_id"] == d["document_id"]], memory)
            for d in g["documents"]}
    by_type: dict[str, list[dict]] = {}
    for c in g["cases"]:
        by_type.setdefault("research" if c["input"]["mode"] == "research" else c["metadata"]["type"], []).append(c)

    extraction = [{"id": c["id"], **score_extraction(docs[c["input"]["document_id"]],
                                                       unit_by_key[c["expected_output"]["unit_key"]])}
                  for c in by_type.get("요구사항 추출 및 내용 보존", [])]
    full = []
    for c in by_type.get("전체 추출 목록", []):
        doc = docs[c["input"]["document_id"]]
        expected = c["expected_output"]["expected_unit_keys"]
        rows = [r for r in extraction if r["unit_key"] in expected]
        matched = {doc.match[k] for k in expected if doc.match.get(k) is not None and
                   next(r for r in rows if r["unit_key"] == k).get("title_ok")}
        full.append({"id": c["id"], "document": doc.meta["file_name"], "expected": len(expected),
                     "sources": len(doc.sources), "recall": len(matched) / len(expected),
                     "precision": len(matched) / (len(doc.sources) or 1),
                     "extra_sources": [s.source_key for i, s in enumerate(doc.sources) if i not in matched],
                     "cost": delivery_cost(doc),
                     "unlabeled_findings": [f"{f.kind} {f.target_id}" for f in doc.findings
                                            if f.kind.startswith("DOC_") and not _labeled(f, doc, g["cases"], unit_by_key)]})
    chunks = [score_chunk(docs[c["input"]["document_id"]], c) for c in by_type.get("청킹 완결성", [])]
    quality = [score_quality(docs[c["input"]["document_id"]], c, unit_by_key) for c in by_type.get("문서 품질 검사", [])]
    research = score_research(docs, by_type.get("research", []), unit_by_key)
    return {"golden": g["meta"]["version"], "docs": ",".join(doc_numbers(spec)), "at": datetime.now().isoformat(timespec="seconds"),
            "summary": summarize(full, extraction, chunks, research, quality),
            "full": full, "extraction": extraction, "chunks": chunks, "research": research, "quality": quality}


def _labeled(f, doc: Doc, cases: list[dict], unit_by_key: dict) -> bool:
    """골든셋 품질 사례가 다루는 경고인가 (종류와 대상이 같음)."""
    for c in cases:
        exp = c["expected_output"]
        if c["metadata"]["type"] != "문서 품질 검사" or QUALITY_KINDS.get(exp.get("issue_type")) != f.kind:
            continue
        units = exp["affected_unit_keys"]
        targets = {unit_by_key[k]["requirement_id"] for k in units} | {s.source_key for k in units if (s := doc.source_of(k))}
        if f.target_id in targets:
            return True
    return False


def summarize(full, extraction, chunks, research, quality) -> dict:
    def rate(rows, key="ok"):
        return f"{sum(bool(r[key]) for r in rows)}/{len(rows)}"
    exp = {a: [r for r in research if r["expected"] == a] for a in ("verified", "relinked", "unconfirmed")}
    relinks = [r for r in research if r["action"] == "relinked"]
    found = [r for r in extraction if r["found"]]
    return {
        "요구사항 추출 재현율": " · ".join(f"{f['document']} {f['recall']:.3f}" for f in full),
        "추출 정밀도": " · ".join(f"{f['document']} {f['precision']:.3f}" for f in full),
        "추출 단위 완결성(내용 조각)": round(sum(r["content"] for r in found) / (len(found) or 1), 4),
        "숫자·조건 조각 보존": round(sum(r["critical"] for r in found) / (len(found) or 1), 4),
        "요구사항 사례 통과": rate(extraction),
        "청킹 통과": rate(chunks),
        "기존 근거 확인": rate(exp["verified"]),
        "자동 재연결 정밀도": f"{sum(r['ok'] for r in relinks if r['expected'] == 'relinked')}/{len(relinks)}",
        "복구 재현율": rate(exp["relinked"]),
        "잘못된 검증 통과": f"{sum(r['action'] != 'unconfirmed' for r in exp['unconfirmed'])}/{len(exp['unconfirmed'])}",
        "검색 Hit@1 / Hit@3": f"{rate([r for r in research if r['expected'] != 'unconfirmed'], 'hit1')} / "
                             f"{rate([r for r in research if r['expected'] != 'unconfirmed'], 'hit3')}",
        "문서 품질 통과": rate([q for q in quality if q["ok"] is not None]),
        "문서당 후속 프롬프트(자)": " · ".join(f"{f['document']} {f['cost']['prompt_chars']:,}" for f in full),
        "처리 시간(s)": " · ".join(f"{f['document']} {f['cost']['seconds']}" for f in full),
        "최대 메모리(MB)": " · ".join(f"{f['document']} {f['cost']['peak_mb']}" for f in full),
    }


def report(result: dict, previous: dict | None) -> str:
    docs = "고객요구사항" + "·".join(sorted(result.get("docs", "3,5").split(",")))
    lines = [f"# document_analyst 평가 ({docs}, 골든셋 v{result['golden']}, {result['at']})", "", "| 지표 | 값 |" +
             (" 이전 |" if previous else ""), "| --- | --- |" + (" --- |" if previous else "")]
    for k, v in result["summary"].items():
        lines.append(f"| {k} | {v} |" + (f" {previous['summary'].get(k, '-')} |" if previous else ""))
    if previous:
        lines += ["", "## 변경 전후 사례", ""]
        for part in ("extraction", "chunks", "research", "quality"):
            before = {r["id"]: r["ok"] for r in previous.get(part, [])}
            fixed = [r["id"] for r in result[part] if r["ok"] and before.get(r["id"]) is False]
            broken = [r["id"] for r in result[part] if r["ok"] is False and before.get(r["id"])]
            lines.append(f"- {part}: 새로 맞힘 {fixed or '없음'} / 새로 틀림 {broken or '없음'}")
    lines += ["", "## 실패 사례", ""]
    for r in result["extraction"]:
        if not r["ok"]:
            why = [k for k in ("id_ok", "title_ok", "category_ok") if r.get(k) is False]
            why += ["내용 누락"] * bool(r.get("missing")) + [f"섞임 {r['mixed_with']}"] * bool(r.get("mixed_with"))
            why += ["위치"] * (r.get("location_ok") is False) + ["못 찾음"] * (not r["found"])
            lines.append(f"- {r['id']} {r['unit_key']}: {', '.join(why)}"
                         + "".join(f"\n  - 누락: {m[:90]}" for m in r.get("missing", [])[:3]))
    for r in result["chunks"]:
        if not r["ok"]:
            lines.append(f"- {r['id']}: 누락 {r['missing']} (들어 있는 곳 {r['spread_over']})")
    for r in result["research"]:
        if not r["ok"]:
            lines.append(f"- {r['id']} [{r['type']}] 기대 {r['expected']} → {r['action']} "
                         f"'{r['quote'][:40]}' → '{r['final_quote'][:50]}' ({r['final_unit']})")
    for r in result["quality"]:
        if r["ok"] is not True:
            lines.append(f"- {r['id']} [{r['issue_type']}/{r['expectation']}] 경고 {r['fired']}")
    for f in result["full"]:
        lines.append(f"- {f['document']} 골든셋에 없는 경고: {f['unlabeled_findings'] or '없음'}; 남는 source: {f['extra_sources'] or '없음'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m evals.eval_document_analyst")
    ap.add_argument("--compare", help="이전 결과 JSON")
    ap.add_argument("--label", default="")
    ap.add_argument("--memory", action="store_true", help="최대 메모리도 잰다 (느림)")
    ap.add_argument("--docs", default=DEFAULT_DOCS, help="채점할 문서 번호 (예: 3,5 · 1,2 · all). 기본 3,5")
    args = ap.parse_args(argv)
    result = evaluate(args.memory, args.docs)
    previous = json.loads(Path(args.compare).read_text(encoding="utf-8")) if args.compare else None
    OUT.mkdir(parents=True, exist_ok=True)
    tag = "" if doc_numbers(args.docs) == doc_numbers(DEFAULT_DOCS) else "-docs" + "".join(sorted(doc_numbers(args.docs)))
    name = datetime.now().strftime("%Y%m%d-%H%M%S") + tag + (f"-{args.label}" if args.label else "")
    (OUT / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    text = report(result, previous)
    (OUT / f"{name}.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"\n저장: {OUT / name}.json")


if __name__ == "__main__":
    main()
