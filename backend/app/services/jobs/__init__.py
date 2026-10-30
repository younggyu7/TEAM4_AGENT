"""화면 요청을 CLI(`python -m app …`) 하위 프로세스 작업으로 실행하고 진행 상황을 기록한다.

  common.py  작업 기록·실행기·종료 코드·LLM 오류 안내 (공통)
  run.py     문서 업로드 → 기능 명세 초안 (`run … --until function_designer`)
  rerun.py   기능 명세 내보내기·ERD 저장·답 반영 → 새 버전 (`rerun data_modeler|function_designer`)
  review.py  ERD 최종 확정 → 최종본 (`review`)
API는 `from app.services import jobs` 후 `jobs.start_run(...)`처럼 이 패키지의 이름으로 부른다.
"""
from app.services.jobs.common import Job, _jobs, _new_job, _Runner, capabilities, get_job, running_job
from app.services.jobs.rerun import start_rerun
from app.services.jobs.review import start_review
from app.services.jobs.run import start_run

__all__ = ["Job", "capabilities", "get_job", "running_job", "start_rerun", "start_review", "start_run"]
