"""요구사항 빠진 항목 채우기: 원문 목록 항목 중 요구사항에 담기지 않은 것을 코드로 찾고(tools/source_items, LLM 없음),
그 단위만 빠진 항목을 짚어 한 번 더 추출해 더한다. 문서 형식(번호 체계·서식 문구)에 기대지 않는다."""
import re

from app.agents.llm import client as llm_module
from app.agents.llm.client import LLMClient, LLMError
from app.agents.roles import requirements_analyst as ra
from app.agents.roles.requirements_analyst import EvidenceOut, ExtractionOut, RequirementOut
from app.agents.tools.source_items import boilerplate, list_items, uncovered_items
from app.core.config import override_settings
from app.schemas.document import BlockLocation, DocumentBlock, RequirementSource
from app.schemas.requirement import Evidence, Requirement

LONG = """[요구사항 표] 고유번호: SFR-001 | 명칭: 근태관리
세부내용:
❍ 근무연수 기반 연차 발생 및 사용 관리 기능
❍ 근태확정 관리
- 일별 근태마감 기능
- 근태 결과 집계·마감 및 일괄 업로드 기능
❍ 근태설정 및 현황관리
- 마이너스 연차 허용 여부 설정 기능
- 출퇴근시간, 근태사용 및 휴게시간 설정 기능인데 줄이
바뀌어 이어지는 문장
산출정보: 단계별 개발 산출물"""
SHORT = """[요구사항 표] 고유번호: SFR-002 | 명칭: 게시판
❍ 공지사항 등록 및 조회 기능
산출정보: 단계별 개발 산출물"""
OTHER = """[요구사항 표] 고유번호: SFR-003 | 명칭: 일정
❍ 개인 일정 등록 기능
산출정보: 단계별 개발 산출물"""


def _doc():
    texts = {"SFR-001": LONG, "SFR-002": SHORT, "SFR-003": OTHER}
    blocks, sources = [], []
    for i, (key, text) in enumerate(texts.items(), 1):
        bid = f"B{i:04d}"
        blocks.append(DocumentBlock(document_id="d", document_version="v", source_block_id=bid, block_type="requirement_table",
                                    location=BlockLocation(order=i), text=text, fields={"code": key}))
        sources.append(RequirementSource(source_key=key, code=key, name=key, block_ids=[bid]))
    return blocks, sources


def _req(key, n, title, quote, bid):
    return Requirement(requirement_id=f"{key}-{n:02d}", source_key=key, type="FUNCTIONAL", scope="SYSTEM", category="기능",
                       title=title, description=title, evidence=[Evidence(source_block_id=bid, quote=quote, verified=True)])


def test_list_items_join_wrapped_lines_and_skip_headers_and_boilerplate():
    blocks, sources = _doc()
    boiler = boilerplate(sources, {b.source_block_id: b for b in blocks})
    assert "산출정보단계별개발산출물" in boiler, "여러 단위에 되풀이되는 서식 줄은 틀 줄"
    items = list_items(LONG, boiler)
    assert "❍ 근태확정 관리" not in items and "❍ 근태설정 및 현황관리" not in items, "하위 항목이 이어지는 목록 머리는 뺀다"
    assert "- 출퇴근시간, 근태사용 및 휴게시간 설정 기능인데 줄이 바뀌어 이어지는 문장" in items, "기호 없는 줄은 앞 항목에 붙인다"
    assert not any("산출정보" in i for i in items), "틀 줄은 항목도 아니고 앞 항목에 붙지도 않는다"


