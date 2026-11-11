# TRD

- 버전·최종 수정일: 2.5 · 2026-11-11
- 상태: 검토 중 (현재 코드 구조로 다시 씀)
- 파일별 설명: [PROJECT_STRUCTURE.md](../../PROJECT_STRUCTURE.md) · 실행·API·설정: [프로젝트 README](../../README.md) · 변경 기록: [IMPLEMENTATION_STATUS_FOR_FASTAPI.md](../../IMPLEMENTATION_STATUS_FOR_FASTAPI.md)

## 1. 현재 범위

FastAPI(`/api/v1`)가 화면(`TEAM4_AGENT_FRONT`, Vite·React)에 업로드, 산출물 조회, 새 버전 만들기, ERD 최종 확정, 작업 상황을 제공한다.
LLM이 걸리는 작업(몇 분)은 서버 안에서 돌리지 않고 같은 코드의 CLI(`python -m app …`)를 하위 프로세스로 띄운다.

| 화면 동작 | CLI | 멈추는 곳 |
| --- | --- | --- |
| 업로드 | `run <문서> --defer --until function_designer` | 기능 명세 초안 |
| 'AI로 답 반영' | `rerun function_designer --answers [--confirm-rules]` | 기능 명세 새 버전 |
| 내보내기 → ERD | `rerun data_modeler --design <확정한 design.json>` | ERD·DDL (의미 검토·데이터 지적 보완 포함) |
| ERD 저장 | `rerun data_modeler --data-model <고친 모델> [--dispositions]` | 다음 ERD 버전 (고친 모델만이면 의미 검토 없음) |
| ERD 최종 확정 | `review <프로젝트> <ERD 버전> [--dispositions]` | 최종본 (DDL 검증·추적 검토·API 설계) |

아직 하지 않은 것: 서버 인증, 분석 전 추출 미리보기 화면, 기능 반영률·추적·문제 탐지 품질 평가, 실제 LLM으로 ERD 단계 의미 검토·보완과 최종 확정(API 설계) 품질 확인, 화면 동작 자동 테스트.

## 2. 구조 결정

| 영역 | 위치 | 결정 |
| --- | --- | --- |
| API | `app/api/v1/`(기능별 라우터), `app/main.py` | 입력 검사는 `app/core/guards.py`, 오류 응답은 `{"detail"}` |
| 작업 | `app/services/jobs/` | CLI 하위 프로세스 + 작업 기록(메모리·`jobs` 테이블). 프로젝트당 진행 중 작업 1개 |
| Agent 흐름 | `app/agents/flow/`(LangGraph 그래프·노드·상태·rerun) | 조정 Agent(규칙 기반)가 다음 행동을 고른다. `stop_after`보다 뒤 단계는 고르지 않는다 |
| Agent | `app/agents/roles/` 9개 | 조정·문서 분석·요구사항·요구사항 검토·기능 명세·데이터 설계·추적 검토·의미 검토·API 설계. API 설계는 조정 Agent에 넣지 않고 `review` 명령에서만 부른다. 의미 검토는 ERD 단계에서 조정 Agent가 부르고, 데이터 지적은 데이터 모델 Agent `semantic` 모드로 넘긴다 |
| 프롬프트 | `app/agents/prompts/<Agent>/<이름>.md` | `load_prompt()`로 읽는다. 코드에 프롬프트 문자열을 두지 않는다 |
| LLM | `app/agents/llm/` (포트 `LLMClient`) | Anthropic·OpenAI·manual 어댑터, 구조화 출력, 병렬 호출, 사용량 집계 |
| 시연용 녹화·재생 (검토 중) | `app/agents/llm/client.py` `MockClient`, `.env` `LLM_MODE`·`LLM_MOCK_DIR`·`LLM_REPLAY_DELAY` | `normal`(기본, 실제 호출) / `record`(실제 호출하며 응답·토큰·걸린 시간을 `<요청 해시>.json`으로, 모델 설정을 `model.json`으로 저장) / `replay`(같은 요청이면 저장 응답으로 답하고 녹화 때 토큰을 보고, 걸린 시간 × `LLM_REPLAY_DELAY`(호출당 최대 8초)만 기다림. 없는 요청은 실제로 부르고 녹화). 키는 manual과 같은 해시(스키마·system·shared+user)라 모델과 무관하고, replay는 모델 설정을 `model.json` 값으로 고정해(화면·CLI 선택 무시) 사용량·금액이 녹화 때와 같다. 작업·Slack·Docker·Git은 그대로 실제로 동작. 녹화와 같은 커밋·프로젝트 이름·문서·답 순서여야 맞는다 |
| 산출물 저장 | `app/agents/storage/` (포트 `ArtifactStore`) | `DATABASE_URL`이 있으면 PostgreSQL 어댑터, 없으면 로컬 폴더 어댑터 |
| 서비스 DB | `app/models/`(테이블), `app/repositories/`(트랜잭션), `app/core/database.py`, `alembic/` | PostgreSQL 16 (루트 `compose.yaml`의 `db`, 볼륨 `team4_agent_pgdata`). 버전 번호는 프로젝트 행 잠금으로 발급 |
| 실행 체크포인트 | LangGraph `PostgresSaver` (DB 없으면 `SqliteSaver`) | rerun·review·resume는 기준 버전을 만든 실행의 마지막 상태에서 시작 |
| 코드 도구 | `app/agents/tools/` (LLM 없음) | 문서 파서, 모델 검사, ERD·DDL 생성기, DB 검증, 산출물 쓰기, 개발 스펙(`dev_spec.py`, `spec-format v1`) |
| 설정 | 루트 `.env` 하나, `app/core/config.py` | docker용 `POSTGRES_*`와 앱 설정을 함께 둔다. 상대 경로는 `backend/` 기준 |
| Slack 알림 (제안) | `app/services/slack.py`, `app/models/notification.py` | 팀 워크스페이스 봇 토큰 하나(`.env` `SLACK_BOT_TOKEN`). 프로젝트마다 비공개 채널·초대한 회원을 `slack_projects`에 둔다: 업로드(`POST /runs`) 때 담당자(`project_members`)로 만들고, 채널이 없는 프로젝트는 백그라운드가 만든다(`auth.test` → `conversations.create`(`is_private`) → `conversations.invite`, 실패하면 10분 뒤 다시). 담당자 바꾸기는 `POST /projects/members` → `conversations.invite`·`conversations.kick`. 서버 안의 백그라운드 스레드 하나가 Slack Web API(표준 라이브러리 HTTP, `chat.postMessage`)로 채널에 올린다. '나에게 온 알림'은 저장된 상태에서 계산하므로 올린 알림 키(종류·프로젝트·화면·버전·시각)를 `slack_notifications`에 남겨 한 번만 올리고, 승인 요청·결정·작업 완료 때 바로, 그 밖에는 `SLACK_SYNC_SECONDS`마다 확인한다. 승인·반려 결과는 결정 API가 바로 큐에 넣는다. 멘션은 알림을 받는 담당자 중 채널에 초대된 회원만. Slack 실패는 로그만 남긴다 |
| 프로젝트 담당자 (검토 중) | `app/services/project_access.py`, `app/models/assignee.py`, `app/api/v1/deps.py` | 담당자(`project_members`: 프로젝트·회원 ID)만 그 프로젝트를 본다(PM도 고른 PM만, PM 최소 한 명. PM을 고른 적이 없는 프로젝트는 PM 모두). `current_user`(async 의존성)가 요청한 회원을 ContextVar에 남기고, 프로젝트를 받는 API가 공통으로 부르는 `_project`·`_file`과 작업 조회가 담당자인지 검사해 아니면 404(없는 프로젝트처럼). `GET /projects`·'나에게 온 알림'·Slack 대상도 같은 규칙으로 거른다. 담당자는 업로드 때 정하고(올린 사람 포함) PM만 바꾼다(`POST /projects/members`). 테스트는 `@pytest.mark.assignees`만 규칙을 그대로 쓰고 나머지는 conftest가 모두에게 보이게 둔다 |

