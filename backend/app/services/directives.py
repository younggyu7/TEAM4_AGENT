"""수정 지시의 진행: 어느 단계가 어느 버전에서 처리할지, 옛 ERD 잠금, 항목 처리, 회원별 알림.

수정 지시는 최종본 버전 기록의 approval(decision=changes, targets, items)에 있다. 처리 순서는 '기능 명세 먼저':
  - 기능 명세가 대상이면 개발자가 그 기능 명세(S)를 고쳐 새 기능 명세(S', 내보내기면 ERD S'.1까지)를 만든다.
    그동안 S 계보의 옛 ERD는 잠근다 (지시가 빠진 ERD로 다시 최종본을 만들지 않게).
  - ERD 항목은 기능 명세가 대상이면 새 기능 명세의 ERD(S'.1)가 생긴 뒤 그 ERD에서, ERD만 대상이면 지금 ERD 계보에서 처리한다.
  - 항목마다 '반영함 / 그대로 둠(이유)'을 고른다 (approval.resolutions, LLM 없음). 화면은 필수 확인 항목으로 둔다.
  - 지시 뒤 그 계보에서 새 최종본이 생기면 지시는 끝난 것이다 (새 최종본의 최종 결정 칸에 처리 결과가 보인다).
날짜는 버전 기록의 created_at과 지시 시각(approval.at)을 비교한다 (둘 다 서버 로컬 ISO 시각).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

from app.agents.storage import get_store

from app.agents.tools.versions import load_versions, update_version, version_key
from app.core.auth import DEVELOPER, allowed, role_of
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.services import drafts, output_store, project_access

SPEC_STAGE = "function_designer"
EDIT = {"spec": "spec_edit", "erd": "erd_edit"}         # 그 단계 초안을 고치는 권한
CONFIRM = {"spec": "spec_confirm", "erd": "erd_confirm"}  # 그 단계를 확정하는 권한 (승인 요청을 받는다)


def rejected_people(doc: dict, stage: str) -> set[int]:
    """반려를 알릴 회원: 요청한 개발자 + 그 초안을 고친 개발자 (반려한 사람 빼고, 저장된 예전 역할도 개발자로 읽음). doc = drafts.read 결과"""
    rejection = doc.get("rejection") or {}
    if not rejection:
        return set()
    ids = {((rejection.get("request") or {}).get("by") or {}).get("id")}
    ids |= {(e.get("by") or {}).get("id") for e in doc.get("log") or [] if stage in EDIT and role_of((e.get("by") or {}).get("role")) == DEVELOPER}
    ids.discard((rejection.get("by") or {}).get("id"))
    return {i for i in ids if isinstance(i, int)}


class Tree:
    """한 프로젝트의 버전 계보 (화면 lib/versions.ts의 specOf·stageOf와 같은 규칙)"""

    def __init__(self, project: str):
        self.project = project
        self.records = {str(v["design_version"]): v for v in load_versions(project)}

    @staticmethod
    def _cut(label: str, depth: int) -> str:
        return ".".join(label.split(".")[: depth + 1])

    def spec_of(self, label: str) -> str:
        for d in range(label.count("."), 0, -1):
            cut = self._cut(label, d)
            if (self.records.get(cut) or {}).get("stage") == SPEC_STAGE:
                return cut
        return self._cut(label, 0)

    def stage_of(self, label: str) -> str:
        level = label.count(".") - self.spec_of(label).count(".")
        return ("spec", "erd", "final")[min(level, 2)]

    def descends(self, spec: str, ancestor: str) -> bool:
        """기능 명세 spec이 ancestor를 고쳐 만든 것인지 (자기 자신 포함)"""
        seen = set()
        while spec and spec not in seen:
            if spec == ancestor:
                return True
            seen.add(spec)
            r = self.records.get(spec) or {}
            spec = self.spec_of(str(r["parent"])) if r.get("stage") == SPEC_STAGE and r.get("parent") else ""
        return False

    def after(self, at: str, stage: str, lineage: str) -> list[str]:
        """지시 뒤(at)에 lineage 기능 명세 계보에서 생긴 그 단계 버전 (최신이 먼저)"""
        out = [v for v, r in self.records.items() if (r.get("created_at") or "") > at and self.stage_of(v) == stage
               and self.descends(self.spec_of(v), lineage)]
        return sorted(out, key=version_key, reverse=True)


def _open(tree: Tree) -> list[dict]:
    """끝나지 않은 수정 지시와 진행 상태"""
    out = []
    for final, r in tree.records.items():
        a = r.get("approval") or {}
        if r.get("stage") != "final" or a.get("decision") != "changes":
            continue
        at, spec = a.get("at", ""), tree.spec_of(final)
        if tree.after(at, "final", spec):
            continue   # 지시 뒤 새 최종본이 나왔다 → 끝 (새 최종본에서 최종 승인할 때 확인)
        targets = a.get("targets") or ([a["target"]] if a.get("target") else [])
        resolutions = a.get("resolutions") or {}
        items = [{**i, "resolution": resolutions.get(f"{i['stage']}:{i['target_id']}")} for i in a.get("items") or []]
        new_specs = [v for v in tree.after(at, "spec", spec) if v != spec]
        spec_done = "spec" not in targets or bool(new_specs)
        if "spec" in targets:
            # ERD는 새 기능 명세의 ERD가 생길 때까지 기다린다
            erds = tree.after(at, "erd", spec)
            erds = [e for e in erds if tree.spec_of(e) != spec]
            erd_version, waiting = (erds[0] if erds else None), not erds
        else:
            erds = tree.after(at, "erd", spec)
            erd_version, waiting = (erds[0] if erds else str(r.get("base") or "")), False
        out.append({"project": tree.project, "final": final, "at": at, "by": a.get("by"), "comment": a.get("comment", ""),
                    "targets": targets, "items": items, "spec_version": spec, "spec_done": spec_done,
                    "new_spec": new_specs[0] if new_specs else None,
                    "erd_version": erd_version, "erd_waiting": waiting})
    return sorted(out, key=lambda d: d["at"], reverse=True)


def open_directives(project: str) -> list[dict]:
    return _open(Tree(project))


def locked(project: str, version: str) -> str:
    """이 ERD 버전을 지금 잠그는 이유 (기능 명세 수정 지시가 진행 중인 옛 계보), 없으면 빈 값"""
    tree = Tree(project)
    if tree.stage_of(version) != "erd":
        return ""
    spec = tree.spec_of(version)
    for d in _open(tree):
        if "spec" in d["targets"] and d["spec_version"] == spec:
            where = f" 새 ERD v{d['erd_version']}에서 작업하세요." if d["erd_version"] else " 새 기능 명세가 올라오면 그 ERD에서 고칩니다."
            return f"PM이 기능 명세 수정을 지시해(최종본 v{d['final']}) 이 ERD는 잠겼습니다.{where}"
    return ""


def ensure_unlocked(project: str, version: str) -> None:
    if reason := locked(project, version):
        raise Forbidden(reason)


def resolve(project: str, final: str, stage: str, target_id: str, choice: str, note: str, member) -> dict:
    """지시 항목 처리: 반영함(applied) / 그대로 둠(kept, 이유 필수). 그 단계를 고칠 수 있는 회원(PM·개발자)만"""
    if choice not in ("applied", "kept", ""):
        raise BadRequest("처리는 반영함·그대로 둠 중 하나입니다.")
    if choice == "kept" and not note.strip():
        raise BadRequest("'그대로 둠'은 이유를 적어야 합니다.")
    if not allowed(member.role, EDIT["spec" if stage == "spec" else "erd"]):
        raise Forbidden("수정 지시 항목은 PM·개발자가 처리합니다.")
    record = next((r for r in load_versions(project) if str(r["design_version"]) == final), None)
    a = (record or {}).get("approval") or {}
    key = f"{stage}:{target_id}"
    if a.get("decision") != "changes" or not any(f"{i['stage']}:{i['target_id']}" == key for i in a.get("items") or []):
        raise NotFound("처리할 수정 지시 항목이 없습니다.")
    resolutions = dict(a.get("resolutions") or {})
    if choice:
        resolutions[key] = {"choice": choice, "note": note.strip()[:500], "by": drafts._who(member), "at": drafts._now()}
    else:
        resolutions.pop(key, None)
    update_version(project, final, approval={**a, "resolutions": resolutions})
    return resolutions.get(key) or {}


# ------------------------------------------------------------------ 알림
# 두 가지: ① 상태 알림(수정 지시·승인 요청·승인 대기·개발 대기) — 지금 상태에서 계산, 처리하면 사라진다.
# ② 일 알림(업로드 분석 완료·ERD 초안·개발 초안) — 그 일이 끝날 때 프로젝트 파일 notices.json에 남긴다.
# 둘 다 '확인'을 누르면 그 회원에게서만 사라진다 (notices.json의 dismissed[회원 ID]).
NOTICE_FILE = "notices.json"
EVERYONE = None
_notice_lock = threading.Lock()


def _notice_data(project: str) -> dict:
    text = get_store().read_project_file(project, NOTICE_FILE)
    data = json.loads(text) if text else {}
    return {"events": data.get("events") or [], "dismissed": data.get("dismissed") or {}}


def add_event(project: str, kind: str, title: str, detail: str, page: str, version: str, roles: set[str] | None,
              member_ids: set[int] | None = None) -> None:
    """일 알림을 남긴다. roles=None이면 모든 회원. member_ids가 있으면 그 회원에게만 (예: 합치기 결과 → 요청한 사람)"""
    with _notice_lock:
        data = _notice_data(project)
        at = datetime.now().isoformat(timespec="seconds")
        event = {"id": f"{kind}:{version}:{at}", "kind": kind, "at": at, "title": title, "detail": detail, "page": page,
                 "version": version, "roles": sorted(roles) if roles is not None else None}
        if member_ids is not None:
            event["member_ids"] = sorted(member_ids)
        data["events"] = [*data["events"][-199:], event]
        get_store().write_project_file(project, NOTICE_FILE, json.dumps(data, ensure_ascii=False))


def _event_notice(project: str, e: dict) -> dict:
    n = {"project": project, **{k: e[k] for k in ("kind", "at", "title", "detail", "page", "version", "id")}}
    if e.get("member_ids") is not None:
        n["members"] = e["member_ids"]
    return n


def member_events(project: str) -> list[dict]:
    """회원을 정해 남긴 일 알림 (Slack이 그 회원만 부를 때)"""
    return [_event_notice(project, e) for e in _notice_data(project)["events"] if e.get("member_ids") is not None]


def dismiss(project: str, notice_id: str, member) -> None:
    """확인: 그 회원에게서 알림을 지운다"""
    with _notice_lock:
        data = _notice_data(project)
        mine = data["dismissed"].setdefault(str(member.id), [])
        if notice_id not in mine:
            mine.append(notice_id)
            del mine[:-500]
        get_store().write_project_file(project, NOTICE_FILE, json.dumps(data, ensure_ascii=False))


def job_event(job) -> None:
    """작업이 끝나면 일 알림: 업로드 분석 완료 → 모든 담당자, ERD 초안·개발 초안 → 개발자"""
    if job.status != "done" or not job.new_version:
        return
    if job.source == "run":
        add_event(job.project, "uploaded", f"새 문서 분석 완료 — 기능 명세 초안 v{job.new_version}",
                  "개발자가 기능 명세를 검토하고 PM이 확정합니다", "spec", job.new_version, EVERYONE)
    elif job.source == "spec":
        add_event(job.project, "erd_draft", f"기능 명세 v{job.base} 확정 — ERD 초안 v{job.new_version}",
                  "개발자가 ERD를 검토·확정합니다", "erd", job.new_version, {DEVELOPER})
    elif job.source == "develop":
        add_event(job.project, "developed", f"최종본 v{job.base} 개발 초안 완료",
                  "최종 개발물 1단계에서 결과와 코드를 확인하세요", "develop", job.new_version, {DEVELOPER})


def notices(member, include_dismissed: bool = False) -> list[dict]:
    """홈·사이드바의 '나에게 온 알림' (내가 담당자인 프로젝트만): 상태 알림(내 역할에 해당하는 수정 지시·승인 요청·승인 대기·개발 대기, 내가 요청했거나 고친 초안의 반려)
    + 일 알림(업로드 분석 완료·ERD 초안·개발 초안). 확인한 것은 뺀다 (include_dismissed=True면 빼지 않는다: Slack 전송용)"""
    out = []
    role = role_of(member.role)
    # 담당자인 프로젝트만 (PM을 고른 적이 없는 프로젝트는 PM 모두): 담당자가 아닌 프로젝트의 알림은 보이지 않는다 (services/project_access)
    for project in project_access.visible(member, [p["id"] for p in output_store.find_projects()]):
        tree = Tree(project)
        for d in _open(tree):
            src = f"{(d['by'] or {}).get('name', 'PM')} · 최종본 v{d['final']}"
            spec_items = [i for i in d["items"] if i["stage"] == "spec"]
            erd_items = [i for i in d["items"] if i["stage"] == "erd"]
            left = lambda items: sum(1 for i in items if not i["resolution"])   # noqa: E731
            if allowed(role, EDIT["spec"]) and "spec" in d["targets"] and not d["spec_done"]:
                out.append({"project": project, "kind": "directive", "at": d["at"], "page": "spec", "version": d["spec_version"],
                            "title": f"기능 명세 수정 지시 ({src})",
                            "detail": f"항목 {len(spec_items)}건 · 미처리 {left(spec_items)}건" if spec_items else d["comment"][:80]})
            if allowed(role, EDIT["erd"]) and ("erd" in d["targets"] or "spec" in d["targets"]):
                if d["erd_waiting"]:
                    out.append({"project": project, "kind": "waiting", "at": d["at"], "page": "erd",
                                "version": d["erd_version"] or d["spec_version"],
                                "title": f"기능 명세 수정 대기 ({src})",
                                "detail": f"새 기능 명세가 올라오면 그 ERD에서 고칩니다{f' · ERD 항목 {len(erd_items)}건' if erd_items else ''}"})
                elif d["erd_version"]:
                    out.append({"project": project, "kind": "directive", "at": d["at"], "page": "erd", "version": d["erd_version"],
                                "title": f"ERD v{d['erd_version']}에서 수정 지시 처리 ({src})",
                                "detail": f"항목 {len(erd_items)}건 · 미처리 {left(erd_items)}건" if erd_items
                                else "바뀐 기능 명세에 맞게 확인하고 승인 요청"})
        # 요청했거나 고친 개발자: 반려 / 그 단계를 확정하는 회원(기능 명세는 PM): 승인 요청 / PM: 승인 대기 최종본
        for label, r in tree.records.items():
            stage = tree.stage_of(label)
            doc = drafts.read(project, label) if stage in EDIT else None
            rejection = (doc or {}).get("rejection")
            if rejection and not doc["request"] and member.id in rejected_people(doc, stage) \
                    and not [v for v in tree.after(rejection["at"], stage, tree.spec_of(label)) if v != label]:
                out.append({"project": project, "kind": "rejected", "at": rejection["at"], "page": stage, "version": label,
                            "title": f"{rejection['by']['name']}님이 승인 요청을 반려했습니다 "
                                     f"({'기능 명세' if stage == 'spec' else 'ERD'} v{label})",
                            "detail": f"사유: {rejection.get('reason', '')} · 고친 뒤 다시 승인 요청"})
            if stage in CONFIRM and allowed(role, CONFIRM[stage]):
                req = doc["request"]
                # 요청 뒤에 다음 단계(ERD·최종본)가 이미 나왔으면 처리된 요청이다 (예전 기록)
                done = req and any(v.startswith(f"{label}.") and (rr.get("created_at") or "") > req["at"]
                                   for v, rr in tree.records.items())
                if req and not done:
                    out.append({"project": project, "kind": "request", "at": req["at"], "page": stage, "version": label,
                                "title": f"{req['by']['name']}님의 수정 완료 승인 요청 ({'기능 명세' if stage == 'spec' else 'ERD'} v{label})",
                                "detail": req.get("note") or ""})
            # 개발자: 최종 승인했는데 아직 개발하지 않은(또는 승인 뒤 다시 개발하지 않은) 최종본
            approval = r.get("approval") or {}
            if r.get("stage") == "final" and allowed(role, "develop") and approval.get("decision") == "approve" \
                    and (r.get("develop") or {}).get("at", "") < approval.get("at", ""):
                out.append({"project": project, "kind": "develop", "at": approval.get("at", ""), "page": "develop", "version": label,
                            "title": f"최종본 v{label} 승인됨 — 개발 대기",
                            "detail": f"{(approval.get('by') or {}).get('name', 'PM')} 승인 · 산출물 보기에서 '최종 개발하기'"})
            if r.get("stage") == "final" and allowed(role, "final_decide") and not r.get("approval") and not tree.after(r.get("created_at") or "", "final", tree.spec_of(label)):
                out.append({"project": project, "kind": "decide", "at": r.get("created_at") or "", "page": "final", "version": label,
                            "title": f"최종본 v{label} 승인 대기", "detail": "최종 승인 또는 수정 지시"})
        data = _notice_data(project)
        out += [_event_notice(project, e) for e in data["events"]
                if (e.get("roles") is None or role in {role_of(x) for x in e["roles"]}) and (e.get("member_ids") is None or member.id in e["member_ids"])]
        gone = set(data["dismissed"].get(str(member.id)) or [])
        if not include_dismissed:
            out = [n for n in out if n["project"] != project or _nid(n) not in gone]
    for n in out:
        n["id"] = _nid(n)
    return sorted(out, key=lambda n: n["at"], reverse=True)


def _nid(n: dict) -> str:
    """알림 ID (확인한 것을 기억하는 데): 일 알림은 저장한 ID, 상태 알림은 종류·버전·시각"""
    return n.get("id") or f"{n['kind']}:{n['version']}:{n['at']}"
