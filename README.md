# 요구사항 → 설계 산출물 Agent (백엔드)

고객 요구사항 문서(PDF·DOCX·HWPX·TXT)를 받아 요구사항 정의서·기능 명세서·ERD·DDL·추적표·검토 보고서를 만든다.
LangGraph로 조정 Agent와 전문 Agent(문서 분석·요구사항 추출·요구사항 검토·기능 명세·데이터 설계·추적 검토·의미 검토)를 돌리고,
FastAPI가 화면(`TEAM4_AGENT_FRONT`)에 산출물 조회와 새 버전 만들기를 제공한다.

## 시작하기 (로컬)

```bash
# 저장소 루트(TEAM4_AGENT)에서 가상환경을 만든다
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
cd backend
cp .env.example .env               # ANTHROPIC_API_KEY 또는 OPENAI_API_KEY 설정
uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
```

가상환경은 루트의 `.venv`를 쓰고, 서버는 반드시 `backend/` 폴더에서 띄운다 (산출물 `output/`, 입력 `data/`, 설계 표준 `standards/`를 이 폴더 기준으로 찾는다).

프론트는 별도 폴더 `TEAM4_AGENT_FRONT`에서 `npm install && npm run dev` (http://localhost:5173). 개발 서버가 `/api`를 `http://127.0.0.1:8000`으로 넘긴다.

DDL을 PostgreSQL로 검증하려면 `docker compose up -d validation-db` (없으면 SQLite로 대체 검증하고 보고서에 표시).

## 문서 처리 (CLI)

API와 같은 코드를 명령행으로도 실행한다. 첫 버전은 화면 홈의 '새 문서 분석'에서 올리거나 여기서 만든다.

```bash
python -m app run data/고객요구사항4.hwpx --title "KISA 데이터 금고 지원 포털 구축" --defer --until function_designer  # 기능 명세 초안까지
python -m app versions 고객요구사항4                                   # 설계 버전 목록
python -m app rerun data_modeler 고객요구사항4 --from v1 --design confirmed.json   # 확정한 기능 명세로 ERD → v1.1
python -m app rerun data_modeler 고객요구사항4 --from v2 --feedback "의견"   # 데이터 설계만 다시 → v2.1
python -m app review 고객요구사항4 v1.1                                # ERD 최종 확정 때 의미 검토 1회 (새 버전 없음)
python -m app resume <실행 ID> --defer                                  # 확인 질문에서 멈춘 실행 이어 가기
```

`--defer`는 확인 질문을 묻지 않고 추천안으로 가정해 끝까지 만든다 (질문은 '보류'로 남고 화면에서 답한다).

## 화면 흐름 (사람이 확인·확정)

1. **업로드**: 문서 분석 → 요구사항 추출·검토 → 기능 명세 초안에서 멈춘다 (`run --until function_designer`). 기능이 연결 안 된 요구사항(누락)은 `FUNC_FIX_ATTEMPTS`번 다시 설계한다.
2. **기능 명세 확인·수정**: `(가정)`·`(확인 필요)` 표시와 확인 질문을 보고 고친다.
3. **'내보내기 → ERD 만들기'**: 화면이 고친 design.json을 보내고 `rerun data_modeler --design`이 그 기능 명세로 ERD·DDL을 만든다. 이 단계는 기능 명세를 다시 설계하지 않는다. 모델·DB 검증 오류는 `MODEL_FIX_ATTEMPTS`·`MAX_DB_ATTEMPTS`번 자동 수정한다.
4. **ERD 수정·저장**: 고친 테이블만 넘기며, 수정만 있으면 LLM 없이 정리·검증한다.
5. **'ERD 최종 확정'**: 의미 검토를 한 번 한다 (`review`, ERD ↔ 기능 명세). 결과는 그 버전의 `semantic_review.md·json`이고 최종 산출물 화면에 나온다.

| 설정 (`backend/.env`) | 뜻 |
| --- | --- |
| `MAX_FIX_ATTEMPTS` | 아래 단계별 값이 비었거나 0일 때 쓰는 자동 수정·재시도 횟수 (문서 분석·요구사항 등) |
| `FUNC_FIX_ATTEMPTS` | 기능 명세: 누락 재설계·실패 재시도 횟수 |
| `MODEL_FIX_ATTEMPTS` | 데이터 설계: 모델 오류 수정·연결 보완·실패 재시도 횟수 |
| `MAX_DB_ATTEMPTS` | DB 검증 실패 후 다시 고치기 횟수 |
| `SEMANTIC_REVIEW_MODE` | `final`(ERD 최종 확정 때만) · `auto`(산출물이 바뀔 때마다, 코드 기본값) · `off` |
| `SEMANTIC_REVIEW_ATTEMPTS` | 의미 검토 실패 시 시도 횟수 |
| `SEMANTIC_REVIEW_SOURCE` | `true`면 최종 확정 검토에서 기능 명세 ↔ 요구사항(원문)도 본다 |

설정 위치는 `backend/app/core/config.py`의 `Settings` (단계별 한도는 `Settings.fix_attempts`).

## API (`/api/v1`)

| 경로 | 내용 |
| --- | --- |
| `GET /projects` | 프로젝트와 설계 버전 목록 (버전 번호는 문자열 `"4"`, `"4.1"`) |
| `GET /design?project=&version=` | design.json (`data_model`·`trace_links`를 합쳐서) |
| `GET·HEAD /file?project=&version=&name=` | 산출물 원문 (`schema.sql`, `erd.mmd`, `functions.md` …) |
| `POST /projects/title` | 버전 제목 저장 |
| `POST /projects/versions` | 기능 명세 내보내기(`source: spec`) → ERD 새 버전, ERD 저장(`source: erd`) → 다음 ERD 버전, `answers`가 있으면 확인 질문 답을 반영한 기능 명세 새 버전 → `202 {job}` |
| `POST /projects/review` | ERD 최종 확정 → 의미 검토 1회 (그 버전 폴더에 `semantic_review.md·json`) → `202 {job}` |
| `POST /runs` (multipart `file`, `project?`, `title?`, `validate_db?`) | 문서 업로드 → 기능 명세 초안까지 실행(`--defer --until function_designer`) → `202 {job}` |
| `GET /jobs?id=` | 작업 진행 상황 `{status: running·done·failed, message, log, new_version, warnings}` |
| `GET /capabilities` | rerun이 기능 명세 수정본(`--design`·`--spec-md`)을 받는지 |
| `GET /health` | 서버 상태 |

오류 응답은 `{"detail": "..."}`. LLM 작업(몇 분)은 서버 안에서 돌리지 않고 `python -m app …` 하위 프로세스로 띄운다.
작업 기록은 메모리에만 있어 서버를 다시 띄우면 사라진다 (산출물 폴더는 남는다). 서버는 작업자 1개로 띄운다.

## 구조

```text
backend/
├── app/
│   ├── main.py            # FastAPI 앱 (CORS, 라우터)
│   ├── cli.py             # python -m app run·resume·rerun·review·versions·confirm·diff·parse
│   ├── api/v1/            # HTTP 경로 (projects.py, health.py)
│   ├── services/          # output_store.py: 산출물 폴더 읽기 · jobs.py: CLI 하위 프로세스 작업
│   ├── agents/
│   │   ├── roles/         # 조정·문서 분석·요구사항 추출·요구사항 검토·기능 명세·데이터 설계·추적 검토·의미 검토 (프롬프트 포함)
│   │   ├── flow/          # LangGraph 그래프(graph)·노드(nodes)·상태(state)·단계 다시 만들기(rerun)
│   │   ├── llm/           # LLM 호출 (Anthropic·OpenAI·manual)
│   │   └── tools/         # 문서 파서·청킹·검색·모델 검사·DDL·ERD·내보내기·버전·DB 검증
│   ├── schemas/           # design.py: 단계 간 계약(요구사항·기능·데이터 모델…) · api.py: 요청 본문
│   └── core/              # config(.env 설정) · exceptions(오류 응답) · guards(요청 값·경로 검사) · logging · security(CORS·비밀 값 가리기)
├── tests/                 # pytest (가짜 LLM, API 키 없이 실행)
├── evals/                 # 문서 분석·요구사항 추출 골든셋 평가 (python -m evals.eval_document_analyst)
├── standards/             # 회사별 설계 표준 프로필 (run --standard public)
├── data/                  # 샘플 고객 요구사항 문서, 업로드 문서(data/uploads, git 제외)
└── output/                # 산출물 버전 폴더·체크포인트 (git 제외)
```

## 검증

```bash
cd backend
../.venv/bin/pytest -q
```

테스트는 가짜 LLM으로 돈다. 실제 LLM 출력 품질과 PostgreSQL 검증은 따로 실행해 확인한다.
