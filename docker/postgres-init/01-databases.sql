-- db 컨테이너를 처음 만들 때 한 번 실행된다 (볼륨이 비어 있을 때만).
-- reqagent             서비스 DB (POSTGRES_DB로 이미 만들어짐): 산출물·버전 기록·작업 기록·실행 체크포인트
-- reqagent_validation  Agent가 만든 DDL을 적용해 보는 검증용 DB (검증마다 임시 스키마를 만들고 지운다)
-- reqagent_test        pytest의 PostgreSQL 테스트용 DB (테스트가 테이블을 만들고 지운다)
CREATE DATABASE reqagent_validation;
CREATE DATABASE reqagent_test;
