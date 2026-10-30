# 프로젝트 폴더 구조

기준일: 2026-10-30 · 대상: `TEAM4_AGENT` (백엔드). 프론트는 옆 폴더 `../TEAM4_AGENT_FRONT` (별도 프로젝트, 화면 설명은 그 폴더 README).

- `📁` 폴더 · `📄` 파일. 이름 옆 `#` 뒤가 한 줄 설명이다.
- 실행하면 생기는 것(`.venv/`, `backend/output/`, `backend/data/uploads/`, `__pycache__/`), 비밀 값(`.env`), DB 파일(`*.sqlite`·`*.db`)은 git에 올리지 않는다.
- 서비스 데이터는 docker의 PostgreSQL(`db` 컨테이너, 볼륨 `team4_agent_pgdata`)에 있다. docker 명령은 루트에서 실행한다. `DATABASE_URL`이 없으면 `backend/output/` 폴더를 쓴다.
- 같은 형식이 반복되는 자료(LLM 응답 기록, 골든셋 문서별 폴더)는 패턴으로 줄여 적었다.

## 전체

```text
📁 TEAM4_AGENT/                                   # 요구사항 문서 → 요구사항·기능 명세·ERD·DDL 설계 Agent (단일 프로젝트 루트)
├── 📄 AGENTS.md                                   # 작업 규칙 (문서 관리·구현·검증 원칙)
├── 📄 README.md                                   # 실행 방법·화면 흐름·API·구조 요약
├── 📄 IMPLEMENTATION_STATUS_FOR_FASTAPI.md        # FastAPI 전환 이후 구현 현황·변경 기록
├── 📄 PROJECT_STRUCTURE.md                        # 이 문서: 폴더·파일 구조와 한 줄 설명
├── 📄 OUTPUTS.md                                  # 산출물 안내: 버전별 파일, 파일별 내용, 규칙 표시·질문 ID, 보는 순서
├── 📄 .gitignore                                  # git 제외 목록 (.venv, .env, DB 파일(*.sqlite·*.db), output, uploads …)
├── 📄 compose.yaml                                # docker: db(PostgreSQL 16: 서비스·DDL 검증·테스트 DB, 볼륨 team4_agent_pgdata) + api(backend 이미지)
├── 📄 Dockerfile                                  # 서버 이미지 (backend/ 코드만 넣음, 산출물은 /data/output 볼륨)
├── 📄 .dockerignore                               # 이미지에 backend/만 보내고 .env·.venv·output·uploads·DB 파일 제외
├── 📄 .env                                        # 실제 설정·API 키 하나 (git 제외): LLM·실행 한도·서비스 DB 주소·docker PostgreSQL
├── 📄 .env.example                                # 설정 예시 (POSTGRES_*·LLM·재시도 횟수·의미 검토 방식·서비스 DB·DB 검증·CORS)
├── 📁 docker/postgres-init/
│   └── 📄 01-databases.sql                        # db 컨테이너를 처음 만들 때 reqagent_validation·reqagent_test DB 생성
├── 📄 debug-design                                # 예전 디버그 실행 스크립트 (없는 모듈을 불러 지금은 동작 안 함)
├── 📁 .venv/                                      # 파이썬 가상환경 (git 제외, python3.12 -m venv .venv)
├── 📁 .github/workflows/
│   └── 📄 ci.yml                                  # CI: 의존성 설치 후 pytest (API 키 없이)
├── 📁 docs/plans/                                 # 기획·설계 기준 문서 (옛 ITSM 구현 기준이라 지금 코드와 다른 부분 있음)
│   ├── 📄 README.md                               # 문서 목록·역할·변경 시 함께 볼 문서
│   ├── 📄 PRD.md                                  # 제품 요구사항 문서
│   ├── 📄 TRD.md                                  # 기술 요구사항 문서
│   ├── 📄 요구사항_정의서.md                      # 요구사항 정의 (FR·NFR·BR ID)
│   └── 📄 기능_명세서.md                          # 기능 명세
├── 📁 evals/                                      # (비어 있음 · 평가 코드는 backend/evals/)
├── 📁 mcp_server/                                 # (비어 있음 · 예전 구조에서 남은 폴더)
└── 📁 backend/                                    # 백엔드: 설계 Agent + FastAPI 서버 (서버·CLI는 이 폴더에서 실행)
```

