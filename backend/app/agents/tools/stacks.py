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
        {"id": "react", "label": "React 18 · TypeScript · Vite", "keys": ["react"]},
        {"id": "nextjs", "label": "Next.js 14 · React 18 · TypeScript", "keys": ["next.js", "nextjs", "next js"]},
        {"id": "vue", "label": "Vue 3 · TypeScript · Vite", "keys": ["vue"]},
    ],
    "backend": [
        {"id": "spring", "label": "Java 17 · Spring Boot 3", "keys": ["spring", "java"]},
        {"id": "fastapi", "label": "Python 3.12 · FastAPI", "keys": ["fastapi", "python"]},
        {"id": "egov", "label": "Java 17 · 전자정부 표준프레임워크 4", "keys": ["전자정부", "egov"]},
        {"id": "nestjs", "label": "TypeScript · NestJS", "keys": ["nestjs", "nest.js", "node.js", "nodejs"]},
    ],
    "database": [
        {"id": "postgresql", "label": "PostgreSQL 16", "keys": ["postgres"]},
        {"id": "mysql", "label": "MySQL 8", "keys": ["mysql", "mariadb"]},
        {"id": "oracle", "label": "Oracle 19c", "keys": ["oracle"]},
    ],
}
DEFAULT = {"frontend": "react", "backend": "spring", "database": "postgresql"}
# 원문이 서버 스택을 정했는지 보는 낱말 (api_designer.STACK_WORDS와 같은 범위 + DB 이름)
SOURCE_BACKEND = re.compile(r"java|spring|전자\s*정부|egov|python|django|fastapi|flask|node\.?js|nest\.?js"
                            r"|\.net|c#|php|laravel|ruby on rails|kotlin", re.I)
# 선택지에 없는 서버 프레임워크 (원문에 있으면 '틀 없음'으로 본다. 'Python'만 있으면 FastAPI로 짐작하지만 Django·Flask면 그 이름)
OTHER_BACKEND = re.compile(r"django|flask|\.net|c#|php|laravel|ruby on rails|express", re.I)
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


def status(design: dict) -> dict:
    """버전의 기술 스택 상태 (화면 안내용, LLM 없음).

    basis: 원문(원문이 정함) · 확정(기능 명세 TQ-001에서 고름) · 미정(아직 안 고름) · 없음(스택 질문도 원문 스택도 없음)
    """
    from app.agents.roles.api_designer import STACK_QUESTION
    questions = design.get("questions") or []
    q = next((q for q in questions if q.get("question_id") == STACK_QUESTION), None)
    arch = design.get("architecture") or {}
    source = [m for m in arch.get("stack_in_source") or [] if m.get("layer") in ("언어", "프레임워크", "DB", "데이터베이스", "화면")]
    unknown: dict[str, str] = {}
    if source or not q:
        if source:
            text = " · ".join(m.get("value", "") for m in source)
        else:   # 기능 명세 초안: 원문 요구사항 글에서 찾는다 (최종 확정 전에는 아키텍처 요약이 없다)
            text = " ".join(f"{r.get('title', '')} {r.get('description', '')}" for r in design.get("requirements") or [])
            if not SOURCE_BACKEND.search(text):
                return {"basis": "없음", "stack": "", "parts": {}, "untemplated": [], "warning": ""}
        found = guess(text)
        if m := OTHER_BACKEND.search(text):
            found["backend"], unknown["backend"] = "", m.group(0)
        elif not found["backend"] and (m := SOURCE_BACKEND.search(text)):
            unknown["backend"] = m.group(0)
        parts = {layer: found[layer] or (DEFAULT[layer] if layer not in unknown else "") for layer in LAYERS}
        missing = untemplated(parts, unknown)
        stack = " · ".join(m.get("value", "") for m in source) if source else ""
        return {"basis": "원문", "stack": stack, "parts": parts, "untemplated": missing, "warning": warning(missing)}
    if q.get("status") == "ANSWERED" and q.get("answer"):
        parts = parts_of(q["answer"])
        missing = untemplated(parts)
        return {"basis": "확정", "stack": q["answer"], "parts": parts, "untemplated": missing, "warning": warning(missing)}
    return {"basis": "미정", "stack": "", "parts": {}, "untemplated": [], "warning": ""}
