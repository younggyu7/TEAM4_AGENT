package app.common;

import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;

/** 요청 값 검사: 스펙의 필수 값·허용값을 코드가 만든 컨트롤러가 부른다. */
public final class Checks {
    private Checks() {
    }

    /** 요청 본문·쿼리를 고칠 수 있는 입력 묶음으로 바꾼다 (업무 로직이 값을 채울 수 있다). */
    public static Map<String, Object> input(Map<String, ?> values) {
        return values == null ? new HashMap<>() : new HashMap<>(values);
    }

    public static void required(Map<String, Object> input, String... names) {
        for (String name : names) {
            Object value = input.get(name);
            if (value == null || "".equals(value)) {
                throw new ApiError(400, "VALIDATION_FAILED", "필수 값이 없습니다: " + name);
            }
        }
    }

    public static void oneOf(Map<String, Object> input, String name, String... allowed) {
        Object value = input.get(name);
        if (value != null && !Arrays.asList(allowed).contains(value.toString())) {
            throw new ApiError(400, "VALIDATION_FAILED", "허용되지 않은 값입니다: " + name);
        }
    }
}
