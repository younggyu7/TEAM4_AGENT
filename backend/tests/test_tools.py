"""결정적 도구 단위 테스트 (LLM 불필요)."""
import pytest

from app.schemas.design import Attribute, DataModel, Entity
from app.agents.flow.state import finding, reconcile_issues
from app.agents.tools.db_validator import validate_ddl
from app.agents.tools.ddl import render_script
from app.agents.tools.doc_quality import build_sources, check_document
from app.agents.tools.document_parser import parse_document
from app.agents.tools.erd import render_mermaid
from app.agents.tools.search import quote_conflict
from app.agents.tools.model_validator import normalize_model, validate_model


def test_parse_rfp_requirement_tables(rfp_path):
    if not rfp_path.exists():
        pytest.skip("RFP 샘플 없음")
    blocks = parse_document(rfp_path)
    tables = [b for b in blocks if b.block_type == "requirement_table"]
    assert len(tables) >= 30
    sfr3 = next(b for b in tables if b.fields.get("code") == "SFR-003")
    assert "담당자 배정" in sfr3.text and sfr3.fields["category"] == "기능 요구사항"
    sources = build_sources(blocks)
    keys = [s.source_key for s in sources]
    assert len(keys) == len(set(keys)), "추출 단위 키는 유일해야 함"
    ecr_common = next(s for s in sources if s.name.startswith("하드웨어 및 소프트웨어 공통"))
    assert len(ecr_common.block_ids) == 2, "(1/2),(2/2)로 이어지는 표는 하나로 병합"
    kinds = {f.kind for f in check_document(blocks, sources)}
    assert {"DOC_DUPLICATE_REQ_CODE", "DOC_DETAIL_MISSING", "DOC_NOT_IN_LIST"} <= kinds


def test_parse_text_chunks(sample_doc):
    blocks = parse_document(sample_doc)
    sources = build_sources(blocks)
    assert len(blocks) == 4 and len(sources) == 1 and sources[0].source_key == "C01"


def _model(ref="app_user"):
    return DataModel(entities=[
        Entity(entity_id="ServiceRequest", label="서비스 요청", kind="TRANSACTION", function_ids=["FN-1"], attributes=[
            Attribute(name="service_request_id", data_type="bigint", primary_key=True),
            Attribute(name="status", data_type="varchar", length=20, nullable=False, allowed_values=["OPEN", "DONE"]),
            Attribute(name="requester_id", data_type="integer", references=ref),
            Attribute(name="user", data_type="varchar"),
        ]),
        Entity(entity_id="app_user", label="사용자", attributes=[
            Attribute(name="app_user_id", data_type="bigint", primary_key=True),
            Attribute(name="email", data_type="varchar", length=200, unique=True),
        ]),
    ])


def test_normalize_and_validate():
    model, notes = normalize_model(_model(), {"FN-1": ["REQ-1"]})
    sr = model.entity("service_request")
    assert sr is not None and sr.requirement_ids == ["REQ-1"]
    assert next(a for a in sr.attributes if a.name == "requester_id").data_type == "bigint", "FK 타입 정렬"
    assert any("FK 타입" in n for n in notes)
    assert len(model.relations) == 1 and model.relations[0].parent == "app_user"
    kinds = {i.kind for i in validate_model(model)}
    assert kinds == {"MODEL_RESERVED_WORD"}

    broken, _ = normalize_model(_model(ref="ghost"), {"FN-1": []})
    errors = [i for i in validate_model(broken) if i.severity == "ERROR"]
    assert errors and errors[0].kind == "MODEL_FK_TARGET_MISSING" and errors[0].target_id == "service_request"


def test_design_standard_checks():
    """설계 표준: 감사 컬럼은 자동 추가, 평문 인증 정보는 오류, 기본키·외래키 명명 위반은 경고."""
    model = DataModel(entities=[
        Entity(entity_id="member", label="회원", attributes=[
            Attribute(name="member_id", data_type="bigint", primary_key=True),
            Attribute(name="password", label="비밀번호", data_type="varchar", length=100),        # 평문 → 오류
            Attribute(name="password_hash", label="비밀번호 해시", data_type="varchar", length=255),
            Attribute(name="password_changed_at", label="비밀번호 변경 일시", data_type="timestamp"),
            Attribute(name="reset_token", label="재설정 토큰", data_type="varchar", length=100),  # 평문 → 오류
            Attribute(name="token_type", data_type="varchar", length=20, allowed_values=["EMAIL", "SMS"]),
            Attribute(name="is_pinned", data_type="boolean"),
            Attribute(name="solution_code", label="솔루션 코드", data_type="varchar", length=20),
            Attribute(name="created_at", data_type="timestamp", nullable=False),
        ]),
        Entity(entity_id="login_log", label="로그인 기록", attributes=[
            Attribute(name="seq", data_type="integer", primary_key=True),                         # PK 표준 위반
            Attribute(name="member", data_type="bigint", references="member"),                    # FK 명명 위반
        ]),
    ])
    model, notes = normalize_model(model, {})
    member, log = model.entity("member"), model.entity("login_log")
    assert [a.name for a in member.attributes if a.name.endswith("_at")][-1] == "updated_at", "감사 컬럼 자동 추가"
    assert {"created_at", "updated_at"} <= {a.name for a in log.attributes}
    assert any("감사 컬럼" in n for n in notes)

    issues = {(i.kind, i.target_id): i for i in validate_model(model)}
    secret = issues[("MODEL_PLAINTEXT_SECRET", "member")]
    assert secret.severity == "ERROR" and secret.target_agent == "data_modeler"
    assert "password(" in secret.message and "reset_token(" in secret.message
    assert "password_hash" not in secret.message.split("→")[0] and "solution_code" not in secret.message
    assert issues[("MODEL_PK_STANDARD", "login_log")].severity == "WARNING"
    assert issues[("MODEL_FK_NAMING", "login_log")].target_agent == "none"


