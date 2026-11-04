"""팀장 수정 지시의 진행: 어느 단계가 어느 버전에서 처리할지, 옛 ERD 잠금, 항목 처리, 회원별 알림.

수정 지시는 최종본 버전 기록의 approval(decision=changes, targets, items)에 있다. 처리 순서는 '기능 명세 먼저':
  - 기능 명세가 대상이면 기획 파트가 그 기능 명세(S)를 고쳐 새 기능 명세(S', 내보내기면 ERD S'.1까지)를 만든다.
    그동안 S 계보의 옛 ERD는 잠근다 (지시가 빠진 ERD로 다시 최종본을 만들지 않게).
  - ERD 항목은 기능 명세가 대상이면 새 기능 명세의 ERD(S'.1)가 생긴 뒤 그 ERD에서, ERD만 대상이면 지금 ERD 계보에서 처리한다.
  - 항목마다 '반영함 / 그대로 둠(이유)'을 고른다 (approval.resolutions, LLM 없음). 화면은 필수 확인 항목으로 둔다.
  - 지시 뒤 그 계보에서 새 최종본이 생기면 지시는 끝난 것이다 (새 최종본의 팀장 결정 칸에 처리 결과가 보인다).
날짜는 버전 기록의 created_at과 지시 시각(approval.at)을 비교한다 (둘 다 서버 로컬 ISO 시각).
"""
from __future__ import annotations

from app.agents.tools.versions import load_versions, update_version, version_key
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.services import drafts, output_store

SPEC_STAGE = "function_designer"
PLANNERS, DATA = {"planner", "planner_head", "lead"}, {"data", "data_head", "lead"}
DEV = {"dev", "dev_head", "lead"}


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
            continue   # 지시 뒤 새 최종본이 나왔다 → 끝 (새 최종본에서 팀장이 확인)
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
            return f"팀장이 기능 명세 수정을 지시해(최종본 v{d['final']}) 이 ERD는 잠겼습니다.{where}"
    return ""


def ensure_unlocked(project: str, version: str) -> None:
    if reason := locked(project, version):
        raise Forbidden(reason)


def resolve(project: str, final: str, stage: str, target_id: str, choice: str, note: str, member) -> dict:
    """지시 항목 처리: 반영함(applied) / 그대로 둠(kept, 이유 필수). 그 단계를 고치는 파트만"""
    if choice not in ("applied", "kept", ""):
        raise BadRequest("처리는 반영함·그대로 둠 중 하나입니다.")
    if choice == "kept" and not note.strip():
        raise BadRequest("'그대로 둠'은 이유를 적어야 합니다.")
    if member.role not in (PLANNERS if stage == "spec" else DATA):
        raise Forbidden("기능 명세 지시는 기획 파트, ERD 지시는 데이터 파트가 처리합니다.")
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


def notices(member) -> list[dict]:
    """홈·단계 화면의 '나에게 온 알림': 내 역할에 해당하는 수정 지시, 파트장에게 온 승인 요청, 팀장의 승인 대기 최종본"""
    out = []
    role = member.role
    for p in output_store.find_projects():
        project = p["id"]
        tree = Tree(project)
        for d in _open(tree):
            src = f"{(d['by'] or {}).get('name', '팀장')} · 최종본 v{d['final']}"
            spec_items = [i for i in d["items"] if i["stage"] == "spec"]
            erd_items = [i for i in d["items"] if i["stage"] == "erd"]
            left = lambda items: sum(1 for i in items if not i["resolution"])   # noqa: E731
            if role in PLANNERS and "spec" in d["targets"] and not d["spec_done"]:
                out.append({"project": project, "kind": "directive", "at": d["at"], "page": "spec", "version": d["spec_version"],
                            "title": f"기능 명세 수정 지시 ({src})",
                            "detail": f"항목 {len(spec_items)}건 · 미처리 {left(spec_items)}건" if spec_items else d["comment"][:80]})
            if role in DATA and ("erd" in d["targets"] or "spec" in d["targets"]):
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
        # 파트장: 승인 요청 / 팀장: 승인 대기 최종본
        for label, r in tree.records.items():
            stage = tree.stage_of(label)
            if (stage == "spec" and role in ("planner_head", "lead")) or (stage == "erd" and role in ("data_head", "lead")):
                req = drafts.read(project, label)["request"]
                # 요청 뒤에 다음 단계(ERD·최종본)가 이미 나왔으면 처리된 요청이다 (예전 기록)
                done = req and any(v.startswith(f"{label}.") and (rr.get("created_at") or "") > req["at"]
                                   for v, rr in tree.records.items())
                if req and not done:
                    out.append({"project": project, "kind": "request", "at": req["at"], "page": stage, "version": label,
                                "title": f"{req['by']['name']}님의 수정 완료 승인 요청 ({'기능 명세' if stage == 'spec' else 'ERD'} v{label})",
                                "detail": req.get("note") or ""})
            # 개발 파트: 팀장이 승인했는데 아직 개발하지 않은(또는 승인 뒤 다시 개발하지 않은) 최종본
            approval = r.get("approval") or {}
            if r.get("stage") == "final" and role in DEV and approval.get("decision") == "approve" \
                    and (r.get("develop") or {}).get("at", "") < approval.get("at", ""):
                out.append({"project": project, "kind": "develop", "at": approval.get("at", ""), "page": "develop", "version": label,
                            "title": f"최종본 v{label} 승인됨 — 개발 대기",
                            "detail": f"{(approval.get('by') or {}).get('name', '팀장')} 승인 · 산출물 보기에서 '최종 개발하기'"})
            if r.get("stage") == "final" and role == "lead" and not r.get("approval") and not tree.after(r.get("created_at") or "", "final", tree.spec_of(label)):
                out.append({"project": project, "kind": "decide", "at": r.get("created_at") or "", "page": "final", "version": label,
                            "title": f"최종본 v{label} 승인 대기", "detail": "최종 승인 또는 수정 지시"})
    return sorted(out, key=lambda n: n["at"], reverse=True)
