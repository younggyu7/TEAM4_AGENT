# FastAPI 전환 구현 현황

기준일: 2026-10-29 · 대상: `TEAM4_AGENT/backend` (백엔드), `TEAM4_AGENT_FRONT` (프론트, 별도 폴더)

## 1. 요약

- `client_requirement`의 설계 Agent(`req_agent`)를 이 저장소 `backend/app` 구조로 옮기고 FastAPI(`/api/v1`)로 감쌌다.
- 기존 ITSM 백엔드 코드(에이전트 7개, PostgreSQL·Alembic 기반 `/api/v1/projects·runs`)는 **지금 코드로 교체**했다. 폴더 구조(`api`·`agents`·`services`·`schemas`·`core`)는 유지했다.
- 프론트는 백엔드와 분리해 `TEAM4_AGENT_FRONT`에 두었다. Vite 개발 서버의 임시 API(`server/outputApi.ts`·`jobs.ts`)를 없애고, 같은 일을 FastAPI가 한다.
- 동작 방식은 전환 전과 같다: 산출물은 DB 없이 `output/<프로젝트>/v<버전>/` 폴더에 저장하고, 오래 걸리는 LLM 실행은 CLI(`python -m app …`)를 하위 프로세스로 띄운다.
- 검증: `pytest` 170 passed · 3 skipped (API 키 없이). 로컬에서 FastAPI + 프론트를 띄워 `고객요구사항4` 산출물이 화면(홈·기능 명세서·ERD·DDL·추적)에 나오는 것과, 화면 요청 → 작업(rerun) → 새 버전 생성까지 확인했다.
- 화면 흐름 변경 (사용자 합의, 아래 9절): 업로드는 기능 명세 초안에서 멈추고, '내보내기 → ERD 만들기'는 화면에서 확정한 기능 명세(`--design`)로 ERD를 만들며, 의미 검토는 'ERD 최종 확정' 때 한 번만 한다. 자동 수정 횟수는 단계별로 나눴다.

## 2. 실행 방법 (로컬)

```bash
# 백엔드 — 가상환경은 저장소 루트(TEAM4_AGENT/.venv)에 하나 둔다
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cd backend
cp .env.example .env                 # ANTHROPIC_API_KEY 또는 OPENAI_API_KEY
uvicorn app.main:app --reload        # http://127.0.0.1:8000/docs

# 프론트 (TEAM4_AGENT_FRONT)
npm install
npm run dev                          # http://localhost:5173 (/api → 127.0.0.1:8000 proxy)

# 첫 버전 만들기: 화면 홈의 '새 문서 분석'에서 올리거나 CLI로
python -m app run data/고객요구사항4.hwpx --title "KISA 데이터 금고 지원 포털 구축" --defer --validate-db
```

- 가상환경을 켠 뒤 서버·CLI는 `backend/` 폴더에서 실행한다 (`output/`·`data/`·`standards/`를 이 폴더 기준으로 찾는다). 작업(rerun·run)의 하위 프로세스는 서버와 같은 파이썬(`sys.executable`)을 쓴다.
- CLI 명령은 `python -m req_agent …` → **`python -m app …`** 으로 바뀌었다. 하위 명령·옵션은 그대로다 (`run`·`resume`·`rerun`·`versions`·`confirm`·`diff`·`parse`·`standards`).
- DDL PostgreSQL 검증: `docker compose up -d validation-db` (없으면 SQLite 대체 검증).

## 3. 코드 이동 (`req_agent` → `backend/app`)

| 이전 (`client_requirement`) | 이후 (`backend/`) |
| --- | --- |
| `req_agent/agents/*.py` (8개 Agent, 프롬프트 포함) | `app/agents/roles/` |
| `req_agent/graph.py`, `nodes.py`, `state.py`, `rerun.py` | `app/agents/flow/` |
| `req_agent/llm.py` | `app/agents/llm/client.py` |
| `req_agent/tools/*.py` | `app/agents/tools/` |
| `req_agent/schemas.py` | `app/schemas/design.py` |
| `req_agent/config.py` | `app/core/config.py` |
| `req_agent/cli.py`, `__main__.py` | `app/cli.py`, `app/__main__.py` |
| `tests/`, `evals/`, `standards/`, `fixtures/`, `data/` | `backend/` 아래 같은 이름 |

