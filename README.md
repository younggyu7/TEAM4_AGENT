# 요구사항 → 설계 산출물 Agent (백엔드)

고객 요구사항 문서(PDF·DOCX·HWPX·TXT)를 받아 요구사항 정의서·기능 명세서·ERD·DDL·추적표·검토 보고서를 만든다.
LangGraph로 조정 Agent와 전문 Agent(문서 분석·요구사항 추출·요구사항 검토·기능 명세·데이터 설계·추적 검토·의미 검토)를 돌리고,
FastAPI가 화면(`TEAM4_AGENT_FRONT`)에 산출물 조회와 새 버전 만들기를 제공한다.

## 시작하기 (로컬)

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # ANTHROPIC_API_KEY 또는 OPENAI_API_KEY 설정
uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
```

서버는 반드시 `backend/` 폴더에서 띄운다 (산출물 `output/`, 입력 `data/`, 설계 표준 `standards/`를 이 폴더 기준으로 찾는다).

프론트는 별도 폴더 `TEAM4_AGENT_FRONT`에서 `npm install && npm run dev` (http://localhost:5173). 개발 서버가 `/api`를 `http://127.0.0.1:8000`으로 넘긴다.

DDL을 PostgreSQL로 검증하려면 `docker compose up -d validation-db` (없으면 SQLite로 대체 검증하고 보고서에 표시).

## 문서 처리 (CLI)

API와 같은 코드를 명령행으로도 실행한다. 화면 업로드가 생기기 전에는 여기서 첫 버전을 만든다.

```bash
python -m app run data/고객요구사항4.hwpx --title "KISA 데이터 금고 지원 포털 구축" --defer --validate-db
python -m app versions 고객요구사항4                                   # 설계 버전 목록
python -m app rerun data_modeler 고객요구사항4 --from v2 --feedback "의견"   # 데이터 설계만 다시 → v2.1
python -m app resume <실행 ID> --defer                                  # 확인 질문에서 멈춘 실행 이어 가기
```

`--defer`는 확인 질문을 묻지 않고 추천안으로 가정해 끝까지 만든다 (질문은 '보류'로 남고 화면에서 답한다).

## API (`/api/v1`)

| 경로 | 내용 |
| --- | --- |
| `GET /projects` | 프로젝트와 설계 버전 목록 (버전 번호는 문자열 `"4"`, `"4.1"`) |
| `GET /design?project=&version=` | design.json (`data_model`·`trace_links`를 합쳐서) |
| `GET·HEAD /file?project=&version=&name=` | 산출물 원문 (`schema.sql`, `erd.mmd`, `functions.md` …) |
| `POST /projects/title` | 버전 제목 저장 |
| `POST /projects/versions` | 기능 명세 내보내기(`source: spec`) → ERD 새 버전, ERD 저장(`source: erd`) → 다음 ERD 버전, `answers`가 있으면 확인 질문 답을 반영한 기능 명세 새 버전 → `202 {job}` |
| `POST /runs` (multipart `file`, `project?`, `title?`, `validate_db?`) | 문서 업로드 → 전체 실행(`--defer`) → `202 {job}` |
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
│   ├── cli.py             # python -m app run·resume·rerun·versions·confirm·diff·parse
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
.venv/bin/pytest -q
```

테스트는 가짜 LLM으로 돈다. 실제 LLM 출력 품질과 PostgreSQL 검증은 따로 실행해 확인한다.
