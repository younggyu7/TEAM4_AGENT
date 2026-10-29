"""DataModel → Mermaid erDiagram (DDL과 같은 모델에서 생성)."""
from __future__ import annotations

from app.schemas.design import DataModel

_CARDINALITY = {
    ("1:N", False): "||--o{",
    ("1:N", True): "|o--o{",
    ("1:1", False): "||--o|",
    ("1:1", True): "|o--o|",
}


def _comment(text: str) -> str:
    """Mermaid 속성 주석용 문자열 정리 (따옴표·줄바꿈 제거, 60자 제한)."""
    return (text or "").replace('"', "'").replace("\n", " ")[:60]


def render_mermaid(model: DataModel) -> str:
    """데이터 모델을 Mermaid erDiagram으로 그린다 (속성·키·관계 카디널리티)."""
    lines = ["erDiagram"]
    for e in model.entities:
        lines.append(f"    {e.entity_id} {{")
        for a in e.attributes:
            keys = ",".join(k for k, on in (("PK", a.primary_key), ("FK", bool(a.references)), ("UK", a.unique)) if on)
            lines.append(f"        {a.data_type} {a.name}{' ' + keys if keys else ''} \"{_comment(a.label)}\"")
        lines.append("    }")
    for r in model.relations:
        lines.append(f"    {r.parent} {_CARDINALITY[(r.cardinality, r.optional)]} {r.child} : \"{r.fk_attribute}\"")
    return "\n".join(lines) + "\n"
