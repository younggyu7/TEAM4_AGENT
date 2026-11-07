# 개발 단계: 개발 스펙으로 서비스 코드 만들기 (`develop`)

'ERD 최종 확정'으로 만든 최종본(개발 스펙 10개가 있는 버전, 예: `v1.1.1`)에서 웹 서비스 프로젝트를 만든다. **기본 실행은 LLM을 부르지 않아 토큰이 0이다.**
고객에게 화면 단위로 보여 주는 데 서버·DB가 필요 없다.

**화면에서** (검토 중): 기술 스택은 기능 명세의 확인 질문 TQ-001에서 화면·서버·DB를 하나씩 고른다(원문에 있으면 원문, 개발 틀이 없으면 경고). 최종 승인한 최종본의 산출물 보기 → '최종 개발하기'(개발자): 앱 틀(로그인 admin·user, 회사 이름=프로젝트 이름의 포털, 기본 동작, DB, Docker 파일)은 코드가, '업무 기능을 AI로 구현'(기본 켬, `--build`)을 고르면 모델·effort·예상 토큰·비용을 보고 AI가 기능별 서버 API·화면을 짠다(검사·컴파일 실패 기능은 코드 틀). 다 만들면 Docker로 켜 admin 로그인 + 모든 조회 API를 불러 자동 확인한다. 단계 표시줄 '최종 개발물 미리보기'는 1단계 개발(구현 방식·고객 답 대기·자동 확인, 파일 구성·코드 보기·고쳐 저장) · 2단계 실행(앱 자체 docker-compose로 독립 실행, 팀원 PC에서 `http://<서버>:<포트>`, 계정 비밀번호, 터미널, 10분 동안 요청이 없으면 끔) · 3단계 Git으로 나뉜다. 개발 틀(층마다 고름): 화면 React(`templates/app/frontend`)·Next.js(`templates/frontend-nextjs`, 같은 화면 부품을 `ui/`에), 서버 Spring Boot(`templates/app/backend`)·FastAPI(`templates/backend-fastapi` + `tools/scaffold_fastapi.py`)·NestJS(`templates/backend-nestjs` + `tools/scaffold_nestjs.py`), DB PostgreSQL·MySQL(`ddl.py`·`sample_data.py` 방언). AI 구현 규칙은 Spring `roles/builder.py`, FastAPI `roles/builder_fastapi.py`, NestJS `roles/builder_nestjs.py`. 빈 로직만 채우는 `--logic`은 `roles/developer.py`가 세 서버 틀 모두(Java·Python·TypeScript 규칙) 한다. 개발하기 창은 '기능 전체 AI'·'빈 로직만 AI'·'AI 안 씀' 중 하나를 고르고, 기능 전체 AI면 화면 스타일 9종(`tools/themes.py`) 중 하나를 고른다. 틀이 없는 층은 `roles/scaffolder.py`가 AI로 뼈대부터 만든다. 서버 쪽은 `app/services/develop.py`·`preview.py`·`repo.py`·`gitpush.py`·`appfiles.py`, API는 `app/api/v1/develop.py`·`repo.py`, 요구사항은 FR-038~044·046.
만들어지는 폴더와 고쳐도 되는 파일은 [OUTPUTS.md 2-1](../OUTPUTS.md#2-1-개발-단계에서-생기는-폴더-develop).

```powershell
cd backend
python -m app develop 고객요구사항4 --from v1.1.1 --plan    # 화면 수와 LLM 예상 비용만 (파일을 쓰지 않음)
python -m app develop 고객요구사항4 --from v1.1.1           # 코드 생성 → OUTPUT_DIR/<프로젝트>/app (토큰 0)
python -m app develop 고객요구사항4 --from v1.1.1 --logic   # 업무 로직 자리까지 LLM으로 채움 (토큰 사용, 바뀐 기능만)
cd output/고객요구사항4/app/frontend; npm install; npm run dev  # 예시 데이터로 모든 화면이 동작
```

- 스펙은 산출물 저장소에서 읽는다 (`DATABASE_URL`이 있으면 서비스 DB, 없으면 `OUTPUT_DIR` 폴더). 코드는 로컬 폴더 `OUTPUT_DIR/<프로젝트>/app`(또는 `--out`)에 쓴다.
- 기술 스택은 최종본의 개발 스펙과 같다. 최종 산출물 화면 '개발 스펙 파일'에서 스택을 확정했으면 그 값을 쓴다.

| 옵션 | 의미 |
| --- | --- |
| `--plan` | 화면·파일 수와 `--logic` 예상 토큰·비용만 보여 준다 |
| `--logic`, `--only FN-…` | 업무 로직 자리를 LLM으로 채운다 (`--only`는 그 기능만). `--provider`·`--model`로 모델 지정, 생각 수준은 `DEV_EFFORT`(기본 medium) |
| `--menu 영역=이름` | 메뉴 이름. 기본은 문서의 영역 이름(`design.json` `meta.group_names`)이고, 없으면 요구사항 제목 |
| `--title 이름` | 화면에 보일 서비스 이름 (기본: 개발 스펙의 사업 이름) |
| `--out 폴더`, `--force` | 코드 폴더 지정, 사람이 고친 파일도 덮어쓰기 |

## 코드 저장소 (검토 중, `app/services/repo.py`)

화면에서 최종 개발하기를 하면 코드 폴더 `OUTPUT_DIR/<프로젝트>/app`이 git 저장소가 된다. 브랜치는 셋이다: **`gen`(AI 생성 결과)**, **`dev`(통합·테스트 배포, 코드 폴더의 작업 브랜치)**, **`main`(고객 납품, 첫 릴리스 때 생김)**. CLI만 돌리면 저장소를 만들지 않는다. 예전 저장소는 처음 열 때 main에서 dev를, dev에서 gen을 만든다.

```
처음:    AI → gen ──자동(같은 커밋)──→ dev ──배정 시 갈라짐──→ task/<묶음> …
재생성:  AI → gen ──합치기 요청 → 다른 개발자 승인──→ dev
사람:    task/<묶음> ──제출 → 다른 개발자 승인──→ dev   (다음에 받는 사람은 이 dev 기준)
릴리스:  dev → main (첫 릴리스가 main을 만든다)
```

- gen·dev·main은 화면에서 고치지 않는다. gen은 최종 개발하기만 커밋한다(작성자 `req-agent`, 태그 `final-v<최종본>`, 같은 최종본을 다시 개발하면 태그를 옮김). 생성기는 gen 작업 폴더 `app/.preview/worktrees/gen`에 쓰므로 사람 코드와 섞이지 않고, 사람이 dev에서 고친 코드와는 git 합치기로 만난다(같은 파일을 둘 다 바꾸면 충돌로 드러남).
- 처음 개발하면 dev를 gen과 같은 커밋으로 만든다. 다시 개발해 바뀐 것이 있으면 gen → dev 합치기 요청이 자동으로 생긴다(요청한 사람 = 개발하기를 누른 개발자, 열린 요청은 하나라 그 사이 또 개발하면 그 요청이 최신 gen으로 바뀜). 다시 개발한 결과는 자동 확인(Docker)을 하지 않으니 합친 뒤 2단계에서 실행해 본다.
- dev가 바뀌는 길: ① 합치기(gen 또는 작업 브랜치) ② 화면 구성 저장. main은 **릴리스**(첫 릴리스면 dev 커밋으로 main을 만들고, 그 뒤엔 dev를 main으로 앞으로 감음, 태그 `release-<날짜>-<n>`)로만 바뀐다.
- 개발자는 브랜치를 dev에서 만든다 (PM은 보기만). 브랜치마다 작업 폴더가 `app/.preview/worktrees/<브랜치, / → __>`에 생기고, 고친 뒤 '저장하기'로 커밋한다(작성자 = 로그인한 회원). 'dev 가져오기'는 dev를 브랜치에 합친다(브랜치에 dev에 없는 커밋이 없으면 합치기 커밋 없이 앞으로 감음).
- 합치기 요청은 프로젝트 파일 `repo.json`에 남고 개발자에게 알림이 간다. **다른 개발자**가 차이를 보고 `git merge --no-ff`로 dev에 합치거나 반려한다(자기 요청은 403, 그 프로젝트의 개발자 담당자가 한 명이면 허용하고 커밋 메시지·기록에 '본인 승인'). 충돌이면 `merge --abort`로 되돌리고 충돌 파일을 알려 준다.
- 커밋하지 않는 것: `.preview/`(미리보기·작업 폴더)·`.req-agent/`(생성기 기록, gen 작업 폴더에 둔다. 'AI가 씀'·'사람이 고침' 판단은 이 기록으로)·`local/`(시연 계정)·패키지·빌드 결과. 생성한 `.gitignore`와 `.git/info/exclude`에 둘 다 적는다.
- 원격은 하나(`git.json` `{url, token}`)이고 버튼으로 **고른 브랜치(dev·main)**를 같은 이름으로, `final-v*`·`release-*` 태그와 함께 올린다(첫 릴리스 전 main은 올리지 않음, gen은 올리지 않음). 원격에 서버에 없는 커밋이 있으면 올리지 않는다(서버 밖에서 정리).

## 작업 묶음·배정·검토 (검토 중, `app/services/work.py`)

최종 개발물 1단계 '작업 묶음'은 코드를 사람에게 나눠 맡기는 단위다. 파일 경로로 정해지고(파일은 모두 한 묶음), 작업 기록은 프로젝트 파일 `work.json`에 둔다.

| 묶음 | 파일 |
|---|---|
| 공통 화면 틀 `common-ui` | frontend의 앱 틀(App·Login·menu·Screen·kit·테마·api)·화면 구성·생성기 관리 파일(`generated/*`, `pages/index.ts`, `menus/index.ts`) |
| 화면 `ui-<메뉴>` | `menus/<메뉴>.tsx`(+ `menus/<메뉴>/` 아래 새 파일), 그 메뉴 기능의 AI 화면 `pages/<기능>.tsx` |
| 공통 서버·DB `common-server` | 인증·공통 코드·조회 API·`db/`·Docker·`.github/`·문서·`spec/` |
| 서버 `fn-<기능>` | 기능의 Controller·Logic·인수 테스트 (FastAPI·NestJS는 `fn_<기능>` 모듈) |

배정 → 작업 브랜치 `task/<묶음>`(그때의 dev에서) → 담당자가 VS Code 확장(별도 저장소 `TEAM4_AGENT_EXTENSION`)이나 웹 코드 보기에서 고쳐 저장 → 제출(묶음 밖 파일이면 거부) → 다른 개발자가 승인(dev에 합침)·반려. 확장은 개인 접속 토큰(`Authorization: Bearer t4_…`)으로 `/develop/work/*` API를 쓴다. 코드 받기 때 서버가 작업 브랜치에 최신 dev(그 사이 다른 사람이 합친 것)를 넣어 준다 — 올린 커밋이 없으면 앞으로 감고, 있으면 합치고, 충돌하면 넣지 않고 알린다.

## 테스트 배포 (생성 앱 `.github/workflows/deploy-dev.yml`)

dev를 원격(GitHub)에 올리면 GitHub Actions가 코드를 테스트 서버로 보내 `APP_HOST=0.0.0.0 docker compose up -d --build`로 다시 띄운다(앱은 `http://<서버>:8080`, 화면·서버가 한 이미지라 Vercel은 필요 없음). 준비:

1. 테스트용 서버(예: EC2)에 Docker(compose 포함)를 설치하고 보안 그룹에서 8080을 연다 (우리 도구 서버와 따로 두는 것을 권장).
2. GitHub 저장소 Settings → Secrets and variables → Actions에 `TEST_HOST`(주소)·`TEST_USER`(예: ubuntu)·`TEST_SSH_KEY`(pem 내용)·`TEST_DIR`(선택, 기본 `app`)을 넣는다. `TEST_HOST`가 없으면 배포를 건너뛴다.
3. 화면 3단계(Git)에서 원격을 설정하고 'dev 올리기'를 누르면 Actions 탭에서 배포가 돈다. main(릴리스)은 배포하지 않는다.

## 결과가 매번 같도록 나눈 것

| 만드는 쪽 | 내용 | 일정함 |
| --- | --- | --- |
| 코드 (`agents/tools/screens.py`·`sample_data.py`·`scaffold.py`, 고정 틀 `agents/templates/app`) | 기능 하나 = 화면(탭) 하나 = 작업 하나로 화면 설계, API 컨트롤러, 공통 DB 처리, 예시 데이터, 수용 기준 테스트 틀 | 같은 스펙이면 파일이 바이트까지 같다 (시각·난수 없음). 모든 화면을 같은 틀 하나가 그린다 |
| LLM (`agents/roles/developer.py`, 지침 `agents/prompts/developer/system.md`) | 기능별 `logic/Fn…Logic.java`의 `// <hole>` 안 Java 본문만 | 입력 지문이 같으면 다시 묻지 않고 저장한 결과(`.req-agent/logic/`)를 쓴다. 받은 코드는 코드로 검사해 틀리면 한 번 다시 묻고, 그래도 틀리면 기본 동작을 두고 '실패'로 보고한다 |

## 업무 동작 (`BEHAVIOR.md`)

상태 전환표·자동 처리·요청에 없는 값의 출처는 최종 확정 때 API 설계와 함께 만든다 (`agents/roles/behavior.py`, 지침 `agents/prompts/behavior/system.md`, 문서당 LLM 요청 1회).
입력(기능·데이터 모델·API)이 이전 최종본과 같으면 다시 묻지 않는다. 고객이 정해야 할 것은 `BQ-…` 질문(최대 `BEHAVIOR_MAX_QUESTIONS`개)으로 남고, 답을 기다리는 항목은 구현하지 않고 `TODO(질문 ID)`로 둔다.

## 화면

- 왼쪽에 업무 영역(메뉴)과 그 영역의 업무를 보여 준다. 첫 화면(업무 홈)에는 처리할 일·공지·업무 영역 목록만 나오고 모든 업무 카드를 나열하지 않는다.
- 같은 영역에서 같은 테이블을 다루는 기능은 **한 화면**으로 합친다(목록 + 오른쪽 패널의 상세·수정 + 등록·처리 버튼). 기능별 탭은 오른쪽 위 '검토 모드'를 켤 때만 보이고, 스펙 ID와 업무 규칙도 그때만 보인다. AI가 기능 전체를 구현한 화면이 있는 업무는 기능별 탭으로 둔다.
- 메뉴 이름·순서·숨김, 업무 이름·숨김, 홈 칸은 화면 구성 `frontend/src/generated/layout.json`을 따른다(웹 1단계 '화면 구성'에서 저장하면 dev 커밋). 구성에 없는 새 화면은 원래 메뉴 끝에 붙는다. 메뉴마다 화면 파일 `frontend/src/menus/<메뉴>.tsx`를 늘 만들고(기본은 공통 메뉴 화면 `menu.tsx`), 화면 작업 담당자가 이 파일을 고친다.
- 문서에 맞춘 값은 코드에 두지 않는다. 메뉴·화면·버튼·상태는 스펙에서 나오고, 코드에 있는 예시 값은 어떤 문서에나 쓰는 일반 값(사람·주소·부서·직급·거래처)뿐이다.
  특정 업무에 맞는 예시 값이 필요하면 프로젝트 파일 `sample_words.json`(`[{"keys": ["프로젝트"], "words": ["…"]}]`, 로컬 저장소면 `OUTPUT_DIR/<프로젝트>/sample_words.json`)을 두면 쓰이고, 이 파일은 git에 넣지 않는다.
- 서버 모드 화면(`VITE_API=server`)은 실제 DB로 동작한다. 참조 값은 ID 대신 이름으로 보이고, 다른 화면에서 등록한 값이 선택 목록에 바로 나온다 (화면 보조 API `/api/ui/lookups`).
  수정 폼의 처음 값은 그 화면이 고칠 수 있는 칸만 수정 API와 같은 역할로 읽는다 (`/api/ui/rows`). 행의 ID를 쓸 수 없는 API(다른 테이블을 고치는 수정·삭제, 부모 ID가 필요한 조회)는 대상을 고르는 칸을 둔다.
- 비밀 컬럼(`*_hash`·`*_enc`)은 화면·API 응답·기본 저장에 넣지 않는다.

## 서버 동작

- 등록·조회·수정·삭제와 상태 바꾸기(승인·반려 등)는 LLM 없이 기본 동작으로 만든다. 상태 변경은 스펙의 상태 전환표대로(허용된 상태에서만), 요청에 없는 값은 스펙의 값 출처대로 채우고,
  스펙에 없는 옛 버전만 동사와 컬럼 모양으로 추정한다. 부모 테이블 값은 JOIN으로 읽는다. 자동 처리는 `--logic`이 업무 로직 자리에 구현한다.
  고객 답을 기다리는 규칙은 구현하지 않고 `TODO(질문 ID)`로 남기고, 검토 모드에 고객 확인 필요로 보여 준다.
- 저장은 요청 하나가 한 묶음이다: 조회가 아닌 API는 `@Transactional`이라 업무 로직·자동 처리에서 오류가 나면 앞선 변경까지 되돌린다.
- 응답 칸이 하나도 없는 조회 API(목록·한 건·요약)는 그 테이블의 컬럼을 보여 준다 (비밀·감사 컬럼 제외). 테이블은 필드·경로 ID·기능의 테이블, 같은 모음 경로를 쓰는 다른 API(`GET /orders` ← `POST /orders`), 경로 이름 순으로 찾고, 생성할 때 어느 API에 채웠는지 알린다.
- 등록 때 요청에 없는 필수 값: 스펙의 값 출처를 따르고, 미정·계산이면 컬럼 모양으로 정한다 (숫자 0, 불린 거짓, 허용값 첫 값, 날짜 현재). 업무 로직이 값을 주면 그 값이 우선한다.
  모양으로도 못 정하는 값(참조 ID, 글자, 조건부 필수)은 DB 오류 대신 `구현 전: API (값 이름)`으로 알린다.
- 상태를 바꾸는 API는 스펙의 값 출처에 적힌 처리자(현재 사용자)·처리 시각(현재 시각)도 상태와 함께 저장한다. API 하나가 여러 상태로 보내면 요청 값이 목표 상태이고 전환표가 허용한 경우만 바꾼다 (`db.moveTo`).
  경로 변수 없는 처리(일괄 처리)는 행이 아니라 화면 위쪽 버튼이다.
- API를 부를 수 있는 주체: 사람은 역할로, 외부 시스템은 `X-Api-Key`로 확인한다 (서버 환경 변수 `SYSTEM_API_KEY`, 정하지 않으면 모두 막힌다). 외부 시스템만 부르는 API는 화면에 두지 않고, 주체가 없는 API도 로그인 없이 열지 않는다.

## 다시 만들기·기술 스택

- 스펙이 바뀌면(새 최종본) 같은 `app` 폴더에 다시 만든다. 바뀐 파일만 달라지고, 사람이 고친 파일은 덮어쓰지 않는다 (`.req-agent/manifest.json`의 해시로 판단). 입력이 달라진 기능만 다시 묻는다.
- 화면(React·TypeScript·Vite)·DB·스펙은 기술 스택과 무관하게 항상 만든다. 백엔드 뼈대는 Spring 계열(Spring Boot, 전자정부 표준프레임워크)만 있고,
  스펙이 다른 스택을 정했으면 스펙과 다른 코드를 내지 않고 백엔드만 빼며 그렇게 알린다. schema.sql은 PostgreSQL 기준이다.

## 업무 로직 채우기 (`--logic`)의 안전장치

LLM이 쓴 코드는 컴파일하기 전에 코드로 검사하고, 틀리면 오류를 붙여 한 번 다시 묻는다 (`agents/roles/developer.py` `check_hook`). 저장해 둔 결과도 지금 검사로 다시 본다.

| 검사 | 왜 |
| --- | --- |
| 주석을 지우고 본다 (주석만 있음·return 없음) | 한 줄에 쓴 본문 앞의 `// 가정 …`이 뒤 코드를 모두 주석으로 만든다 (컴파일 오류) |
| `ctx.user().role()`로 권한을 다시 검사하지 않는다 | 권한은 코드가 메서드 앞에서 이미 검사한다 (역할 이름을 지어내 403이 난다) |
| 날짜·시각 글자를 Java에서 파싱하지 않는다 | 시간대가 붙은 값·빈 값에서 500이 난다. 비교는 SQL의 `CAST(:값 AS timestamptz)` |
| 조회(목록·한 건)는 `crud.get()`을 부른다 | 직접 쿼리로 바꾸면 목록 모양(`items·page·size·total`)과 필드 이름이 달라져 화면이 멈춘다 |
| SQL의 테이블이 스키마에 있다 | `FROM CAST(…)` 같은 함수는 테이블로 보지 않는다 |

코드로 바로잡는 것: LLM 코드의 `Map.of(…)`는 쓸 때 `Db.values(…)`로 바꾼다 (`Map.of`는 값이 null이면 오류, 비워 둔 조회 조건에서 500).
DB 드라이버는 `stringtype=unspecified`로 글자 값(경로의 ID 등)을 컬럼 타입에 맞춘다 (직접 쿼리가 `bigint = varchar`로 실패하지 않게).
화면은 업무 로직이 목록을 배열로 돌려줘도 멈추지 않는다.

## 확인한 것·아직 하지 않은 것

- client_requirement(원본 CLI)에서 확인했다: 고객요구사항13_test v4로 `--logic`(Haiku 5.5, 약 $0.12) 뒤 서버 API 5xx 0·화면 38개 열림, 다른 문서 7건(ITSM·KOBACO·KISA·화물차 등)에서 컴파일·DB·화면 오류 0, 서버 5xx 0.
  이 저장소에서는 가짜 LLM 테스트(`tests/test_develop.py`·`test_behavior.py`)만 돌렸다. 실제 LLM으로 최종본 → `develop --logic`은 아직 이 저장소에서 확인하지 않았다.
- 파라미터를 `:x IS NULL`처럼 타입을 알 수 없는 자리에 쓰는 코드는 아직 걸러내지 못한다.
- 경로 ID가 부모 키인 고치기·처리(`PUT /employees/{id}/personnel-info` 등)는 '구현 전'이다. 부모 키가 유일한(1:1) 테이블은 그 키로 고칠 수 있게 할 만하다.
- 자식 테이블·계산 값이 필요한 조회 API는 응답 일부가 빈다. 테이블을 정할 수 없는 요약 조회(예: 예산 현황)는 '구현 전'이다.
- 공통 규칙 모듈과 실제 인증은 없다 (시연용 헤더). 화면(TEAM4_AGENT_FRONT)에서 `develop`을 실행하는 버튼·API는 없다.
