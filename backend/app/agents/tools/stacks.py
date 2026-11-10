"""개발 스택: 화면 / 백엔드 / DB를 나눠 정한다.

- 원문이 스택을 정했으면 그대로 따른다. 개발 틀이 없는 스택이면 기능 명세 단계부터 경고한다
  (최종 개발하기 때 AI가 그 층의 뼈대부터 만들어 토큰·비용이 더 든다).
- 원문에 없으면 기능 명세 초안의 필수 확인 질문(TQ-001)에서 세 칸을 하나씩 고른다. 기능 명세를 확정하면 스택도 확정이다.
- 고른 세 값은 기술 스택 문장(예: 'Java 17 · Spring Boot 3 · PostgreSQL 16 · React 18 · TypeScript · Vite')이 되어
  TQ-001의 답과 개발 스펙 10개에 들어가고, 최종 개발하기(develop)가 그 층의 틀로 만든다.
- supported는 '개발 틀이 있음'이다 (TEMPLATED). 틀이 없는 선택지도 고를 수는 있다 (고객 요구를 도구가 바꾸지 않는다).
"""
from __future__ import annotations

import re

LAYERS = {"frontend": "화면", "backend": "서버", "database": "DB"}
# 개발 틀이 있는 것 (agents/templates/app/<층>/<id> · DDL 문법). 틀을 추가하면 여기에 넣는다
TEMPLATED = {"frontend": {"react", "nextjs"}, "backend": {"spring", "fastapi", "nestjs"}, "database": {"postgresql", "mysql"}}
OPTIONS: dict[str, list[dict]] = {
    "frontend": [
        {"id": "react", "label": "React 18 · TypeScript · Vite", "keys": ["react", "리액트"]},
        {"id": "nextjs", "label": "Next.js 14 · React 18 · TypeScript", "keys": ["next.js", "nextjs", "next js", "넥스트"]},
        {"id": "vue", "label": "Vue 3 · TypeScript · Vite", "keys": ["vue"]},
    ],
    "backend": [
        {"id": "spring", "label": "Java 17 · Spring Boot 3", "keys": ["spring", "java", "스프링", "자바"]},
        {"id": "fastapi", "label": "Python 3.12 · FastAPI", "keys": ["fastapi", "python", "파이썬"]},
        {"id": "egov", "label": "Java 17 · 전자정부 표준프레임워크 4", "keys": ["전자정부 표준", "전자정부프레임", "전자정부 프레임", "표준프레임워크", "표준 프레임워크", "egov"]},
        {"id": "nestjs", "label": "TypeScript · NestJS", "keys": ["nestjs", "nest.js", "node.js", "nodejs", "네스트"]},
    ],
    "database": [
        {"id": "postgresql", "label": "PostgreSQL 16", "keys": ["postgres", "포스트그레"]},
        {"id": "mysql", "label": "MySQL 8", "keys": ["mysql", "mariadb", "마리아"]},
        {"id": "oracle", "label": "Oracle 19c", "keys": ["oracle", "오라클"]},
    ],
}
DEFAULT = {"frontend": "react", "backend": "spring", "database": "postgresql"}
# 원문이 서버 언어·프레임워크를 정했는지 보는 낱말 (api_designer.STACK_WORDS가 이것을 쓴다. 한글 표기도, JavaScript는 제외)
SOURCE_BACKEND = re.compile(r"java(?!\s*script)|spring|전자\s*정부\s*(표준\s*)?프레임\s*워크|egov|python|django|fastapi|flask|node\.?js|nest\.?js"
                            r"|\.net|c#|php|laravel|ruby on rails|kotlin|자바(?!\s*스크립트)|스프링|파이썬|장고|플라스크|코틀린", re.I)
# 화면·DB 이름 (원문 근거를 보여 주고 TQ-001 칸을 미리 채운다. 서버가 없으면 질문은 그대로 낸다)
SOURCE_OTHER = re.compile(r"react|리액트|next\.?js|vue\.?js|\bvue\b|postgre|포스트그레|mysql|maria\s*db|마리아\s*db|oracle|오라클", re.I)
# 선택지에 없는 서버 프레임워크 (원문에 있으면 '틀 없음'으로 본다. 'Python'만 있으면 FastAPI로 짐작하지만 Django·Flask면 그 이름)
OTHER_BACKEND = re.compile(r"django|flask|\.net|c#|php|laravel|ruby on rails|express|장고|플라스크", re.I)
# 아키텍처 요약(api_designer.StackMentionOut)의 층 중 스택으로 보는 것
SOURCE_LAYERS = ("언어", "프레임워크", "DBMS", "DB", "데이터베이스", "프런트엔드", "화면")
COST_NOTE = "최종 개발하기 때 AI가 그 층의 뼈대부터 만들어 토큰·비용이 더 들고, 결과가 실행마다 다를 수 있습니다"