- import는 스크립트로 일괄 변경했다 (상대 import도 절대 경로 `app.…`로). Agent 로직은 바꾸지 않았다.
- `standards/` 경로 계산(`convention.py`)만 새 폴더 깊이에 맞췄다.
- 기존 ITSM 코드에서 지운 것: `app/agents`(옛 구현), `app/models`, `app/db`, `app/schemas`(옛 계약), `app/services`(옛 서비스), `api/v1/workflow.py`·`deps.py`, `core/tracing.py`(Langfuse), `alembic.ini`·마이그레이션, `tests/test_core.py`(옛 테스트).

## 4. 새로 만든 것

### 4.1 API (`app/api/v1/projects.py`, prefix `/api/v1`)

| 경로 | 내용 |
| --- | --- |
| `GET /projects` | 프로젝트와 설계 버전 목록 (버전 번호는 문자열 `"4"`, `"4.1"`, 화면 제목 `title` 포함) |
| `GET /design?project=&version=` | design.json. 새 형식은 `data_model.json`·`traceability.csv`를 읽어 `data_model`·`trace_links`로 합쳐 준다 |
| `GET·HEAD /file?project=&version=&name=` | 산출물 원문 (`schema.sql`, `erd.mmd`, `functions.md` …, 허용 목록만) |
| `POST /projects/title` | 버전 제목 저장 (`output/<프로젝트>/titles.json`) |
| `POST /projects/versions` | 화면의 새 버전 만들기 → `202 {job}` (아래 4.2) |
| `POST /projects/review` | **신규.** ERD 최종 확정 → `review` (의미 검토 1회, 그 버전 폴더에 `semantic_review.md·json`) → `202 {job}`. ERD 없는 버전은 400 |
| `POST /runs` | **신규.** 문서 업로드(multipart `file`, `project?`, `title?`, `validate_db?`) → `run --defer --until function_designer` → `202 {job}` |
| `GET /jobs?id=` | 작업 진행 상황 `{status, message, log, new_version, warnings}` |
| `GET /capabilities` | rerun이 기능 명세 수정본(`--design`·`--spec-md`)을 받는지 |
| `GET /health` | 서버 상태 |

- 경로·응답 형태는 전환 전 Vite 개발 API와 같다. 바뀐 것은 앞부분 `/api` → `/api/v1` 뿐이다.
- 오류 응답은 모두 `{"detail": "..."}`.

### 4.2 작업 실행 (`app/services/jobs.py`)

전환 전 `frontend/server/jobs.ts`를 그대로 옮겼다.

| 화면 동작 | 실행하는 CLI |
| --- | --- |
| 기능 명세 '답 반영 → 기능 명세 다시' (`source: spec` + `answers`) | `rerun function_designer --answers` (기능 수정 요약은 `--feedback`) |
| 기능 명세 '내보내기 → ERD 만들기' (`source: spec`) | `rerun data_modeler --design <화면에서 확정한 design.json>` |
| ERD '저장하고 새 버전 만들기' (`source: erd`) | `rerun data_modeler --data-model <고친 모델> --feedback <의견>` |
| ERD '최종 확정' (`POST /projects/review`) | `review <프로젝트> v<버전>` (새 버전 없이 결과 파일) |
| 문서 업로드 (`POST /runs`) | `run <문서> --project <이름> --defer --until function_designer` |

- 확인 질문에서 멈추면(종료 코드 3) `resume --defer`로 이어 간다.
- 끝나면 새 버전 폴더를 찾아 `new_version`으로 알려 주고, 판단 기록의 실패·완료 조건 미충족·LLM 오류(사용 한도·크레딧·키)를 `warnings`로 모은다.
- 한 프로젝트에 작업은 하나만 (두 번째 요청은 409).
- backend 안의 경로는 상대 경로로 넘겨 `versions.json`의 `output_dir`이 폴더를 옮겨도 맞게 했다.

### 4.3 산출물 읽기 (`app/services/output_store.py`)

`frontend/server/outputApi.ts`를 옮겼다. `v<N>/design.json`이 있는 폴더를 프로젝트로 찾고(3단계 아래까지), `versions.json`·`titles.json`을 합쳐 버전 목록을 만든다.

