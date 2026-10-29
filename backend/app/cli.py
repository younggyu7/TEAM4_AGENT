"""명령행 실행기.

  python -m app run   <문서> [--project 이름] [--defer] [--validate-db] [--no-semantic-review] [--only SFR,PER-002]
                            [--until requirements_analyst|function_designer] [--by-stage]
  python -m app resume <run_id> [--defer] [--retry-failed]
  python -m app rerun data_modeler <프로젝트> --from v2 [--feedback "의견"]   # 데이터 설계만 다시 → v2.1, v2.2 …
  python -m app confirm <프로젝트> v2 [--note 메모]   # 단계 버전 확정 (다음 단계는 확정한 버전에서)
  python -m app diff <프로젝트> v1.1 v1.2             # 두 버전의 요구사항·기능·엔티티 변경과 영향
  python -m app versions [프로젝트]   # 프로젝트의 설계 버전 목록. 생략하면 프로젝트 목록

단계별 진행 (팀 규칙 3): 요구사항 v1 … v6 → 확정한 v6에서 기능 명세 v6.1 … → 확정한 v6.3에서 데이터 설계 v6.3.1 …
  run --by-stage는 단계마다 멈춰 [확정하고 다음 단계 / 의견 넣고 다시 만들기 / 멈춤]을 묻는다 (터미널 필요).
  python -m app parse [문서|폴더]     # LLM 없이 문서분석만. 생략하면 data/ 폴더 전체
"""
from __future__ import annotations

import argparse
import json
import os
import logging
import sys
from datetime import datetime
from pathlib import Path

from langgraph.types import Command

from app.agents.roles.coordinator import STAGE_LABELS, stuck_stages
from app.core.config import get_settings, override_settings
from app.agents.flow.graph import build_graph, open_checkpointer
from app.agents.llm.client import LLMFatalError, LLMPending, pending_requests, resolve_provider
from app.agents.tools.doc_quality import build_sources, check_document
from app.agents.tools.document_parser import (UnsupportedFormatError, check_supported, document_codes, file_fingerprint,
                                              parse_document, textless_pages)
from app.agents.flow.rerun import (STAGES, RerunError, ancestor, base_for, changed_requirements, has_functions, load_answers,
                                   prepare_data_model, prepare_functions, prepare_requirements, seed_state)
from app.schemas.design import QUESTION_STAGES
from app.agents.tools.convention import ConventionError, list_profiles, load_profile
from app.agents.tools.exporter import changes_md
from app.agents.tools.trace_diff import compare_versions, load_previous
from app.agents.tools.versions import INDEX, find_version, load_versions, project_dir, project_name, update_version, version_label

log = logging.getLogger("req_agent")


