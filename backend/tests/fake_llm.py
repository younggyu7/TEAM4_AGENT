"""API 없이 그래프 흐름을 검증하기 위한 가짜 LLM. 프롬프트에서 ID를 읽어 스키마에 맞는 응답을 만든다."""
from __future__ import annotations

import re
from collections import Counter

from app.agents.roles.data_modeler import AttributeOut, AttributesOut, CatalogOut, CoverageOut, EntityAttributesOut, \
                                          EntityPlanOut, MappingOut, RedesignScopeOut
from app.agents.roles.function_designer import (CodeSetOut, CommonDecisionsOut, ConfirmOut, FunctionDesignOut,
                                                FunctionOut, FunctionPatchOut, FunctionScopeOut, InputOut,
                                                PatchedFunctionOut, PolicyOut, ResolutionOut, TermOut)
from app.agents.roles.requirements_analyst import EvidenceOut, ExtractionOut, QuestionOut, RequirementOut
from app.agents.roles.semantic_reviewer import FunctionFinding, FunctionReviewOut, ModelFinding, ModelReviewOut
from app.agents.llm.client import LLMClient


def _attr(name, label, data_type="varchar", length=100, nullable=True, ref=""):
    """실제 모델처럼 기본값 항목은 생략한 컬럼 응답 (기본키·감사 컬럼은 코드가 추가하므로 내지 않는다)."""
    extra = {"length": length} if data_type == "varchar" else {}
    return AttributeOut(name=name, label=label, data_type=data_type, nullable=nullable, references=ref, **extra)


