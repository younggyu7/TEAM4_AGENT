# 요구사항 → 설계 산출물 Agent (백엔드)

고객 요구사항 문서(웹 업로드는 PDF·DOCX·HWPX, 스캔본·이미지 제외)를 받아 요구사항 정의서·기능 명세서·ERD·DDL·추적표·검토 보고서를 만든다.
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

1. **업로드**: 문서 분석 → 요구사항 추출·검토 → 기능 명세 초안에서 멈춘다 (`run --until function_designer`). 요구사항 추출 뒤에는 원문 목록 항목 중 어느 요구사항에도 담기지 않은 것을 코드로 찾아 그 단위만 짚어 한 번 더 추출한다(`REQ_FILL_GAPS`, 항목이 많은 단위에서 뒤쪽 항목이 빠지는 것을 막음). 기능이 연결 안 된 요구사항(누락)은 `FUNC_FIX_ATTEMPTS`번 다시 설계한다.
2. **기능 명세 확인·수정**: `(가정)`·`(확인 필요)` 표시와 확인 질문을 보고 고친다.
   **추천안 자동 적용(`AUTO_ASSUME`, 기본 켬)**: 답에 따라 기능 범위·데이터 구조·권한·금액·법적 의무·삭제/보존 결과가 크게 달라지는 것만 묻고(기능 명세 질문 최대 `FUNC_MAX_QUESTIONS`개, 일부만 다시 설계하면 3개), 추천안이 있는 나머지는 추천안으로 정해 `(가정)`으로 둔다(한도 없음, 명세서·화면 '가정'에 보이고 필요하면 고친다). 요구사항 단계도 분류가 애매한 요구사항은 묻지 않고 검토 Agent가 판단한 분류를 결정으로 남긴다(RC 질문 없음). 요구사항 Agent의 질문(Q)·원문에 없는 요구사항 포함 여부(RC)·요구사항 충돌(CF)은 그대로 묻는다 (원문끼리 다른 이관 범위처럼 막지 않는 질문도 고객이 정할 내용이라). 끄면(`AUTO_ASSUME=false`) 아래 한도 방식이다.
   **고르는 질문만(`ASK_CHOICES_ONLY`, 기본 켬)**: 기능 명세 확인 질문 중 선택지가 '예 / 아니오'·'둔다 / 두지 않는다'처럼 부정만 다른 것은 만들지 않고 추천안대로 확정한다(고객이 서로 다른 안 중에서 골라야 하는 것만 질문). 질문 없이 남은 영향이 작은 `(확인 필요)`도 영역 묶음 질문('추천안대로 확정할까요?') 대신 추천안대로 확정한다. 확정한 규칙은 표시 없이 들어가고 기능 명세서 '고객 확인'에 목록으로 남는다.
   **'(가정)' 한도·'(확인 필요)' 규칙 (`AUTO_ASSUME=false`)**: '(가정)' 규칙은 전체 업무 규칙의 `FUNC_ASSUMPTION_LIMIT`(10%) 이하로 둔다. 넘는 만큼 확인 질문으로 정하고(최대 `FUNC_MAX_QUESTIONS`개), 그래도 넘는 규칙은 추천안을 `(확인 필요) …`로 남긴다. 화면 '확인 필요 규칙'에서 '추천안 맞음'을 고르면 내보낼 때 LLM 없이 `(결정) …`이 되고, 표시만 바뀐 기능은 ERD 단계에서 테이블을 다시 설계하지 않는다.
   확정 버튼은 화면 오른쪽 위에만 있고 필수 항목을 다 해결해야 초록색으로 켜진다. 확정하면 취소할 수 없고, 바꾸려면 새 버전을 만든다 ('기능 명세 다시 만들기 (새 버전)', 'ERD 저장하고 새 버전 만들기').
