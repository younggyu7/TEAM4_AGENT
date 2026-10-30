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
cp .env.example .env               # 설정 하나 (TEAM4_AGENT/.env): ANTHROPIC_API_KEY 또는 OPENAI_API_KEY 설정, DB 값은 예시 그대로
docker compose up -d db            # 서비스 DB·DDL 검증 DB (PostgreSQL 16, 데이터는 docker 볼륨 team4_agent_pgdata)
cd backend
alembic upgrade head               # 서비스 DB 테이블 만들기·최신으로 올리기
python -m app import-output        # (처음 한 번) 예전 output 폴더의 산출물·체크포인트를 DB로 가져오기
uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
```

가상환경과 설정(`.env`)은 루트에 하나씩 두고, 서버·CLI·alembic은 `backend/` 폴더에서 실행한다 (`app` 패키지가 이 폴더에 있다). `.env`의 상대 경로(`DATA_DIR`·`OUTPUT_DIR`)는 어디서 띄워도 `backend/` 기준이다.

프론트는 별도 폴더 `TEAM4_AGENT_FRONT`에서 `npm install && npm run dev` (http://localhost:5173). 개발 서버가 `/api`를 `http://127.0.0.1:8000`으로 넘긴다.

### 데이터 저장 (PostgreSQL)

| 저장하는 것 | 위치 |
| --- | --- |
| 산출물·버전 기록·화면 제목·작업 기록 | 서비스 DB `reqagent`의 테이블 (`app/models`, 트랜잭션은 `app/repositories`, 마이그레이션은 `alembic/`) |
| 실행 체크포인트 (resume·rerun·review) | 서비스 DB `reqagent`의 LangGraph 체크포인트 테이블 (PostgresSaver가 관리) |
| Agent가 만든 DDL 검증 | 같은 컨테이너의 `reqagent_validation` (검증마다 임시 스키마를 만들고 지움) |

- `DATABASE_URL`을 비우면 예전처럼 `OUTPUT_DIR` 폴더 + `checkpoints.sqlite`에 저장한다 (테스트 기본값).
- 모델을 바꾸면 `alembic revision --autogenerate -m "설명"` → `alembic upgrade head`.
- docker 명령(`docker compose …`)은 루트(`TEAM4_AGENT`)에서 실행한다. DB 데이터까지 지우려면 `docker compose down -v` (볼륨 삭제).

#### PostgreSQL 환경변수

루트 `.env` 한 파일에 두 묶음이 있고 **같은 값**이어야 한다. 기본값 그대로 쓰면 아무것도 바꿀 필요 없다.

| 변수 | 쓰는 곳 |
| --- | --- |
| `POSTGRES_USER` · `POSTGRES_PASSWORD` · `POSTGRES_DB` · `POSTGRES_PORT` | docker가 DB 컨테이너를 만들 때 (`compose.yaml`) |
| `DATABASE_URL` · `VALIDATION_DB_URL` | 서버·CLI·alembic이 DB에 접속할 때 (`app/core/config.py`가 루트 `.env`를 읽음) |

```
DATABASE_URL=postgresql://<POSTGRES_USER>:<POSTGRES_PASSWORD>@localhost:<POSTGRES_PORT>/<POSTGRES_DB>
VALIDATION_DB_URL=postgresql://<POSTGRES_USER>:<POSTGRES_PASSWORD>@localhost:<POSTGRES_PORT>/reqagent_validation
```

- 사용자·비밀번호는 볼륨을 처음 만들 때만 적용된다. 바꾸려면 `docker compose down -v`로 지우고 다시 만든 뒤 `alembic upgrade head` → `python -m app import-output`.
- `docker compose up -d`로 api까지 docker에서 띄우면 같은 `.env`를 쓰되, DB 주소(`db` 서비스 이름)와 산출물 폴더(`/data/output` 볼륨)는 compose가 덮어쓴다. 이미지(루트 `Dockerfile`)에는 `backend/` 코드만 들어가고 `.env`·산출물·DB 파일은 `.dockerignore`로 뺀다.
- `.env`와 DB 파일(`*.sqlite`·`*.db`)은 `.gitignore`로 git에 올리지 않는다. 올리는 것은 `.env.example`뿐이다.

## 문서 처리 (CLI)

API와 같은 코드를 명령행으로도 실행한다. 첫 버전은 화면 홈의 '새 문서 분석'에서 올리거나 여기서 만든다.

```bash
python -m app run data/고객요구사항4.hwpx --title "KISA 데이터 금고 지원 포털 구축" --defer --until function_designer  # 기능 명세 초안까지
python -m app versions 고객요구사항4                                   # 설계 버전 목록
python -m app rerun data_modeler 고객요구사항4 --from v1 --design confirmed.json   # 확정한 기능 명세로 ERD → v1.1
python -m app rerun data_modeler 고객요구사항4 --from v2 --feedback "의견"   # 데이터 설계만 다시 → v2.1
python -m app review 고객요구사항4 v1.1                                # ERD 최종 확정 → 최종본 v1.1.1 (추적·의미 검토)
python -m app resume <실행 ID> --defer                                  # 확인 질문에서 멈춘 실행 이어 가기
```

`--defer`는 확인 질문을 묻지 않고 추천안으로 가정해 끝까지 만든다 (질문은 '보류'로 남고 화면에서 답한다).

## 화면 흐름 (사람이 확인·확정)