### 4.4 core (`app/core/`)

| 파일 | 내용 |
| --- | --- |
| `config.py` | `.env` 설정 (기존 Agent 설정 + `LOG_LEVEL`, `CORS_ORIGINS`) |
| `exceptions.py` | `AppError`와 `BadRequest`·`NotFound`·`Conflict`·`PayloadTooLarge`, 예외 핸들러. 예상 못 한 오류는 500 + 일반 문구 (내부 정보는 서버 로그에만) |
| `guards.py` | 요청 값 검사: output 밖 경로 차단(`inside`), 버전 형식, 프로젝트 이름, 업로드 파일 이름, 업로드 최대 50MB |
| `logging.py` | 로그 형식 (기존 유지) |
| `security.py` | CORS 허용 출처, 접속 URL 비밀번호 가리기(`mask_url`), 화면에 나가는 작업 기록에서 API 키·비밀번호 가리기(`redact`) |

## 5. 프론트 (`TEAM4_AGENT_FRONT`)

- `client_requirement/frontend`에서 `server/`(Vite 임시 API)만 빼고 복사했다.
- API 주소: `src/api.ts`의 `BASE = VITE_API_BASE || '/api/v1'`. 파일 링크(`/file`)도 같은 함수(`apiUrl`)를 쓴다.
- `vite.config.ts`: `/api` → `VITE_API_PROXY` (기본 `http://127.0.0.1:8000`).
- 배포 시: 프론트 빌드에 `VITE_API_BASE=https://<백엔드>/api/v1`, 백엔드 `.env`의 `CORS_ORIGINS`에 프론트 주소.
- 화면 코드는 API 주소와 안내 문구(`python -m app …`)를 바꾸고, 홈에 **문서 업로드('새 문서 분석')**를 추가했다: 파일·프로젝트 이름·사업 이름·DB 검증 여부 → `POST /runs` → 진행 상황(`JobPanel`) → 끝나면 기능 명세 첫 버전 열기. 타입 검사 통과.

## 6. 설정·배포 파일

| 파일 | 변경 |
| --- | --- |
| `backend/requirements.txt` | FastAPI·uvicorn·python-multipart + Agent 의존성(langgraph, anthropic, openai, pdfplumber …) |
| `backend/.env.example` | Agent 설정 + API 서버(`LOG_LEVEL`, `CORS_ORIGINS`) |
| `backend/Dockerfile` | 산출물을 `/data/output` 볼륨에 저장 |
| `backend/compose.yaml` | 서비스 DB(PostgreSQL) 제거, `validation-db`(DDL 검증용) + `api` |
| `.github/workflows/ci.yml` | PostgreSQL·Alembic 단계 제거, `pytest`만 (API 키 불필요) |
| `.gitignore` | `backend/output/`, `backend/data/uploads/` 제외 |
| `README.md` | 실행 방법·API·구조를 새 코드 기준으로 다시 씀 |

## 7. 테스트

| 테스트 | 내용 |
| --- | --- |
| 기존 155개 (`tests/test_*.py`, 3개 건너뜀) | Agent·그래프·도구 (가짜 LLM). import만 바꿔 그대로 통과 |
| `tests/test_api.py` (7) | 프로젝트 목록·design 합치기·파일 GET/HEAD·경로 탈출 거부·제목 저장·새 버전 입력 검사·작업 409/404·지원 안 하는 문서 거부·rerun 인자 |
| `tests/test_core.py` (4) | guards 경로·값 검사, 비밀 값 가리기, 오류 응답 형태 |
| `tests/test_api.py` 추가 (2) | 업로드 인자 `--until function_designer`·review 작업 인자, `POST /projects/review` 202·400(ERD 없음)·404 |
| `tests/test_hitl_flow.py` (5) | 단계별 시도 횟수, 초안 멈춤 → `--design`으로 ERD(기능 명세 재설계 0회), 잘못된 요구사항 ID 거부, `final` 모드에서 실행 중 의미 검토 0회·`review` 1회(`--source`면 기능 명세도), ERD 없는 버전 review 거부 |

- `tests/conftest.py`: `.env` 키가 없으면 테스트용 키를 넣는다 (CI에서 키 없이 실행).
- 실제 LLM 출력 품질과 PostgreSQL 검증은 테스트에 포함되지 않는다.