3. **'내보내기 → ERD 만들기'**: 화면이 고친 design.json을 보내고 `rerun data_modeler --design`이 그 기능 명세로 ERD·DDL을 만든다. 이 단계는 기능 명세를 다시 설계하지 않는다. 모델·DB 검증 오류는 `MODEL_FIX_ATTEMPTS`·`MAX_DB_ATTEMPTS`번 자동 수정한다. 이어서 의미 검토(`SEMANTIC_REVIEW_MODE=erd`)가 ERD를 기능 명세와 대조해 빠진 데이터·제약을 찾고, 데이터 모델 Agent가 관련 테이블만 한 번 다시 설계한 뒤 고친 테이블 범위만 다시 검토한다.
4. **ERD 확인·수정·저장**: 남은 의미 검토 지적마다 '반영함·문제 아님·미해결로 둠'을 고른다 (필수). 고친 테이블만 넘기며, 수정만 있으면 LLM 없이 정리·검증하고 의미 검토를 다시 하지 않는다 (지적과 처리는 새 버전에 이어진다, `--dispositions`).
5. **'ERD 최종 확정하기'**: 고른 지적 처리를 남기고, 모델 오류가 남았거나 생성 DDL이 검증 DB에 적용되지 않으면 최종본을 만들지 않는다. 통과하면 최종본 v<ERD>.1을 만들고 추적 검토(코드) → API 설계(`API_DESIGN`, 기능 `API_BATCH_SIZE`개마다 1회 + 아키텍처 1회, 이전 최종본에서 바뀐 기능만)를 한다 (`review`). 의미 검토는 다시 하지 않는다. 최종 산출물 화면은 보기만 한다. 최종본에는 개발 스펙 문서 9개(`API.md`·`openapi.yaml` …, 형식은 [backend/SPEC_FORMAT.md](backend/SPEC_FORMAT.md))가 함께 생긴다. 질문에서 멈추지 않는다. 기술 스택은 기능 명세 초안에서 코드가 낸 필수 질문 `TQ-001`의 답을 쓰고(원문에 언어·프레임워크가 없을 때만 묻는다), 답이 없으면 추천안으로 가정한다. 최종 산출물 화면은 스택을 보여 주기만 한다. API(`POST /projects/tech-stack`)로 최종본의 스택을 확정하면 LLM 없이 같은 최종본의 개발 스펙 9개를 다시 쓰고 design.json(TQ-001 답·`meta.dev_spec`)과 버전 기록(`tech_stack`)에 남긴다. 원문이 정한 스택은 바꾸지 않는다. 결과·토큰 사용량은 최종본에 남고 최종 산출물 화면에 나온다.

버전별로 생기는 파일, 파일마다의 내용, 규칙 표시·질문 ID 읽는 법은 [OUTPUTS.md](OUTPUTS.md).

| 설정 (`.env`) | 뜻 |
| --- | --- |
| `REQ_FILL_GAPS` · `REQ_FILL_MIN_ITEMS` | 요구사항 빠진 항목 채우기 (기본 true · 2): 담기지 않은 원문 목록 항목이 이만큼 이상인 단위만 한 번 더 추출 (담김 기준 `REQ_REVIEW_COVERAGE`) |
| `MAX_FIX_ATTEMPTS` | 아래 단계별 값이 비었거나 0일 때 쓰는 자동 수정·재시도 횟수 (문서 분석·요구사항 등) |
| `FUNC_FIX_ATTEMPTS` | 기능 명세: 누락 재설계·실패 재시도 횟수 |
| `MODEL_FIX_ATTEMPTS` | 데이터 설계: 모델 오류 수정·연결 보완·실패 재시도 횟수 |
| `MAX_DB_ATTEMPTS` | DB 검증 실패 후 다시 고치기 횟수 |
| `SEMANTIC_REVIEW_MODE` | `erd`(ERD를 만들 때 데이터 모델만 검토·보완, `.env.example` 값. 예전 값 `final`도 erd) · `auto`(산출물이 바뀔 때마다, 코드 기본값) · `off` |
| `SEMANTIC_REVIEW_ATTEMPTS` | 의미 검토 실패 시 시도 횟수 |
| `ASK_CHOICES_ONLY` | 고르는 질문만 묻기 (기본 true): 예/아니오 확인 질문·영역 묶음 질문 없이 추천안대로 확정 (기능 명세서에 목록). false면 영역 묶음 질문(`FUNC_RULE_BUNDLE`) |
| `AUTO_ASSUME` | 추천안 자동 적용 (기본 true): 꼭 확인할 것만 묻고 추천안이 있는 나머지는 추천안 '(가정)'으로. false면 '(가정)' 한도 방식 |
| `FUNC_ASSUMPTION_LIMIT` | 기능 명세 '(가정)' 규칙 한도 (`AUTO_ASSUME=false`일 때만, 전체 업무 규칙 대비, 기본 0.1). 1이면 묻지 않고 모두 '(가정)' |
| `FUNC_MAX_QUESTIONS` | 기능 명세 확인 질문 상한 (기본 12). `AUTO_ASSUME`이면 꼭 필요한 것만 이 수까지, 한도 방식이면 덮지 못한 것은 '(확인 필요)'로 남아 화면에서 확인 |
| `API_DESIGN` | 'ERD 최종 확정' 때 API 설계·개발 스펙을 만들지 (기본 true). 업로드·ERD 단계에는 영향 없음 |
| `API_FIX_ATTEMPTS` · `API_BATCH_SIZE` · `API_EFFORT` | API 검사 오류를 그 기능만 다시 설계하는 횟수(기본 1) · 요청 1건의 기능 수(6) · 생각 수준 |