def test_ddl_erd_and_sqlite_validation():
    model, _ = normalize_model(_model(), {"FN-1": []})
    sql = render_script(model, 1)
    assert "GENERATED BY DEFAULT AS IDENTITY" in sql
    assert '"user" VARCHAR(255)' in sql, "예약어 컬럼은 따옴표 처리"
    assert "CHECK (status IN ('OPEN', 'DONE'))" in sql
    assert "FOREIGN KEY (requester_id) REFERENCES app_user (app_user_id)" in sql
    assert "app_user |o--o{ service_request" in render_mermaid(model)
    result = validate_ddl(model, 1, "test")
    assert result.passed, result.errors
    assert result.engine == "sqlite" and result.foreign_keys == 1


def test_postgres_unreachable_falls_back_to_sqlite():
    """검증용 PostgreSQL에 접속할 수 없으면 실패(→ 모델 수정)가 아니라 SQLite 대체 검증으로 기록한다."""
    model, _ = normalize_model(_model(), {"FN-1": []})
    result = validate_ddl(model, 1, "test", "postgresql://u:secret@127.0.0.1:1/none")
    assert result.passed and result.engine == "sqlite"
    assert "접속 실패" in result.note and "docker compose up -d validation-db" in result.note
    assert "secret" not in result.note, "접속 비밀번호는 보고서에 남기지 않음"


def test_reconcile_issue_lifecycle():
    owns = lambda k: k.startswith("MODEL_")  # noqa: E731
    first = reconcile_issues([], [finding("MODEL_NO_PK", "ERROR", "x", "data_modeler", "t1")], owns, 2)
    assert first[0].issue_id == "ISS-0001" and first[0].status == "OPEN"
    tried = [first[0].model_copy(update={"attempts": 2})]
    again = reconcile_issues(tried, [finding("MODEL_NO_PK", "ERROR", "x", "data_modeler", "t1")], owns, 2)
    assert again[0].status == "UNRESOLVED", "시도 한도를 넘기면 미해결로 보존"
    fixed = reconcile_issues(first, [], owns, 2)
    assert fixed[0].status == "RESOLVED"


@pytest.mark.parametrize("quote, line, same", [
    ("이벤트 발생 후 10초 이내 알림 전달", "• (성능기준) 이벤트 발생 후 10초 이내 알림 전달", True),
    ("이벤트 발생 후 5초 이내 알림 전달", "• (성능기준) 이벤트 발생 후 10초 이내 알림 전달", False),
    ("TLS 1.2 기반 통신 암호화", "• (전송 데이터 암호화) TLS 1.3 기반 통신 암호화, SSL 인증서 적용", False),
    ("오류코드는 모든 사용자에게 공개되어야", "• (오류확인) 오류코드는 관리자에게만 공개되어야 하며", False),
    ("SIEM 연동 시에는 보안로그 전송 허용", "SIEM 연동 시에는 보안로그 전송 금지", False),
    ("정기점검 보고서를 제출해야 한다", "- 정기점검 보고서를 제출하여야 함", True),
])
def test_quote_conflict_keeps_numbers_conditions_and_targets(quote, line, same):
    assert (quote_conflict(quote, line) is None) == same


def _research(blocks, claimed: str, quote: str) -> dict:
    from app.agents.roles import document_analyst
    from app.schemas.design import Evidence, Requirement
    req = Requirement(requirement_id="R-1", type="FUNCTIONAL", scope="SYSTEM", category="기능", title="t",
                      description="", evidence=[Evidence(source_block_id=claimed, quote=quote)])
    state = {"blocks": [b.model_dump() for b in blocks], "requirements": [req.model_dump()],
             "next_action": {"mode": "research", "scope": ["R-1"]}}
    return document_analyst.run(state)["requirements"][0]["evidence"][0]


