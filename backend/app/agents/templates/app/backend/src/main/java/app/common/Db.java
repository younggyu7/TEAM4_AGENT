package app.common;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * 공통 DB 처리. 테이블·컬럼·타입 이름은 코드가 만든 상수(Table, Col)에서만 오고, 값은 모두 바인딩한다.
 * 업무 로직에서 직접 쿼리가 필요하면 query·execute를 쓴다.
 */
@Component
public class Db {
    public static final int DEFAULT_SIZE = 20;
    public static final int MAX_SIZE = 200;
    /** 값 자리에 넣으면 현재 시각이 들어간다. */
    public static final Object NOW = new Object();

    /** 값 자리에 넣으면 날짜와 임의 문자로 만든 기본 번호가 들어간다. 실제 채번 규칙은 업무 로직에서 정한다. */
    public record AutoNo(int length) {
    }

    public static AutoNo autoNo(int length) {
        return new AutoNo(length);
    }

    private static final ObjectMapper JSON = new ObjectMapper();

    private final NamedParameterJdbcTemplate jdbc;

    public Db(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public static Map<String, Object> values(Object... pairs) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (int i = 0; i + 1 < pairs.length; i += 2) {
            out.put((String) pairs[i], pairs[i + 1]);
        }
        return out;
    }

    /** 응답 컬럼 목록. 응답에 컬럼이 하나도 없으면 기본키만 읽는다 (빈 SELECT 목록은 SQL 오류다). */
    private static String selectList(Table t, List<Col> out) {
        List<String> parts = new ArrayList<>();
        for (Col c : out) {
            String column = c.type().equals("jsonb") ? "CAST(" + c.column() + " AS text)"   // JDBC jsonb 값은 JSON으로 나가지 않는다
                    : c.type().equals("yn") ? "(" + c.column() + " = 'Y')" : c.column();
            parts.add(column + " AS \"" + c.field() + "\"");
        }
        return parts.isEmpty() ? t.key() + " AS \"id\"" : String.join(", ", parts);
    }

    private static String bind(String name, String type) {
        return "CAST(:" + name + " AS " + ("yn".equals(type) ? "varchar" : type) + ")";
    }

    /** 표준이 boolean을 Y/N 글자로 바꾼 컬럼: API는 참·거짓, DB는 Y/N이다. */
    private static Object toColumn(Col c, Object value) {
        Object cleaned = clean(value);
        if ("yn".equals(c.type()) && cleaned != null) {
            return Boolean.parseBoolean(cleaned.toString()) || "Y".equals(cleaned) ? "Y" : "N";
        }
        if ("jsonb".equals(c.type()) && cleaned instanceof String text) {
            return jsonText(text);
        }
        return cleaned;
    }

    /** 화면에서 글자로 입력한 값도 jsonb에 들어가게 한다: JSON이 아니면 JSON 문자열로 저장한다. */
    private static String jsonText(String text) {
        try {
            JSON.readTree(text);
            return text;
        } catch (JsonProcessingException notJson) {
            try {
                return JSON.writeValueAsString(text);
            } catch (JsonProcessingException e) {
                throw new ApiError(400, "VALIDATION_FAILED", "요청 형식이 올바르지 않습니다.");
            }
        }
    }

    /** 화면이 비워 보낸 값은 NULL로 저장한다 (빈 문자열은 숫자·날짜로 바꿀 수 없다). */
    private static Object clean(Object value) {
        if (value instanceof Map || value instanceof List) {
            try {
                return JSON.writeValueAsString(value);
            } catch (JsonProcessingException e) {
                throw new ApiError(400, "VALIDATION_FAILED", "요청 형식이 올바르지 않습니다.");
            }
        }
        return "".equals(value) ? null : value;
    }

    private static int intOf(Object value, int fallback) {
        if (value == null) {
            return fallback;
        }
        try {
            return Integer.parseInt(value.toString());
        } catch (NumberFormatException e) {
            throw new ApiError(400, "VALIDATION_FAILED", "숫자가 아닙니다: " + value);
        }
    }

    private String where(List<Col> filters, Map<String, Object> input, Map<String, Object> params) {
        List<String> parts = new ArrayList<>();
        for (Col c : filters) {
            Object value = input.get(c.field());
            if (value == null || "".equals(value)) {
                continue;
            }
            params.put(c.field(), toColumn(c, value));
            parts.add(c.type().equals("varchar") || c.type().equals("text")
                    ? c.column() + " LIKE '%' || :" + c.field() + " || '%'"
                    : c.column() + " = " + bind(c.field(), c.type()));
        }
        return parts.isEmpty() ? "" : " WHERE " + String.join(" AND ", parts);
    }

