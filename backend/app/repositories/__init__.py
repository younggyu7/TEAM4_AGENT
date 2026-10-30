"""서비스 DB 저장소 (기능별 파일, SQLAlchemy 세션 안에서 쓰는 함수).

트랜잭션은 부르는 쪽이 core.database.session_scope()로 연다 (블록 하나 = 트랜잭션 하나).
  project_repository   프로젝트·프로젝트 파일 (프로젝트 행 잠금 포함)
  version_repository   설계 버전 번호 잡기·버전 기록·제목
  artifact_repository  버전별 산출물 파일 내용
  job_repository       작업 진행 기록
"""