def test_research_relinks_same_quote_but_not_altered_quote(tmp_path):
    path = tmp_path / "doc.md"
    path.write_text("# 요구사항\n\n## 알림\n\n이벤트 발생 후 10초 이내 알림을 전달하여야 함\n\n"
                    "## 로그\n\n접속기록은 최소 1년 이상 보관하여야 함\n", encoding="utf-8")
    blocks = parse_document(path)
    alarm = next(b for b in blocks if "10초" in b.text).source_block_id
    log_block = next(b for b in blocks if "1년" in b.text).source_block_id
    moved = _research(blocks, log_block, "10초 이내 알림을 전달하여야 함")
    assert moved["verified"] and moved["source_block_id"] == alarm
    assert moved["quote"] == "10초 이내 알림을 전달하여야 함", "원문에 그대로 있으면 인용문은 바꾸지 않는다"
    altered = _research(blocks, alarm, "이벤트 발생 후 5초 이내 알림을 전달하여야 함")
    assert not altered["verified"], "숫자가 다른 인용문은 비슷한 원문으로 바꿔 확인하지 않는다"
    assert altered["quote"] == "이벤트 발생 후 5초 이내 알림을 전달하여야 함"


def test_prefix_inconsistent_in_same_category():
    from app.schemas.design import BlockLocation, DocumentBlock

    def table(i, code, name):
        return DocumentBlock(document_id="d", document_version="v", source_block_id=f"B{i:04d}",
                             block_type="requirement_table", location=BlockLocation(order=i), text=name,
                             fields={"code": code, "name": name, "category": "보안 요구사항", "details": name})
    blocks = [table(1, "SER-001", "인증"), table(2, "SER-002", "암호화"), table(3, "SER-003", "권한"),
              table(4, "SR-004", "로그")]
    issues = check_document(blocks, build_sources(blocks))
    assert [(i.kind, i.target_id) for i in issues] == [("DOC_CODE_PREFIX_INCONSISTENT", "SR-004")]


def test_category_checks_ignore_notation_umbrella_headings_and_bullets():
    """고객요구사항7~10에서 본 표기 차이는 경고하지 않고, 실제 원문 문제만 경고한다."""
    from app.schemas.design import BlockLocation, DocumentBlock

    def table(i, code, name, category, heading):
        return DocumentBlock(document_id="d", document_version="v", source_block_id=f"B{i:04d}",
                             block_type="requirement_table", text=name,
                             location=BlockLocation(order=i, heading_path=["Ⅲ. 요구사항", heading]),
                             fields={"code": code, "name": name, "category": category, "details": name})
    listed = [["ECR-001", "서버 구성"], ["ECR-002", "백업 장비"], ["SFR-001", "‣ 상품 등록"], ["SFR-002", "출장 신청"],
              ["PER-001", "시스템 지원 효율성"], ["PER-002", "응답 시간"], ["SER-002", "접근 통제"], ["PSR-001", "교육"],
              ["SER-001", "테스트 방안"]]
    blocks = [DocumentBlock(document_id="d", document_version="v", source_block_id="B0000", block_type="table",
                            location=BlockLocation(order=0), text="요구사항 목록", rows=[["번호", "명칭"], *listed]),
              table(1, "ECR-001", "서버 구성", "시스템 장비구성(ECR)", "가. 시스템 장비 구성 요구사항"),   # 표기 차이
              table(2, "SFR-001", "상품 등록 (핵심 기능항목)", "MRO 지급 신청서 - 모바일", "나. 기능 요구사항"),  # 업무 묶음 분류
              table(3, "SFR-002", "출장 신청", "시내출장 - 모바일", "나. 기능 요구사항"),
              table(4, "PER-001", "시스템 자원 효율성", "성능 (PER)", "다. 비기능 요구사항"),   # 여러 분류를 묶는 제목
              table(5, "PER-002", "응답 시간", "성능 (PER)", "다. 비기능 요구사항"),
              table(6, "SER-002", "접근 통제", "보안 (SER)", "다. 비기능 요구사항"),
              table(7, "PSR-001", "교육", "지원 (PSR)", "다. 비기능 요구사항"),                # 분류에 접두어를 적음
              table(8, "ECR-002", "백업 장비", "시스템 장비구성 요구사항", "라. 성능 요구사항"),  # 다른 분류 제목 아래
              table(9, "SER-001", "테스트 방안", "테스트 요구사항", "마. 테스트 요구사항")]       # 접두어와 다른 분류
    issues = check_document(blocks, build_sources(blocks))
    assert sorted((i.kind, i.target_id) for i in issues) == [
        ("DOC_CODE_CATEGORY_MISMATCH", "SER-001"), ("DOC_HEADING_CATEGORY_MISMATCH", "ECR-002"),
        ("DOC_NAME_MISMATCH", "PER-001#1")]