    /** 목록 조회: 응답 {items, page, size, total} */
    public Map<String, Object> page(Table t, List<Col> out, List<Col> filters, Map<String, Object> input) {
        int page = Math.max(intOf(input.get("page"), 0), 0);
        int size = Math.min(Math.max(intOf(input.get("size"), DEFAULT_SIZE), 1), MAX_SIZE);
        Map<String, Object> params = new HashMap<>();
        String where = where(filters, input, params);
        Long total = jdbc.queryForObject("SELECT count(*) FROM " + t.source() + where, params, Long.class);
        params.put("__limit", size);
        params.put("__offset", page * size);
        List<Map<String, Object>> items = jdbc.queryForList(
                "SELECT " + t.key() + " AS \"__pk\", " + selectList(t, out) + " FROM " + t.source() + where + " ORDER BY " + t.key()
                        + " LIMIT :__limit OFFSET :__offset", params);
        return values("items", items, "page", page, "size", size, "total", total);
    }

    /** 조건 조회 한 건 (요약 화면) */
    public Map<String, Object> first(Table t, List<Col> out, List<Col> filters, Map<String, Object> input) {
        Map<String, Object> params = new HashMap<>();
        String where = where(filters, input, params);
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT " + selectList(t, out) + " FROM " + t.source() + where + " ORDER BY " + t.key() + " LIMIT 1", params);
        return rows.isEmpty() ? new LinkedHashMap<>() : rows.get(0);
    }