def test_list_items_follow_the_document_own_layout():
    """문서마다 다른 목록 모양: 'ㅇ' 글머리, 기호 없이 한 줄에 한 문장, 줄바꿈으로 끊긴 문장."""
    assert list_items("ㅇ 가입신청 심사·승인 워크플로우 제공\nㅇ 관리자 계정 2단계 인증 적용") == \
        ["ㅇ 가입신청 심사·승인 워크플로우 제공", "ㅇ 관리자 계정 2단계 인증 적용"], "'ㅇ ' 글머리도 항목 기호"
    assert list_items("현행 DB를 분석하여 이행방안을 수립하여야 함\n데이터 이행 후 안정화 작업을 지원하여야 함") == \
        ["현행 DB를 분석하여 이행방안을 수립하여야 함", "데이터 이행 후 안정화 작업을 지원하여야 함"], "문장으로 끝난 줄 다음 줄은 새 항목"
    assert list_items("• 플랫폼을 안정적으로 운영하기 위해 필요한 인\n프라 환경을 정의함.\n• 엑셀 다운로드 지원") == \
        ["• 플랫폼을 안정적으로 운영하기 위해 필요한 인 프라 환경을 정의함.", "• 엑셀 다운로드 지원"], "끊긴 줄은 앞 항목에 붙인다"


def test_near_duplicate_sibling_is_not_covered_by_its_neighbour():
    """거의 같은 형제 항목 중 하나만 추출됐으면 나머지는 빠진 것이다 (요구사항 하나가 담는지로 본다)."""
    src_text = "○ 하자관리 기능\n- 공종별 조사계획을 조회, 등록할 수 있는 기능 제공\n- 공종별 조사계획에 대한 검사대상을 조회, 등록할 수 있는 기능 제공"
    block = DocumentBlock(document_id="d", document_version="v", source_block_id="B1", block_type="requirement_table",
                          location=BlockLocation(order=1), text=src_text, fields={})
    src = RequirementSource(source_key="SFT-013", code="SFT-013", name="하자관리", block_ids=["B1"])
    got = _req("SFT-013", 1, "조사계획 조회·등록", "공종별 조사계획을 조회, 등록할 수 있는 기능 제공", "B1")
    assert [i for _, i in uncovered_items(src, {"B1": block}, [got], set(), 0.6)] == \
        ["- 공종별 조사계획에 대한 검사대상을 조회, 등록할 수 있는 기능 제공"]


def test_uncovered_items_are_those_no_requirement_mentions():
    blocks, sources = _doc()
    by_id = {b.source_block_id: b for b in blocks}
    reqs = [_req("SFR-001", 1, "근무연수 기반 연차 발생·사용", "근무연수 기반 연차 발생 및 사용 관리 기능", "B0001"),
            _req("SFR-001", 2, "일별 근태마감", "일별 근태마감 기능", "B0001")]
    gaps = [item for _, item in uncovered_items(sources[0], by_id, reqs, boilerplate(sources, by_id), 0.6)]
    assert gaps == ["- 근태 결과 집계·마감 및 일괄 업로드 기능", "- 마이너스 연차 허용 여부 설정 기능",
                    "- 출퇴근시간, 근태사용 및 휴게시간 설정 기능인데 줄이 바뀌어 이어지는 문장"], \
        "표 제목 줄(단위 번호가 든 줄)·담긴 항목·목록 머리·서식 줄은 빼고 담기지 않은 항목만"