설정 위치는 `backend/app/core/config.py`의 `Settings` (단계별 한도는 `Settings.fix_attempts`).

## API (`/api/v1`)

| 경로 | 내용 |
| --- | --- |
| `GET /projects` | 프로젝트와 설계 버전 목록 (버전 번호는 문자열 `"4"`, `"4.1"`). 내가 담당자인 프로젝트만 (PM을 고른 적이 없는 프로젝트는 PM 모두) |
| `GET /design?project=&version=` | design.json (`data_model`·`trace_links`를 합쳐서) |
| `GET·HEAD /file?project=&version=&name=` | 산출물 원문 (`schema.sql`, `erd.mmd`, `functions.md`, 최종본의 개발 스펙 `API.md`·`openapi.yaml` …) |
| `GET /spec-format` | 개발 스펙 문서의 형식 정의 (`backend/SPEC_FORMAT.md`) |
| `POST /projects/title` | 버전 제목 저장 |
| `POST /projects/name` | 프로젝트 이름 바꾸기 (`{project, name}`, PM만, 80자, 다른 프로젝트와 같은 이름은 409, 비우면 처음 이름). 목록·Slack 메시지·다음 최종 개발하기의 회사 이름에 보이는 이름만 바뀌고 프로젝트 ID(주소·코드 폴더·Slack 채널 이름)는 그대로. 프로젝트 파일 `project.json`의 `name` |
| `DELETE /projects?project=&confirm=` | 프로젝트 삭제 (PM만, `confirm`에 프로젝트 이름 그대로, 작업 중이면 409). 버전·산출물·프로젝트 파일·코드 폴더(git)·미리보기 컨테이너와 DB·작업·담당자·Slack 기록을 지움 (Slack 채널·원격 저장소는 그대로, 되돌릴 수 없음) |
| `POST /projects/dev-spec/fill` | 업무 동작 분석 전에 만든 예전 최종본의 빠진 개발 스펙 채우기: 업무 동작 설계 LLM 1회 → 개발 스펙 다시 쓰기 (`{project, version}`, PM·개발자, 이미 채웠으면 400, LLM 실패면 502로 파일을 그대로 둠) |
| `GET /tech-stacks` · `POST /projects/tech-stack` | 개발 스펙의 기술 스택 선택지 · 최종본의 기술 스택 확정 → 개발 스펙 9개를 LLM 없이 다시 쓰기 (`{project, version, stack}`) |
| `POST /projects/versions` | 기능 명세 내보내기(`source: spec`) → ERD 새 버전, ERD 저장(`source: erd`, `dispositions`: 의미 검토 지적 처리) → 다음 ERD 버전, `source: answers`(또는 `spec` + `answers`) → 기능 명세 새 버전 (답이 없어도 수정·의견으로). `confirm_rules`('추천안 맞음'으로 확인한 규칙)는 LLM 없이 `(결정)` → `202 {job}` |
| `POST /projects/review` | ERD 최종 확정 (`{project, version, dispositions?}`) → 지적 처리 기록·DDL 검증 → 최종본 v<ERD>.1 (추적 검토·API 설계·개발 스펙) → `202 {job}` |
| `POST /runs` (multipart `file`, `project?`, `title?`, `model?`, `effort?`, `members?`) | 문서 업로드 → 새 프로젝트면 담당자 = `members`(회원 ID, 쉼표) + 올린 사람 (같은 이름의 남의 프로젝트면 409) → (Slack이 켜져 있으면 프로젝트 비공개 채널을 만들고 담당자 초대, 실패해도 계속) → 기능 명세 초안까지 실행(`--defer --until function_designer`) → `202 {job, slack: {team, channel_name, invited, failed, existing} 또는 {error} 또는 null(Slack 꺼짐)}`. 고른 모델·생각 수준은 그 프로젝트의 ERD·최종 확정까지 쓴다 |
| `GET /llm-options` | 업로드에서 고를 모델(키가 있는 공급자만, 모델별 effort 지원 여부)·생각 수준·`.env` 기본값 |
| `GET /jobs?id=` | 작업 진행 상황 `{status: running·done·failed, message, log, new_version, warnings}` (담당자가 아닌 프로젝트의 작업은 404) |
| `GET /jobs/running?project=` | 그 프로젝트에서 진행 중인 작업 `{job}` (없으면 `null`). 로그아웃·새로고침 뒤 화면이 진행 창을 다시 띄운다 |
| `GET /capabilities` | rerun이 기능 명세 수정본(`--design`·`--spec-md`)·추천안 확인 규칙(`--confirm-rules`)을 받는지 |
| `GET /health` | 서버 상태 |
| `POST /auth/signup` · `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` · `GET /auth/roles` | 회원가입(처음 가입자 = 관리자·PM, 그 뒤 승인 대기, Slack 멤버 ID 선택) · 로그인(httpOnly 쿠키 세션) · 로그아웃 · 로그인한 회원과 권한 · 역할 목록. **이 밖의 API는 로그인 필요(401)** |
| `GET /users` · `POST /users/update` · `POST /users/create` · `POST /users/password` | 회원 목록 · 역할·승인·중지·관리자 지정·Slack 멤버 ID(`slack_id`) · 계정 바로 만들기(역할을 정해 승인된 상태, `slack_id?`) · 비밀번호 초기화 (관리자만, 화면 '관리자 페이지') |
| `GET /members` | 승인된 회원 `{id, name, role, role_label, has_slack}` + `slack_enabled` (업로드·프로젝트 화면에서 담당자 고르기, 로그인한 회원 누구나) |
| `GET·POST /drafts` · `POST /drafts/request` · `POST /drafts/request/reject` | 버전의 수정 초안 함께 쓰기 (`{project, version, state, rev}`, 늦은 저장은 409, 응답에 변경 내역 `log`·승인 요청 `request`·마지막 반려 `rejection`) · 개발자의 기능 명세 수정 완료 승인 요청 (PM은 요청 없이도 확정, ERD는 승인 요청 없이 PM·개발자가 바로 확정하므로 400) · 승인 요청 반려 (`{project, version, reason}`, PM, 사유 필수, 요청이 없으면 404) |
| `POST /projects/approval` | 최종본 승인 / 수정 지시 (`{project, version, decision: approve·changes, targets?: [spec, erd], items?: [{stage, target_id, label, note}], comment}`, PM만) |
| `GET /projects/directives?project=` · `POST /projects/directives/resolve` | 끝나지 않은 수정 지시와 진행(처리할 버전, 기능 명세 대기, 옛 ERD 잠금) · 지시 항목 처리 반영함·그대로 둠(이유) |
| `GET /notices` · `POST /notices/dismiss` | 나에게 온 알림 (담당자인 프로젝트만. 상태: 수정 지시·대기·승인 요청·승인 대기·개발 대기, 회원별 반려 `kind: rejected` — 내가 요청했거나 고친 초안 / 일: 업로드 분석 완료·기능 명세 다시 만듦(모든 회원, `spec_redone` = AI로 답 반영·다시 만들기 결과)·ERD 초안(개발자)·개발 초안(개발자)) · 확인(나에게서만 지움, Slack 전송과 무관). 내가 한 일로 생긴 일 알림(릴리스·합치기·작업 배정·작업 완료 등)은 나에게 보이지 않고 Slack에서도 나를 부르지 않음 (팀원에게는 그대로) |
| `GET /develop/plan` · `POST /develop` | 승인 최종본의 최종 개발하기: 예상(화면·파일, 틀 없는 층 경고, AI 업무 기능 구현 예상 토큰) · 코드(+`build`·`model`·`effort`면 AI 구현, gen에 커밋 — 처음이면 dev도 같은 커밋, 다시 개발이면 gen → dev 합치기 요청) + 예시 데이터 화면 + Docker 자동 확인(처음만) → `202 {job}` (개발자만) |
| `POST /develop/check` | 자동 확인 다시 하기: 지금 dev를 Docker로 켜고 admin 로그인·조회 API 검사, 최종본 기록의 결과 갱신(작업, 개발자, LLM 없음) |
| `GET /preview/<프로젝트>/` (`mock/` · `status` · `start` · `stop` · `accounts`) | 최종 개발물 실행: 안내 페이지 → 앱 자체 docker-compose로 독립 실행 → 앱 주소 `http://<서버>:<포트>`(`PREVIEW_BIND`, 기본 0.0.0.0). 10분 동안 요청이 없으면 끔, 동시 3개. `accounts`는 admin·user 비밀번호(PM·개발자) |
| `GET /projects/stack` | 버전의 기술 스택 상태: 원문·확정(기능 명세 TQ-001)·미정, 개발 틀이 없는 층 경고 |
| `GET /develop/files` · `GET·PUT /develop/file` | 최종 개발물 코드 파일 구성(`branch`: main 또는 브랜치 작업 폴더, 빈 업무 로직·사람이 고친 파일·커밋 안 한 변경 표시) · 내용 보기 · 브랜치에서 고쳐 저장(main 거부, 개발자 `repo_write`) |
| `GET /preview/<프로젝트>/terminal` | 실행 미리보기 터미널: 서버가 실행한 명령·출력 + 앱 서버 로그 |
| `GET·POST /develop/git` · `POST /develop/git/push` | 원격 하나 `{url, token}`(토큰 암호화, 화면엔 `has_token`·받아 둔 원격 기준 dev·main 앞섬·뒤처짐 `targets`) · 고른 브랜치 `{target: dev·main}`를 같은 이름으로 push + `final-v*`·`release-*` 태그(원격에 서버에 없는 커밋이 있으면 거부, 첫 릴리스 전 main은 400) → `202 {job}` (개발자). dev를 올리면 생성 앱의 `.github/workflows/deploy-dev.yml`이 테스트 서버에 배포 |
| `GET /develop/repo` (`/log` · `/commit` · `/diff`) | 코드 저장소(`gen` = AI 생성 결과, 작업 브랜치 `dev` = 통합·테스트, `main` = 고객 납품·첫 릴리스 때 생김, `released`·`pending_release`): 브랜치(dev 기준 앞섬·뒤처짐·커밋 안 한 변경·열린 요청)·합치기 요청 · 모든 브랜치 커밋(이력 그래프, `ai`=req-agent) · 커밋 하나의 diff · dev...브랜치 차이(`branch=main`이면 릴리스할 차이, `branch=gen`이면 AI 재생성 차이) |
| `POST·DELETE /develop/repo/branch` · `POST /develop/repo/commit` · `/update-branch` · `/request` | 브랜치(+작업 폴더, dev에서) 만들기·지우기 · 저장하기(작성자=로그인 회원, gen·dev·main 거부) · dev 가져오기(충돌이면 409 `conflicts`) · 합치기 요청(→ 개발자 알림) (개발자) |
| `POST /develop/repo/merge` · `/reject` · `/release` · `/fetch` | dev에 합치기(`--no-ff`, 충돌이면 되돌리고 409 `conflicts`) · 반려(사유 필수) — 요청한 회원에게만 알림, **자기 요청은 403(개발자 담당자가 한 명이면 허용·'본인 승인' 기록)** · 릴리스(dev → main 앞으로 감기, 첫 릴리스면 main 생성 + 태그 `release-<날짜>-<n>`) · 원격 확인(fetch) (개발자) |
| `GET·PUT /develop/layout` · `POST /develop/layout/suggest` | 생성 앱 화면 구성 `frontend/src/generated/layout.json`(업무 영역, 메뉴 이름·숨김, 업무 이름·숨김, 홈 칸) 보기 · 저장(dev 커밋, 진행 중인 영역의 메뉴 이동 400, 개발자) · 영역 다시 묶기 제안(`mode` rules=규칙 / ai=LLM 한 번, 저장 안 함, AI 실패 502) |
| `GET /develop/work` · `/work/item` | 작업 묶음(공통 화면 틀·공통 서버·DB + 업무 영역마다 화면·서버)·영역 목록 `areas`·상태·담당자·빈 로직·AI 수와 진행 현황 · 작업 하나 + 작업 설명(TASK.md: 서버는 기능별 개발 스펙 절, 화면은 화면 설계 지시) |
| `POST /develop/work/assign` · `/assign-many` · `/submit` · `/approve` · `/reject` | 배정(작업 브랜치 `task/<묶음>` 생성·알림, 개발자) · 일괄 배정(`keys[]`, 묶음별 결과, 알림 한 번) · 제출(담당자, 묶음 밖 파일이면 400) · 승인(dev에 합침)·반려 (다른 개발자, 개발자 1명이면 본인) |
| `GET /develop/work/mine` · `/archive` · `POST /develop/work/upload` · `GET /develop/work/review` | VS Code 확장: 내 작업·검토할 작업(모든 프로젝트) · 작업 코드 받기(먼저 최신 dev를 작업 브랜치에 넣고(충돌이면 `sync.conflicts`) 브랜치 파일 base64 + `.task/TASK.md`·`files.txt`·`RULES.md`) · 바뀐 파일 올리기(받은 커밋이 다르면 409, `submit`이면 제출까지) · 검토 비교(dev 쪽·작업 쪽 내용) |
| `GET /version` · `GET·POST·DELETE /auth/tokens` | API 버전 `{api: 1}`(확장 호환 확인) · 내 개인 접속 토큰(만들 때만 원문, 서버엔 해시) — `Authorization: Bearer t4_…`로 쿠키 없이 API 사용 |
| `GET·POST /projects/members` | 프로젝트 담당자 `{members: [{id, name, role, role_label, has_slack, in_channel, always}], slack: {team, channel_name, members} 또는 null, slack_enabled}` · 담당자 바꾸기 (`{project, members: [회원 ID]}`, PM만, 결과 `result: {added, removed, failed}`. Slack 채널이 있으면 채널도 맞춤, Slack이 없어도 됨) |

