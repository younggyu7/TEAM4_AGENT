"""새 버전 만들기 작업 (기준 버전에서 한 단계만 다시):
- 기능 명세 화면에서 확인 질문 답을 AI로 반영: `rerun function_designer --answers` (추천과 다른 답이 바꾼 것만 다시 설계)
- 기능 명세 내보내기 / ERD 저장: `rerun data_modeler` (확정한 기능 명세는 --design, 고친 데이터 모델은 --data-model, 의견은 --feedback)
- 확인 질문에서 멈추면(종료 코드 3) `resume --defer`로 보류(가정) 처리하고 이어 간다 (공통 실행기).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from app.services.jobs.common import Job, _Runner, _new_job, _path_arg, _temp_file, capabilities


def start_rerun(*, output_dir: Path, project: str, base: str, source: str, data_model=None, design=None,
                spec_md: str | None = None, spec_summary: str | None = None, answers: dict | None = None,
                feedback: str | None = None, on_done: Callable[[str], None] | None = None) -> Job:
    """기준 버전에서 한 단계만 다시 만들어 새 버전(v4.1 …)을 만든다."""
    project_dir = (output_dir / project).resolve()
    # rerun은 '--output-dir 아래의 프로젝트 이름'으로 찾으므로 한 단계 위 폴더와 이름으로 나눈다
    rerun_root, name = project_dir.parent, project_dir.name
    if source == "spec" and answers:
        source = "answers"
    job = _new_job(project=project, base=base, source=source,
                   message=f"기능 명세 v{base}에 확인 질문 답 {len(answers or {})}건을 반영하는 중" if source == "answers"
                   else f"기능 명세 v{base}로 ERD를 만드는 중" if source == "spec" else f"ERD v{base}에서 새 버전을 만드는 중")
    temp: list[Path] = []
    pre: list[str] = []   # 시작 전에 정해지는 안내 (완료 화면 경고에 합친다)
    model_file = _temp_file(job, "data_model.json", json.dumps(data_model, ensure_ascii=False, indent=2), temp) \
        if data_model else None
    feedback = (feedback or "").strip()
    summary = (spec_summary or "").strip()
    spec_args: list[str] = []
    if source == "answers":
        # 기능 명세 수정은 rerun function_designer가 파일로 받지 않으므로 변경 요약을 의견으로 함께 넘긴다
        spec_args += ["--answers", str(_temp_file(job, "answers.json", json.dumps(answers, ensure_ascii=False), temp))]
        if summary:
            feedback = "\n\n".join(x for x in (summary, feedback) if x)
            pre.append("기능 명세 수정 내용은 Agent 의견으로 넘겼습니다 (그 수정과 관련된 기능만 다시 설계).")
    elif source == "spec":
        caps = capabilities()
        if caps["design"] and design:
            spec_args += ["--design", str(_temp_file(job, "design.json", json.dumps(design, ensure_ascii=False), temp))]
        if caps["spec_md"] and spec_md:
            spec_args += ["--spec-md", str(_temp_file(job, "functions.md", spec_md, temp))]
        if not caps["design"] and not caps["spec_md"] and summary:
            feedback = "\n\n".join(x for x in (summary, feedback) if x)
            pre.append("백엔드 rerun에 기능 명세 수정본 옵션(--design·--spec-md)이 아직 없어 수정 내용을 Agent 의견으로 넘겼습니다.")
        # 수정도 의견도 없으면 이 기능 명세로 ERD를 처음부터 설계한다
        if not spec_args and not feedback and not model_file:
            spec_args.append("--full")

    args = ["rerun", "function_designer" if source == "answers" else "data_modeler", name, "--from", f"v{base}",
            "--output-dir", _path_arg(rerun_root),
            *(["--data-model", str(model_file)] if model_file else []),
            *spec_args,
            *(["--feedback", feedback] if feedback else [])]
    _Runner(job, project, rerun_root, submitted_md=spec_md if source == "spec" else None, pre=pre, temp=temp,
            on_done=on_done).start(args)
    return job
