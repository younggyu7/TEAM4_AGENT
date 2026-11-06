"""수정 초안 함께 쓰기: 버전별 초안, 변경 내역(누가·언제·무엇을), 개발자의 기능 명세 수정 완료 승인 요청.

초안 파일(프로젝트 파일 draft-v<버전>.json):
    {"state": 화면 초안, "rev": 저장 번호, "updated_at", "updated_by",
     "log": [{"by", "at", "changes": [{"target", "label", "what"}]}],   # 최신이 끝, LOG_LIMIT개까지
     "request": {"by", "at", "note"} | null,                           # 개발자의 수정 완료 승인 요청 (기능 명세)
     "rejection": {"by", "at", "reason", "request"} | null}            # 마지막 반려 (새 요청·확정이면 지움)

- 변경 내역은 저장할 때 서버가 이전 초안과 비교해 만든다. 같은 사람이 MERGE_MINUTES 안에 이어 고치면 한 묶음에 합친다.
- 개발자가 요청한 뒤 다시 고치면 요청은 취소된다 (고친 것을 보고 승인해야 하므로). PM이 고치는 것은 취소하지 않는다.
- 기능 명세 확정(내보내기)은 PM만 한다 (요청이 없어도 된다). 개발자는 고친 뒤 승인 요청을 보내 PM에게 알린다.
  ERD 최종 확정은 개발자·PM이 승인 요청 없이 한다 (ERD는 승인 요청을 받지 않는다: REQUEST_STAGES).
- PM은 요청을 반려할 수 있다 (사유 필수). 요청은 닫히고 개발자는 고친 뒤 다시 요청한다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.agents.storage import get_store
from app.core.auth import PM, role_of
from app.core.exceptions import Forbidden

LOG_LIMIT = 300
MERGE_MINUTES = 10
HEADS = {PM}   # 고쳐도 승인 요청을 취소하지 않는 역할 (요청을 받는 쪽)
REQUEST_STAGES = {"spec"}   # 수정 완료 승인 요청을 받는 단계 (ERD는 개발자가 승인 요청 없이 확정)

FUNCTION_FIELDS = {"name": "이름", "description": "설명", "actors": "행위자", "preconditions": "선행 조건", "inputs": "입력",
                   "outputs": "출력", "business_rules": "업무 규칙", "normal_flow": "처리 순서", "exception_flows": "예외 처리",
                   "data_objects": "사용 데이터"}
ENTITY_FIELDS = {"label": "한글 이름", "description": "설명", "kind": "종류", "unique_together": "조합 유일",
                 "exactly_one_of": "정확히 하나", "required_when": "조건부 필수", "checks": "값 조건"}
CHOICES = {"applied": "반영함", "not_issue": "문제 아님", "later": "미해결로 둠"}


def _name(version: str) -> str:
    return f"draft-v{version}.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _who(member) -> dict:
    return {"id": member.id, "name": member.name, "role": member.role}


def read(project: str, version: str) -> dict:
    text = get_store().read_project_file(project, _name(version))
    doc = json.loads(text) if text else {}
    return {"state": doc.get("state"), "rev": doc.get("rev", 0), "updated_at": doc.get("updated_at"),
            "updated_by": doc.get("updated_by"), "log": doc.get("log") or [], "request": doc.get("request"),
            "rejection": doc.get("rejection")}


def _write(project: str, version: str, doc: dict) -> None:
    doc["log"] = doc["log"][-LOG_LIMIT:]
    get_store().write_project_file(project, _name(version), json.dumps(doc, ensure_ascii=False))


def _columns(old: list[dict], new: list[dict]) -> str:
    """컬럼 변경을 사람이 읽을 글로: 추가·삭제·바뀐 컬럼 이름"""
    before = {a.get("name"): a for a in old or []}
    after = {a.get("name"): a for a in new or []}
    parts = []
    if added := [n for n in after if n not in before]:
        parts.append(f"컬럼 추가 {', '.join(added)}")
    if removed := [n for n in before if n not in after]:
        parts.append(f"컬럼 삭제 {', '.join(removed)}")
    if changed := [n for n in after if n in before and after[n] != before[n]]:
        parts.append(f"컬럼 수정 {', '.join(changed)}")
    return " · ".join(parts) or "컬럼 순서"


def _dicts(value) -> dict:
    return {k: v for k, v in value.items() if isinstance(v, dict)} if isinstance(value, dict) else {}


def diff(old: dict | None, new: dict | None, base: dict | None = None) -> list[dict]:
    """이전 초안 → 새 초안에서 바뀐 것 [{target, label, what}] (기능·테이블·질문 답·지적 처리·확인 표시).
    base: 그 버전의 원본 {"functions": {ID: 기능}, "entities": {ID: 테이블}} — 처음 고친 기능은 원본과 비교한다."""
    old, new, base = old or {}, new or {}, base or {}
    changes: list[dict] = []
    for kind, fields in (("functions", FUNCTION_FIELDS), ("entities", ENTITY_FIELDS)):
        before, after = _dicts(old.get(kind)), _dicts(new.get(kind))
        for target in dict.fromkeys([*before, *after]):
            a, b = before.get(target), after.get(target)
            if a == b:
                continue
            item = b or a or {}
            label = item.get("name" if kind == "functions" else "label") or ""
            if b is None:
                what = "원본으로 되돌림"
            else:
                a = a or (base.get(kind) or {}).get(target) or {}
                # 원본을 모르면(버전에 없는 기능) '수정'만. 알면 사람이 고치는 칸 이름으로
                keys = [k for k in dict.fromkeys([*a, *b]) if a and a.get(k) != b.get(k) and (k in fields or k == "attributes")]
                parts = [_columns(a.get("attributes"), b.get("attributes")) if k == "attributes"
                         else f"{fields.get(k, k)} 수정" for k in keys]
                what = " · ".join(parts) or "수정"
            changes.append({"target": target, "label": label, "what": what})
    for key in ("answers", "dispositions", "checked"):   # 형식이 틀린 값은 비교하지 않는다
        old = {**old, key: old.get(key) if isinstance(old.get(key), dict) else {}}
        new = {**new, key: new.get(key) if isinstance(new.get(key), dict) else {}}
    for qid in dict.fromkeys([*(old.get("answers") or {}), *(new.get("answers") or {})]):
        a, b = (old.get("answers") or {}).get(qid, ""), (new.get("answers") or {}).get(qid, "")
        if a != b:
            changes.append({"target": qid, "label": "확인 질문", "what": f"답: {b[:60]}" if b else "답 지움"})
    for iid in dict.fromkeys([*(old.get("dispositions") or {}), *(new.get("dispositions") or {})]):
        a, b = (old.get("dispositions") or {}).get(iid), (new.get("dispositions") or {}).get(iid)
        if a != b:
            what = f"지적 처리: {CHOICES.get(b.get('choice'), b.get('choice'))}" + (f" ({b['note'][:40]})" if b.get("note") else "") \
                if isinstance(b, dict) else "지적 처리 취소"
            changes.append({"target": iid, "label": "의미 검토 지적", "what": what})
    # 확인 표시는 항목별로 남긴다 (누가 어느 규칙·지적을 체크했는지 화면 '전체 보기'에 보이게). 키: a:기능ID:규칙 · i:지적ID
    for key in dict.fromkeys([*(old.get("checked") or {}), *(new.get("checked") or {})]):
        b = bool((new.get("checked") or {}).get(key))
        if bool((old.get("checked") or {}).get(key)) != b:
            changes.append({"target": key, "label": "확인 표시", "what": f"{'확인함' if b else '확인 해제'} — {_check_text(key)}"})
    return changes


def _check_text(key: str) -> str:
    """확인 표시 키 → 사람이 읽는 대상 ('FN-001 규칙: …' · '지적 SI-003')"""
    kind, _, rest = key.partition(":")
    if kind == "a":
        target, _, rule = rest.partition(":")
        return f"{target} 규칙: {rule[:60]}"
    return f"지적 {rest}" if kind == "i" else key[:60]


def _base(project: str, version: str) -> dict:
    """그 버전의 원본 기능·테이블 (처음 고친 것을 원본과 비교하려고)"""
    from app.services import output_store
    design = output_store.read_design(project, version) or {}
    return {"functions": {f.get("function_id"): f for f in design.get("functions") or []},
            "entities": {e.get("entity_id"): e for e in (design.get("data_model") or {}).get("entities") or []}}


def _log(doc: dict, member, changes: list[dict], at: str) -> None:
    """같은 사람이 MERGE_MINUTES 안에 이어 고치면 마지막 묶음에 합친다 (같은 대상은 최신 내용으로)"""
    if not changes:
        return
    last = doc["log"][-1] if doc["log"] else None
    if last and last["by"]["id"] == member.id and not last.get("event") \
            and datetime.fromisoformat(at) - datetime.fromisoformat(last["at"]) < timedelta(minutes=MERGE_MINUTES):
        merged = {(c["target"], c["label"]): c for c in last["changes"]}
        merged.update({(c["target"], c["label"]): c for c in changes})
        last["changes"], last["at"] = list(merged.values()), at
    else:
        doc["log"].append({"by": _who(member), "at": at, "changes": changes})


def save(project: str, version: str, state: dict, rev: int, member) -> dict | None:
    """초안 저장. rev가 서버와 다르면 None (호출한 쪽이 409). 저장한 문서를 돌려준다."""
    doc = read(project, version)
    if doc["rev"] != rev:
        return None
    at = _now()
    changes = diff(doc["state"], state, _base(project, version))
    _log(doc, member, changes, at)
    if doc["request"] and role_of(member.role) not in HEADS and changes:
        doc["log"].append({"by": _who(member), "at": at, "event": "request_cancelled",
                           "changes": [{"target": "", "label": "승인 요청", "what": "요청 뒤 다시 고쳐 요청이 취소됨"}]})
        doc["request"] = None
    doc.update(state=state, rev=rev + 1, updated_at=at, updated_by=_who(member))
    _write(project, version, doc)
    return doc


def request(project: str, version: str, member, note: str = "") -> dict:
    """개발자의 수정 완료 승인 요청 (같은 버전에 다시 요청하면 새 요청으로 바꾼다)"""
    doc = read(project, version)
    at = _now()
    doc["request"] = {"by": _who(member), "at": at, "note": note.strip()[:500]}
    doc["rejection"] = None
    doc["log"].append({"by": _who(member), "at": at, "event": "request",
                       "changes": [{"target": "", "label": "승인 요청", "what": f"수정 완료 승인 요청{f' — {note.strip()[:80]}' if note.strip() else ''}"}]})
    _write(project, version, doc)
    return doc["request"]


def reject(project: str, version: str, member, reason: str) -> dict | None:
    """PM의 반려: 요청을 닫고 사유를 남긴다. 반려한 요청을 돌려준다 (요청이 없으면 None)"""
    doc = read(project, version)
    req = doc["request"]
    if not req:
        return None
    at, reason = _now(), reason.strip()[:500]
    doc["log"].append({"by": _who(member), "at": at, "event": "request_rejected",
                       "changes": [{"target": "", "label": "승인 요청", "what": f"반려 — {reason[:80]}"}]})
    doc["request"] = None
    doc["rejection"] = {"by": _who(member), "at": at, "reason": reason, "request": req}
    _write(project, version, doc)
    return req


def note_event(project: str, version: str, member, label: str, what: str) -> dict | None:
    """확정을 변경 내역에 남기고 승인 요청을 닫는다 (화면에서 누가 언제 확정했는지 보이게, 같은 요청이 알림에 남지 않게).
    확정으로 승인한 개발자의 요청을 돌려준다 (요청 없이 확정했으면 None)"""
    doc = read(project, version)
    req = doc["request"]
    doc["log"].append({"by": _who(member), "at": _now(), "event": "confirm", "changes": [{"target": "", "label": label, "what": what}]})
    doc["request"], doc["rejection"] = None, None
    _write(project, version, doc)
    return req