역할은 **PM**과 **개발자** 둘이다 (기획·데이터·개발 파트로 나누지 않는다). 역할별 권한(서버가 검사, `app/core/auth.py`): 업로드·기능 명세 수정·ERD 수정·저장·ERD 최종 확정·실행 끄기·앱 시연 계정 = PM·개발자 · 최종 승인·수정 지시·기술 스택 = PM·개발자 · 기능 명세 확정(내보내기)·승인 요청 반려·담당자 바꾸기·프로젝트 이름·삭제 = PM · 최종 개발하기·브랜치·코드 고치기·저장하기·합치기 요청·합치기·반려·원격 올리기 = 개발자. 개발자는 기능 명세를 고친 뒤 PM에게 '수정 완료 승인 요청'을 보내고, ERD는 승인 요청 없이 바로 확정한다. 예전 역할(팀장 → PM, 기획·데이터·개발 파트장·파트원 → 개발자)은 마이그레이션 `f3b5d7e9a1c2`가 바꾸고, 기록에 남은 예전 역할 값도 같은 규칙으로 읽는다. 회원 테이블은 `alembic upgrade head`로 만든다 (서비스 DB가 없으면 `OUTPUT_DIR/service.sqlite`).

**프로젝트 담당자** (`backend/app/services/project_access.py`, `project_members` 테이블): 담당자로 고른 회원만 그 프로젝트를 본다. **PM도 고른 PM만 보고, 담당자에는 PM이 최소 한 명 있어야 한다.**

