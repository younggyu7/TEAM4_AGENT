"""사용자가 고친 데이터 모델 반영 (결정적, LLM 없음).

기준 버전을 만든 실행의 모델(원본)과 사용자가 고친 data_model.json을 엔티티 단위로 비교한다.
- 새로 만들거나 고친 엔티티 → user_edited(고정): 이후 LLM이 다시 설계하지 않는다
- 이름만 바꾼 엔티티(지운 엔티티와 새 엔티티의 컬럼이 거의 같음) → 다른 엔티티의 참조·상위 엔티티를 새 이름으로 바꾼다
- 지운 엔티티 → 고정되지 않은 다른 엔티티에서 그 엔티티를 참조하던 외래키 컬럼과 상위 엔티티 연결을 지운다
  (고정된 엔티티의 컬럼은 건드리지 않는다. 남은 참조 오류는 검증에서 '직접 확인'으로 보고된다)
파일에서 user_edited를 false로 바꾸면 고정이 풀린다. 관계(relations)와 관련 요구사항은 정규화에서 다시 계산하므로 비교하지 않는다.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.data_model import DataModel, Entity

# 엔티티 비교 항목 (사용자가 고칠 수 있는 내용)
COMPARED = {"label", "description", "kind", "attributes", "function_ids", "parents", "unique_together",
            "exactly_one_of", "required_when", "checks"}
AUDIT_NAMES = {"created_at", "updated_at"}
RENAME_SIMILARITY = 0.6   # 지운 엔티티와 새 엔티티의 컬럼 이름 겹침(자카드)이 이 이상이면 이름 변경으로 본다


class ReworkSummary(BaseModel):
    """데이터 설계 다시 만들기 요약: 반영한 사용자 수정과 LLM이 다시 설계한 범위."""
    edited: list[str] = Field(default_factory=list)          # 사용자가 고친 엔티티 (고정)
    added: list[str] = Field(default_factory=list)           # 사용자가 새로 만든 엔티티 (고정)
    removed: list[str] = Field(default_factory=list)         # 사용자가 지운 엔티티 (다시 만들지 않음)
    renamed: dict[str, str] = Field(default_factory=dict)    # 이전 이름 → 새 이름 (고정)
    dropped_columns: list[str] = Field(default_factory=list) # 지운 엔티티를 참조하던 컬럼 (엔티티.컬럼)
    full: bool = False                                       # 전체를 다시 설계함 (고정 엔티티는 유지)
    redesigned: list[str] = Field(default_factory=list)      # LLM이 컬럼을 다시 설계한 엔티티
    skipped: list[str] = Field(default_factory=list)         # 의견·기능 변경과 관련 있지만 사용자 수정이라 건너뛴 엔티티
    # 기능 명세 단계 버전에서 시작할 때: 이어받은 모델의 버전과 그 뒤 바뀐 기능
    inherited_from: str = ""
    changed_functions: list[str] = Field(default_factory=list)
    added_functions: list[str] = Field(default_factory=list)
    removed_functions: list[str] = Field(default_factory=list)

    def has_edits(self) -> bool:
        """사용자 수정이 하나라도 있는지."""
        return bool(self.edited or self.added or self.removed or self.renamed)

    def edit_text(self) -> str:
        """사용자 수정 요약 한 줄 (없으면 빈 문자열)."""
        parts = [f"{label} {len(items)}개 ({', '.join(items)})" for label, items in (
            ("고침", self.edited), ("새로 만듦", self.added), ("지움", self.removed),
            ("이름 바꿈", [f"{a}→{b}" for a, b in self.renamed.items()])) if items]
        if self.dropped_columns:
            parts.append(f"지운 엔티티를 참조하던 컬럼 제거 {len(self.dropped_columns)}개 ({', '.join(self.dropped_columns)})")
        return " · ".join(parts)


def _content(e: Entity) -> dict:
    """사용자 수정 여부를 가릴 비교 내용."""
    return e.model_dump(mode="json", include=COMPARED)


def _columns(e: Entity) -> set[str]:
    """이름 변경 판정용 컬럼 이름 (기본키·감사 컬럼 제외)."""
    return {a.name for a in e.attributes if not a.primary_key and a.name not in AUDIT_NAMES}


def _similarity(a: Entity, b: Entity) -> float:
    """두 엔티티의 컬럼 이름 겹침 (자카드)."""
    x, y = _columns(a), _columns(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def apply_user_edits(original: DataModel | None, edited: DataModel) -> tuple[DataModel, ReworkSummary]:
    """사용자가 고친 모델을 원본과 비교해 고정 표시를 하고, 지우거나 이름을 바꾼 엔티티의 참조를 정리한다."""
    summary = ReworkSummary()
    if original is None:
        return edited, summary
    before = {e.entity_id: e for e in original.entities}
    after = {e.entity_id: e for e in edited.entities}
    removed = [n for n in before if n not in after]
    added = [n for n in after if n not in before]
    pairs = sorted(((_similarity(before[r], after[a]), r, a) for r in removed for a in added), reverse=True)
    for score, old, new in pairs:
        if score >= RENAME_SIMILARITY and old not in summary.renamed and new not in summary.renamed.values():
            summary.renamed[old] = new
    summary.removed = [n for n in removed if n not in summary.renamed]
    summary.added = [n for n in added if n not in summary.renamed.values()]

    entities = []
    for e in edited.entities:
        prev = before.get(e.entity_id)
        changed = prev is None or _content(e) != _content(prev)
        if changed and prev is not None:
            summary.edited.append(e.entity_id)
        entities.append(e.model_copy(update={"user_edited": e.user_edited or changed}))

    gone = set(summary.removed)
    for idx, e in enumerate(entities):
        attrs = []
        for a in e.attributes:
            if a.references in summary.renamed:          # 이름 변경은 사용자 의도가 분명하므로 고정 엔티티에도 반영
                a = a.model_copy(update={"references": summary.renamed[a.references]})
            elif a.references in gone and not e.user_edited:
                summary.dropped_columns.append(f"{e.entity_id}.{a.name}")
                continue
            attrs.append(a)
        parents = [summary.renamed.get(p, p) for p in e.parents if not (p in gone and not e.user_edited)]
        entities[idx] = e.model_copy(update={"attributes": attrs, "parents": parents})
    return DataModel(entities=entities), summary