## backend/ 설정·배포 파일

```text
📁 backend/
├── 📄 requirements.txt                            # 파이썬 의존성 (FastAPI·uvicorn·langgraph·anthropic·openai·pdfplumber …)
├── 📄 pyproject.toml                              # 도구 설정 (pytest·ruff)
├── 📄 SPEC_FORMAT.md                              # 개발 스펙 문서 9개의 형식 정의 (spec-format v1, GET /spec-format으로 화면에 보임)
├── 📄 alembic.ini                                 # Alembic 설정 (DB 주소는 .env의 DATABASE_URL)
└── 📁 alembic/                                    # 서비스 DB 마이그레이션
    ├── 📄 env.py                                  # 실행 환경: app/models 기준, LangGraph 체크포인트 테이블은 제외
    ├── 📄 script.py.mako                          # 새 마이그레이션 파일 틀
    └── 📁 versions/
        └── 📄 20261030_634de8920307_service_tables.py   # 첫 마이그레이션: projects·design_versions·artifacts·project_files·jobs
```

## backend/app/ (애플리케이션 코드)

```text
📁 backend/app/
├── 📄 __init__.py                                 # 패키지 표시
├── 📄 __main__.py                                 # `python -m app …` 진입점 (cli.main 호출)
├── 📄 main.py                                     # FastAPI 앱: CORS·오류 처리, /health, 기능별 라우터를 /api/v1에 연결
├── 📄 cli.py                                      # 명령행: run·resume·rerun·review·versions·confirm·diff·parse·standards
│
├── 📁 api/v1/                                     # HTTP API (기능별 파일 하나에 라우터 하나, prefix /api/v1)
│   ├── 📄 __init__.py                             # 패키지 표시
│   ├── 📄 deps.py                                 # 공통 입력 검사: 프로젝트·산출물 값, 진행 중 작업(409)
│   ├── 📄 projects.py                             # GET /projects 프로젝트·버전 목록 · POST /projects/title 버전 제목
│   ├── 📄 artifacts.py                            # GET /design design.json · GET·HEAD /file 산출물 원문 · GET /spec-format 개발 스펙 형식
│   ├── 📄 versions.py                             # POST /projects/versions 새 버전(내보내기·ERD 저장·답 반영) · GET /capabilities
│   ├── 📄 review.py                               # POST /projects/review ERD 최종 확정 → 최종본
│   ├── 📄 runs.py                                 # POST /runs 문서 업로드 → 기능 명세 초안 실행
│   └── 📄 jobs.py                                 # GET /jobs 작업 진행 상황
│
├── 📁 services/                                   # API가 쓰는 앱 서비스
│   ├── 📄 __init__.py                             # 패키지 표시
│   ├── 📄 output_store.py                         # 화면용 산출물 읽기: 프로젝트·버전 목록, design 합치기, 파일, 버전 제목
│   ├── 📄 output_import.py                        # 로컬 output 폴더 → 서비스 DB 가져오기 (import-output, 체크포인트 포함)
│   └── 📁 jobs/                                   # 화면 요청을 CLI 하위 프로세스 작업으로 실행·기록
│       ├── 📄 __init__.py                         # 패키지 공개 이름 (jobs.start_run 등)
│       ├── 📄 common.py                           # 공통: 작업 기록(Job)·실행기(_Runner)·종료 코드·LLM 오류 안내
│       ├── 📄 run.py                              # 문서 업로드 → `run … --until function_designer`
│       ├── 📄 rerun.py                            # 새 버전 → `rerun data_modeler|function_designer` (--design·--data-model·--answers)
│       └── 📄 review.py                           # ERD 최종 확정 → `review` (최종본 v<ERD>.1)
│
├── 📁 schemas/                                    # 데이터 계약 (기능별 입력·출력, pydantic)
│   ├── 📄 __init__.py                             # 패키지 표시
│   ├── 📄 document.py                             # 문서 분석: DocumentBlock(원문 블록)·RequirementSource(추출 단위)
│   ├── 📄 requirement.py                          # 요구사항 추출·검토: Requirement·Question·근거·검토 표시
│   ├── 📄 function.py                             # 기능 명세: FunctionSpec·입출력·예외 흐름
│   ├── 📄 data_model.py                           # 데이터 설계: DataModel·Entity·Attribute·제약·DB 검증 결과
│   ├── 📄 review.py                               # 검토·추적·조정: Issue·TraceLink·CoordinatorDecision
│   └── 📄 version.py                              # 설계 버전: DesignVersion + 화면 요청 본문(새 버전·최종 확정·제목)
│
├── 📁 core/                                       # 공통 기반
│   ├── 📄 __init__.py                             # 패키지 표시
│   ├── 📄 config.py                               # .env 설정 (LLM·단계별 재시도 횟수·의미 검토 방식·서비스 DB·경로·CORS)
│   ├── 📄 database.py                             # 서비스 DB 연결, session_scope(블록 하나 = 트랜잭션 하나)
│   ├── 📄 exceptions.py                           # API 오류(400·404·409·413)와 오류 응답 형식
│   ├── 📄 guards.py                               # 요청 값 검사: 경로 탈출 차단·버전 형식·프로젝트 이름·업로드 크기
│   ├── 📄 logging.py                              # 로그 형식
│   └── 📄 security.py                             # CORS, 접속 URL·API 키 가리기
│
├── 📁 agents/                                     # 설계 Agent (LangGraph)
│   ├── 📄 __init__.py                             # 패키지 표시
│   ├── 📁 flow/                                   # 실행 흐름
│   │   ├── 📄 __init__.py                         # 패키지 표시
│   │   ├── 📄 graph.py                            # LangGraph 그래프 구성·체크포인트 (서비스 DB PostgresSaver, DB 없으면 SQLite)
│   │   ├── 📄 nodes.py                            # Agent 외 노드: 접수·결과 검사·질문·완료 조건·산출물 제공·DB 검증
│   │   ├── 📄 state.py                            # 실행 상태 정의, 이슈 갱신(reconcile_issues)
│   │   └── 📄 rerun.py                            # 단계만 다시 만들기: 기준 버전 읽기·확정 명세(--design)·답 코드 반영
│   ├── 📁 roles/                                  # Agent (판단·LLM 호출)
│   │   ├── 📄 __init__.py                         # 패키지 표시
│   │   ├── 📄 coordinator.py                      # 조정: 다음 행동·범위·재시도 라운드 결정 (규칙 기반)
│   │   ├── 📄 document_analyst.py                 # 문서 분석: 블록·추출 단위·근거 재검색 (LLM 없음)
│   │   ├── 📄 requirements_analyst.py             # 요구사항 추출·분류·의견 반영
│   │   ├── 📄 requirement_reviewer.py             # 요구사항 검토: 원문 대조·충돌, 요구사항→기능 연결 검사
│   │   ├── 📄 function_designer.py                # 기능 명세: 공통 결정·그룹 설계·누락 채우기·확인 질문·답 반영
│   │   ├── 📄 data_modeler.py                     # 데이터 설계: 엔티티 카탈로그·속성 설계·수정·연결 보완
│   │   ├── 📄 trace_reviewer.py                   # 추적 검토: 추적표 생성, 기능→ERD 연결 누락 (LLM 없음)
│   │   ├── 📄 api_designer.py                     # API 설계: 기능별 API·아키텍처 요약·기술 스택 질문, 최종 확정 때만 (바뀐 기능만)
│   │   └── 📄 semantic_reviewer.py                # 의미 검토: ERD↔기능 명세(·기능 명세↔요구사항), 최종 확정 때
│   ├── 📁 prompts/                                # Agent 시스템 프롬프트 (.md 하나에 프롬프트 하나)
│   │   ├── 📄 __init__.py                         # load_prompt(): .md를 읽어 문자열로 (끝 줄바꿈 하나 제거)
│   │   ├── 📁 requirements_analyst/
│   │   │   └── 📄 system.md                       # 요구사항 추출·분류 지침
│   │   ├── 📁 requirement_reviewer/
│   │   │   ├── 📄 source.md                       # 원문 대조 검토 지침
│   │   │   └── 📄 conflict.md                     # 요구사항 충돌 검토 지침
│   │   ├── 📁 function_designer/
│   │   │   └── 📄 system.md                       # 기능 명세 설계 지침
│   │   ├── 📁 data_modeler/
│   │   │   ├── 📄 catalog.md                      # 엔티티 카탈로그 설계 지침
│   │   │   └── 📄 attribute.md                    # 속성(컬럼)·제약 설계 지침
│   │   ├── 📁 api_designer/
│   │   │   ├── 📄 api.md                          # 기능별 REST API 설계 지침
│   │   │   └── 📄 architecture.md                 # 아키텍처 요약·기술 스택 지침
│   │   └── 📁 semantic_reviewer/
│   │       ├── 📄 functions.md                    # 기능 명세 의미 검토 지침 (본문)
│   │       ├── 📄 model.md                        # 데이터 모델 의미 검토 지침 (본문)
│   │       └── 📄 common_rules.md                 # 두 검토 공통 규칙 ({limit} 자리 표시)
│   ├── 📁 llm/                                    # LLM 포트·어댑터
│   │   ├── 📄 __init__.py                         # 패키지 표시
│   │   └── 📄 client.py                           # LLMClient(포트)·Anthropic·OpenAI·Manual 어댑터, get_llm·set_llm, 병렬 호출
│   ├── 📁 storage/                                # 산출물 저장소 포트·어댑터
│   │   ├── 📄 __init__.py                         # get_store·set_store (어댑터 선택·교체)
│   │   ├── 📄 port.py                             # ArtifactStore(포트): 버전 기록·제목·버전 번호·산출물 읽기·쓰기
│   │   ├── 📄 postgres.py                         # PostgresArtifactStore: 서비스 DB 어댑터 (DATABASE_URL이 있을 때)
│   │   └── 📄 local.py                            # LocalArtifactStore: output/<프로젝트>/v<버전>/ 폴더 어댑터 (DB가 없을 때)
│   └── 📁 tools/                                  # 코드 도구 (LLM 없음)
│       ├── 📄 __init__.py                         # 패키지 표시
│       ├── 📄 document_parser.py                  # PDF·DOCX·HWPX·TXT 파싱, 요구사항 표 인식·복구
│       ├── 📄 doc_quality.py                      # 문서 품질 경고, 추출 단위 만들기
│       ├── 📄 chunker.py                          # 표가 없는 본문을 추출 단위로 나누기
│       ├── 📄 search.py                           # 원문 인용 확인·유사도 검색
│       ├── 📄 model_validator.py                  # 데이터 모델 정규화·구조 검사 (PK·FK·이름·제약)
│       ├── 📄 model_edits.py                      # 사람이 고친 데이터 모델 반영 (고정 엔티티·참조 정리)
│       ├── 📄 convention.py                       # 회사별 설계 표준(이름 규칙) 적용, 표준 이름 대응표
│       ├── 📄 erd.py                              # ERD(Mermaid) 그리기
│       ├── 📄 ddl.py                              # PostgreSQL DDL 생성
│       ├── 📄 db_validator.py                     # DDL을 격리 DB(PostgreSQL, 없으면 SQLite)에 적용해 검증
│       ├── 📄 exporter.py                         # 산출물 쓰기: 요구사항·기능 명세·ERD·DDL·추적표·검토 보고서·의미 검토
│       ├── 📄 dev_spec.py                         # 개발 스펙 9개(AGENTS·PRD·ARCHITECTURE·DATABASE·API·openapi·AC·TASKS·DECISIONS) 고정 형식 생성
│       ├── 📄 trace_diff.py                       # 이전 버전 대비 변경과 영향 (추적표 4절·diff)
│       └── 📄 versions.py                         # 버전 번호 규칙·버전 기록(versions.json)
│
├── 📁 models/                                     # 서비스 DB 테이블 (SQLAlchemy ORM, 기능별)
│   ├── 📄 __init__.py                             # 모든 테이블 불러오기 (Alembic이 Base.metadata를 봄)
│   ├── 📄 base.py                                 # Base, 시각 기본값
│   ├── 📄 project.py                              # projects 프로젝트 · project_files 프로젝트 파일(diff 등)
│   ├── 📄 version.py                              # design_versions 버전 번호·버전 기록(JSONB)·화면 제목
│   ├── 📄 artifact.py                             # artifacts 버전별 산출물 파일 내용
│   └── 📄 job.py                                  # jobs 작업 진행 기록
├── 📁 repositories/                               # 서비스 DB 읽기·쓰기 (세션 안에서, 트랜잭션은 session_scope)
│   ├── 📄 __init__.py                             # 저장소 목록·트랜잭션 규칙
│   ├── 📄 project_repository.py                   # 프로젝트 만들기·잠그기·목록, 프로젝트 파일
│   ├── 📄 version_repository.py                   # 버전 번호 잡기(프로젝트 행 잠금)·버전 기록·제목
│   ├── 📄 artifact_repository.py                  # 산출물 읽기·쓰기(덮어쓰기)·있는지
│   └── 📄 job_repository.py                       # 작업 기록 저장·조회, 서버 재시작 때 진행 중 작업 닫기
├── 📁 integrations/                               # (비어 있음 · 예전 구조에서 남은 폴더)
└── 📁 rag/                                        # (비어 있음 · 예전 구조에서 남은 폴더)
```