- 정하기: 업로드할 때 홈 '새 문서 분석'의 '담당자'에서 고른 회원 + 올린 사람. PM이 하나도 없으면 '올리고 분석 시작'이 꺼지고 'PM 최소 한 명은 필요합니다'가 뜬다(서버도 새 프로젝트면 400). 같은 프로젝트에 다시 올리면 담당자는 그대로다.
- 바꾸기: 프로젝트 화면 오른쪽 '담당자' 카드에서 PM만 '바꾸기'(`POST /projects/members`, PM이 없으면 저장이 꺼지고 서버도 400). 뺀 회원은 바로 못 본다.
- 담당자가 아니면 없는 프로젝트처럼: 프로젝트 목록·'나에게 온 알림'에서 빠지고, 그 프로젝트를 받는 API(`project=`·작업 조회 등)는 404, Slack 알림도 받지 않는다. 같은 이름으로 업로드하면 409.
- PM을 고른 적이 없는 프로젝트(이 규칙 전에 만든 프로젝트, CLI로 만든 프로젝트, 고른 PM이 모두 사용 중지됨)는 PM 모두가 보고 담당자로 친다. PM이 담당자를 다시 저장하면 고른 PM만 남는다. 이 기능 전에 만든 프로젝트는 마이그레이션이 Slack 채널 담당자를 그대로 옮겨 왔다.
- 서버 검사: `api/v1/deps.py`의 `current_user`가 요청한 회원을 남기고 `_project`·`_file`이 담당자인지 본다. 테스트는 `@pytest.mark.assignees`가 붙은 것만 이 규칙을 그대로 쓴다(`tests/test_project_access.py`·`test_slack.py`, 나머지는 `conftest`가 모두에게 보이게 둠).

