"""회사별 설계 표준(프로필): 표준 파일을 읽고, 설계한 데이터 모델(설계 이름)을 표준 이름으로 바꾼다 (LLM 없음).

프로필 폴더 standards/<이름>/ (형식: standards/README.md)
  rules.yaml : 이름 대소문자·테이블 접두어·최대 길이·PK·FK 이름·감사 컬럼·타입 규칙
  words.csv  : 표준 단어 사전 (한글명 → 영문약어)
  guide.md   : 규칙으로 쓰기 어려운 서술형 지침 (데이터 모델 Agent 프롬프트에 그대로 넣는다)

설계(LLM)는 지금처럼 영문 snake_case 이름과 한글 라벨로 하고, 표준 이름은 코드가 한글 라벨을 단어 사전으로 나눠 조합한다.
같은 입력이면 항상 같은 이름이 나오고 토큰이 늘지 않는다. 사전에 없는 단어가 있으면 그 이름은 설계 이름을 쓰고
'표준 단어 미등록'으로 보고한다. data_model.json은 설계 이름 그대로 두고(다시 설계·사용자 수정의 기준),
ERD·테이블 정의서·DDL·DB 검증만 표준 이름을 쓴다. 대문자 표준은 PostgreSQL DDL에서 소문자로 쓴다 (ddl_model).
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.schemas.data_model import Attribute, DataModel, Entity
from app.agents.tools.model_validator import PG_RESERVED

STANDARDS_DIR = Path(__file__).resolve().parents[3] / "standards"
DATA_TYPES = {"bigint", "integer", "decimal", "varchar", "text", "boolean", "date", "timestamp", "jsonb", "uuid"}
KINDS = {"MASTER", "TRANSACTION", "HISTORY", "MAPPING", "CODE"}
TOKEN = re.compile(r"[가-힣]+|[A-Za-z0-9]+")
ASCII = re.compile(r"[A-Za-z0-9]+")
IDENTIFIER = re.compile(r"[A-Za-z_]")   # DB 이름은 영문자나 _로 시작해야 한다


class ConventionError(ValueError):
    """프로필을 읽을 수 없거나 형식이 틀림 (사용자에게 그대로 보여 준다)."""


@dataclass
class Profile:
    """설계 표준 프로필 하나."""
    key: str                                  # 폴더 이름 (--standard 값)
    name: str
    description: str = ""
    case: str = "lower"
    table_prefix: str = ""
    table_prefix_by_kind: dict[str, str] = field(default_factory=dict)   # 예: {CODE: TC_, HISTORY: TH_}
    suffixes: dict[str, str] = field(default_factory=dict)               # 예: {자: R} → 응답자 = 응답 + R
    max_length: int = 63
    primary_key: str = ""                     # 예: "{table}_SN" (빈 값이면 라벨로)
    foreign_key: str = "label"                # parent_pk | label
    audit_rename: dict[str, str] = field(default_factory=dict)
    audit_add: list[dict] = field(default_factory=list)
    types: dict[str, dict] = field(default_factory=dict)
    words: dict[str, str] = field(default_factory=dict)
    guide: str = ""

    @property
    def renames(self) -> bool:
        """설계 이름을 바꾸는 프로필인지 (기본 프로필은 지금 규칙 그대로라 아무것도 바꾸지 않는다)."""
        return bool(self.words or self.table_prefix or self.table_prefix_by_kind or self.case == "upper"
                    or self.primary_key or self.audit_rename or self.audit_add or self.types)

    def prefix(self, kind: str) -> str:
        """엔티티 종류(MASTER·CODE·HISTORY …)의 테이블 접두어."""
        return self.table_prefix_by_kind.get(kind, self.table_prefix)


def _read_words(path: Path, problems: list[str]) -> dict[str, str]:
    """표준 단어 사전 CSV (한글명, 영문약어[, 영문명, 설명]). 첫 줄이 머리글이면 건너뛴다."""
    words: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for number, row in enumerate(csv.reader(f), start=1):
            if not row or not "".join(row).strip() or (number == 1 and row[0].strip() == "한글명"):
                continue
            korean, abbr = (row + ["", ""])[0].strip(), (row + ["", ""])[1].strip()
            if not korean or not ASCII.fullmatch(abbr or "-"):
                problems.append(f"{path.name} {number}행: 한글명과 영문약어(영문·숫자)가 필요합니다 ({','.join(row)[:60]})")
            elif korean in words and words[korean] != abbr.upper():
                problems.append(f"{path.name} {number}행: '{korean}'이 다른 약어로 두 번 있습니다 ({words[korean]}, {abbr})")
            else:
                words[korean] = abbr.upper()
    return words


def load_profile(name: str, root: Path | None = None) -> Profile:
    """프로필을 폴더 이름이나 rules.yaml의 name으로 찾아 읽고 형식을 검사한다. 틀리면 ConventionError."""
    root = root or STANDARDS_DIR
    folder = root / name
    if not (folder / "rules.yaml").exists():
        folder = next((p.parent for p in sorted(root.glob("*/rules.yaml"))
                       if (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("name") == name), folder)
    if not (folder / "rules.yaml").exists():
        known = ", ".join(sorted(p.parent.name for p in root.glob("*/rules.yaml"))) or "없음"
        raise ConventionError(f"설계 표준 프로필을 찾을 수 없습니다: {name} (있는 프로필: {known})")
    try:
        rules = yaml.safe_load((folder / "rules.yaml").read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConventionError(f"{folder.name}/rules.yaml을 읽을 수 없습니다: {e}") from e
    problems: list[str] = []
    case = str(rules.get("case", "lower"))
    if case not in ("upper", "lower"):
        problems.append(f"case는 upper 또는 lower입니다 (지금: {case})")
    max_length = rules.get("max_length", 63)
    if not isinstance(max_length, int) or not 1 <= max_length <= 63:
        problems.append(f"max_length는 1~63 정수입니다 (지금: {max_length})")
    primary_key = str(rules.get("primary_key") or "")
    if primary_key and "{table}" not in primary_key:
        problems.append(f"primary_key 틀에는 {{table}}이 있어야 합니다 (지금: {primary_key})")
    foreign_key = str(rules.get("foreign_key", "label"))
    if foreign_key not in ("parent_pk", "label"):
        problems.append(f"foreign_key는 parent_pk 또는 label입니다 (지금: {foreign_key})")
    audit = rules.get("audit_columns") or {}
    rename = {str(k): str(v) for k, v in (audit.get("rename") or {}).items()}
    add = list(audit.get("add") or [])
    for column in add:
        if not isinstance(column, dict) or not column.get("name") or column.get("type") not in DATA_TYPES:
            problems.append(f"audit_columns.add 항목에는 name과 type({', '.join(sorted(DATA_TYPES))})이 필요합니다: {column}")
    types = {str(k): dict(v or {}) for k, v in (rules.get("types") or {}).items()}
    for logical, rule in types.items():
        if logical not in DATA_TYPES or rule.get("type") not in DATA_TYPES:
            problems.append(f"types.{logical}: 논리 타입과 type은 {', '.join(sorted(DATA_TYPES))} 중 하나입니다")
    by_kind = {str(k).upper(): str(v) for k, v in (rules.get("table_prefix_by_kind") or {}).items()}
    for kind in by_kind:
        if kind not in KINDS:
            problems.append(f"table_prefix_by_kind의 종류는 {', '.join(sorted(KINDS))} 중 하나입니다 (지금: {kind})")
    suffixes = {str(k): str(v).upper() for k, v in (rules.get("suffixes") or {}).items()}
    for korean, abbr in suffixes.items():
        if not re.fullmatch(r"[가-힣]+", korean) or not ASCII.fullmatch(abbr):
            problems.append(f"suffixes는 한글 → 영문약어입니다 (지금: {korean}: {abbr})")
    words = _read_words(folder / "words.csv", problems) if (folder / "words.csv").exists() else {}
    if problems:
        raise ConventionError(f"설계 표준 프로필 {folder.name} 형식 오류:\n- " + "\n- ".join(problems))
    guide = (folder / "guide.md").read_text(encoding="utf-8").strip() if (folder / "guide.md").exists() else ""
    return Profile(key=folder.name, name=str(rules.get("name") or folder.name), description=str(rules.get("description") or ""),
                   case=case, table_prefix=str(rules.get("table_prefix") or ""), table_prefix_by_kind=by_kind,
                   suffixes=suffixes, max_length=max_length, primary_key=primary_key, foreign_key=foreign_key,
                   audit_rename=rename, audit_add=add, types=types, words=words, guide=guide)


@lru_cache(maxsize=16)
def _cached(name: str) -> Profile:
    return load_profile(name)


def profile_of(options: dict | None) -> Profile | None:
    """실행 설정(options.standard)의 프로필 (지정하지 않았으면 None = 지금 규칙). 실행 시작 때 형식을 이미 검사했다."""
    key = (options or {}).get("standard")
    return _cached(key) if key else None


def guide_block(options: dict | None) -> str:
    """데이터 모델 Agent에 넘길 서술형 지침 블록 (없으면 빈 문자열). 이름 규칙은 코드가 적용하므로 넣지 않는다."""
    profile = profile_of(options)
    if not profile or not profile.guide:
        return ""
    return f"## 설계 표준 지침 ({profile.name}. 이름 규칙은 시스템이 적용하므로 영문 설계 이름은 지금처럼 쓴다)\n{profile.guide}"


def list_profiles(root: Path | None = None) -> list[tuple[str, Profile | None, str]]:
    """(폴더 이름, 프로필 또는 None, 형식 오류) 목록."""
    out = []
    for path in sorted((root or STANDARDS_DIR).glob("*/rules.yaml")):
        try:
            out.append((path.parent.name, load_profile(path.parent.name, root), ""))
        except ConventionError as e:
            out.append((path.parent.name, None, str(e)))
    return out


def _segment(word: str, words: dict[str, str], longest: int) -> tuple[list[str], list[str]]:
    """한글 낱말을 사전 단어로 나눈다 (앞에서부터 가장 긴 단어). (약어 목록, 사전에 없는 조각)"""
    abbrs, missing, rest, i = [], [], "", 0
    while i < len(word):
        piece = next((word[i:i + n] for n in range(min(longest, len(word) - i), 0, -1) if word[i:i + n] in words), "")
        if piece:
            if rest:
                missing.append(rest)
                rest = ""
            abbrs.append(words[piece])
            i += len(piece)
        else:
            rest += word[i]
            i += 1
    if rest:
        missing.append(rest)
    return abbrs, missing


def standard_name(label: str, words: dict[str, str], suffixes: dict[str, str] | None = None) -> tuple[str | None, list[str]]:
    """한글 라벨 → 표준 이름 (예: '회원 승인 이력' → MBR_APRV_HST). 영문·숫자 조각(ID, IP, km)은 그대로 대문자로 쓴다.
    사전으로 다 나뉘지 않는 한글 낱말이 있으면 (None, 그 낱말들). 낱말 전체를 돌려줘야 사전에 넣을 단어가 된다
    (예: '전달'을 '전'+'달'로 잘못 나누고 '달'만 보고하지 않게).
    suffixes(예: {자: R})는 사전에 없는 낱말의 끝말을 떼어 앞부분이 사전으로 나뉘면 마지막 약어에 붙인다 (응답자 → RSPNSR)."""
    longest = max((len(w) for w in words), default=1)
    parts, missing = [], []
    for token in TOKEN.findall(label or ""):
        if ASCII.fullmatch(token):
            parts.append(token.upper())
            continue
        abbrs, miss = _segment(token, words, longest)
        if miss:
            end = next((s for s in sorted(suffixes or {}, key=len, reverse=True)
                        if token.endswith(s) and len(token) > len(s)), "")
            stem, stem_miss = _segment(token[:-len(end)], words, longest) if end else ([], [token])
            if stem and not stem_miss:
                parts += [*stem[:-1], stem[-1] + suffixes[end]]
                continue
            missing.append(token)
        else:
            parts += abbrs
    if missing or not parts:
        return None, missing
    return "_".join(parts), []


@dataclass
class NameRow:
    """표준 이름 대응표 한 줄 (standard_names.csv)."""
    table: str            # 설계 테이블 이름
    column: str           # 설계 컬럼 이름 (테이블 줄이면 빈 값)
    label: str
    physical_table: str
    physical_column: str
    how: str              # 사전 / 규칙 / 설계 이름 (사전에 없는 단어) / 추가 (표준 컬럼)


@dataclass
class ConventionReport:
    """표준 적용 결과 (검토 보고서 '표준 준수')."""
    profile: str
    profile_key: str
    rows: list[NameRow] = field(default_factory=list)
    missing: Counter = field(default_factory=Counter)                       # 사전에 없는 조각 → 횟수
    missing_examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    violations: list[str] = field(default_factory=list)
    guide: str = ""

    def count(self, kind: str) -> tuple[int, int]:
        """(사전·규칙으로 만든 이름 수, 전체 수). kind: table / column."""
        rows = [r for r in self.rows if (not r.column) == (kind == "table") and r.how != "추가"]
        return sum(r.how in ("사전", "규칙") for r in rows), len(rows)

    def logical(self) -> dict[tuple[str, str], tuple[str, str]]:
        """(표준 테이블, 표준 컬럼) → (설계 테이블, 설계 컬럼)."""
        return {(r.physical_table, r.physical_column): (r.table, r.column) for r in self.rows}


def apply_profile(model: DataModel, profile: Profile) -> tuple[DataModel, ConventionReport]:
    """설계 이름의 데이터 모델을 표준 이름의 데이터 모델로 바꾼다. 원래 모델은 바꾸지 않는다."""
    report = ConventionReport(profile=profile.name, profile_key=profile.key, guide=profile.guide)
    cased = (lambda s: s.upper()) if profile.case == "upper" else (lambda s: s.lower())   # noqa: E731

    def convert(label: str, fallback: str, where: str, prefix: str = "") -> tuple[str, str]:
        name, missing = standard_name(label, profile.words, profile.suffixes) if profile.words else (None, [])
        for piece in missing:
            report.missing[piece] += 1
            if len(report.missing_examples[piece]) < 3:
                report.missing_examples[piece].append(where)
        if name and not IDENTIFIER.match(prefix + name):   # '2단계 인증' → 2_STEP_… 처럼 숫자로 시작하면 DB 이름이 될 수 없다
            report.violations.append(f"숫자로 시작하는 표준 이름이라 설계 이름을 씀: {where} '{label}' → {name}")
            name = None
        return (cased(name), "사전") if name else (cased(fallback), "설계 이름" if profile.words else "규칙")

    # 테이블: 라벨 → 표준 이름 (앞에 종류별 접두어), 겹치면 _2 …
    tables: dict[str, tuple[str, str]] = {}
    physical: dict[str, str] = {}
    for e in model.entities:
        prefix = cased(profile.prefix(e.kind))
        base, how = convert(e.label, e.entity_id, f"테이블 {e.entity_id}", prefix)
        name, n = base, 1
        while prefix + name in physical.values():
            n += 1
            name = f"{base}_{n}"
        if n > 1:
            report.violations.append(f"테이블 이름이 겹쳐 번호를 붙임: {e.entity_id} → {prefix}{name}")
        tables[e.entity_id] = (name, how)
        physical[e.entity_id] = prefix + name
    for e in model.entities:
        report.rows.append(NameRow(e.entity_id, "", e.label, physical[e.entity_id], "", tables[e.entity_id][1]))

    # 기본키: 틀이 있으면 틀로 (예: {table}_SN), 없으면 라벨로
    pk_name: dict[str, str] = {}
    for e in model.entities:
        pk = e.pk()
        if len(pk) == 1 and profile.primary_key:
            name = cased(profile.primary_key.format(table=tables[e.entity_id][0]))
            # 테이블 이름이 숫자로 시작하면('1:1 문의' → 1_1_INQRY) 접두어 없는 기본키 틀도 숫자로 시작한다 → 설계 이름
            pk_name[e.entity_id] = name if IDENTIFIER.match(name) else cased(pk[0].name)

    columns: dict[tuple[str, str], str] = {}
    for e in model.entities:
        taken: set[str] = set()
        for a in e.attributes:
            if a.name in profile.audit_rename:
                name, how = cased(profile.audit_rename[a.name]), "규칙"
            elif a.primary_key and e.entity_id in pk_name:
                name, how = pk_name[e.entity_id], "규칙"
            elif (a.references and profile.foreign_key == "parent_pk" and a.name == f"{a.references}_id"
                  and a.references in pk_name):
                name, how = pk_name[a.references], "규칙"     # 역할 없는 외래키 = 참조 테이블 기본키 이름
            else:
                name, how = convert(a.label, a.name, f"{e.entity_id}.{a.name}")
            unique, n = name, 1
            while unique in taken:
                n += 1
                unique = f"{name}_{n}"
            if n > 1:
                report.violations.append(f"{physical[e.entity_id]}: 컬럼 이름이 겹쳐 번호를 붙임 {a.name} → {unique}")
            taken.add(unique)
            columns[(e.entity_id, a.name)] = unique
            report.rows.append(NameRow(e.entity_id, a.name, a.label, physical[e.entity_id], unique, how))

    entities = [_entity(e, physical, columns, profile, cased) for e in model.entities]
    for e in entities:
        for a in e.attributes:
            if (e.entity_id, a.name) not in {(r.physical_table, r.physical_column) for r in report.rows}:
                report.rows.append(NameRow(next(t for t, p in physical.items() if p == e.entity_id), "", a.label,
                                           e.entity_id, a.name, "추가"))
    relations = [r.model_copy(update={"parent": physical.get(r.parent, r.parent), "child": physical.get(r.child, r.child),
                                      "fk_attribute": columns.get((r.child, r.fk_attribute), r.fk_attribute)})
                 for r in model.relations]
    for name in sorted({r.physical_table for r in report.rows} | {r.physical_column for r in report.rows if r.physical_column}):
        if len(name) > profile.max_length:
            report.violations.append(f"이름 길이 {len(name)}자 > {profile.max_length}자: {name}")
        if name.lower() in PG_RESERVED:
            report.violations.append(f"예약어인 이름: {name} (DDL에서 따옴표 처리)")
    return DataModel(entities=entities, relations=relations), report


def _entity(e: Entity, physical: dict[str, str], columns: dict[tuple[str, str], str], profile: Profile, cased) -> Entity:
    """엔티티 하나를 표준 이름·타입으로 바꾸고 표준 컬럼(audit_columns.add)을 더한다."""
    col = lambda c: columns.get((e.entity_id, c), c)   # noqa: E731
    attrs = []
    for a in e.attributes:
        update = {"name": col(a.name), "references": physical.get(a.references, a.references) if a.references else ""}
        rule = profile.types.get(a.data_type)
        if rule:
            update.update(data_type=rule["type"], length=int(rule.get("length") or 0),
                          allowed_values=[str(v) for v in rule.get("values") or a.allowed_values])
        attrs.append(a.model_copy(update=update))
    names = {a.name for a in attrs}
    for extra in profile.audit_add:
        name = cased(str(extra["name"]))
        if name not in names:
            attrs.append(Attribute(name=name, label=str(extra.get("label") or ""), data_type=extra["type"],
                                   length=int(extra.get("length") or 0), nullable=bool(extra.get("nullable", True))))
    return e.model_copy(update={
        "entity_id": physical[e.entity_id], "attributes": attrs,
        "parents": [physical.get(p, p) for p in e.parents],
        "unique_together": [[col(c) for c in g] for g in e.unique_together],
        "exactly_one_of": [[col(c) for c in g] for g in e.exactly_one_of],
        "required_when": [r.model_copy(update={"column": col(r.column), "when_column": col(r.when_column)})
                          for r in e.required_when],
        "checks": [c.model_copy(update={"column": col(c.column), "value": col(c.value)}) for c in e.checks],
    })


def ddl_model(model: DataModel, profile: Profile | None) -> DataModel:
    """DDL·DB 검증에 쓸 모델: 대문자 표준이면 이름을 소문자로 (PostgreSQL은 따옴표 없는 이름을 소문자로 바꾸므로)."""
    if profile is None or profile.case != "upper":
        return model
    physical = {e.entity_id: e.entity_id.lower() for e in model.entities}
    columns = {(e.entity_id, a.name): a.name.lower() for e in model.entities for a in e.attributes}
    lower = Profile(key="", name="", case="lower")
    entities = [_entity(e, physical, columns, lower, str.lower) for e in model.entities]
    relations = [r.model_copy(update={"parent": r.parent.lower(), "child": r.child.lower(),
                                      "fk_attribute": r.fk_attribute.lower()}) for r in model.relations]
    return DataModel(entities=entities, relations=relations)


def names_csv(report: ConventionReport) -> str:
    """표준 이름 대응표 CSV 내용 (엑셀에서 한글이 깨지지 않게 BOM 포함)."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["설계 테이블", "설계 컬럼", "한글명", "표준 테이블", "표준 컬럼", "만든 방법"])
    for r in report.rows:
        w.writerow([r.table, r.column, r.label, r.physical_table, r.physical_column, r.how])
    return "\ufeff" + out.getvalue()