ERD·DDL은 `data_model.json` 하나에서 코드 생성기가 만든다. 생성 DDL 검증은 `VALIDATION_DB_URL`(비우면 같은 컨테이너의 `reqagent_validation`)에 임시 스키마로 적용하며, 접속할 수 없으면 SQLite로 대체 검증하고 보고서에 적는다. 서비스 DB와 검증 DB는 다른 DB다.

## 3. 호출량 결정

- 각 단계는 다음 단계 Agent를 부르지 않는다 (업로드는 기능 명세에서, ERD 만들기는 ERD에서 멈춤).
- 의미 검토는 `SEMANTIC_REVIEW_MODE=erd`면 ERD를 만들 때 데이터 모델만: 검토 1회 → 데이터 지적이 있으면 관련 테이블만 다시 설계(데이터 모델 `semantic` 모드, 따로 세는 `MODEL_FIX_ATTEMPTS`번) → 다시 검토 1회(고친 테이블·그 테이블을 쓰는 기능만. 기능 1/4 넘게 쓰는 공통 테이블은 지적 대상 기능만, 범위 밖 지적은 그대로 둠). 사람이 고친 ERD만 저장하거나 최종 확정할 때는 하지 않는다. 다시 설계한 엔티티가 없으면 모델 수정 횟수를 올리지 않아 검토가 다시 돌지 않는다.
- API 설계는 최종 확정 때만 바뀐 기능만(지문 비교). 최종 확정의 나머지(지적 처리 기록·DDL 검증·추적 검토)는 코드다.
- 사람이 확인한 답·'추천안 맞음'은 코드로 반영하고, 규칙 앞 표시만 바뀐 기능은 데이터 설계에서 다시 설계하지 않는다.
- 단계별 재시도 한도는 `Settings.fix_attempts(agent)`가 정한다 (`FUNC_FIX_ATTEMPTS`·`MODEL_FIX_ATTEMPTS`·`SEMANTIC_REVIEW_ATTEMPTS`·`API_FIX_ATTEMPTS`, 비면 `MAX_FIX_ATTEMPTS`).

## 4. 검증

- `backend/tests` (가짜 LLM, API 키 없이): DB 없이 318 passed · 9 skipped (2026-11-04, `test_slack` 포함). PostgreSQL 포함 실행은 2026-10-30 기록(199 passed · 2 skipped) 뒤 다시 하지 않았다. CI(`.github/workflows/ci.yml`)는 PostgreSQL 서비스와 함께 돈다.
- 화면(`TEAM4_AGENT_FRONT`): 타입 검사(`tsc -b`)와 빌드만 한다. 확정 버튼·지적 처리 화면은 브라우저로 확인하지 않았다.
- 마이그레이션: `alembic check`로 모델과 DB 차이를 확인한다.
- 품질: `backend/evals`의 골든셋으로 문서 분석·요구사항 추출을 LLM 없이 채점한다.
- 실제 Anthropic·OpenAI 호출 품질, API 설계 응답 품질, 화면 조작은 자동 테스트 밖이며 사람이 따로 확인한다.