class _ExtractLLM(LLMClient):
    """처음 추출은 단위마다 첫 항목만 요구사항으로 낸다 (뒤쪽 항목을 빠뜨린 상황). 채우기 요청은 짚은 항목을 요구사항으로 낸다."""
    provider = "scripted"

    def __init__(self, fail_fill=False, echo_existing=False):
        super().__init__("scripted")
        self.fill_prompts: list[str] = []
        self.fail_fill, self.echo_existing = fail_fill, echo_existing

    def _call(self, system, user, schema, max_tokens, reduce_effort=False):
        keys = re.findall(r'<source key="([^"]+)"', user)
        reqs = []
        if "빠진 항목 채우기" in user:
            self.fill_prompts.append(user)
            if self.fail_fill:
                raise LLMError("응답 형식 오류")
            for key in keys:
                bid = re.search(rf'<source key="{key}"[^>]*>\n<block id="(B\d+)">', user).group(1)
                listed = re.findall(rf"\(({bid})\) - (.+)", user.split(f"## [{key}] 아직 담기지 않은 원문 항목")[1].split("## [")[0])
                if self.echo_existing:   # 이미 있는 요구사항을 또 낸다 → 버려야 한다
                    listed = [(bid, "근무연수 기반 연차 발생 및 사용 관리 기능")]
                for i, (b, text) in enumerate(listed, 1):
                    quote = text.split("인데")[0] if "인데" in text else text
                    reqs.append(RequirementOut(key=f"F{i}", source_key=key, type="FUNCTIONAL", scope="SYSTEM", category="기능",
                                               title=text[:20], description=text, priority="MUST", acceptance_criteria=["확인"],
                                               status="CONFIRMED", evidence=[EvidenceOut(source_block_id=b, quote=quote)]))
        else:
            for key in keys:
                bid, text = re.search(rf'<source key="{key}"[^>]*>\n<block id="(B\d+)">\n(.*?)\n</block>', user, re.S).groups()
                first = next(line for line in text.splitlines() if line.startswith("❍")).lstrip("❍ ")
                reqs.append(RequirementOut(key="R1", source_key=key, type="FUNCTIONAL", scope="SYSTEM", category="기능",
                                           title=first[:20], description=first, priority="MUST", acceptance_criteria=["확인"],
                                           status="CONFIRMED", evidence=[EvidenceOut(source_block_id=bid, quote=first)]))
        return ExtractionOut(requirements=reqs, questions=[]), 10, 5


def _state():
    blocks, sources = _doc()
    return {"blocks": [b.model_dump() for b in blocks], "sources": [s.model_dump() for s in sources], "next_action": {"mode": "full"}}


def _run(llm):
    llm_module.set_llm(llm)
    try:
        return ra.run(_state())
    finally:
        llm_module.set_llm(None)


def test_missing_items_are_extracted_again_only_for_units_with_gaps():
    override_settings(req_batch_size=3)
    llm = _ExtractLLM()
    out = _run(llm)
    ids = [r["requirement_id"] for r in out["requirements"]]
    assert ids == ["SFR-001-01", "SFR-001-02", "SFR-001-03", "SFR-001-04", "SFR-001-05", "SFR-002-01", "SFR-003-01"], \
        "빠진 항목은 그 단위 다음 번호로 이어 붙인다 (빠진 것이 없는 단위는 그대로)"
    assert len(llm.fill_prompts) == 1 and '<source key="SFR-001"' in llm.fill_prompts[0]
    assert '<source key="SFR-002"' not in llm.fill_prompts[0], "빠진 항목이 없는 단위는 다시 보내지 않는다"
    prompt = llm.fill_prompts[0]
    existing = prompt.split("## [SFR-001] 이미 추출한 요구사항")[1].split("## [SFR-001] 아직 담기지 않은")[0]
    assert "근무연수 기반 연차 발생 및 사용 관리 기능" in existing, "이미 추출한 요구사항을 함께 보여 준다 (다시 쓰지 않게)"
    assert "(B0001) - 일별 근태마감 기능" in prompt and "산출정보" not in prompt.split("아직 담기지 않은")[1]
    descriptions = [r["description"] for r in out["requirements"]]
    assert "일별 근태마감 기능" in descriptions and "마이너스 연차 허용 여부 설정 기능" in descriptions
    assert all(e["verified"] for r in out["requirements"] for e in r["evidence"]), "채운 요구사항도 인용을 원문에서 확인한다"


def test_fill_drops_duplicates_and_keeps_first_extraction_when_it_fails():
    out = _run(_ExtractLLM(echo_existing=True))
    assert [r["requirement_id"] for r in out["requirements"] if r["source_key"] == "SFR-001"] == ["SFR-001-01"], \
        "이미 있는 요구사항과 같은 내용은 버린다"
    out = _run(_ExtractLLM(fail_fill=True))
    assert len(out["requirements"]) == 3 and not out.get("failures"), "채우기가 실패해도 처음 추출은 그대로 (추출 실패로 세지 않음)"


def test_fill_can_be_turned_off():
    override_settings(req_fill_gaps=False)
    llm = _ExtractLLM()
    out = _run(llm)
    assert llm.fill_prompts == [] and len(out["requirements"]) == 3
