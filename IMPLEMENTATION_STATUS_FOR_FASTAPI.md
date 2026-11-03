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
cp .env.example .env                 # 루트 .env 하나: ANTHROPIC_API_KEY 또는 OPENAI_API_KEY
cd backend
uvicorn app.main:app --reload        # http://127.0.0.1:8000/docs

# 프론트 (TEAM4_AGENT_FRONT)
npm install
npm run dev                          # http://localhost:5173 (/api → 127.0.0.1:8000 proxy)

# 첫 버전 만들기: 화면 홈의 '새 문서 분석'에서 올리거나 CLI로
python -m app run data/고객요구사항4.hwpx --title "KISA 데이터 금고 지원 포털 구축" --defer --validate-db
```

- 가상환경을 켠 뒤 서버·CLI는 `backend/` 폴더에서 실행한다 (`output/`·`data/`·`standards/`를 이 폴더 기준으로 찾는다). 작업(rerun·run)의 하위 프로세스는 서버와 같은 파이썬(`sys.executable`)을 쓴다.
- CLI 명령은 `python -m req_agent …` → **`python -m app …`** 으로 바뀌었다. 하위 명령·옵션은 그대로다 (`run`·`resume`·`rerun`·`versions`·`confirm`·`diff`·`parse`·`standards`).
- DDL PostgreSQL 검증: 루트에서 `docker compose up -d db` → `reqagent_validation` (없으면 SQLite 대체 검증).

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
| `POST /projects/review` | **신규.** ERD 최종 확정 → `review` → 최종본 v<ERD>.1 (추적·의미 검토) → `202 {job}`, 끝나면 `new_version`. ERD 없는 버전은 400 |
| `POST /runs` | **신규.** 문서 업로드(multipart `file`, `project?`, `title?`, `model?`, `effort?`; `validate_db`는 없앰 — DDL은 항상 검증) → `run --defer --until function_designer` → `202 {job}` |
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
| 'ERD 최종 확정' | `review`: 확정한 ERD 아래 **최종본 v<ERD>.1**을 만든다 (산출물은 ERD 그대로 복사·재출력, 추적 검토(코드) + 의미 검토(ERD ↔ 기능 명세 1회), `semantic_review.md·json`, 최종본 보고서의 'LLM 사용량' = 최종 확정 단계 사용량) → 최종 산출물 화면 | 실패 시 `SEMANTIC_REVIEW_ATTEMPTS`번 |

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

## 10. 산출물 저장소 포트 분리 (2026-10-30)

설계 버전 기록(versions.json)과 버전별 산출물 파일을 포트 뒤로 옮겼다. LLM 포트(`agents/llm`)와 같은 방식이다.

| 구성 | 위치 | 내용 |
| --- | --- | --- |
| 포트 | `app/agents/storage/port.py` `ArtifactStore` | 프로젝트 목록·프로젝트 파일(versions.json·titles.json·diff), 버전 번호 잡기·버전 목록·위치, 산출물 read·write·exists |
| 어댑터 | `app/agents/storage/local.py` `LocalArtifactStore` | 지금 동작 그대로 `output/<프로젝트>/v<버전>/`. 묶음 아래 프로젝트(`team/sample`) 지원, 저장소 밖 경로는 ValueError |
| 선택 | `app/agents/storage/__init__.py` | `get_store()`(설정의 OUTPUT_DIR·--output-dir 로컬 폴더) · `set_store()`(다른 어댑터·테스트) |

- 포트를 쓰게 바꾼 곳: `tools/versions.py`(번호 규칙·기록 정렬은 그대로, 읽기·쓰기만 포트), `tools/exporter.py`(산출물·의미 검토·DB 보고서 쓰기), `tools/trace_diff.py`(이전 버전 읽기), `flow/rerun.py`(기준·이어받을 버전 읽기), `flow/nodes.py`(제공·DB 보고서), `cli.py`(rerun·review·diff·versions), `services/output_store.py`·`api/v1/projects.py`(화면 API 읽기·제목 저장), `services/jobs.py`(새 버전 찾기·결과 읽기).
- 함수 시그니처: 버전 함수는 `root` 인자를 없앴다 (`find_version(project, v)`, `claim_next_version(project, base) -> 번호`). 산출물 함수는 폴더 대신 프로젝트·버전을 받는다 (`export_design(state, version)`, `load_previous(project, parent)`, `prepare_data_model(values, project, version, …)`).
- 포트 밖에 남긴 것 (이번 범위 아님): 실행 체크포인트(`checkpoints.sqlite`, LangGraph SqliteSaver), 업로드 원본(`data/uploads`), CLI 하위 프로세스에 넘기는 `--output-dir`(로컬 폴더 경로), 사용자가 넘기는 입력 파일(`--design`·`--data-model`·`--answers`).
- 검증: `pytest` 177 passed · 3 skipped. `tests/test_storage.py`: 로컬 어댑터 읽기·쓰기·묶음 프로젝트·경로 탈출 차단, **메모리 어댑터로 바꿔 끼워** 기능 명세 → ERD(`--design`) → 최종본 → 화면 API까지 도는지. 실제 `backend/output`(프로젝트 5개)을 화면 API로 읽어 전과 같은지 확인했다.

## 11. 폴더 구조 정리: 기능별 schemas·jobs·API 라우터, 프롬프트 .md (2026-10-30)

동작을 바꾸지 않고 파일만 나눴다. 옮긴 코드는 원래 줄을 그대로 쓰고, 아래처럼 전후를 비교해 확인했다.

| 정리 | 이전 | 이후 | 같은지 확인한 방법 |
| --- | --- | --- | --- |
| 스키마 | `schemas/design.py`·`api.py` | 기능별 입력·출력: `document`(문서 블록·추출 단위) · `requirement`(요구사항·확인 질문) · `function`(기능 명세) · `data_model`(엔티티·관계·DB 검증 결과) · `review`(이슈·추적·조정 판단) · `version`(버전 기록 + 화면 요청 본문) | 모델·상수 32개의 JSON 스키마 전후 비교 32/32 같음. 모든 import를 새 모듈로 바꿈 |
| 작업 | `services/jobs.py` | `services/jobs/`: `common`(작업 기록·실행기·종료 코드·LLM 오류 안내) · `run`(문서 업로드) · `rerun`(새 버전) · `review`(최종 확정). `from app.services import jobs` 그대로 | 테스트(작업 인자·409·404), rerun 옵션 감지(capabilities) 같음. 폴더가 한 단계 깊어져 `BACKEND_ROOT`만 `parents[3]` |
| API | `api/v1/projects.py`·`health.py` | `api/v1/`: `projects`·`artifacts`·`versions`·`review`·`runs`·`jobs`·`deps`(공통 입력 검사). `/health`는 `main.py`에 직접, 라우터는 `main.py`에서 `/api/v1` 아래 연결 | OpenAPI 명세 전체(경로·메서드·파라미터·본문·요약·태그·operationId) 전후 같음 |
| 프롬프트 | Agent 코드 안 문자열 상수 9개 | `agents/prompts/<Agent>/<이름>.md` + `load_prompt()`. 의미 검토는 본문 .md + `common_rules.md`({limit}) 조합 그대로 | 9개 모두 전후 문자열이 한 글자도 다르지 않음 (LLM 입력·프롬프트 캐시·manual 재생 키 동일) |

- 프롬프트 파일 규칙: 파일 끝 줄바꿈 하나는 편집기용이라 읽을 때 지운다. Windows 줄바꿈은 \n으로 읽는다. 프롬프트를 고치면 LLM 입력이 바뀌므로 manual 재생 기록(`fixtures/llm_exchange`)은 다시 만들어야 한다.
- 검증: `pytest` 177 passed · 3 skipped (각 단계마다 실행).

## 12. 서비스 DB: PostgreSQL(docker) · SQLAlchemy 모델 · 저장소 · Alembic (2026-10-30)

사용자 결정: 서버 데이터를 모두 PostgreSQL로, 기존 output은 DB로 가져오기, 기본 테스트는 DB 없이 + PostgreSQL 테스트 따로.

| 저장하는 것 | 이전 | 이후 |
| --- | --- | --- |
| 산출물·버전 기록·화면 제목·diff | `output/<프로젝트>/` 파일 | 서비스 DB `reqagent`: `artifacts`·`design_versions`(record·title)·`projects`·`project_files` |
| 실행 체크포인트 | `output/checkpoints.sqlite` | 서비스 DB의 LangGraph 체크포인트 테이블 (`PostgresSaver`, 테이블은 LangGraph `setup()`이 관리) |
| 작업 기록 (`/jobs`) | 서버 메모리 (재시작하면 사라짐) | 메모리 + `jobs` 테이블 (상태가 바뀔 때·실행 기록 2초마다 저장). 서버 시작 때 '진행 중'으로 남은 작업은 실패로 닫음 |
| DDL 검증 | `validation-db` 컨테이너 `reqagent` | 같은 `db` 컨테이너의 `reqagent_validation` (서비스 DB와 분리) |

- 구조: `app/models/`(테이블, 기능별 project·version·artifact·job) · `app/repositories/`(세션 안의 읽기·쓰기, 버전 번호는 프로젝트 행 잠금) · `app/core/database.py`(연결·`session_scope` 트랜잭션) · `app/agents/storage/postgres.py`(저장소 포트의 PostgreSQL 어댑터) · `alembic.ini`·`alembic/versions/20261030_634de8920307_service_tables.py`.
- 저장소 포트에 `load/save_version_records`·`load/save_titles`를 더했다 (DB는 버전 행의 컬럼, 로컬 폴더는 지금과 같은 versions.json·titles.json).
- `DATABASE_URL`이 없으면 예전처럼 폴더·SQLite (기존 테스트 177개는 이 모드).
- docker: 루트 `compose.yaml`(프로젝트 이름 `team4-agent`)의 `db`(postgres:16, 포트 55432, 볼륨 `team4_agent_pgdata`, 처음 만들 때 `docker/postgres-init`이 `reqagent_validation`·`reqagent_test` 생성), `api`는 시작할 때 `alembic upgrade head`.
- 가져오기: `python -m app import-output` — 로컬 output의 산출물·기록·제목·diff와 실행마다 마지막 체크포인트·대기 중 쓰기. 실제 데이터: 프로젝트 5개·버전 13개·파일 122개·실행 체크포인트 13개, 가져온 실행 상태 13/13이 SQLite 원본과 같음.
- 검증: DB 없이 177 passed · 9 skipped, `TEST_DATABASE_URL` 포함 183 passed · 3 skipped (`tests/test_postgres.py` 6개: 마이그레이션·모델 일치·되돌리기, 어댑터·경로 차단, 동시 번호 잡기 8개 중복 없음, PostgreSQL에서 전체 흐름(초안→--design ERD→최종본→화면 API), 작업 기록 재시작·고아 작업 닫기, 가져오기 후 rerun). `reqagent_validation`에서 DDL 검증 테스트 통과·임시 스키마 0개 남음. CI에 postgres 서비스 추가.
- 남은 것: 화면 수정 초안·확인 체크·확정 표시는 여전히 브라우저(localStorage). 업로드 원본은 `data/uploads` 파일.
- docker 위치 이동: `backend/compose.yaml`·`backend/docker/` → 루트 `compose.yaml`·`docker/` 이어서 `backend/Dockerfile`·`.dockerignore`·`.env`·`.env.example`도 루트로 옮겼다. 설정은 루트 `.env` 하나 (`POSTGRES_*`는 compose, 나머지는 `app/core/config.py`가 `load_dotenv(PROJECT_ROOT/.env)`로 읽음, `DATABASE_URL`·`VALIDATION_DB_URL`은 `POSTGRES_*`와 같은 값). `DATA_DIR`·`OUTPUT_DIR`·`LLM_EXCHANGE_DIR`의 상대 경로는 `backend/` 기준으로 바꿔 어느 폴더에서 띄워도 같다. 이미지는 루트를 빌드 컨텍스트로 `backend/`만 넣고, compose api는 `env_file: .env`에 DB 주소·`OUTPUT_DIR=/data/output`을 덮어쓴다. `.gitignore`에 `.env.*`(예시 제외)·`*.sqlite`·`*.db`·`pgdata/`를 더했다. 볼륨 이름을 `team4_agent_pgdata`로 고정하고 예전 `backend_pgdata` 데이터를 복사했다 (프로젝트 5·버전 13·파일 122·체크포인트 13 그대로, 예전 볼륨은 백업으로 남김).


## 13. client_requirement 추가 기능 반영: 가정 한도·질문 상한·사용자 역할·추천안 확인·API 설계·개발 스펙 (2026-10-30)

`client_requirement`의 `90f65402` 이후 커밋 5개(`984fc261` api_designer·dev_spec, `22b11910` 가정 한도, `7cd2d4b9` 질문 상한, `04b3482e` 경로 정리·사용자 역할·추천안 맞음, `134c97b4` OUTPUTS.md)를 옮겼다.
사용자 결정: **각 단계의 Agent가 다음 단계를 부르지 않는 지금 흐름(업로드 → 기능 명세에서 멈춤, ERD 만들기 → ERD에서 멈춤)과 호출량은 그대로**, API 설계는 'ERD 최종 확정' 때만.

| 기능 | 원본 | 여기 |
| --- | --- | --- |
| '(가정)' 한도 `FUNC_ASSUMPTION_LIMIT`(0.1) · 질문 상한 `FUNC_MAX_QUESTIONS`(12) | 기능 명세 확인 질문 고르기 | 같음 (`FUNC_QUESTION_RATIO`는 없앰). LLM 호출 수 같음 |
| 사용자 역할 공통 결정 | 공통 결정에 `roles`, 행위자를 역할 이름으로 | 같음 (공통 결정 요청 1회의 출력이 조금 늘어남) |
| '추천안 맞음' → `(결정)` | `rerun function_designer --confirm-rules` | 같은 옵션('AI로 답 반영') + **내보내기**는 확정한 design.json의 `confirm_rules`를 `load_submitted_design`이 코드로 반영. LLM 없음 |
| 표시만 바뀐 기능 | ERD 단계에서 엔티티 다시 설계 안 함 | 같음 (`flow/rerun.changed_functions`) |
| API 설계 Agent | 조정 Agent가 데이터 설계 뒤마다 고름 (ERD 만들 때마다 호출) | **조정 Agent에 넣지 않음.** `cli review`(ERD 최종 확정)에서만 `_design_api`: 가장 최근 최종본의 API 설계를 이어받아 지문이 바뀐 기능만, 검사 오류는 `API_FIX_ATTEMPTS`(1)번 그 기능만 |
| 기술 스택 질문 `TQ-001` | 아키텍처 요약(LLM) 뒤에 묻거나 보류, ERD 화면에서 답 → `rerun data_modeler --answers` | **기능 명세 초안에서 코드가 낸다** (`api_designer.stack_question`: 요구사항 제목·설명에 언어·프레임워크 이름이 없을 때만, 필수·추천 1번). 기능 명세 화면에서 답하면 내보내기 design.json으로 ERD·최종본까지 이어지고 개발 스펙에 '확정'. 규칙·ERD는 다시 설계하지 않음. `data_modeler --answers`는 옮기지 않음 |
| 개발 스펙 9개 | 데이터 설계 버전마다 | API 설계를 한 **최종본에만** (`exporter`가 `api_design.functions`가 있을 때, 저장소 포트로 쓰기) |
| 완료 조건 '개발 스펙(API)' | 8개째 조건 | 넣지 않음 (ERD 버전은 지금처럼 7개) |
| 형식 정의 `SPEC_FORMAT.md` | 저장소 루트, Vite 서버 `/api/spec-format` | `backend/SPEC_FORMAT.md` (docker 이미지에 포함), `GET /api/v1/spec-format` |
| 화면 | 개발 스펙 탭·Markdown 보기·zip·확인 필요 규칙·추천안 맞음, ERD 화면 DQ·TQ 답하기 | ERD 화면 질문 답하기는 빼고, TQ는 기능 명세 화면 '확인 필요'에서 답함 (`TEAM4_AGENT_FRONT`) |
| `OUTPUTS.md` | 원본 흐름(전체 실행 v1 …) | 우리 흐름(초안 v1 → ERD v1.1 → 최종본 v1.1.1, DB 저장)에 맞게 다시 씀 |

- 프롬프트: API 설계 지침 2개는 `agents/prompts/api_designer/{api,architecture}.md`. 기능 명세 지침(system.md)에 사용자 역할 한 줄. 옮긴 프롬프트 5개(기능 명세 system·공통 결정·확인 질문, API·아키텍처)가 원본 최종 문자열과 같음을 확인했다.
- 최종본 사용량: `review_report.md`의 LLM 사용량은 최종 확정 단계 전체(API 설계 + 의미 검토), `semantic_review.json`은 의미 검토가 쓴 양만, `design.json`의 `api_design.usage`는 API 설계가 쓴 양.
- 검증: DB 없이 `pytest` 192 passed · 9 skipped, `TEST_DATABASE_URL`·`TEST_PG_URL` 포함 199 passed · 2 skipped, `alembic check` 차이 없음, 프론트 `tsc --noEmit`·`vite build` 통과.
  새 테스트: `tests/test_dev_spec.py` 8개(최종본에만 생성·ERD 버전엔 없음, SPEC_FORMAT 절 제목 일치, 다시 확정하면 API 설계 호출 0회·같은 문서, 기술 스택 가정, 지문, 경로 나눔, 없는 컬럼 1회 수정, `API_DESIGN=false`), `test_function_designer` 가정 한도·질문 상한·추천안 확인·사용자 역할, `test_hitl_flow` 업로드·ERD 단계 API 설계 호출 0회, `test_api` confirm_rules 전달·`/spec-format`.
- 기술 스택 확정 (추가, 사용자 요청): 최종 산출물 '개발 스펙 파일'에서 선택지 3개·직접 입력으로 확정 → `POST /projects/tech-stack` → `exporter.confirm_tech_stack`이 같은 최종본의 개발 스펙 9개·design.json(TQ-001 답·meta.dev_spec)·버전 기록 `tech_stack`만 다시 쓴다 (LLM 없음, 설계 산출물·API 설계 그대로, 원문이 정한 스택은 400). 화면은 그 버전의 design·파일 캐시만 지우고 다시 읽는다(`refreshVersion`). 테스트 `test_dev_spec` 2개 추가.
- 미검증: 실제 LLM으로 최종 확정(API 설계 프롬프트 응답 품질·토큰), 브라우저에서 개발 스펙 탭·'추천안 맞음' 조작. 기술 스택 고르기 화면도 브라우저 조작은 미검증.

## 14. 의미 검토를 ERD 단계로 · 지적 처리 필수 · 최종 확정은 만들기만 (2026-11-02)

사용자 결정: ERD를 만들 때 연결 누락과 데이터 누락을 다 잡고 고친 ERD를 내고, 사람이 남은 것만 판단해 최종 확정한다. 최종 산출물·산출물 보기는 보여 주기만 한다.
API 설계는 최종 확정에 남긴다. 호출이 크게 늘지 않는 범위에서 데이터 지적 자동 보완을 넣는다.

| 바뀐 것 | 전 | 후 |
| --- | --- | --- |
| 의미 검토 시점 | `SEMANTIC_REVIEW_MODE=final`: 'ERD 최종 확정' 때 1회 (`review_saved`) | `erd`(예전 `final`도 erd로 읽음): ERD를 만드는 실행에서 추적 검토 뒤 데이터 모델만 (`state.semantic_parts`). `review_saved`·`SEMANTIC_REVIEW_SOURCE`·`review --source` 없앰 |
| 데이터 지적 | 보고만 | 조정 Agent가 열린 `SEM_MODEL_*` 지적(시도 0회)을 데이터 모델 Agent `semantic` 모드로 한 번 넘김 → 의견 반영 범위 1회 + 고를 테이블 속성 설계 → 고친 테이블 범위만 다시 검토(`state.semantic_recheck`, 고객요구사항13_test4 기준 입력 5.6만 → 1.5만 자). 횟수는 `SEM_FIX` 사유로 따로 셈(`MODEL_FIX_ATTEMPTS`). 다시 설계한 엔티티가 없으면 모델 수정 횟수를 올리지 않음 |
| ERD 저장 (고친 모델만) | 의미 검토 지적을 버림 | 의미 검토를 다시 하지 않고 기준 버전의 지적을 이어받음, `--dispositions`로 처리를 남김 (`cli._keep_remarks`) |
| 사람의 지적 처리 | 브라우저의 '확인함' 표시만 | `Issue.resolution` (반영함 / 문제 아님 → RESOLVED, 미해결로 둠 → UNRESOLVED, 메모). `flow/rerun.apply_dispositions`. 다시 발견되면 처리를 지움 |
| 최종 확정 (`review`) | 추적 검토 → API 설계 → 의미 검토 | 지적 처리 기록 → 모델 오류가 남으면 멈춤(코드 2) → 생성 DDL을 검증 DB에 적용, 실패하면 멈춤 → 추적 검토 → API 설계. 각 단계를 판단 기록에 이어 붙임(`mode: final`), `db_validation.md`·`semantic_review.*`(지적과 처리) 씀 |
| API | `ReviewCreate {project, version}` | `dispositions` 추가, `VersionCreate.dispositions`, `source: answers`(답 없이 의견·수정으로 기능 명세 새 버전) |
| 화면 (`TEAM4_AGENT_FRONT`) | '확인 필요' 아래 '최종 확정' 막대·확정 취소, 최종 산출물 '최종 확정하기'·'다시 검토'·스택 고르기 | 확정 버튼은 오른쪽 위에만: 기능 명세 '내보내기 → ERD 만들기', ERD 'ERD 최종 확정하기'(필수 다 하면 초록색, 한 번 더 확인). 확정 취소 없음(다음 결과가 안 생기고 작업이 실패하면 다시 누를 수 있음). 확정 뒤 '기능 명세 다시 만들기 (새 버전)'. ERD '확인 필요'에 의미 검토 지적 처리(필수). '저장' → '임시저장'. 최종 산출물은 보기만(지적과 처리 카드). 기술 스택만 '개발 스펙 파일'에서 고름(LLM 없이 개발 스펙 다시 쓰기) |

- 기능 명세 다시 만들기: 예전에는 사람이 고친 기능을 '어느 칸을 고쳤는지' 요약(1,800자 제한)으로만 의견에 넘겨 원본에서 다시 시작했다. 이제 고친 기능을 `--functions`로 넘겨 기준 명세에 바꿔 넣고 `state.function_locked`로 고정한다(의견 반영·확인 질문 고르기 뒤에도 되돌림). 고친 것만이면 LLM 0회(`test_function_designer`).
- 다시 내보내기·버전 번호: 기능 명세 버튼을 내보내기 전후 같게 했다. 기능 명세를 다시 만들면(`rerun function_designer`, 기준에 기능 명세가 있음) 다음 최상위 번호(v2)를 받는다. 이미 ERD가 있는 기능 명세를 `rerun data_modeler --design`으로 다시 내보내면 CLI가 먼저 고친 명세로 기능 명세 v2를 만들고(`--design`을 기능 명세 단계에도 받음, 고친 기능 고정, LLM 없음) 그 버전에서 ERD v2.1을 만든다. ERD는 `latest_model`로 앞 기능 명세의 최신 ERD를 이어받아 바뀐 기능의 엔티티만 다시 설계(엔티티 카탈로그 0회). 같은 초에 이어 시작한 두 실행이 실행 ID·체크포인트를 같이 쓰던 문제를 `_unique_run_id`로 막았다(`test_hitl_flow`, 번호가 바뀐 기존 테스트 갱신).
- 모델·생각 수준 선택: 홈 업로드의 DB 검증 체크칸을 모델·effort 선택으로 바꿨다(`GET /llm-options`, `run --model --effort`, 모델 이름으로 공급자 결정). 값은 실행 설정(`options.settings`)에 남아 rerun과 **최종 확정(`review`)도 그 설정을 쓴다** — 예전에는 최종 확정만 `.env` 모델을 써서 ERD와 최종본의 모델이 달랐다(test4 v1.3 Haiku 4.5 → v1.3.1 Haiku 5.5). DDL 검증은 `run` 기본 켬, rerun은 예전 실행 설정과 상관없이 켬(`--no-validate-db`로만 끔).
- 검증: DB 없이 `pytest` 200 passed · 9 skipped. 새·바뀐 테스트: `test_hitl_flow` ERD 단계 검토 → 보완 → 재검토 호출 수(2·1·0), ERD 저장 시 재검토 0회와 처리 이어받기, 최종 확정 의미 검토 0회·API 설계 2회·판단 기록 4단계·처리 기록, 모델 오류면 최종본 없음. `test_api` 처리·기능 명세 다시 만들기 인자. `test_dev_spec` 최종 확정 의미 검토 0회. 프론트 `tsc -b`·`vite build` 통과.
- 미검증: PostgreSQL 포함 테스트, 실제 LLM으로 ERD 단계 의미 검토·보완 품질과 호출량, 브라우저에서 확정 버튼·지적 처리·최종 화면 조작.


## 15. 회원·권한·함께 쓰기·팀장 결정 (2026-11-02)

| 항목 | 내용 |
| --- | --- |
| 회원·세션 | `app/models/user.py`(users·user_sessions, Alembic `7b1e4c2a9d10`), `app/services/users.py`(가입·로그인·승인, 서비스 DB 없으면 `OUTPUT_DIR/service.sqlite`), `app/core/auth.py`(역할·권한표, scrypt 해시, 세션 토큰은 SHA-256 해시만 저장). 쿠키 `reqagent_session`(httpOnly·SameSite=Strict) |
| API | `app/api/v1/auth.py`(signup·login·logout·me·roles·users·users/update), `app/api/v1/drafts.py`(drafts, projects/approval). `main.py`가 auth 밖 라우터에 `Depends(current_user)`. 쓰기 API는 `ensure(member, 권한)` |
| 기록 | 화면 요청으로 만든 버전에 `created_by`, 최종본에 `approval`·`approvals`. 한 작업이 버전을 둘 만들면(다시 내보내기) 둘 다 |
| 화면 | 로그인·회원가입, 회원 관리(`#/members`, 관리자), 권한 없는 버튼·입력칸 끔(`fieldset disabled`, 초안도 편집 안 받음), 서버 초안 동기화(1.2초·임시저장·확정 때, 409면 다시 불러오기), 최종 산출물 '팀장 결정', 단계 화면 위 수정 지시, 단계 표시줄 승인 표시, 대시보드 '진행 중·최종 승인'(서버 기준) |
| 검증 | `pytest` 203 passed · 9 skipped (새: 가입·승인·로그인, 역할별 403, 초안 공유·409, 팀장 결정). `alembic upgrade head` → `alembic check` 차이 없음. 실행 중 서버에서 `/projects` 401 확인. 화면은 `tsc -b`·`vite build`만 (브라우저 조작 미확인) |
| 남은 것 | 비밀번호 바꾸기·찾기 없음, 로그인 시도 횟수 제한 없음, 회원 활동 기록(누가 언제 무엇을)은 버전 기록의 만든 사람·결정만, 조직(팀)별 프로젝트 분리 없음 |

