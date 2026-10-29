# ITSM 요구사항 기반 설계 에이전트

PDF·DOCX·TXT 요구사항 문서를 받아 요구사항, 기능, 데이터 모델, ERD·DDL, 추적표와 검토 결과 생성

## 시작하기

### macOS / Linux

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# .env에 ANTHROPIC_API_KEY, DATABASE_URL, STORAGE_ROOT를 설정
python -m uvicorn app.main:app --reload
```

### Windows PowerShell

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# .env에 ANTHROPIC_API_KEY, DATABASE_URL, STORAGE_ROOT를 설정
python -m uvicorn app.main:app --reload
```

`INITIALIZE_DATABASE=true`로 설정하면 시작 시 Alembic 마이그레이션을 적용합니다. 개발 서버는 단일 작업자만 지원합니다. API 계약은 `http://127.0.0.1:8000/docs`에서 확인합니다.

실행 순서는 프로젝트 생성 → 문서 업로드 → 실행 시작 → 질문 답변 또는 미해결 처리 → 실행 재개 → 결과·산출물 조회입니다. 생성 DDL은 `VALIDATION_DATABASE_URL`을 별도로 설정했을 때만 격리 PostgreSQL에서 검증합니다.

## 구조

```text
backend/app/
├── api/             # HTTP 요청·응답
├── agents/
│   ├── roles/       # 7개 설계 역할
│   ├── flow/        # 전이·단계·그래프·실행기
│   ├── prompts/     # 역할별 프롬프트
│   ├── llm/         # 모델 호출
│   ├── modeling/    # 구조 검사와 ERD·DDL·추적 생성
│   └── tools/examples/ # 새 도구 작성 예시
├── models/          # 운영 DB ORM
├── schemas/         # 단계·HTTP 계약
└── services/        # 문서·DB·파일·격리 DB 접근
```

7개 에이전트는 `agents/roles/`의 `coordinator`, `document_analyst`, `requirements_analyst`, `requirement_reviwer`, `function_designer`, `data_modeler`, `trace_reviewer`입니다. `legacy/`와 `nodes/`는 없습니다.

기존 단일 텍스트 `/api/v1/design` API, 전용 LangGraph Studio 설정, 기준 시나리오와 디버그 도구는 새 워크플로와 중복되어 제거했습니다.

## 검증

```bash
cd backend
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

남은 두 테스트는 업로드 원문·위치 보존, 정상 모델 호출 이력 저장, 분석 실패의 결과 비공개를 확인합니다. 실제 Anthropic 출력 품질과 외부 PostgreSQL 검증은 별도로 수행해야 합니다. Docker 실행은 `/data` 볼륨에 문서·산출물을 보관합니다.

기획·요구사항과 남은 구현 범위는 [문서 관리표](docs/plans/README.md)를 확인하세요.