## backend/ 자료·테스트·평가

```text
📁 backend/
├── 📁 data/                                       # 입력 문서
│   ├── 📄 고객요구사항*.pdf / .hwpx / .docx        # 샘플 고객 요구사항 문서 (1~13)
│   └── 📁 uploads/<시각-ID>/<원본 파일>           # 화면에서 올린 문서 (git 제외)
├── 📁 output/                                     # DATABASE_URL이 없을 때의 산출물 저장소 (git 제외, import-output으로 DB에 가져옴)
│   ├── 📄 checkpoints.sqlite                      # 실행 체크포인트 (resume·rerun의 출발 상태)
│   └── 📁 <프로젝트>/                             # 고객 문서 하나
│       ├── 📄 versions.json                       # 버전 기록 (부모·단계·실행 ID·DB 검증·확정)
│       ├── 📄 titles.json                         # 화면에서 붙인 버전 제목
│       └── 📁 v<버전>/                            # v1 기능 명세 · v1.1 ERD · v1.1.1 최종본
│           ├── 📄 design.json                     # 요구사항·질문·기능 명세·이슈·판단 기록 (화면이 읽는 원본)
│           ├── 📄 requirements.md                 # 요구사항 정의서
│           ├── 📄 functions.md                    # 기능 명세서
│           ├── 📄 data_model.json                 # 데이터 모델 (ERD·DDL의 공통 원본)
│           ├── 📄 erd.mmd · erd.md                # ERD 그림(Mermaid) · 테이블 정의서
│           ├── 📄 schema.sql                      # DDL (PostgreSQL)
│           ├── 📄 traceability.md · .csv          # 추적표 (요구사항→기능→엔티티)
│           ├── 📄 review_report.md                # 검토 보고서 (이슈·판단 기록·LLM 사용량)
│           ├── 📄 db_validation.md                # DB 검증 보고서 (DB 검증을 했을 때)
│           ├── 📄 functions.submitted.md          # ERD를 만들 때 넘긴 확정 기능 명세 (기록용)
│           └── 📄 semantic_review.md · .json      # 의미 검토 결과 (최종본)
├── 📁 standards/                                  # 회사별 설계 표준 프로필 (run --standard)
│   ├── 📄 README.md                               # 프로필 형식·사용법
│   ├── 📁 default/
│   │   └── 📄 rules.yaml                          # 기본 이름 규칙
│   └── 📁 public/
│       ├── 📄 rules.yaml                          # 공공 표준 이름 규칙
│       ├── 📄 words.csv                           # 표준 단어 사전 (한글 → 영문 약어)
│       └── 📄 guide.md                            # 데이터 설계 지침 (프롬프트에 넣는 서술형 규칙)
├── 📁 fixtures/llm_exchange/                      # manual 공급자용 LLM 요청·응답 기록 (API 없이 재생)
│   └── 📁 <문서 세트>/<해시>.request.md · .response.json   # customer4_full · itsm_rfp · kobaco_subset
├── 📁 evals/                                      # Agent 품질 평가 (골든셋)
│   ├── 📄 README.md                               # 평가 방법·지표·결과 해석
│   ├── 📄 __init__.py                             # 패키지 표시
│   ├── 📄 build_golden_docs.py                    # 골든셋 문서·정답 만들기
│   ├── 📄 eval_document_analyst.py                # 문서 분석 평가 (python -m evals.eval_document_analyst)
│   ├── 📄 eval_requirements_analyst.py            # 요구사항 추출 평가
│   └── 📁 golden/                                 # 골든셋
│       ├── 📄 골든셋_목록.json · 검증결과.json     # 골든셋 문서 목록 · 최근 검증 결과
│       ├── 📄 정답_검토표.md · 사용안내.md         # 정답 검토 기록 · 사용 안내
│       └── 📁 doc<N>/                              # 문서별 정답 (doc1·2·3·5·6)
│           ├── 📄 document_analyst.json           # 문서 분석 정답 (블록·추출 단위)
│           ├── 📄 ra_inputs.json                  # 요구사항 Agent 입력
│           └── 📄 requirements_analyst.yaml       # 요구사항 추출 정답
└── 📁 tests/                                      # pytest (가짜 LLM, API 키 없이 실행)
    ├── 📄 conftest.py                             # 공통 설정: 테스트마다 임시 output·설정·LLM·저장소 초기화
    ├── 📄 fake_llm.py                             # 가짜 LLM (스키마별 고정 응답, 호출 횟수 기록)
    ├── 📄 pdf_fixture.py                          # 테스트용 RFP 형식 PDF 생성기
    ├── 📄 test_api.py                             # 화면 API: 목록·design·파일·제목·작업 인자·입력 검사
    ├── 📄 test_core.py                            # core: 경로·값 검사, 비밀 값 가리기, 오류 응답
    ├── 📄 test_storage.py                         # 저장소 포트: 로컬 어댑터, 메모리 어댑터로 전체 흐름
    ├── 📄 test_postgres.py                        # 서비스 DB: 마이그레이션·어댑터·동시 번호·전체 흐름·작업 기록·가져오기 (TEST_DATABASE_URL)
    ├── 📄 test_graph_offline.py                   # 전체 흐름·재질문·누락 채우기·재시도 라운드
    ├── 📄 test_coordinator_stages.py              # 단계별 버전(요구사항→기능 명세→데이터 설계)·확정·diff
    ├── 📄 test_hitl_flow.py                       # 화면 흐름: 초안 멈춤·--design·답 코드 반영·최종본
    ├── 📄 test_dev_spec.py                        # 개발 스펙: 최종본에만 생성·형식 고정·기술 스택 가정·바뀐 기능만·경로 정리
    ├── 📄 test_versions.py                        # 버전 번호·프로젝트 이름
    ├── 📄 test_parsing.py                         # 문서 파서 (PDF·HWPX·DOCX)
    ├── 📄 test_broken_documents.py                # 깨진·나뉜 요구사항 표 복구
    ├── 📄 test_requirements_analyst.py            # 요구사항 추출
    ├── 📄 test_requirement_reviewer.py            # 요구사항 검토 (원문 대조·충돌)
    ├── 📄 test_function_designer.py               # 기능 명세 (그룹·공통 결정·사용자 역할·가정 한도·질문 상한·추천안 확인)
    ├── 📄 test_data_modeler.py                    # 데이터 설계 (카탈로그·속성·사용자 수정·연결 보완)
    ├── 📄 test_trace_reviewer.py                  # 추적 검토
    ├── 📄 test_semantic_reviewer.py               # 의미 검토
    ├── 📄 test_convention.py                      # 설계 표준 적용
    ├── 📄 test_tools.py                           # 코드 도구 (검색·모델 검사·이슈 갱신 등)
    ├── 📄 test_llm.py                             # LLM 클라이언트 (재시도·잘림·사용량)
    └── 📄 test_pg_validation.py                   # PostgreSQL DDL 검증 (DB가 있을 때만)
```