def main(argv: list[str] | None = None) -> int:
    """명령행 진입점: run / resume / rerun / versions / parse 하위 명령을 해석해 실행한다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="python -m app", description="요구사항 문서 → 요구사항·명세·ERD·DDL 생성 Agent")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="문서를 분석해 산출물 생성")
    p_run.add_argument("document")
    p_run.add_argument("--project", default="",
                       help="프로젝트(고객) 이름. 같은 프로젝트는 실행이 달라도 설계 버전이 v1, v2 …로 이어진다. "
                            "생략하면 문서 파일 이름")
    p_run.add_argument("--defer", action="store_true", help="확인 질문을 묻지 않고 가정·보류로 기록")
    p_run.add_argument("--validate-db", action="store_true", help="산출 후 별도 DB에 DDL 적용·검증")
    p_run.add_argument("--only", default="", help="추출 단위 필터 (예: SFR,PER-002)")
    p_run.add_argument("--max-sources", type=int, default=0, help="추출 단위 최대 개수 (빠른 시험용)")
    p_run.add_argument("--include-narrative", action="store_true",
                       help="요구사항 표가 있어도 표 밖 본문의 의무 문장 절(예: 일반 요건)을 추출 대상에 넣음")
    p_run.add_argument("--title", default="",
                       help="사업 이름 (예: 'KISA 데이터 금고 지원 포털 구축'). 표지 없이 요구사항 부분만 정제한 문서에서 "
                            "LLM에 사업 맥락을 준다. 생략하면 문서 첫 줄")
    p_run.add_argument("--no-semantic-review", action="store_true",
                       help="의미 검토 Agent(요구사항 분류·모순, 기능 명세, 데이터 모델 내용 검토)를 끔")
    p_run.add_argument("--until", choices=["requirements_analyst", "function_designer"],
                       help="이 단계까지만 만들고 멈춤 (확정한 뒤 rerun <다음 단계> --from 그 버전으로 이어 간다)")
    p_run.add_argument("--by-stage", action="store_true",
                       help="단계마다 멈춰 확정 / 의견 넣고 다시 만들기 / 멈춤을 묻는다 (요구사항 → 기능 명세 → 데이터 설계)")
    p_run.add_argument("--standard", default="",
                       help="회사별 설계 표준 프로필 (standards/ 폴더 이름, 예: public). ERD·테이블 정의서·DDL이 표준 이름을 쓴다")
    p_run.add_argument("--provider", choices=["openai", "anthropic", "manual"])
    p_run.add_argument("--model", help="LLM 모델 ID (provider 기본값 대체)")
    p_run.add_argument("--output-dir")

    p_resume = sub.add_parser("resume", help="질문 대기·중단된 실행 재개")
    p_resume.add_argument("run_id")
    p_resume.add_argument("--defer", action="store_true", help="대기 중 질문을 보류로 처리하고 계속")
    p_resume.add_argument("--provider", choices=["openai", "anthropic", "manual"], help="기본: 처음 실행한 공급자")
    p_resume.add_argument("--retry-failed", action="store_true",
                          help="반복 실패로 멈춘 단계(예: 데이터 모델)부터 다시 진행. 앞 단계 결과는 그대로 쓰고, "
                               "끝난 실행이면 다음 설계 버전(v2…)으로 만든다")
    p_resume.add_argument("--output-dir")

    p_rerun = sub.add_parser("rerun", help="기준 버전의 앞 단계 결과는 두고 한 단계만 다시 만들어 아래 번호 버전(v2.1 …)을 만든다")
    p_rerun.add_argument("stage", choices=sorted(STAGES), help="다시 만들 단계 (Agent 이름)")
    p_rerun.add_argument("project", help="프로젝트 이름 (versions로 확인)")
    p_rerun.add_argument("--from", dest="from_version", required=True,
                         help="기준 버전 (예: v2). 같은 단계를 다시 만든 버전(v2.1)을 주면 다음 형제 번호(v2.2)")
    p_rerun.add_argument("--feedback", default="",
                         help="이번 설계에 반영할 의견 (예: '차량에 소유 회원을 연결'). --entities가 없으면 의견과 관련된 엔티티만 다시 설계")
    p_rerun.add_argument("--requirements", default="",
                         help="(function_designer) 기능을 다시 설계할 요구사항 ID (쉼표로 구분). 그 요구사항의 기능 그룹만 다시 설계")
    p_rerun.add_argument("--entities", default="",
                         help="(data_modeler) 컬럼을 다시 설계할 엔티티 (쉼표로 구분, 예: vehicle,drive_record). "
                              "사용자가 고친 엔티티도 지정하면 다시 설계")
    p_rerun.add_argument("--data-model", dest="data_model",
                         help="(data_modeler) 사용자가 고친 data_model.json (생략하면 기준 버전 폴더의 data_model.json). "
                              "고치거나 새로 만든 엔티티는 고정되어 LLM이 다시 설계하지 않음")
    p_rerun.add_argument("--answers",
                         help="(function_designer) 확인 질문 답 JSON {\"질문 ID\": \"답\"}. 요구사항·분류·충돌·기능 명세 질문의 답을 "
                              "한 번에 반영하고, 추천과 다른 답이 바꾼 요구사항·기능만 다시 설계 (화면 1단계 제출)")
    p_rerun.add_argument("--full", action="store_true",
                         help="그 단계 전체를 다시 만듦 (데이터 설계는 사용자가 고친 엔티티 유지)")
    p_rerun.add_argument("--validate-db", action=argparse.BooleanOptionalAction, default=None,
                         help="DB 검증 여부 (생략하면 기준 버전 실행 설정을 따름)")
    p_rerun.add_argument("--model", help="LLM 모델 ID (생략하면 기준 버전을 만든 실행의 모델)")
    p_rerun.add_argument("--standard", default="",
                         help="설계 표준 프로필을 바꿈 (생략하면 기준 버전 실행의 프로필, default면 지금 규칙)")
    p_rerun.add_argument("--output-dir")

    p_versions = sub.add_parser("versions", help="프로젝트의 설계 버전 목록 (생략하면 프로젝트 목록)")
    p_versions.add_argument("project", nargs="?")
    p_versions.add_argument("--output-dir")

    p_confirm = sub.add_parser("confirm", help="단계 버전을 확정한다 (팀 규칙: 다음 단계는 확정한 버전에서 시작)")
    p_confirm.add_argument("project")
    p_confirm.add_argument("version", help="확정할 버전 (예: v2.1)")
    p_confirm.add_argument("--note", default="", help="확정 메모 (예: 고객 회의 10-28 합의)")
    p_confirm.add_argument("--output-dir")

    p_diff = sub.add_parser("diff", help="두 버전의 요구사항·기능·엔티티 변경과 영향을 비교한다")
    p_diff.add_argument("project")
    p_diff.add_argument("old", help="비교 기준 버전 (예: v1.1)")
    p_diff.add_argument("new", help="비교할 버전 (예: v1.2)")
    p_diff.add_argument("--out", help="비교 결과 파일 (생략하면 output/<프로젝트>/diff-v<기준>-v<비교>.md)")
    p_diff.add_argument("--output-dir")

    p_standards = sub.add_parser("standards", help="설계 표준 프로필 목록과 형식 검사 (standards/ 폴더)")
    p_standards.add_argument("--output-dir")

    p_parse = sub.add_parser("parse", help="LLM 없이 문서분석만 실행 (문서 하나 또는 폴더 전체)")
    p_parse.add_argument("document", nargs="?", help="문서 파일 또는 폴더 (생략하면 DATA_DIR, 기본 data/)")
    p_parse.add_argument("--output-dir")
    p_parse.add_argument("--include-narrative", action="store_true", help="run --include-narrative의 추출 단위 미리보기")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "openai", "anthropic", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.output_dir:
        override_settings(output_dir=Path(args.output_dir))
        if not os.getenv("LLM_EXCHANGE_DIR"):
            override_settings(llm_exchange_dir=Path(args.output_dir) / "llm_exchange")

    if args.command == "parse":
        return _parse(_resolve_input(args.document) if args.document else get_settings().data_dir,
                      args.include_narrative)
    if args.command == "versions":
        return _versions(args.project)
    if args.command == "confirm":
        return _confirm(args.project, args.version, args.note)
    if args.command == "diff":
        return _diff(args.project, args.old, args.new, args.out)
    if args.command == "standards":
        return _standards()
    if args.command == "rerun":
        return _rerun(args)
    if args.command == "run":
        if args.provider:
            override_settings(llm_provider=args.provider)
        if args.model:
            key = "anthropic_model" if resolve_provider() == "anthropic" else "openai_model"
            override_settings(**{key: args.model})
        return _run(args)
    return _resume(args)


def _run(args) -> int:
    """새 실행(run_id)을 만들고 그래프를 처음부터 실행한다."""
    path = _resolve_input(args.document)
    if not _readable(path):
        return 2
    _, version = file_fingerprint(path)
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{version[:6]}"
    if resolve_provider() == "manual" and not os.getenv("REQ_BATCH_SIZE"):
        override_settings(req_batch_size=1)   # 사람이 답하기 쉽게 원문 단위 1개당 요청 1건
    options = {
        "ask_mode": "defer" if args.defer or not sys.stdin.isatty() else "ask",
        "validate_db": args.validate_db,
        "only": [o for o in args.only.split(",") if o.strip()],
        "max_sources": args.max_sources or 0,
        "include_narrative": args.include_narrative,
        "semantic_review": get_settings().semantic_review and not args.no_semantic_review,
        "title": args.title.strip(),
        "project": project_name(args.project or path.stem),
        # 프롬프트 구성에 영향을 주는 설정 → resume 때 그대로 재적용 (manual 응답 재생 키도 유지됨)
        "settings": {"llm_provider": resolve_provider(), **_prompt_settings(), **_model_settings()},
    }
    if options["ask_mode"] == "defer" and not args.defer:
        log.info("입력 터미널이 아니므로 확인 질문은 보류(--defer)로 처리합니다.")
    if args.by_stage and not sys.stdin.isatty():
        print("--by-stage는 단계마다 답을 받아야 하므로 터미널에서 실행하세요 "
              "(터미널 없이 단계별로 하려면 --until과 confirm·rerun 명령).")
        return 2
    until = "requirements_analyst" if args.by_stage else args.until
    if until:
        options["stop_after"] = until
    if args.standard:
        try:
            options["standard"] = load_profile(args.standard).key   # 형식 오류는 실행 전에 알린다
        except ConventionError as e:
            print(e)
            return 2
    initial = {"run_id": run_id, "source_path": str(path.resolve()), "options": options}
    code = _drive(run_id, initial)
    if code or not args.by_stage:
        return code
    version = _version_of(options["project"], run_id)
    return _by_stage(options["project"], version) if version else code


STAGE_FLOW = ("requirements_analyst", "function_designer", "data_modeler")


def _version_of(project: str, run_id: str) -> str | None:
    """실행이 만든 마지막 버전 번호."""
    made = [v["design_version"] for v in load_versions(get_settings().output_dir, project) if v.get("run_id") == run_id]
    return made[-1] if made else None


def _stage_of(record: dict) -> str:
    """버전이 어느 단계까지의 결과인지 (요구사항 / 기능 명세 / 데이터 설계)."""
    if record.get("until") in STAGE_FLOW:
        return record["until"]
    return "data_modeler" if record.get("stage") in ("full", "data_modeler", None) else record["stage"]


def _by_stage(project: str, version: str) -> int:
    """단계별 진행: 버전마다 [확정하고 다음 단계 / 의견 넣고 다시 만들기 / 멈춤]을 묻고 rerun으로 이어 간다."""
    while True:
        record = find_version(get_settings().output_dir, project, version)
        stage = _stage_of(record)
        label = STAGES[stage]["label"]
        last = stage == STAGE_FLOW[-1]
        print(f"\n[{label} 단계] v{version} — {record['output_dir']}")
        print(f"  1) 확정{'' if last else '하고 다음 단계(' + STAGES[STAGE_FLOW[STAGE_FLOW.index(stage) + 1]]['label'] + ')'}"
              f"   2) 의견 넣고 {label} 다시 만들기   3) 여기서 멈춤")
        try:
            choice = input("  선택> ").strip()
        except EOFError:
            choice = "3"
        if choice == "1":
            _confirm(project, version, "")
            if last:
                print("모든 단계를 확정했습니다.")
                return 0
            stage, feedback = STAGE_FLOW[STAGE_FLOW.index(stage) + 1], ""
        elif choice == "2":
            try:
                feedback = input("  의견> ").strip()
            except EOFError:
                feedback = ""
            if not feedback:
                print("  의견이 비어 있어 다시 묻습니다.")
                continue
        else:
            nxt = STAGE_FLOW[min(STAGE_FLOW.index(stage) + 1, len(STAGE_FLOW) - 1)]
            print(f"멈췄습니다. 이어 가려면: python -m app confirm {project} v{version} 후 "
                  f"python -m app rerun {nxt} {project} --from v{version}")
            return 0
        out: dict = {}
        code = _rerun(argparse.Namespace(stage=stage, project=project, from_version=version, feedback=feedback,
                                         requirements="", entities="", data_model=None, full=False, validate_db=None,
                                         model=None), out)
        if code:
            return code
        version = _version_of(project, out["run_id"]) or version


def _confirm(project: str, version: str, note: str) -> int:
    """버전을 확정한다 (versions.json에 확정 시각·메모). 확정한 버전도 고치면 새 버전이 생긴다 (덮어쓰지 않음)."""
    root = get_settings().output_dir
    try:
        record = find_version(root, project, version)
    except ValueError as e:
        print(e)
        return 2
    if not record:
        print(f"버전을 찾을 수 없습니다: {project_name(project)} v{version.lstrip('vV')}")
        return 2
    update_version(root, project, record["design_version"], confirmed=True,
                   confirmed_at=datetime.now().isoformat(timespec="seconds"), confirm_note=note.strip())
    stage = _stage_of(record)
    nxt = STAGE_FLOW.index(stage) + 1
    print(f"v{record['design_version']} 확정 ({STAGES[stage]['label']} 단계)"
          + (f" — 다음 단계: python -m app rerun {STAGE_FLOW[nxt]} {project_name(project)} "
             f"--from v{record['design_version']}" if nxt < len(STAGE_FLOW) else ""))
    return 0


def _diff(project: str, old: str, new: str, out: str | None) -> int:
    """두 버전의 변경과 영향을 비교해 파일로 쓰고 요약을 보여 준다 (LLM 없음)."""
    root = get_settings().output_dir
    try:
        a, b = find_version(root, project, old), find_version(root, project, new)
    except ValueError as e:
        print(e)
        return 2
    if not a or not b:
        print(f"버전을 찾을 수 없습니다: {old if not a else new} (python -m app versions {project_name(project)})")
        return 2
    folder = project_dir(root, project)
    previous, current = load_previous(folder, a["design_version"]), load_previous(folder, b["design_version"])
    if previous is None or current is None:
        print("비교할 버전 폴더의 design.json이 없습니다.")
        return 2
    changes = compare_versions(current, previous, (folder / f"v{b['design_version']}" / "data_model.json").exists())
    path = Path(out) if out else folder / f"diff-v{a['design_version']}-v{b['design_version']}.md"
    title = f"# 버전 비교: v{a['design_version']} → v{b['design_version']}"
    path.write_text("\n".join([title, "", *changes_md(changes)]) + "\n", encoding="utf-8")
    c = changes["counts"]
    print(f"v{a['design_version']} → v{b['design_version']}: 요구사항 {_counts(c['requirements'])} · "
          f"기능 {_counts(c['functions'])}" + (f" · 엔티티 {_counts(c['entities'])}" if c["entities"] is not None else ""))
    print(f"→ {path}")
    return 0


def _standards() -> int:
    """설계 표준 프로필 목록과 형식 검사 결과."""
    profiles = list_profiles()
    if not profiles:
        print("설계 표준 프로필이 없습니다 (standards/<이름>/rules.yaml)")
        return 2
    for key, profile, error in profiles:
        if profile is None:
            print(f"✘ {key}: {error}")
            continue
        print(f"✔ {key} ({profile.name}) — {profile.description}")
        print(f"    이름 {profile.case} · 접두어 '{profile.table_prefix}' · 최대 {profile.max_length}자 · "
              f"표준 단어 {len(profile.words)}개 · 지침 {'있음' if profile.guide else '없음'}"
              + ("" if profile.renames else " · 이름을 바꾸지 않음(지금 규칙)"))
    return 0 if all(p for _, p, _ in profiles) else 2


def _counts(values: list[int] | None) -> str:
    return " · ".join(f"{label} {n}" for label, n in zip(("새", "바뀜", "지움"), values or [0, 0, 0]))


def _model_settings() -> dict:
    """사용한 모델과 생각 수준 (resume·rerun이 같은 모델로 이어 가도록 저장. 저장이 없던 예전 실행은 .env 값을 쓴다)."""
    s = get_settings()
    return {"anthropic_model": s.anthropic_model, "anthropic_effort": s.anthropic_effort,
            "openai_model": s.openai_model, "openai_reasoning_effort": s.openai_reasoning_effort}


def _prompt_settings() -> dict:
    """프롬프트 구성에 영향을 주는 배치 설정 (resume 때 그대로 재적용)."""
    s = get_settings()
    return {"req_batch_size": s.req_batch_size, "func_group_size": s.func_group_size,
            "attr_batch_size": s.attr_batch_size}


def _resume(args) -> int:
    """체크포인트에서 실행 상태를 읽어 이어서 실행한다. 질문 대기 중이면 답변을 받는다."""
    with open_checkpointer(get_settings().output_dir) as saver:
        graph = build_graph(saver)
        config = _config(args.run_id)
        snapshot = graph.get_state(config)
        if not snapshot.values:
            print(f"실행을 찾을 수 없습니다: {args.run_id}")
            return 2
        saved = dict((snapshot.values.get("options") or {}).get("settings") or {})
        if args.provider:
            saved["llm_provider"] = args.provider
        override_settings(**saved)
        if args.retry_failed:
            stop = _retry_failed(graph, config, snapshot)
            if stop is not None:
                return stop
        elif not snapshot.next:
            print("이미 완료된 실행입니다.")
            _print_summary(snapshot.values)
            return 0
    if snapshot.interrupts:
        if not args.defer and not sys.stdin.isatty():
            print("확인 질문에 답하려면 터미널에서 실행하거나, --defer로 보류 처리하세요.")
            return 3
        payload = Command(resume={} if args.defer else _ask(snapshot.interrupts[0].value))
    else:
        payload = None
    return _drive(args.run_id, payload)


def _retry_failed(graph, config: dict, snapshot) -> int | None:
    """반복 실패로 멈춘 단계의 실패 횟수를 지우고 조정 Agent부터 다시 진행하게 한다.

    이어서 실행하면 None, 실행하지 않고 끝내야 하면 종료 코드를 돌려준다.

    앞 단계 산출물(요구사항·기능 명세 등)은 그대로 쓰고, 이미 끝난 실행이면 결과는 다음 설계 버전(v2…)으로 나온다.
    """
    values = snapshot.values
    if snapshot.interrupts:
        print("확인 질문 대기 중인 실행입니다. --retry-failed 없이 resume으로 먼저 답하세요.")
        return 3
    stuck = stuck_stages(values)
    if not stuck:
        if snapshot.next:
            print("반복 실패로 멈춘 단계가 없어 그대로 이어서 실행합니다.")
            return None
        print("반복 실패로 멈춘 단계가 없는 완료된 실행입니다.")
        _print_summary(values)
        return 0
    failures = {k: v for k, v in (values.get("failures") or {}).items() if k not in stuck}
    graph.update_state(config, {"failures": failures}, as_node="record_decision")   # 다음 노드: 조정 Agent
    step = values.get("step") or 0
    override_settings(max_steps=step + get_settings().max_steps)   # 이미 쓴 단계 수와 별개로 한도만큼 더 진행
    print(f"다시 시도: {', '.join(STAGE_LABELS[a] for a in stuck)} (단계 {step}부터, "
          f"{'다음 설계 버전으로 제공' if not snapshot.next else '이어서'})")
    return None


def _project(values: dict) -> str:
    """실행 상태의 프로젝트 이름 (요약 출력용)."""
    from app.agents.tools.versions import project_of
    return project_of(values)


def _versions(project: str | None) -> int:
    """프로젝트의 설계 버전 목록을 보여준다. 프로젝트를 주지 않으면 버전 기록이 있는 프로젝트 목록."""
    root = get_settings().output_dir
    if not project:
        projects = sorted(p.parent.name for p in root.glob(f"*/{INDEX}"))
        print("프로젝트: " + (", ".join(projects) if projects else "없음"))
        return 0
    versions = load_versions(root, project)
    if not versions:
        print(f"버전 기록이 없습니다: {project_name(project)}")
        return 2
    print(f"# {project_name(project)} 설계 버전 {len(versions)}개")
    for v in versions:
        parent = f"v{v['parent']}" if v.get("parent") else "-"
        what = STAGES.get(v.get("stage", ""), {}).get("label", "전체")
        if v.get("until") in STAGES:   # 한 단계만 다시 만든 버전은 '기능 명세 단계', 단계에서 멈춘 전체 실행은 '전체→요구사항까지'
            what = f"{what} 단계" if v["until"] == v.get("stage") else f"{what}→{STAGES[v['until']]['label']}까지"
        print(f"  v{v['design_version']:<7} {v['created_at']}  {what:<12} 이전 {parent:<7} "
              f"DB {v.get('db_validation', '-'):<6} {'확정 ✔' if v.get('confirmed') else '      '} 실행 {v.get('run_id', '')}")
        for extra in (v.get("feedback") and f"의견: {v['feedback']}", v.get("note"),
                      v.get("confirmed") and f"확정 {v.get('confirmed_at', '')} {v.get('confirm_note', '')}".rstrip()):
            if extra:
                print(f"           {extra}")
        print(f"           {v['output_dir']}")
    return 0


def _rerun(args, out: dict | None = None) -> int:
    """기준 버전에서 한 단계만 다시 만든다 (팀 규칙: v2가 마음에 들면 다음 단계는 v2.1부터).

    out을 주면 새 실행 ID를 넣는다 (단계별 진행이 새 버전 번호를 찾는 데 쓴다).
    """
    root = get_settings().output_dir
    try:
        record = find_version(root, args.project, args.from_version)
    except ValueError as e:
        print(e)
        return 2
    if not record:
        print(f"버전을 찾을 수 없습니다: {project_name(args.project)} v{args.from_version.lstrip('vV')} "
              f"(python -m app versions {project_name(args.project)})")
        return 2
    design_path = Path(record["output_dir"]) / "design.json"
    if not design_path.exists():
        print(f"기준 버전의 design.json이 없습니다: {design_path}")
        return 2
    design = json.loads(design_path.read_text(encoding="utf-8"))
    with open_checkpointer(root) as saver:
        graph = build_graph(saver)
        values = graph.get_state(_config(record["run_id"])).values
        if not values:
            print(f"기준 버전을 만든 실행의 상태(체크포인트)를 찾을 수 없습니다: {record['run_id']}")
            return 2
        try:
            answers = _rerun_answers(args, design)
            model, targets, rework, plan = _rerun_scope(args, root, record, values, design, bool(answers))
        except RerunError as e:
            print(e)
            return 2
        override_settings(**dict((values.get("options") or {}).get("settings") or {}))
        if args.model:
            override_settings(**{"anthropic_model" if resolve_provider() == "anthropic" else "openai_model": args.model})
        base = base_for(record, args.stage)
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{(values.get('document') or {}).get('document_version', '')[:6]}"
        seeded = seed_state(values, design, args.stage, run_id, base, project_name(args.project),
                            args.feedback, args.validate_db, model, targets, rework)
        # 이번 실행이 실제로 쓰는 모델을 저장해 resume도 같은 모델로 이어 가게 한다
        options = seeded["options"]
        if getattr(args, "standard", ""):
            try:
                options["standard"] = load_profile(args.standard).key
            except ConventionError as e:
                print(e)
                return 2
        options["settings"] = {**(options.get("settings") or {}), "llm_provider": resolve_provider(), **_model_settings()}
        if answers:
            plan.insert(0, _apply_answers(seeded, answers))
        graph.update_state(_config(run_id), seeded, as_node="record_decision")   # 다음 노드: 조정 Agent
    if out is not None:
        out["run_id"] = run_id
    target = "다음 최상위 번호" if args.stage == "requirements_analyst" else f"v{version_label(base)}.N"
    print(f"{STAGES[args.stage]['label']} 다시 만들기: 기준 v{record['design_version']} → {target} "
          f"(실행 {run_id}){' · 의견: ' + args.feedback.strip() if args.feedback.strip() else ''}")
    if _stage_of(record) != args.stage and not record.get("confirmed"):
        print(f"  참고: v{record['design_version']}는 아직 확정되지 않았습니다 (팀 규칙: 다음 단계는 확정한 버전에서. "
              f"python -m app confirm {project_name(args.project)} v{record['design_version']})")
    for line in plan:
        print(f"  {line}")
    return _drive(run_id, None)


def _rerun_answers(args, design: dict) -> dict[str, str]:
    """--answers 파일을 읽는다 (기능 명세 단계만)."""
    if not getattr(args, "answers", None):
        return {}
    if args.stage != "function_designer":
        raise RerunError("--answers는 function_designer 단계 옵션입니다 (기능 명세 초안과 함께 받은 확인 질문의 답).")
    return load_answers(Path(args.answers), design.get("questions") or [])


def _apply_answers(seeded: dict, answers: dict[str, str]) -> str:
    """확인 질문 답을 시작 상태에 반영한다 (사용자 답변 기록 노드와 같은 처리). 안내 줄을 돌려준다.

    답이 바꾼 요구사항은 dirty_requirements·rev로, 기능 명세 질문의 다른 답은 function_targets로 이어져
    조정 Agent가 요구사항 검토(충돌 재검토) → 기능 명세 부분 재설계를 고른다."""
    from app.agents.flow.nodes import record_decision
    explicit = seeded.get("function_targets")
    applied = record_decision({**seeded, "next_action": {"action": "ask_user", "scope": list(answers)},
                               "pending_answers": answers})
    applied.pop("decisions", None)
    answered = applied.get("function_targets")
    if explicit is not None and answered is not None:   # 지정한 범위와 기능 명세 질문 답의 범위를 합친다
        applied["function_targets"] = ["*"] if "*" in explicit + answered else sorted(set(explicit) | set(answered))
    elif explicit is not None:
        applied.pop("function_targets", None)
    seeded.update(applied)
    dirty = applied.get("dirty_requirements") or []
    return (f"확인 질문 답 {len(answers)}건 반영 → 다시 설계할 요구사항 {len(dirty)}건"
            + ("" if dirty or applied.get("function_targets") else " (모두 추천안과 같아 확정 표시만)"))


def _rerun_scope(args, root: Path, record: dict, values: dict, design: dict, answers: bool = False):
    """rerun 단계별 범위를 정한다: (시작 모델, 다시 설계할 대상, 데이터 설계 요약, 안내 줄). 진행할 수 없으면 RerunError.

    answers: 확인 질문 답이 있으면 범위 옵션이 없어도 된다 (답이 바꾼 것만 다시 설계)."""
    feedback = args.feedback.strip()
    if args.stage == "requirements_analyst":
        if args.requirements or args.entities or args.data_model or args.full:
            raise RerunError("요구사항 다시 만들기는 --feedback만 받습니다 (의견이 바꾸는·빼는·새 요구사항만 반영).")
        return None, prepare_requirements(feedback), None, [
            "다시 만들기: 의견이 바꾸는·빼는·새로 만드는 요구사항만 (다시 추출하지 않음, 요청 1회)",
            "기능 명세는 하지 않음 — 이 버전을 확정한 뒤 rerun function_designer --from <새 버전>"]
    if args.stage == "function_designer":
        if args.entities or args.data_model:
            raise RerunError("--entities·--data-model은 data_modeler 단계 옵션입니다.")
        notes = []
        if not design.get("functions"):
            # 요구사항 단계 버전: 가장 가까운 기능 명세 버전에서 바뀐 요구사항의 그룹만 다시 설계 (없으면 처음부터 전체)
            source = ancestor(root, args.project, str(record["design_version"]), has_functions)
            if source is None:
                targets = prepare_functions(design, args.requirements, False, feedback) if args.requirements else ["*"]
                notes.append("기준 버전에 기능 명세가 없어 처음부터 설계")
            else:
                old = json.loads((Path(source["output_dir"]) / "design.json").read_text(encoding="utf-8"))
                design["functions"] = old.get("functions") or []
                changed = changed_requirements(old.get("requirements") or [], design.get("requirements") or [])
                explicit = args.requirements or args.full or feedback
                targets = prepare_functions(design, args.requirements, args.full, feedback) if explicit else changed
                if not explicit and not changed and answers:
                    targets = None   # 답이 바꾼 것만
                elif not explicit and not changed:
                    raise RerunError(f"v{source['design_version']}의 기능 명세와 비교해 바뀐 요구사항이 없습니다. "
                                     "전체를 다시 만들려면 --full을 주세요.")
                notes.append(f"v{source['design_version']}의 기능 명세를 이어받음 (바뀐 요구사항 {len(changed)}건)")
        elif answers and not (args.requirements or args.full or feedback):
            targets = None   # 답이 바꾼 요구사항·기능만
        else:
            targets = prepare_functions(design, args.requirements, args.full, feedback)
        scope = ("확인 질문 답이 바꾼 요구사항·기능만" if targets is None else "전체" if "*" in targets else f"요구사항 {len(targets)}건의 기능 그룹 ({', '.join(targets)})" if targets
                 else "의견과 관련된 기능 그룹만 (바꿀 기능만 받음)")
        return None, targets, None, [*notes, f"다시 설계: {scope}",
                                     "데이터 설계는 하지 않음 — 이 버전을 확정한 뒤 rerun data_modeler --from <새 버전>"]
    if args.requirements:
        raise RerunError("--requirements는 function_designer 단계 옵션입니다.")
    if not design.get("functions"):
        raise RerunError("기준 버전에 기능 명세가 없어 데이터 설계를 다시 만들 수 없습니다 "
                         "(요구사항 단계 버전이면 먼저 rerun function_designer).")
    inherited = None
    if record.get("stage") == "function_designer" and record.get("base"):
        # 기능 명세 단계 버전: 기준 버전 쪽으로 올라가며 데이터 모델이 있는 가장 가까운 버전의 모델을 이어받는다
        base_rec = ancestor(root, args.project, str(record["base"]), lambda p: (p / "data_model.json").exists())
        if base_rec and (Path(base_rec["output_dir"]) / "design.json").exists():
            base_design = json.loads((Path(base_rec["output_dir"]) / "design.json").read_text(encoding="utf-8"))
            inherited = (Path(base_rec["output_dir"]), base_design.get("functions") or [], str(base_rec["design_version"]))
    model, targets, rework = prepare_data_model(
        values, Path(record["output_dir"]), Path(args.data_model) if args.data_model else None,
        str(values.get("design_version")) == str(record["design_version"]),
        args.entities, args.full, feedback, inherited, design.get("functions") or [])
    if model is None:
        scope = "전체 (이어받을 데이터 모델이 없음)"
    elif targets is None:
        scope = "전체 (사용자가 고친 엔티티는 유지)"
    elif rework.inherited_from:
        scope = (f"v{rework.inherited_from}의 데이터 모델을 이어받아 바뀐 기능 {len(rework.changed_functions)}개에 연결된 "
                 f"엔티티 {len(targets)}개 ({', '.join(targets) or '없음'}), 새 기능 {len(rework.added_functions)}개는 연결 보완")
    else:
        scope = (f"엔티티 {len(targets)}개 ({', '.join(targets)})" if targets else
                 "의견과 관련된 엔티티만" if feedback else "없음 (사용자 수정만 정리·검증)")
    plan = [f"다시 설계: {scope}"] + ([f"사용자 수정: {rework.edit_text()}"] if rework.has_edits() else [])
    return model, targets, rework, plan


def _config(run_id: str) -> dict:
    """LangGraph 실행 설정 (thread_id = run_id)."""
    return {"configurable": {"thread_id": run_id}, "recursion_limit": 1000}


def _drive(run_id: str, payload) -> int:
    """그래프를 실행하고, 질문 대기·LLM 오류·응답 대기에서 멈추면 재개 방법을 안내한다."""
    with open_checkpointer(get_settings().output_dir) as saver:
        graph = build_graph(saver)
        config = _config(run_id)
        while True:
            try:
                graph.invoke(payload, config)
            except LLMPending:
                exchange = get_settings().llm_exchange_dir
                pending = pending_requests(exchange)
                print(f"\nLLM 응답 대기 {len(pending)}건 (manual) — {exchange}")
                for req in pending:
                    print(f"  {req.name} → {req.name.replace('.request.md', '.response.json')}")
                print(f"응답 파일 작성 후 재개: python -m app resume {run_id}")
                return 5
            except LLMFatalError as e:
                print(f"\nLLM 설정 오류로 실행을 멈췄습니다: {e}")
                print(f"원인(키·크레딧·모델 설정, manual 응답 형식 등)을 해결한 뒤 재개: python -m app resume {run_id}")
                return 4
            snapshot = graph.get_state(config)
            if not snapshot.interrupts:
                break
            interrupt_value = snapshot.interrupts[0].value
            if not sys.stdin.isatty():
                print(f"\n확인 질문 대기 중(WAITING_USER). 재개: python -m app resume {run_id}")
                return 3
            payload = Command(resume=_ask(interrupt_value))
        _print_summary(snapshot.values)
    return 0


def _ask(value: dict) -> dict[str, str]:
    """확인 질문을 터미널에 표시하고 답변을 받는다. 빈 입력은 보류."""
    answers: dict[str, str] = {}
    questions = value.get("questions", [])
    print("\n" + "=" * 70)
    print(f"확인 질문 {len(questions)}건 — 번호 선택 또는 직접 입력, 빈 입력은 보류(가정으로 진행)")
    for q in questions:
        print("-" * 70)
        stage = QUESTION_STAGES.get(q.get("stage"), "요구사항")
        print(f"[{q['question_id']}] ({stage} · {q['kind']}) {q['question']}")
        if q.get("recommended"):
            print("  빈 입력이면 추천 선택지로 설계한 결과를 그대로 씁니다.")
        print(f"  이유: {q['context']}")
        if q.get("requirement_ids"):
            print(f"  관련 요구사항: {', '.join(q['requirement_ids'])}")
        for ev in q.get("evidence", [])[:2]:
            print(f"  근거: {ev[:160]}")
        for i, opt in enumerate(q.get("options", []), start=1):
            print(f"   {i}) {opt}{'  ← 추천' if opt == q.get('recommended') else ''}")
        print(f"   {len(q.get('options', [])) + 1}) 직접 입력")
        try:
            raw = input("  답변> ").strip()
        except EOFError:
            print("\n입력이 닫혀 남은 질문은 보류로 처리합니다.")
            break
        options = q.get("options", [])
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            raw = options[int(raw) - 1]
        elif raw.isdigit() and int(raw) == len(options) + 1:
            try:
                raw = input("  직접 입력> ").strip()
            except EOFError:
                raw = ""
        answers[q["question_id"]] = raw
    print("=" * 70 + "\n")
    return answers


def _print_summary(values: dict) -> None:
    """실행 결과 요약(요구사항 수, 완료 조건, DB 검증, LLM 사용량)을 출력한다."""
    versions = values.get("versions") or []
    completion = values.get("completion") or {}
    reqs = values.get("requirements") or []
    print("\n" + "=" * 70)
    print(f"프로젝트: {_project(values)}  |  설계 버전: v{values.get('design_version', 0)}  |  실행: {values.get('run_id')}")
    if versions:
        print(f"산출물: {versions[-1]['output_dir']}")
    func = sum(1 for r in reqs if r["type"] == "FUNCTIONAL")
    print(f"요구사항 {len(reqs)}건 (기능 {func} / 비기능 {len(reqs) - func})  ·  기능 {len(values.get('functions') or [])}개"
          f"  ·  엔티티 {len((values.get('data_model') or {}).get('entities', []))}개")
    for c in completion.get("checks", []):
        print(f"  {'✔' if c['passed'] else '✘'} {c['name']}: {c['detail']}")
    remarks = sum(1 for i in values.get("issues") or [] if i["kind"].startswith("SEM_") and i["status"] != "RESOLVED")
    if values.get("semantic_review"):
        print(f"의미 검토 지적 {remarks}건 (review_report.md '의미 검토'에서 담당자 확인)")
    db = values.get("db_validation")
    if db:
        print(f"DB 검증({db['engine']}): {'통과' if db['passed'] else '실패'} — {'; '.join(db['checks'])}")
        if db.get("engine") == "sqlite":
            print(f"  └ {db.get('note', '')}")
    usage = values.get("llm_usage") or {}
    cache = (f" · 캐시 읽기 {usage.get('cache_read_tokens', 0):,} / 쓰기 {usage.get('cache_write_tokens', 0):,}"
             if usage.get("cache_read_tokens") or usage.get("cache_write_tokens") else "")
    print(f"LLM 호출 {usage.get('calls', 0)}회 · 토큰 in {usage.get('input_tokens', 0):,} / out {usage.get('output_tokens', 0):,}{cache}")
    print("=" * 70)


def _readable(path: Path) -> bool:
    """문서가 있고 지원 형식인지 확인한다. 아니면 이유와 변환 방법을 출력한다."""
    if not path.exists():
        print(f"문서를 찾을 수 없습니다: {path}")
        return False
    try:
        check_supported(path)
    except UnsupportedFormatError as e:
        print(f"{e}\n  → {e.hint}")
        return False
    return True


def _resolve_input(name: str) -> Path:
    """입력 경로를 찾는다. 그대로 없으면 DATA_DIR(기본 data/) 안에서 같은 이름을 찾는다."""
    path = Path(name)
    if path.exists():
        return path
    candidate = get_settings().data_dir / name
    return candidate if candidate.exists() else path


def _parse(target: Path, include_narrative: bool = False) -> int:
    """LLM 없이 문서분석만 실행한다. 폴더면 안의 문서를 모두 분석해 요약하고, 파일이면 상세 결과를 보여준다."""
    if target.is_dir():
        return _parse_folder(target, include_narrative)
    if not _readable(target):
        return 2
    lines, out_dir = _analyze_file(target, include_narrative)
    print("\n".join(lines))
    print(f"\n→ {out_dir}")
    return 0


def _parse_folder(folder: Path, include_narrative: bool = False) -> int:
    """폴더 안의 모든 문서를 분석한다. 지원하지 않는 형식은 변환 안내와 함께 건너뛴다."""
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(("~$", ".")))
    if not files:
        print(f"분석할 문서가 없습니다: {folder}")
        return 2
    rows = []
    for path in files:
        try:
            check_supported(path)
        except UnsupportedFormatError as e:
            rows.append((path.name, "건너뜀", e.hint))
            continue
        try:
            lines, out_dir = _analyze_file(path, include_narrative)
        except Exception as e:  # noqa: BLE001 — 한 문서 실패가 나머지 분석을 멈추지 않게
            rows.append((path.name, "실패", str(e)[:120]))
            continue
        rows.append((path.name, "완료", f"{lines[2][2:]} → {out_dir}"))
    print(f"# 문서분석: {folder} ({len(files)}개)\n")
    for name, status, detail in rows:
        print(f"- [{status}] {name}\n    {detail}")
    return 0 if any(status == "완료" for _, status, _ in rows) else 2


def _analyze_file(path: Path, include_narrative: bool = False) -> tuple[list[str], Path]:
    """문서 하나를 파싱해 블록·추출 단위·품질 경고를 output/parse-<해시>/에 남기고, 보고서 줄 목록을 돌려준다."""
    blocks = parse_document(path)
    sources = build_sources(blocks, include_narrative)
    findings = check_document(blocks, sources, document_codes(path), textless_pages(path))
    out_dir = get_settings().output_dir / f"parse-{file_fingerprint(path)[1][:6]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "blocks.json").write_text(
        json.dumps([b.model_dump(mode="json") for b in blocks], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "sources.json").write_text(
        json.dumps([s.model_dump() for s in sources], ensure_ascii=False, indent=2), encoding="utf-8")
    tables = sum(b.block_type == "requirement_table" for b in blocks)
    warnings = sum(f.severity != "INFO" for f in findings)
    lines = [f"# 문서분석 결과: {path.name}", "",
             f"- 블록 {len(blocks)}개 (요구사항 표 {tables}개) → 요구사항 추출 단위 {len(sources)}개, 문서 경고 {warnings}건", "",
             "## 추출 단위", "", *[f"- {s.source_key} {s.name or s.heading}{f' ({s.category})' if s.category else ''} ← {', '.join(s.block_ids)}"
               for s in sources],
             "", "## 문서 품질 경고", "", *[f"- [{f.severity}] {f.kind}: {f.message}" for f in findings]]
    (out_dir / "doc_quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines, out_dir