    public Map<String, Object> one(Table t, List<Col> out, Object id) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT " + selectList(t, out) + " FROM " + t.source() + " WHERE " + t.key() + " = " + bind("__id", t.pkType()),
                Map.of("__id", id));
        if (rows.isEmpty()) {
            throw ApiError.notFound();
        }
        return rows.get(0);
    }

    /** 코드로 값을 정할 수 없는 필수 컬럼이 있는 등록: 업무 로직이 채우지 않으면 구현 전이다 (DB 제약 오류로 보여 주지 않는다). */
    public Map<String, Object> insert(Table t, List<Col> in, List<Col> out, Map<String, Object> input, Map<String, Object> defaults,
                                      String operation, String... needed) {
        for (String field : needed) {
            if (clean(input.get(field)) == null) {
                throw ApiError.notImplemented(operation + " (" + field + ")");
            }
        }
        return insert(t, in, out, input, defaults);
    }

    public Map<String, Object> insert(Table t, List<Col> in, List<Col> out, Map<String, Object> input, Map<String, Object> defaults) {
        List<String> columns = new ArrayList<>();
        List<String> marks = new ArrayList<>();
        Map<String, Object> params = new HashMap<>();
        for (Col c : in) {
            if (input.containsKey(c.field())) {
                columns.add(c.column());
                marks.add(bind(c.field(), c.type()));
                params.put(c.field(), toColumn(c, input.get(c.field())));
            }
        }
        int number = 0;
        for (Map.Entry<String, Object> entry : defaults.entrySet()) {
            String column = entry.getKey();
            Object value = entry.getValue();
            if (columns.contains(column)) {
                continue;
            }
            columns.add(column);
            if (NOW == value) {
                marks.add("now()");
            } else if (value instanceof AutoNo no) {
                marks.add("left(to_char(now(), 'YYMMDD') || '-' || substr(md5(random()::text), 1, 6), " + no.length() + ")");
            } else {
                marks.add(":__d" + number);
                params.put("__d" + number, value);
                number += 1;
            }
        }
        for (String column : t.created()) {
            columns.add(column);
            marks.add("now()");
        }
        Object id = jdbc.queryForObject("INSERT INTO " + t.name() + " (" + String.join(", ", columns) + ") VALUES ("
                + String.join(", ", marks) + ") RETURNING " + t.pk(), params, Object.class);
        return one(t, out, id);
    }

    public Map<String, Object> update(Table t, List<Col> in, List<Col> out, Object id, Map<String, Object> input) {
        return update(t, in, out, id, input, List.of(), Map.of());
    }

    /** 수정 + 함께 남기는 값 (처리자·처리 시각) */
    public Map<String, Object> update(Table t, List<Col> in, List<Col> out, Object id, Map<String, Object> input, List<Col> cols, Map<String, Object> values) {
        List<String> sets = new ArrayList<>();
        Map<String, Object> params = new HashMap<>();
        params.put("__id", id);
        for (Col c : in) {
            if (input.containsKey(c.field())) {
                sets.add(c.column() + " = " + bind(c.field(), c.type()));
                params.put(c.field(), toColumn(c, input.get(c.field())));
            }
        }
        sets.addAll(stamps(cols, values, params));
        for (String column : t.updated()) {
            sets.add(column + " = now()");
        }
        if (!sets.isEmpty()) {
            int count = jdbc.update("UPDATE " + t.name() + " SET " + String.join(", ", sets) + " WHERE " + t.pk() + " = "
                    + bind("__id", t.pkType()), params);
            if (count == 0) {
                throw ApiError.notFound();
            }
        }
        return one(t, out, id);
    }

    /** 상태 값 바꾸기 (승인·반려 같은 처리의 기본 동작) */
    public Map<String, Object> set(Table t, String column, String value, Object id, List<Col> out) {
        Map<String, Object> input = values("__value", value);
        return update(t, List.of(new Col("__value", column, "varchar")), out, id, input);
    }

    /** 함께 남기는 값의 SET 조각 (처리자·처리 시각). values는 필드 이름 → 값, Db.NOW면 현재 시각이다. */
    private static List<String> stamps(List<Col> cols, Map<String, Object> values, Map<String, Object> params) {
        List<String> out = new ArrayList<>();
        for (Col c : cols) {
            Object value = values.get(c.field());
            if (NOW == value) {
                out.add(c.column() + " = now()");
            } else {
                out.add(c.column() + " = " + bind(c.field(), c.type()));
                params.put(c.field(), toColumn(c, value));
            }
        }
        return out;
    }

    /** 상태를 바꾸는 SET 절: 상태, 함께 남기는 값, 수정 시각 */
    private static String stateSets(Table t, String state, List<Col> cols, Map<String, Object> values, Map<String, Object> params) {
        List<String> sets = new ArrayList<>(List.of(state));
        sets.addAll(stamps(cols, values, params));
        for (String column : t.updated()) {
            sets.add(column + " = now()");
        }
        return String.join(", ", sets);
    }

    /** 상태 값 바꾸기 + 처리자·처리 시각 */
    public Map<String, Object> set(Table t, String column, String value, Object id, List<Col> out, List<Col> cols, Map<String, Object> values) {
        Map<String, Object> params = new HashMap<>(Map.of("__value", value, "__id", id));
        int count = jdbc.update("UPDATE " + t.name() + " SET " + stateSets(t, column + " = :__value", cols, values, params)
                + " WHERE " + t.pk() + " = " + bind("__id", t.pkType()), params);
        if (count == 0) {
            throw ApiError.notFound();
        }
        return one(t, out, id);
    }

    /** 상태 전환: 허용된 현재 상태에서만 바꾼다 (BEHAVIOR.md 1절). 다른 상태면 409로 거절하고 값을 바꾸지 않는다. */
    public Map<String, Object> move(Table t, String column, List<String> from, String to, Object id, List<Col> out) {
        return move(t, column, from, to, id, out, List.of(), Map.of());
    }

    public Map<String, Object> move(Table t, String column, List<String> from, String to, Object id, List<Col> out, List<Col> cols, Map<String, Object> values) {
        Map<String, Object> params = new HashMap<>(Map.of("__to", to, "__id", id, "__from", from));
        int count = jdbc.update("UPDATE " + t.name() + " SET " + stateSets(t, column + " = :__to", cols, values, params)
                + " WHERE " + t.pk() + " = " + bind("__id", t.pkType()) + " AND " + column + " IN (:__from)", params);
        if (count == 0) {
            one(t, out, id);   // 대상이 없으면 404
            throw new ApiError(409, "INVALID_STATE", "지금 상태에서는 처리할 수 없습니다.");
        }
        return one(t, out, id);
    }

    /** API 하나가 여러 상태로 보내는 전환: 요청 값이 목표 상태이고, allowed는 목표 → 허용된 현재 상태다. 표에 없는 목표는 409다. */
    public Map<String, Object> moveTo(Table t, String column, Map<String, List<String>> allowed, Object to, Object id, List<Col> out, List<Col> cols, Map<String, Object> values) {
        List<String> from = allowed.get(String.valueOf(to));
        if (from == null) {
            throw new ApiError(409, "INVALID_STATE", "지금 상태에서는 처리할 수 없습니다.");
        }
        return move(t, column, from, to.toString(), id, out, cols, values);
    }

    public Map<String, Object> delete(Table t, Object id) {
        int count = jdbc.update("DELETE FROM " + t.name() + " WHERE " + t.pk() + " = " + bind("__id", t.pkType()), Map.of("__id", id));
        if (count == 0) {
            throw ApiError.notFound();
        }
        return new LinkedHashMap<>();
    }

    public List<Map<String, Object>> query(String sql, Map<String, ?> params) {
        return jdbc.queryForList(sql, params);
    }

    public int execute(String sql, Map<String, ?> params) {
        return jdbc.update(sql, params);
    }
}
