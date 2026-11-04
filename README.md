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

프론트는 이 저장소 옆 폴더 `../TEAM4_AGENT_FRONT`(별도 프로젝트, [README](../TEAM4_AGENT_FRONT/README.md))에서 `npm install && npm run dev` (http://localhost:5173). 개발 서버가 `/api`를 `http://127.0.0.1:8000`으로 넘긴다.

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
python -m app review 고객요구사항4 v1.1 --dispositions choices.json  # ERD 최종 확정 → 최종본 v1.1.1 (지적 처리·DDL 검증·추적 검토·API 설계·개발 스펙)
python -m app develop 고객요구사항4 --from v1.1.1 [--plan|--logic]   # 최종본 개발 스펙 → 서비스 코드 output/<프로젝트>/app (기본 토큰 0, docs/DEVELOP.md)
python -m app resume <실행 ID> --defer                                  # 확인 질문에서 멈춘 실행 이어 가기
```

`--defer`는 확인 질문을 묻지 않고 추천안으로 가정해 끝까지 만든다 (질문은 '보류'로 남고 화면에서 답한다).

## 화면 흐름 (사람이 확인·확정)

1. **업로드**: 문서 분석 → 요구사항 추출·검토 → 기능 명세 초안에서 멈춘다 (`run --until function_designer`). 기능이 연결 안 된 요구사항(누락)은 `FUNC_FIX_ATTEMPTS`번 다시 설계한다.
2. **기능 명세 확인·수정**: `(가정)`·`(확인 필요)` 표시와 확인 질문을 보고 고친다.
   **'(가정)' 한도·'(확인 필요)' 규칙**: '(가정)' 규칙은 전체 업무 규칙의 `FUNC_ASSUMPTION_LIMIT`(10%) 이하로 둔다. 넘는 만큼 확인 질문으로 정하고(최대 `FUNC_MAX_QUESTIONS`개), 그래도 넘는 규칙은 추천안을 `(확인 필요) …`로 남긴다. 화면 '확인 필요 규칙'에서 '추천안 맞음'을 고르면 내보낼 때 LLM 없이 `(결정) …`이 되고, 표시만 바뀐 기능은 ERD 단계에서 테이블을 다시 설계하지 않는다.
   확정 버튼은 화면 오른쪽 위에만 있고 필수 항목을 다 해결해야 초록색으로 켜진다. 확정하면 취소할 수 없고, 바꾸려면 새 버전을 만든다 ('기능 명세 다시 만들기 (새 버전)', 'ERD 저장하고 새 버전 만들기').
3. **'내보내기 → ERD 만들기'**: 화면이 고친 design.json을 보내고 `rerun data_modeler --design`이 그 기능 명세로 ERD·DDL을 만든다. 이 단계는 기능 명세를 다시 설계하지 않는다. 모델·DB 검증 오류는 `MODEL_FIX_ATTEMPTS`·`MAX_DB_ATTEMPTS`번 자동 수정한다. 이어서 의미 검토(`SEMANTIC_REVIEW_MODE=erd`)가 ERD를 기능 명세와 대조해 빠진 데이터·제약을 찾고, 데이터 모델 Agent가 관련 테이블만 한 번 다시 설계한 뒤 고친 테이블 범위만 다시 검토한다.
4. **ERD 확인·수정·저장**: 남은 의미 검토 지적마다 '반영함·문제 아님·미해결로 둠'을 고른다 (필수). 고친 테이블만 넘기며, 수정만 있으면 LLM 없이 정리·검증하고 의미 검토를 다시 하지 않는다 (지적과 처리는 새 버전에 이어진다, `--dispositions`).
5. **'ERD 최종 확정하기'**: 고른 지적 처리를 남기고, 모델 오류가 남았거나 생성 DDL이 검증 DB에 적용되지 않으면 최종본을 만들지 않는다. 통과하면 최종본 v<ERD>.1을 만들고 추적 검토(코드) → API 설계(`API_DESIGN`, 기능 `API_BATCH_SIZE`개마다 1회 + 아키텍처 1회, 이전 최종본에서 바뀐 기능만)를 한다 (`review`). 의미 검토는 다시 하지 않는다. 최종 산출물 화면은 보기만 한다. 최종본에는 개발 스펙 문서 9개(`API.md`·`openapi.yaml` …, 형식은 [backend/SPEC_FORMAT.md](backend/SPEC_FORMAT.md))가 함께 생긴다. 질문에서 멈추지 않는다. 기술 스택은 기능 명세 초안에서 코드가 낸 필수 질문 `TQ-001`의 답을 쓰고(원문에 언어·프레임워크가 없을 때만 묻는다), 답이 없으면 추천안으로 가정한다. 최종 산출물 화면은 스택을 보여 주기만 한다. API(`POST /projects/tech-stack`)로 최종본의 스택을 확정하면 LLM 없이 같은 최종본의 개발 스펙 9개를 다시 쓰고 design.json(TQ-001 답·`meta.dev_spec`)과 버전 기록(`tech_stack`)에 남긴다. 원문이 정한 스택은 바꾸지 않는다. 결과·토큰 사용량은 최종본에 남고 최종 산출물 화면에 나온다.

버전별로 생기는 파일, 파일마다의 내용, 규칙 표시·질문 ID 읽는 법은 [OUTPUTS.md](OUTPUTS.md).

| 설정 (`.env`) | 뜻 |
| --- | --- |
| `MAX_FIX_ATTEMPTS` | 아래 단계별 값이 비었거나 0일 때 쓰는 자동 수정·재시도 횟수 (문서 분석·요구사항 등) |
| `FUNC_FIX_ATTEMPTS` | 기능 명세: 누락 재설계·실패 재시도 횟수 |
| `MODEL_FIX_ATTEMPTS` | 데이터 설계: 모델 오류 수정·연결 보완·실패 재시도 횟수 |
| `MAX_DB_ATTEMPTS` | DB 검증 실패 후 다시 고치기 횟수 |
| `SEMANTIC_REVIEW_MODE` | `erd`(ERD를 만들 때 데이터 모델만 검토·보완, `.env.example` 값. 예전 값 `final`도 erd) · `auto`(산출물이 바뀔 때마다, 코드 기본값) · `off` |
| `SEMANTIC_REVIEW_ATTEMPTS` | 의미 검토 실패 시 시도 횟수 |
| `FUNC_ASSUMPTION_LIMIT` | 기능 명세 '(가정)' 규칙 한도 (전체 업무 규칙 대비, 기본 0.1). 1이면 묻지 않고 모두 '(가정)' |
| `FUNC_MAX_QUESTIONS` | 기능 명세 확인 질문 상한 (기본 12). 덮지 못한 것은 '(확인 필요)'로 남아 화면에서 확인 |
| `API_DESIGN` | 'ERD 최종 확정' 때 API 설계·개발 스펙을 만들지 (기본 true). 업로드·ERD 단계에는 영향 없음 |
| `API_FIX_ATTEMPTS` · `API_BATCH_SIZE` · `API_EFFORT` | API 검사 오류를 그 기능만 다시 설계하는 횟수(기본 1) · 요청 1건의 기능 수(6) · 생각 수준 |

설정 위치는 `backend/app/core/config.py`의 `Settings` (단계별 한도는 `Settings.fix_attempts`).

## API (`/api/v1`)

| 경로 | 내용 |
| --- | --- |
| `GET /projects` | 프로젝트와 설계 버전 목록 (버전 번호는 문자열 `"4"`, `"4.1"`) |
| `GET /design?project=&version=` | design.json (`data_model`·`trace_links`를 합쳐서) |
| `GET·HEAD /file?project=&version=&name=` | 산출물 원문 (`schema.sql`, `erd.mmd`, `functions.md`, 최종본의 개발 스펙 `API.md`·`openapi.yaml` …) |
| `GET /spec-format` | 개발 스펙 문서의 형식 정의 (`backend/SPEC_FORMAT.md`) |
| `POST /projects/title` | 버전 제목 저장 |
| `GET /tech-stacks` · `POST /projects/tech-stack` | 개발 스펙의 기술 스택 선택지 · 최종본의 기술 스택 확정 → 개발 스펙 9개를 LLM 없이 다시 쓰기 (`{project, version, stack}`) |
| `POST /projects/versions` | 기능 명세 내보내기(`source: spec`) → ERD 새 버전, ERD 저장(`source: erd`, `dispositions`: 의미 검토 지적 처리) → 다음 ERD 버전, `source: answers`(또는 `spec` + `answers`) → 기능 명세 새 버전 (답이 없어도 수정·의견으로). `confirm_rules`('추천안 맞음'으로 확인한 규칙)는 LLM 없이 `(결정)` → `202 {job}` |
| `POST /projects/review` | ERD 최종 확정 (`{project, version, dispositions?}`) → 지적 처리 기록·DDL 검증 → 최종본 v<ERD>.1 (추적 검토·API 설계·개발 스펙) → `202 {job}` |
| `POST /runs` (multipart `file`, `project?`, `title?`, `model?`, `effort?`) | 문서 업로드 → 기능 명세 초안까지 실행(`--defer --until function_designer`) → `202 {job}`. 고른 모델·생각 수준은 그 프로젝트의 ERD·최종 확정까지 쓴다 |
| `GET /llm-options` | 업로드에서 고를 모델(키가 있는 공급자만, 모델별 effort 지원 여부)·생각 수준·`.env` 기본값 |
| `GET /jobs?id=` | 작업 진행 상황 `{status: running·done·failed, message, log, new_version, warnings}` |
| `GET /capabilities` | rerun이 기능 명세 수정본(`--design`·`--spec-md`)·추천안 확인 규칙(`--confirm-rules`)을 받는지 |
| `GET /health` | 서버 상태 |
| `POST /auth/signup` · `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` · `GET /auth/roles` | 회원가입(처음 가입자 = 관리자·팀장, 그 뒤 승인 대기) · 로그인(httpOnly 쿠키 세션) · 로그아웃 · 로그인한 회원과 권한 · 역할 목록. **이 밖의 API는 로그인 필요(401)** |
| `GET /users` · `POST /users/update` · `POST /users/create` · `POST /users/password` | 회원 목록 · 역할·승인·중지·관리자 지정 · 계정 바로 만들기(역할을 정해 승인된 상태) · 비밀번호 초기화 (관리자만, 화면 '관리자 페이지') |
| `GET·POST /drafts` · `POST /drafts/request` | 버전의 수정 초안 함께 쓰기 (`{project, version, state, rev}`, 늦은 저장은 409, 응답에 변경 내역 `log`·승인 요청 `request`) · 파트원의 수정 완료 승인 요청 (파트장의 확정은 이 요청이 있어야, 팀장은 없이) |
| `POST /projects/approval` | 최종본 승인 / 수정 지시 (`{project, version, decision: approve·changes, targets?: [spec, erd], items?: [{stage, target_id, label, note}], comment}`, 팀장만) |
| `GET /projects/directives?project=` · `POST /projects/directives/resolve` | 끝나지 않은 수정 지시와 진행(처리할 버전, 기능 명세 대기, 옛 ERD 잠금) · 지시 항목 처리 반영함·그대로 둠(이유) |
| `GET /notices` · `POST /notices/dismiss` | 나에게 온 알림 (상태: 수정 지시·대기·승인 요청·승인 대기·개발 대기 / 일: 업로드 분석 완료·ERD 초안·개발 초안) · 확인(나에게서만 지움) |
| `GET /develop/plan` · `POST /develop` | 승인 최종본의 최종 개발하기: 예상(화면·파일, 틀 없는 층 경고, AI 업무 기능 구현 예상 토큰) · 코드(+`build`·`model`·`effort`면 AI 구현) + 예시 데이터 화면 + Docker 자동 확인 → `202 {job}` (개발 파트장·팀장) |
| `GET /preview/<프로젝트>/` (`mock/` · `status` · `start` · `stop` · `accounts`) | 최종 개발물 실행: 안내 페이지 → 앱 자체 docker-compose로 독립 실행 → 앱 주소 `http://<서버>:<포트>`(`PREVIEW_BIND`, 기본 0.0.0.0). 10분 동안 요청이 없으면 끔, 동시 3개. `accounts`는 admin·user 비밀번호(개발 파트·팀장) |
| `GET /projects/stack` | 버전의 기술 스택 상태: 원문·확정(기능 명세 TQ-001)·미정, 개발 틀이 없는 층 경고 |
| `GET /develop/files` · `GET·PUT /develop/file` | 최종 개발물 코드 파일 구성(빈 업무 로직·사람이 고친 파일 표시) · 내용 보기 · 고쳐 저장(개발 파트원·파트장·팀장) |
| `GET /preview/<프로젝트>/terminal` | 실행 미리보기 터미널: 서버가 실행한 명령·출력 + 앱 서버 로그 |
| `GET·POST /develop/git` · `POST /develop/git/push` | 프로젝트별 화면·백엔드 저장소 설정(토큰 암호화) · 서버 코드 폴더를 두 저장소에 커밋·push → `202 {job}` |

역할별 권한(서버가 검사, `app/core/auth.py`): 업로드·기능 명세 수정 = 기획 파트원 이상 · 기능 명세 확정(내보내기) = 기획 파트장 · ERD 수정·저장 = 데이터 파트원 이상 · ERD 최종 확정 = 데이터 파트장 · 최종 승인·수정 지시·기술 스택 = 팀장(모든 단계 가능). 회원 테이블은 `alembic upgrade head`로 만든다 (서비스 DB가 없으면 `OUTPUT_DIR/service.sqlite`).

**[임시] 시연용 계정**: 계정은 git이 아니라 각 PC의 DB에 있다. 그래서 회원이 한 명도 없는 DB면 서버가 시작할 때 계정 7개를 만든다(`admin@team4.local` 관리자·팀장, `plan.head`·`plan1`·`plan2`(기획), `data.head`·`data1`·`data2`(데이터) `@team4.local`, 비밀번호 `Team4!2026`, 목록은 `backend/app/services/users.py` `TEST_ACCOUNTS`). 이미 회원이 있으면 만들지 않는다. 없는 계정만 직접 만들기: `python -m app seed-accounts`. 끄기: `.env` `SEED_TEST_ACCOUNTS=false`. 실제로 쓸 때는 끄고 지운다.

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