# 최종 개발하기를 하지 않는 것: AI 뼈대로도 실행되는 앱을 못 만든다 (Oracle: DB 스크립트·실행 설정을 만들지 못함). 설계 산출물까지만
NO_DEVELOP = {"database": {"oracle"}}
NO_DEVELOP_NOTE = "최종 개발하기는 지원하지 않습니다 (설계 산출물까지만)"


def catalog() -> dict[str, list[dict]]:
    """화면의 선택지 (supported = 개발 틀 있음, develop = 최종 개발하기를 할 수 있음)"""
    return {layer: [{"id": o["id"], "label": o["label"], "supported": o["id"] in TEMPLATED[layer],
                     "develop": o["id"] not in NO_DEVELOP.get(layer, set())} for o in options]
            for layer, options in OPTIONS.items()}


def no_develop(parts: dict[str, str]) -> list[dict]:
    """최종 개발하기를 하지 않는 층 [{layer, name, label}]"""
    return [{"layer": layer, "name": LAYERS[layer], "label": _option(layer, parts[layer])["label"]}
            for layer in LAYERS if parts.get(layer) in NO_DEVELOP.get(layer, set())]


CATALOG = catalog()


def _option(layer: str, oid: str) -> dict | None:
    return next((o for o in OPTIONS[layer] if o["id"] == oid), None)


def compose(parts: dict[str, str]) -> str:
    """고른 세 값 → 기술 스택 문장 (서버 · DB · 화면). 빠졌거나 없는 값이면 ValueError (틀이 없는 값은 받는다)"""
    labels = {}
    for layer, name in LAYERS.items():
        option = _option(layer, parts.get(layer, ""))
        if option is None:
            raise ValueError(f"{name}을(를) 골라 주세요.")
        labels[layer] = option["label"]
    return f"{labels['backend']} · {labels['database']} · {labels['frontend']}"


def guess(stack: str) -> dict[str, str]:
    """스택 문장에서 세 값을 짐작한다. 못 찾은 층은 빈 값"""
    text = (stack or "").lower()
    out = {}
    for layer, options in OPTIONS.items():
        # 전자정부는 Spring 낱말도 함께 나오므로 먼저 본다
        ordered = sorted(options, key=lambda o: o["id"] != "egov")
        out[layer] = next((o["id"] for o in ordered if any(k in text for k in o["keys"])), "")
    if out["frontend"] == "react" and any(k in text for k in ("next.js", "nextjs", "next js")):
        out["frontend"] = "nextjs"
    return out


def parts_of(stack: str, parts: dict | None = None) -> dict[str, str]:
    """개발할 세 값: 저장된 값이 있으면 그것, 없으면 문장에서 짐작하고 못 찾은 층은 기본 틀"""
    found = {k: v for k, v in (parts or {}).items() if k in LAYERS and v} or guess(stack)
    return {layer: found.get(layer) or DEFAULT[layer] for layer in LAYERS}


def untemplated(parts: dict[str, str], unknown: dict[str, str] | None = None) -> list[dict]:
    """개발 틀이 없는 층 [{layer, name, label}] (unknown: 원문에 있지만 선택지에 없는 이름)"""
    out = []
    for layer, name in LAYERS.items():
        if unknown and unknown.get(layer):
            out.append({"layer": layer, "name": name, "label": unknown[layer]})
        elif parts.get(layer) and parts[layer] not in TEMPLATED[layer]:
            out.append({"layer": layer, "name": name, "label": _option(layer, parts[layer])["label"]})
    return out


def warning(missing: list[dict]) -> str:
    blocked = [m for m in missing if m["label"] in {o["label"] for layer, ids in NO_DEVELOP.items() for o in OPTIONS[layer] if o["id"] in ids}]
    rest = [m for m in missing if m not in blocked]
    out = []
    if blocked:
        out.append(f"{', '.join(f'{m['name']} {m['label']}' for m in blocked)}: {NO_DEVELOP_NOTE}.")
    if rest:
        out.append(f"{', '.join(f'{m['name']} {m['label']}' for m in rest)}은(는) 개발 틀이 없습니다. 설계 산출물은 그대로 만들어지지만, {COST_NOTE}.")
    return " ".join(out)


def requirement_text(r) -> str:
    """요구사항에서 스택을 찾는 글: 제목·설명·수용 기준 (dict·Requirement 모두)"""
    get = r.get if isinstance(r, dict) else lambda k, d=None: getattr(r, k, d)
    return " ".join([get("title", "") or "", get("description", "") or "", *(get("acceptance_criteria") or [])])