## 8. 아직 안 된 것 · 주의

- **화면 업로드는 LLM 실행 미확인**: 업로드 화면과 `POST /runs` 입력 검사(지원 안 하는 형식 400)는 확인했지만, 실제 LLM으로 끝까지 도는 업로드 실행은 아직 확인하지 않았다.
- **작업 기록은 메모리**: 서버를 다시 띄우면 `/jobs` 기록이 사라진다 (산출물 폴더는 남음). 서버는 작업자 1개로 띄운다.
- **로그인·편집 초안·최종 확정**: 아직 브라우저(localStorage)에만 저장한다. 백엔드 저장은 미구현.
- **ruff**: `pyproject.toml` 설정은 남았지만 옮긴 코드에 적용하지 않아 CI에서 뺐다.
- **Agent 예외 클래스**(`LLMFatalError`, `RerunError`, `UnsupportedFormatError` 등)는 원래 모듈에 있다 (`core/exceptions.py`로 모으지 않음).
- **문서 불일치**: `docs/plans/`(ITSM PRD·TRD 등)와 `AGENTS.md`는 옛 ITSM 구현 기준이라 지금 코드와 다르다.
- **루트 `debug-design`**: 없는 모듈(`scripts.debug_design`)을 실행하는 옛 스크립트라 동작하지 않는다.
- **로컬 데이터**: `backend/output/`에 `고객요구사항4` 산출물과 체크포인트(`checkpoints.sqlite`)를 넣어 두었다 (git 제외). `rerun`은 기준 버전을 만든 실행의 체크포인트가 있어야 한다.

## 9. 화면 흐름 변경: 기능 명세에서 멈춤 · 확정한 명세로 ERD · 최종 확정 때 의미 검토 (2026-10-29)

사용자와 합의한 흐름: LLM 초안은 사람이 확인할 곳((가정)·(확인 필요)·확인 질문)을 표시해 보여 주고, 많이 돌리지 않는다.

| 단계 | 동작 | 자동 반복 |
| --- | --- | --- |
| 업로드 | 문서 분석 → 요구사항 추출·검토 → 기능 명세 + 확인 질문 고르기 → 멈춤 | 기능 없는 요구사항은 빠진 기능만 추가 `FUNC_FIX_ATTEMPTS`라운드 |
| '내보내기 → ERD 만들기' | `rerun data_modeler --design`: 화면에서 고친 functions·common으로 데이터 설계 → ERD·DDL(코드) | 모델 오류 수정·연결 보완 `MODEL_FIX_ATTEMPTS`번, DB 검증 `MAX_DB_ATTEMPTS`번. 기능 명세는 다시 설계하지 않고 누락은 이슈로만 |
| ERD 수정·저장 | 고친 테이블만 반영 (수정만 있으면 LLM 없음) | 위와 같음 |
| 'ERD 최종 확정' | `review`: ERD ↔ 기능 명세 의미 검토 1회 → `semantic_review.md·json` → 최종 산출물 화면 | 실패 시 `SEMANTIC_REVIEW_ATTEMPTS`번 |

- `--design` 입력은 JSON이다. data_modeler는 기능의 ID·이름·행위자·데이터 객체·입출력 이름·업무 규칙을 구조로 읽기 때문이다 (MD는 기록용 `functions.submitted.md`). 기능 ID 중복·기준 버전에 없는 요구사항 ID는 거부한다.
- 설정 (`backend/app/core/config.py` `Settings`, `.env`): `FUNC_FIX_ATTEMPTS`·`MODEL_FIX_ATTEMPTS`·`SEMANTIC_REVIEW_ATTEMPTS`(0·빈 값이면 `MAX_FIX_ATTEMPTS`), `MAX_DB_ATTEMPTS`, `SEMANTIC_REVIEW_MODE`(final·auto·off, 코드 기본값 auto), `SEMANTIC_REVIEW_SOURCE`. 단계별 한도는 `Settings.fix_attempts(agent)`가 정하고, 조정 Agent(`gave_up`·`stuck_stages`)와 이슈 시도 횟수(`reconcile_issues`)가 같이 쓴다.
- 프론트: ERD '확인 필요' 탭의 'ERD 최종 확정'이 `POST /projects/review`를 부른다. 최종 산출물 화면에 '의미 검토' 카드(지적·근거·제안, 다시 검토·받기)를 추가했다. 확정 표시 자체는 여전히 브라우저에만 저장한다.
- 실제 LLM 확인: `고객요구사항4` v3 review 1회 — 호출 1회, 입력 20,812 · 출력 5,328 토큰, 지적 7건. 업로드 → `--design` ERD 전체 흐름의 실제 LLM 실행은 아직 하지 않았다.

