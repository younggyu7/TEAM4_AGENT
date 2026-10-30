# TRD

- 버전·최종 수정일: 2.0 · 2026-10-30
- 상태: 검토 중 (현재 코드 구조로 다시 씀)
- 파일별 설명: [PROJECT_STRUCTURE.md](../../PROJECT_STRUCTURE.md) · 실행·API·설정: [프로젝트 README](../../README.md) · 변경 기록: [IMPLEMENTATION_STATUS_FOR_FASTAPI.md](../../IMPLEMENTATION_STATUS_FOR_FASTAPI.md)

## 1. 현재 범위

FastAPI(`/api/v1`)가 화면(`TEAM4_AGENT_FRONT`, Vite·React)에 업로드, 산출물 조회, 새 버전 만들기, ERD 최종 확정, 작업 상황을 제공한다.
LLM이 걸리는 작업(몇 분)은 서버 안에서 돌리지 않고 같은 코드의 CLI(`python -m app …`)를 하위 프로세스로 띄운다.

| 화면 동작 | CLI | 멈추는 곳 |
| --- | --- | --- |
| 업로드 | `run <문서> --defer --until function_designer` | 기능 명세 초안 |
| 'AI로 답 반영' | `rerun function_designer --answers [--confirm-rules]` | 기능 명세 새 버전 |
| 내보내기 → ERD | `rerun data_modeler --design <확정한 design.json>` | ERD·DDL |
| ERD 저장 | `rerun data_modeler --data-model <고친 모델>` | 다음 ERD 버전 |
| ERD 최종 확정 | `review <프로젝트> <ERD 버전>` | 최종본 |

아직 하지 않은 것: 서버 인증, 분석 전 추출 미리보기 화면, 검토 처분 서버 저장, 기능 반영률·추적·문제 탐지 품질 평가, 실제 LLM으로 최종 확정(API 설계) 품질 확인.

## 2. 구조 결정

| 영역 | 위치 | 결정 |
| --- | --- | --- |
| API | `app/api/v1/`(기능별 라우터), `app/main.py` | 입력 검사는 `app/core/guards.py`, 오류 응답은 `{"detail"}` |
| 작업 | `app/services/jobs/` | CLI 하위 프로세스 + 작업 기록(메모리·`jobs` 테이블). 프로젝트당 진행 중 작업 1개 |
| Agent 흐름 | `app/agents/flow/`(LangGraph 그래프·노드·상태·rerun) | 조정 Agent(규칙 기반)가 다음 행동을 고른다. `stop_after`보다 뒤 단계는 고르지 않는다 |
| Agent | `app/agents/roles/` 9개 | 조정·문서 분석·요구사항·요구사항 검토·기능 명세·데이터 설계·추적 검토·의미 검토·API 설계. API 설계는 조정 Agent에 넣지 않고 `review` 명령에서만 부른다 |
| 프롬프트 | `app/agents/prompts/<Agent>/<이름>.md` | `load_prompt()`로 읽는다. 코드에 프롬프트 문자열을 두지 않는다 |
| LLM | `app/agents/llm/` (포트 `LLMClient`) | Anthropic·OpenAI·manual 어댑터, 구조화 출력, 병렬 호출, 사용량 집계 |
| 산출물 저장 | `app/agents/storage/` (포트 `ArtifactStore`) | `DATABASE_URL`이 있으면 PostgreSQL 어댑터, 없으면 로컬 폴더 어댑터 |
| 서비스 DB | `app/models/`(테이블), `app/repositories/`(트랜잭션), `app/core/database.py`, `alembic/` | PostgreSQL 16 (루트 `compose.yaml`의 `db`, 볼륨 `team4_agent_pgdata`). 버전 번호는 프로젝트 행 잠금으로 발급 |
| 실행 체크포인트 | LangGraph `PostgresSaver` (DB 없으면 `SqliteSaver`) | rerun·review·resume는 기준 버전을 만든 실행의 마지막 상태에서 시작 |
| 코드 도구 | `app/agents/tools/` (LLM 없음) | 문서 파서, 모델 검사, ERD·DDL 생성기, DB 검증, 산출물 쓰기, 개발 스펙(`dev_spec.py`, `spec-format v1`) |
| 설정 | 루트 `.env` 하나, `app/core/config.py` | docker용 `POSTGRES_*`와 앱 설정을 함께 둔다. 상대 경로는 `backend/` 기준 |

ERD·DDL은 `data_model.json` 하나에서 코드 생성기가 만든다. 생성 DDL 검증은 `VALIDATION_DB_URL`(비우면 같은 컨테이너의 `reqagent_validation`)에 임시 스키마로 적용하며, 접속할 수 없으면 SQLite로 대체 검증하고 보고서에 적는다. 서비스 DB와 검증 DB는 다른 DB다.

## 3. 호출량 결정

- 각 단계는 다음 단계 Agent를 부르지 않는다 (업로드는 기능 명세에서, ERD 만들기는 ERD에서 멈춤).
- 의미 검토는 `SEMANTIC_REVIEW_MODE=final`이면 'ERD 최종 확정' 때 1회, API 설계는 최종 확정 때만 바뀐 기능만(지문 비교).
- 사람이 확인한 답·'추천안 맞음'은 코드로 반영하고, 규칙 앞 표시만 바뀐 기능은 데이터 설계에서 다시 설계하지 않는다.
- 단계별 재시도 한도는 `Settings.fix_attempts(agent)`가 정한다 (`FUNC_FIX_ATTEMPTS`·`MODEL_FIX_ATTEMPTS`·`SEMANTIC_REVIEW_ATTEMPTS`·`API_FIX_ATTEMPTS`, 비면 `MAX_FIX_ATTEMPTS`).

## 4. 검증

- `backend/tests` (가짜 LLM, API 키 없이): DB 없이 192 passed · 9 skipped, `TEST_DATABASE_URL`·`TEST_PG_URL`(PostgreSQL) 포함 199 passed · 2 skipped (2026-10-30). CI(`.github/workflows/ci.yml`)는 PostgreSQL 서비스와 함께 돈다.
- 마이그레이션: `alembic check`로 모델과 DB 차이를 확인한다.
- 품질: `backend/evals`의 골든셋으로 문서 분석·요구사항 추출을 LLM 없이 채점한다.
- 실제 Anthropic·OpenAI 호출 품질, API 설계 응답 품질, 화면 조작은 자동 테스트 밖이며 사람이 따로 확인한다.