**Slack 알림** (`backend/app/services/slack.py`): 팀 Slack 워크스페이스 하나에 프로젝트마다 비공개 채널을 자동으로 만들어 그 채널에 알림을 올린다.

- 준비: 이 웹의 회원이 모두 들어 있는 워크스페이스에 Slack 앱을 만들고(<https://api.slack.com/apps>, Bot Token Scopes `chat:write`·`groups:write`) 설치 → 'Bot User OAuth Token'(`xoxb-…`)을 루트 `.env` `SLACK_BOT_TOKEN`에 넣고 서버를 다시 시작한다. 비우면 Slack 알림을 끈다.
- 채널: 업로드하면 서버가 비공개 채널 `#team4-<프로젝트>`를 만들고 그 프로젝트의 담당자를 초대한다(이름이 있으면 `-2`, 한글 이름을 받지 않는 워크스페이스면 영문 이름). 채널이 없는 프로젝트는 서버가 알아서 만들고 담당자를 초대한다. Slack이 실패해도 업로드·분석은 그대로 하고, 서버가 `RETRY_SECONDS`(10분) 뒤에 다시 만든다. 채널·초대한 회원은 `slack_projects` 테이블.
- 채널 멤버 = 담당자 중 채널에 실제로 초대된 회원. Slack 멤버 ID가 없거나 워크스페이스에 없는 ID면 초대하지 못하고 업로드 결과·'담당자' 카드에 보여 준다. 담당자가 아닌 회원은 초대하지도 멘션하지도 않는다.
- 멘션: 알림마다 그 알림을 받는 담당자(역할별 규칙: 업로드 분석 완료·기능 명세 다시 만듦 → 모든 담당자, ERD 초안·개발 대기·개발 초안·합치기 요청·수정 지시 → 개발자, 기능 명세 승인 요청·최종 승인 대기 → PM 등) 중 채널에 있는 회원을 멘션한다. 받을 담당자가 아무도 채널에 없으면 알림이 묻히지 않게 PM을 대신 멘션하고 `⚠️ 알림 받을 개발자(이름)이(가) 이 채널에 없어 멘션하지 못했습니다` 안내를 붙인다. 승인·반려 결과도 부를 사람이 채널에 없으면 같은 안내를 붙인다.
- 담당자 바꾸기: 중간에 담당자가 바뀌면 프로젝트 화면 오른쪽 '담당자' 카드에서 PM이 '바꾸기'로 다시 고른다(`POST /projects/members`). 더한 회원은 채널에 초대하고(전에 초대하지 못한 담당자도 다시 초대), 뺀 회원은 채널에서 내보내고, 채널에 `[담당자 변경]`을 올린다. 그 뒤 멘션은 새 담당자 기준이다.
- 회원 Slack 멤버 ID(`U…`, Slack 프로필 → ⋮ → '멤버 ID 복사')는 회원가입·관리자 페이지에서 넣는다. [임시] 시연용 계정 8개에는 팀 시연 워크스페이스의 실제 멤버 ID가 들어 있다 (예전 시연 계정의 ID를 그대로 옮김: PM1·PM2·개발자1·2 / 개발자3·4 / 개발자5·6이 한 사람씩). 계정을 만들 때만 들어가고, 이미 있는 계정은 바꾸지 않는다 (관리자 페이지에서 바꾼다).

| 언제 | 채널에 올리는 메시지 (멘션) |
| --- | --- |
| 홈 '나에게 온 알림'에 새 항목 (승인 요청·최종 승인 대기·수정 지시·수정 대기) | `[승인 요청]` 등 + 화면 링크, 알림마다 하나씩 (그 알림을 받는 담당자). 같은 알림은 한 번만 (`slack_notifications`) |
| PM이 기능 명세 확정('내보내기 → ERD 만들기') | `[승인]` (요청한 개발자) |
| PM이 반려 (`POST /drafts/request/reject`) | `[반려]` + 사유 (요청한 개발자 + 그 초안을 고친 개발자 담당자). 홈 '나에게 온 알림'에도 '반려'가 남는다(Slack에는 다시 안 올림) |
| 최종 승인 (PM·개발자) | `[최종 승인]` (그 최종본·바탕 ERD·기능 명세를 만들거나 고친 회원) |
| 수정 지시 (PM·개발자) | `[반려 · 수정 지시]` + 지시 내용 (위 회원 + 지시를 처리할 개발자, 결정한 사람 자신 빼고). 같은 지시의 `[수정 지시]` 알림은 따로 올리지 않음 |
| 담당자 바꾸기 | `[담당자 변경]` 추가·제외 (새 담당자) |

알림은 승인 요청·결정·작업 완료 때 바로, 그 밖에는 `SLACK_SYNC_SECONDS`(30)초마다 확인한다(채널이 없는 프로젝트도 이때 만든다). 링크 주소는 `APP_BASE_URL`(기본 `http://localhost:5173`). Slack이 실패해도 화면 요청은 그대로 되고 서버 로그에만 남는다.

**[임시] 시연용 계정**: 계정은 git이 아니라 각 PC의 DB에 있다. 그래서 서버가 시작할 때 없는 시연 계정 8개를 만든다(`pm1@team4.local` 관리자·PM, `pm2` PM, `developer1`~`developer6` 개발자 `@team4.local`, 비밀번호 `Team4!2026`, 목록은 `backend/app/services/users.py` `TEST_ACCOUNTS`). 이미 있는 계정은 그대로 둔다. 역할을 PM·개발자로 나누기 전의 시연 계정 10개(`admin`·`plan.head`·`plan1`·`plan2`·`data.head`·`data1`·`data2`·`dev.head`·`dev1`·`dev2` `@team4.local`)는 새 관리자 `pm1`이 있으면 사용 중지한다(지우지 않아 기록은 남음, 관리자 페이지에서 다시 켤 수 있음). 그 계정이 담당자였던 프로젝트는 PM이 '담당자 → 바꾸기'에서 새 계정으로 다시 고른다. 없는 계정만 직접 만들기: `python -m app seed-accounts`. 끄기: `.env` `SEED_TEST_ACCOUNTS=false`. 실제로 쓸 때는 끄고 지운다.

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
│   │                      #   · runs(문서 업로드) · jobs(작업 상황) · develop·repo(최종 개발물·저장소) · work(화면 구성·작업 묶음·확장 API) · deps(공통 입력 검사)
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