### 9.1 연결 검사 역할 정리 · 누락 채우기 (2026-10-29)

| 연결 | 검사 | 누락 처리 |
| --- | --- | --- |
| 원문 → 요구사항 | 요구사항 검토 Agent (원문 대조, LLM) | 보충·확인 질문 |
| 요구사항 → 기능 명세 | 요구사항 검토 Agent `coverage_findings` (코드, 결과 검사 노드에서 기능 명세가 바뀔 때마다) → `REQ_WITHOUT_FUNCTION` | 초안: 기능 명세 Agent `gap` 모드로 빠진 기능만 추가 / 확정한 명세(`rerun data_modeler`): 보고만 |
| 기능 명세 → ERD | 추적 검토 Agent (코드) → `TRACE_FUNC_WITHOUT_ENTITY` | 데이터 모델 Agent 연결 보완 `MODEL_FIX_ATTEMPTS`라운드 |
| 의미 | 의미 검토 Agent ('ERD 최종 확정', `SEMANTIC_REVIEW_SOURCE=false`면 ERD ↔ 기능 명세만) | 보고만 |

- 문제였던 실행(`고객요구사항13_test2`): 누락 1건을 채우려고 '부분 재설계'가 그룹 2개(누락 요구사항 그룹 + 공통 기능을 맡은 그룹)를 처음부터 다시 써서 다른 요구사항 연결이 깨졌다 (누락 1 → 8건, 기능 50 → 38 → 58개). 재시도 한도도 이슈마다 세어 계속 돌았다.
- 바꾼 것: 재시도는 Agent별 라운드로 센다 (`coordinator.fixes_left`). 누락 채우기(`function_designer` `gap`)는 누락 요구사항의 그룹만, `_patch`(바꿀 기능만 받음)로 요청하고, 공통 기능이 맡은 요구사항이라도 그 그룹이 직접 연결하게 한다 (`_group_scope(gaps)`). 기존 기능은 다시 쓰지 않는다.
- 테스트: `test_graph_offline.py`의 `test_gap_redesign_rounds_are_limited_per_stage`·`test_gap_fill_adds_only_missing_function`·`test_confirmed_spec_gap_is_reported_not_redesigned`. 실제 LLM으로 누락 채우기를 돌려 확인하지는 않았다.

### 9.2 확인 질문 답은 코드로 반영 (2026-10-29)

- 기능 명세 화면에서 새로 답한 질문이 있어도 '기능 명세 최종 확정'과 '내보내기 → ERD 만들기'가 막히지 않는다. 내보내면 `rerun data_modeler --design`이 답을 **LLM 없이** 반영한다 (`function_designer.apply_answers_by_code`, `rerun._record_answers`).
  - 추천안과 같은 답: 규칙 표시 `(확인 필요 FQ-…)` → `(결정 FQ-…)`
  - 다른 답: 그 질문 표시가 붙은 규칙을 `(결정 FQ-…) 질문 → 답`으로 바꾼다. 공통 결정은 근거 '결정'·답 기록
  - 요구사항: 결정 기록, 분류·포함 여부(RC)는 답대로 고침, 충돌(CF)·요구사항 질문은 검토 표시를 지우고 확정
- 코드는 질문 표시가 붙은 규칙만 바꾼다. 답이 흐름·입력·행위자·데이터·다른 기능에 주는 영향은 화면이 '추천안과 다른 답' 항목에 확인하라고 표시하고 사람이 고친다.
- LLM으로 답을 반영하고 싶을 때만 오른쪽 위 '답 N건 AI로 반영 → 기능 명세 다시' (기존 `rerun function_designer --answers`, 토큰 사용).
- 테스트: `test_hitl_flow.py::test_confirmed_answers_are_applied_by_code`. 프론트는 타입 검사만 했다.