def mentions(requirements: list) -> list[dict]:
    """원문 요구사항(범위에서 뺀 것 제외)에서 스택을 적은 곳 [{id, title, text}] 앞의 5곳 (text: 찾은 낱말 앞뒤 원문, LLM 없음)"""
    out = []
    for r in requirements or []:
        if r.get("status") == "EXCLUDED":
            continue
        text = " ".join(requirement_text(r).split())
        if len(out) < 5 and (m := SOURCE_BACKEND.search(text) or SOURCE_OTHER.search(text)):
            a, b = max(m.start() - 30, 0), min(m.end() + 30, len(text))
            out.append({"id": r.get("requirement_id", ""), "title": r.get("title", ""),
                        "text": ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")})
    return out


def _label(layer: str, oid: str) -> str:
    return (_option(layer, oid) or {}).get("label", oid)


def status(design: dict) -> dict:
    """버전의 기술 스택 상태 (화면 안내용, LLM 없음).

    basis: 원문(원문이 정함) · 확정(기능 명세 TQ-001에서 고름) · 미정(아직 안 고름) · 없음(스택 질문도 원문 스택도 없음)
    source: 업로드 문서에 적힌 스택 {stack, parts(찾은 층만), evidence[{id, title, text}]} (없으면 None).
      서버를 정하지 않고 화면·DB만 적은 원문도 여기에 담아 TQ-001 칸을 미리 채운다.
    conflict: 고른 값이 업로드 문서와 다른 층 안내 (같거나 원문이 없으면 빈 값)
    changed: 개발 범위 확정(최종 산출물)에서 사람이 고른 값인지 — 원문이 있어도 이 값을 따른다
    """
    from app.agents.roles.api_designer import STACK_QUESTION
    questions = design.get("questions") or []
    q = next((q for q in questions if q.get("question_id") == STACK_QUESTION), None)
    # 최종 확정의 아키텍처 요약은 api_design 안에 저장된다 (예전 형식은 맨 위)
    arch = (design.get("api_design") or {}).get("architecture") or design.get("architecture") or {}
    named = [m for m in arch.get("stack_in_source") or [] if m.get("layer") in SOURCE_LAYERS]
    evidence = mentions(design.get("requirements") or [])
    text = " · ".join(m.get("value", "") for m in named) if named else " ".join(
        requirement_text(r) for r in design.get("requirements") or [] if r.get("status") != "EXCLUDED")
    # 원문이 서버 언어·프레임워크를 정했는지: 아키텍처 요약이 있으면 그 판단(개발 스펙 tech_stack과 같게), 없으면 원문 글
    decided = any(m.get("layer") in ("언어", "프레임워크") for m in named) if named else bool(SOURCE_BACKEND.search(text))
    found = guess(text)
    unknown: dict[str, str] = {}
    if decided:
        if m := OTHER_BACKEND.search(text):
            found["backend"], unknown["backend"] = "", m.group(0)
        elif not found["backend"] and (m := SOURCE_BACKEND.search(text)):
            unknown["backend"] = m.group(0)
    else:
        found["backend"] = ""
    written = {layer: v for layer, v in found.items() if v}
    source = None
    if written or unknown:
        source = {"stack": " · ".join(m.get("value", "") for m in named)
                  or " · ".join([*unknown.values(), *(_label(layer, written[layer]) for layer in ("backend", "database", "frontend") if layer in written)]),
                  "parts": written, "evidence": evidence}
    answered = bool(q and q.get("status") == "ANSWERED" and q.get("answer"))
    spec = (design.get("meta") or {}).get("dev_spec") or {}
    changed = answered and spec.get("stack_basis") == "확정" and not spec.get("stack_from_answer")
    chosen = parts_of(q["answer"]) if answered else {}
    diff = [layer for layer, v in written.items() if chosen and chosen.get(layer) != v]
    if unknown and chosen:
        diff.append("backend")
    where = ", ".join(e["id"] for e in evidence[:3] if e["id"])
    conflict = ""
    if diff and source:
        conflict = (f"업로드 문서에는 {', '.join(f'{LAYERS[layer]} {_label(layer, written[layer]) if layer in written else unknown[layer]}' for layer in diff)}"
                    f"(으)로 명시되어 있습니다{f' ({where})' if where else ''}. "
                    + ("개발 범위 확정에서 바꾼 값과 다릅니다 — 개발 스펙과 AI 개발은 바꾼 값을 따르고, 문서 내용을 근거로 함께 넘깁니다."
                       if changed else "기능 명세에서 고른 값과 다릅니다."))
    # 아키텍처 요약이 원문 서버 스택을 찾았으면 개발 스펙은 원문을 따른다 (기능 명세 답보다 우선, 개발 범위 확정에서 바꾼 값보다는 뒤)
    named_backend = named and decided and not changed
    if decided and (named_backend or not answered):
        parts = {layer: found[layer] or (DEFAULT[layer] if layer not in unknown else "") for layer in LAYERS}
        missing = untemplated(parts, unknown)
        if conflict:
            conflict += " 개발 스펙은 업로드 문서를 따릅니다."
        return {"basis": "원문", "stack": source["stack"] if named else "", "parts": parts, "untemplated": missing,
                "warning": warning(missing), "source": source, "conflict": conflict, "changed": False}
    if answered:
        missing = untemplated(chosen)
        return {"basis": "확정", "stack": q["answer"], "parts": chosen, "untemplated": missing, "warning": warning(missing),
                "source": source, "conflict": conflict, "changed": changed}
    rest = {"stack": "", "parts": {}, "untemplated": [], "warning": "", "source": source, "conflict": "", "changed": False}
    return {"basis": "미정" if q else "없음", **rest}