1. **업로드**: 문서 분석 → 요구사항 추출·검토 → 기능 명세 초안에서 멈춘다 (`run --until function_designer`). 기능이 연결 안 된 요구사항(누락)은 `FUNC_FIX_ATTEMPTS`번 다시 설계한다.
2. **기능 명세 확인·수정**: `(가정)`·`(확인 필요)` 표시와 확인 질문을 보고 고친다.
3. **'내보내기 → ERD 만들기'**: 화면이 고친 design.json을 보내고 `rerun data_modeler --design`이 그 기능 명세로 ERD·DDL을 만든다. 이 단계는 기능 명세를 다시 설계하지 않는다. 모델·DB 검증 오류는 `MODEL_FIX_ATTEMPTS`·`MAX_DB_ATTEMPTS`번 자동 수정한다.
4. **ERD 수정·저장**: 고친 테이블만 넘기며, 수정만 있으면 LLM 없이 정리·검증한다.
5. **'ERD 최종 확정'**: 최종본 v<ERD>.1을 만들고 추적 검토(코드)·의미 검토(ERD ↔ 기능 명세, LLM 1회)를 한다 (`review`). 결과·토큰 사용량은 최종본에 남고 최종 산출물 화면에 나온다.

| 설정 (`.env`) | 뜻 |
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
| `POST /projects/review` | ERD 최종 확정 → 최종본 v<ERD>.1 (추적·의미 검토) → `202 {job}` |
| `POST /runs` (multipart `file`, `project?`, `title?`, `validate_db?`) | 문서 업로드 → 기능 명세 초안까지 실행(`--defer --until function_designer`) → `202 {job}` |
| `GET /jobs?id=` | 작업 진행 상황 `{status: running·done·failed, message, log, new_version, warnings}` |
| `GET /capabilities` | rerun이 기능 명세 수정본(`--design`·`--spec-md`)을 받는지 |
| `GET /health` | 서버 상태 |

오류 응답은 `{"detail": "..."}`. LLM 작업(몇 분)은 서버 안에서 돌리지 않고 `python -m app …` 하위 프로세스로 띄운다.
작업 기록은 서비스 DB `jobs` 테이블에도 남아 서버를 다시 띄워도 조회된다 (그때 진행 중이던 작업은 실패로 닫는다). 서버는 작업자 1개로 띄운다.

## 구조

```text
compose.yaml · docker/     # (루트) PostgreSQL(db: reqagent·reqagent_validation·reqagent_test) + api
Dockerfile · .dockerignore # (루트) 서버 이미지: backend/ 코드만 넣음
.env · .env.example        # (루트) 설정 하나: LLM·실행 한도·서비스 DB·docker PostgreSQL (.env는 git 제외)
backend/
├── app/
│   ├── main.py            # FastAPI 앱: CORS·오류 처리, /health, 기능별 라우터를 /api/v1 아래에 연결
│   ├── cli.py             # python -m app run·resume·rerun·review·versions·confirm·diff·parse
│   ├── api/v1/            # 기능별 라우터: projects(목록·제목) · artifacts(design·파일) · versions(새 버전) · review(최종 확정)
│   │                      #   · runs(문서 업로드) · jobs(작업 상황) · deps(공통 입력 검사)
│   ├── models/            # 서비스 DB 테이블 (기능별): project · version · artifact · job
│   ├── repositories/      # 서비스 DB 트랜잭션 (기능별): project · version(번호 잠금) · artifact · job
│   ├── services/          # output_store.py: 화면용 산출물 읽기 · jobs/: CLI 하위 프로세스 작업
│   │                      #   (common 공통 실행기 · run 문서 업로드 · rerun 새 버전 · review 최종 확정)
│   ├── agents/
│   │   ├── roles/         # 조정·문서 분석·요구사항 추출·요구사항 검토·기능 명세·데이터 설계·추적 검토·의미 검토
│   │   ├── prompts/       # Agent 시스템 프롬프트 .md (<Agent>/<이름>.md 하나에 프롬프트 하나, load_prompt로 읽음)
│   │   ├── flow/          # LangGraph 그래프(graph)·노드(nodes)·상태(state)·단계 다시 만들기(rerun)
│   │   ├── llm/           # LLM 포트(LLMClient)와 어댑터 (Anthropic·OpenAI·manual), get_llm()·set_llm()
│   │   ├── storage/       # 산출물 저장소 포트(ArtifactStore)와 어댑터(PostgreSQL·로컬 폴더), get_store()·set_store()
│   │   └── tools/         # 문서 파서·청킹·검색·모델 검사·DDL·ERD·내보내기·버전 규칙·DB 검증 (산출물 읽기·쓰기는 storage 포트로)
│   ├── schemas/           # 기능별 입력·출력: document · requirement · function · data_model · review · version(화면 요청 포함)
│   └── core/              # config(.env 설정) · database(서비스 DB 연결·트랜잭션) · exceptions(오류 응답) · guards(요청 값·경로 검사) · logging · security
├── alembic.ini · alembic/ # 서비스 DB 마이그레이션 (DB 주소는 .env의 DATABASE_URL)
├── tests/                 # pytest (가짜 LLM, API 키 없이. PostgreSQL 테스트는 TEST_DATABASE_URL이 있을 때만)
├── evals/                 # 문서 분석·요구사항 추출 골든셋 평가 (python -m evals.eval_document_analyst)
├── standards/             # 회사별 설계 표준 프로필 (run --standard public)
├── data/                  # 샘플 고객 요구사항 문서, 업로드 문서(data/uploads, git 제외)
└── output/                # DATABASE_URL이 없을 때의 산출물 폴더·체크포인트 (git 제외, import-output으로 DB에 가져옴)
```

## 검증

```bash
cd backend
../.venv/bin/pytest -q
```

PostgreSQL 테스트까지: `TEST_DATABASE_URL=postgresql://reqagent:reqagent@localhost:55432/reqagent_test ../.venv/bin/pytest -q`

테스트는 가짜 LLM으로 돈다. 실제 LLM 출력 품질과 PostgreSQL 검증은 따로 실행해 확인한다.