class FakeLLM(LLMClient):
    provider = "fake"

    def __init__(self, broken_fk: bool = True):
        super().__init__("fake-model")
        self.calls: Counter[str] = Counter()
        self.broken_fk = broken_fk

    def _call(self, system, user, schema, max_tokens, shared="", **_):
        if shared:
            user = f"{shared}\n\n{user}"
        self.calls[schema.__name__] += 1
        handler = getattr(self, f"_{schema.__name__}")
        return handler(user), 100, 50

    # 요구사항 추출: 블록마다 기능 1 + 첫 블록에 비기능 1, 차단 질문 1
    def _ExtractionOut(self, user):
        source = re.search(r'<source key="([^"]+)"', user).group(1)
        blocks = re.findall(r'<block id="(B\d+)">\n(.*?)\n</block>', user, re.S)
        reqs = []
        for i, (bid, text) in enumerate(blocks, start=1):
            if "응답" in text:
                reqs.append(RequirementOut(
                    key=f"R{i}", source_key=source, type="NON_FUNCTIONAL", scope="SYSTEM", category="성능",
                    title="응답 시간", description=text, priority="MUST", acceptance_criteria=["3초 이내"],
                    evidence=[EvidenceOut(source_block_id=bid, quote=text[:20])], status="CONFIRMED"))
            elif "요청" in text or "배정" in text:
                reqs.append(RequirementOut(
                    key=f"R{i}", source_key=source, type="FUNCTIONAL", scope="SYSTEM", category="기능",
                    title=text[3:18], description=text, priority="MUST", acceptance_criteria=[],
                    evidence=[EvidenceOut(source_block_id=bid, quote=text[:20])], status="CONFIRMED"))
        func_key = next(r.key for r in reqs if r.type == "FUNCTIONAL")
        question = QuestionOut(kind="AMBIGUITY", question="배정 전에 관리자 승인이 필요한가?",
                               context="승인 절차가 원문에 없음", options=["필요", "불필요"],
                               requirement_keys=[func_key], evidence_block_ids=[blocks[0][0]], blocking=True)
        return ExtractionOut(requirements=reqs, questions=[question])

    def _RequirementFeedbackOut(self, user):
        """요구사항 의견 반영: '제외'면 배정 요구사항을 빼고, '바꿔'면 첫 기능 요구사항에 결정, '추가'면 새 요구사항 1개."""
        from app.agents.roles.requirements_analyst import (ExcludedOut, NewRequirementOut, RequirementChangeOut,
                                                           RequirementFeedbackOut)
        feedback = user.split("## 사용자 의견 (반드시 반영)")[1].split("## 이번 요청")[0]
        lines = re.findall(r"^- \[([^\]]+)\] \((기능|비기능)[^)]*\) (.+)$", user, re.M)
        func = [(rid, text) for rid, kind, text in lines if kind == "기능"]
        out = RequirementFeedbackOut()
        if "제외" in feedback:
            out.excluded = [ExcludedOut(requirement_id=rid, reason="이번 범위 아님") for rid, text in func if "배정" in text]
        if "바꿔" in feedback:
            out.changes = [RequirementChangeOut(requirement_id=func[0][0], decision="요청 제목은 50자 이내로 받는다")]
        if "추가" in feedback:
            out.added = [NewRequirementOut(type="FUNCTIONAL", category="기능", title="요청 취소",
                                           description="사용자는 처리 전 요청을 취소할 수 있다", acceptance_criteria=[])]
        return out

    def _FunctionDesignOut(self, user):
        section = user.split("## 설계 대상 기능 요구사항")[1].split("\n## ")[0]   # 공통 앞부분(공통 결정·비기능) 다음의 그룹 부분
        req_ids = re.findall(r"^- \[([^\]]+)\]", section, re.M)
        decided = "결정:" in section
        functions = [FunctionOut(
            name=f"기능 {rid}" + (" (승인 반영)" if decided else ""), description="설명", requirement_ids=[rid],
            nfr_ids=[], actors=["요청자"], preconditions=[], inputs=[InputOut(name="제목", description="")],
            outputs=[], business_rules=[], normal_flow=["입력", "저장"], exception_flows=[],
            data_objects=["서비스 요청"]) for rid in req_ids]
        return FunctionDesignOut(functions=functions)

    def _CommonDecisionsOut(self, user):
        """공통 결정: 업무 객체 이름 1, 요청 상태 1(가정), 공통 규칙 1(원문)."""
        rid = re.search(r"^- \[([^\]]+)\]", user, re.M).group(1)
        return CommonDecisionsOut(
            terms=[TermOut(name="서비스 요청", synonyms=["IT 서비스 요청"])],
            code_sets=[CodeSetOut(name="요청 상태", values=["접수", "배정", "완료"], basis="가정", requirement_ids=[rid])],
            policies=[PolicyOut(rule="요청은 등록한 사용자와 배정된 담당자만 볼 수 있다", basis="원문", requirement_ids=[rid])])

    def _FunctionScopeOut(self, user):
        """의견 반영 범위: 의견에 '배정'이 있으면 마지막 요구사항(담당자 배정), 아니면 첫 요구사항."""
        ids = re.findall(r"^- \[([^\]]+)\] .+? →", user, re.M)
        feedback = user.split("## 사용자 의견")[1]
        return FunctionScopeOut(requirement_ids=[ids[-1] if "배정" in feedback else ids[0]])

    def _FunctionPatchOut(self, user):
        """의견 반영: 그룹의 첫 기능만 고쳐서 돌려준다 (규칙 하나 추가, 나머지는 그대로라 응답에 넣지 않음)."""
        fid, name = re.search(r"^### \[([^\]]+)\] (.+?): ", user, re.M).groups()
        rid = re.search(r"^요구사항: ([^ |]+)", user, re.M).group(1)
        return FunctionPatchOut(functions=[PatchedFunctionOut(
            replaces=fid, name=name, description="설명", requirement_ids=[rid], nfr_ids=[], actors=["요청자"],
            preconditions=[], inputs=[InputOut(name="제목", description="")], outputs=[],
            business_rules=["(가정) 의견 반영"], normal_flow=["입력", "저장"], exception_flows=[],
            data_objects=["서비스 요청"])])

    def _ConfirmOut(self, user):
        """확인 질문 고르기: 기본은 질문 없이 C 후보만 추천안으로 정한다 (테스트에서 _confirm_questions를 바꿔 씀)."""
        tail = re.search(r"resolutions에 반드시 다음 C 후보 모두: (.+)$", user, re.M)
        c_ids = [x.strip() for x in tail.group(1).split(",")] if tail else []
        return ConfirmOut(questions=self._confirm_questions(user),
                          resolutions=[ResolutionOut(item_id=c, rule=f"추천안 {c}") for c in c_ids])

    def _confirm_questions(self, user):
        return []

    def _CatalogOut(self, user):
        fids = re.findall(r"\[(FN-[^\]]+)\]", user)
        return CatalogOut(entities=[
            EntityPlanOut(name="service_request", label="서비스 요청", description="요청", kind="TRANSACTION",
                          function_ids=fids, parents=["app_user"]),
            EntityPlanOut(name="app_user", label="사용자", description="사용자", kind="MASTER",
                          function_ids=[], parents=[]),
        ], questions=self._design_questions(user))

    def _design_questions(self, user):
        """데이터 설계 확인 질문 (기본은 없음, 테스트에서 바꿔 씀)."""
        return []

    def _CoverageOut(self, user):
        fids = re.findall(r"\[(FN-[^\]]+)\]", user.split("## 엔티티에 연결해야 할 기능")[1])
        return CoverageOut(mappings=[MappingOut(function_id=f, entity_names=["service_request"]) for f in fids],
                           new_entities=[])

    def _RedesignScopeOut(self, user):
        """의견 반영 범위: 의견에 '요청'이 있으면 service_request만 (나머지는 관계없음)."""
        feedback = user.split("## 사용자 의견")[1]
        return RedesignScopeOut(entity_names=["service_request"] if "요청" in feedback else [])

    def _AttributesOut(self, user):
        names = re.findall(r"^### (\w+) ", user, re.M)
        fixing = "반드시 해결할 검증 오류" in user
        out = []
        for name in names:
            if name == "service_request":
                ref = "ghost" if self.broken_fk and not fixing else "app_user"
                attrs = [_attr("title", "제목", nullable=False), _attr("requester_id", "요청자", "bigint", ref=ref)]
            else:
                attrs = [_attr("user_name", "이름")]
            out.append(EntityAttributesOut(name=name, attributes=attrs))
        return AttributesOut(entities=out)

    # 요구사항 원문 대조: 기본은 모두 지금 분류 유지, 빠진 것 없음 (테스트에서 바꿔 씀)
    def _SourceReviewOut(self, user):
        from app.agents.roles.requirement_reviewer import RequirementVerdict, SourceReviewOut
        heads = re.findall(r"^- \[([^\]]+)\] 검토 대상 \(지금: (\w+) / (\w+) / (\S+)\)", user, re.M)
        return SourceReviewOut(verdicts=[RequirementVerdict(
            requirement_id=rid, type=t, scope=s, category=c, confidence="high",
            reason="지금 분류가 맞음", evidence="-") for rid, t, s, c in heads])

    # 요구사항 충돌 검토: 기본은 충돌 없음 (테스트에서 바꿔 씀)
    def _ConflictReviewOut(self, user):
        from app.agents.roles.requirement_reviewer import ConflictReviewOut
        return ConflictReviewOut(conflicts=[])

    # 의미 검토: 기능 표시 없는 가정 1, 데이터 모델 제약 누락 1 + 없는 엔티티를 가리키는 지적 1(버려져야 함)

    def _FunctionReviewOut(self, user):
        fids = re.findall(r"^### \[(FN-[^\]]+)\]", user, re.M)
        names = re.findall(r"^### \[FN-[^\]]+\] (.+) \(근거", user, re.M)
        return FunctionReviewOut(findings=[FunctionFinding(
            category="UNMARKED_ASSUMPTION", target_ids=fids[:1], message="원문에 없는 처리 기한을 단정함",
            evidence="업무 규칙: 24시간 이내 처리", suggestion="'(가정)' 표시 후 고객 확인",
            quote=f"[{fids[0]}] {names[0]}")] if fids else [])

    def _ModelReviewOut(self, user):
        return ModelReviewOut(findings=[
            ModelFinding(category="INTEGRITY", target_ids=["service_request"], message="요청자 참조가 NULL 허용",
                         evidence="requester_id:bigint→app_user", suggestion="requester_id를 NOT NULL로",
                         quote="requester_id:bigint→app_user"),
            ModelFinding(category="GAP", target_ids=["ghost_table"], message="없는 엔티티", evidence="-",
                         suggestion="-", quote="-"),
        ])
