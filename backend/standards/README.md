# 회사별 설계 표준 (프로필)

회사(고객)마다 다른 DB 설계 표준을 이 폴더의 **프로필**로 둔다. 실행할 때 `--standard <프로필>`로 고르면
ERD·테이블 정의서·DDL·DB 검증이 그 표준 이름을 쓴다. 지정하지 않으면 지금 규칙(`default`)이다.

```powershell
python -m app run "고객요구사항5.pdf" --standard public
python -m app standards                 # 프로필 목록과 형식 검사
```

## 프로필 하나 = 폴더 하나

| 파일 | 필수 | 내용 |
| --- | --- | --- |
| `rules.yaml` | 예 | 이름 형식(대소문자)·테이블 접두어·최대 길이·PK·FK 이름·감사 컬럼·타입 규칙 |
| `words.csv` | 이름을 바꿀 때 | 표준 단어 사전. 열: `한글명,영문약어[,영문명,설명]` (엑셀은 'CSV UTF-8'로 저장) |
| `guide.md` | 아니요 | 규칙으로 쓰기 어려운 서술형 지침 (예: 코드성 데이터는 공통코드 테이블로). 데이터 모델 Agent에 그대로 전달 |

## 예시 프로필

- `default`: 지금 규칙 그대로 (이름을 바꾸지 않음)
- `public` 공공기관형: 대문자, `TB_`, 기본키 `{table}_SN`, `REG_DT`·`MDFCN_DT`·`REG_ID`·`MDFCN_ID`, 여부 Y/N, 사람 끝말 `자 → R`.
  표준 단어 576개는 **고객 문서 12개와 4개 도메인(ITSM·KOBACO·KISA 포털·화물차 앱)의 데이터 모델에서 여러 곳에 나오는 일반 단어**로 만든
  예시다 (정부 공통표준용어가 아님). 한 고객에게만 나오는 업무 용어(예: 화물운송종사자격증, 차계부)는 일부러 넣지 않았다 — 고객 표준 사전으로 채운다.

| 데이터 모델 (실제 실행 결과) | 표준 이름 테이블 | 표준 이름 컬럼 | 표준 이름 DDL PostgreSQL 검증 |
| --- | --- | --- | --- |
| ITSM 구축 (69 · 641) | 97% | 95% | 통과 |
| KOBACO 통합유지운영 (8 · 75) | 88% | 100% | 통과 |
| 고객요구사항4 KISA 포털 (26 · 250) | 96% | 96% | 통과 |
| 고객요구사항5 화물차 앱 Haiku (50 · 549) / Sonnet (80 · 770) | 90% / 89% | 96% / 95% | 통과 |

## 어떻게 적용되나

- 설계(LLM)는 지금처럼 영문 이름과 한글 라벨로 한다. **표준 이름은 코드가** 한글 라벨을 단어 사전으로 나눠 조합한다
  (예: `회원 승인 이력` → `TB_MBR_APRV_HST`, `생성 일시` → `CRT_DT`). 토큰이 늘지 않고, 같은 입력이면 항상 같은 이름이 나온다.
- 사전에 없는 단어가 있으면 그 이름은 설계 이름(영문)을 대문자로 쓰고, 검토 보고서 '표준 준수'에 **표준 단어 미등록**으로 올린다.
  사전에 단어를 추가하면 다음 실행부터 표준 이름이 된다.
- `data_model.json`은 설계 이름 그대로 둔다 (다시 설계·사용자 수정의 기준). 표준 이름 대응표는 `standard_names.csv`.
- 대문자 표준(`case: upper`)은 문서·ERD에 `TB_MBR`로 쓰고, PostgreSQL DDL에는 `tb_mbr`로 쓴다
  (PostgreSQL은 따옴표 없는 이름을 소문자로 바꾸므로 쿼리에 따옴표가 필요 없게).

## rules.yaml 항목

| 항목 | 예 | 뜻 |
| --- | --- | --- |
| `name`, `description` | `공공기관형` | 표시 이름과 설명 |
| `case` | `upper` / `lower` | 표준 이름의 대소문자 |
| `table_prefix` | `TB_` | 테이블 이름 앞에 붙임 |
| `table_prefix_by_kind` | `{CODE: TC_, HISTORY: TH_}` | 엔티티 종류(MASTER·TRANSACTION·HISTORY·MAPPING·CODE)별 접두어 (없는 종류는 `table_prefix`) |
| `suffixes` | `{자: R}` | 사전에 없는 낱말의 끝말 규칙: 앞부분이 사전으로 나뉘면 마지막 약어에 붙임 (응답자 → 응답 RSPNS + R → `RSPNSR`) |
| `max_length` | `30` | 이름 최대 길이 (넘으면 '표준 준수'에 위반으로 보고) |
| `primary_key` | `"{table}_SN"` | 기본키 이름 틀. `{table}`은 접두어를 뺀 테이블 표준 이름. 비우면 라벨로 만든다 |
| `foreign_key` | `parent_pk` / `label` | 외래키 이름: 참조 테이블의 기본키 이름을 그대로(역할이 있는 외래키는 라벨로) / 라벨로 |
| `audit_columns.rename` | `created_at: REG_DT` | 시스템이 만드는 감사 컬럼의 표준 이름 |
| `audit_columns.add` | `[{name: REG_ID, ...}]` | 모든 테이블 끝에 더하는 컬럼 |
| `types` | `boolean: {type: varchar, length: 1, values: [Y, N]}` | 논리 타입을 표준 타입으로 |
