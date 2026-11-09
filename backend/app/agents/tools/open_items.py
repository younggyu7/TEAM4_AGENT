"""최종 승인 전에 남은 확인 항목 (결정적, LLM 없음).

최종 산출물 화면에서 바로 답하고 승인한다 (앞 단계로 돌아가지 않는다). 개발 단계로 '확인 필요'가 넘어가지 않게 하는 곳이다.
- 항목: 답 없는 질문(기술 스택·데이터 설계 질문은 제외), '(확인 필요)' 규칙, 근거가 '확인 필요'인 상태 전환·자동 처리, 출처 '미정'인 값.
  같은 규칙 문장·같은 값은 한 항목으로 묶는다.
- 답(apply)은 design.json의 질문·규칙·업무 동작·값 출처를 고친다. 개발 스펙은 호출한 쪽이 다시 쓴다 (exporter.resolve_open_items).
- 값 출처에 '개발 담당자가 정한다'를 고르면 미정으로 남고, 개발 AI가 구현하지 못하면 작업 묶음의 TODO로 사람에게 간다.
"""
from __future__ import annotations

import hashlib
import re

from app.agents.tools.dev_spec import rule_tag

CONFIRM = "문장대로 확정"
LATER = "개발 담당자가 구현하며 정한다"
SKIP = ("TQ-", "DQ-")      # 기술 스택은 기능 명세에서, 데이터 설계 질문은 화면에서 답하지 않는다


def _open_questions(design: dict) -> dict[str, dict]:
    return {q["question_id"]: q for q in design.get("questions") or []
            if q.get("status") != "ANSWERED" and not q["question_id"].startswith(SKIP)}


def _ops(design: dict):
    for fid, entry in ((design.get("api_design") or {}).get("functions") or {}).items():
        for op in entry.get("operations") or []:
            if not op.get("shared_with"):
                yield fid, op


def _behavior(design: dict) -> dict:
    return (design.get("api_design") or {}).get("behavior") or {}


def items(design: dict) -> list[dict]:
    """남은 항목 [{id, kind, title, detail, options, recommended, where}]. recommended가 비면 답을 적어야 승인된다"""
    if not (design.get("api_design") or {}).get("functions"):
        return []
    questions = _open_questions(design)
    used: dict[str, list[str]] = {}
    rules: dict[str, dict] = {}
    for f in design.get("functions") or []:
        for rule in f.get("business_rules") or []:
            kind, text, qids = rule_tag(rule)
            if kind != "보류":
                continue
            for q in qids:
                used.setdefault(q, []).append(f["function_id"])
            if not any(q in questions for q in qids):
                rules.setdefault(text, {"text": text, "where": []})["where"].append(f["function_id"])
    out = [{"id": qid, "kind": "question", "title": q["question"], "detail": q.get("context") or "", "options": q.get("options") or [],
            "recommended": q.get("recommended") or "", "where": list(dict.fromkeys(used.get(qid, [])))} for qid, q in questions.items()]
    out += [{"id": "rule:" + hashlib.sha1(r["text"].encode()).hexdigest()[:12], "kind": "rule", "title": r["text"], "detail": "업무 규칙",
             "options": [CONFIRM], "recommended": CONFIRM, "where": list(dict.fromkeys(r["where"]))} for r in rules.values()]
    motion = _behavior(design)
    for a in motion.get("automations") or []:
        if a.get("basis") == "확인 필요" and a.get("question_id") not in questions:
            out.append({"id": f"auto:{a['id']}", "kind": "automation", "title": f"{a['name']}: {a['action']}", "detail": "자동 처리",
                        "options": [CONFIRM], "recommended": CONFIRM, "where": a.get("function_ids") or []})
    for s in motion.get("states") or []:
        if s.get("question") and s.get("basis") != "결정" and s.get("question_id") not in questions:
            out.append({"id": f"state:{s['column']}", "kind": "state", "title": s["question"], "detail": f"상태 전환 ({s['column']})",
                        "options": [LATER], "recommended": s.get("recommended") or LATER, "where": []})
    sources: dict[str, list[str]] = {}
    for fid, op in _ops(design):
        for s in op.get("sources") or []:
            if s["kind"] == "미정" and s.get("basis") != "결정":
                sources.setdefault(s["target"], []).append(fid)
    out += [{"id": f"source:{target}", "kind": "source", "title": f"'{target}' 값은 어디서 가져오나요?", "detail": "값의 출처",
             "options": [LATER], "recommended": LATER, "where": list(dict.fromkeys(fids))} for target, fids in sources.items()]
    return out


def _decide(rule: str, answer: str | None) -> str:
    """'(확인 필요 …) 문장' → '(결정 …) 문장'. answer가 있으면 문장을 답으로 바꾼다"""
    m = re.match(r"^\(([^)]*)\)\s*", rule)
    if m and "확인 필요" in m.group(1):
        return f"({m.group(1).replace('확인 필요', '결정')}) {answer or rule[m.end():]}"
    return "(결정) " + (answer or re.sub(r"\s*\(확인 필요[^)]*\)", "", rule))


def apply(design: dict, answers: dict[str, str]) -> list[dict]:
    """답을 design에 반영한다 (design을 고침). 답도 추천도 없는 항목이 있으면 ValueError. → 반영한 기록 [{id, title, answer}]"""
    found = items(design)
    chosen = {i["id"]: (str(answers.get(i["id"]) or "").strip() or i["recommended"]).strip()[:2000] for i in found}
    if empty := [i for i in found if not chosen[i["id"]]]:
        raise ValueError("답을 적어야 하는 항목이 남았습니다: " + ", ".join(i["id"] for i in empty[:10]))
    questions = _open_questions(design)
    by_id = {i["id"]: i for i in found}
    for qid, q in questions.items():
        answer = chosen[qid]
        q.update(status="ANSWERED", answer=answer)
        other = answer if answer != by_id[qid]["recommended"] else None
        for f in design.get("functions") or []:
            f["business_rules"] = [_decide(r, other) if rule_tag(r)[0] == "보류" and qid in rule_tag(r)[2] else r for r in f.get("business_rules") or []]
    for i in found:
        if i["kind"] == "rule":
            other = chosen[i["id"]] if chosen[i["id"]] != CONFIRM else None
            for f in design.get("functions") or []:
                f["business_rules"] = [_decide(r, other) if rule_tag(r)[0] == "보류" and rule_tag(r)[1] == i["title"] else r for r in f.get("business_rules") or []]
    motion = _behavior(design)
    for a in motion.get("automations") or []:
        if (answer := chosen.get(f"auto:{a['id']}")) is not None:
            a.update(basis="결정", **({} if answer == CONFIRM else {"answer": answer}))
    for s in motion.get("states") or []:
        if (answer := chosen.get(f"state:{s['column']}")) is not None:
            s.update(basis="결정", answer=answer)
    for _, op in _ops(design):
        for s in op.get("sources") or []:
            answer = chosen.get(f"source:{s['target']}")
            if answer is None or s["kind"] != "미정":
                continue
            s.update(basis="결정", value=answer, kind="미정" if answer == LATER else "계산")   # 계산: 코드 틀은 값을 지어내지 않고 개발 AI가 내용대로 짠다
    return [{"id": i["id"], "title": i["title"], "answer": chosen[i["id"]]} for i in found]
