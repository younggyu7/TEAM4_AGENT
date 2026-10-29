# TRD

- 버전·최종 수정일: 1.5 · 2026-10-19
- 상태: 개발팀 검토용 현재 구조

## 현재 범위

FastAPI는 프로젝트 생성, PDF·DOCX·UTF-8 TXT 업로드, 단계별 설계 실행, 질문 답변·재개, 결과와 산출물 제공을 지원한다. 웹 UI, 실제 모델 품질 평가, 다중 프로세스 작업자, 긴 문서 입력 예산 분할은 아직 구현·검증하지 않았다.

실행은 문서 분석 → 요구사항 분석 → 요구사항 리뷰 → 사용자 확인 → 기능 설계 → 데이터 설계 → 구조 검사 → ERD·DDL·추적 생성 → 결정적 검토 → 선택적 격리 DB 검증 → 제공 순서다. 실패는 실행 상태에 남으며 성공 결과로 대체하지 않는다.

## 구조 결정

설계 모델 검증과 ERD·DDL 생성은 `app/agents/modeling/`에 둔다. 문서 파일·운영 DB·산출물 파일·검증 DB 접근은 `app/services/`가 담당하고, 운영 DB 모델은 `app/models/`에 둔다. `app/domain/`·`app/infrastructure/` 같은 추가 계층은 현재 규모에 맞지 않아 만들지 않는다. 7개 역할은 `app/agents/roles/`에 유지한다. 실행 전이는 `flow/transitions.py`, 단계 구현은 `flow/stages.py`, 그래프 선언은 `flow/graph.py`, 실행 선점은 `flow/runner.py`가 각각 맡는다.

생성 DDL은 `VALIDATION_DATABASE_URL`이 비어 있으면 `not_run`인 미검증 초안이다. 운영 DB와 검증 DB는 반드시 다르게 설정한다.

## 검증

`backend/tests/test_core.py`의 두 테스트는 업로드 원문·위치 보존, 정상 모델 호출의 이력 저장, 분석 실패의 결과 비공개를 확인한다. 실제 Anthropic과 격리 PostgreSQL 검증은 배포 전 별도로 수행한다.
