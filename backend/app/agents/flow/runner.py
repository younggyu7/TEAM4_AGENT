"""DB 실행을 선점하고 LangGraph를 호출하는 단일 작업자 경계."""

import asyncio

from sqlalchemy import select

from app.agents.flow.graph import workflow_graph
from app.agents.flow.stages import WorkflowContext, cursor_for
from app.agents.flow.transitions import StageFailure, advance_cursor, apply_cursor
from app.core.config import Settings
from app.core.tracing import get_tracer
from app.db.session import SessionLocal
from app.models import DesignRun

_worker_lock = asyncio.Lock()


async def process_run(run_id: str, settings: Settings, session_factory=SessionLocal) -> None:
    """queued 실행을 선점하고 DB 저장 상태에서 그래프를 시작한다."""
    async with _worker_lock:
        await _process_locked(run_id, settings, session_factory)


async def _process_locked(run_id: str, settings: Settings, session_factory) -> None:
    with session_factory() as db:
        run = db.scalar(select(DesignRun).where(DesignRun.id == run_id).with_for_update())
        if run is None or run.status != "queued":
            return
        cursor = advance_cursor(cursor_for(run), "start")
        apply_cursor(run, cursor)
        db.commit()
        phase = cursor.phase
    context = WorkflowContext(settings=settings, session_factory=session_factory, tracer=get_tracer(settings))
    try:
        async with asyncio.timeout(15 * 60):
            await workflow_graph.ainvoke({"run_id": run_id, "phase": phase, "stop": False}, context=context)
    except BaseException as exc:
        with session_factory() as db:
            run = db.get(DesignRun, run_id)
            if run is not None and run.status == "running":
                cursor = cursor_for(run)
                code = "STAGE_TIMEOUT" if isinstance(exc, TimeoutError) else "WORKER_INTERRUPTED"
                failure = StageFailure(
                    stage=cursor.phase,
                    code=code,
                    message="실행이 중단되었습니다. 새 실행을 시작하세요.",
                    retryable=False,
                    attempts=0,
                )
                apply_cursor(run, advance_cursor(cursor, "technical_failure", failure=failure))
                db.commit()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
