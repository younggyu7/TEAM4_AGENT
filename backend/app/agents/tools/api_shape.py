"""API 모양 판단 (LLM 없음): 메서드·경로·필드로 API가 하는 일과 다루는 테이블을 정한다.

API 설계·화면 설계·서비스 코드 생성이 같은 판단을 쓰도록 한곳에 둔다.
"""
from __future__ import annotations

import re
from collections import Counter

from app.schemas.data_model import Attribute, Entity

CREATE_VERBS = {"create", "add", "register", "upload", "insert", "issue"}
AUDIT_COLUMNS = {"created_at", "updated_at"}
USER_TABLE = re.compile(r"(^|_)(user|employee|member|staff)s?($|_)|^account$")   # 작성자·신청자처럼 현재 사용자로 채울 외래키
NUMBER_COLUMN = re.compile(r"(_no|_number|_code)$")                              # 문서번호·코드: 요청에 없으면 자동 번호


def collection(table: str) -> str:
    """테이블 이름 → 경로 조각 (복수형 kebab-case): approval_document_instance → approval-document-instances."""
    name = table.replace("_", "-")
    if re.search(r"(s|x|z|ch|sh)$", name):
        return name + "es"
    if re.search(r"[^aeiou]y$", name):
        return name[:-1] + "ies"
    return name + "s"


def snake(name: str) -> str:
    """camelCase → snake_case."""
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def path_params(path: str) -> list[str]:
    return re.findall(r"\{(\w+)\}", path)


def main_table(op: dict) -> str:
    """API가 주로 다루는 테이블 (설계 이름): 컬럼을 가장 많이 쓴 테이블, 같으면 먼저 나온 것."""
    counts = Counter(f["column"].split(".")[0] for f in [*op.get("request", []), *op.get("response", [])] if f.get("column"))
    return counts.most_common(1)[0][0] if counts else ""


def table_of(op: dict, entities: list[Entity], linked: list[str] | tuple = ()) -> str:
    """API가 다루는 테이블. 필드에 컬럼이 없으면(DELETE·처리 API) 경로의 ID로 찾고, 기능에 테이블이 하나면 그 테이블이다."""
    table = main_table(op)
    if table:
        return table
    for param in reversed(path_params(op["path"])):
        column = snake(param)
        hit = next((e.entity_id for e in entities if any(a.primary_key and a.name == column for a in e.attributes)), "")
        if hit:
            return hit
    return linked[0] if len(linked) == 1 else ""


def op_kind(op: dict) -> str:
    """API 모양으로 하는 일을 정한다: list·get·report·create·add·update·delete·action."""
    parts = [p for p in op["path"].split("/") if p]
    tail_param = bool(parts) and parts[-1].startswith("{")
    has_param = any(p.startswith("{") for p in parts)
    verb = re.match(r"[a-z]+", op.get("operation_id") or "")
    verb = verb.group(0) if verb else ""
    if op["method"] == "GET":
        return "get" if tail_param else "list" if op.get("list_response") else "report"
    if op["method"] == "DELETE":
        return "delete" if tail_param else "action"
    if op["method"] in ("PUT", "PATCH"):
        return "update" if tail_param else "action"
    if verb in CREATE_VERBS:
        return "add" if has_param else "create"
    table = main_table(op)
    if not has_param and table and parts and parts[-1] == collection(table):   # POST /chat-messages (sendChatMessage)
        return "create"
    return "action"


def is_status(attr: Attribute) -> bool:
    """상태 컬럼: 허용값이 있고 이름이나 뜻이 상태인 컬럼 (표준 이름을 쓰는 문서도 설계 이름·라벨로 판단한다)."""
    return bool(attr.allowed_values) and bool(re.search(r"status|state|stage", attr.name) or "상태" in (attr.label or ""))
