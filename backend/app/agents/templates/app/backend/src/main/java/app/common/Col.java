package app.common;

/** API 필드 ↔ DB 컬럼. type은 PostgreSQL 타입 이름이고 코드가 만든 상수에서만 온다. */
public record Col(String field, String column, String type) {
}