## 16. client_requirement 추가 기능 반영: 업무 동작·개발 단계·의미 검토 지적 보완·빠진 기능만 채우기 (2026-11-03)

`client_requirement`의 `134c97b4`(10-30, 13절에서 옮긴 마지막 커밋) 이후 커밋 12개(`e440187b` … `53379088`)를 3-way 병합으로 옮겼다.
13절의 결정(API 설계는 조정 Agent에 넣지 않고 'ERD 최종 확정' 때만)은 그대로 두고, 그 안에서 돌게 맞췄다.

| 기능 | 원본 | 여기 |
| --- | --- | --- |
| 업무 동작 `BEHAVIOR.md` (spec-format v2, 개발 스펙 10개) | 데이터 설계 버전마다 API 설계와 함께 | `cli review`의 API 설계와 함께 (`agents/roles/behavior.py`, 프롬프트 `prompts/behavior/system.md`). 이전 최종본과 입력이 같으면 다시 묻지 않음. 고객 확인 `BQ-…`(최대 `BEHAVIOR_MAX_QUESTIONS`). 수용 기준·작업에 상태 전환·자동 처리 행 |
| 개발 단계 `develop` | `python -m req_agent develop <프로젝트> --from v1` (버전 폴더에서 읽음) | `python -m app develop <프로젝트> --from v1.1.1` (최종본만). 스펙은 **산출물 저장소**(DB·로컬)에서 읽고 코드는 `OUTPUT_DIR/<프로젝트>/app`에 쓴다. 예시 값 `sample_words.json`은 프로젝트 파일. 화면·API는 없음 ([docs/DEVELOP.md](docs/DEVELOP.md)) |
| 업무 로직 채우기 `--logic` | `agents/developer.py` + 코드 검사 | `agents/roles/developer.py` (프롬프트 `prompts/developer/system.md`), 생각 수준 `DEV_EFFORT` |
| 서비스 코드 틀 | `req_agent/templates/app` | `app/agents/templates/app` (React·Vite 화면, Spring Boot 공통 코드 22개) |
| 의미 검토 데이터 지적 보완 | 데이터 지적·상태 컬럼 누락을 이 실행에서 설계한 테이블만 한 번 다시 설계, 고친 테이블만 다시 검토 | 같음. `SEMANTIC_REVIEW_MODE=erd`(기본)면 ERD를 만들 때. 지적 글을 컬럼 설계 요청에도 넣음, 보고서에 고친 테이블·해결된 지적 |
| 기능 명세 누락 채우기 | 빠진 기능만 추가 (그룹을 통째로 다시 쓰지 않음), 다른 영역 요구사항은 공통 기능에만 연결 | 같음 (`function_designer._linked_ids`) |
| 규칙 근거 대조·정리 | 표시 없는 규칙을 원문과 대조해 '(가정)'(`FUNC_BASIS_CHECK`), 되풀이 규칙 정리(`FUNC_RULE_TRIM`), 영역별 묶음 질문(`FUNC_RULE_BUNDLE`) | 같음 (`agents/tools/rule_basis.py`) |
| 단계별 수정 한도 | `FUNC_·MODEL_·API_FIX_ATTEMPTS`, `Settings.fix_attempts` | 같음 (`.env.example` 값은 이 저장소 기본 1 유지) |
| 사용자 역할 근거 | 원문에 없는 역할 구분은 '가정' | 같음 (기능 명세 공통 결정 표에 근거 칸) |
| 화면 | 개발 스펙 탭에 BEHAVIOR.md | `TEAM4_AGENT_FRONT` `artifacts.ts`에 '업무 동작', 화면 글의 '개발 스펙 9개' → 10개. 백엔드 `output_store.FILES`에 BEHAVIOR.md |

- 프롬프트: 옮긴 13개(.md)가 원본 최종 문자열과 같음을 확인했다 (behavior·developer 새로, api·attribute·function_designer system 갱신).
- 테스트를 이 흐름에 맞춤: 개발 스펙·업무 동작 테스트는 `run` 뒤 `review`로 만든 최종본 `v1.1`에서 본다. 조정 Agent 루프에 API 설계가 없음을 확인하는 줄로 바꿈(`test_fix_loops`).
- 검증: DB 없이 `pytest` 310 passed · 9 skipped (새 파일 `test_behavior`·`test_develop`·`test_fix_loops`·`test_rule_basis`, 기존 파일에 병합된 테스트 포함). `ruff --select F`는 이번에 고친 파일에서 0건 (남은 9건은 main에 있던 것).
- 미검증: `TEST_DATABASE_URL`·`TEST_PG_URL` 테스트(9 skipped), PostgreSQL 저장소에서 `develop`, 실제 LLM으로 최종 확정(업무 동작 요청) → `develop --logic`, 프론트 `tsc`·`vite build`(이 PC에 node_modules 없음). 원본에서는 13_test v4와 다른 문서 7건으로 서버 5xx 0을 확인했다.
